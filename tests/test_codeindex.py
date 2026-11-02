from pathlib import Path

import pytest

from orchestragent.codeindex import CodeIndex


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "engine").mkdir(parents=True)
    (root / "engine" / "metrics.py").write_text(
        "def sharpe(returns, rf=0.0):\n    return (returns - rf) / 2\n"
    )
    (root / "engine" / "backtest.py").write_text(
        "from engine.metrics import sharpe\n\n\ndef run(data):\n    return sharpe(data)\n"
    )
    (root / "README.md").write_text("# engine\n")
    (root / ".git").mkdir()
    (root / ".git" / "ignored.py").write_text("def hidden():\n    pass\n")
    return root


def test_index_finds_a_symbol_and_skips_repositories(tmp_path: Path, project: Path) -> None:
    root = project
    index = CodeIndex.ensure(tmp_path / "index.sqlite")
    stats = index.build("p1", root)
    assert stats["total"] == 3
    hits = index.search("p1", "sharpe")
    assert [hit.path for hit in hits][0] == "engine/metrics.py"
    assert all(".git" not in hit.path for hit in hits)


def test_context_returns_real_bytes_and_skips_stale_files(tmp_path: Path, project: Path) -> None:
    root = project
    index = CodeIndex.ensure(tmp_path / "index.sqlite")
    index.build("p1", root)
    text = index.context("p1", root, "sharpe")
    assert "def sharpe" in text and "--- engine/metrics.py ---" in text
    (root / "engine" / "metrics.py").write_text("def sharpe(x):\n    return 0\n")
    assert "def sharpe" not in index.context("p1", root, "sharpe")


def test_build_is_incremental_and_drops_deleted_files(tmp_path: Path, project: Path) -> None:
    root = project
    index = CodeIndex.ensure(tmp_path / "index.sqlite")
    index.build("p1", root)
    assert index.build("p1", root)["indexed"] == 0
    (root / "README.md").unlink()
    index.build("p1", root)
    assert index.search("p1", "README") == []


@pytest.fixture
def audit_project(tmp_path: Path, project: Path) -> tuple[Path, CodeIndex]:
    root = project
    (root / "contracts").mkdir()
    (root / "contracts" / "schema.py").write_text("SCHEMA = {'required': ['id']}\n")
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text("name: CI\n")
    for number in range(30):
        (root / "engine" / f"module{number}.py").write_text("# engine\n" * 100)
    index = CodeIndex.ensure(tmp_path / "index.sqlite")
    index.build("p1", root)
    return root, index


@pytest.mark.parametrize("path", ["engine/metrics.py", "contracts/schema.py",
                                  ".github/workflows/ci.yml"])
def test_wide_audit_includes_unmatched_repository_areas(
    audit_project: tuple[Path, CodeIndex], path: str,
) -> None:
    root, index = audit_project
    text = index.context("p1", root, "审计整个项目", 200_000)
    assert f"--- {path} ---" in text


def test_prefix_is_applied_before_ranking_limit(audit_project: tuple[Path, CodeIndex]) -> None:
    root, index = audit_project
    text = index.context("p1", root, "engine", 8192, ["contracts/"])
    assert "--- contracts/schema.py ---" in text
    assert "--- engine/" not in text


def test_small_budget_includes_utf8_excerpt_and_marks_truncation(
    tmp_path: Path, project: Path,
) -> None:
    root = project
    (root / "engine" / "metrics.py").write_text("# 测量\n" * 3000)
    index = CodeIndex.ensure(tmp_path / "index.sqlite")
    index.build("p1", root)
    text = index.context("p1", root, "metrics", 8192)
    assert 0 < len(text.encode()) <= 8192
    assert "[truncated]" in text
    assert "\ufffd" not in text


def test_wide_budget_has_no_old_twenty_four_file_cutoff(
    audit_project: tuple[Path, CodeIndex],
) -> None:
    root, index = audit_project
    text = index.context("p1", root, "engine", 200_000)
    assert text.count("--- ") == 35


def test_requested_files_share_hash_and_path_checks(
    audit_project: tuple[Path, CodeIndex], tmp_path: Path,
) -> None:
    root, index = audit_project
    assert "SCHEMA" in index.files("p1", root, ["contracts/schema.py"])
    outside = tmp_path / "outside.py"
    outside.write_text("PRIVATE = True\n")
    (root / "contracts" / "schema.py").unlink()
    (root / "contracts" / "schema.py").symlink_to(outside)
    assert index.files("p1", root, ["contracts/schema.py", "../outside.py"]) == ""
    (root / "engine" / "metrics.py").write_text("def modified(): pass\n")
    assert index.files("p1", root, ["engine/metrics.py"]) == ""
