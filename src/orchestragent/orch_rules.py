"""Rule lifecycle shared by builder and planner: lesson -> candidate -> A/B -> active
-> retired.

Builder rules come from Claude's successful patches; each candidate is shown to half
of the eligible DSF attempts (task layer, so the shared prefix stays cached). Planner
rules come from planning mistakes: lint rejections, replans, wrong frozen tests and
weak tests found by mutation testing. A planner candidate is shown to half of the
plans; because a plan's real outcome (did any task end up BLOCKED, replanned or
disputed?) arrives late and is noisy, planner rules are judged on a proxy (first
draft lint-clean, no vacuous tests, no coupled files split) measured online and by
offline replay of past specs, as long as that proxy stays correlated with the real
outcome; otherwise on real outcomes only. Status changes happen only at a checkpoint
boundary: the start of a run (builder) or of a plan (planner).
"""

import math
from dataclasses import dataclass, replace

from orchestragent.envelope import digest

MIN_TRIALS = 6  # attempts per arm before a candidate can change status
MIN_GAIN = 0.15  # pass-rate lift required for promotion
MIN_Z = 1.0  # one-sided two-proportion z
HARM = 0.10  # pass-rate drop that retires a candidate
RULE_BYTES = 1200  # layer-2 budget for active builder rules
PLANNER_RULE_BYTES = 1500  # planner prompt budget for active planner rules
MAX_LESSON = 240
PROXY_RUNS = 5  # runs with both proxy and real outcome before the proxy is checked
PROXY_RHO = 0.3  # minimum Spearman correlation for the proxy to stay trusted
KINDS = {"builder": "r", "planner": "p"}  # id prefixes


@dataclass
class Rule:
    id: str
    text: str
    source: str
    created_at: float
    status: str = "candidate"  # candidate | active | retired
    with_passed: int = 0
    with_total: int = 0
    without_passed: int = 0
    without_total: int = 0
    note: str = ""
    kind: str = "builder"  # builder | planner
    # Planner rules only: proxy outcomes (online plans and offline replays). The
    # with/without counters above hold real run outcomes for planner rules.
    proxy_with_passed: int = 0
    proxy_with_total: int = 0
    proxy_without_passed: int = 0
    proxy_without_total: int = 0

    @property
    def gain(self) -> float:
        return rate(self.with_passed, self.with_total) - rate(
            self.without_passed, self.without_total
        )


def rate(passed: int, total: int) -> float:
    return passed / total if total else 0.0


def z_score(rule: Rule) -> float:
    """One-sided two-proportion z for 'with the rule passes more often'."""
    n1, n2 = rule.with_total, rule.without_total
    if not n1 or not n2:
        return 0.0
    pooled = (rule.with_passed + rule.without_passed) / (n1 + n2)
    spread = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    return rule.gain / spread if spread else 0.0


def verdict(rule: Rule) -> tuple[str, str]:
    """(status, note) a candidate should move to given its A/B counts."""
    if rule.status != "candidate":
        return rule.status, rule.note
    if min(rule.with_total, rule.without_total) < MIN_TRIALS:
        return "candidate", ""
    stats = (
        f"with {rule.with_passed}/{rule.with_total}, "
        f"without {rule.without_passed}/{rule.without_total}"
    )
    if rule.gain >= MIN_GAIN and z_score(rule) >= MIN_Z:
        return "active", f"promoted: {stats}"
    if rule.gain <= -HARM:
        return "retired", f"harmful: {stats}"
    return "candidate", ""


def planner_verdict(rule: Rule, proxy_trusted: bool) -> tuple[str, str]:
    """A planner candidate's verdict on proxy evidence while the proxy is trusted,
    on real run outcomes otherwise."""
    if proxy_trusted:
        judged = replace(
            rule, with_passed=rule.proxy_with_passed, with_total=rule.proxy_with_total,
            without_passed=rule.proxy_without_passed, without_total=rule.proxy_without_total,
        )
        status, note = verdict(judged)
        return status, f"{note} (proxy)" if note else ""
    return verdict(rule)


def spearman(xs: list[float], ys: list[float]) -> float:
    """Rank correlation with average ranks for ties; 0 when either side is constant."""

    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        ranked = [0.0] * len(values)
        start = 0
        while start < len(order):
            end = start
            while end + 1 < len(order) and values[order[end + 1]] == values[order[start]]:
                end += 1
            for position in range(start, end + 1):
                ranked[order[position]] = (start + end) / 2
            start = end + 1
        return ranked

    if len(xs) < 2:
        return 0.0
    a, b = ranks(xs), ranks(ys)
    mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b, strict=True))
    spread = math.sqrt(sum((x - mean_a) ** 2 for x in a) * sum((y - mean_b) ** 2 for y in b))
    return cov / spread if spread else 0.0


def layer(rules: list[Rule], kind: str = "builder") -> tuple[str, list[str], list[Rule]]:
    """Active rules of one kind that fit the byte budget, best measured gain first.

    Returns (text, ids shown, active rules that no longer fit and must retire).
    """
    budget = RULE_BYTES if kind == "builder" else PLANNER_RULE_BYTES
    chosen: list[str] = []
    ids: list[str] = []
    evicted: list[Rule] = []
    used = 0
    for rule in sorted(rules, key=lambda r: (-r.gain, r.id)):
        if rule.status != "active" or rule.kind != kind:
            continue
        line = f"- {rule.text}\n"
        if used + len(line.encode()) > budget:
            evicted.append(rule)
            continue
        chosen.append(line)
        ids.append(rule.id)
        used += len(line.encode())
    return "".join(chosen), ids, evicted


def lesson(value: object) -> str:
    """A usable one-line lesson, or empty."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text if 12 <= len(text) <= MAX_LESSON else ""


def arm(rule_id: str, task_id: str, attempt: int) -> bool:
    """Deterministic, balanced assignment: True shows the candidate."""
    return int(digest(f"{rule_id}/{task_id}/{attempt}")[:8], 16) % 2 == 0
