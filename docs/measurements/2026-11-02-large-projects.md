# Large features: orch vs a single Opus 5.5 agent at medium, high and max (2026-11-02)

Three cases, each a multi-module feature graded by 8 hidden acceptance tests that no model
sees: `boltons-buildgraph` (5 modules in Boltons, 18k lines), `networkx-rollout` (8 modules
in NetworkX, 116k lines) and `networkx-timeflow` (9 tightly coupled modules in NetworkX).
Specs, acceptance tests and source revisions are in [`examples/orch-bench`](../../examples/orch-bench).

Method: every run starts from cold provider caches (a per-run nonce), the same spec and the
same pinned source. Both sides run unattended from the spec to the end; nobody intervenes.
The single agent is Claude Code (Opus 5.5) at the given effort with read, edit, write and a
sandboxed test helper, and a 2400 s wall limit (the same limit orch has). orch uses its
verified model set (planner Opus 5.5 medium, tests and builds DeepSeek V4.1 Flash low)
unless the configuration names a different test reviewer. Costs: Claude at provider list
price as reported by Claude Code; DeepSeek as settled in CNY, converted at 7.1 CNY/USD.

## Summary

| case | Opus 5.5 · medium | Opus 5.5 · high | Opus 5.5 · max | orch (verified set) |
|---|---|---|---|---|
| boltons-buildgraph | – | 8/8 · 778 s · $3.32 | – | **8/8 · 511 s · $0.62** |
| networkx-rollout | 8/8 · 677 s · $1.94 | 8/8 · 1030 s · $2.72 | did not finish in 2400 s | 8/8 · 975 s · **$1.54** |
| networkx-timeflow | 8/8 · 632 s · $2.33 | 8/8 · 1078 s · $3.82 | 8/8 · 2219 s · $10.29 | 7/8 · 1022 s · **$1.60** |

What the data says:

- **Cost.** orch is the cheapest configuration on every case: about 5× cheaper than Opus
  high on the Boltons build graph, and below even Opus medium on NetworkX.
- **Speed.** orch is fastest on the Boltons build graph. On NetworkX, Opus 5.5 at medium is
  the fastest configuration (≈ 650 s); orch is about as fast as Opus high.
- **Reliability.** Opus 5.5 at medium and high passed every run on NetworkX. orch reached
  8/8 in 6 of its 18 NetworkX runs across all configurations below, and in most others
  missed exactly one hidden requirement — a different one from run to run. This is the
  gap we are working on.
- **More effort is not better for the single agent.** Opus 5.5 at max took 3.5× as long
  as medium, cost 4.4× as much on timeflow, and did not finish rollout within 2400 s.
- **A stronger test reviewer does not close the gap.** Reviewing each test shard with Opus
  (planner or low), Sonnet 5.5 high or DeepSeek Flash before freezing cost 2–3× more and did
  not raise the pass rate; the verified set keeps the reviewer off.

## Every run

