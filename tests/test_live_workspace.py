import json
from collections.abc import Callable
from pathlib import Path

import pytest

from orchestragent.live_workspace import PROJECT_CPU_SECONDS, PROJECT_TEST_TIMEOUT, LiveWorkspace
from orchestragent.orch_environment import PytestEnvironment
from orchestragent.platform import is_macos, sandbox_available

pytestmark = pytest.mark.skipif(not sandbox_available(), reason="OS sandbox acceptance")

GOOD_TEST = (
    "import unittest\n"
    "class TestExample(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(2 + 3, 5)\n"
)


# Seatbelt denies outside paths; bwrap never mounts them and binds the pinned bundle
# read-only (EROFS). Network sockets, fork and exec are denied on both (EPERM).
HIDDEN = "PermissionError" if is_macos() else "FileNotFoundError"
READ_ONLY = "PermissionError" if is_macos() else "OSError"
PRIVATE = "/Users" if is_macos() else "/etc/passwd"
SANDBOX_OPERATIONS = [
    ("socket", "socket.create_connection(('127.0.0.1', 9), timeout=1)", "PermissionError"),
    ("socket", "socket.create_connection(('1.1.1.1', 443), timeout=1)", "PermissionError"),
    ("os", f"os.open({PRIVATE!r}, os.O_RDONLY)", HIDDEN),
    ("subprocess", "subprocess.run(['/bin/sh', '-c', 'exit 0'], check=True)", "PermissionError"),
    ("subprocess, sys", "subprocess.run([sys.executable, '-c', 'pass'], check=True)",
     "PermissionError"),
    ("os", "os.fork()", "PermissionError"),
]
Project = Callable[[dict[str, str]], tuple[Path, Path]]


@pytest.fixture
def workspace(tmp_path: Path) -> LiveWorkspace:
    return LiveWorkspace(tmp_path / "workspace")


@pytest.fixture
def bundle() -> list[dict[str, str]]:
    return [{"path": "test_example.py", "content": GOOD_TEST}]


@pytest.mark.parametrize("content", [GOOD_TEST, "\n\n" + GOOD_TEST + "\n\n"])
def test_pinned_bytes_and_standard_unittest_pass(workspace: LiveWorkspace, content: str) -> None:
    task_id = "a" * 32
    revision, _ = workspace.implement_bundle(
        task_id, [{"path": "test_example.py", "content": content}]
    )
    files = json.loads(workspace.bundle_at(task_id, revision))
    pinned = next(file["content"] for file in files if file["path"] == "test_example.py")
    passed, report = workspace.integrate_bundle(task_id, revision)
    assert (pinned, passed, "Ran 1 test" in report) == (content, True, True)


@pytest.mark.parametrize(
    ("imports", "operation", "expected"),
    SANDBOX_OPERATIONS,
)
def test_real_sandbox_blocks_network_home_and_child_processes(
    workspace: LiveWorkspace,
    imports: str,
    operation: str,
    expected: str,
) -> None:
    source = (
        f"import unittest, {imports}\n"
        "class TestSandbox(unittest.TestCase):\n"
        "    def test_restriction(self):\n"
        f"        with self.assertRaises({expected}):\n"
        f"            {operation}\n"
    )
    revision, _ = workspace.implement_bundle(
        "b" * 32, [{"path": "test_sandbox.py", "content": source}]
    )
    passed, report = workspace.integrate_bundle("b" * 32, revision)
    assert passed, report


def test_real_sandbox_keeps_threads_and_local_socketpairs(workspace: LiveWorkspace) -> None:
    source = (
        "import socket, threading, unittest\n"
        "class TestCapability(unittest.TestCase):\n"
        "    def test_thread_and_socketpair(self):\n"
        "        left, right = socket.socketpair()\n"
        "        thread = threading.Thread(target=left.sendall, args=(b'ok',))\n"
        "        thread.start()\n"
        "        thread.join(2)\n"
        "        self.assertEqual(right.recv(2), b'ok')\n"
    )
    revision, _ = workspace.implement_bundle(
        "9" * 32, [{"path": "test_capability.py", "content": source}]
    )
    passed, report = workspace.integrate_bundle("9" * 32, revision)
    assert passed, report


def test_output_flood_is_bounded(workspace: LiveWorkspace) -> None:
    source = "import os\nwhile True:\n    os.write(1, b'x' * 65536)\n"
    revision, _ = workspace.implement_bundle(
        "c" * 32, [{"path": "test_flood.py", "content": source}]
    )
    passed, report = workspace.integrate_bundle("c" * 32, revision)
    assert (passed, "1 MiB" in report, len(report) < 16000) == (False, True, True)


