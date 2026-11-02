"""Persisted models. State transitions belong exclusively to Controller."""

from dataclasses import dataclass, field

STAGES = ("plan", "implement", "review", "test", "summarize")
SCENARIOS = frozenset(
    (
        "happy",
        "approval",
        "rate_limit",
        "quota",
        "auth",
        "timeout",
        "failure",
        "review_failure",
        "test_failure",
    )
)
TERMINAL = frozenset(("completed", "cancelled", "failed"))
RESUMABLE = frozenset(("paused", "rate_limited", "quota_exhausted", "auth_required", "timed_out"))


@dataclass
class Step:
    id: str
    role: str
    depends_on: list[str]
    status: str = "pending"
    summary: str = ""
    revision: str = ""
    project: str = "default"
    reworks: int = 0
    provider: str = "mock"
    model: str = ""
    session_id: str = ""


@dataclass
class Approval:
    id: str
    operation: str
    operation_digest: str
    revision: str
    status: str = "pending"


@dataclass
class Artifact:
    name: str
    content: str


@dataclass
class Task:
    id: str
    request_id: str
    prompt: str
    scenario: str
    created_at: float
    updated_at: float
    status: str = "queued"
    stage: str = "plan"
    provider: str = "mock"
    session_id: str = ""
    attempt: int = 0
    next_run_at: float = 0.0
    result: str = ""
    error: str = ""
    revision: str = ""
    project: str = "default"
    reworks: int = 0
    cost_usd: float | None = 0.0
    steps: list[Step] = field(default_factory=list)
    approval: Approval | None = None
    artifacts: list[Artifact] = field(default_factory=list)
    run_token: str = ""
    lease_until: float = 0.0
    approved: bool = False
    policy_revision: int = 0
    policy: dict[str, object] = field(default_factory=dict)
    mode: str = "mock"
    billing: str = "mock"
    usage: dict[str, object] = field(default_factory=dict)


@dataclass
class Event:
    seq: int
    task_id: str
    kind: str
    at: float
    data: dict[str, object]
