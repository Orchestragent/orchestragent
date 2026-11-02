"""Validated file bundles and no-network OS-sandboxed execution of generated tests."""

import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from orchestragent.orch_environment import PytestEnvironment
from orchestragent.platform import (
    sandbox_available,
    sandbox_command,
    sandbox_name,
    sandbox_python,
)
from orchestragent.serialization import dumps
from orchestragent.workspace import Workspace

PROJECT_TEST_TIMEOUT = 600.0
PROJECT_CPU_SECONDS = 300


def safe_relative(path: str) -> bool:
    """A relative, normalized, visible path of any file type (any language)."""
    parts = PurePosixPath(path).parts
    return bool(parts) and not (
        path.startswith("/")
        or "\\" in path
        or len(path) > 200
        or str(PurePosixPath(path)) != path
        or any(part.startswith(".") for part in parts)
        or not re.fullmatch(r"[A-Za-z0-9_./@+-]+", path)
    )


class LiveWorkspace(Workspace):
    @staticmethod
    def safe_path(path: str) -> bool:
        """A relative, normalized, visible path with a plain-text project suffix."""
        parts = PurePosixPath(path).parts
        return not (
            not parts
            or path.startswith("/")
            or "\\" in path
            or len(path) > 160
            or str(PurePosixPath(path)) != path
            or any(part.startswith(".") for part in parts)
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", path)
            or PurePosixPath(path).suffix not in {".py", ".md", ".txt", ".json"}
        )

    @staticmethod
    def is_test_file(path: str) -> bool:
        return PurePosixPath(path).name.startswith("test_") and path.endswith(".py")

    @classmethod
    def validate_bundle(
        cls, value: object, max_files: int = 40, max_bytes: int = 512000,
        *, require_tests: bool = True,
    ) -> list[dict[str, str]]:
        if not isinstance(value, list) or not 1 <= len(value) <= max_files:
            raise ValueError(f"Expected 1–{max_files} generated files")
        files: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, dict) or set(item) != {"path", "content"}:
                raise ValueError("Invalid file entry")
            path, content = item["path"], item["content"]
            if not isinstance(path, str) or not isinstance(content, str):
                raise ValueError("Invalid file content")
            if not cls.safe_path(path) or path.casefold() in seen:
                raise ValueError("Unsafe or duplicate generated path")
            if len(content.encode()) > 80000:
                raise ValueError("Generated file too large")
            seen.add(path.casefold())
            files.append({"path": path, "content": content})
        if len(dumps(files).encode()) > max_bytes:
            raise ValueError("Generated bundle too large")
        if require_tests and not any(cls.is_test_file(f["path"]) for f in files):
            raise ValueError("Generated project must contain unittest test_*.py files")
        return files

    def implement_bundle(self, task_id: str, value: object) -> tuple[str, str]:
        files = self.validate_bundle(value)
        repo, _ = self.prepare(task_id)
        attempt = uuid.uuid4().hex
        implementation = repo / "implementations" / attempt
        implementation.parent.mkdir(exist_ok=True)
        self.git(
            repo, "worktree", "add", "-b", f"implementation/{attempt}", str(implementation), "main"
        )
        for file in files:
            target = implementation / file["path"]
            if target.is_symlink() or not target.resolve().is_relative_to(implementation):
                raise ValueError("Generated path escapes worktree")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(file["content"])
        self.git(implementation, "add", "--", *[f["path"] for f in files])
        if self.git(implementation, "diff", "--cached", "--name-only"):
            self.git(implementation, "commit", "-m", "Generated implementation")
        revision = self.git(implementation, "rev-parse", "HEAD")
        return revision, self.git(repo, "diff", "main", revision)

    def bundle_at(self, task_id: str, revision: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise ValueError("A fixed commit is required")
        repo = self.repo(task_id)
        review = repo / "review"
        if review.exists() and self.git(review, "rev-parse", "HEAD") != revision:
            # A rework produced a new revision; the review worktree must follow it.
            self.git(repo, "worktree", "remove", "--force", str(review))
        if not review.exists():
            self.git(repo, "worktree", "add", "--detach", str(review), revision)
        if self.git(review, "rev-parse", "HEAD") != revision:
            raise ValueError("Review commit changed")
        names = self.git(repo, "ls-tree", "-r", "--name-only", revision).splitlines()
        files = [
            {"path": name, "content": self.git(repo, "show", f"{revision}:{name}", strip=False)}
            for name in names
        ]
        return dumps(self.validate_bundle(files))

    @staticmethod
    def sandbox_profile(directory: Path) -> str:
        directory = directory.resolve()
        # Read OS/runtime files but not user homes, mounted volumes or shared temp caches.
        excluded = (
            "/Users",
            "/Volumes",
            "/Network",
            "/private/tmp",
            "/private/var/tmp",
            "/private/var/folders",
        )
        filters = " ".join(f'(require-not (subpath "{p}"))' for p in excluded)
        return (
            "(version 1)(deny default)"
            # The binary the sandbox really executes (a framework launcher's real Python).
            "(allow process-exec (literal "
            f"{dumps(sandbox_python(str(Path(sys.executable).resolve())))}))"
            "(allow process-info*)(allow sysctl-read)"
            f"(allow file-read* (require-all {filters}))"
            f"(allow file-read* file-write* (subpath {dumps(str(directory))}))"
            '(allow file-write-data (literal "/dev/null"))'
        )

    def integrate_bundle(self, task_id: str, revision: str) -> tuple[bool, str]:
        repo = self.repo(task_id)
        files = json.loads(self.bundle_at(task_id, revision))
        self.git(repo, "merge", "--ff-only", revision)
        if self.git(repo, "rev-parse", "HEAD") != revision:
            raise ValueError("Integrated revision differs from reviewed revision")
        # Recreate the pinned bytes separately from mutable implementation/review worktrees.
        execution = repo / "verification" / revision / uuid.uuid4().hex
        execution.mkdir(parents=True)
        for file in files:
            path = execution / file["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(file["content"])
        return self.run_unittests(execution, repo, revision)

    @classmethod
    def run_unittests(
        cls, execution: Path, scratch: Path, revision: str, names: list[str] | None = None,
        *, timeout: float = 25.0, environment: PytestEnvironment | None = None,
    ) -> tuple[bool, str]:
        """Run frozen stdlib unittest ids (or discovery) through the OS sandbox."""
        if names is not None and (
            not names or not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,200}", n) for n in names)
        ):
            raise ValueError("Test names must be dotted identifiers")
        loader = (
            "unittest.TestLoader().discover('.',pattern='test_*.py')"
            if names is None
            else f"unittest.TestLoader().loadTestsFromNames({names!r})"
        )
        setup = cls._environment_setup(execution, scratch, environment) if environment else ""
        script = setup + (
            "import unittest\n"
            "sys.path.insert(0,os.getcwd())\n"
            f"suite={loader}\n"
            "count=suite.countTestCases()\n"
            "result=unittest.TextTestRunner(verbosity=2).run(suite)\n"
            "passed=result.wasSuccessful()\n"
        )
        return cls._run_tests(execution, scratch, revision, script,
                              timeout=timeout, kind="unittest",
                              read_root=environment.root if environment else None,
                              fingerprint=environment.fingerprint if environment else "")

    @classmethod
    def run_pytest(
        cls, execution: Path, scratch: Path, revision: str, names: list[str] | None = None,
        *, environment: PytestEnvironment, timeout: float = PROJECT_TEST_TIMEOUT,
    ) -> tuple[bool, str]:
        """Run pytest with only the explicitly pinned bundle and local conftest files."""
        setup = cls._environment_setup(execution, scratch, environment)
        execution = execution.resolve()
        if names is not None:
            if not names:
                raise ValueError("Pytest nodeids must be nonempty")
            for name in names:
                parts = name.split("::")
                path = execution / parts[0]
                if (len(name) > 512 or name.startswith("-") or not cls.safe_path(parts[0])
                        or not parts[0].endswith(".py") or path.is_symlink()
                        or not path.resolve().is_relative_to(execution)
                        or any(not re.fullmatch(
                            r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\[\]/\\\x00-\x1f]{1,120}\])?", part
                        ) for part in parts[1:])):
                    raise ValueError("Unsafe pytest nodeid")
        temporary = execution / f".orch-pytest-{uuid.uuid4().hex}"
        arguments = ["-q", "-c", "/dev/null", "--rootdir=.", "--confcutdir=.",
                     "-p", "no:cacheprovider", "-o", "addopts=", "-o", "pythonpath=",
                     f"--basetemp={temporary.name}", *(names or ["."])]
        script = setup + (
            "sys.path.insert(0,os.getcwd())\n"
            "class Completion:\n"
            "    count=0\n    completed=0\n    calls=0\n    finished=False\n"
            "    def pytest_runtest_logreport(self,report):\n"
            # Subtest reports share the parent's nodeid and are not collected items.
            # The parent report and pytest's exit code still account for failures.
            "        if getattr(report,'context',None) is not None:\n            return\n"
            "        if report.when=='call':\n"
            "            self.completed+=1\n            self.calls+=int(report.passed)\n"
            "        elif report.when=='setup' and (report.failed or report.skipped):\n"
            "            self.completed+=1\n"
            "    def pytest_sessionfinish(self,session,exitstatus):\n"
            "        self.count=session.testscollected\n        self.finished=True\n"
            "observer=Completion()\n"
            f"code=pytest.main({arguments!r},plugins=[observer])\n"
            "count=observer.count\n"
            "passed=(code==0 and observer.finished and observer.completed==count "
            "and observer.calls>0)\n"
        )
        try:
            return cls._run_tests(execution, scratch, revision, script,
                                  timeout=timeout, kind="pytest",
                                  read_root=environment.root, fingerprint=environment.fingerprint)
        finally:
            if temporary.is_symlink() or temporary.is_file():
                temporary.unlink()
            elif temporary.is_dir():
                shutil.rmtree(temporary)

    @staticmethod
    def _environment_setup(
        execution: Path, scratch: Path, environment: PytestEnvironment,
    ) -> str:
        """The same pinned import environment for node contracts and project regressions."""
        if not sandbox_available():
            raise RuntimeError("Pytest verification requires the OS sandbox "
                               "(sandbox-exec on macOS, bubblewrap on Linux)")
        if PytestEnvironment.load(environment.root) != environment:
            raise ValueError("Pytest environment fingerprint changed")
        execution = execution.resolve()
        if (environment.root.is_relative_to(execution)
                or execution.is_relative_to(environment.root)
                or scratch.resolve().is_relative_to(environment.root)):
            raise ValueError("Pytest environment overlaps writable execution/scratch")
        reserved = {name.casefold() for name in environment.modules}
        roots = [execution / root for root in environment.source_roots]
        if any(not root.is_dir() or root.is_symlink()
               or not root.resolve().is_relative_to(execution) for root in roots):
            raise ValueError("Project source roots are missing or unsafe")
        if any((path.is_dir() and path.name.casefold() in reserved)
               or (path.suffix in {".py", ".pyc", ".so", ".pyd"}
                   and path.name.split(".")[0].casefold() in reserved)
               for root in roots for path in root.iterdir()):
            raise ValueError("Project collides with a reserved pytest runner package")
        return (f"sys.path.insert(0,{str(environment.root)!r})\n"
                "import pytest,_pytest,pluggy,packaging,iniconfig,pygments,py\n"
                f"sys.path[:0]={[str(root) for root in roots]!r}\n")

    @classmethod
    def _run_tests(
        cls, execution: Path, scratch: Path, revision: str, script: str, *,
        timeout: float, kind: str, read_root: Path | None = None, fingerprint: str = "",
    ) -> tuple[bool, str]:
        """One constrained executor; scripts define count/passed before its final marker."""
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Test timeout must be finite and positive")
        cpu = max(1, min(PROJECT_CPU_SECONDS if kind == "pytest" else 15, math.ceil(timeout)))
        completion = f"ORCHESTRAGENT_TEST_COMPLETE_{uuid.uuid4().hex}"
        bootstrap = (
            "import resource,sys,os\n"
            f"resource.setrlimit(resource.RLIMIT_CPU,({cpu},{cpu}))\n"
            "resource.setrlimit(resource.RLIMIT_FSIZE,(1048576,1048576))\n"
            "resource.setrlimit(resource.RLIMIT_NOFILE,(64,64))\n"
            + script
            + f"print({completion!r},count,int(passed),flush=True)\n"
            + "sys.exit(0 if count and passed else 1)\n"
        )
        profile = cls.sandbox_profile(execution)
        if read_root is not None:
            profile += f"(allow file-read* (subpath {dumps(str(read_root))}))"
            # Pytest stats ancestors while finding its collection root. Exact metadata
            # access grants neither directory listing nor ancestor file contents.
            for parent in {*execution.parents, *read_root.parents}:
                profile += f"(allow file-read-metadata (literal {dumps(str(parent))}))"
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(execution), "TMPDIR": str(execution),
                       "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        # A regular file enforces RLIMIT_FSIZE; its inherited descriptor is the only
        # writable access outside the execution tree. Keep the same bounded tail.
        with tempfile.TemporaryFile(dir=scratch) as output:
            try:
                result = subprocess.run(
                    sandbox_command(execution, str(Path(sys.executable).resolve()), bootstrap,
                                    profile, (read_root,) if read_root is not None else ()),
                    cwd=execution, env=environment, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT, timeout=timeout, check=False,
                    start_new_session=True,
                )
            except subprocess.TimeoutExpired:
                code, report = -1, f"测试超过 {timeout:g} 秒，已终止。"
            else:
                code = result.returncode
                size = output.tell()
                output.seek(max(0, size - 16000))
                report = output.read(16000).decode("utf-8", errors="replace")
                if size >= 1048576:
                    code, report = -1, "测试输出超过 1 MiB 限制，已终止。"
        marker = re.search(rf"(?m)^{completion} ([0-9]+) ([01])$", report)
        passed = (code == 0 and marker is not None
                  and int(marker.group(1)) > 0 and marker.group(2) == "1")
        if marker is not None:
            report = report[:marker.start()] + report[marker.end():]
        if code == 0 and not passed:
            report += "\n测试运行器未确认完成至少一个测试；拒绝提前退出。"
        audit = (
            f"issuer=controller\nmode={sandbox_name()}\nrevision={revision}\n"
            f"exit={code}\ncommand=python -I -S -B -c <{kind} bootstrap>\n"
            f"wall_limit_s={timeout:g}\ncpu_limit_s={cpu}\n"
            + (f"environment={fingerprint}\n" if fingerprint else "")
        )
        return passed, (report.strip() or f"Test process exited {code}") + "\n\n" + audit
