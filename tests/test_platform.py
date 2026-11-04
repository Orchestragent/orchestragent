from pathlib import Path

import pytest

from orchestragent import platform as host
from orchestragent.platform import seccomp_filter


def test_seccomp_filter_fails_closed_on_an_unknown_architecture() -> None:
    with pytest.raises(RuntimeError, match="riscv64"):
        seccomp_filter("riscv64")


@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
def test_seccomp_filter_is_whole_instructions_ending_in_allow(machine: str) -> None:
    program = seccomp_filter(machine)
    assert len(program) % 8 == 0 and program[-8:-4] == b"\x06\x00\x00\x00"


def test_data_dir_prefers_env_then_checkout_then_per_user_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORCHESTRA_DATA_DIR", str(tmp_path / "explicit"))
    assert host.default_data_dir() == tmp_path / "explicit"
    monkeypatch.delenv("ORCHESTRA_DATA_DIR")
    checkout = host.source_root()
    assert checkout is not None and host.default_data_dir() == checkout / ".data"
    monkeypatch.setattr(host, "source_root", lambda: None)  # installed as a package
    monkeypatch.setattr(host, "is_macos", lambda: False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert host.default_data_dir() == tmp_path / "xdg" / "orch"
    monkeypatch.setattr(host, "is_macos", lambda: True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert host.default_data_dir() == (
        tmp_path / "home" / "Library" / "Application Support" / "orch")
