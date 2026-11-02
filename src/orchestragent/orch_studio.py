"""`orch studio` over a real repository (docs/STUDIO.md). In progress: use --demo."""

from __future__ import annotations

from pathlib import Path


class RepoBackend:
    demo = False

    def __init__(self, repo: Path, data_dir: Path) -> None:
        raise SystemExit("orch studio: the repository backend is still being built; "
                         "use `orch studio --demo` meanwhile")
