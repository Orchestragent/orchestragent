# orch studio: API contract (v1)

`orch studio` serves one repository's runs to an interactive client: the task graph, each
node's own state, a conversation with each node's agent, per-node model choice, and
settings (keys, subscription logins, role models). The macOS app (`apps/macos`, see
[STUDIO-MACOS.md](STUDIO-MACOS.md)) is the first client; anything that speaks HTTP can be
another.

Everything a client can do goes through the same engine and gates as the CLI and MCP: a
person's guidance only reaches the node it was given to, a node only ever changes its own
files, and frozen checks decide every merge.

## Launch

```
orch studio --repo DIR [--port 0] [--bind 127.0.0.1] [--token TOKEN] [--machine] [--demo]
```

- `--port 0` picks a free port. `--bind` defaults to loopback; any other address needs a
  token (one is always generated anyway).
- `--machine`: after the socket is bound, print exactly ONE JSON line to stdout and nothing
  else afterwards (logs go to stderr):
  `{"url": "http://127.0.0.1:53817/", "token": "…", "pid": 4242, "version": "0.2.0", "api": 1}`.
  The server exits when its stdin reaches EOF, so a parent app that dies takes it along.
- `--demo`: no repository or providers needed. Serves a scripted in-memory project that
  changes over time (nodes start, fail, park, merge) and accepts every POST, so a client can
  be built and tested without spending anything. `--repo` is ignored.
- Remote use: run `orch studio --repo DIR --port 8790` on the server, tunnel
  `ssh -L 8790:localhost:8790 host`, then connect the client to `http://localhost:8790/`
  with the printed token.

## Conventions

- Auth: every request carries `Authorization: Bearer <token>`. (Browsers may instead use
  `?token=` once, swapped for an HttpOnly cookie, but every POST still needs the header
  `Authorization: Bearer` or `X-Orch-Token`; a cookie alone never authorises a change.)
- JSON in and out, UTF-8. Times are Unix seconds (float). Money: `usd` float (Claude and
  other USD-billed providers), `cny` float (DeepSeek).
- Errors: HTTP status plus `{"error": {"code": "...", "message": "..."}}` where `message`
  is a sentence a person can read. Codes: `bad_request` 400, `unauthorized` 401,
  `not_found` 404, `conflict` 409 (the action is not allowed in the node's/run's current
  state), `invalid` 422 (e.g. an unknown route), `internal` 500.
- `run` and `task` are query parameters on GET and body fields on POST.
- Unknown fields may be added in later versions; clients must ignore them.

## Types

```
Run = {
  id: "r7313527a", title: "first line of the spec", status, live: bool,
  created: 1762107000.0, tasks: 5, done: 3, parked: 1,
  cost: {usd: 0.84, cny: 1.97}
}
# status: planning | running | done | parked | paused | cancelled | failed
# live: a process is working on it right now

Route = {
  label: "dsh/deepseek-flash:low",            # stable id; POST it back to choose it
  provider: "dsh", model: "deepseek-flash", effort: "low",
  vendor: "deepseek",                          # see vendors below
  name: "DeepSeek V4.1 Flash · low",           # display text
  verified: true                               # part of the benchmarked set
}
# vendor: anthropic | deepseek | openai | cursor | google | moonshot | qwen | person | other

Diagnosis = {
  kind: "own",                                 # see table below
  summary: "2 of its own checks fail: TestT003_R3_escape …",
  blamed: ["T-001"],                           # other nodes implicated
  checks: ["cmd:{…}"]                          # failing check ids when known
}
```

| kind | meaning |
|---|---|
| `done` | merged; its checks pass on the integration branch |
| `running` | an attempt, verification or merge is in progress |
| `queued` | ready, waiting for a worker slot |
| `waiting` | dependencies not finished yet (`blamed` = those) |
| `own` | its own checks fail on its own code |
| `upstream` | the failure output names files other nodes own, or the merge gate failed on other nodes' checks (`blamed` = those nodes) |
| `test` | the builder disputed a frozen test; a review decides |
| `environment` | a check could not run (exit 126/127: tool missing) or timed out |
| `replanned` | replaced by new nodes (see `replaced_by`) |

```
Node = {
  id: "T-003", goal: "ExportJSON writes canonical JSON", status: "PARKED",
  since: 1762107400.0,                         # last status change
  attempts: 4, rung: "replan",                 # first try | retry | samples | Claude patch | thicken | replan | test review
  route: Route, route_override: false,         # override: a person chose this node's model
  files: ["api/ledger/export.go"], reqs: ["R3"], depends_on: ["T-001", "T-002"],
  diagnosis: Diagnosis,
  cost: {usd: 0.21, cny: 0.35},
  person: {messages: 2, taken: false},
  ripple: "",                                  # "" | affected | unaffected (after an upstream change)
  replaced_by: []
}
# status: PLANNED READY RUNNING VERIFYING MERGING DONE PARKED REPLANNED
```

