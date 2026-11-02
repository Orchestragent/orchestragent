"""What each provider can actually run, verified on this Mac.

The GUI and the API both read this so a preference cannot promise something the
adapter will later reject.
"""

import re

MODELS: dict[str, list[tuple[str, str]]] = {
    "codex": [("gpt-6-astra", "GPT-6 Astra")],
    "claude": [
        ("claude-opus-5-5", "Opus 5.5"),
        ("claude-sonnet-5-5", "Sonnet 5.5"),
        ("claude-haiku-5-5", "Haiku 5.5"),
        ("opus", "Opus（别名）"),
        ("haiku", "Haiku（别名）"),
    ],
    "dsh": [("deepseek-flash", "V4.1 Flash 1017"), ("deepseek-v4-pro", "V4 Pro")],
    "kimi": [],
}

EFFORTS: dict[str, set[str]] = {
    "codex": {"low", "medium", "high", "xhigh", "max"},
    "claude": {"low", "medium", "high", "max"},
    "dsh": {"off", "low", "high", "max"},
    "cursor": set(),
    "kimi": set(),
}

CURSOR_POOL = re.compile(
    r"(composer-2\.5(-fast)?|(cursor-)?grok-4\.(5|6)-(low|medium|high|xhigh)(-fast)?"
    r"|grok-4\.7-(low|medium|high|xhigh)(-fast)?)"
)


def valid_model(provider: str, model: str) -> bool:
    if not model:
        return True
    if provider == "cursor":
        return bool(CURSOR_POOL.fullmatch(model))
    return any(model == key for key, _label in MODELS.get(provider, []))


def valid_effort(provider: str, effort: str) -> bool:
    if not effort:
        return True
    return effort in EFFORTS.get(provider, set())


def models(provider: str) -> list[dict[str, str]]:
    return [{"id": key, "label": label} for key, label in MODELS.get(provider, [])]
