# MVP contract v1

> 本文记录初始 Mock 基线；当前真实 Mac 执行增量以 [LIVE_CONTRACT.md](LIVE_CONTRACT.md) 为准。

All providers are Mock or disabled. Backend binds 127.0.0.1 only, port 8765.
No login or credential creation in this milestone. Loopback-only development HTTP,
no CORS; browser Origin requests rejected. A physical iPhone connection is deferred
until authenticated TLS transport is approved. iOS simulator can use localhost.

## HTTP JSON

GET /health -> {"mode":"mock","version":1}
GET /tasks -> {"tasks":[Task]} newest first.
POST /tasks body {"request_id":"UUID", "prompt":"text", "scenario":"happy"}
-> 201 Task; identical request_id and payload -> 200 same Task; changed payload -> 409.
Scenarios: happy, approval, rate_limit, quota, auth, timeout, failure, review_failure,
test_failure. Scenario is a Mock demo control, not prompt interpretation.
GET /tasks/{id} -> Task
GET /tasks/{id}/events?after=0 -> {"events":[Event],"cursor":integer}
POST /tasks/{id}/actions body {"action":"pause|resume|cancel"} -> Task
POST /tasks/{id}/approvals/{approval_id} body
{"decision":"approve|deny", "operation_digest":"exact displayed digest"} -> Task
Errors: {"error":"safe description"}, HTTP 400/404/409; requests max 16 KiB.

Task fields:
id, request_id, prompt, scenario, status, stage (all strings);
created_at, updated_at (UNIX seconds numbers); provider="mock";
session_id (string), attempt (integer), next_run_at (number);
result (string), error (string), revision (string), cost_usd (number, always 0);
steps: [{id, role, depends_on:[string], status, summary, revision}];
approval: null or {id, operation, operation_digest, revision, status};
artifacts: [{name, content}]. Additional internal fields may be ignored by client.
Statuses: queued, running, waiting_approval, rate_limited, quota_exhausted,
auth_required, timed_out, failed, paused, cancelled, completed.
Stages: plan, implement, review, test, summarize.
Event: {seq, task_id, kind, at, data:object}. Sequence is monotonic and persisted.
Polling every ~1 second is sufficient. Server persists progress without subscribers.
Resume allowed for paused, rate_limited, quota_exhausted, auth_required, timed_out;
Mock quota/auth/timeout scenarios intentionally recur on retry. No provider fallback.

## Python adapter boundary (owned by adapters worker)

Define in adapters.py:
@dataclass(frozen=True) StepContext:
  task_id:str; prompt:str; stage:str; scenario:str; attempt:int;
  session_id:str; revision:str; artifact:str; approved:bool=False
@dataclass(frozen=True) StepResult:
  outcome:str="ok"; summary:str=""; artifact:str="";
  session_id:str=""; retry_after:float=0.0; cost_usd:float=0.0
class Adapter(Protocol):
  def run(self, context:StepContext) -> StepResult: ...
class MockAdapter: implements run
class CodexAdapter / ClaudeCodeAdapter: run always raises ProviderDisabledError.

Mock outcomes: ok, waiting_approval, rate_limited, quota_exhausted,
auth_required, timed_out, failed. Faults occur only at implement stage;
rate_limit only when attempt==1; approval when not approved.
review_failure occurs at review; test_failure is handled by controller's actual
artifact test. Mock artifact at implement is a deterministic Python module
def add(a: int, b: int) -> int returning a+b (test_failure returns a-b).
Review must inspect the supplied artifact and fixed revision, use distinct
review session identity, return failed for missing revision or incorrect artifact.
Controller owns Git worktrees, commit pinning, tests, queue, and JSON serialization.
Never execute real CLI commands or inspect credential files from adapters.

All task execution enters `dispatch.Dispatcher.run(context)` (Mock for this MVP).
The shared vendor boundary is `Dispatcher.dispatch(provider, context) -> StepResult`.
Codex/Claude routes use their disabled adapter skeletons; DSH/Cursor/Kimi routes
explicitly refuse. Unknown providers raise, and no route silently falls back.

## Serialization / utility inventory

Initial grep: empty repository; none of the six user-mentioned utilities exist here.
Controller will create serialization.dumps(value: object)->str using dataclasses.asdict
at call sites and one shared finite float normalizer. Reuse it; no new JSON wrappers.
Python stdlib hashlib.sha256 for digests, SQLite transactions for atomic persistence.
No statistical/matching/scoring/credential tools are needed for this milestone.

Acceptance: uv run pytest; uv run ruff check .; uv run mypy;
xcodebuild -project ios/Orchestragent.xcodeproj -scheme Orchestragent
-sdk iphonesimulator -configuration Debug -derivedDataPath .build/ios
CODE_SIGNING_ALLOWED=NO build.

## Settings v1 (added user requirement)

GET /settings -> Settings
POST /settings body {"base_revision":integer,"policy":Policy} -> Settings (409 if stale).
POST /settings/proposals body {"instruction":"一句话要求"} -> Proposal.
POST /settings/proposals/{id}/apply body {} -> Settings; exact proposal/version only.
Settings: {revision:integer, mode:"mock", policy:Policy}
Policy: {scheduler_provider:string, scheduler_model:string, instructions:string,
providers:[{id:string, model:string, instructions:string}]}
Provider IDs exactly codex, claude, dsh, cursor, kimi. All entries are preferences only,
not execution/billing permissions. No enabled, token, budget, shell-command fields.
Default scheduler codex / gpt-6.1-sol (official recommendation for repeated complex
agent work, account availability unverified). Default provider models may be blank.
Proposal: {id:string, base_revision:integer, mode:"mock", summary:string, policy:Policy}.
Mock one-sentence setting stores the full sentence as instructions: if exactly one
provider name appears, append to that provider's instructions; otherwise append to
global instructions. It does NOT claim semantic model interpretation; preview says so.
Manual fields allow choosing the desired scheduler provider/model and individual
vendor model/instructions. Unknown providers, extra keys, oversized input rejected.
Task gains policy_revision:integer and policy:object immutable creation snapshot.
Mock context may display policy instructions but does not obey arbitrary text; fixed
workflow remains deterministic. Natural-language semantic translation needs a real
approved scheduler in a later milestone. Permissions remain controller-owned.

Python settings interface (adapters worker now owns settings.py and test_settings.py):
class SettingsStore:
  __init__(self, store:Store)
  read(self)->dict[str,object]
  update(self, base_revision:int, policy:object)->dict[str,object]
  propose(self, instruction:str)->dict[str,object]
  apply(self, proposal_id:str)->dict[str,object]
Use existing SQLite store transactions; configuration and proposal audit in new tables,
not task events. Helper snapshot(db:sqlite3.Connection)->dict[str,object] lets controller
snapshot settings within task submission transaction. All payloads serializable with
shared dumps. SettingsStore initialization creates tables/default policy safely.
