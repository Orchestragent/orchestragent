import json
from pathlib import Path
from typing import cast

import pytest

from orchestragent.live_workspace import LiveWorkspace
from orchestragent.orch_bench import ACCEPTANCE_PACKAGE, Case, load_suite
from tests.conftest import local_runner

SUITE = Path(__file__).resolve().parents[1] / "examples" / "orch-bench" / "real-suite.json"
REFERENCES = SUITE.parent / "reference"
CASE_IDS = (
    "dag-batches",
    "ttl-lru",
    "json-merge-patch",
    "csv-people",
    "usage-aggregate",
    "event-pipeline",
)


@pytest.fixture
def cases() -> dict[str, Case]:
    return {case.id: case for case in load_suite(SUITE)}


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_trusted_reference_passes_held_out_acceptance(
    tmp_path: Path, cases: dict[str, Case], case_id: str,
) -> None:
    case = cases[case_id]
    reference = cast(
        dict[str, str], json.loads((REFERENCES / f"{case_id}.json").read_text())
    )
    assert reference
    assert not set(reference) & set(case.seed)
    assert all(source not in case.spec for source in reference.values())

    project = tmp_path / "project"
    project.mkdir()
    for name, source in case.seed.items():
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
    for name, source in reference.items():
        assert LiveWorkspace.safe_path(name), name
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)

    acceptance = project / ACCEPTANCE_PACKAGE
    acceptance.mkdir()
    (acceptance / "__init__.py").write_text("")
    for name, source in case.acceptance.items():
        (acceptance / name).write_text(source)
    test_names = [f"{ACCEPTANCE_PACKAGE}.{Path(name).stem}" for name in case.acceptance]
    passed, detail = local_runner(project, tmp_path / "scratch", "trusted-reference", test_names)
    assert passed, f"reference for {case_id} failed held-out acceptance:\n{detail}"