## Endpoints

### GET /api/boot
`{api: 1, version, repo: "/path/to/repo", repo_name, platform: "darwin"|"linux", demo: bool, runs: [Run]}`
(runs newest first)

### GET /api/runs → `[Run]`

### GET /api/graph?run=ID
```
{
  run: Run & {started, elapsed_s, phase: "planning"|"test design"|"building"|"final gate"|"finished"},
  nodes: [Node],                               # in id order
  edges: [{from: "T-001", to: "T-003"}]        # from must finish before to
}
```

### GET /api/node?run=ID&task=T-003
Only this node's own information:
```
{
  node: Node,
  contract: "text", requirements: [{id: "R3", text: "…"}],
  files: [{path, content, state: "stub"|"built"|"candidate"}],   # owned files, newest version
  checks: [{id, command, files: [{path, content}]}],              # frozen; read-only
  attempts: [{
    n: 3, label: "a3", route: Route, rung, started, ended,       # ended null while running
    outcome: "passed"|"failed"|"running"|"error"|"replanned",
    category: "test"|"integration"|"edit"|"provider"|…,
    report: "failure output tail (≤ 8 KB)", diff: "unified diff of its files",
    summary: "the agent's own one-line summary"
  }],
  thread: [ThreadItem],
  routes: [Route]                              # what this node may switch to
}
ThreadItem = {
  id, at, author: "you"|"agent"|"orch",
  kind: "message"|"summary"|"started"|"failed"|"passed"|"merged"|"parked"|"ripple"|"route"|"note",
  text: "…", attempt: 3                        # attempt optional
}
```

### POST /api/node/message `{run, task, text, rework: bool}`
Guidance for this node's agent; every later attempt of this node sees it (only this node).
A PARKED node returns to READY with a fresh ladder. For a DONE node, `rework: true` reopens
it to be rebuilt with the guidance (then the ripple below applies); `rework: false` keeps it
DONE and the guidance waits for a later rebuild. `409 conflict` for REPLANNED nodes and runs
that were cancelled. Starts the run's worker process if none is live.
→ `{ok: true, node: Node}`

### POST /api/node/route `{run, task, route}`
`route` is a `label` from that node's `routes`, or `""` for the role default. Takes effect
from the node's next attempt. → `{ok: true, node: Node}`

### POST /api/node/retry `{run, task}`
PARKED → READY with a fresh ladder (no new guidance). → `{ok: true, node: Node}`

### POST /api/run/start `{spec}`
Plan and run a new spec in the background (title = its first line). → `{ok: true, run: Run}`

### POST /api/run/resume `{run}` · POST /api/run/cancel `{run}` → `{ok: true, run: Run}`

### GET /api/settings
```
{
  providers: [{
    name: "claude", vendor: "anthropic", title: "Claude (Claude Code subscription)",
    kind: "subscription"|"key", configured: true,
    detail: "logged in" | "no key" | "CLI not installed" | …,
    login: {command: "claude", then: "/login"} | null       # for subscriptions
  }],
  roles: [{role: "planner"|"test_designer"|"builder"|"reviewer", title, description,
           route: Route, default: Route}],
  routes: [Route]                                           # every choice configured providers allow
}
```

### POST /api/settings/key `{provider, key}`
Stores the key (macOS Keychain / a 0600 file on Linux). The key is never returned, logged
or echoed. → `{ok: true, provider: {…configured: true}}`

### POST /api/settings/role `{role, route}` (`route: ""` restores the default) → `{ok: true}`

### GET /api/events?run=ID — Server-Sent Events
- `event: hello` `{seq}` on connect
- `event: changed` `{run, seq}` when anything in that run changes → refetch `/api/graph`
  and the open `/api/node`
- `event: runs` `{}` when the runs list changes (a run started or finished)
- `event: ping` `{}` every 10 s

Without `run`, only `runs` and `ping` are sent. Reconnect with backoff when the stream drops.

## Change ripple

When a DONE node is rebuilt (a person's `rework`), its merge runs every merged node's
checks, as always, and its own checks must pass. A downstream node whose checks still pass
is marked `unaffected` (no model call). One whose checks now fail is reopened with the
failure and the upstream change as context (`affected`), and the same ripple applies when
it merges. Failing checks of nodes that do not depend on it still reject the change.
