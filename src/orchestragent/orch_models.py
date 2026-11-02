"""Which model serves each orch role, and whether that is the benchmarked set.

Roles: the planner (task boundaries), the test designer (frozen test shards), the builder
(implementations) and the reviewer (checks each test shard before it is frozen). The
VERIFIED set is what the published benchmarks measured; any other choice works through
the same adapters but is unverified. Precedence: environment variable > the user's
`orch models set` file (<data dir>/models.json) > the verified default.

A route is written `provider/model:effort`; the reviewer takes a short name.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from orchestragent.platform import default_data_dir


@dataclass(frozen=True)
class Choice:
    provider: str
    model: str
    effort: str

    def __str__(self) -> str:
        return f"{self.provider}/{self.model}:{self.effort}"


CLAUDE_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")
CLAUDE_EFFORTS = ("low", "medium", "high", "max")
DSF_MODELS = ("deepseek-flash", "deepseek-v4-pro")
DSF_EFFORTS = ("off", "low", "high", "max")
REVIEWERS = ("planner", "opus-low", "sonnet", "sonnet-high", "dsf", "off")

ROLES = ("planner", "test_designer", "builder", "reviewer")
# What the published benchmarks ran (README, docs/measurements).
VERIFIED: dict[str, str] = {
    "planner": "claude/claude-opus-5-5:medium",
    "test_designer": "dsh/deepseek-flash:low",
    "builder": "dsh/deepseek-flash:low",
    "reviewer": "planner",
}
DESCRIPTIONS = {
    "planner": "splits the spec into tasks, boundaries and interfaces",
    "test_designer": "writes each task's frozen tests",
    "builder": "implements tasks against the frozen tests",
    "reviewer": "checks each test shard before it is frozen",
}
# Environment variables that override a role (older single-purpose names included).
ENVIRONMENT = {
    "planner": "ORCHESTRA_PLANNER",
    "test_designer": "ORCHESTRA_TEST_DESIGNER",
    "builder": "ORCHESTRA_BUILDER",
    "reviewer": "ORCHESTRA_REVIEW_MODEL",
}


def parse(role: str, value: str) -> Choice | str:
    """Validate one role's value: a Choice for model roles, a name for the reviewer."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    value = value.strip()
    if role == "reviewer":
        if value not in REVIEWERS:
            raise ValueError(f"reviewer must be one of {', '.join(REVIEWERS)}")
        return value
    provider, _, rest = value.partition("/")
    model, _, effort = rest.partition(":")
    if role == "planner":
        if provider != "claude" or model not in CLAUDE_MODELS or effort not in CLAUDE_EFFORTS:
            raise ValueError("planner must be claude/<" + "|".join(CLAUDE_MODELS) + ">:<"
                             + "|".join(CLAUDE_EFFORTS) + ">")
    elif provider != "dsh" or model not in DSF_MODELS or effort not in DSF_EFFORTS:
        raise ValueError(f"{role} must be dsh/<" + "|".join(DSF_MODELS) + ">:<"
                         + "|".join(DSF_EFFORTS) + ">")
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
    if role == "reviewer" and os.environ.get("ORCHESTRA_COVERAGE_REVIEW") == "0":
        return "off"
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
        parse(role, result[role][0])  # fail early and clearly on a bad value
    return result


def choose(role: str, data_dir: Path | None = None) -> Choice | str:
    return parse(role, effective(data_dir)[role][0])


def set_role(role: str, value: str, data_dir: Path | None = None) -> str:
    parse(role, value)
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
