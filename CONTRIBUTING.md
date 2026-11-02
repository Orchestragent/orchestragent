# Contributing

```sh
uv sync                       # Python 3.12+, installs dev and index groups
uv run pytest -q              # full suite; no model calls, no network
uv run ruff check src tests && uv run mypy
```

- Runtime code uses the Python standard library only (tree-sitter is optional).
- Tests never call a model provider; use the scripted callers in `tests/conftest.py`.
- Keep changes small and measured: performance or quality claims need repeated real runs
  (see `scripts/orch_repository_probe.py` and `docs/measurements`).
- Never commit keys, `.data/`, or benchmark outputs containing personal paths.
