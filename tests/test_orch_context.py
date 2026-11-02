from collections.abc import Callable
from pathlib import Path

import pytest

from orchestragent.dsh import DshClient
from orchestragent.orch_baseline import SCOPED_SCHEMA
from orchestragent.orch_context import ScopedContext, owned_sources, requested_sources
from orchestragent.orch_index import Index, RepoIndex
from orchestragent.workspace import Workspace
from tests.conftest import MATH_TESTS


@pytest.mark.parametrize(("mode", "partial"), [
    ("default", True), ("full-read", False), ("more-symbols", False), ("dynamic", False),
    ("unresolved", False), ("small", False),
])
def test_owned_context_slices_known_targets_and_falls_back_when_uncertain(
    large_owned_source: str, mode: str, partial: bool,
) -> None:
    source = large_owned_source
    if mode == "dynamic":
        source += "def reflective():\n    return globals()\n"
    elif mode == "small":
        source = source.replace("context ballast " * 8000, "small")
    selectors = (["adder.py"] if mode == "full-read" else ["adder.py::unrelated"]
                 if mode == "more-symbols" else [])
    index = RepoIndex.from_sources({"adder.py": source, "test_mathops.py": MATH_TESTS})
    tests = [] if mode == "unresolved" else ["test_mathops.AddTest.test_r1_adds"]
    view = owned_sources(index, {"adder.py": source}, tests, selectors)["adder.py"]
    assert (view != source, "def helper" in view, "def unrelated" in view) == (
        partial, True, not partial)
    assert len(view.encode()) <= len(source.encode())


@pytest.fixture
def context_factory(orch_repo: Path, tmp_path: Path) -> Callable[..., ScopedContext]:
    def make(
        sources: dict[str, str], spec: str = "Fix target.add", limit: int = 4000,
    ) -> ScopedContext:
        for path, source in sources.items():
            target = orch_repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source)
        Workspace.git(orch_repo, "add", ".")
        Workspace.git(orch_repo, "commit", "-m", "context seed")
        revision = Workspace.git(orch_repo, "rev-parse", "HEAD")
        return ScopedContext(Index(orch_repo, tmp_path / "index.sqlite"), orch_repo,
                             revision, spec, limit)
    return make


def test_explicit_symbol_selects_imports_and_tests_beyond_irrelevant_prefix(
    context_factory: Callable[..., ScopedContext],
) -> None:
    context = context_factory({
        "aaa.py": "# irrelevant flood\n" * 4000,
        "target.py": "from helper import add\n",
        "helper.py": "def add(a, b):\n    return a - b\n",
        "test_target.py": "from target import add\n# relevant seed test\n",
    }, "Fix target.py addition")
    prompt = context.prompt(context.base, "", SCOPED_SCHEMA)
    assert set(context.visible) == {"target.py", "helper.py", "test_target.py"}
    assert "irrelevant flood" not in prompt and "relevant seed test" in prompt
    assert len(DshClient.task_text(prompt, SCOPED_SCHEMA).encode()) <= context.limit


@pytest.mark.parametrize("spec", ["Fix target.add", "Fix target.py"])
def test_multibyte_whole_files_skip_large_then_include_small(
    context_factory: Callable[..., ScopedContext], spec: str,
) -> None:
    context = context_factory({
        "target.py": "from small import VALUE\ndef add():\n    return VALUE\n" + "# 中文\n" * 600,
        "small.py": "VALUE = '完整中文'\n",
    }, spec, 2048)
    prompt = context.prompt(context.base, "错" * 2000, SCOPED_SCHEMA)
    assert context.visible == {"small.py": "VALUE = '完整中文'\n"}
    assert len(DshClient.task_text(prompt, SCOPED_SCHEMA).encode()) <= 2048
    context.request(["target.py"], context.base)
    with pytest.raises(ValueError, match="Requested complete files exceed"):
        context.prompt(context.base, "", SCOPED_SCHEMA)


