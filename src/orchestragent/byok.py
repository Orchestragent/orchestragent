"""Bring your own key: any OpenAI-compatible chat-completions API as an orch provider.

`orch providers add NAME --base-url URL [--key-env VAR]` registers an endpoint (OpenAI,
DeepSeek, Qwen/DashScope, Moonshot, Zhipu, OpenRouter, a local Ollama or vLLM, ...);
the key comes from that environment variable or from `orch keys NAME`. A role then uses
`NAME/model:effort` (see orch_models). One request per call, no tools, the answer must be
one JSON object; usage is read from the response. Standard library only.

You are responsible for using each provider within its terms (see DISCLAIMER.md).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestragent.native import NativeResult

# Provider names orch already uses for built-in adapters.
BUILT_IN = frozenset({"claude", "codex", "cursor", "dsh", "kimi"})
NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
JSON_ONLY = (
    "You are a component of an automated build system. Answer with exactly one JSON object "
    "that matches the requested schema: no prose, no markdown, no code fences."
)


def registry_path(data_dir: Path) -> Path:
    return data_dir / "providers.json"


def registry(data_dir: Path) -> dict[str, dict[str, Any]]:
    path = registry_path(data_dir)
    if not path.is_file():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a JSON object of name -> provider")
    return {name: entry for name, entry in value.items()
            if NAME.fullmatch(name) and isinstance(entry, dict)}


def add(data_dir: Path, name: str, base_url: str, key_env: str = "",
        max_tokens: int = 0) -> str:
    if not NAME.fullmatch(name) or name in BUILT_IN:
        raise ValueError("name: lower-case letters, digits, - and _; not a built-in provider")
    if not re.fullmatch(r"https?://[^\s]+", base_url):
        raise ValueError("base_url must be an http(s) URL, e.g. https://api.openai.com/v1")
    if base_url.startswith("http://") and not re.match(
            r"http://(localhost|127\.0\.0\.1|\[::1\])[:/]", base_url):
        raise ValueError("plain http is allowed only for a local server (localhost)")
    if key_env and not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", key_env):
        raise ValueError("key_env must be an environment variable name")
    if type(max_tokens) is not int or not 0 <= max_tokens <= 1_000_000:
        raise ValueError("max_tokens must be 0 (provider default) to 1000000")
    entries = registry(data_dir)
    entries[name] = {"type": "openai-compatible", "base_url": base_url.rstrip("/"),
                     **({"key_env": key_env} if key_env else {}),
                     **({"max_tokens": max_tokens} if max_tokens else {})}
    path = registry_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2) + "\n")
    where = f"${key_env}" if key_env else f"`orch keys {name}`"
    return f"registered {name} -> {base_url} (key from {where}); use it as {name}/<model>"


def remove(data_dir: Path, name: str) -> str:
    entries = registry(data_dir)
    if entries.pop(name, None) is None:
        raise ValueError(f"no provider named {name}")
    registry_path(data_dir).write_text(json.dumps(entries, indent=2) + "\n")
    return f"removed {name}"


def api_key(entry: dict[str, Any], stored: Callable[[], str]) -> str:
    env = entry.get("key_env")
    return (os.environ.get(env, "") if isinstance(env, str) and env else "") or stored()


def _content(text: str) -> object:
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    return json.loads(fenced.group(1) if fenced else text)


def call(entry: dict[str, Any], key: str, model: str, prompt: str,
         schema: dict[str, object], cancelled: Callable[[], bool], timeout: float,
         effort: str = "") -> NativeResult:
    """One chat-completions request whose answer must be a JSON object."""
    if not key:
        return NativeResult("auth_required", {}, usage={"forwarded": 0},
                            message="API key missing (key_env or orch keys)")
    if cancelled():
        return NativeResult("cancelled", {}, usage={"forwarded": 0})
    task = (f"{prompt}\n\nRespond with ONE JSON object matching this JSON schema:\n"
            f"{json.dumps(schema, separators=(',', ':'))}")
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": JSON_ONLY},
                     {"role": "user", "content": task}],
        "response_format": {"type": "json_object"},
    }
    if entry.get("max_tokens"):
        body["max_tokens"] = entry["max_tokens"]
    if effort in {"low", "medium", "high"}:
        body["reasoning_effort"] = effort
    request = urllib.request.Request(
        f"{entry['base_url']}/chat/completions", data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST")
    outcome: dict[str, Any] = {}

    def send() -> None:
        try:
            with urllib.request.urlopen(request, timeout=timeout or None) as response:
                outcome["body"] = response.read()
        except urllib.error.HTTPError as error:
            outcome["status"] = error.code
        except (TimeoutError, urllib.error.URLError, OSError) as error:
            outcome["error"] = type(error).__name__

    worker = threading.Thread(target=send, daemon=True)
    worker.start()
    while worker.is_alive():
        if cancelled():
            return NativeResult("cancelled", {}, message="cancelled; the request may be billed")
        worker.join(0.2)
    if "error" in outcome:
        return NativeResult(f"exception:{outcome['error']}", {}, usage={"forwarded": 1})
    status = outcome.get("status")
    if status is not None:
        kind = {401: "auth_required", 403: "auth_required", 402: "quota_exhausted",
                429: "rate_limited"}.get(status, "failed")
        usage: dict[str, object] = {"forwarded": 1, **({"rejected": status}
                                                      if status < 500 else {})}
        return NativeResult(kind, {}, usage=usage, message=f"upstream HTTP {status}")
    try:
        reply = json.loads(outcome["body"])
        choice = reply["choices"][0]
        text = choice["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError):
        return NativeResult("failed", {}, usage={"forwarded": 1},
                            message="Invalid JSON result: not a chat completion")
    raw = reply.get("usage") or {}
    usage = {"forwarded": 1, "served_model": str(reply.get("model", model))[:128]}
    for source, target in (("prompt_tokens", "input_tokens"),
                           ("completion_tokens", "output_tokens")):
        if type(raw.get(source)) is int:
            usage[target] = raw[source]
    cached = (raw.get("prompt_tokens_details") or {}).get("cached_tokens")
    if type(cached) is int:
        usage["cache_read_input_tokens"] = cached
    if choice.get("finish_reason") == "length":
        usage["stop_reason"] = "max_tokens"
    try:
        data = _content(text)
    except ValueError:
        return NativeResult("failed", {}, usage=usage, text=text[:20000],
                            message="Invalid JSON result from the provider")
    if not isinstance(data, dict):
        return NativeResult("failed", {}, usage=usage, message="Invalid JSON result: not an object")
    return NativeResult("ok", data, usage=usage)


def probe(entry: dict[str, Any], key: str, model: str, timeout: float = 60) -> str:
    """A tiny real request, for `orch providers test`."""
    started = time.monotonic()
    result = call(entry, key, model, "Reply with the JSON object {\"ok\": true}.",
                  {"type": "object", "properties": {"ok": {"type": "boolean"}},
                   "required": ["ok"]}, lambda: False, timeout)
    return (f"{result.outcome} in {time.monotonic() - started:.1f}s"
            + (f": {result.message}" if result.message else "")
            + (f" (tokens in {result.usage.get('input_tokens')} out "
               f"{result.usage.get('output_tokens')})" if result.outcome == "ok" else ""))
