import shutil
import subprocess
from pathlib import Path
from typing import cast

import pytest

from orchestragent import orch_generic, orch_verify
from orchestragent.orch_verify import Check
from orchestragent.platform import sandbox_available

needs_sandbox = pytest.mark.skipif(not sandbox_available(), reason="no sandbox backend")


def test_check_ids_round_trip_and_report_failures() -> None:
    check = Check("cd web && npx vitest run t.test.ts", ("web/t.test.ts",))
    assert orch_verify.parse(check.id) == check and orch_verify.is_check(check.id)
    assert orch_verify.parse("pkg.test_mod.Case.test_r1") is None
    other = Check("go test ./api/...", ("api/x_test.go",))
    assert orch_verify.files_of([check.id, other.id, "a.b.C.test"]) == [
        "api/x_test.go", "web/t.test.ts"]
    report = f"PASS: {check.id}\nFAIL (exit 1): {other.id}\n\n--- go test\nboom"
    assert orch_verify.failed(report, [check.id, other.id]) == [other.id]


def test_sandbox_roots_never_include_home_or_private_dirs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    for name in (".cargo/bin", ".ssh", ".config/gh", "go/pkg", "tools/bin", "tools/lib",
                 ".local/bin", ".local/share/keyrings", "bin"):
        (home / name).mkdir(parents=True)
    roots = orch_verify.toolchain_roots({
        "HOME": str(home),
        "PATH": f"{home}/.cargo/bin:{home}/tools/bin:{home}/.local/bin:{home}/bin:"
                f"{home}/.ssh:/usr/bin",
        "SECRET_HOME": str(home / ".config"),
        "ORCH_SANDBOX_READ": str(home),
    })
    # a toolchain's whole install tree (its lib/ too: CI's Python lives under the home)
    assert home / ".cargo" in roots and home / "tools" in roots and home / "go" in roots
    # a shared prefix mounts only its bin, and ~/bin never mounts the home
    assert home / ".local/bin" in roots and home / ".local" not in roots
    assert home / "bin" in roots and home not in roots
    assert not any(r.is_relative_to(home / ".ssh") or r.is_relative_to(home / ".config")
                   for r in roots)


BOUNDARY: dict[str, object] = {
    "summary": "s",
    "requirements": [{"id": "R1", "text": "a"}, {"id": "R2", "text": "b"}],
    "files": [{"path": "api/calc.go", "content": "package api\n"},
              {"path": "web/src/add.ts", "content": "export {}\n"}],
    "tasks": [
        {"id": "T-001", "goal": "g", "reqs": ["R1"], "files": ["api/calc.go"],
         "depends_on": [], "interface_depends_on": [], "contract": "c", "tests": []},
        {"id": "T-002", "goal": "g", "reqs": ["R2"], "files": ["web/src/add.ts"],
         "depends_on": ["T-001"], "interface_depends_on": [], "contract": "c", "tests": []},
    ],
}


def test_boundary_accepts_any_language_and_checks_structure() -> None:
    assert orch_generic.boundary_errors(BOUNDARY, frozenset()) == []
    tasks = cast(list[dict[str, object]], BOUNDARY["tasks"])
    broken = {**BOUNDARY, "tasks": [{**tasks[0], "reqs": ["R1", "R2"]},
                                    {**tasks[1], "depends_on": ["T-002"]}]}
    errors = "; ".join(orch_generic.boundary_errors(broken, frozenset()))
    assert "R2: accepted by both" in errors and "acyclic" in errors
    escaping = {**BOUNDARY, "files": [{"path": "../x.go", "content": "x"}]}
    assert any("safe relative" in e for e in orch_generic.boundary_errors(escaping, frozenset()))


def test_planner_cases_reach_the_test_designer_only_as_worked_examples() -> None:
    from orchestragent import orch_thin
    tasks = cast(list[dict[str, object]], BOUNDARY["tasks"])
    cased = {**BOUNDARY, "tasks": [{**tasks[0], "cases": ["Add(2, 3) == 5"]}, tasks[1]]}
    assert orch_generic.boundary_errors(cased, frozenset()) == []
    bad = {**BOUNDARY, "tasks": [{**tasks[0], "cases": "Add(2, 3) == 5"}, tasks[1]]}
    assert any("cases must be a list" in e for e in orch_generic.boundary_errors(bad, frozenset()))
    prompt = orch_thin.test_prompt("spec", cased, "", [], task_id="T-001")
    assert "Add(2, 3) == 5" in prompt and orch_thin.CASES_USE in prompt
    assert "`cases`" in orch_thin.boundary_prompt("spec", "", [])


def test_shards_carry_their_command_and_assemble_into_check_ids() -> None:
    files, check = orch_generic.test_shard(BOUNDARY, "T-002", {
        "files": [{"path": "web/test/add.test.ts", "content": "it('R2 adds', () => {})"}],
        "command": "cd web && npx vitest run test/add.test.ts"})
    assert check == Check("cd web && npx vitest run test/add.test.ts", ("web/test/add.test.ts",))
    with pytest.raises(ValueError, match="command"):
        orch_generic.test_shard(BOUNDARY, "T-002", {"files": files})
    with pytest.raises(ValueError, match="new files"):
        orch_generic.test_shard(BOUNDARY, "T-001", {
            "files": [{"path": "api/calc.go", "content": "x"}], "command": "go test ./..."})
    plan = orch_generic.assemble(BOUNDARY, [("T-002", files, check.id)], frozenset())
    assert plan["engine"] == "generic"
    assert [t["tests"] for t in cast(list[dict[str, object]], plan["tasks"])] == [
        [], [check.id]]


