"""The benchmark gate: every case, N runs each, one table, compared with a saved baseline.

    python3 scripts/orch_gate.py --tag after-step1 --runs 2            # orch on every case
    python3 scripts/orch_gate.py --tag base --runs 1 --single high     # a single-agent arm too
    python3 scripts/orch_gate.py --compare before after-step1          # regressions -> exit 1

A change ships only when no case's pass count drops against the baseline. The point is to
stop fixing one run at a time: a failure becomes a case here, and the whole suite decides.
Cases and their pinned sources: examples/orch-bench/cases.json.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from orchestragent.orch_timing import stages, state_db  # noqa: E402

CASES = ROOT / "examples" / "orch-bench"
GATE = ROOT / ".data" / "gate"
PY = ROOT / ".venv" / "bin" / "python"


def run_case(case: str, spec: dict[str, str], out: Path, single: str, timeout: float) -> dict:
    source = Path(spec["source"])
    if not source.is_absolute():
        source = ROOT / source
    args = [str(PY), str(ROOT / "scripts" / "orch_bench.py"), "--source", str(source),
            "--revision", spec["revision"], "--case", str(CASES / case), "--output", str(out),
            "--cold", "--timeout", str(timeout)]
    if single:
        args += ["--single-agent", "--effort", single]
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"),
           "PATH": f"{Path.home()}/.cargo/bin:{Path.home()}/.local/bin:{os.environ['PATH']}"}
    started = time.monotonic()
    with (out.parent / f"{out.name}.log").open("w") as log:
        subprocess.run(args, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                       timeout=timeout + 1800, check=False)
    report = out / "report.json"
    data = json.loads(report.read_text()) if report.exists() else {}
    costs = data.get("usage", {}).get("costs", {})
    db = state_db(out / "project")
    timing = stages(db) if db is not None else {}
    return {
        "case": case, "arm": "single:" + single if single else "orch",
        "passed": bool(data.get("acceptance_passed")),
        "status": data.get("status", data.get("plan_failed") and "plan_failed" or "?"),
        "wall_s": data.get("wall_s") or round(time.monotonic() - started, 1),
        "usd": round(costs.get("provider_estimate_usd", {}).get("known_sum", 0.0), 2),
        "cny": round(costs.get("settled_cents", {}).get("known_sum", 0) / 100, 2),
        "output": str(out.relative_to(ROOT)),
        "stages": {k: timing.get(k) for k in ("plan", "design", "build", "final")},
        "calls": timing.get("calls", {}),
    }


def table(results: list[dict]) -> str:
    lines = ["| case | arm | pass | wall s (median) | plan / design / build / final s | "
             "usd | cny |", "|---|---|---|---:|---|---:|---:|"]
    for (case, arm), rows in group(results).items():
        chosen = [r for r in rows if r["passed"]] or rows
        walls = [r["wall_s"] for r in chosen]
        usd = sum(r["usd"] for r in rows) / len(rows)
        cny = sum(r["cny"] for r in rows) / len(rows)
        spans = []
        for key in ("plan", "design", "build", "final"):
            values = [r.get("stages", {}).get(key) for r in chosen]
            known = [v for v in values if v is not None]
            spans.append(f"{statistics.median(known):.0f}" if known else "-")
        lines.append(f"| {case} | {arm} | {sum(r['passed'] for r in rows)}/{len(rows)} | "
                     f"{statistics.median(walls):.0f} | {' / '.join(spans)} | "
                     f"{usd:.2f} | {cny:.2f} |")
    return "\n".join(lines)


def group(results: list[dict]) -> dict[tuple[str, str], list[dict]]:
    out: dict[tuple[str, str], list[dict]] = {}
    for row in results:
        out.setdefault((row["case"], row["arm"]), []).append(row)
    return out


def compare(before: str, after: str) -> int:
    old = group(json.loads((GATE / before / "summary.json").read_text()))
    new = group(json.loads((GATE / after / "summary.json").read_text()))
    worse = []
    for key, rows in new.items():
        if key not in old:
            continue
        passes = sum(r["passed"] for r in rows) / len(rows)
        base = sum(r["passed"] for r in old[key]) / len(old[key])
        mark = "REGRESSION" if passes < base else "ok"
        if passes < base:
            worse.append(key)
        print(f"{key[0]:<20} {key[1]:<12} pass {base:.2f} -> {passes:.2f}  {mark}")
    return 1 if worse else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", help="name of this gate run (.data/gate/<tag>)")
    parser.add_argument("--cases", nargs="*", help="subset of cases (default: all)")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--single", default="", help="also run the single-agent arm at this effort")
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    args = parser.parse_args()
    if args.compare:
        return compare(*args.compare)
    if not args.tag:
        parser.error("--tag is required to run the gate")
    cases = json.loads((CASES / "cases.json").read_text())
    chosen = args.cases or list(cases)
    folder = GATE / args.tag
    folder.mkdir(parents=True, exist_ok=True)
    summary = folder / "summary.json"
    results: list[dict] = json.loads(summary.read_text()) if summary.exists() else []
    done = {(r["case"], r["arm"], r["output"]) for r in results}
    for case in chosen:
        for arm in ([""] + ([args.single] if args.single else [])):
            for n in range(1, args.runs + 1):
                out = folder / f"{case}-{'single-' + arm if arm else 'orch'}-r{n}"
                if any(d[2] == str(out.relative_to(ROOT)) for d in done):
                    continue  # resumable: a gate run can be stopped and continued
                row = run_case(case, cases[case], out, arm, args.timeout)
                results.append(row)
                summary.write_text(json.dumps(results, indent=1))
                print(json.dumps(row), flush=True)
    (folder / "summary.md").write_text(table(results) + "\n")
    print(table(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
