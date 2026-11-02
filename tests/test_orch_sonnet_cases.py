import json
from pathlib import Path
from typing import cast

import pytest

from orchestragent.live_workspace import LiveWorkspace
from orchestragent.orch_bench import Case, load_suite
from tests.conftest import CaseProjectFactory, local_runner

SUITE = Path(__file__).resolve().parents[1] / "examples" / "orch-bench" / "sonnet-pilot.json"
REFERENCES = SUITE.parent / "reference"
CASE_IDS = ("stock-reconcile", "stream-frames")


@pytest.fixture
def cases() -> dict[str, Case]:
    return {case.id: case for case in load_suite(SUITE)}


@pytest.fixture(params=CASE_IDS)
def case(request: pytest.FixtureRequest, cases: dict[str, Case]) -> Case:
    return cases[str(request.param)]


@pytest.fixture
def reference_sources(case: Case) -> dict[str, str]:
    return cast(dict[str, str], json.loads((REFERENCES / f"{case.id}.json").read_text()))


@pytest.fixture
def projects(
    case_project: CaseProjectFactory, case: Case, reference_sources: dict[str, str],
) -> dict[str, Path]:
    return {
        "seed": case_project(case, "seed", acceptance=False),
        "baseline": case_project(case, "baseline"),
        "reference": case_project(case, "reference", reference_sources),
    }


@pytest.mark.parametrize(
    ("case_id", "solution_paths", "seed_paths", "acceptance_paths"),
    [
        pytest.param(
            "stock-reconcile",
            {"stock_aggregate.py", "reconcile_stock.py"},
            {"stock_events.py", "test_stock_events.py"},
            {"test_reconcile_stock.py"},
            id="inventory-reconciliation",
        ),
        pytest.param(
            "stream-frames",
            {"frame_parser.py", "frame_stream.py"},
            {"legacy_frame_parser.py", "legacy_frame_stream.py", "test_legacy_frame_stream.py"},
            {"test_stream_frames.py"},
            id="streaming-frames",
        ),
    ],
)
def test_case_contract_keeps_acceptance_and_references_separate(
    cases: dict[str, Case],
    case_id: str,
    solution_paths: set[str],
    seed_paths: set[str],
    acceptance_paths: set[str],
) -> None:
    case = cases[case_id]
    reference = cast(dict[str, str], json.loads((REFERENCES / f"{case_id}.json").read_text()))
    assert case.mock_shape is None
    assert set(case.seed) == seed_paths
    assert set(reference) == solution_paths
    assert not set(reference) & set(case.seed)
    assert set(case.acceptance) == acceptance_paths
    assert not set(case.acceptance) & set(case.seed)
    assert all(source not in case.spec for source in case.acceptance.values())
    assert all(source not in case.spec for source in reference.values())
    assert all(LiveWorkspace.safe_path(path) for path in (*case.seed, *reference, *case.acceptance))


def test_seeds_pass_baselines_but_references_pass_held_out_acceptance(
    tmp_path: Path, case: Case, projects: dict[str, Path],
) -> None:
    seed_passed, seed_detail = local_runner(
        projects["seed"], tmp_path / "seed-scratch", f"{case.id}-seed", None,
    )
    assert seed_passed, f"seed tests for {case.id} failed:\n{seed_detail}"

    baseline_passed, baseline_detail = local_runner(
        projects["baseline"], tmp_path / "baseline-scratch", f"{case.id}-baseline", None,
    )
    assert not baseline_passed, (
        f"seed implementation unexpectedly passed {case.id} acceptance:\n{baseline_detail}"
    )

    reference_passed, reference_detail = local_runner(
        projects["reference"], tmp_path / "reference-scratch", f"{case.id}-reference", None,
    )
    assert reference_passed, (
        f"reference for {case.id} failed seed or held-out tests:\n{reference_detail}"
    )