@needs_sandbox
def test_red_then_green_with_a_declared_shell_check(tmp_path: Path) -> None:
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "greet.sh").write_text(
        'greet() { echo "not implemented" >&2; return 1; }\n')
    (tmp_path / "test_greet.sh").write_text(
        '. lib/greet.sh\n[ "$(greet Ada)" = "hello Ada" ] || { echo "R1 failed"; exit 1; }\n')
    check = Check("sh test_greet.sh", ("test_greet.sh",))
    assert orch_verify.red(tmp_path, check, 30) == (True, "")
    usable, problem = orch_verify.red(tmp_path, Check("no-such-runner x", ()), 30)
    assert not usable and "could not run" in problem
    (tmp_path / "lib" / "greet.sh").write_text('greet() { echo "hello $1"; }\n')
    passed, report = orch_verify.run(tmp_path, tmp_path, "rev", [check.id], timeout=30)
    assert passed and report.startswith("PASS: ")
    usable, problem = orch_verify.red(tmp_path, check, 30)
    assert not usable and "prove nothing" in problem


@needs_sandbox
@pytest.mark.skipif(not shutil.which("go"), reason="needs go")
def test_polyglot_checks_run_side_by_side(tmp_path: Path) -> None:
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "go.mod").write_text("module example.com/api\n\ngo 1.21\n")
    (tmp_path / "api" / "calc.go").write_text(
        "package api\n\nfunc Add(a, b int) int { return a + b }\n")
    (tmp_path / "api" / "calc_test.go").write_text(
        'package api\n\nimport "testing"\n\nfunc TestR1Add(t *testing.T) {\n'
        '\tif Add(2, 3) != 5 {\n\t\tt.Fatal("sum")\n\t}\n}\n')
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "test_slug.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n"
        "    def test_r2(self): self.assertEqual('a-b', '-'.join('A B'.lower().split()))\n")
    checks = [Check('cd api && GOCACHE="$TMPDIR/go" go test ./...', ("api/calc_test.go",)),
              Check("cd tools && python3 -m unittest -q test_slug", ("tools/test_slug.py",))]
    passed, report = orch_verify.run(tmp_path, tmp_path, "rev", [c.id for c in checks],
                                     timeout=300)
    assert passed, report


def test_ignored_dependency_dirs_are_linked_and_never_staged(tmp_path: Path) -> None:
    repo, tree = tmp_path / "repo", tmp_path / "tree"
    repo.mkdir()
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("node_modules/\n")
    (repo / "web").mkdir()
    (repo / "web" / "index.ts").write_text("export {}\n")
    (repo / "web" / "node_modules" / "dep").mkdir(parents=True)
    (repo / "web" / "node_modules" / "dep" / "index.js").write_text("module.exports = 1\n")
    subprocess.run([*git, "-C", str(repo), "add", "-A"], check=True)
    subprocess.run([*git, "-C", str(repo), "commit", "-qm", "base"], check=True)
    subprocess.run([*git, "-C", str(repo), "worktree", "add", "-q", "--detach", str(tree)],
                   check=True)
    orch_verify.link_ignored(repo, tree)
    assert (tree / "web" / "node_modules").is_symlink()
    (tree / "web" / "index.ts").write_text("export const x = 1\n")
    subprocess.run([*git, "-C", str(tree), "add", "-A", "--", ".",
                    *orch_verify.excludes(tree)], check=True)
    staged = subprocess.run([*git, "-C", str(tree), "diff", "--cached", "--name-only"],
                            capture_output=True, text=True, check=True).stdout.split()
    assert staged == ["web/index.ts"]


@pytest.mark.parametrize("pattern", ["node_modules\n", "node_modules/\n"])
def test_links_in_worktrees_under_git_are_found_and_never_staged(tmp_path: Path,
                                                                 pattern: str) -> None:
    """orch's worktrees live under <repo>/.git/orch/; linked dependency directories there
    must still be mounted and kept out of commits, whatever the ignore pattern's style."""
    tree = tmp_path / "repo" / ".git" / "orch" / "r1" / "work" / "task-1"
    tree.mkdir(parents=True)
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(tree)]
    subprocess.run([*git, "init", "-q"], check=True)
    (tree / ".gitignore").write_text(pattern)
    (tree / "web").mkdir()
    (tree / "web" / "app.ts").write_text("export {}\n")
    (tree / "web" / "node_modules").symlink_to(tmp_path, target_is_directory=True)
    (tree / orch_verify.SCRATCH).mkdir()
    (tree / orch_verify.SCRATCH / "cache").write_text("x")
    assert orch_verify._links(tree) == [tree / "web" / "node_modules"]
    assert tmp_path.resolve() in orch_verify.toolchain(tree).read_only
    subprocess.run([*git, "add", "-A", "--", ".", *orch_verify.excludes(tree)], check=True)
    staged = subprocess.run([*git, "diff", "--cached", "--name-only"], capture_output=True,
                            text=True, check=True).stdout.split()
    assert staged == [".gitignore", "web/app.ts"]
