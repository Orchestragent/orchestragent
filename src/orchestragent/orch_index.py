"""Code index for orchestration: symbols, imports and references, cached per git blob.

Two levels, as the design asks: tree-sitter gives syntax-level definitions for every
language it has a grammar for (and for Python files that do not parse); Python files
that parse get semantic data from the standard `ast` module as well: resolved imports
and the symbols each definition references. Entries are keyed by (path, blob id), so
after a merge only changed files are parsed again; a checkpoint stores a snapshot.

Uses: the planner's and builder's repo maps, dependency code packed along the call
graph, the static-dependency plan lint, import checks on builder output, test impact
analysis for the merge gate, assertion counts for test corrections, AST-span blame
and the git co-change coupling graph.
"""

import ast
import importlib
import json
import re
import sqlite3
import threading
from collections import Counter, deque
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from functools import cache
from pathlib import Path, PurePosixPath

from orchestragent.codeindex import definitions
from orchestragent.envelope import digest
from orchestragent.serialization import dumps
from orchestragent.workspace import Workspace

try:
    from tree_sitter import Language, Node, Parser

    TREE_SITTER = True
except ImportError:  # optional: Python keeps its ast index, other languages go unparsed
    TREE_SITTER = False

INDEX_VERSION = 2
MAX_BLOB = 400_000
LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
}
GRAMMARS = {
    "python": ("tree_sitter_python", "language"),
    "javascript": ("tree_sitter_javascript", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
    "go": ("tree_sitter_go", "language"),
    "rust": ("tree_sitter_rust", "language"),
    "java": ("tree_sitter_java", "language"),
}
_SCRIPT = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "method_definition": "method",
    "method_signature": "method",
    "function_signature": "function",
    "interface_declaration": "type",
    "type_alias_declaration": "type",
    "enum_declaration": "type",
}
DEFINITIONS: dict[str, dict[str, str]] = {
    "python": {"function_definition": "function", "class_definition": "class"},
    "javascript": _SCRIPT,
    "typescript": _SCRIPT,
    "tsx": _SCRIPT,
    "go": {"function_declaration": "function", "method_declaration": "method",
           "type_spec": "type"},
    "rust": {"function_item": "function", "function_signature_item": "function",
             "struct_item": "type", "enum_item": "type", "trait_item": "type",
             "impl_item": "impl", "mod_item": "module"},
    "java": {"class_declaration": "class", "interface_declaration": "type",
             "enum_declaration": "type", "record_declaration": "class",
             "method_declaration": "method", "constructor_declaration": "method"},
}
CONTAINERS = frozenset({
    "class_definition", "class_declaration", "abstract_class_declaration",
    "interface_declaration", "impl_item", "trait_item", "mod_item", "enum_declaration",
    "record_declaration",
})
# Nodes that hold definitions without being one: export wrappers, decorators, bodies.
PASSTHROUGH = frozenset({
    "export_statement", "decorated_definition", "class_body", "declaration_list",
    "interface_body", "enum_body", "block", "type_declaration", "object_type",
})
IMPORTS = frozenset({
    "import_statement", "import_from_statement", "import_declaration", "use_declaration",
})
SCRIPT_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


@dataclass(frozen=True)
class Symbol:
    name: str  # qualified inside its file: "Class.method"
    kind: str  # function | method | class | type | module
    line: int  # 1-based first line of the definition
    end: int  # 1-based last line
    body: int  # first line of the body (after a docstring): what a task wrote
    signature: str
    digest: str  # of the normalized node: unchanged when only other code moved


@dataclass(frozen=True)
class FileIndex:
    path: str
    language: str
    backend: str  # "ast", "tree-sitter", or "none" (no grammar, too large, unparsable)
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)  # dotted (Python) or raw specifiers
    refs: dict[str, list[str]] = field(default_factory=dict)  # symbol ("" = module) -> dotted
    dynamic: bool = False  # importlib/__import__: impact analysis must assume anything
    aliases: dict[str, str] = field(default_factory=dict)  # final direct import bindings
    star_imports: list[str] = field(default_factory=list)
    exports: list[str] | None = None  # None: no __all__; [] also covers unknown dynamic __all__

    @classmethod
    def load(cls, body: str) -> "FileIndex":
        data = json.loads(body)
        data["symbols"] = [Symbol(**item) for item in data["symbols"]]
        return cls(**data)


