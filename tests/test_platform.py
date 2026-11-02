from pathlib import Path

import pytest

from orchestragent import platform as host
from orchestragent.platform import sandbox_command, seccomp_filter


def test_seccomp_filter_fails_closed_on_an_unknown_architecture() -> None:
    with pytest.raises(RuntimeError, match="riscv64"):
        seccomp_filter("riscv64")


@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
def test_seccomp_filter_is_whole_instructions_ending_in_allow(machine: str) -> None:
    program = seccomp_filter(machine)
    assert len(program) % 8 == 0 and program[-8:-4] == b"\x06\x00\x00\x00"


def test_linux_sandbox_keeps_host_paths_and_binds_runtime_and_bundle_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host, "is_macos", lambda: False)
    monkeypatch.setattr("orchestragent.platform.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(host, "seccomp_filter", lambda: b"")
    work, bundle = tmp_path / "work", tmp_path / "bundle"
    python = tmp_path / "uv-python" / "bin" / "python3"
    command = sandbox_command(work, str(python), "pass", read_only=(bundle,))
    pairs = {(command[i], command[i + 1], command[i + 2]) for i in range(len(command) - 2)}
    resolved = [str(p.resolve()) for p in (work, bundle, tmp_path / "uv-python")]
    assert {("--bind", resolved[0], resolved[0]), ("--ro-bind", resolved[1], resolved[1]),
            ("--ro-bind", resolved[2], resolved[2])} <= pairs
    assert command[command.index("--chdir") + 1] == resolved[0]
    assert command[-1].endswith("pass") and "/work" not in command


def test_data_dir_prefers_env_then_checkout_then_per_user_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orchestragent import platform as plat
    monkeypatch.setenv("ORCHESTRA_DATA_DIR", str(tmp_path / "explicit"))
    assert plat.default_data_dir() == tmp_path / "explicit"
    monkeypatch.delenv("ORCHESTRA_DATA_DIR")
    checkout = plat.source_root()
    assert checkout is not None and plat.default_data_dir() == checkout / ".data"
    monkeypatch.setattr(plat, "source_root", lambda: None)  # installed as a package
    monkeypatch.setattr(plat, "is_macos", lambda: False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert plat.default_data_dir() == tmp_path / "xdg" / "orch"
    monkeypatch.setattr(plat, "is_macos", lambda: True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert plat.default_data_dir() == (
        tmp_path / "home" / "Library" / "Application Support" / "orch")