@pytest.mark.parametrize("content", ["", "import os\nos._exit(0)\n", "import sys\nsys.exit(0)\n"])
def test_early_exit_and_no_tests_cannot_claim_success(
    workspace: LiveWorkspace, content: str
) -> None:
    revision, _ = workspace.implement_bundle(
        "d" * 32, [{"path": "test_empty.py", "content": content}]
    )
    passed, _ = workspace.integrate_bundle("d" * 32, revision)
    assert passed is False


@pytest.mark.parametrize("path", ["../test_x.py", "test//x.py", "./test_x.py", "x/./test_x.py"])
def test_path_aliases_and_traversal_rejected(
    workspace: LiveWorkspace,
    bundle: list[dict[str, str]],
    path: str,
) -> None:
    bundle[0]["path"] = path
    with pytest.raises(ValueError):
        workspace.validate_bundle(bundle)


@pytest.mark.parametrize("require_tests", [True, False])
def test_sparse_existing_project_edits_require_explicit_validation_mode(
    require_tests: bool,
) -> None:
    files = [{"path": "module.py", "content": "VALUE = 1\n"}]
    if require_tests:
        with pytest.raises(ValueError, match="test_\\*\\.py"):
            LiveWorkspace.validate_bundle(files)
    else:
        assert LiveWorkspace.validate_bundle(files, require_tests=False) == files


def test_explicit_local_retest_preserves_both_attempts(
    workspace: LiveWorkspace, bundle: list[dict[str, str]]
) -> None:
    revision, _ = workspace.implement_bundle("e" * 32, bundle)
    first = workspace.integrate_bundle("e" * 32, revision)
    second = workspace.integrate_bundle("e" * 32, revision)
    attempts = list((workspace.repo("e" * 32) / "verification" / revision).iterdir())
    assert (first[0], second[0], len(attempts)) == (True, True, 2)


def test_implementation_retry_preserves_old_commit_without_stale_files(
    workspace: LiveWorkspace, bundle: list[dict[str, str]]
) -> None:
    first, _ = workspace.implement_bundle(
        "f" * 32, bundle + [{"path": "old.py", "content": "old = True\n"}]
    )
    second, _ = workspace.implement_bundle("f" * 32, bundle)
    repo = workspace.repo("f" * 32)
    assert (
        "old.py" in workspace.git(repo, "ls-tree", "--name-only", first),
        "old.py" in workspace.git(repo, "ls-tree", "--name-only", second),
    ) == (True, False)


@pytest.fixture
def pytest_environment(tmp_path: Path) -> PytestEnvironment:
    return PytestEnvironment.create(tmp_path / "pytest-environment")


@pytest.fixture
def pytest_project(tmp_path: Path) -> Project:
    execution, scratch = tmp_path / "project", tmp_path / "scratch"
    execution.mkdir()
    scratch.mkdir()

    def write(files: dict[str, str]) -> tuple[Path, Path]:
        for name, source in files.items():
            target = execution / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source)
        return execution, scratch

    return write


