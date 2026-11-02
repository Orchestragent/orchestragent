"""Revision-pinned context reads for scoped single-model and cluster execution."""

import re
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from orchestragent.dsh import DshClient
from orchestragent.live_workspace import LiveWorkspace
from orchestragent.orch_excerpt import definition_view, excerpt
from orchestragent.orch_index import Index, RepoIndex, dependency_code
from orchestragent.serialization import dumps


def planning_sources(index: Index, revision: str, paths: Iterable[str]) -> dict[str, str]:
    """Legacy task files plus referenced definitions, never a transitive module import dump."""
    selected = sorted({path for path in paths if path.endswith(".py")
                       and not LiveWorkspace.is_test_file(path)})
    snapshot = index.at(revision)
    sources = index.read(revision, selected)
    sources.update(dependency_code(
        snapshot, lambda names: index.read(revision, names), snapshot.roots(selected, []),
        set(selected), DshClient.input_limit,
    ))
    return sources


def owned_sources(
    index: RepoIndex, sources: dict[str, str], tests: Iterable[str], selectors: Iterable[str] = (),
) -> dict[str, str]:
    """Conservative editable views; explicit whole-file reads always recover full context."""
    requested = set(selectors)
    selected: dict[str, set[str]] = {path: set() for path in sources}
    for path, symbol, _ in index.referenced(index.roots([], tests)):
        if path in selected:
            selected[path].add(symbol.name)
    for selector in requested:
        path, separator, name = selector.partition("::")
        if separator and path in selected:
            selected[path].add(name)
    result = {}
    for path, source in sources.items():
        view = (excerpt(path, source, selected[path])
                if len(source.encode()) > 16384 and path not in requested else source)
        result[path] = view if len(view.encode()) < len(source.encode()) else source
    return result


def requested_sources(
    index: Index, tree: Path, revision: str, selectors: object, *, catalog_bytes: int = 0,
) -> dict[str, str]:
    """Resolve explicit Python file/symbol reads at one candidate revision."""
    if (not isinstance(selectors, list) or not 1 <= len(selectors) <= 16
            or any(not isinstance(item, str) for item in selectors)):
        raise ValueError("need_files must be a nonempty list of Python paths or path::symbol")
    names = sorted(str(item) for item in selectors)
    if len(set(names)) != len(names):
        raise ValueError("need_files selectors must be unique")
    paths = list(dict.fromkeys(name.split("::", 1)[0] for name in names))
    guard = ScopedContext(index, tree, revision, "", 32768)
    guard.request(paths, revision)
    repo = index.at(revision)
    sources = index.read(revision, paths)
    for name in names:
        path, separator, symbol = name.partition("::")
        if separator and (not symbol or path not in repo.files
                          or symbol not in {item.name for item in repo.files[path].symbols}):
            raise ValueError(f"Unknown symbol selector: {name}")
    result = {}
    for name in names:
        path, separator, symbol = name.partition("::")
        # Validate every selector first; a valid parent must not mask an invalid child.
        # A whole class already contains its methods, including in exact-definition mode.
        if separator and (any(name.startswith(other + ".") for other in names if "::" in other)
                          or (not catalog_bytes and path in names)):
            continue
        if catalog_bytes:
            if separator:
                result[name] = definition_view(path, sources[path], symbol)
            else:
                result[name] = (
                    "# PARTIAL CONTEXT: full file exceeds request budget; signatures only.\n"
                    "# Request path.py::Symbol for definitions; module support is omitted.\n"
                    f"# revision: {revision}\n"
                    + RepoIndex({path: repo.files[path]}).repo_map(catalog_bytes)
                )
            continue
        result[name] = excerpt(path, sources[path], {symbol}) if separator else sources[path]
    return result