@dataclass(frozen=True)
class Coupling:
    a: str
    b: str
    together: int  # commits that changed both
    weight: float  # together / changes of the less frequently changed file


def language_of(path: str) -> str:
    return LANGUAGES.get(PurePosixPath(path).suffix, "")


def python_module(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


@cache
def grammar(name: str) -> "Language | None":
    if not TREE_SITTER or name not in GRAMMARS:
        return None
    module, attribute = GRAMMARS[name]
    try:
        return Language(getattr(importlib.import_module(module), attribute)())
    except (ImportError, AttributeError, ValueError, TypeError):
        return None


def backend() -> str:
    """What this installation can index, for `orch doctor` and `orch index`."""
    names = [name for name in GRAMMARS if grammar(name) is not None]
    if not names:
        return "ast (Python only; tree-sitter not installed)"
    return f"ast + tree-sitter ({', '.join(names)})"


# --- Python: syntax and semantics from ast ------------------------------------------
def _absolute(module: str | None, level: int, package: str) -> str | None:
    if not level:
        return module
    parts = package.split(".") if package else []
    if level - 1 > len(parts):
        return None
    base = parts[: len(parts) - (level - 1)]
    return ".".join([*base, *([module] if module else [])]) or None


def _dotted(node: ast.expr) -> str | None:
    names: list[str] = []
    while isinstance(node, ast.Attribute):
        names.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([node.id, *reversed(names)])


def _bind(node: ast.Import | ast.ImportFrom, package: str, into: dict[str, str]) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.asname:
                into[alias.asname] = alias.name
            else:
                top = alias.name.split(".")[0]
                into[top] = top
        return
    base = _absolute(node.module, node.level, package)
    if base is None:
        return
    for alias in node.names:
        if alias.name != "*":
            into[alias.asname or alias.name] = f"{base}.{alias.name}"


def _uses(nodes: Iterable[ast.AST], names: dict[str, str]) -> list[str]:
    """Dotted targets of every resolvable name or attribute chain loaded in `nodes`."""
    found: set[str] = set()
    stack = list(nodes)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
            dotted = _dotted(node)
            if dotted is not None:
                head, _, rest = dotted.partition(".")
                if head in names:
                    found.add(f"{names[head]}.{rest}" if rest else names[head])
                continue
        stack.extend(ast.iter_child_nodes(node))
    return sorted(found)




def _body_line(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> int:
    body = node.body[1:] if ast.get_docstring(node) and node.body[1:] else node.body
    return body[0].lineno


def _python(path: str, source: str) -> FileIndex | None:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    module = python_module(path)
    package = module if path.endswith("__init__.py") else module.rpartition(".")[0]
    imports: set[str] = set()
    dynamic = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _absolute(node.module, node.level, package)
            if base is not None:
                imports.add(base)
                imports.update(f"{base}.{a.name}" for a in node.names if a.name != "*")
        elif isinstance(node, ast.Call):
            target = _dotted(node.func) or ""
            dynamic = dynamic or target in {"__import__", "importlib.import_module",
                                            "import_module"}
    names: dict[str, str] = {}
    aliases: dict[str, str] = {}
    star_imports: list[str] = []
    exports: list[str] | None = None
    for statement in tree.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            _bind(statement, package, names)
            _bind(statement, package, aliases)
            if (isinstance(statement, ast.ImportFrom)
                    and any(a.name == "*" for a in statement.names)):
                base = _absolute(statement.module, statement.level, package)
                if base is not None:
                    star_imports.append(base)
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            aliases.pop(statement.name, None)
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
            expression = statement.value
            if expression is None:
                continue
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for binding_target in targets:
                for name in ast.walk(binding_target):
                    if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store):
                        aliases[name.id] = ""  # assignments are not imported aliases
                        if name.id == "__all__":
                            try:
                                value = ast.literal_eval(expression)
                                exports = (list(value) if isinstance(value, (list, tuple))
                                           and all(isinstance(v, str) for v in value) else [])
                            except (ValueError, TypeError, SyntaxError):
                                exports = []
        elif any(isinstance(node, ast.Name) and node.id == "__all__"
                 for node in ast.walk(statement)):
            # Mutations/conditional rewrites of __all__ cannot justify a static export.
            exports = []
    # Names a module imports from others are what its code is meant to call.
    pulled = {
        names[alias.asname or alias.name]
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom)
        for alias in statement.names
        if (alias.asname or alias.name) in names
    }
    found = definitions(tree)
    for qual, _, _ in found:
        if "." not in qual:
            names[qual] = f"{module}.{qual}"
    symbols: list[Symbol] = []
    refs: dict[str, list[str]] = {}
    for qual, node, line in found:
        kind = "class" if isinstance(node, ast.ClassDef) else "method" if "." in qual else (
            "function"
        )
        symbols.append(Symbol(
            qual, kind, node.lineno, node.end_lineno or node.lineno, _body_line(node), line,
            digest(ast.dump(node))[:12],
        ))
        local = dict(names)
        for inner in ast.walk(node):
            if isinstance(inner, (ast.Import, ast.ImportFrom)):
                _bind(inner, package, local)
        if isinstance(node, ast.ClassDef):  # methods are their own symbols
            parts: list[ast.AST] = [*node.bases, *node.decorator_list] + [
                item for item in node.body
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
        else:
            parts = [*node.body, *node.decorator_list, node.args]
        used = [target for target in _uses(parts, local) if target != f"{module}.{qual}"]
        if used:
            refs[qual] = used
    top = [s for s in tree.body if not isinstance(
        s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)
    )]
    if (used := sorted({*_uses(top, names), *pulled})):
        refs[""] = used
    return FileIndex(path, "python", "ast", symbols, sorted(imports), refs, dynamic,
                     aliases, star_imports, exports)


