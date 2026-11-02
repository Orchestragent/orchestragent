import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from orchestragent.access import Access
from orchestragent.jev import MODEL, JevClient
from orchestragent.serialization import dumps

KEY = "fake-gateway-key-123"
ANSWER: dict[str, object] = {
    "model": MODEL,
    "answers": {"rung": {"type": "choice", "choice": "thicken",
                         "probabilities": {"retry": 0.1, "thicken": 0.9}}},
    "usage": {"inputTokens": 275, "outputTokens": 20},
    "providerMetadata": {"gateway": {"cost": "0.00001155"}},
}


class Gateway(BaseHTTPRequestHandler):
    reply: tuple[int, dict[str, object]] = (200, ANSWER)
    seen: list[tuple[str, str, dict[str, object]]] = []

    def log_message(self, *args: object) -> None:
        return

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append((self.path, self.headers["Authorization"], body))
        status, payload = type(self).reply
        data = dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def gateway() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Gateway.reply, Gateway.seen = (200, ANSWER), []
    yield f"http://127.0.0.1:{server.server_port}/v1/evaluate"
    server.shutdown()
    server.server_close()


@pytest.fixture
def access(tmp_path: Path) -> Access:
    granted = Access(tmp_path)
    operation = granted.operation("jev", 1000, "budget")
    granted.authorize("jev", 1000, "budget", str(operation["operation_digest"]), KEY)
    return granted


QUESTION: dict[str, object] = {"rung": {"type": "choice", "instructions": "?", "criteria": {}}}


def test_decision_posts_state_with_bearer_key_and_settles_reported_cost(
    gateway: str, access: Access
) -> None:
    result = JevClient(access, gateway).decide({"diff": "x"}, QUESTION, "run-1")
    path, auth, body = Gateway.seen[0]
    assert (result.outcome, result.answers) == ("ok", ANSWER["answers"])
    assert result.usage == {"model": MODEL, "input_tokens": 275, "output_tokens": 20,
                            "cost_usd": 0.00001155, "settled_cents": 1}
    assert (path, auth, body) == (
        "/v1/evaluate", f"Bearer {KEY}", {"model": MODEL, "state": {"diff": "x"},
                                          "questions": QUESTION},
    )
    assert (access.status("jev")["spent_cents"], access.status("jev")["reserved_cents"]) == (1, 0)
    assert KEY not in dumps(result.usage) + result.message


@pytest.mark.parametrize(
    ("status", "expected", "spent"),
    [(401, "auth_required", 0), (402, "quota_exhausted", 0), (429, "rate_limited", 0),
     (500, "failed", 0)],
)
def test_refused_requests_release_their_reservation(
    gateway: str, access: Access, status: int, expected: str, spent: int
) -> None:
    Gateway.reply = (status, {"message": "no", "error_type": "x"})
    result = JevClient(access, gateway).decide("s", QUESTION, "run-2")
    assert (result.outcome, access.status("jev")["spent_cents"]) == (expected, spent)
    assert access.status("jev")["reserved_cents"] == 0


def test_unreachable_gateway_keeps_the_reservation_as_spend(access: Access) -> None:
    result = JevClient(access, "http://127.0.0.1:9/v1/evaluate", timeout=2).decide(
        "s", QUESTION, "run-3"
    )
    assert (result.outcome, access.status("jev")["spent_cents"]) == ("failed", 2)


@pytest.mark.parametrize(
    ("grant", "key", "cap", "expected"),
    [(False, "", 0, "auth_required"), (False, KEY, 0, "waiting_approval"),
     (True, "", 1, "auth_required")],
)
def test_missing_key_budget_or_headroom_never_calls_the_gateway(
    gateway: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    grant: bool, key: str, cap: int, expected: str,
) -> None:
    access = Access(tmp_path)
    if grant:
        operation = access.operation("jev", cap, "budget")
        access.authorize("jev", cap, "budget", str(operation["operation_digest"]), KEY)
    monkeypatch.setattr(access, "key", lambda provider: key)
    result = JevClient(access, gateway).decide("s", QUESTION, "run-4")
    assert (result.outcome, Gateway.seen) == (expected, [])
