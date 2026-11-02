"""Fail-closed Cursor boundary until tool and extension isolation is verified."""

import json
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path

from orchestragent.native import NativeClients, NativeResult
from orchestragent.platform import cursor_binary


class CursorClient:
    """Subscription-pool models only, best-effort isolation, no claimed guarantee.

    Audited official CLI 2026.10.19-6bbf2c4: #print keeps read tools and loads hooks
    from the user, project, Claude, enterprise and team sources; no documented switch
    disables them, and --force would silently drop the sandbox. We therefore run with
    a throwaway HOME and workspace, --mode ask, --sandbox enabled, never --force, and
    only models that the subscription pool includes. Enterprise and team hooks remain
    a residual surface and are reported as such.
    """

    blocker = ""
    explanation = (
        "Cursor runs on subscription-pool models in ask mode inside a fresh read-only "
        "workspace; user, project and team hooks cannot be disabled and remain a "
        "residual surface."
    )
    pool_model = re.compile(
        r"(composer-2\.5(-fast)?|(cursor-)?grok-4\.(5|6)-(low|medium|high|xhigh)(-fast)?"
        r"|grok-4\.7-(low|medium|high|xhigh)(-fast)?)"
    )

    def __init__(self, native: NativeClients, root: Path):
        self.native = native
        self.root = root.resolve() / "cursor"
        self.root.mkdir(parents=True, exist_ok=True)
        self._models: list[str] | None = None
        self.executable = cursor_binary()

    def status(self) -> dict[str, object]:
        """Report installation and the CLI's own authentication flag; never credentials."""
        installed = self.executable.is_file() and os.access(self.executable, os.X_OK)
        result: dict[str, object] = {
            "ready": False,
            "installed": installed,
            "auth": "unavailable" if not installed else "unknown",
            "billing": "subscription_pool",
            "isolation": "partial",
            "blocker": self.blocker,
            "message": self.explanation,
        }
        if not installed:
            return result
        outcome, code, stdout, _stderr = self.native._run(
            [str(self.executable), "status", "--format", "json"],
            self.root,
            "",
            min(self.native.timeout or 15, 15),
            lambda: False,
        )
        if outcome != "ok":
            return {**result, "outcome": outcome}
        try:
            value = json.loads(stdout)
        except (ValueError, UnicodeError):
            return result
        if isinstance(value, dict) and value.get("isAuthenticated") is True and code == 0:
            return {**result, "ready": True, "auth": "cursor_subscription"}
        if isinstance(value, dict) and value.get("isAuthenticated") is False:
            return {**result, "auth": "logged_out"}
        return result

    def list_models(self) -> list[str]:
        """Only subscription-pool ids, straight from the official CLI (cached)."""
        if self._models is not None:
            return list(self._models)
        found = self._list_models_uncached()
        self._models = found
        return list(found)

    def _list_models_uncached(self) -> list[str]:
        if not self.executable.is_file():
            return []
        outcome, code, stdout, _stderr = self.native._run(
            [str(self.executable), "models"],
            self.root,
            "",
            min(self.native.timeout or 20, 20),
            lambda: False,
        )
        if outcome != "ok" or code != 0:
            return ["composer-2.5", "composer-2.5-fast"].copy()
        found = []
        for line in stdout.decode("utf-8", "replace").splitlines():
            model = line.split(" - ", 1)[0].strip()
            if model and self.pool_model.fullmatch(model):
                found.append(model)
        return sorted(set(found))

    def call(
        self,
        model: str,
        prompt: str,
        schema: dict[str, object],
        run_id: str,
        cancelled: Callable[[], bool],
        *,
        trusted_billing_consent: bool = False,
    ) -> NativeResult:
        if cancelled():
            return NativeResult("cancelled", {}, message="Cursor request cancelled before launch")
        if (
            not model.strip()
            or model.strip().lower() == "auto"
            or len(model) > 128
            or not prompt.strip()
            or len(prompt.encode("utf-8")) > NativeClients.prompt_limit
            or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", run_id)
        ):
            return NativeResult(
                "failed", {}, message="Explicit Cursor model and valid request required"
            )
        if not self.pool_model.fullmatch(model.strip()):
            return NativeResult(
                "policy_blocked",
                {},
                message="仅允许订阅池模型（Composer / Grok）；未切换到按 API 计价的模型",
            )
        with tempfile.TemporaryDirectory(prefix=f"{run_id}-", dir=self.root) as directory:
            run = Path(directory)
            workspace = run / "workspace"
            workspace.mkdir()
            environment = self.native._environment()
            # Native subscription auth resolves from the real HOME, so the run keeps it;
            # --trust covers only this fresh, empty, read-only workspace.
            environment.update({"NO_OPEN_BROWSER": "1"})
            outcome, code, stdout, stderr = self.native._run(
                [
                    str(self.executable),
                    "--print",
                    "--output-format",
                    "json",
                    "--mode",
                    "ask",
                    "--trust",
                    "--sandbox",
                    "enabled",
                    "--model",
                    model.strip(),
                    "--workspace",
                    str(workspace),
                    prompt,
                ],
                workspace,
                "",
                self.native.timeout,
                cancelled,
                environment=environment,
            )
        if outcome != "ok":
            return NativeResult(outcome, {}, message="Cursor process stopped")
        return self._decode(code, stdout, stderr)

    def _decode(self, code: int, stdout: bytes, stderr: bytes) -> NativeResult:
        if code != 0:
            return self.native._decode("codex", code, b"", stderr)
        try:
            packet = json.loads(stdout)
        except (ValueError, UnicodeError):
            return NativeResult("failed", {}, message="Invalid Cursor JSON result")
        if not isinstance(packet, dict) or packet.get("is_error") is True:
            return NativeResult("failed", {}, message="Cursor reported an error")
        text = packet.get("result")
        session = packet.get("session_id")
        usage = packet.get("usage")
        if not isinstance(text, str):
            return NativeResult("failed", {}, message="Cursor returned no result text")
        body = text.strip()
        if body.startswith("```"):
            body = body.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            value = json.loads(body)
        except (ValueError, UnicodeError):
            return NativeResult("failed", {}, message="Cursor result was not JSON")
        if not isinstance(value, dict):
            return NativeResult("failed", {}, message="Cursor result was not an object")
        return NativeResult(
            "ok",
            value,
            session if isinstance(session, str) else "",
            usage if isinstance(usage, dict) else {},
        )
