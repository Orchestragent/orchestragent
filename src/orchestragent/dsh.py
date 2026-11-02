"""Internal DSH API transport; caller must authorize credentials and budget first."""

import json
import re
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from orchestragent.access import REJECTED_STATUSES
from orchestragent.dsh_guard import bound_transport
from orchestragent.native import NativeClients, NativeResult
from orchestragent.platform import dsh_binary
from orchestragent.serialization import dumps


def cls_text(value: object) -> str:
    """Accept a bare JSON object, a fenced block, or one surrounded by prose."""
    if not isinstance(value, str):
        raise ValueError("Expected final text")
    body = value.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1]
        if "```" in body:
            body = body.rsplit("```", 1)[0]
        body = body.strip()
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("No JSON object")
    return body[start : end + 1]


def json_failure(text: str) -> tuple[str, str]:
    """Where a settled answer stops being JSON, and a bounded excerpt around that point.

    Diagnoses only; the answer is never repaired or guessed. object_chars versus
    error_char separates an early cut (truncation or brace slicing) from a bad escape.
    """
    try:
        body = cls_text(text)
    except ValueError:
        return f"final_chars={len(text)}; no JSON object", text[-600:]
    try:
        json.loads(body)
    except json.JSONDecodeError as error:
        trailing = len(text) - text.find(body) - len(body)
        return (f"final_chars={len(text)}; object_chars={len(body)}; "
                f"error_char={error.pos}; trailing_chars={trailing}",
                body[max(0, error.pos - 400):error.pos + 200])
    except ValueError:
        pass
    return f"final_chars={len(text)}; object_chars={len(body)}", ""


TURN_ENDS = ("completed", "max-tokens", "blocked", "aborted", "error")
# Effort suffix for the one wider output budget used only after a provider-confirmed
# max_tokens stop. 131072 stays under the model's documented 393216 output maximum, takes
# about 10-14 minutes at the 160-220 tokens/s measured for this route, and costs at most
# 131 fen of output at deepseek-flash rates.
EXTENDED = "-extended"
EXTENDED_OUTPUT_TOKENS = 131_072