# --- every other language (and broken Python): syntax only, from tree-sitter ---------
def _text(source: bytes, node: "Node") -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _name(node: "Node", source: bytes) -> str:
    if node.type == "impl_item":
        target = node.child_by_field_name("type")
        return _text(source, target) if target is not None else ""
    if node.type == "method_declaration" and node.child_by_field_name("receiver") is not None:
        receiver = node.child_by_field_name("receiver")
        assert receiver is not None
        owner = re.findall(r"[A-Za-z_]\w*", _text(source, receiver))
        name = node.child_by_field_name("name")
        if owner and name is not None:
            return f"{owner[-1]}.{_text(source, name)}"
    name = node.child_by_field_name("name")
    return _text(source, name) if name is not None else ""


def _header(node: "Node", source: bytes) -> str:
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    text = " ".join(source[node.start_byte : end].decode("utf-8", "replace").split())
    if body is None:
        text = text.split(" {")[0]
    return text.rstrip("{: ")[:160]


def _imported(node: "Node", source: bytes, language: str) -> list[str]:
    if language in {"javascript", "typescript", "tsx"}:
        spec = node.child_by_field_name("source")
        return [_text(source, spec).strip("'\"`")] if spec is not None else []
    if language == "go":
        return re.findall(r'"([^"]+)"', _text(source, node))
    text = _text(source, node)
    for word in ("import", "use", "from", "static", "pub"):
        text = re.sub(rf"^\s*{word}\b", "", text).strip()
    return [text.rstrip(";").strip()]


