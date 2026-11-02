# Any-language orch: design

Status: design, 2026-11-02. Goal: orch builds features in projects of any language, with the
same guarantee it gives Python projects today — a task merges only when its frozen tests
pass in an offline sandbox. Python becomes one adapter, not the core.

## What is language-bound today

An audit found ~90 Python-specific places in seven groups:

| group | examples | approach |
|---|---|---|
| sandbox | seccomp and Seatbelt forbid fork/exec; the command is always `python -c` | **blocker**: run an argv; allow processes, keep the network denied |
| test execution and reports | unittest/pytest runners, traceback parsing, dotted ids | adapter `run()` + JUnit XML / native JSON reports |
| test identity | `test_r<N>_` names, `test_*.py`, `module.Class.method` ids | adapter `list_tests / file_of / req_of / shard_path` |
| stub and interface gates | `raise NotImplementedError`, AST shape rules, `compile()` | the toolchain is the linter (`tsc --noEmit`, `go vet`, `cargo check`) + tree-sitter signatures |
| test lints | AST fixture and constant-assertion lints | language-neutral gates (below); AST lints stay as Python extras |
| prompts | "stdlib unittest", "Python boundary plan" | adapter-supplied prompt fragments |
| paths and index | suffix allow-list `.py/.md/.txt/.json`; imports resolved for Python/JS only | adapter-supplied suffixes; tree-sitter imports per language; "all tests" when unsure |

## The adapter

```text
LanguageAdapter
  detect(repo) -> confidence           # files, manifests, lockfiles
  toolchain() -> Toolchain             # binaries + read-only caches to mount, env, rlimits
  stub_fragment / test_fragment / builder_fragment   # prompt text
  stub_check(files) -> errors          # compile/typecheck the boundary with the toolchain
  shard_path(task) / test_ids(file) / req_of(test_id) / file_of(test_id)
  run(workdir, ids|None, timeout) -> Report   # argv in the sandbox, native report parsed
  report: per-test outcome, failure message, file:line frames
```

Adapters, in order: **python** (today's code, moved behind the interface), **typescript /
javascript** (vitest or jest; npm, pnpm or yarn), **go** (`go test -json`), **rust**
(`cargo test`, nextest JUnit when present), **java/kotlin** (Gradle/Maven, Surefire JUnit XML),
and **generic**: the user gives a test command in `orch.toml` with `{files}` / `{tests}`
placeholders and a JUnit XML path; orch then works for any language at whole-suite precision.

## Language-neutral gates that keep tests honest

These replace the Python AST lints for every language:

1. **Compiles**: the shard compiles and type-checks against the frozen stubs.
2. **Red on stubs**: every frozen test fails while the code under test is a stub. A test
   that passes on stubs constrains nothing and is rejected.
3. **Requirement ids**: every test name carries its requirement (`R3` in the test or
   describe name); every requirement has at least one test.
4. **Integration shards** for tasks that cite another task's requirement (as today).
5. **Contract fidelity** in the planner: each spec-stated error at the operation the spec
   names; upstream conventions kept exactly.

## Sandbox

- Linux: bubblewrap with `--unshare-all` (no network namespace route), the toolchain and
  dependency caches bind-mounted read-only, a writable temp/home, rlimits per toolchain;
  seccomp keeps denying sockets other than AF_UNIX but allows fork/exec (passed with
  `--seccomp`), so compilers and test runners work.
- macOS: Seatbelt allows process-fork and process-exec only for the toolchain paths, denies
  the network and writes outside the workdir.
- Dependencies are never fetched inside the sandbox. `orch doctor` checks that the project's
  own tests pass offline (dependencies already installed: `node_modules`, Go module cache,
  `cargo fetch`, Maven/Gradle cache) and says how to prepare it if not.

## Plan

1. Sandbox runs an argv with processes allowed and network denied (Linux + macOS), with the
   existing Python runner moved onto it. All existing tests stay green.
2. Adapter interface; Python moved behind it with no behaviour change.
3. TypeScript/JavaScript adapter + generic adapter; a TypeScript benchmark case with hidden
   acceptance tests, compared with a single Opus agent.
4. Go and Rust adapters + benchmark cases. Then Java/Kotlin.
5. README: supported languages and how to prepare a project; fresh-user trial on a TS repo.
