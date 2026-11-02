import json
from pathlib import Path
from typing import cast

import pytest

from orchestragent.live_workspace import LiveWorkspace
from orchestragent.orch_bench import Case, load_suite
from tests.conftest import CaseProjectFactory, local_runner

SUITE = Path(__file__).resolve().parents[1] / "examples" / "orch-bench" / "project-suite.json"
REFERENCE = SUITE.parent / "reference" / "log-insights.json"


@pytest.fixture
def project_case() -> Case:
    [case] = load_suite(SUITE)
    return case


@pytest.fixture
def reference_sources() -> dict[str, str]:
    return cast(dict[str, str], json.loads(REFERENCE.read_text()))


@pytest.fixture
def reference_project(
    case_project: CaseProjectFactory, project_case: Case, reference_sources: dict[str, str],
) -> Path:
    return case_project(project_case, "reference", reference_sources)


@pytest.mark.parametrize("check", [
    "curated", "seed", "modules", "held_out", "acceptance", "safe_paths",
])
def test_project_suite_is_curated_modular_and_keeps_acceptance_held_out(
    project_case: Case, reference_sources: dict[str, str], check: str,
) -> None:
    modules = {
        "log_parser.py", "status_counts.py", "latency_quantile.py", "time_buckets.py",
        "failure_fraction.py", "event_summary.py",
    }
    checks = {
        "curated": project_case.id == "log-insights" and project_case.mock_shape is None,
        "seed": {"log_events.py", "test_log_events.py"} <= project_case.seed.keys(),
        "modules": modules == reference_sources.keys(),
        "held_out": not set(reference_sources) & set(project_case.seed)
        and "test_log_insights.py" not in project_case.seed
        and "test_log_insights.py" not in project_case.spec,
        "acceptance": project_case.acceptance.keys() == {"test_log_insights.py"},
        "safe_paths": all(LiveWorkspace.safe_path(path) for path in reference_sources),
    }
    assert checks[check]


def test_reference_and_seed_tests_pass_together_with_local_runner(
    tmp_path: Path, reference_project: Path,
) -> None:
    passed, detail = local_runner(
        reference_project, tmp_path / "scratch", "trusted-log-insights-reference", None,
    )
    assert passed, f"trusted reference or preserved seed tests failed:\n{detail}"