def _tree_sitter(path: str, source: bytes, language: str) -> FileIndex | None:
    lang = grammar(language)
    if lang is None:
        return None
    # tree-sitter is pinned below 0.26: 0.26.0 returned invalid field nodes for a generated
    # Go test file and corrupted the heap (orch died with SIGSEGV, polyglot-ledger N9-N11).
    parser = Parser(lang)
    tree = parser.parse(source)
    root = tree.root_node
    kinds = DEFINITIONS[language]
    symbols: list[Symbol] = []
    imports: list[str] = []

    def make(qual: str, kind: str, node: "Node") -> Symbol:
        body = node.child_by_field_name("body")
        return Symbol(
            qual, kind, node.start_point.row + 1, node.end_point.row + 1,
            (body.start_point.row + 1) if body is not None else node.start_point.row + 1,
            _header(node, source), digest(_text(source, node))[:12],
        )

    def visit(node: "Node", owner: str, depth: int) -> None:
        for child in node.named_children:
            if child.type in IMPORTS:
                imports.extend(_imported(child, source, language))
                continue
            kind = kinds.get(child.type)
            if kind is None:
                if child.type in PASSTHROUGH and (depth or child.type != "block"):
                    visit(child, owner, depth)
                elif child.type in {"lexical_declaration", "variable_declaration"} and not owner:
                    for declarator in child.named_children:
                        value = declarator.child_by_field_name("value")
                        named = declarator.child_by_field_name("name")
                        if value is not None and named is not None and value.type in {
                            "arrow_function", "function_expression", "function"
                        }:
                            symbols.append(make(_text(source, named), "function", declarator))
                continue
            name = _name(child, source)
            if not name:
                continue
            qual = f"{owner}.{name}" if owner else name
            if kind == "function" and owner:
                kind = "method"
            if kind != "impl":
                symbols.append(make(qual, kind, child))
            if child.type in CONTAINERS and depth < 3:
                visit(child, qual, depth + 1)

    visit(root, "", 0)
    del root, tree, parser  # nodes first, then the tree they point into
    return FileIndex(path, language, "tree-sitter", symbols, sorted(set(imports)), {}, False)


def index_file(path: str, raw: bytes) -> FileIndex:
    language = language_of(path)
    if not language or len(raw) > MAX_BLOB:
        return FileIndex(path, language, "none")
    if language == "python":
        found = _python(path, raw.decode("utf-8", "replace"))
        if found is not None:
            return found
    return _tree_sitter(path, raw, language) or FileIndex(path, language, "none")


