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

## Which pipeline runs

- **Pure Python projects** keep the tuned unittest/pytest pipeline. It is benchmarked, and its
  AST lints catch Python-specific test mistakes.
- **Everything else** uses the generic pipeline above:
  - a project with another build manifest anywhere (`package.json`, `go.mod`, `Cargo.toml`,
    Maven/Gradle files, `*.csproj`, …);
  - a Python project that also has such a manifest;
  - a brand-new, empty project.
- `ORCH_ENGINE=python|generic` overrides the choice.

This is routing only. The generic pipeline is free to use Python too. Once it matches the
Python pipeline on the Python benchmarks, the two converge.

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

## Status (2026-11-02)

- The generic pipeline runs end to end with scripted models on a mixed shell + Python
  project: plan → red gate → build → green → merge.
- The sandbox and checks were exercised by hand on vitest + tsc, go test, cargo test and
  unittest, side by side in one repository.
- Next:
  - a project-level check declared by the planner (pre-existing tests as a baseline);
  - integration checks across components;
  - a real multi-language benchmark against a single Opus agent.
