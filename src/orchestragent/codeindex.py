"""Static code index for a real project tree.

Only names and signatures are indexed; prompts always receive the bytes on disk,
verified by hash, so a stale index can miss a file but never feed a wrong one.
"""

import ast
import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path

SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".build", ".data", "dist"}
MAX_FILE = 400_000
MAX_FILES = 20_000
TEXT_SUFFIX = {".py", ".md", ".txt", ".toml", ".yaml", ".yml", ".json", ".cfg"}


@dataclass(frozen=True)
class Hit:
    path: str
    score: int
    symbols: str
    size: int


def _symbols(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    names.append(target.id)
    return names


Definition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def definitions(tree: ast.Module) -> list[tuple[str, Definition, str]]:
    """(qualified name, node, one-line signature) for top-level functions, classes and
    their methods, in file order. The interface check and the repo map share it."""

    def render(node: ast.FunctionDef | ast.AsyncFunctionDef, owner: str) -> str:
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
        return f"{prefix} {owner}{node.name}({ast.unparse(node.args)}){returns}"

    found: list[tuple[str, Definition, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append((node.name, node, render(node, "")))
        elif isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(base) for base in node.bases)
            found.append(
                (node.name, node, f"class {node.name}({bases})" if bases else f"class {node.name}")
            )
            found.extend(
                (f"{node.name}.{item.name}", item, render(item, f"{node.name}."))
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
    return found


def signatures(source: str) -> list[str]:
    """Top-level functions, classes and their methods as one-line signatures.

    Raises SyntaxError for unparsable source; order follows the file.
    """
    return [line for _, _, line in definitions(ast.parse(source))]


class CodeIndex:
    max_bytes = 60_000
    max_file_bytes = 24_000

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @classmethod
    def ensure(cls, path: Path) -> "CodeIndex":
        index = cls(path)
        with index.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS code_files ("
                "project TEXT, rel TEXT, symbols TEXT, imports TEXT, size INTEGER,"
                " digest TEXT, PRIMARY KEY(project, rel))"
            )
        return index

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A fresh connection per call: HTTP handlers run on other threads."""
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def build(self, project: str, root: Path) -> dict[str, int]:
        with self.connect() as db:
            return self._build(db, project, root)

    def _build(
        self, db: sqlite3.Connection, project: str, root: Path
    ) -> dict[str, int]:
        root = root.resolve()
        if not root.is_dir():
            raise ValueError("Project root is not a directory")
        seen: set[str] = set()
        added = skipped = 0
        for file in sorted(root.rglob("*")):
            if len(seen) >= MAX_FILES:
                break
            if not file.is_file() or file.suffix.lower() not in TEXT_SUFFIX:
                continue
            if not file.resolve().is_relative_to(root):
                continue
            if any(part in SKIP for part in file.relative_to(root).parts):
                continue
            rel = str(file.relative_to(root))
            try:
                raw = file.read_bytes()
            except OSError:
                skipped += 1
                continue
            if len(raw) > MAX_FILE:
                skipped += 1
                continue
            seen.add(rel)
            digest = hashlib.sha256(raw).hexdigest()
            row = db.execute(
                "SELECT digest FROM code_files WHERE project=? AND rel=?", (project, rel)
            ).fetchone()
            if row is not None and row["digest"] == digest:
                continue
            names = ""
            imports = ""
            if file.suffix == ".py":
                try:
                    tree = ast.parse(raw.decode("utf-8", "replace"))
                    names = " ".join(_symbols(tree))
                    imports = " ".join(
                        node.module or ""
                        for node in ast.walk(tree)
                        if isinstance(node, ast.ImportFrom)
                    )
                except (SyntaxError, ValueError):
                    names = ""
            db.execute(
                "INSERT OR REPLACE INTO code_files VALUES(?,?,?,?,?,?)",
                (project, rel, names, imports, len(raw), digest),
            )
            added += 1
        # Drop files that disappeared between builds.
        known = {
            row["rel"]
            for row in db.execute(
                "SELECT rel FROM code_files WHERE project=?", (project,)
            ).fetchall()
        }
        for rel in known - seen:
            db.execute("DELETE FROM code_files WHERE project=? AND rel=?", (project, rel))
        return {"indexed": added, "skipped": skipped, "total": len(seen)}

    def search(self, project: str, query: str, limit: int = 8) -> list[Hit]:
        with self.connect() as db:
            return self._search(db, project, query, limit)

    def _search(
        self, db: sqlite3.Connection, project: str, query: str, limit: int
    ) -> list[Hit]:
        terms = [term.lower() for term in query.replace("/", " ").split() if len(term) > 1]
        rows = db.execute(
            "SELECT rel, symbols, imports, size FROM code_files WHERE project=? LIMIT ?",
            (project, MAX_FILES),
        ).fetchall()
        hits: list[Hit] = []
        for row in rows:
            haystack = f"{row['rel']} {row['symbols']} {row['imports']}".lower()
            symbols = row["symbols"].lower()
            score = sum(
                3 if term in symbols else 1 for term in terms if term in haystack
            )
            if score:
                hits.append(Hit(row["rel"], score, row["symbols"], row["size"]))
        hits.sort(key=lambda hit: (hit.score, -hit.size), reverse=True)
        return hits[:limit]

    def context(
        self,
        project: str,
        root: Path,
        query: str,
        budget: int | None = None,
        prefixes: list[str] | None = None,
    ) -> str:
        """Return real file contents for the best matches, inside a byte budget."""
        with self.connect() as db:
            return self._context(
                db, project, root, query, budget or self.max_bytes, prefixes or []
            )

    def _context(
        self,
        db: sqlite3.Connection,
        project: str,
        root: Path,
        query: str,
        budget: int,
        prefixes: list[str],
    ) -> str:
        hits = self._search(db, project, query, MAX_FILES)
        if budget > self.max_bytes or prefixes:
            matched = {hit.path for hit in hits}
            rows = db.execute(
                "SELECT rel, symbols, size FROM code_files WHERE project=? ORDER BY rel LIMIT ?",
                (project, MAX_FILES),
            ).fetchall()
            hits.extend(Hit(row["rel"], 0, row["symbols"], row["size"])
                        for row in rows if row["rel"] not in matched)
        if prefixes:
            hits = [hit for hit in hits if any(hit.path.startswith(p) for p in prefixes)]
        if budget > self.max_bytes:
            # Audit context covers directories before spending the rest on one area.
            groups: dict[str, list[Hit]] = {}
            for hit in hits:
                groups.setdefault(str(Path(hit.path).parent), []).append(hit)
            hits = [hit for row in zip_longest(*groups.values()) for hit in row if hit is not None]
        elif not prefixes:
            hits = hits[:8]
        return self._render(db, project, root, hits, budget, self.max_file_bytes)

    def files(self, project: str, root: Path, names: list[str], budget: int = 200_000) -> str:
        """One bounded request for exact indexed paths, using the same hash and path checks."""
        with self.connect() as db:
            hits = []
            for name in dict.fromkeys(names[:20]):
                row = db.execute(
                    "SELECT rel, symbols, size FROM code_files WHERE project=? AND rel=?",
                    (project, name),
                ).fetchone()
                if row is not None:
                    hits.append(Hit(row["rel"], 0, row["symbols"], row["size"]))
            return self._render(db, project, root, hits, budget, 40_000)

    def _render(
        self, db: sqlite3.Connection, project: str, root: Path, hits: list[Hit],
        budget: int, per_file: int,
    ) -> str:
        root = root.resolve()
        chunks: list[str] = []
        used = 0
        for hit in hits:
            path = (root / hit.path).resolve()
            if not path.is_relative_to(root):
                continue
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            digest = hashlib.sha256(raw).hexdigest()
            row = db.execute(
                "SELECT digest FROM code_files WHERE project=? AND rel=?", (project, hit.path)
            ).fetchone()
            if row is None or row["digest"] != digest:
                continue  # A stale index must never supply unverified contents.
            header = f"--- {hit.path} ---\n"
            marker = "\n[truncated]\n"
            available = budget - used - len(header.encode()) - len(marker.encode())
            if available <= 0:
                break
            text = raw[:min(per_file, available)].decode("utf-8", "ignore")
            suffix = marker if len(text.encode()) < len(raw) else "\n"
            block = header + text + suffix
            chunks.append(block)
            used += len(block.encode())
        return "".join(chunks)
