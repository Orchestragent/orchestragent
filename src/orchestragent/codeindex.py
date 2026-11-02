"""Static code index for a real project tree.

Only names and signatures are indexed; prompts always receive the bytes on disk,
verified by hash, so a stale index can miss a file but never feed a wrong one.
"""

import ast
from dataclasses import dataclass

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




