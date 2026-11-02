"""The generic engine end to end: a project mixing languages, with model-declared checks.

The models (scripted here) choose the languages, paths and verification commands; orch only
freezes them, checks red on stubs and green on the implementation, and merges.
"""

from pathlib import Path
from typing import cast

import pytest

from orchestragent.native import NativeResult
from orchestragent.orch_run import Orchestrator
from orchestragent.platform import sandbox_available
from orchestragent.workspace import Workspace
from tests.conftest import ScriptedCaller, edit_result, local_runner

pytestmark = pytest.mark.skipif(not sandbox_available(), reason="no sandbox backend")

GREET_STUB = 'greet() { echo "not implemented" >&2; return 1; }\n'
SLUG_STUB = "def slug(text):\n    raise NotImplementedError\n"
BOUNDARY: dict[str, object] = {
    "summary": "a shell greeter and a Python slug helper",
    "requirements": [{"id": "R1", "text": "greet NAME prints 'hello NAME'"},
                     {"id": "R2", "text": "slug(text) lower-cases and joins words with -"}],
    "files": [{"path": "lib/greet.sh", "content": GREET_STUB},
              {"path": "tools/slug.py", "content": SLUG_STUB}],
    "tasks": [
        {"id": "T-001", "goal": "greeter", "reqs": ["R1"], "files": ["lib/greet.sh"],
         "depends_on": [], "interface_depends_on": [], "contract": "greet NAME -> hello NAME",
         "tests": []},
        {"id": "T-002", "goal": "slug", "reqs": ["R2"], "files": ["tools/slug.py"],
         "depends_on": [], "interface_depends_on": [], "contract": "slug(str) -> str",
         "tests": []},
    ],
}
GREET_TEST = ('. lib/greet.sh\n[ "$(greet Ada)" = "hello Ada" ] '
              '|| { echo "R1 greet Ada"; exit 1; }\n')
SLUG_TEST = ("import unittest\nfrom slug import slug\n\n"
             "class SlugTest(unittest.TestCase):\n"
             "    def test_r2_slug(self):\n"
             "        self.assertEqual(slug('Hello Big World'), 'hello-big-world')\n")


def design(path: str, content: str, command: str) -> NativeResult:
    return NativeResult("ok", {"files": [{"path": path, "content": content}],
                               "command": command})


@pytest.fixture
def polyglot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ORCH_ENGINE", "generic")
    repo = tmp_path / "project"
    repo.mkdir()
    Workspace.git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("polyglot demo\n")
    Workspace.git(repo, "add", "README.md")
    Workspace.git(repo, "commit", "-m", "seed")
    return repo


def test_mixed_language_plan_freezes_declared_checks_builds_and_merges(polyglot: Path) -> None:
    caller = ScriptedCaller({
        "plan": [NativeResult("ok", BOUNDARY)],
        "dsh:T-001": [
            # a vacuous test (passes on the stub) is sent back; then a real one
            design("tests/greet_test.sh", "true\n", "sh tests/greet_test.sh"),
            design("tests/greet_test.sh", GREET_TEST, "sh tests/greet_test.sh"),
            edit_result("lib/greet.sh", 'greet() { echo "hello $1"; }\n', search=""),
        ],
        "dsh:T-002": [
            design("tools/test_slug.py", SLUG_TEST,
                   "cd tools && python3 -m unittest -q test_slug"),
            edit_result("tools/slug.py", "return '-'.join(text.lower().split())"),
        ],
    })
    orch = Orchestrator(polyglot, caller, local_runner, notify=lambda _: None)
    assert orch.generic
    planned = orch.plan("A shell greeter and a Python slug helper", distributed=True,
                        test_jobs=1)
    tasks = {t["id"]: t for t in cast(list[dict[str, list[str]]], planned.plan["tasks"])}
    assert all(test.startswith("cmd:") for t in tasks.values() for test in t["tests"])
    prompts = [text for _, key, text in caller.calls if key == "dsh:T-001"]
    assert "prove nothing" in prompts[1]  # the vacuous shard was rejected by the red gate
    run = orch.execute(planned.id)
    assert run.status == "done", orch.gate_failure
    branch = f"orch/{run.id}"
    assert "hello" in Workspace.git(polyglot, "show", f"{branch}:lib/greet.sh")
    assert "join" in Workspace.git(polyglot, "show", f"{branch}:tools/slug.py")
    assert Workspace.git(polyglot, "show", f"{branch}:tests/greet_test.sh") == GREET_TEST.strip()
    listed = Workspace.git(polyglot, "ls-tree", "-r", "--name-only", branch).split()
    assert not any(".orch-tmp" in path for path in listed)
