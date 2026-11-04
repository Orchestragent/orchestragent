# Benchmark cases

Each case is a feature request against a pinned repository, graded by hidden acceptance
tests that no model ever sees.

A case directory holds:

- `spec.md`: the feature request, as numbered requirements;
- the hidden acceptance files;
- `acceptance.json`, which names those files and the shell command that runs them:

```json
{"command": "python3 -m unittest -q test_acceptance", "files": ["test_acceptance.py"]}
```

The acceptance tests can be in any language, and so can the project. After a run, the files
are copied into a disposable worktree of the result. The command then runs there in orch's
offline toolchain sandbox, and its exit code decides. Both arms are graded by that same
check: orch, and a single native agent working in the same repository.

```sh
python3 scripts/orch_bench.py --source <repo> --revision <sha> \
    --case examples/orch-bench/<case> --output .data/bench/<run> --cold
python3 scripts/orch_bench.py ... --single-agent --effort high   # the baseline arm
```

`--cold` prefixes the spec with a fresh nonce, so no provider prompt cache carries over
between runs.

## Cases

| case | repository | what it asks for |
|---|---|---|
| `boltons-checkpoint` | Boltons `d31d821d` | a checkpointed iterator feature |
| `boltons-buildgraph` | Boltons `d31d821d` | build graph + single-flight cache + parallel runner (5 modules) |
| `networkx-rollout` | NetworkX `4fa222d2` | deployment planning and a durable rollout controller (8 modules) |
| `networkx-timeflow` | NetworkX `4fa222d2` | time-expanded flows, schedules and outage audit (9 coupled modules) |
| `polyglot-ledger` | a Go service + TypeScript client seed | CSV parsing and canonical JSON export in Go, a TypeScript client that reads, formats and reconciles it, and a cross-language contract |
| `meridian` | an empty Rust + Go + TypeScript + Python + Java seed | a sharded, replicated key-value store: storage node, router, client, rebalancing planner and metrics aggregator, with a cluster-level acceptance across all five (20 requirements) |
| `tessera` | an empty C + Go + Ruby seed | a binary record archive: a C codec library with dump/filter tools, a Go pack/unpack/verify/merge command and a Ruby library and report command, which must agree byte for byte (18 requirements) |

`polyglot-ledger` mixes languages on purpose: its hidden acceptance runs Go tests, vitest
tests and the TypeScript type check, and one requirement (R7) only holds if the two
components agree byte for byte.

`tessera` is the held-out case: it was designed after the 2026-11-03 redesign, by an agent that never read orch's source, in two languages (C, Ruby) orch had never built. It must pass with no change to orch.