@pytest.mark.parametrize("paths", [
    ["../target.py"], ["unknown.py"], ["README.md"], ["target.py", "target.py"],
    ["./target.py"], ["TARGET.py"], [], "target.py",
])
def test_file_requests_require_exact_safe_tracked_python_paths(
    context_factory: Callable[..., ScopedContext], paths: object,
) -> None:
    context = context_factory({"target.py": "VALUE = 1\n"})
    with pytest.raises(ValueError, match="need_files"):
        context.request(paths, context.base)


def test_on_demand_context_replaces_old_request_and_does_not_accumulate_feedback(
    context_factory: Callable[..., ScopedContext],
) -> None:
    context = context_factory({"one.py": "ONE = 1\n", "two.py": "TWO = 2\n"}, "Make a feature")
    context.request(["one.py"], context.base)
    context.prompt(context.base, "old failure", SCOPED_SCHEMA)
    context.request(["two.py"], context.base)
    prompt = context.prompt(context.base, "new failure", SCOPED_SCHEMA)
    assert context.visible == {"two.py": "TWO = 2\n"}
    assert "old failure" not in prompt and "new failure" in prompt


@pytest.mark.parametrize("path", [
    "test_target.py", "tests/helper.py", "tests/__init__.py", "__init__.py",
    "conftest.py", "new/conftest.py", "TARGET.py", "unseen.py", "README.md",
])
def test_frozen_tests_package_markers_and_unseen_existing_files_reject_edits(
    context_factory: Callable[..., ScopedContext], path: str,
) -> None:
    context = context_factory({
        "target.py": "VALUE = 1\n", "unseen.py": "VALUE = 2\n",
        "test_target.py": "import target\n", "tests/helper.py": "VALUE = 3\n",
    }, "Fix target.py")
    context.prompt(context.base, "", SCOPED_SCHEMA)
    with pytest.raises(ValueError, match="frozen|Request complete current"):
        context.validate_edits([{"path": path, "content": ""}], context.base)


def test_new_sources_and_visible_existing_sources_allow_edits(
    context_factory: Callable[..., ScopedContext],
) -> None:
    context = context_factory({"target.py": "VALUE = 1\n"}, "Fix target.py")
    context.prompt(context.base, "", SCOPED_SCHEMA)
    context.validate_edits([
        {"path": "target.py", "content": "VALUE = 2\n"},
        {"path": "new.py", "content": "VALUE = 3\n"},
    ], context.base)
    assert set(context.visible) == {"target.py"}


def test_symlink_requests_and_oversized_spec_fail_without_partial_source(
    context_factory: Callable[..., ScopedContext], tmp_path: Path,
) -> None:
    context = context_factory({"target.py": "VALUE = 1\n"}, "文" * 4000, 2048)
    with pytest.raises(ValueError, match="Specification, schema"):
        context.prompt(context.base, "", SCOPED_SCHEMA)
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET = 1\n")
    (context.tree / "alias.py").symlink_to(outside)
    Workspace.git(context.tree, "add", "alias.py")
    Workspace.git(context.tree, "commit", "-m", "unsafe alias")
    revision = Workspace.git(context.tree, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="safe existing"):
        context.request(["alias.py"], revision)
    assert context.visible == {}



def test_shared_package_imports_do_not_select_unrelated_tests(
    context_factory: Callable[..., ScopedContext],
) -> None:
    context = context_factory({
        "pkg/__init__.py": "from .common import VALUE\n",
        "pkg/common.py": "VALUE = 1\n",
        "pkg/target.py": "def add():\n    return 1\n",
        "pkg/other.py": "def other():\n    return 2\n",
        "test_target.py": "from pkg.target import add\n# relevant suite\n",
        "test_other.py": "from pkg.other import other\n# unrelated suite\n",
    }, "Fix pkg.target.add", 6000)
    prompt = context.prompt(context.base, "", SCOPED_SCHEMA)
    assert "test_target.py" in context.visible and "test_other.py" not in context.visible
    assert "relevant suite" in prompt and "unrelated suite" not in prompt


