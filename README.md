# orch — plan with a strong model, build in parallel with a cheap one, merge only what the tests prove

`orch` turns a written specification into working code in an existing git repository:

1. **Plan.** A strong model (Claude Opus 5.5) splits the spec into tasks with explicit
   boundaries, owned files and interfaces.
2. **Freeze tests.** For every task — and for the task that integrates the others — a
   cheaper model (DeepSeek V4.1 Flash) writes independent unit tests. They are linted,
   checked against the stubs, and frozen before any implementation exists.
3. **Build in parallel.** Builders implement tasks concurrently in isolated git worktrees.
   Every test run happens inside an OS sandbox (bubblewrap + seccomp on Linux, Seatbelt on
   macOS) with no network.
4. **Merge only what passes.** A task merges onto `orch/<run_id>` only when its frozen tests
   pass; a failure walks a ladder (retry with the failure, sampled candidates, Opus repair,
   replan) instead of starting over. Your checked-out branch is never touched — you review
   and merge the result branch yourself.

Everything is recorded: every model call, token, cache hit, cost and state transition, so a
run can be resumed, audited (`orch audit`), traced (`orch trace`, `orch why`) and watched
live (`orch watch`).

> **Status: alpha.** Linux is the primary platform; macOS works but is less exercised.
> See [Benchmarks](#benchmarks) for what is and is not established yet.

## Requirements

- Linux with [bubblewrap](https://github.com/containers/bubblewrap) (`apt install bubblewrap`),
  or macOS (uses the built-in `sandbox-exec`)
- Python 3.12+ and git
- [Claude Code](https://code.claude.com) logged in (`claude auth login`) — the planner
- A DeepSeek API key — test design and builders. The official DSH CLI is used when
  installed, otherwise orch calls the DeepSeek API directly.

## Install

```sh
uv tool install "orchestragent[index] @ git+https://github.com/Orchestragent/orchestragent"
# or: pipx install "orchestragent[index] @ git+https://github.com/Orchestragent/orchestragent"
orch keys dsh        # paste your DeepSeek key (stored locally, 0600 / Keychain)
orch doctor          # checks models, keys and the sandbox
```

`[index]` adds tree-sitter for non-Python code indexing; without it orch falls back to
Python's `ast`. Data (keys, spend ledger) lives in `~/.local/share/orch`
(`~/Library/Application Support/orch` on macOS) or `$ORCHESTRA_DATA_DIR`.

## Quick start

Write the requirements as numbered, testable statements — they are the authoritative input:

```markdown
# spec.md
R1. textstats.py: word_count(text: str) -> int counts whitespace-separated words;
    non-str input raises TypeError.
R2. textstats.py: top_words(text, n) returns the n most frequent lower-cased words with
    counts, ties broken alphabetically; n must be a non-negative int, else ValueError.
R3. slug.py: slugify(text) lower-cases, replaces runs of non-alphanumerics with "-",
    and strips leading/trailing "-".
```

```sh
cd your-project                       # a git repo with at least one commit
orch plan --distributed spec.md       # plan + frozen tests, prints the run id
orch run --notify                     # parallel builders, gated merges; desktop alert at the end
orch watch                            # (another terminal) live view of the whole system
orch status && orch cost              # outcome and spend
git diff HEAD...orch/<run_id>         # review, then merge it yourself
```

## Use orch from other agents (MCP)

`orch mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server over stdio.
An agent can hand a whole multi-module build to orch, keep working, and review the branch.

```sh
orch mcp --install claude-code      # or: cursor, claude-desktop (config is merged, backed up)
```

Or by hand — `claude mcp add orch -- orch mcp`, or in `.cursor/mcp.json` /
`claude_desktop_config.json`:

```json
{ "mcpServers": { "orch": { "command": "orch", "args": ["mcp"] } } }
```

Tools: `orch_start` (plan + run in the background, returns a job id), `orch_wait`
(bounded blocking wait), `orch_status`, `orch_report` (outcome, branch, diff stat),
`orch_cost`, `orch_cancel`, `orch_doctor`.

## Watch it work

`orch watch` draws the live architecture in the terminal — planner, every test-design
shard and builder, the DeepSeek and Claude APIs, the sandbox and the merge gate — with
messages moving along the edges, a per-lane timeline and running cost. It degrades from
256 colours to monochrome/ASCII and to a plain text stream when there is no TTY, so it
works over plain SSH. `orch watch --replay` replays a finished run; `orch watch --web`
serves the same view to a browser (loopback by default; any other bind requires a token).

## Benchmarks

Boltons "resumable checkpoint queue" case — 4 requirements, existing 18k-line repository,
8 hidden acceptance tests. Cold provider caches, runs interleaved, same inputs
(full data in [`docs/measurements`](docs/measurements)):

| | runs | acceptance | wall time (median) | cost per run |
|---|---:|---:|---:|---:|
| Claude Opus 5.5 (high), single native agent | 4 | 4/4 | 388 s | $1.42–1.84 |
| orch (Opus medium plan + DeepSeek Flash) | 4 | 4/4 | 444 s | ≈ $0.50–0.86 |

On this medium-sized task orch delivers the same result at roughly a third of the cost and
similar wall time. **Larger projects are work in progress**: on earlier engine versions,
three larger cases (up to a 116k-line repository) were won by the single agent because
orch's planning failed or rework accumulated; current-engine reruns are underway and will
be published whether or not they favour orch.

## How it is built

- `src/orchestragent/orch_run.py` — planner, test design, scheduler, failure ladder
- `orch_thin.py` / `orch_plan.py` — boundary plans, lint, frozen-test shards
- `orch_build.py` / `orch_context.py` / `orch_index.py` — builder context packs and code index
- `platform.py` / `live_workspace.py` — sandboxed test execution
- `orch_watch*.py` — live views · `orch_mcp.py` — MCP server · `orch_cli.py` — the CLI

Development: `uv sync && uv run pytest -q && uv run ruff check src tests && uv run mypy`.
Detailed design notes (Chinese): [docs/ORCH.md](docs/ORCH.md).

## Security model

Models return JSON; only the controller writes files and runs commands. Generated tests
run in the OS sandbox without network. Keys stay on your machine (Keychain on macOS, a
0600 file elsewhere) and are never logged. There is no local spending cap: spend is
recorded and shown by `orch cost` / `orch doctor`, and only your provider balance limits it.

## License

Apache-2.0 — see [LICENSE](LICENSE).
