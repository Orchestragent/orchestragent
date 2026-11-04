"""The repository as an agent reads it: listing, find, search, read — at one revision."""

from pathlib import Path

import pytest

from orchestragent.orch_repo import MAX_REQUESTS, Repo, mentioned_paths
from orchestragent.workspace import Workspace


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    (path / "api" / "ledger").mkdir(parents=True)
    (path / "web" / "src").mkdir(parents=True)
    (path / "api" / "ledger" / "parse.go").write_text(
        "package ledger\n\n// ParseCSV reads rows.\nfunc ParseCSV() {}\n\nfunc helper() {}\n")
    (path / "web" / "src" / "ledger.ts").write_text(
        "export function parseExport(json: string) {\n  return JSON.parse(json);\n}\n")
    (path / "README.md").write_text("# demo\nSee api/ledger/parse.go and web/src/ledger.ts\n")
    Workspace.git(path, "init", "-b", "main")
    Workspace.git(path, "add", "-A")
    Workspace.git(path, "commit", "-m", "seed")
    (path / "web" / "src" / "ledger.ts").write_text("// changed after HEAD\n")
    return path


def test_listing_find_search_and_read_are_pinned_to_the_revision(repo: Path) -> None:
    view = Repo(repo)
    listing = view.listing("HEAD")
    assert listing.startswith("3 tracked files") and "api/ledger/" in listing
    assert "parse.go" in listing and "ledger.ts" in listing
    assert view.find("HEAD", "*.go") == ["api/ledger/parse.go"]
    assert view.find("HEAD", "web/**") == ["web/src/ledger.ts"]
    assert view.find("HEAD", "ledger") == ["api/ledger/parse.go", "web/src/ledger.ts"]
    hits = view.search("HEAD", "parse")
    assert "api/ledger/parse.go:4" in hits and "web/src/ledger.ts:1" in hits
    assert ">" in hits and "// ParseCSV reads rows." in hits  # the matching line and context
    assert "(no matches" in view.search("HEAD", "nothing-here-at-all")
    whole = view.read_request("HEAD", "web/src/ledger.ts")
    assert "JSON.parse" in whole and "changed after HEAD" not in whole  # the committed tree
    span = view.read_request("HEAD", "api/ledger/parse.go:4-6")
    assert span.splitlines()[1].strip().startswith("4") and "helper" in span
    assert "no tracked file" in view.read_request("HEAD", "nope.txt")
    assert "line range is start-end" in view.read_request("HEAD", "README.md:x")


def test_a_round_of_requests_is_answered_in_order_and_malformed_ones_are_explained(
        repo: Path) -> None:
    view = Repo(repo)
    text = view.answer("HEAD", [{"find": "*.ts"}, {"search": "ParseCSV"},
                                {"read": "README.md:1-1"}])
    assert text.index("$ find *.ts") < text.index("$ search ParseCSV") < text.index("$ read")
    assert "web/src/ledger.ts" in text and "parse.go:4" in text and "# demo" in text
    for bad in ([], [{"grep": "x"}], [{"find": ""}], [{"find": "a", "read": "b"}],
                [{"read": "x"}] * (MAX_REQUESTS + 1)):
        with pytest.raises(ValueError):
            view.answer("HEAD", bad)


def test_paths_a_spec_names_are_found_in_the_tree(repo: Path) -> None:
    tree = Repo(repo).tree("HEAD")
    assert mentioned_paths("Change api/ledger/parse.go; add web/src/ledger.ts; see x/y.go",
                           tree) == ["api/ledger/parse.go", "web/src/ledger.ts"]
    assert mentioned_paths("Mentions parse.go by name only", tree) == ["api/ledger/parse.go"]
    assert mentioned_paths("nothing", tree) == []


def test_seeds_keep_build_products_but_never_live_process_state(tmp_path: Path) -> None:
    """tsx names its IPC socket after the pid; pids repeat across sandbox runs."""
    import socket

    from orchestragent import orch_verify
    work = tmp_path / "work"
    tmp = work / orch_verify.SCRATCH / "tmp"
    (tmp / "tsx-1000").mkdir(parents=True)
    (tmp / "cache.bin").write_bytes(b"x" * 10)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(tmp / "tsx-1000" / "164.pipe"))
    try:
        seed = tmp_path / "seeds" / "abc"
        orch_verify.keep_seed(work, seed)
    finally:
        listener.close()
    assert (seed / "cache.bin").is_file()
    assert (seed / "tsx-1000").is_dir() and not (seed / "tsx-1000" / "164.pipe").exists()