# --- a whole revision ------------------------------------------------------------
class RepoIndex:
    def __init__(self, files: dict[str, FileIndex], source_roots: tuple[str, ...] = (".",)) -> None:
        self.files = files
        self.modules = {
            python_module(path): path for path, f in files.items() if f.language == "python"
        }
        for root in source_roots:
            if root == ".":
                continue
            for path, entry in files.items():
                if entry.language != "python" or not path.startswith(root + "/"):
                    continue
                module = python_module(path[len(root) + 1:])
                if not module:
                    continue
                if module in self.modules and self.modules[module] != path:
                    raise ValueError(f"Ambiguous project import {module} across source roots")
                self.modules[module] = path
        self.packages = {
            ".".join(module.split(".")[:depth])
            for module in self.modules
            for depth in range(1, module.count(".") + 1)
        }
        self._resolved: dict[str, tuple[str, str] | None] = {}
        self.deps = {path: frozenset(self._resolve_imports(f)) for path, f in files.items()}
        # What importing a file executes: its imports plus every package __init__.py on
        # the way (importing pkg.mod runs pkg/__init__.py first). Impact analysis needs
        # these; task ordering does not (an __init__ only has to import, not work).
        self.loads = {
            path: frozenset(
                {*targets, *self._inits(path), *(i for t in targets for i in self._inits(t))}
            ) - {path}
            for path, targets in self.deps.items()
        }
        self.users: dict[str, set[str]] = {path: set() for path in files}
        for path, targets in self.deps.items():
            for target in targets:
                self.users[target].add(path)
        self._reach: dict[str, frozenset[str]] = {}

    @classmethod
    def from_sources(
        cls, sources: dict[str, str], source_roots: tuple[str, ...] = (".",),
    ) -> "RepoIndex":
        return cls({
            path: index_file(path, text.encode())
            for path, text in sources.items()
            if language_of(path)
        }, source_roots)

    def resolve(self, dotted: str) -> tuple[str, str] | None:
        """Resolve explicit aliases and unambiguous star exports without executing code."""
        if dotted not in self._resolved:
            self._resolved[dotted] = self._resolve(dotted, frozenset())
        return self._resolved[dotted]

    def _resolve(
        self, dotted: str, seen: frozenset[str], *, follow_exports: bool = True,
    ) -> tuple[str, str] | None:
        if dotted in seen:
            return None
        seen = seen | {dotted}
        parts = dotted.split(".")
        for cut in range(len(parts), 0, -1):
            path = self.modules.get(".".join(parts[:cut]))
            if path is None:
                continue
            rest = parts[cut:]
            if not rest:
                return path, ""
            entry = self.files[path]
            head = rest[0]
            own = None
            if follow_exports and head in entry.aliases:
                target = entry.aliases[head]
                if not target:
                    return path, ""
                own = self._resolve(".".join([target, *rest[1:]]), seen)
            known = {symbol.name for symbol in entry.symbols}
            if head not in entry.aliases or not follow_exports:
                for size in range(len(rest), 0, -1):
                    if ".".join(rest[:size]) in known:
                        own = (path, ".".join(rest[:size]))
                        break
            if not follow_exports:
                return own or (path, "")
            candidates = {own} if own is not None else set()
            for imported in entry.star_imports:
                target_path = self.modules.get(imported)
                if target_path is None:
                    continue
                exposed = self.files[target_path].exports
                if (exposed is not None and head not in exposed
                        or exposed is None and head.startswith("_")):
                    continue
                candidate = self._resolve(".".join([imported, *rest]), seen)
                if candidate is not None and candidate[1]:
                    candidates.add(candidate)
            if len(candidates) == 1:
                return candidates.pop()
            return path, ""
        return None

    def _inits(self, path: str) -> list[str]:
        """The package __init__.py files Python runs before it can import `path`."""
        parts = PurePosixPath(path).parts[:-1]
        found = ["/".join([*parts[:depth], "__init__.py"]) for depth in range(1, len(parts) + 1)]
        return [init for init in found if init in self.files and init != path]

    def _resolve_imports(self, f: FileIndex) -> set[str]:
        found: set[str] = set()
        if f.language == "python":
            for name in f.imports:
                # Imports execute the facade; call references follow exported definitions.
                target = self._resolve(name, frozenset(), follow_exports=False)
                if target is not None and target[0] != f.path:
                    found.add(target[0])
        elif f.language in {"javascript", "typescript", "tsx"}:
            folder = PurePosixPath(f.path).parent
            for spec in f.imports:
                if not spec.startswith("."):
                    continue
                base = _normalize(str(folder / spec))
                options = [base, *(base + s for s in SCRIPT_SUFFIXES),
                           *(f"{base}/index{s}" for s in SCRIPT_SUFFIXES)]
                hit = next((option for option in options if option in self.files), None)
                if hit is not None and hit != f.path:
                    found.add(hit)
        return found


    def reach(self, path: str) -> frozenset[str]:
        """Every file `path` imports, transitively, including itself."""
        if path not in self._reach:
            seen = {path}
            queue = deque([path])
            while queue:
                for target in self.loads.get(queue.popleft(), ()):
                    if target not in seen:
                        seen.add(target)
                        queue.append(target)
            self._reach[path] = frozenset(seen)
        return self._reach[path]

    def affected(self, tests: Iterable[str], changed: Iterable[str]) -> list[str]:
        """Test ids whose module can observe a change: the merge gate's impact analysis.

        Conservative: a changed non-Python file, an unindexed test module or a dynamic
        import anywhere in a test's import closure selects that test.
        """
        changed = set(changed)
        tests = list(tests)
        if any(language_of(path) != "python" for path in changed):
            return tests
        chosen = []
        for test in tests:
            if test.startswith("cmd:"):  # a declared check: always selected
                chosen.append(test)
                continue
            path = self.modules.get(test.rsplit(".", 2)[0])
            closure = self.reach(path) if path is not None else frozenset()
            if (
                path is None
                or self.files[path].backend != "ast"
                or closure & changed
                or any(self.files[p].dynamic for p in closure)
            ):
                chosen.append(test)
        return chosen

    def referenced(
        self, roots: Iterable[tuple[str, str]], depth: int = 2
    ) -> list[tuple[str, Symbol, int]]:
        """Symbols that the root (path, symbol) pairs use, along the call graph up to
        `depth` hops: (path, symbol, hop), nearest first, then by position."""
        frontier = [root for root in roots if root[0] in self.files]
        seen: set[tuple[str, str]] = set(frontier)
        found: list[tuple[str, Symbol, int]] = []
        for hop in range(1, depth + 1):
            following: list[tuple[str, str]] = []
            for path, name in frontier:
                for dotted in self.files[path].refs.get(name, []):
                    target = self.resolve(dotted)
                    if target is None or not target[1] or target in seen:
                        continue
                    seen.add(target)
                    symbol = next(
                        s for s in self.files[target[0]].symbols if s.name == target[1]
                    )
                    found.append((target[0], symbol, hop))
                    following.append(target)
            frontier = following
        return sorted(found, key=lambda item: (item[2], item[0], item[1].line))

    def roots(self, files: Iterable[str], tests: Iterable[str]) -> list[tuple[str, str]]:
        """Call-graph roots of a task: everything in its files (imports included), plus
        its own test methods with their class and the class's non-test helpers."""
        found = [(path, name) for path in files if path in self.files
                 for name in self.files[path].refs]
        for test in tests:
            if test.startswith("cmd:") or test.count(".") < 2:
                continue  # a model-declared check (orch_verify), not a dotted unittest id
            module, owner, method = test.rsplit(".", 2)
            path = self.modules.get(module)
            if path is None:
                continue
            for name in self.files[path].refs:
                head, _, tail = name.partition(".")
                if name in {owner, f"{owner}.{method}"} or (
                    head == owner and not tail.startswith("test")
                ):
                    found.append((path, name))
        return sorted(set(found))

    def users_of(self, path: str, name: str) -> list[tuple[str, str]]:
        """(path, symbol) pairs whose code references `path`'s symbol `name`."""
        hits = []
        for other, f in sorted(self.files.items()):
            for owner, targets in sorted(f.refs.items()):
                if any(self.resolve(t) == (path, name) for t in targets):
                    hits.append((other, owner))
        return hits

    def find(self, name: str) -> list[tuple[str, Symbol]]:
        """Definitions named `name` or `Owner.name`, by path."""
        return [
            (path, symbol)
            for path, f in sorted(self.files.items())
            for symbol in f.symbols
            if symbol.name == name or symbol.name.endswith(f".{name}")
        ]

    def symbol_at(self, path: str, line: int) -> Symbol | None:
        f = self.files.get(path)
        inside = [s for s in (f.symbols if f else []) if s.line <= line <= s.end]
        return max(inside, key=lambda s: s.line) if inside else None

    def rank(self) -> dict[str, int]:
        """Importers plus references into each file: what the rest of the code leans on."""
        score = {path: len(users) for path, users in self.users.items()}
        for f in self.files.values():
            for targets in f.refs.values():
                for dotted in targets:
                    target = self.resolve(dotted)
                    if target is not None and target[0] != f.path:
                        score[target[0]] += 1
        return score

    def planning_map(self, spec: str, budget: int = 12_000) -> str:
        """Prioritize named paths/symbols and their immediate dependency neighborhood.

        This remains a bounded map, not a claim that lexical anchors capture every
        semantic dependency. No anchors fall back to the normal centrality ranking.
        """
        seeds: set[str] = set()
        for token in sorted(set(re.findall(r"[A-Za-z_][\w./:]*", spec))):
            token = token.rstrip(".")
            if token in self.files:
                seeds.add(token)
            resolved = self.resolve(token.replace("::", "."))
            if resolved is not None:
                seeds.add(resolved[0])
            elif len(token) >= 3:
                seeds.update(path for path, _ in self.find(token))
        neighbors = {p for seed in seeds for p in self.loads[seed] | self.users[seed]}
        header = ("Requirement-scoped signature map; omitted code may still be relevant.\n"
                  if seeds else "No exact code anchors found; compact global signature map.\n")
        return header + self.repo_map(max(0, budget - len(header.encode())),
                                      [*sorted(seeds), *sorted(neighbors - seeds)])

    def repo_map(self, budget: int, preferred: Iterable[str] = ()) -> str:
        """Signature-level map in path order. Over budget, the most-used files are kept
        (whole files only) and the omission is stated; the output is byte-stable."""
        blocks = {
            path: f"{path}\n" + "".join(f"  {s.signature}\n" for s in f.symbols)
            for path, f in sorted(self.files.items())
        }
        total = sum(len(block.encode()) for block in blocks.values())
        if total <= budget:
            return "".join(blocks.values())
        rank = self.rank()
        priority = {path: order for order, path in enumerate(preferred)}
        marker = f"(repo map truncated: {{}} of {len(blocks)} files omitted)\n"
        room = budget - len(marker.format(len(blocks)).encode())
        kept: set[str] = set()
        used = 0
        for path in sorted(blocks, key=lambda p: (priority.get(p, len(priority)), -rank[p], p)):
            size = len(blocks[path].encode())
            if used + size <= room:
                kept.add(path)
                used += size
        return "".join(blocks[p] for p in sorted(kept)) + marker.format(len(blocks) - len(kept))


