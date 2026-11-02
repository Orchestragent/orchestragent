"""Manual, single-shot Kimi route.

The subscription is licensed for personal interactive use, so this client never
queues, retries, batches or runs in the background: one human action, one prompt,
one answer. It is intentionally absent from the automated dispatch pipeline.
"""

import re
import threading
from pathlib import Path

from orchestragent.native import NativeClients
from orchestragent.platform import kimi_binary

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


class KimiManual:
    timeout = 180

    def __init__(self, native: NativeClients, root: Path) -> None:
        self.native = native
        self.executable = kimi_binary()
        self.home = root.resolve() / "kimi"
        self.home.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def catalog(self) -> list[dict[str, object]]:
        """Read the CLI's own config so the app offers real model aliases."""
        import tomllib

        path = Path.home() / ".kimi-code" / "config.toml"
        if not path.is_file():
            return []
        try:
            config = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        default = str(config.get("default_model", ""))
        return [
            {
                "id": alias,
                "label": str(entry.get("display_name") or alias),
                "context": int(entry.get("max_context_size") or 0),
                "efforts": [str(item) for item in entry.get("support_efforts", [])],
                "default_effort": str(entry.get("default_effort", "")),
                "default": alias == default,
            }
            for alias, entry in (config.get("models") or {}).items()
            if isinstance(entry, dict)
        ]

    def status(self) -> dict[str, object]:
        return {
            "installed": self.executable.is_file(),
            "mode": "manual_single_shot",
            "modes": ["interactive", "auto"],
            "pipeline": False,
            "message": "手动单次调用；不进入自动流水线，不重试、不并发。",
        }

    def ask(self, prompt: str, *, auto: bool = False, model: str = "") -> dict[str, object]:
        if not prompt.strip() or len(prompt) > 8000:
            raise ValueError("Question must contain 1 to 8000 characters")
        if model and (len(model) > 64 or not model.strip().isascii()):
            raise ValueError("Invalid Kimi model alias")
        if not self.executable.is_file():
            return {"ok": False, "text": "", "message": "Kimi CLI 未安装"}
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "text": "", "message": "上一轮还没结束，请稍后再问"}
        try:
            environment = self.native._environment()
            # Native subscription auth lives in the CLI's own home, so the run keeps
            # the real HOME and only hardens telemetry and auto-update.
            environment.update(
                {
                    "KIMI_CODE_NO_AUTO_UPDATE": "1",
                    "KIMI_DISABLE_TELEMETRY": "1",
                    "KIMI_LOG_LEVEL": "off",
                }
            )
            outcome, code, stdout, stderr = self.native._run(
                [
                    str(self.executable),
                    *(["--auto"] if auto else []),
                    *(["-m", model] if model else []),
                    "-p",
                    prompt,
                ],
                self.home,
                "",
                self.timeout,
                lambda: False,
                environment=environment,
            )
        finally:
            self._lock.release()
        text = ANSI.sub("", stdout.decode("utf-8", "replace")).strip()
        if outcome != "ok":
            return {"ok": False, "text": "", "message": f"本轮未完成（{outcome}）"}
        if code != 0 and not text:
            detail = ANSI.sub("", stderr.decode("utf-8", "replace")).strip()[:200]
            return {"ok": False, "text": "", "message": detail or "Kimi 返回失败"}
        return {"ok": True, "text": text[:8000], "message": ""}