Invalid runs are listed with the reason and excluded from the summary. Two Opus max runs
were cut off by limits in our own benchmark harness (a per-call timeout, then a 2 MB cap
on the agent's output stream); both were fixed before the valid reruns.

| case | configuration | run | acceptance | wall | cost ≈ USD | note |
|---|---|---|---:|---:|---:|---|
| boltons-buildgraph | orch · earlier engine | `2026-11-01-large-14dc757-boltons-buildgraph-orch` | 8/8 | 511 s | $0.62 |  |
| boltons-buildgraph | orch · earlier engine | `2026-11-01-large-4b12bef-boltons-buildgraph-orch` | – | 425 s | $0.54 | infrastructure error during planning; no result |
| boltons-buildgraph | orch · reviewer opus-low | `2026-11-02-C-f4857a0-boltons-buildgraph-orch-opus-low` | 8/8 | 908 s | $1.75 |  |
| boltons-buildgraph | single Opus 5.5 · high | `2026-11-01-large-4b12bef-boltons-buildgraph-single` | 8/8 | 778 s | $3.32 |  |
| networkx-rollout | orch · coverage review on | `2026-11-02-cov-5588d86-networkx-rollout-r1` | 7/8 | 1325 s | $3.12 |  |
| networkx-rollout | orch · earlier engine | `2026-11-01-large-14dc757-networkx-rollout-orch` | 7/8 | 984 s | $1.04 |  |
| networkx-rollout | orch · earlier engine | `2026-11-01-large-4b12bef-networkx-rollout-orch` | 7/8 | 1157 s | $1.10 |  |
| networkx-rollout | orch · reviewer DeepSeek Flash | `2026-11-02-B-f72c0ed-networkx-rollout-dsf` | 8/8 | 1169 s | $1.86 |  |
| networkx-rollout | orch · reviewer off (current default) | `2026-11-02-B-f72c0ed-networkx-rollout-off` | 8/8 | 975 s | $1.54 |  |
| networkx-rollout | orch · reviewer opus-low | `2026-11-02-C-f4857a0-networkx-rollout-orch-opus-low` | 4/8 | 1513 s | $5.23 |  |
| networkx-rollout | orch · reviewer planner (Opus) | `2026-11-02-B-f72c0ed-networkx-rollout-opus` | 8/8 | 1086 s | $3.92 |  |
| networkx-rollout | orch · reviewer sonnet-high | `2026-11-02-D-7ccc8a8-networkx-rollout-orch-sonnet-high` | 7/8 | 1177 s | $3.24 |  |
| networkx-rollout | orch · test review on | `2026-11-02-rev-f6cdf35-networkx-rollout-r1` | 7/8 | 1050 s | $4.05 |  |
| networkx-rollout | single Opus 5.5 · high | `2026-11-01-large-4b12bef-networkx-rollout-single` | 8/8 | 1030 s | $2.72 |  |
| networkx-rollout | single Opus 5.5 · max | `2026-11-02-C-f4857a0-networkx-rollout-single-max` | 6/8 | 1310 s | not reported | invalid: killed by the harness's 1200 s per-call timeout |
| networkx-rollout | single Opus 5.5 · max | `2026-11-02-C2-48ee0bf-networkx-rollout-single-max` | 8/8 | 2309 s | not reported | invalid: killed by the harness's 2 MB stream cap (since fixed); usage lost |
| networkx-rollout | single Opus 5.5 · max | `2026-11-02-E-4cd55d4-networkx-rollout-single-max` | – | 2401 s | not reported | did not finish within the 2400 s wall limit |
| networkx-rollout | single Opus 5.5 · medium | `2026-11-02-C-f4857a0-networkx-rollout-single-medium` | 8/8 | 677 s | $1.94 |  |
| networkx-timeflow | orch · coverage review on | `2026-11-02-cov-5588d86-networkx-timeflow-r1` | 8/8 | 1443 s | $3.51 |  |
| networkx-timeflow | orch · earlier engine | `2026-11-01-large-14dc757-networkx-timeflow-orch` | 7/8 | 1259 s | $2.50 |  |
| networkx-timeflow | orch · earlier engine | `2026-11-01-large-4b12bef-networkx-timeflow-orch` | 8/8 | 1847 s | $3.29 |  |
| networkx-timeflow | orch · reviewer DeepSeek Flash | `2026-11-02-B-f72c0ed-networkx-timeflow-dsf` | 3/8 | 1657 s | $2.94 |  |
| networkx-timeflow | orch · reviewer off (current default) | `2026-11-02-B-f72c0ed-networkx-timeflow-off` | 7/8 | 1022 s | $1.60 |  |
| networkx-timeflow | orch · reviewer opus-low | `2026-11-02-C-f4857a0-networkx-timeflow-orch-opus-low` | 7/8 | 1299 s | $5.11 |  |
| networkx-timeflow | orch · reviewer planner (Opus) | `2026-11-02-B-f72c0ed-networkx-timeflow-opus` | 7/8 | 1406 s | $4.05 |  |
| networkx-timeflow | orch · reviewer sonnet-high | `2026-11-02-D-7ccc8a8-networkx-timeflow-orch-sonnet-high` | 8/8 | 1155 s | $5.15 |  |
| networkx-timeflow | orch · test review on | `2026-11-02-rev-f6cdf35-networkx-timeflow-r1` | 7/8 | 1340 s | $4.77 |  |
| networkx-timeflow | single Opus 5.5 · high | `2026-11-01-large-4b12bef-networkx-timeflow-single` | 8/8 | 1078 s | $3.82 |  |
| networkx-timeflow | single Opus 5.5 · max | `2026-11-02-C2-48ee0bf-networkx-timeflow-single-max` | 7/8 | 1947 s | not reported | invalid: killed by the harness's 2 MB stream cap; usage lost |
| networkx-timeflow | single Opus 5.5 · max | `2026-11-02-E-4cd55d4-networkx-timeflow-single-max` | 8/8 | 2219 s | $10.29 |  |
| networkx-timeflow | single Opus 5.5 · medium | `2026-11-02-C2-48ee0bf-networkx-timeflow-single-medium` | 8/8 | 632 s | $2.33 |  |
