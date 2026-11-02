"""Revision-pinned context reads for planners, test designers and builders, in any language."""

from collections.abc import Iterable
from pathlib import Path

from orchestragent.live_workspace import safe_relative
from orchestragent.orch_index import Index

TRUNCATED = "\n... [truncated by orch: {omitted} more bytes; the file continues]\n"


def planning_sources(index: Index, revision: str, paths: Iterable[str], *,
                     any_file: bool = True) -> dict[str, str]:
    """The pinned existing contents of these tracked files (new files are simply absent)."""
    return index.read(revision, sorted(set(paths)))


def requested_sources(
    index: Index, tree: Path, revision: str, selectors: object, *, catalog_bytes: int = 0,
    any_file: bool = True,
) -> dict[str, str]:
    """Whole tracked files a model asked to read, at one candidate revision.

    `catalog_bytes` > 0 caps each file (a request that would not fit otherwise); the cut
    is marked so the model knows the file continues.
    """
    if (not isinstance(selectors, list) or not 1 <= len(selectors) <= 16
            or any(not isinstance(item, str) for item in selectors)):
        raise ValueError("need_files must be a nonempty list of tracked paths")
    names = sorted(str(item) for item in selectors)
    if len(set(names)) != len(names):
        raise ValueError("need_files selectors must be unique")
    tracked = index.tree(revision)
    for name in names:
        target = tree / name
        if (not safe_relative(name) or name not in tracked or target.is_symlink()
                or not target.resolve().is_relative_to(tree.resolve())):
            raise ValueError(f"Unknown or unsafe path: {name}")
    sources = index.read(revision, names)
    if not catalog_bytes:
        return sources
    return {name: (text if len(text.encode()) <= catalog_bytes else
                   text.encode()[:catalog_bytes].decode("utf-8", "ignore")
                   + TRUNCATED.format(omitted=len(text.encode()) - catalog_bytes))
            for name, text in sources.items()}