class DshClient:
    # The provider supplies a 1M-token window (input + output). Bytes are NOT tokens:
    # this generous transport guard only bounds local payload/memory, not model context.
    # The provider enforces the token limit, including its prompt template and output.
    context_window_tokens = 1_048_576
    input_limit = 16 * 1024 * 1024
    models = frozenset({"deepseek-flash", "deepseek-v4-pro"})
    # Required services verified against the installed official 0.2.1-rc.2 source.
    # Everything else in the current shipped profile is disabled, including future tools.
    core = {
        "timer": "@deepseek-ai/cordis-plugin-timer",
        "llm": "@deepseek-ai/dsh-llm",
        "session": "@deepseek-ai/dsh-session",
        "agent": "@deepseek-ai/dsh-agent",
        "agent-default-model": "@deepseek-ai/dsh-agent-default-model",
        "session-projection": "@deepseek-ai/dsh-session-projection",
        "tools": "@deepseek-ai/dsh-tools",
        "system-prompt": "@deepseek-ai/dsh-system-prompt",
        "agent-loop": "@deepseek-ai/dsh-agent-loop",
        "llm-deepseek": "@deepseek-ai/dsh-llm-deepseek-api-key",
        "headless-startup": "@deepseek-ai/dsh-headless/startup",
        "headless-runner": "@deepseek-ai/dsh-headless",
    }

    # The desktop account grant needs these official credential plugins mounted.
    account_plugins = {
        "authorization": "@deepseek-ai/dsh-authorization",
        "deepseek-account": "@deepseek-ai/dsh-deepseek-account-platform",
        "credentials": "@deepseek-ai/dsh-credentials-local",
        "llm-deepseek-account": "@deepseek-ai/dsh-llm-deepseek-account",
    }

    @staticmethod
    def output_settings(effort: str) -> tuple[int, str, str]:
        """(max_tokens, thinking, reasoning effort) for one DSF effort, for both transports.

        The official DSH request builder sends thinking.type and, unless off,
        output_config.effort; max_tokens bounds reasoning plus the visible answer.
        """
        base = effort.removesuffix(EXTENDED)
        reasoning = base if base in {"low", "high", "max"} else "off"
        if reasoning == "off":
            # Without thinking the visible answer is the whole output; the wide tier keeps a
            # long test file or implementation from being cut.
            return (EXTENDED_OUTPUT_TOKENS if effort == "off" + EXTENDED else 8192,
                    "disabled", "off")
        if effort.endswith(EXTENDED):
            return EXTENDED_OUTPUT_TOKENS, "enabled", reasoning
        # Every thinking effort shares the max limit: run r2b588c22 hit 16384 (reasoning
        # plus answer) on three of four first high test-design calls with
        # stop_reason=max_tokens, and an implementation answer alone reached 30k tokens.
        return 32768, "enabled", reasoning

    @staticmethod
    def task_text(prompt: str, schema: dict[str, object]) -> str:
        """The builder prompt plus the one-JSON-object answer contract (both transports)."""
        return (
            prompt
            + "\n\nRespond with ONE JSON object matching this schema. "
            "No prose, no markdown, no code fences; the first character must be { "
            "and the last must be }. Source code inside a JSON string keeps its own escapes "
            "and is then JSON-escaped: every \\ becomes \\\\, every \" becomes \\\", "
            "every newline becomes \\n (Python '\\n' is written \"'\\\\n'\"). "
            "UTF-8 text is fine.\n"
            + dumps(schema)
        )

    def __init__(self, native: NativeClients, root: Path, account: bool = False):
        self.native = native
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.account = account and (Path.home() / ".dsh").is_dir()
        self.executable = dsh_binary()

    @classmethod
    def _patch(
        cls,
        default: bytes,
        model: str,
        base_url: str = "https://api.deepseek.com/anthropic",
        provider: str = "deepseek-official",
        extra: dict[str, str] | None = None,
        effort: str = "",
    ) -> list[dict[str, object]]:
        # Only accept the CLI's flat, generated entry-list format; never evaluate YAML !!js.
        text = default.decode("utf-8")
        rows = re.findall(r"(?m)^- id: ([a-z0-9-]+)\n  name: '?(@[a-z0-9-]+/[a-z0-9/-]+)'?$", text)
        entries = dict(rows)
        if (
            not rows
            or len(rows) != len(entries)
            or len(rows) != len(re.findall(r"(?m)^- ", text))
            or any(entries.get(key) != package for key, package in cls.core.items())
            or any(
                entries.get(key) != package
                for key, package in (extra or {}).items()
            )
        ):
            raise ValueError("Unsupported DSH default profile")
        enabled = set(cls.core) | set(extra or {})
        limit, thinking, reasoning = cls.output_settings(effort)
        patches: list[dict[str, object]] = [
            {"id": key, "disabled": key not in enabled} for key in entries
        ]
        patches.extend(
            [
                {
                    "id": "agent-default-model",
                    "config": {"provider": provider, "model": model},
                },
                {
                    "id": "llm-deepseek",
                    "config": {
                        "baseURL": base_url,
                        "apiKeyEnv": "DEEPSEEK_API_KEY",
                        "maxTokens": limit,
                        # thinking must agree with effort or the plugin refuses to load.
                        "thinking": thinking,
                        "reasoningEffort": reasoning,
                        "models": [{"id": model, "inputModalities": ["text"]}],
                        # No retryPolicy here: an explicit empty retryableCodes list is
                        # rejected at load, and llm-retry is disabled by the whitelist.
                    },
                },
                {"id": "tools", "config": {"mode": "native"}},
                {"id": "agent-loop", "config": {"agents": []}},
            ]
        )
        return patches

    def call(
        self,
        model: str,
        prompt: str,
        schema: dict[str, object],
        run_id: str,
        cancelled: Callable[[], bool],
        *,
        api_key: str,
        effort: str = "",
    ) -> NativeResult:
        account = self.account
        if not account and (
            not api_key or len(api_key) > 4096 or any(char.isspace() for char in api_key)
        ):
            return NativeResult("auth_required", {}, message="Authorized DSH API key required")
        try:
            task = self.task_text(prompt, schema)
            if (
                model not in self.models
                or not prompt.strip()
                or len(task.encode("utf-8")) > self.input_limit
                or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", run_id)
                or (not account and "\x00" in api_key)
            ):
                raise ValueError("Invalid request")
        except (ValueError, TypeError, UnicodeError, OverflowError):
            return NativeResult("failed", {}, message="Invalid DSH request")
        with tempfile.TemporaryDirectory(prefix=f"{run_id}-", dir=self.root) as directory:
            cwd = Path(directory)
            home = cwd / "home"
            home.mkdir()
            environment = self.native._environment()
            environment.update(
                {"HOME": str(home), "DSH_HOME": str(home), "DSH_TELEMETRY_DISABLED": "1"}
            )
            # Discovery does not mount the profile or receive a credential/task.
            outcome, code, stdout, stderr = self.native._run(
                [str(self.executable), "headless", "--dump-default-config"],
                cwd,
                "",
                min(self.native.timeout or 15, 15),
                cancelled,
                environment=environment,
            )
            if outcome != "ok":
                return NativeResult(outcome, {}, message="DSH configuration check stopped")
            if code != 0:
                return self.native._decode("codex", code, b"", stderr)
            self.root.mkdir(parents=True, exist_ok=True)
            log_path = self.root / "guard.log"
            with bound_transport(
                "https://api.deepseek.com/anthropic",
                log_path=log_path,
                models=frozenset({model}),
            ) as (base, guard):
                try:
                    patch = self._patch(
                        stdout,
                        model,
                        base + "/anthropic",
                        "deepseek-account" if account else "deepseek-official",
                        self.account_plugins if account else None,
                        effort,
                    )
                except (ValueError, UnicodeError):
                    return NativeResult("failed", {}, message="Unsupported DSH default profile")
                patch_path = cwd / "restricted.json"
                patch_path.write_text(dumps(patch), encoding="utf-8")
                if account:
                    environment["DSH_HOME"] = str(Path.home() / ".dsh")
                    environment["DEEPSEEK_BASE_URL"] = base + "/anthropic"
                else:
                    environment["DEEPSEEK_API_KEY"] = api_key
                outcome, code, stdout, stderr = self.native._run(
                    [str(self.executable), "headless", "--patch", str(patch_path), "--json", "-"],
                    cwd,
                    task,
                    self.native.timeout,
                    cancelled,
                    environment=environment,
                )
                if guard.forwarded > 1:
                    return NativeResult(
                        "failed", {}, message="DSH exceeded its single model request"
                    )
                if guard.refused_model and not guard.forwarded:
                    # Refused locally before any upstream connection: nothing was billed.
                    return NativeResult(
                        "policy_blocked", {},
                        message=f"DSH asked for model {guard.refused_model!r}; pinned to {model}",
                    )
                status = guard.upstream_status
                if status is not None and status >= 400:
                    # Only explicit request rejections prove that inference never began.
                    # Timeout/unknown 4xx and all 5xx retain their unknown exposure.
                    usage: dict[str, object] = {"forwarded": guard.forwarded}
                    if status in REJECTED_STATUSES:
                        usage["rejected"] = status
                    kind = {401: "auth_required", 403: "auth_required",
                            402: "quota_exhausted", 429: "rate_limited"}.get(status, "failed")
                    return NativeResult(kind, {}, usage=usage,
                                        message=f"DSH upstream HTTP {status}")
                served = guard.served_model
                forwarded = guard.forwarded
                wire = {**guard.request,
                        **({"stop_reason": guard.stop_reason} if guard.stop_reason else {})}
        if outcome != "ok":
            return NativeResult(
                outcome, {}, usage={"forwarded": forwarded, **wire},
                message="DSH process stopped without a valid result",
            )
        result = self._decode(code, stdout, stderr)
        extra: dict[str, object] = {"forwarded": forwarded, **wire}
        if effort:
            extra["effort"] = effort
        if served:
            extra["served_model"] = served
        return replace(result, usage={**result.usage, **extra})

    def _decode(self, code: int, stdout: bytes, stderr: bytes) -> NativeResult:
        failure_reason = ""
        seen_text = ""
        session = ""
        usage: dict[str, object] = {}
        ended = False
        final: dict[str, object] | None = None
        final_text = stop = ""  # stop: a turn_end kind other than completed
        error_code = ""
        errors = bytearray(stderr if code else b"")
        failed = code != 0
        step_seen = False
        try:
            for line in stdout.splitlines():
                event = json.loads(line)
                if not isinstance(event, dict) or final is not None:
                    raise ValueError("Invalid event order")
                kind = event.get("type")
                if kind == "session":
                    value = event.get("sessionId")
                    if (
                        session
                        or not isinstance(value, str)
                        or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", value)
                    ):
                        raise ValueError("Invalid session")
                    session = value
                elif kind == "error":
                    failed = True
                    errors.extend(dumps(event).encode("utf-8"))
                elif kind == "status" and not ended:
                    phase = event.get("phase")
                    if phase == "turn_end":
                        ended = True
                        reason = event.get("reason")
                        kind_of = reason.get("kind") if isinstance(reason, dict) else None
                        usage["turn_end"] = (kind_of if kind_of in TURN_ENDS else "other")
                        if kind_of != "completed":
                            stop = str(usage["turn_end"])
                            failed = True
                            errors.extend(dumps(event).encode("utf-8"))
                        detail = reason.get("error") if isinstance(reason, dict) else None
                        value = detail.get("code") if isinstance(detail, dict) else None
                        if kind_of == "error" and isinstance(value, str):
                            # Only a short upper-case code (e.g. TIMEOUT), never the message.
                            error_code = (value if re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", value)
                                          else "OTHER")
                            usage["turn_error"] = error_code
                    elif phase == "step_end":
                        if step_seen:
                            raise ValueError("Unexpected extra model step")
                        step_seen = True
                        counts = event.get("usage", {})
                        if isinstance(counts, dict):
                            for source, target in (
                                ("inputTokens", "input_tokens"),
                                ("outputTokens", "output_tokens"),
                                ("cacheReadTokens", "cache_read_input_tokens"),
                                ("cacheWriteTokens", "cache_creation_input_tokens"),
                                ("reasoningTokens", "reasoning_tokens"),
                                ("totalTokens", "total_tokens"),
                            ):
                                count = counts.get(source)
                                if type(count) is int and count >= 0:
                                    usage[target] = count
                    elif phase not in {"turn_start", "step_start"}:
                        raise ValueError("Unknown status")
                elif kind == "final" and ended and session and stop not in {"", "max-tokens"}:
                    # The turn failed (error, aborted, blocked): whatever final text exists,
                    # often empty, is not a malformed answer; classify the failure instead.
                    continue
                elif kind == "final" and ended and session:
                    final_text = event["text"] if isinstance(event.get("text"), str) else ""
                    candidate = json.loads(cls_text(event["text"]))
                    if not isinstance(candidate, dict):
                        raise ValueError("Expected object")
                    dumps(candidate)
                    final = candidate
                elif kind == "text" and isinstance(event.get("text"), str):
                    seen_text = str(event["text"])
                elif kind not in {"text", "thinking"} or ended:
                    # Includes every tool_call/tool_result and unknown event type.
                    raise ValueError(f"Unexpected DSH event: {str(kind)[:32]}")
            if not failed and final is not None and ended and session:
                return NativeResult("ok", final, session, usage)
        except (ValueError, TypeError, KeyError, UnicodeError, OverflowError) as error:
            failure_reason = str(error)[:120]
            if final is None and final_text:
                failure_reason = f"{failure_reason}; {json_failure(final_text)[0]}"
            if stop:  # e.g. max-tokens: the partial answer is why it does not parse
                failure_reason = f"{failure_reason}; stop={stop}"
            if final is None and seen_text:
                snippet = " ".join(seen_text.split())[:80]
                failure_reason = f"{failure_reason}; text={snippet}"
        if failure_reason:
            return NativeResult(
                "failed", {}, session, usage, f"Invalid DSH JSON result: {failure_reason}",
                text=final_text if final is None else "",
            )
        classified = self.native._decode("codex", 1, b"", bytes(errors))
        message = classified.message + (f"; stop={stop}" if stop else "") + (
            f"; error_code={error_code}" if error_code else "")
        return replace(classified, session_id=session, usage=usage, message=message)