def test_real_pytest_supports_fixtures_params_local_imports_and_curated_packages(
    pytest_environment: PytestEnvironment, pytest_project: Project, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only")
    monkeypatch.setenv("PYTEST_PLUGINS", "unavailable_ambient_plugin")
    (tmp_path / "conftest.py").write_text("raise RuntimeError('ancestor conftest loaded')")
    execution, scratch = pytest_project({
        "conftest.py": "import pytest\n@pytest.fixture\ndef offset():\n    return 2\n",
        "app.py": "from packaging.version import Version\ndef value(x):\n    return x + 2\n",
        "pytest.ini": "[pytest]\naddopts = --invalid-project-option\n",
        "test_app.py": (
            "import os,pytest\nfrom app import value\n"
            "@pytest.mark.parametrize('x',[1,2,3])\n"
            "def test_value(x,offset):\n    assert value(x)==x+offset\n"
            "    assert 'PYTEST_ADDOPTS' not in os.environ\n"
        ),
    })
    passed, report = LiveWorkspace.run_pytest(execution, scratch, "a" * 40,
                                             environment=pytest_environment)
    assert passed, report
    assert "3 passed" in report
    assert f"environment={pytest_environment.fingerprint}" in report
    assert f"revision={'a' * 40}" in report


@pytest.mark.parametrize("runner", ["unittest", "pytest"])
def test_contract_and_project_tests_share_dependencies_with_separate_bounded_budgets(
    dependency_environment: PytestEnvironment, pytest_project: Project, runner: str,
) -> None:
    cpu = PROJECT_CPU_SECONDS if runner == "pytest" else 15
    execution, scratch = pytest_project({
        "app.py": "from typing_extensions import assert_type\n"
                  "def value():\n    return assert_type(42, int)\n",
        "test_app.py": "import unittest,resource\nfrom app import value\n"
                       "class TestApp(unittest.TestCase):\n"
                       "    def test_value(self):\n        self.assertEqual(value(),42)\n"
                       "        self.assertEqual(resource.getrlimit(resource.RLIMIT_CPU)[0],"
                       f"{cpu})\n",
    })
    run = LiveWorkspace.run_pytest if runner == "pytest" else LiveWorkspace.run_unittests
    passed, report = run(execution, scratch, "a" * 40, environment=dependency_environment)
    assert passed, report
    assert f"environment={dependency_environment.fingerprint}" in report
    assert f"cpu_limit_s={cpu}" in report
    wall = PROJECT_TEST_TIMEOUT if runner == "pytest" else 25
    assert f"wall_limit_s={wall:g}" in report


@pytest.mark.parametrize(("action", "expected"), [
    ("self.assertTrue(True)", True),
    ("self.assertTrue(False)", False),
    ("self.skipTest('unsupported')", True),
    ("pytest.exit('early', returncode=0)", False),
])
def test_real_pytest_subtest_reports_do_not_count_as_collected_items(
    pytest_environment: PytestEnvironment, pytest_project: Project, action: str, expected: bool,
) -> None:
    execution, scratch = pytest_project({"test_subtests.py": (
        "import unittest,pytest\nclass TestSubtests(unittest.TestCase):\n"
        "    def test_parent(self):\n"
        "        with self.subTest(n=1):\n            self.assertTrue(True)\n"
        f"        with self.subTest(n=2):\n            {action}\n"
    )})
    passed, report = LiveWorkspace.run_pytest(execution, scratch, "a" * 40,
                                             environment=pytest_environment)
    assert passed is expected, report


def test_real_pytest_selects_explicit_parameterized_nodeid(
    pytest_environment: PytestEnvironment, pytest_project: Project,
) -> None:
    execution, scratch = pytest_project({"tests/test_selected.py": (
        "import pytest\n@pytest.mark.parametrize('x',[1,2])\n"
        "def test_value(x):\n    assert x==2\n"
        "def test_unselected():\n    assert False\n"
    )})
    passed, report = LiveWorkspace.run_pytest(
        execution, scratch, "b" * 40, ["tests/test_selected.py::test_value[2]"],
        environment=pytest_environment)
    assert passed, report
    assert "1 passed" in report and "failed" not in report


@pytest.mark.parametrize("source", [
    "", "import os\nos._exit(0)\n", "import sys\nsys.exit(0)\n",
    "import pytest\ndef test_early():\n    pytest.exit('early',returncode=0)\n",
    "def test_failed():\n    assert False\n",
])
def test_real_pytest_no_tests_early_exit_and_failed_assertions_cannot_pass(
    pytest_environment: PytestEnvironment, pytest_project: Project, source: str,
) -> None:
    execution, scratch = pytest_project({"test_failure.py": source})
    passed, report = LiveWorkspace.run_pytest(execution, scratch, "c" * 40,
                                             environment=pytest_environment)
    assert not passed
    assert f"environment={pytest_environment.fingerprint}" in report


def test_real_pytest_wall_timeout_is_bounded_and_audited(
    pytest_environment: PytestEnvironment, pytest_project: Project,
) -> None:
    execution, scratch = pytest_project({
        "test_timeout.py": "import time\ndef test_slow():\n    time.sleep(5)\n",
    })
    passed, report = LiveWorkspace.run_pytest(execution, scratch, "d" * 40,
                                             environment=pytest_environment, timeout=1)
    assert not passed and "超过 1 秒" in report
    assert f"environment={pytest_environment.fingerprint}" in report


@pytest.mark.parametrize(("imports", "operation", "expected"), [
    *SANDBOX_OPERATIONS,
    ("pathlib", "pathlib.Path(BUNDLE_FILE).write_text('changed')", READ_ONLY),
    # Seatbelt grants ancestors metadata only; bwrap ancestors are synthetic and empty.
    ("os", "os.listdir(ANCESTOR_DIR)", "PermissionError") if is_macos() else
    ("os", "assert 'private-parent.txt' not in os.listdir(ANCESTOR_DIR)", None),
    ("pathlib", "pathlib.Path(ANCESTOR_FILE).read_text()", HIDDEN),
])
def test_real_pytest_keeps_os_sandbox_and_environment_read_only(
    pytest_environment: PytestEnvironment, pytest_project: Project, imports: str, operation: str,
    expected: str | None,
) -> None:
    operation = operation.replace("BUNDLE_FILE", repr(str(pytest_environment.root / "py.py")))
    ancestor = pytest_environment.root.parent
    (ancestor / "private-parent.txt").write_text("ancestor-content-must-stay-private")
    operation = operation.replace("ANCESTOR_DIR", repr(str(ancestor))).replace(
        "ANCESTOR_FILE", repr(str(ancestor / "private-parent.txt")))
    check = (f"    {operation}\n" if expected is None
             else f"    with pytest.raises({expected}):\n        {operation}\n")
    execution, scratch = pytest_project({"test_restriction.py": (
        f"import pytest,{imports}\ndef test_restriction():\n{check}"
    )})
    passed, report = LiveWorkspace.run_pytest(execution, scratch, "e" * 40,
                                             environment=pytest_environment)
    assert passed, report
    assert PytestEnvironment.load(pytest_environment.root) == pytest_environment


@pytest.mark.parametrize("name", [
    "../test_app.py", "/tmp/test_app.py", "--help", "-x.py", "test_app.py::--help",
    "test_app.py::test_value[../escape]", "test_app.py::",
])
def test_pytest_nodeids_reject_options_and_path_escape(
    pytest_environment: PytestEnvironment, pytest_project: Project, name: str,
) -> None:
    execution, scratch = pytest_project({"test_app.py": "def test_value():\n    assert True\n"})
    with pytest.raises(ValueError, match="nodeid"):
        LiveWorkspace.run_pytest(execution, scratch, "f" * 40, [name],
                                  environment=pytest_environment)


@pytest.mark.parametrize("name", ["pytest.py", "_pytest", "pluggy.py", "py.py"])
def test_pytest_runner_packages_cannot_be_shadowed_by_project(
    pytest_environment: PytestEnvironment, pytest_project: Project, name: str,
) -> None:
    files = {"test_ok.py": "def test_ok():\n    assert True\n"}
    files[name + "/__init__.py" if "." not in name else name] = "raise RuntimeError('shadow')"
    execution, scratch = pytest_project(files)
    with pytest.raises(ValueError, match="reserved"):
        LiveWorkspace.run_pytest(execution, scratch, "f" * 40, environment=pytest_environment)


def test_pytest_bundle_cannot_overlap_writable_execution(pytest_project: Project) -> None:
    execution, scratch = pytest_project({"test_ok.py": "def test_ok():\n    assert True\n"})
    environment = PytestEnvironment.create(execution / "environment")
    with pytest.raises(ValueError, match="overlap"):
        LiveWorkspace.run_pytest(execution, scratch, "f" * 40, environment=environment)


def test_pytest_rejects_a_previously_loaded_but_mutated_environment(
    pytest_environment: PytestEnvironment, pytest_project: Project,
) -> None:
    execution, scratch = pytest_project({"test_ok.py": "def test_ok():\n    assert True\n"})
    (pytest_environment.root / "py.py").write_text("raise RuntimeError('changed')")
    with pytest.raises(ValueError, match="changed"):
        LiveWorkspace.run_pytest(execution, scratch, "f" * 40, environment=pytest_environment)


def test_pytest_fails_closed_without_os_sandbox(
    pytest_project: Project, monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution, scratch = pytest_project({})
    monkeypatch.setattr("orchestragent.live_workspace.sandbox_available", lambda: False)
    with pytest.raises(RuntimeError, match="OS sandbox"):
        LiveWorkspace.run_pytest(execution, scratch, "f" * 40,
                                  environment=PytestEnvironment(Path("unused"), "unused"))


def test_pytest_nodeid_symlink_cannot_escape_execution(
    pytest_environment: PytestEnvironment, pytest_project: Project, tmp_path: Path,
) -> None:
    execution, scratch = pytest_project({})
    outside = tmp_path / "outside.py"
    outside.write_text("def test_escape():\n    assert True\n")
    (execution / "test_alias.py").symlink_to(outside)
    with pytest.raises(ValueError, match="nodeid"):
        LiveWorkspace.run_pytest(execution, scratch, "f" * 40, ["test_alias.py::test_escape"],
                                  environment=pytest_environment)
