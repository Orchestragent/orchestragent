import importlib.util
from pathlib import Path

import pytest

from orchestragent.orch_usage import Call, tokens

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "report_overnight.py"
SPEC = importlib.util.spec_from_file_location("report_overnight", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)
USAGE: dict[str, object] = {"input_tokens": 2, "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 700, "output_tokens": 5}


@pytest.mark.parametrize(("patch", "review_start", "counted"), [
    ((0.0, 301.0), 301.5, False),  # a long request just finished: its cache aged meanwhile
    ((0.0, 20.0), 30.0, True),
    ((0.0, 50.0), 40.0, False),  # overlapping requests cannot read each other's writes
])
def test_followups_age_from_the_earlier_request_start(
    patch: tuple[float, float], review_start: float, counted: bool,
) -> None:
    calls = [Call("p", "r", "claude", "claude-opus-5-5", "patch", patch[0], task_id="T-001",
                  finished_at=patch[1], outcome="ok", usage=USAGE),
             Call("v", "r", "claude", "claude-opus-5-5", "review", review_start,
                  task_id="T-001", finished_at=review_start + 10, outcome="ok", usage=USAGE)]
    result = REPORT.cross_role_followups(calls)
    expected = [{"task": "T-001", "from": "patch", "to": "review", "call_id": "v",
                 "gap_s": review_start - patch[1], "request_age_s": review_start - patch[0],
                 "later_cache_write_tokens": 700}] if counted else []
    assert result["pairs"] == expected and result["ttl_clock"] == "earlier request start"
    assert result["later_cache_write_tokens"] == (
        tokens("claude", USAGE).cache_write_tokens if counted else 0)