class ScopedContext:
    def __init__(self, index: Index, tree: Path, base: str, spec: str, limit: int) -> None:
        self.index, self.tree, self.base, self.spec = index, tree, base, spec
        self.limit = min(limit, DshClient.input_limit)
        original = index.tree(base)
        self.seed_tests = any(LiveWorkspace.is_test_file(path) for path in original)
        self.frozen = {
            path for path in original
            if LiveWorkspace.is_test_file(path)
            or {"test", "tests"} & set(PurePosixPath(path).parts[:-1])
            or PurePosixPath(path).name == "conftest.py"
        }
        for path in tuple(self.frozen):
            for parent in PurePosixPath(path).parents:
                self.frozen.add(str(parent / "__init__.py"))
        self.visible: dict[str, str] = {}
        self.requested: list[str] = []

    def _safe(self, path: str, tracked: dict[str, str]) -> bool:
        target = self.tree / path
        return (
            LiveWorkspace.safe_path(path) and path.endswith(".py") and path in tracked
            and not target.is_symlink()
            and target.resolve().is_relative_to(self.tree.resolve())
        )

    def request(self, value: object, revision: str) -> None:
        tracked = self.index.tree(revision)
        if (not isinstance(value, list) or not 1 <= len(value) <= 40
                or any(not isinstance(path, str) for path in value)):
            raise ValueError("need_files must contain 1–40 tracked Python paths")
        paths = [str(path) for path in value]
        if len(set(paths)) != len(paths) or any(not self._safe(p, tracked) for p in paths):
            raise ValueError("need_files requires unique safe existing tracked Python paths")
        self.requested = paths

    def prompt(self, revision: str, feedback: str, schema: dict[str, object]) -> str:
        repo = self.index.at(revision)
        tracked = self.index.tree(revision)
        roots = {
            path for path in re.findall(r"[A-Za-z0-9_/-]+\.py\b", self.spec)
            if path in repo.files
        }
        for token in re.findall(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b", self.spec):
            resolved = repo.resolve(token)
            if resolved and (token in repo.modules or resolved[1]):
                roots.add(resolved[0])
        dependencies = set().union(*(repo.reach(path) for path in roots))
        tests = {
            path for path in repo.files
            if LiveWorkspace.is_test_file(path) and repo.reach(path) & roots
        }
        priority = list(dict.fromkeys([
            *self.requested, *sorted(roots), *sorted(tests), *sorted(dependencies),
        ]))
        candidates = self.index.read(
            revision, [p for p in priority if self._safe(p, tracked)],
        )
        visible: dict[str, str] = {}
        feedback = feedback.encode()[-512:].decode("utf-8", "ignore")
        repo_map = repo.repo_map(min(2400, self.limit // 5))

        def render() -> str:
            return (
                "Implement the full specification. Return exactly one nonempty action: "
                "files (changed files only, complete replacements/additions) OR need_files "
                "(tracked Python paths). "
                "Keep the other array empty. Request unseen files before editing them. "
                "Original tests, test packages and conftest.py are frozen. Use stdlib only; "
                "include unittest test_*.py tests if none exist. Prefer root-level tests; "
                "nested test directories need __init__.py in every parent directory "
                "for discovery. File requests consume the call limit.\n\n"
                f"SPECIFICATION:\n{self.spec}\n\nCONTEXT:\n" + dumps({
                    "base_revision": self.base, "current_revision": revision,
                    "repository_map": repo_map, "visible_python_sources": visible,
                    "latest_feedback": feedback,
                })
            )

        def fits() -> bool:
            return len(DshClient.task_text(render(), schema).encode()) <= self.limit

        if not fits():
            raise ValueError("Specification, schema and map exceed context_bytes; shorten spec "
                             "or increase context_bytes")
        for path in priority:
            if path not in candidates:
                if path in self.requested:
                    raise ValueError(f"Requested file is unavailable or unsafe: {path}")
                continue
            visible[path] = candidates[path]
            if not fits():
                del visible[path]
                if path in self.requested:
                    raise ValueError(f"Requested complete files exceed context_bytes at {path}; "
                                     "request fewer files or increase context_bytes")
        self.visible = visible
        return render()

    def validate_edits(self, files: list[dict[str, str]], revision: str) -> None:
        tracked = self.index.tree(revision)
        aliases = {path.casefold(): path for path in tracked}
        frozen = {path.casefold() for path in self.frozen}
        available = set(tracked) | {file["path"] for file in files}
        for file in files:
            path = file["path"]
            if path.casefold() in frozen or PurePosixPath(path).name.casefold() == "conftest.py":
                raise ValueError(f"Original test/collection file is frozen: {path}")
            existing = aliases.get(path.casefold())
            if existing is not None and (existing != path or path not in self.visible):
                raise ValueError(f"Request complete current source before replacing: {path}")
            if LiveWorkspace.is_test_file(path):
                for parent in PurePosixPath(path).parents:
                    if parent == PurePosixPath("."):
                        continue
                    marker = str(parent / "__init__.py")
                    if marker not in available:
                        raise ValueError(f"unittest discovery requires {marker}; "
                                         "include it or put the new test at repository root")
