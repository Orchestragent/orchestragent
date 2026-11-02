"""List every cold large-case benchmark run, one markdown row each.

    python3 scripts/large_case_runs.py > runs.md

Reads .data/bench/*/report.json, manifest.json and acceptance.log. Costs: Claude at the
provider estimate Claude Code reports, DeepSeek as settled in CNY converted at CNY_PER_USD.
"""

import json
import re
from pathlib import Path

CNY_PER_USD = 7.1
CASES = {"boltons-buildgraph", "networkx-rollout", "networkx-timeflow"}
NOTES = {
    "2026-11-01-large-4b12bef-boltons-buildgraph-orch":
        "infrastructure error during planning; no result",
    "2026-11-02-C-f4857a0-networkx-rollout-single-max":
        "invalid: killed by the harness's 1200 s per-call timeout",
    "2026-11-02-C2-48ee0bf-networkx-rollout-single-max":
        "invalid: killed by the harness's 2 MB stream cap (since fixed); usage lost",
    "2026-11-02-C2-48ee0bf-networkx-timeflow-single-max":
        "invalid: killed by the harness's 2 MB stream cap; usage lost",
    "2026-11-02-E-4cd55d4-networkx-rollout-single-max":
        "did not finish within the 2400 s wall limit",
}
REVIEWERS = (
    ("-off", "orch · reviewer off (current default)"),
    ("-opus-low", "orch · reviewer opus-low"),
    ("-sonnet-high", "orch · reviewer sonnet-high"),
    ("-opus", "orch · reviewer planner (Opus)"),
    ("-dsf", "orch · reviewer DeepSeek Flash"),
    ("cov-", "orch · coverage review on"),
    ("rev-", "orch · test review on"),
)


def configuration(name: str, report: dict) -> str:
    if report.get("single_agent") or name.endswith("-single"):
        return f"single Opus 5.5 · {report.get('single_effort') or 'high'}"
    for marker, label in REVIEWERS:
        if marker in name:
            return label
    return "orch · earlier engine"


def acceptance(report: dict, log: str) -> str:
    if report.get("acceptance_passed") is True:
        return "8/8"
    failed = re.search(r"FAILED \(([^)]*)\)", log)
    if failed:
        return f"{8 - sum(int(n) for n in re.findall(r'=(\d+)', failed.group(1)))}/8"
    return "–"


def cost(report: dict) -> float:
    usage = report.get("usage", {})
    usd = sum((call.get("usage") or {}).get("provider_estimate_usd") or 0
              for call in usage.get("calls", []) if "claude" in (call.get("model") or ""))
    cents = usage.get("totals", {}).get("costs", {}).get("settled_cents", {}).get("known_sum")
    return usd + (cents or 0) / 100 / CNY_PER_USD


def main() -> None:
    rows = []
    for run in sorted(Path(".data/bench").glob("*/")):
        try:
            report = json.loads((run / "report.json").read_text())
            manifest = json.loads((run / "manifest.json").read_text())
        except (OSError, ValueError):
            continue
        if manifest.get("case") not in CASES or not manifest.get("cold_cache_nonce"):
            continue
        log_path = run / "acceptance.log"
        log = log_path.read_text() if log_path.exists() else ""
        rows.append((manifest["case"], configuration(run.name, report), run.name,
                     acceptance(report, log), round(report.get("wall_s") or 0), cost(report),
                     NOTES.get(run.name, "")))
    print("| case | configuration | run | acceptance | wall | cost ≈ USD | note |")
    print("|---|---|---|---:|---:|---:|---|")
    for case, config, name, passed, wall, total, note in sorted(rows):
        shown = f"${total:.2f}" if total else "not reported"
        print(f"| {case} | {config} | `{name}` | {passed} | {wall} s | {shown} | {note} |")


if __name__ == "__main__":
    main()
