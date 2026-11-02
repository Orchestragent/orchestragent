# Native Mac live execution v0.2

User authorized real-provider execution on this Mac on 2026-10-21. No API billing,
account switching, remote push, production servers or persistent launch agent authorized.
Codex native login reports ChatGPT; installed model catalog provides gpt-6-astra,
gpt-5.6-sol/terra/luna. Prefer gpt-6-astra at low reasoning. Claude native auth is logged out;
official native login is now authorized by the user. Real routing uses existing native subscription auth.

## Boundaries

First live slice: Python standard-library projects. Models return a structured plan,
file bundle and independent review. They have NO tool access, hooks, plugins or MCP.
Controller writes validated files into its own worktree; generated tests run under
macOS process sandbox with no network, credentials or access to the user home.
No dependencies are installed from model output. Other project types report limitations.

Single native client seam, owned by native worker:
`native.py` defines:
@dataclass(frozen=True) NativeResult:
  outcome:str; data:dict[str,object]; session_id:str="";
  usage:dict[str,object]=field(default_factory=dict); message:str=""
class NativeClients:
  __init__(self, root:Path, timeout:float=180)
  status(self)->dict[str,object]  # sanitized per-provider readiness; no secret/email
  call(self, provider:str, model:str, prompt:str, schema:dict[str,object],
       run_id:str, cancelled:Callable[[],bool])->NativeResult
Native outcomes ok, quota_exhausted, rate_limited, auth_required, timed_out,
cancelled, failed, waiting_approval, policy_blocked; no controller-level automatic retries/fallback. Native CLI may use bounded transport retries. Native session IDs retained,
but interrupted live calls pause on restart instead of automatic replay/spend.
NativeClients handles codex/claude; ProviderClients adds DSH with explicit API grants,
and blocked Cursor/Kimi routes. `status()` must verify native auth route under same clean env
used for calls. Reject API-key/unknown route before inference. Codex forced chatgpt config;
Claude safe-mode/restricted/strict-mcp/tools empty/no permissions prompts, never --bare.
Stdout JSONL parsed internally, stderr/credentials never persisted or logged. Bound output
size and timeout; terminate entire process group on timeout/cancel. Real invocation only
by controller after tests; worker must not launch paid/subscription inference itself.

Existing utility inventory: serialization.normalize/dumps; Store.transaction; Workspace.git;
hashlib.sha256 for digest. Original benchmark/statistical tools absent and not needed.
New shared native process runner is declared here; no duplicate canonical JSON/atomic writer.

## API / iOS additions (root owns backend, iOS worker owns ios/)

GET /health -> {mode:"mock"|"live", version:2}; --live enables server live task creation.
GET /providers -> {codex:{ready:bool,auth:string,...},claude:{ready:bool,auth:string,...}}.
POST /tasks optional mode:"mock"|"live"; absent defaults to server mode.
Mock scenarios still force mode=mock when scenario != happy; normal iOS sends chosen mode.
Task adds mode:string default mock; cost_usd becomes nullable for live unknown billing,
billing:string (mock / subscription / includes_api), usage:object, provider actual scheduler id.
Step adds provider:string, model:string, session_id:string; show in expanded details.
Live adds policy_blocked for unsupported provider policy/isolation. Live review is separate native session/provider;
no Claude credential => auth_required, no same-provider fallback. Settings may explicitly
select provider preference for review through existing instructions only until typed roles added.
Default live roles codex plan/implement, claude review, local test and deterministic summary.
Client shows LIVE vs MOCK based on selected task or health, never labels real cost $0.
Connection strip: subscription / exact charge unknown. Configuration preferences are used
in prompts and model selection, immutable snapshot; no direct billing enablement.
Settings sentence proposals in live server use actual scheduler schema output then same
versioned user apply API. Must not mutate permissions or billing fields.

Ownership this wave:
- native worker: src/orchestragent/native.py, tests/test_native.py only.
- iOS worker: ios/ only.
- root: all other files; integrate orchestration, workspace sandbox, settings and server.
Acceptance: full pytest/ruff/mypy once after integration, simulator build and tests;
real smoke only after native auth checks, one bounded task, no automatic retry costs.


## Current validation boundary

Codex real planning and implementation succeeded for the Unicode slugify smoke task.
Claude independent review is blocked on native subscription login; no multi-vendor
completion is claimed. iPhone simulator uses this loopback Mac service. Physical iPhone
access requires authenticated connectivity and installation confirmation separately.


## Expansion and local setup

`providers.ProviderClients` is the unified dispatch seam for all five names.
DSH uses the official headless CLI with tool/plugin/retry restrictions, an explicit
API budget grant and an ephemeral in-memory key. Calls currently remain policy_blocked:
the official agent loop can make further requests after unexpected tool calls, so a
single-request reservation does not yet establish a hard spend bound. Cursor remains policy_blocked until
extension and tool isolation are verified. Kimi Max remains policy_blocked for
unattended work under the subscription rules; official login supports manual use only.
No account-specific Cursor entitlement or API spend has been authorized.

`POST /providers/authorization/preview` accepts provider=dsh, cap_cents (1..50000),
funding_kind=balance|budget; returns a digest bound to that exact operation.
`POST /providers/authorization/apply` requires those fields plus operation_digest and
api_key. Keys never appear in responses, database or logs. This is native loopback
only and grants no model-controlled approval. `scripts/login.command` opens official
CLI login/configuration; DSH secrets are entered via getpass in the user's terminal.
