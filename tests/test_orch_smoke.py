"""CLI smoke test: the real ProviderClients, DSH budget ledger and OS sandbox, fake vendors.

Fake `claude` and `dsh` executables stand in for the subscriptions; everything else
(native process supervision, DSH config patching, reservations and settlement,
sandboxed red and green checks, git) is the code that runs on the Mac. Skipped without a sandbox.
"""

import json
import sys
from pathlib import Path

import pytest

from orchestragent.access import Access
from orchestragent.dsh import DshClient
from orchestragent.orch_cli import main
from orchestragent.platform import sandbox_available
from orchestragent.serialization import dumps

pytestmark = pytest.mark.skipif(
    not sandbox_available(),
    reason="needs sandbox-exec or bwrap",
)
TOKENIZER_STUB = (
    "def tokenize(text: str) -> list[str]:\n"
    '    """Lowercase words split on non-alphanumerics."""\n'
    "    raise NotImplementedError\n"
)
STATS_STUB = (
    "from textstats.tokenizer import tokenize\n\n\n"
    "def word_count(text: str) -> int:\n    raise NotImplementedError\n"
)
TOKENIZE_TESTS = (
    "import unittest\n\nfrom textstats.tokenizer import tokenize\n\n\n"
    "class TokenizeTest(unittest.TestCase):\n"
    "    def test_r1_splits_and_lowercases(self) -> None:\n"
    "        self.assertEqual(tokenize('Hello, World!'), ['hello', 'world'])\n"
)
COUNT_TESTS = (
    "import unittest\n\nfrom textstats.stats import word_count\n\n\n"
    "class CountTest(unittest.TestCase):\n"
    "    def test_r2_counts_words(self) -> None:\n"
    "        self.assertEqual(word_count('a b, c'), 3)\n"
)
PLAN = {  # the planner's boundary: stubs only; test designers add the checks
    "summary": "textstats",
    "requirements": [{"id": "R1", "text": "tokenize"}, {"id": "R2", "text": "count words"}],
    "files": [
        {"path": "textstats/__init__.py", "content": "# textstats\n"},
        {"path": "textstats/tokenizer.py", "content": TOKENIZER_STUB},
        {"path": "textstats/stats.py", "content": STATS_STUB},
    ],
    "tasks": [
        {
            "id": "T-001", "goal": "tokenize", "reqs": ["R1"],
            "files": ["textstats/__init__.py", "textstats/tokenizer.py"], "depends_on": [],
            "interface_depends_on": [], "tests": [],
            "contract": "re.split on non-alphanumerics, lowercase, drop empty",
        },
        {
            "id": "T-002", "goal": "count words", "reqs": ["R2"],
            "files": ["textstats/stats.py"], "depends_on": ["T-001"],
            "interface_depends_on": [], "tests": [], "contract": "len(tokenize(text))",
        },
    ],
}
DESIGNS = {
    "T-001": {"files": [{"path": "test_tokenizer.py", "content": TOKENIZE_TESTS}],
              "command": "python3 -m unittest -q test_tokenizer"},
    "T-002": {"files": [{"path": "test_stats.py", "content": COUNT_TESTS}],
              "command": "python3 -m unittest -q test_stats"},
}
BODIES = {
    "T-001": ("textstats/tokenizer.py", "    raise NotImplementedError",
              "    import re\n\n    return [w for w in re.split(r'\\W+', text.lower()) if w]"),
    "T-002": ("textstats/stats.py", "    raise NotImplementedError",
              "    return len(tokenize(text))"),
}
CLAUDE = """
import json, sys
args = sys.argv[1:]
if "status" in args:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                      "apiProvider": "firstParty", "subscriptionType": "max"}))
    sys.exit(0)
prompt = sys.stdin.read()
# the look-around before the boundary: this planner has seen enough already
answer = {"requests": [], "note": ""} if "You are about to plan this build" in prompt else PLAN
print(json.dumps({"type": "system", "session_id": "plan-session"}))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "session_id": "plan-session", "structured_output": answer,
                  "usage": {"input_tokens": 900, "output_tokens": 700,
                            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}))
"""
DSH = """
import json, re, sys
args = sys.argv[1:]
if "--dump-default-config" in args:
    sys.stdout.write(PROFILE)
    sys.exit(0)
text = sys.stdin.read()
design = "## ASSIGNED COMPONENT" in text
task = "root" if design else re.search(r'"task":"(T-[0-9]{3})"', text).group(1)
if design:  # the root component's test designer: every task's test files and command
    answer = {"shards": [{"task": tid, **DESIGNS[tid]} for tid in sorted(DESIGNS)]}
else:
    path, search, replace = BODIES[task]
    answer = {"status": "DONE", "summary": "ok", "reason": "",
              "edits": [{"path": path, "search": search, "replace": replace}]}
for event in (
    {"type": "session", "sessionId": "dsh-" + task},
    {"type": "status", "phase": "turn_start"},
    {"type": "status", "phase": "step_start"},
    {"type": "status", "phase": "step_end",
     "usage": {"inputTokens": 3000, "outputTokens": 200, "cacheReadTokens": 1000,
               "cacheWriteTokens": 0, "totalTokens": 4200}},
    {"type": "status", "phase": "turn_end", "reason": {"kind": "completed"}},
    {"type": "final", "text": json.dumps(answer)},
):
    print(json.dumps(event))
"""


