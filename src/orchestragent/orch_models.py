"""Which model serves each orch role, and whether that is the benchmarked set.

Roles: the planner (task boundaries, integration checks, supervision), the test designer
(each component's tests) and the builder (implementations). The
VERIFIED set is what the published benchmarks measured; any other choice works through
the same adapters but is unverified. Precedence: environment variable > the user's
`orch models set` file (<data dir>/models.json) > the verified default.

A route is written `provider/model:effort`. Providers:
bring your own CLI subscription or key — `claude` (Claude Code), `codex` (Codex CLI),
`cursor` (Cursor CLI) — or bring your own key — `dsh` (DeepSeek, pinned route) and any
OpenAI-compatible endpoint registered with `orch providers add`. Kimi's subscription
forbids non-interactive automation and is refused. Using each provider within its terms is
the user's responsibility (DISCLAIMER.md).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from orchestragent.platform import default_data_dir


@dataclass(frozen=True)
class Choice:
    provider: str
    model: str
    effort: str

    def __str__(self) -> str:
        return f"{self.provider}/{self.model}" + (f":{self.effort}" if self.effort else "")


CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
CODEX_EFFORTS = ("low", "medium", "high", "xhigh", "max")
DSF_MODELS = ("deepseek-flash",)  # V4 Pro is not offered (maintainer decision)
DSF_EFFORTS = ("off", "low", "high", "max")
BYOK_EFFORTS = ("", "low", "medium", "high")
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")
# Subscriptions whose terms forbid automated, non-interactive use.
FORBIDDEN = {"kimi": "Kimi's subscription does not allow non-interactive automation"}

ROLES = ("planner", "supervisor", "test_designer", "builder")
# What the published benchmarks ran (README, docs/measurements).
VERIFIED: dict[str, str] = {
    "planner": "claude/claude-opus-5-5:medium",
    "supervisor": "claude/claude-opus-5-5:medium",
    "test_designer": "dsh/deepseek-flash:low",
    "builder": "dsh/deepseek-flash:low",
    # Off by default: on NetworkX (2026-11-02) the shard review added US$1-3 and 100-300 s of
    # planning per run without a consistent acceptance gain; the decisive-assertion rule in
    # test design fixed the misses it targeted (rollout 8/8, 975 s, review off).
}
DESCRIPTIONS = {
    "planner": "splits the spec into components, tasks and contracts; writes the "
               "integration checks",
    "supervisor": "judges a task's failures (retry, revise tests, split, escalate ...) "
                  "and makes the escalated attempt",
    "test_designer": "writes each task's frozen tests",
    "builder": "implements tasks against the frozen tests",
}
# Environment variables that override a role (older single-purpose names included).
ENVIRONMENT = {
    "planner": "ORCHESTRA_PLANNER",
    "supervisor": "ORCHESTRA_SUPERVISOR",
    "test_designer": "ORCHESTRA_TEST_DESIGNER",
    "builder": "ORCHESTRA_BUILDER",
}


def parse(role: str, value: str, data_dir: Path | None = None) -> Choice | str:
    """Validate one role's value as a Choice."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    value = value.strip()
    provider, _, rest = value.partition("/")
    model, _, effort = rest.rpartition(":") if ":" in rest else (rest, "", "")
    if provider not in {"claude", "codex", "cursor", "dsh"} and effort not in BYOK_EFFORTS:
        model, effort = rest, ""  # e.g. an Ollama tag such as qwen2.5:7b
    if provider in FORBIDDEN:
        raise ValueError(f"{provider} refused: {FORBIDDEN[provider]}")
    if not MODEL_ID.fullmatch(model or "-"):
        raise ValueError(f"{role}: model id missing or invalid in {value!r}")
    allowed = {"claude": CLAUDE_EFFORTS, "codex": CODEX_EFFORTS, "cursor": ("",),
               "dsh": DSF_EFFORTS}
    if provider == "dsh" and model not in DSF_MODELS:
        raise ValueError("dsh serves " + " or ".join(DSF_MODELS)
                         + "; other models: register a provider (orch providers add)")
    if provider in allowed:
        if effort not in allowed[provider]:
            raise ValueError(f"{provider} effort must be one of "
                             + ", ".join(e or "(none)" for e in allowed[provider]))
    else:
        from orchestragent.byok import registry
        if provider not in registry(data_dir or default_data_dir()):
            raise ValueError(f"unknown provider {provider!r}: use claude, codex, cursor, dsh "
                             "or register one with `orch providers add`")
        if effort not in BYOK_EFFORTS:
            raise ValueError("a registered provider takes effort low, medium, high or none")
    return Choice(provider, model, effort)


def config_path(data_dir: Path | None = None) -> Path:
    return (data_dir or default_data_dir()) / "models.json"


def saved(data_dir: Path | None = None) -> dict[str, str]:
    path = config_path(data_dir)
    if not path.is_file():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a JSON object of role -> model")
    return {str(k): str(v) for k, v in value.items() if k in ROLES}


def legacy(role: str) -> str | None:
    """Older single-purpose variables kept working: effort-only and on/off switches."""
    if role == "planner" and os.environ.get("ORCHESTRA_PLAN_EFFORT"):
        return f"claude/claude-opus-5-5:{os.environ['ORCHESTRA_PLAN_EFFORT']}"
    if role == "test_designer" and os.environ.get("ORCHESTRA_DSF_TEST_EFFORT"):
        return f"dsh/deepseek-flash:{os.environ['ORCHESTRA_DSF_TEST_EFFORT']}"
    return None


def effective(data_dir: Path | None = None) -> dict[str, tuple[str, str]]:
    """role -> (value, source) where source is env, config or verified."""
    stored = saved(data_dir)
    result: dict[str, tuple[str, str]] = {}
    for role in ROLES:
        env = os.environ.get(ENVIRONMENT[role]) or legacy(role)
        if env:
            result[role] = (env, "env")
        elif role in stored:
            result[role] = (stored[role], "config")
        else:
            result[role] = (VERIFIED[role], "verified")
        parse(role, result[role][0], data_dir)  # fail early and clearly on a bad value
    return result


def choose(role: str, data_dir: Path | None = None) -> Choice | str:
    return parse(role, effective(data_dir)[role][0], data_dir)


def set_role(role: str, value: str, data_dir: Path | None = None) -> str:
    parse(role, value, data_dir)
    stored = saved(data_dir)
    stored[role] = value
    path = config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stored, indent=2) + "\n")
    note = "" if value == VERIFIED[role] else " (unverified: benchmarks ran on " \
        f"{VERIFIED[role]})"
    return f"{role} = {value}{note}"


def reset(role: str = "", data_dir: Path | None = None) -> str:
    stored = saved(data_dir)
    for name in ([role] if role else list(ROLES)):
        if name not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        stored.pop(name, None)
    path = config_path(data_dir)
    if stored:
        path.write_text(json.dumps(stored, indent=2) + "\n")
    elif path.exists():
        path.unlink()
    return f"reset {role or 'all roles'} to the verified set"


def table(data_dir: Path | None = None) -> list[str]:
    lines = ["role           model                               source    status"]
    for role, (value, source) in effective(data_dir).items():
        status = "verified" if value == VERIFIED[role] else "unverified"
        lines.append(f"{role:<14} {value:<35} {source:<9} {status}")
    custom = [r for r, (v, _) in effective(data_dir).items() if v != VERIFIED[r]]
    lines.append("")
    lines.append("All roles use the benchmarked set." if not custom else
                 "Custom: " + ", ".join(custom) + ". Results may differ from the published "
                 "benchmarks; `orch models reset` restores the verified set.")
    return lines
