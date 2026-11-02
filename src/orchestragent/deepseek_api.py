"""Direct DeepSeek API transport for the pinned builder route, for machines without the
DSH CLI (bring your own key).

It sends the one request the DSH guard would allow, from this process, to DeepSeek's
Anthropic-compatible endpoint: the model is fixed before anything leaves the process,
no tools are offered, the answer must be one JSON object, and usage comes back in the
fields the budget ledger settles on. Explicitly rejected requests report `rejected` so the
caller can release the reservation; anything that may have been billed keeps it.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from orchestragent.access import REJECTED_STATUSES
from orchestragent.dsh import DshClient, cls_text, json_failure
from orchestragent.dsh_guard import STOP_REASONS, request_shape
from orchestragent.native import NativeResult
from orchestragent.serialization import dumps

ENDPOINT = "https://api.deepseek.com/anthropic/v1/messages"
USAGE = ("input_tokens", "output_tokens", "cache_read_input_tokens",
         "cache_creation_input_tokens")


def endpoint() -> str:
    """The official endpoint; tests point ORCHESTRA_DEEPSEEK_URL at a local fake."""
    return os.environ.get("ORCHESTRA_DEEPSEEK_URL", ENDPOINT)


def call(
    api_key: str,
    model: str,
    prompt: str,
    schema: dict[str, object],
    cancelled: Callable[[], bool],
    timeout: float,
    log_path: Path | None = None,
    effort: str = "",
) -> NativeResult:
    if model not in DshClient.models:
        return NativeResult("policy_blocked", {}, usage={"forwarded": 0},
                            message=f"model {model!r} is not a pinned DeepSeek route")
    if not api_key or len(api_key) > 4096 or any(char.isspace() for char in api_key):
        return NativeResult("auth_required", {}, usage={"forwarded": 0},
                            message="Authorized DeepSeek API key required")
    task = DshClient.task_text(prompt, schema)
    if not prompt.strip() or len(task.encode("utf-8")) > DshClient.input_limit:
        return NativeResult("failed", {}, usage={"forwarded": 0}, message="Invalid request")
    # Same generation settings as the DSH CLI profile, sent explicitly, never left to defaults.
    limit, thinking, reasoning = DshClient.output_settings(effort)
    body = dumps({"model": model, "max_tokens": limit, "thinking": {"type": thinking},
                  **({"output_config": {"effort": reasoning}} if thinking == "enabled" else {}),
                  "messages": [{"role": "user", "content": task}]}).encode()
    request = urllib.request.Request(
        endpoint(), body, method="POST",
        headers={"content-type": "application/json", "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"},
    )
    outcome: dict[str, object] = {}

    def send() -> None:
        try:
            with urllib.request.urlopen(request, timeout=timeout or 900) as response:
                outcome["status"], outcome["body"] = response.status, response.read()
        except urllib.error.HTTPError as error:
            outcome["status"], outcome["body"] = error.code, error.read()[:4096]
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            outcome["error"] = str(error)[:200]

    worker = threading.Thread(target=send, daemon=True)
    worker.start()
    deadline = time.monotonic() + (timeout or 900) + 5
    while worker.is_alive():
        if cancelled() or time.monotonic() > deadline:
            # The request may still be processed and billed: keep the reservation.
            return NativeResult("cancelled" if cancelled() else "timeout", {},
                                usage={"forwarded": 1})
        worker.join(0.2)
    if "error" in outcome:
        return NativeResult("failed", {}, usage={"forwarded": 1},
                            message=f"DeepSeek API unreachable: {outcome['error']}")
    status = int(str(outcome["status"]))
    raw = bytes(outcome["body"]) if isinstance(outcome["body"], bytes) else b""
    _log(log_path, status, raw)
    if status != 200:
        kind = {401: "auth_required", 403: "auth_required", 402: "quota_exhausted",
                429: "rate_limited"}.get(status, "failed")
        usage: dict[str, object] = {"forwarded": 1}
        if status in REJECTED_STATUSES:
            usage["rejected"] = status  # refused before any inference: nothing billed
        detail = raw[:200].decode("utf-8", "replace")
        return NativeResult(kind, {}, usage=usage, message=f"DeepSeek API {status}: {detail}")
    result = decode(raw, limit)
    # The body this client sent is the wire request: its own shape, not a constant.
    return replace(result, usage={**request_shape(body), **result.usage})


def decode(raw: bytes, limit: int = 8192) -> NativeResult:
    """One Anthropic-format message -> the JSON object the builder had to return."""
    try:
        message = json.loads(raw)
    except (ValueError, UnicodeError):
        message = None
    if not isinstance(message, dict):
        return NativeResult("failed", {}, usage={"forwarded": 1},
                            message="Invalid DeepSeek response")
    counts = message.get("usage")
    usage: dict[str, object] = {"forwarded": 1}
    if isinstance(counts, dict):
        usage.update({key: counts[key] for key in USAGE if type(counts.get(key)) is int})
    if isinstance(message.get("model"), str):
        usage["served_model"] = str(message["model"])[:80]
    stop = message.get("stop_reason")
    if isinstance(stop, str):
        usage["stop_reason"] = stop if stop in STOP_REASONS else "other"
    session = str(message.get("id", ""))[:128]
    if stop == "max_tokens":
        return NativeResult("failed", {}, session, usage,
                            f"answer cut at {limit} tokens; stop=max_tokens")
    blocks = message.get("content")
    text = "".join(
        str(block.get("text", "")) for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ) if isinstance(blocks, list) else ""
    try:
        data = json.loads(cls_text(text))
    except (ValueError, UnicodeError) as error:
        return NativeResult("failed", {}, session, usage,
                            f"Invalid DeepSeek JSON result: {str(error)[:120]}; "
                            f"{json_failure(text)[0]}", text=text)
    if not isinstance(data, dict):
        return NativeResult("failed", {}, session, usage, "Expected a JSON object")
    return NativeResult("ok", data, session, usage)


def _log(path: Path | None, status: int, raw: bytes) -> None:
    """The same shape the DSH guard logs: status, size and the served model only."""
    if path is None:
        return
    served = ""
    try:
        value = json.loads(raw)
        served = str(value.get("model", "")) if isinstance(value, dict) else ""
    except ValueError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"api /v1/messages {status} {len(raw)} {served[:80]}\n")
