from dataclasses import dataclass
from pathlib import Path

import pytest

from orchestragent.orch_context import planning_sources, requested_sources
from orchestragent.orch_index import Index
from orchestragent.workspace import Workspace


@dataclass(frozen=True)
class Pinned:
    index: Index
    tree: Path
    base: str


@pytest.fixture
def pinned(orch_repo: Path, tmp_path: Path) -> Pinned:
    """A committed project in two languages; reads are pinned to that commit."""
    sources = {
        "target.py": "def add():\n    return 1\n",
        "web/cart.ts": "export const total = (xs: number[]) => 0;\n",
        "big.txt": "x" * 45000 + "\nTAIL\n",
    }
    for path, source in sources.items():
        target = orch_repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
    Workspace.git(orch_repo, "add", ".")
    Workspace.git(orch_repo, "commit", "-m", "context seed")
    return Pinned(Index(orch_repo, tmp_path / "index.sqlite"), orch_repo,
                  Workspace.git(orch_repo, "rev-parse", "HEAD"))


@pytest.mark.parametrize("selectors", [
    ["../target.py"], ["missing.py"], ["target.py::add"], ["target.py::"], ["TARGET.py"],
    ["./target.py"], ["target.py", "target.py"], [], "target.py", [1],
])
def test_requested_sources_reject_unsafe_unknown_or_malformed_selectors(
    pinned: Pinned, selectors: object,
) -> None:
    with pytest.raises(ValueError):
        requested_sources(pinned.index, pinned.tree, pinned.base, selectors)


def test_requested_sources_are_whole_tracked_files_of_any_language_at_the_revision(
    pinned: Pinned,
) -> None:
    (pinned.tree / "target.py").write_text("CHANGED = True\n")
    assert requested_sources(pinned.index, pinned.tree, pinned.base,
                             ["web/cart.ts", "target.py", "README.md"]) == {
        "README.md": "demo\n",
        "target.py": "def add():\n    return 1\n",
        "web/cart.ts": "export const total = (xs: number[]) => 0;\n",
    }


def test_catalog_bytes_cut_large_files_and_say_they_continue(pinned: Pinned) -> None:
    selected = requested_sources(pinned.index, pinned.tree, pinned.base,
                                 ["big.txt", "target.py"], catalog_bytes=512)
    assert selected["target.py"] == "def add():\n    return 1\n"
    assert len(selected["big.txt"]) < 1000 and "TAIL" not in selected["big.txt"]
    assert "truncated by orch" in selected["big.txt"]


def test_symlinked_request_is_refused(pinned: Pinned, tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET = 1\n")
    (pinned.tree / "alias.py").symlink_to(outside)
    Workspace.git(pinned.tree, "add", "alias.py")
    Workspace.git(pinned.tree, "commit", "-m", "unsafe alias")
    revision = Workspace.git(pinned.tree, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="unsafe"):
        requested_sources(pinned.index, pinned.tree, revision, ["alias.py"])


def test_planning_sources_read_the_pinned_revision_and_skip_new_files(pinned: Pinned) -> None:
    (pinned.tree / "target.py").write_text("CHANGED = True\n")
    assert planning_sources(pinned.index, pinned.base, ["web/cart.ts", "new/file.go",
                                                        "target.py"]) == {
        "target.py": "def add():\n    return 1\n",
        "web/cart.ts": "export const total = (xs: number[]) => 0;\n",
    }
