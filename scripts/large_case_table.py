"""Summarise large-case benchmark runs into markdown tables (one row per configuration).

    python3 scripts/large_case_table.py .data/bench > docs/measurements/large-cases.md

Each run directory has report.json, manifest.json and acceptance.log. Rows group runs by
case and configuration: native single Opus at its effort, or orch with its reviewer.
"""

import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

CNY_PER_USD = 7.1  # for one comparable total; both currencies are also shown


def row(path: Path) -> tuple[str, str, dict[str, float]] | None:
    try:
        report = json.loads((path / "report.json").read_text())
        manifest = json.loads((path / "manifest.json").read_text())
    except (OSError, ValueError):
        return None
    case = str(manifest.get("case", ""))
    if case not in {"boltons-buildgraph", "networkx-rollout", "networkx-timeflow"}:
        return None
    if not manifest.get("cold_cache_nonce"):
        return None  # only cold-cache runs are comparable
    calls = report.get("usage", {}).get("calls", [])
    usd = sum((c.get("usage") or {}).get("provider_estimate_usd") or 0 for c in calls
              if "claude" in (c.get("model") or ""))
    cny = (report.get("usage", {}).get("totals", {}).get("costs", {})
           .get("settled_cents", {}).get("known_sum") or 0) / 100
    if manifest.get("single_agent"):
        config = f"single Opus 5.5 · {manifest.get('single_effort') or 'high'}"
    else:
        reviewer = re.search(r"orch-([a-z-]+)$", path.name)
        config = "orch · reviewer " + (reviewer.group(1) if reviewer else "default")
    acceptance = (path / "acceptance.log").read_text() if (path / "acceptance.log").exists() \
        else ""
    ran = re.search(r"Ran (\d+) tests", acceptance)
    failed = re.search(r"FAILED \((?:failures|errors)=(\d+)(?:, (?:failures|errors)=(\d+))?",
                       acceptance)
    total = int(ran.group(1)) if ran else 8
    bad = sum(int(g) for g in failed.groups() if g) if failed else 0
    return case, config, {"passed": total - bad if ran else 0, "total": total,
                          "wall": float(report.get("wall_s", 0)), "usd": usd, "cny": cny}


def main(root: Path) -> None:
    groups: dict[tuple[str, str], list[dict[str, float]]] = defaultdict(list)
    for path in sorted(root.iterdir()):
        found = row(path) if path.is_dir() else None
        if found:
            groups[(found[0], found[1])].append(found[2])
    for case in sorted({case for case, _ in groups}):
        print(f"\n### {case}\n")
        print("| configuration | runs | acceptance | wall (median) | cost per run |")
        print("|---|---:|---|---:|---:|")
        for (c, config), runs in sorted(groups.items()):
            if c != case:
                continue
            accept = " · ".join(f"{int(r['passed'])}/{int(r['total'])}" for r in runs)
            wall = statistics.median(r["wall"] for r in runs)
            cost = statistics.median(r["usd"] + r["cny"] / CNY_PER_USD for r in runs)
            print(f"| {config} | {len(runs)} | {accept} | {wall:.0f} s | ≈${cost:.2f} |")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