def _normalize(path: str) -> str:
    parts: list[str] = []
    for part in path.split("/"):
        if part == "..":
            if parts:
                parts.pop()
        elif part not in {"", "."}:
            parts.append(part)
    return "/".join(parts)




def segment(source: str, symbol: Symbol) -> str:
    return "".join(source.splitlines(keepends=True)[symbol.line - 1 : symbol.end])






# --- persistence: parse each blob once, snapshot each checkpoint ----------------------
class Index:
    """Per-repository cache in `<git dir>/orch/index.sqlite`; safe across threads."""

    def __init__(self, repo: Path, path: Path) -> None:
        self.repo = repo
        self.path = path
        self.lock = threading.RLock()
        self._files: dict[tuple[str, str], FileIndex] = {}
        self.source_roots: tuple[str, ...] = (".",)
        self._revisions: dict[tuple[str, tuple[str, ...]], RepoIndex] = {}
        self._trees: dict[str, dict[str, str]] = {}
        self.parsed = 0  # blobs parsed by this process: incremental updates parse few
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = f"{INDEX_VERSION} {backend()}"
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT NOT NULL, blob TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(path, blob));
                CREATE TABLE IF NOT EXISTS snapshots (
                    revision TEXT PRIMARY KEY, label TEXT NOT NULL, entries TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cochange (revision TEXT PRIMARY KEY, body TEXT NOT NULL);
            """)
            row = db.execute("SELECT value FROM meta WHERE key='stamp'").fetchone()
            if row is None or row[0] != stamp:
                # A new parser or grammar set: cached entries would disagree with it.
                db.execute("DELETE FROM files")
                db.execute("INSERT OR REPLACE INTO meta VALUES('stamp', ?)", (stamp,))

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def tree(self, revision: str) -> dict[str, str]:
        """path -> blob id of every file at `revision` (submodules excluded)."""
        with self.lock:
            if revision in self._trees:
                return self._trees[revision]
        raw = Workspace.git_bytes(self.repo, "ls-tree", "-r", "-z", "--full-tree", revision)
        entries: dict[str, str] = {}
        for item in raw.split(b"\0"):
            if not item:
                continue
            meta, _, name = item.partition(b"\t")
            _, kind, blob = meta.decode().split()
            if kind == "blob":
                entries[name.decode("utf-8", "surrogateescape")] = blob
        with self.lock:
            if len(self._trees) > 64:
                self._trees.clear()
            self._trees[revision] = entries
        return entries

    def blobs(self, ids: Iterable[str]) -> dict[str, bytes]:
        wanted = sorted(set(ids))
        if not wanted:
            return {}
        out = Workspace.git_bytes(
            self.repo, "cat-file", "--batch", stdin=("\n".join(wanted) + "\n").encode()
        )
        found: dict[str, bytes] = {}
        position = 0
        while position < len(out):
            newline = out.index(b"\n", position)
            header = out[position:newline].decode().split()
            position = newline + 1
            if len(header) != 3:
                continue  # "<id> missing"
            size = int(header[2])
            found[header[0]] = out[position : position + size]
            position += size + 1
        return found

    def read(self, revision: str, paths: Iterable[str] | None = None,
             suffix: str | tuple[str, ...] = "") -> dict[str, str]:
        """File contents at `revision` in one batch: named paths, or every path ending
        in `suffix`. Missing paths are left out."""
        tree = self.tree(revision)
        names = [p for p in (tree if paths is None else paths) if p in tree and p.endswith(suffix)]
        raw = self.blobs(tree[name] for name in names)
        return {name: raw[tree[name]].decode("utf-8", "replace") for name in sorted(names)}

    def at(self, revision: str) -> RepoIndex:
        """The index of one revision; only blobs never seen before are parsed."""
        with self.lock:
            key = revision, self.source_roots
            if key in self._revisions:
                return self._revisions[key]
        tree = self.tree(revision)
        code = {path: blob for path, blob in tree.items() if language_of(path)}
        # Files without a parser (any other language, configs, SQL, scripts) are still part
        # of the map, listed by path, so planners see the whole project.
        listed = {path: FileIndex(path, "", "none") for path in tree if path not in code}
        with self.lock:
            unknown = [(p, b) for p, b in code.items() if (p, b) not in self._files]
        if unknown:
            stored = self._load(unknown)
            fresh = [(p, b) for p, b in unknown if (p, b) not in stored]
            raw = self.blobs(blob for _, blob in fresh)
            parsed = {(p, b): index_file(p, raw.get(b, b"")) for p, b in fresh}
            if parsed:
                with self._db() as db:
                    db.executemany(
                        "INSERT OR REPLACE INTO files VALUES(?,?,?)",
                        [(p, b, dumps(asdict(f))) for (p, b), f in parsed.items()],
                    )
            with self.lock:
                self._files.update(stored)
                self._files.update(parsed)
                self.parsed += len(parsed)
        with self.lock:
            built = RepoIndex({**listed, **{p: self._files[(p, b)] for p, b in code.items()}},
                              key[1])
            built.files = dict(sorted(built.files.items()))
            if len(self._revisions) > 16:
                self._revisions.clear()
            self._revisions[key] = built
            return built

    def _load(self, keys: list[tuple[str, str]]) -> dict[tuple[str, str], FileIndex]:
        found: dict[tuple[str, str], FileIndex] = {}
        with self._db() as db:
            for start in range(0, len(keys), 300):
                chunk = keys[start : start + 300]
                marks = ",".join("(?,?)" for _ in chunk)
                rows = db.execute(
                    f"SELECT path, blob, body FROM files WHERE (path, blob) IN (VALUES {marks})",
                    [value for key in chunk for value in key],
                ).fetchall()
                for path, blob, body in rows:
                    found[(path, blob)] = FileIndex.load(body)
        return found

    def snapshot(self, revision: str, label: str) -> int:
        """Freeze the index of a checkpoint (the same moment the repo map freezes)."""
        index = self.at(revision)
        tree = self.tree(revision)
        entries = sorted((path, tree[path]) for path in index.files)
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO snapshots VALUES(?,?,?)",
                (revision, label, dumps(entries)),
            )
        return len(entries)

    def snapshots(self) -> list[tuple[str, str, int]]:
        with self._db() as db:
            rows = db.execute("SELECT revision, label, entries FROM snapshots").fetchall()
        return sorted((rev, label, len(json.loads(body))) for rev, label, body in rows)

    def cochange(self, revision: str, limit: int = 1000, widest: int = 30) -> list[Coupling]:
        """Files that change together in git history: zero tokens, cached per revision.

        Commits touching more than `widest` files (renames, formatting) are ignored.
        """
        with self._db() as db:
            row = db.execute("SELECT body FROM cochange WHERE revision=?", (revision,)).fetchone()
        if row is not None:
            return [Coupling(**item) for item in json.loads(row[0])]
        log = Workspace.git(
            self.repo, "log", "--no-merges", f"-n{limit}", "--name-only", "--format=%x1e",
            revision, timeout=120,
        )
        changes: Counter[str] = Counter()
        pairs: Counter[tuple[str, str]] = Counter()
        for chunk in log.split("\x1e"):
            files = sorted({line.strip() for line in chunk.splitlines() if line.strip()})
            if not files or len(files) > widest:
                continue
            changes.update(files)
            pairs.update((a, b) for i, a in enumerate(files) for b in files[i + 1 :])
        found = sorted(
            (
                Coupling(a, b, n, round(n / min(changes[a], changes[b]), 3))
                for (a, b), n in pairs.items()
                if n >= 2
            ),
            key=lambda c: (-c.weight, -c.together, c.a, c.b),
        )
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO cochange VALUES(?,?)",
                (revision, dumps([asdict(c) for c in found])),
            )
        return found


