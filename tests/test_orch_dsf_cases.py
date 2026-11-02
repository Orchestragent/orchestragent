import json
from pathlib import Path
from typing import cast

import pytest

from orchestragent.live_workspace import LiveWorkspace
from orchestragent.orch_bench import Case, load_suite
from tests.conftest import CaseProjectFactory, local_runner

SUITE = Path(__file__).resolve().parents[1] / "examples" / "orch-bench" / "dsf-context.json"
REFERENCE = SUITE.parent / "reference" / "dsf-context.json"
CASE_IDS = ("interval-compact", "interval-expanded")
DISTRACTOR_PREFIX = "stress_pkg/distractors/"
TARGET_PATH = "stress_pkg/z_intervals.py"


@pytest.fixture
def cases() -> dict[str, Case]:
    return {case.id: case for case in load_suite(SUITE)}


@pytest.fixture(params=CASE_IDS)
def case(request: pytest.FixtureRequest, cases: dict[str, Case]) -> Case:
    return cases[str(request.param)]


@pytest.fixture
def reference_sources() -> dict[str, str]:
    return cast(dict[str, str], json.loads(REFERENCE.read_text()))


@pytest.fixture
def projects(
    case_project: CaseProjectFactory, case: Case, reference_sources: dict[str, str],
) -> dict[str, Path]:
    return {
        "seed": case_project(case, "seed", acceptance=False),
        "baseline": case_project(case, "baseline"),
        "reference": case_project(case, "reference", reference_sources),
    }


def test_cases_match_except_for_artificial_context(cases: dict[str, Case]) -> None:
    compact = cases["interval-compact"]
    expanded = cases["interval-expanded"]
    distractor_sources = {
        path: content for path, content in expanded.seed.items()
        if path.startswith(DISTRACTOR_PREFIX)
    }
    expanded_without_distractors = {
        path: content for path, content in expanded.seed.items()
        if not path.startswith(DISTRACTOR_PREFIX)
    }
    assert compact.id != expanded.id
    assert compact.spec == expanded.spec
    assert compact.acceptance == expanded.acceptance
    assert compact.mock_shape is None and expanded.mock_shape is None
    assert compact.seed == expanded_without_distractors
    assert set(distractor_sources) >= {
        f"{DISTRACTOR_PREFIX}__init__.py",
        f"{DISTRACTOR_PREFIX}a_catalog_00.py",
        f"{DISTRACTOR_PREFIX}a_catalog_55.py",
    }
    assert sum(len(content.encode("utf-8")) for content in distractor_sources.values()) > 40_000
    assert all(
        "def lookup(" in content
        for path, content in distractor_sources.items()
        if "a_catalog_" in path
    )
    for path, content in distractor_sources.items():
        compile(content, path, "exec")
    assert max(distractor_sources) < TARGET_PATH
    assert TARGET_PATH in compact.seed and TARGET_PATH in expanded.seed


def test_reference_only_replaces_target_module(
    reference_sources: dict[str, str], cases: dict[str, Case],
) -> None:
    assert set(reference_sources) == {TARGET_PATH}
    assert all(LiveWorkspace.safe_path(path) for path in reference_sources)
    for case in cases.values():
        assert TARGET_PATH in case.seed
        assert reference_sources[TARGET_PATH] != case.seed[TARGET_PATH]
        assert set(case.acceptance) == {"test_interval_merge.py"}
        assert not set(case.acceptance) & set(case.seed)
        assert all(LiveWorkspace.safe_path(path) for path in (*case.seed, *case.acceptance))


def test_seed_tests_pass_baseline_fails_and_reference_passes(
    tmp_path: Path, case: Case, projects: dict[str, Path],
) -> None:
    seed_passed, seed_detail = local_runner(
        projects["seed"], tmp_path / "seed-scratch", f"{case.id}-seed", None,
    )
    assert seed_passed, f"seed regressions failed for {case.id}:\n{seed_detail}"

    baseline_passed, baseline_detail = local_runner(
        projects["baseline"], tmp_path / "baseline-scratch", f"{case.id}-baseline", None,
    )
    assert not baseline_passed, (
        f"seed target unexpectedly passed held-out tests:\n{baseline_detail}"
    )
    assert "AssertionError" in baseline_detail, baseline_detail

    reference_passed, reference_detail = local_runner(
        projects["reference"], tmp_path / "reference-scratch", f"{case.id}-reference", None,
    )
    assert reference_passed, (
        f"reference failed seed or held-out tests for {case.id}:\n{reference_detail}"
    )
