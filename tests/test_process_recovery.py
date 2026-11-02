import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def restart_environment(tmp_path: Path) -> dict[str, str]:
    if shutil.which("flock") is None:
        pytest.skip("og-restart runs on the Linux deployment host")
    commands = {
        "curl": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CURL_MARKER"\n'
                'case "$*" in *"/health"*) echo healthy;; '
                '*) printf %s "$DRAIN_CODE";; esac\n',
        "sudo": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$RESTART_MARKER"\n'
                'exit "${RESTART_EXIT:-0}"\n',
        "sleep": "#!/bin/sh\nexit 0\n",
    }
    for name, content in commands.items():
        path = tmp_path / name
        path.write_text(content)
        path.chmod(0o700)
    return {"PATH": f"{tmp_path}:{os.defpath}", "WAIT": "0", "TMPDIR": str(tmp_path),
            "RESTART_MARKER": str(tmp_path / "restarted"),
            "CURL_MARKER": str(tmp_path / "requests")}


@pytest.mark.parametrize("code,expected", [("200", True), ("409", False),
                                           ("404", False), ("500", False)])
def test_restart_script_fails_closed_unless_gate_is_acquired(
    restart_environment: dict[str, str], code: str, expected: bool,
) -> None:
    result = subprocess.run(["bash", str(ROOT / "scripts/safe_restart.sh")],
                            env={**restart_environment, "DRAIN_CODE": code},
                            capture_output=True, timeout=5)
    marker = Path(restart_environment["RESTART_MARKER"])
    assert (result.returncode == 0, marker.exists()) == (expected, expected)
    if expected:
        assert marker.read_text() == "systemctl restart orchestragent\n"


def test_failed_restart_releases_admission_gate(restart_environment: dict[str, str]) -> None:
    result = subprocess.run(["bash", str(ROOT / "scripts/safe_restart.sh")],
                            env={**restart_environment, "DRAIN_CODE": "200", "RESTART_EXIT": "1"},
                            capture_output=True, timeout=5)
    assert result.returncode != 0
    assert '{"enabled":false}' in Path(restart_environment["CURL_MARKER"]).read_text()