@pytest.mark.parametrize(("path", "markers", "missing"), [
    ("test_new.py", [], None),
    ("tests/test_new.py", [], "tests/__init__.py"),
    ("tests/test_new.py", ["tests/__init__.py"], None),
    ("tests/unit/test_new.py", ["tests/unit/__init__.py"], "tests/__init__.py"),
    ("tests/unit/test_new.py", ["tests/__init__.py", "tests/unit/__init__.py"], None),
])
def test_new_tests_require_discoverable_packages(
    context_factory: Callable[..., ScopedContext], path: str,
    markers: list[str], missing: str | None,
) -> None:
    context = context_factory({"target.py": "VALUE = 1\n"})
    files = [{"path": name, "content": ""} for name in [path, *markers]]
    if missing:
        with pytest.raises(ValueError, match=missing):
            context.validate_edits(files, context.base)
    else:
        context.validate_edits(files, context.base)


def test_existing_package_marker_satisfies_test_discovery(
    context_factory: Callable[..., ScopedContext],
) -> None:
    context = context_factory({"tests/__init__.py": ""})
    context.validate_edits([{"path": "tests/test_new.py", "content": ""}], context.base)


@pytest.mark.parametrize("selector", ["../target.py", "missing.py", "target.py::missing",
                                     "target.py::", "README.md", "TARGET.py"])
def test_requested_sources_reject_unsafe_or_unknown_selectors(
    context_factory: Callable[..., ScopedContext], selector: str,
) -> None:
    context = context_factory({"target.py": "def add():\n    return 1\n"})
    with pytest.raises(ValueError):
        requested_sources(context.index, context.tree, context.base, [selector])


def test_requested_symbols_are_pinned_and_can_expand_to_full_files(
    context_factory: Callable[..., ScopedContext],
) -> None:
    original = "def add():\n    return 1\n\ndef other():\n    return 2\n"
    context = context_factory({"target.py": original})
    (context.tree / "target.py").write_text("CHANGED = True\n")
    selected = requested_sources(context.index, context.tree, context.base, ["target.py::add"])
    assert ("return 1" in selected["target.py::add"]
            and "def other" not in selected["target.py::add"])
    assert requested_sources(context.index, context.tree, context.base, ["target.py"]) == {
        "target.py": original,
    }


def test_catalog_and_exact_definition_do_not_claim_complete_dynamic_module(
    context_factory: Callable[..., ScopedContext],
) -> None:
    original = ("import inspect\nSTATE = '" + "x" * 45000 + "'\n"
                "@decorator\ndef selected():\n    return 7\n"
                "def unrelated():\n    return 8\n")
    context = context_factory({"target.py": original})
    selected = requested_sources(context.index, context.tree, context.base,
                                 ["target.py", "target.py::selected"], catalog_bytes=512)
    assert all("PARTIAL CONTEXT" in text for text in selected.values())
    assert "@decorator\ndef selected():\n    return 7" in selected["target.py::selected"]
    assert "def unrelated" not in selected["target.py::selected"]
    assert all("x" * 100 not in text for text in selected.values())


@pytest.mark.parametrize("catalog_bytes", [0, 512])
def test_parent_read_covers_methods_without_duplicate_payloads_or_order_changes(
    context_factory: Callable[..., ScopedContext], catalog_bytes: int,
) -> None:
    context = context_factory({
        "target.py": "class Parent:\n    def work(self):\n        return 'exact body'\n",
    })
    selectors = ["target.py::Parent.work", "target.py::Parent"]
    selected = requested_sources(context.index, context.tree, context.base, selectors,
                                 catalog_bytes=catalog_bytes)
    assert list(selected) == ["target.py::Parent"]
    assert "return 'exact body'" in selected["target.py::Parent"]
    assert selected == requested_sources(context.index, context.tree, context.base,
                                         selectors[::-1], catalog_bytes=catalog_bytes)
    with pytest.raises(ValueError, match="Unknown symbol"):
        requested_sources(context.index, context.tree, context.base,
                          ["target.py::Parent", "target.py::Parent.missing"],
                          catalog_bytes=catalog_bytes)
