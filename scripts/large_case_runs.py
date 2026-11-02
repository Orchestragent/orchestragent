"""List every cold large-case run (one row each) for docs/measurements: python3 scripts/large_case_runs.py"""
import json, os, re, glob
RATE = 7.1
NOTES = {
 "2026-11-01-large-4b12bef-boltons-buildgraph-orch": "infrastructure error during planning; no result",
 "2026-11-02-C-f4857a0-networkx-rollout-single-max": "invalid: killed by the harness's 1200 s per-call timeout",
 "2026-11-02-C2-48ee0bf-networkx-rollout-single-max": "invalid: killed by the harness's 2 MB stream cap (since fixed); usage lost",
 "2026-11-02-C2-48ee0bf-networkx-timeflow-single-max": "invalid: killed by the harness's 2 MB stream cap; usage lost",
 "2026-11-02-E-4cd55d4-networkx-rollout-single-max": "did not finish within the 2400 s wall limit",
}
def cfg(name, r):
    if r.get("single_agent") or name.endswith("-single"):
        return f"single Opus 5.5 · {r.get('single_effort') or 'high'}"
    for k, v in (("-off", "orch · reviewer off (current default)"), ("-opus-low", "orch · reviewer opus-low"),
                 ("-sonnet-high", "orch · reviewer sonnet-high"), ("-opus", "orch · reviewer planner (Opus)"),
                 ("-dsf", "orch · reviewer DeepSeek Flash"), ("cov-", "orch · coverage review on"), ("rev-", "orch · test review on")):
        if k in name: return v
    return "orch · earlier engine"
rows = []
for d in sorted(glob.glob(".data/bench/*/")):
    name = os.path.basename(d.rstrip("/"))
    try:
        r = json.load(open(d + "report.json")); m = json.load(open(d + "manifest.json"))
    except Exception: continue
    case = m.get("case", "")
    if case not in ("boltons-buildgraph", "networkx-rollout", "networkx-timeflow") or not m.get("cold_cache_nonce"): continue
    acc = open(d + "acceptance.log").read() if os.path.exists(d + "acceptance.log") else ""
    fails = re.search(r"FAILED \(((?:failures|errors)=\d+(?:, (?:failures|errors)=\d+)?)\)", acc)
    if r.get("acceptance_passed") is True: a = "8/8"
    elif fails: a = f"{8 - sum(int(x) for x in re.findall(r'=(\d+)', fails.group(1)))}/8"
    else: a = "–"
    cs = r.get("usage", {}).get("calls", [])
    usd = sum((c.get("usage") or {}).get("provider_estimate_usd") or 0 for c in cs if "claude" in (c.get("model") or ""))
    cny = (r.get("usage", {}).get("totals", {}).get("costs", {}).get("settled_cents", {}).get("known_sum") or 0) / 100
    total = usd + cny / RATE
    rows.append((case, cfg(name, r), name, a, round(r.get("wall_s") or 0), total, NOTES.get(name, "")))
print("| case | configuration | run | acceptance | wall | cost ≈ USD | note |")
print("|---|---|---|---:|---:|---:|---|")
for case, c, name, a, w, t, n in sorted(rows):
    print(f"| {case} | {c} | `{name}` | {a} | {w} s | {'$%.2f' % t if t else 'not reported'} | {n} |")
