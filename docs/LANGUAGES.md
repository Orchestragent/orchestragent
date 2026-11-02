# Any language, and several at once

orch does not decide which languages a project uses. A real project mixes them: a Go service,
a TypeScript web app, a Rust core, Python scripts, SQL, shell. The models already write all of
them, so orch leaves those choices to the models and keeps deterministic gates only where they
protect correctness and safety.

## What the models decide

- **Planner (Opus):** the requirements and tasks, and for each task the files it owns and the
  stub interfaces. Stubs can be written in any language. Their bodies only fail with "not
  implemented".
- **Test designer (one per task):** the frozen test files, in whatever framework the tested
  component uses, plus **the shell command that runs exactly those tests**. Examples:
  - `cd web && npx vitest run test/cart.test.ts`
  - `go test ./api/...`
  - `CARGO_TARGET_DIR="$TMPDIR/t" cargo test --test orch_t_005`
  - `cd tools && python3 -m unittest -q test_slug`
- **Builder:** the implementation of the files it owns, in their languages.

## What orch enforces (language-neutral)

1. **Sandbox.** Every command runs from the repository root under `/bin/sh -c`.
   - It has no network.
   - It cannot see users' home directories, except the toolchains it is given.
   - It can write only inside its worktree and `$TMPDIR`.
   - Installed toolchains are mounted read-only, and so are the repository's ignored
     dependency directories (`node_modules`, `.venv`, `vendor`, …).
2. **Ownership.** A task edits only the files it owns.
3. **Frozen checks.** Test files and their commands are frozen with the plan. A check's id is
   `cmd:{command, files}`, so it carries everything needed to run it again in any worktree.
4. **Red, then green.**
   - At design time, a check must run on the stubs and fail. A command that cannot run
     (exit 126/127) or that passes on stubs is sent back to the designer.
   - After the build, the check must exit 0.
5. **Merge only on green.** A final gate runs every task's check together. A regression is
   bisected to one task and that task is rebuilt.
6. **Builds stay clean.** Sandbox scratch space and linked dependency directories never enter
   a commit.

None of these gates parses a language. Reports such as JUnit XML may still be shown to the
models to help them, but a report never decides pass or fail.

## One pipeline

There is one pipeline, and it is language-agnostic. orch has no Python engine, no language
table and no routing by project type: a Python project, a Go service, a TypeScript app and a
repository mixing all three go through the same planning, test design, build and merge
gates.

## Toolchains in the sandbox

Toolchains are discovered rather than tabulated: orch mounts read-only

- the install roots of PATH entries;
- `*_HOME`, `*_ROOT` and `GOPATH` directories;
- common toolchain homes, such as rustup's toolchains or Go/Maven/Gradle caches;
- anything listed in `ORCH_SANDBOX_READ` (colon-separated).

It never mounts the home directory itself or private directories (`.ssh`, `.config`, cloud
credentials, agent logins).

Dependencies are never downloaded during a run. The project must already be installed and
able to run its tests offline: `npm ci`, `go mod download`, `cargo fetch`, a populated
`.venv`, and so on.

## What never enters the sandbox

orch never mounts or links these into the sandbox:

- the home directory itself, credential stores (`.ssh`, `.gnupg`, `.aws`, `.config`,
  `~/Library`, keyrings) and agent logins;
- orch's own data directory, where API keys live;
- ignored directories whose names suggest secrets (`secrets/`, `credentials/`,
  `.env*`, `keys/`, ...).

Ignored files are never linked at all. Only ignored *directories* are linked (installed
dependencies), and they are mounted read-only.

## Status (2026-11-02)

- One language-agnostic pipeline. The Python-only machinery is removed: AST lints,
  unittest/pytest runners, the pinned pytest environment, mutation testing, and the
  routing by project type.
- Verified end to end with scripted models on a shell + Python project, and with real models
  (Opus 5.5 plans, DeepSeek V4.1 Flash designs and builds) on `polyglot-ledger`, a Go
  service plus a TypeScript client graded by hidden Go, vitest and tsc checks.
- Benchmarks are run with `scripts/orch_bench.py` (see `examples/orch-bench`).
