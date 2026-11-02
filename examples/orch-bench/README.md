# Curated real-model pilot cases

`real-suite.json` contains six small, independent cases intended for a curated pilot.
They are not a representative benchmark and their results are not intelligence evidence.
The `reference/` JSON files map project-relative paths to trusted reference source; they
are test oracles only and are never included in a case seed or prompt.

Offline acceptance checks live in the suite manifest. `tests/test_orch_bench_cases.py`
materializes each reference and runs that case's held-out tests with the existing local
unittest runner. Reproduce them with `pytest -q tests/test_orch_bench_cases.py`; these
checks do not call a model provider.

## Pinned existing-repository trials

`boltons-checkpoint/` and `boltons-buildgraph/` are experimental feature tasks against a
fixed Boltons checkout. The latter connects graph algorithms, single-flight caching,
persistence and resource-aware parallel execution across five source modules. These
medium-repository cases are development data; both have now informed framework changes
and are ineligible for future policy-promotion held-outs.

The shared probe creates a fresh local clone, pins a pytest environment, checks declared
sandbox capabilities and keeps hidden acceptance outside the model's working checkout.
The existing Boltons overlay is reused explicitly:

```sh
PYTHONPATH=src uv run python scripts/orch_repository_probe.py \
  --source .data/bench/boltons-upstream \
  --revision d31d821d56c8f7756af9c30a864077f2e5855321 \
  --case examples/orch-bench/boltons-buildgraph \
  --source-path boltons \
  --sandbox-overlay examples/orch-bench/boltons-checkpoint/sandbox_conftest.py \
  --output .data/bench/buildgraph-preflight-new \
  --jobs 4 --max-calls 64 --max-cents 1500 --timeout 1200 --wall-limit 1200
```

This command is provider-free. A real cluster adds `--live --distributed`; a native
tool-using single Opus adds `--live --single-agent`. Each needs a distinct output directory.
Real execution uses subscription/API allocations and must respect the cumulative budget.
The single native session may make multiple internal model/tool rounds; its receipt is
not directly comparable to one cluster leaf call.
The native test helper accepts optional repository-relative pytest nodeids for local iteration;
the controller still runs the complete regression gate after the agent finishes. Nodeids reuse
the existing sandbox runner validation. Its narrow trailing argument wildcard follows the
[official permission-rule syntax](https://code.claude.com/docs/en/permissions#wildcard-patterns).
Each runner cleans only its own temporary directory, so overlapping invocations cannot delete
another call's active test data.

The recorded paired trial used engine `623e49c`; current code includes subsequent fixes
and will produce a new experiment. Never replace the recorded results with a later run.
[Recorded comparison](../../docs/measurements/2026-10-31-buildgraph-comparison.md) includes
the complete inputs, failed artifacts and usage. Static fixture/frontier replay uses the
tracked `scripts/audit_repository_fixtures.py` and makes no model calls.

`networkx-rollout` freezes a larger control-plane task on NetworkX 3.5 commit
`4fa222d2fb157e1b7f8c753c9f92e5907d1ddeb4`: optimal failure-domain placement,
edge-disjoint routing, rollout batches, atomic CAS journals, durable leases and a CLI.
The original 313 library source files contain 116,035 lines. Both routes have now run once on
engine `5fef719`; the native single delivered an artifact, while the cluster stopped during
boundary review. Two original acceptance defects were corrected after both runs and the same
artifacts were reassessed offline, without model reruns. This case has informed framework
changes and is no longer eligible for policy-promotion held-outs.
[Original results and corrected-oracle assessment](../../docs/measurements/2026-10-31-networkx-comparison.md).
Provider-free preflight uses the same command with `--source .data/bench/networkx-upstream`,
that revision, `--case examples/orch-bench/networkx-rollout`, `--source-path networkx`,
and a fresh output directory; omit the Boltons overlay. Its full upstream baseline passed
5446 tests with 335 optional-dependency skips. Project regression budgets are 600 wall seconds
and 300 CPU seconds; per-node unittest budgets remain 25/15 seconds. The budgets are included
in input matching. A preflight is not an agent-delivery or speed result. Negative preflight
alone does not establish oracle correctness; use an independent positive control before
spending on a new case.

`networkx-timeflow` modifies the existing minimum-cost-flow APIs and composes lower bounds,
time expansion, deterministic flow decomposition, outage analysis and a CLI. Reference code
is applied only in a separate positive-control clone; it never enters the agent checkout.
The final provider-free preflight passed its positive oracle and upstream regression gates.
[Frozen comparison protocol](../../docs/measurements/2026-10-31-timeflow-design.md).
The frozen paired trial is now observed: native Opus passed 8/8 in 768.77 seconds;
the cluster stopped at requirement-ownership lint before implementation. It provides no
speedup or token-saving evidence and is no longer an independent promotion holdout.
[Results and paused handoff](../../docs/measurements/2026-10-31-timeflow-comparison.md).
