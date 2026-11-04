"""Validated file bundles and no-network OS-sandboxed execution of generated tests."""

import re
from pathlib import PurePosixPath

PROJECT_TEST_TIMEOUT = 600.0
PROJECT_CPU_SECONDS = 300


def safe_relative(path: str) -> bool:
    """A relative, normalized, visible path of any file type (any language)."""
    parts = PurePosixPath(path).parts
    return bool(parts) and not (
        path.startswith("/")
        or "\\" in path
        or len(path) > 200
        or str(PurePosixPath(path)) != path
        or any(part.startswith(".") for part in parts)
        or not re.fullmatch(r"[A-Za-z0-9_./@+-]+", path)
    )
