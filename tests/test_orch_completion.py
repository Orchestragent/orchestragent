import shutil
import subprocess
from pathlib import Path

import pytest

from orchestragent.orch_cli import main, parser
from orchestragent.orch_completion import commands, render


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion_covers_every_command_and_is_valid_shell(
    shell: str, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["completion", shell]) == 0
    script = capsys.readouterr().out
    assert script == render(shell, parser())
    _, subs = commands(parser())
    names = {cmd.name for cmd in subs}
    assert {"plan", "run", "watch", "mcp", "doctor", "completion"} <= names
    assert all(name in script for name in names)
    assert "claude-desktop" in script and ("-l replay" if shell == "fish" else "--replay") in script
    binary = shutil.which(shell)
    if binary is None:
        pytest.skip(f"{shell} not installed")
    path = tmp_path / f"orch.{shell}"
    path.write_text(script)
    assert subprocess.run([binary, "-n", str(path)], capture_output=True).returncode == 0


def test_bash_completion_offers_subcommands_flags_and_choices(tmp_path: Path) -> None:
    if shutil.which("bash") is None:
        pytest.skip("bash not installed")
    path = tmp_path / "orch.bash"
    path.write_text(render("bash", parser()))
    probe = (f"source {path}\n"
             "COMP_WORDS=(orch wa); COMP_CWORD=1; _orch; echo \"${COMPREPLY[*]}\"\n"
             "COMP_WORDS=(orch mcp --install ''); COMP_CWORD=3; _orch; echo \"${COMPREPLY[*]}\"\n")
    lines = subprocess.run(["bash", "-c", probe], capture_output=True, text=True,
                           check=True).stdout.splitlines()
    assert lines[0] == "watch"
    assert lines[1].split() == ["claude-code", "cursor", "claude-desktop"]


def test_suppressed_flags_are_not_offered() -> None:
    _, subs = commands(parser())
    keys = next(cmd for cmd in subs if cmd.name == "keys")
    assert "--yes" not in {flag for flag, _, _ in keys.flags}  # hidden compatibility flag


def test_homebrew_formula_renders_from_the_release_wheel(tmp_path: Path) -> None:
    import hashlib
    import importlib.util
    script = Path(__file__).resolve().parents[1] / "scripts" / "render_formula.py"
    spec = importlib.util.spec_from_file_location("render_formula", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    render = module.render
    wheel = tmp_path / "orchestragent-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel bytes")
    formula = render(wheel)
    assert "releases/download/v1.2.3/orchestragent-1.2.3-py3-none-any.whl" in formula
    assert f'sha256 "{hashlib.sha256(b"wheel bytes").hexdigest()}"' in formula
    assert "@VERSION@" not in formula and "@SHA256@" not in formula
    assert 'generate_completions_from_executable(bin/"orch", "completion")' in formula
    with pytest.raises(SystemExit):
        render(tmp_path / "other-1.0.0.whl")
