"""Jev (TypeSafe AI) decision model through Vercel AI Gateway; API-billed under a CNY cap.

Jev answers typed questions about a state with probabilities instead of free text.
The key lives only in Access (keychain on macOS); this client never logs it, never
retries, and never falls back to another model. Each call reserves from the local
budget first and settles at the cost the gateway reports.
"""

import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from orchestragent.access import Access
from orchestragent.serialization import dumps

ENDPOINT = "https://ai-gateway.vercel.sh/v1/evaluate"
MODEL = "typesafe-ai/jev"
RESERVATION = 2  # fen; a 24K-character state costs well under 1 fen at list price
STATE_LIMIT = 24_000  # bytes of serialized state, far inside the 32K-token context
FEN_PER_USD = 750  # 7.5 CNY/USD, rounded up per call like the DSH ledger


@dataclass(frozen=True)
class Decision:
    outcome: str  # ok | auth_required | waiting_approval | quota_exhausted | rate_limited | failed
    answers: dict[str, object] = field(default_factory=dict)
    usage: dict[str, object] = field(default_factory=dict)
    message: str = ""


class JevClient:
    def __init__(self, access: Access, endpoint: str = ENDPOINT, timeout: float = 30) -> None:
        self.access = access
        self.endpoint = endpoint
        self.timeout = timeout

    def status(self) -> dict[str, object]:
        budget = self.access.status("jev")
        ready = budget["credential_ready"] is True and budget["authorized"] is True
        return {**budget, "ready": ready, "auth": "api_explicit_budget", "model": MODEL}

    def decide(self, state: object, questions: dict[str, object], run_id: str) -> Decision:
        if not self.access.key("jev"):
            return Decision("auth_required", message="Jev key missing: scripts/login.command")
        if self.access.status("jev")["authorized"] is not True:
            return Decision("waiting_approval", message="Jev budget not authorized")
        body = dumps({"model": MODEL, "state": state, "questions": questions}).encode()
        if len(body) > STATE_LIMIT + 4000:
            return Decision("failed", message="Jev request too large")
        if not self.access.reserve("jev", run_id, RESERVATION):
            return Decision("quota_exhausted", message="Jev budget exhausted")
        request = urllib.request.Request(
            self.endpoint,
            body,
            headers={
                "Authorization": f"Bearer {self.access.key('jev')}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read(1_000_000))
        except urllib.error.HTTPError as error:
            # A refused request was not served, so nothing was spent.
            self.access.release(run_id)
            outcome = {401: "auth_required", 403: "auth_required", 402: "quota_exhausted",
                       429: "rate_limited"}.get(error.code, "failed")
            return Decision(outcome, message=f"Jev HTTP {error.code}")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            # The request may have been served: keep the reservation as spend.
            self.access.settle(run_id, "jev", RESERVATION)
            return Decision("failed", message=f"Jev unreachable: {type(error).__name__}")
        answers = payload.get("answers") if isinstance(payload, dict) else None
        if not isinstance(answers, dict):
            self.access.settle(run_id, "jev", RESERVATION)
            return Decision("failed", message="Jev returned no answers")
        usage, cents = self._usage(payload)
        self.access.settle(run_id, "jev", cents)
        return Decision("ok", answers, {**usage, "settled_cents": cents})

    @staticmethod
    def _usage(payload: dict[str, object]) -> tuple[dict[str, object], int]:
        counts = payload.get("usage")
        usage: dict[str, object] = {"model": str(payload.get("model", ""))[:80]}
        if isinstance(counts, dict):
            for source, target in (("inputTokens", "input_tokens"),
                                   ("outputTokens", "output_tokens")):
                value = counts.get(source)
                if type(value) is int and value >= 0:
                    usage[target] = value
        metadata = payload.get("providerMetadata")
        gateway = metadata.get("gateway") if isinstance(metadata, dict) else None
        try:
            cost = float(gateway.get("cost", "")) if isinstance(gateway, dict) else math.nan
        except (TypeError, ValueError):
            cost = math.nan
        if not math.isfinite(cost) or cost < 0:
            return usage, RESERVATION
        usage["cost_usd"] = cost
        return usage, max(1, math.ceil(cost * FEN_PER_USD))