def fake(path: Path, body: str, **constants: object) -> Path:
    header = "".join(
        f"{name} = {json.loads(dumps(value))!r}\n" for name, value in constants.items()
    )
    path.write_text(f"#!{Path(sys.executable).resolve()}\n{header}{body}")
    path.chmod(0o700)
    return path


@pytest.fixture
def vendors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    profile = "\n".join(f"- id: {k}\n  name: '{v}'" for k, v in DshClient.core.items())
    monkeypatch.setenv("ORCHESTRA_CLAUDE_BIN", str(fake(tmp_path / "claude", CLAUDE, PLAN=PLAN)))
    monkeypatch.setenv(
        "ORCHESTRA_DSH_BIN", str(fake(tmp_path / "dsh", DSH, PROFILE=profile, BODIES=BODIES,
                                            DESIGNS=DESIGNS))
    )
    data = tmp_path / "data"
    vault: dict[str, str] = {}
    monkeypatch.setattr(Access, "_persist", staticmethod(lambda: True))
    monkeypatch.setattr(Access, "_keychain_store", staticmethod(vault.__setitem__))
    monkeypatch.setattr(Access, "_keychain_load", lambda self, provider: vault.get(provider, ""))
    monkeypatch.setattr("orchestragent.access.is_macos", lambda: True)
    access = Access(data)
    operation = access.operation("dsh", 10000, "budget")
    access.authorize("dsh", 10000, "budget", str(operation["operation_digest"]), "fake-smoke-key")
    return data


def orch(
    repo: Path, data: Path, capsys: pytest.CaptureFixture[str], *args: str
) -> tuple[int, str]:
    code = main(["--repo", str(repo), "--data-dir", str(data), *args])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_cli_end_to_end_with_fake_vendors(
    orch_repo: Path, vendors: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = tmp_path / "spec.md"
    spec.write_text("Build textstats.")
    doctor = orch(orch_repo, vendors, capsys, "doctor")
    first_ready = orch(orch_repo, vendors, capsys, "doctor", "--allow-unverified-auth")
    planned = orch(orch_repo, vendors, capsys, "plan", str(spec))
    ran = orch(orch_repo, vendors, capsys, "run", "--jobs", "2")
    why = orch(orch_repo, vendors, capsys, "why", "R2")[1]
    trace = orch(orch_repo, vendors, capsys, "trace", "textstats/stats.py:5")[1]
    cost = json.loads(orch(orch_repo, vendors, capsys, "cost", "--by", "model", "--json")[1])
    spent = Access(vendors).status("dsh")
    assert doctor[0] == 1 and "authentication unverified" in doctor[1], doctor[1]
    assert first_ready[0] == 0 and "authentication unverified" in first_ready[1], first_ready[1]
    assert planned[0] == 0, planned[1]
    assert ran[0] == 0 and "merge when satisfied: git merge orch/" in ran[1], ran[1]
    assert "  check T-002: python3 -m unittest -q test_stats" in why
    assert "Orch-Task: T-002" in trace and "Orch-Model: dsh/deepseek-flash" in trace
    dsf = cost["groups"]["dsh/deepseek-flash"]
    # one component test design and two builds
    assert (dsf["calls"], dsf["known_tokens"]["total_tokens"], dsf["cache_hit_rate"]) == (
        3, 12600, 0.25,
    )
    # includes Claude's look-around and boundary calls
    assert cost["totals"]["known_tokens"]["total_tokens"] == 15800
    assert all(call["session_id"] for call in cost["calls"])
    assert (spent["spent_cents"], spent["reserved_cents"]) == (3, 0)
