import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestragent import orch_models
from orchestragent.orch_models import Choice, effective, parse, reset, set_role, table


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [*orch_models.ENVIRONMENT.values(), "ORCHESTRA_PLAN_EFFORT",
                 "ORCHESTRA_DSF_TEST_EFFORT"]:
        monkeypatch.delenv(name, raising=False)


def test_the_default_is_the_verified_benchmark_set(tmp_path: Path) -> None:
    assert {role: value for role, (value, _) in effective(tmp_path).items()} == \
        orch_models.VERIFIED
    assert table(tmp_path)[-1] == "All roles use the benchmarked set."


@pytest.mark.parametrize(("role", "value"), [
    ("planner", "openai/gpt:high"), ("planner", "claude/claude-opus-5-5:extreme"),
    ("builder", "claude/claude-opus-5-5:extreme"), ("test_designer", "dsh/deepseek-chat:low"),
    ("builder", "kimi/k3"), ("builder", "unregistered/model"), ("builder", "codex/"),
    ("builder", "dsh/deepseek-v4-pro:low"),  # not offered
    ("reviewer", "off"), ("pilot", "x"),  # the reviewer role no longer exists
])
def test_unsupported_choices_are_refused_with_the_allowed_form(role: str, value: str) -> None:
    with pytest.raises(ValueError):
        parse(role, value)


def test_set_reset_and_precedence_env_over_config_over_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "unverified" in set_role("builder", "dsh/deepseek-flash:high", tmp_path)
    assert effective(tmp_path)["builder"] == ("dsh/deepseek-flash:high", "config")
    monkeypatch.setenv("ORCHESTRA_BUILDER", "dsh/deepseek-flash:max")
    assert effective(tmp_path)["builder"] == ("dsh/deepseek-flash:max", "env")
    monkeypatch.delenv("ORCHESTRA_BUILDER")
    assert "Custom: builder" in table(tmp_path)[-1]
    reset("builder", tmp_path)
    assert effective(tmp_path)["builder"] == (orch_models.VERIFIED["builder"], "verified")
    assert not (tmp_path / "models.json").exists()


def test_older_single_purpose_variables_still_work(monkeypatch: pytest.MonkeyPatch,
                                                   tmp_path: Path) -> None:
    monkeypatch.setenv("ORCHESTRA_PLAN_EFFORT", "high")
    monkeypatch.setenv("ORCHESTRA_DSF_TEST_EFFORT", "off")
    chosen = effective(tmp_path)
    assert chosen["planner"][0] == "claude/claude-opus-5-5:high"
    assert parse("test_designer", chosen["test_designer"][0]) == Choice(
        "dsh", "deepseek-flash", "off")
    assert set(chosen) == set(orch_models.ROLES)


def test_the_engine_uses_the_configured_roles(tmp_path: Path) -> None:
    set_role("builder", "dsh/deepseek-flash:high", tmp_path)
    with pytest.raises(ValueError, match="role must be one of"):
        set_role("reviewer", "off", tmp_path)
    env = {**os.environ, "ORCHESTRA_DATA_DIR": str(tmp_path),
           "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    for name in orch_models.ENVIRONMENT.values():
        env.pop(name, None)
    probe = ("import orchestragent.orch_run as o; "
             "print(o.DSF.model, o.DSF_EXTENDED.model, o.PLANNER.effort)")
    out = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True,
                         text=True, check=True).stdout.split()
    assert out == ["deepseek-flash", "deepseek-flash", "medium"]


def test_byoc_and_registered_byok_providers_serve_any_model_role(tmp_path: Path) -> None:
    from orchestragent import byok
    byok.add(tmp_path, "local", "http://localhost:11434/v1")
    assert parse("builder", "codex/gpt-6-astra:high", tmp_path) == Choice(
        "codex", "gpt-6-astra", "high")
    assert parse("planner", "cursor/composer-2.5", tmp_path) == Choice(
        "cursor", "composer-2.5", "")
    assert parse("test_designer", "local/qwen2.5:7b", tmp_path) == Choice(
        "local", "qwen2.5:7b", "")  # an Ollama tag stays part of the model id
    assert parse("planner", "local/llama:high", tmp_path) == Choice("local", "llama", "high")
    with pytest.raises(ValueError, match="refused"):
        parse("planner", "kimi/k3", tmp_path)
    assert "unverified" in set_role("builder", "local/qwen2.5:7b", tmp_path)



def test_any_deepseek_route_gets_the_extended_output_tier(orchestrator) -> None:  # type: ignore[no-untyped-def]
    """A DeepSeek planner or supervisor thinks as long as a DeepSeek builder: the wider
    output budget is a property of the provider, not of the role (run r44d28e97: the
    integration design stopped at 32768 tokens of reasoning, and planning failed)."""
    from orchestragent.orch_run import Route
    widened = orchestrator._widened(Route("dsh", "deepseek-flash", "high"), "spec", {})
    assert widened == Route("dsh", "deepseek-flash", "high-extended")
    assert orchestrator._widened(widened, "spec", {}) is None  # never widened twice
    assert orchestrator._widened(Route("claude", "claude-opus-5-5", "medium"), "spec", {}) is None
