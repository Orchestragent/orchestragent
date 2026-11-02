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
- Models: for the verified (benchmarked) set, the two below; any role can instead use
  another CLI subscription or API key — see [Models](#models-bring-your-own-cli-or-key-swap-any-role).
- [Claude Code](https://code.claude.com), authenticated the way Claude Code supports — the
  planner. Either log in with a Claude subscription (`claude auth login`), or use an
  Anthropic API key exactly as Claude Code documents it (`export ANTHROPIC_API_KEY=...`;
  `ANTHROPIC_BASE_URL` for a gateway; Bedrock / Vertex settings also work). orch passes
  those settings to the `claude` process only and never stores them. `orch doctor` shows
  which mode is in use. With an API key, planning is billed per token by Anthropic.
- A DeepSeek API key — test design and builders. The official DSH CLI is used when
  installed, otherwise orch calls the DeepSeek API directly.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/Orchestragent/orchestragent/main/install.sh | sh
```

The installer (macOS and Linux) checks git and the sandbox, installs [uv](https://astral.sh/uv)
and orch as an isolated tool, offers to store your DeepSeek key, registers `orch mcp` with
Claude Code / Claude Desktop / Cursor when present, and finishes with `orch doctor`.
`sh install.sh --dry-run` shows every step without changing anything.

On macOS with Homebrew (installs shell completion for zsh, bash and fish too):

```sh
brew install orchestragent/tap/orchestragent
```

Or by hand:

```sh
uv tool install "orchestragent[index] @ git+https://github.com/Orchestragent/orchestragent"
# or: pipx install "orchestragent[index] @ git+https://github.com/Orchestragent/orchestragent"
orch keys dsh        # paste your DeepSeek key (stored locally, 0600 / Keychain)
orch doctor          # checks models, keys and the sandbox
```

Shell completion without Homebrew: `orch completion zsh > "${fpath[1]}/_orch"` (or `bash`,
`fish`). `[index]` adds tree-sitter for non-Python code indexing; without it orch falls back to
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

## Models: bring your own CLI or key, swap any role

orch is the architecture; the models are yours. Every role can be served by a CLI you
are logged into with your own subscription (**BYOC**) or by an API key (**BYOK**):

| provider | how | kind |
|---|---|---|
| `claude/<model>:<effort>` | Claude Code — `claude auth login`, or `ANTHROPIC_API_KEY` as Claude Code documents | BYOC or BYOK |
| `codex/<model>:<effort>` | Codex CLI, logged in with your ChatGPT plan or key | BYOC or BYOK |
| `cursor/<model>` | Cursor CLI, logged in with your Cursor plan | BYOC |
| `dsh/deepseek-flash:<effort>` | DeepSeek (DSH CLI or direct API) — `orch keys dsh` | BYOK |
| `<name>/<model>[:effort]` | any OpenAI-compatible API you register (OpenAI, Qwen, Moonshot, Zhipu, OpenRouter, a local Ollama or vLLM, ...) | BYOK |

```sh
orch providers add openrouter --base-url https://openrouter.ai/api/v1 --key-env OPENROUTER_API_KEY
orch providers add local --base-url http://localhost:11434/v1     # Ollama, no key needed
orch keys qwen                                                     # or store a key locally
orch providers test openrouter some/model                          # one tiny real request
orch models set builder openrouter/some/model
```

The four roles and the **verified set** — the combination the benchmarks below measured.
Keep it to reproduce our results ("copy the homework"), or swap anything:

| role | verified default | what it does |
|---|---|---|
| planner | `claude/claude-opus-5-5:medium` | splits the spec into tasks, boundaries and interfaces |
| test_designer | `dsh/deepseek-flash:low` | writes each task's frozen tests |
| builder | `dsh/deepseek-flash:low` | implements tasks against the frozen tests |
| reviewer | `planner` (or `opus-low`, `sonnet`, `sonnet-high`, `dsf`, `off`) | checks each test shard before freezing |

```sh
orch models                 # each role's model, and whether it is the verified set
orch models reset           # back to the benchmarked set
```

Anything else works through the same adapters but has not been benchmarked: quality, speed
and cost may differ (for example, letting a model review its own tests missed correlated
mistakes in our runs). `orch doctor` says when a custom set is active. Roles need models
that reliably return JSON; strong reasoning matters most for the planner and test designer.

**Use providers within their terms.** Some consumer subscriptions do not allow automated
or third-party use; don't connect those (orch refuses the ones it knows, e.g. Kimi's).
See [DISCLAIMER.md](DISCLAIMER.md).

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
similar wall time.

**Larger projects (work in progress).** Three multi-module features on existing
repositories, 8 hidden acceptance tests each, single runs, cold caches:

| case (repository) | single Opus 5.5 high | orch |
|---|---|---|
| build graph + cache + runner (Boltons, 18k lines) | 8/8 · 778 s · $3.32 | **8/8 · 511 s · ≈ $0.61** |
| rollout controller (NetworkX, 116k lines) | 8/8 · 1030 s · $2.72 | 7/8 · 984–1325 s · $1.0–4.1 |
| time-expanded flows (NetworkX, 116k lines) | 8/8 · 1078 s · $3.82 | 7/8–8/8 · 1259–1443 s · $2.5–3.5 |

orch now wins clearly on the Boltons case. On the NetworkX cases it still misses one
requirement clause in some runs: frozen tests that did not pin a stated behaviour down
(work on test review is ongoing). These numbers will be updated as runs complete.

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

Apache-2.0 — see [LICENSE](LICENSE). Use of model providers through orch is subject to
their terms; see [DISCLAIMER.md](DISCLAIMER.md).
