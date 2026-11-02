"""Conservative Python excerpts for prompt context, never replacement file content."""

import ast

from orchestragent.codeindex import Definition, definitions

_DYNAMIC = {
    "__import__", "import_module", "importlib", "inspect", "eval", "exec", "globals",
    "locals", "vars", "dir", "getattr", "setattr", "delattr", "__dict__", "__getattr__",
    "__getattribute__", "getmembers", "getfixturevalue", "__subclasses__",
}
_SETUP = {"setUpModule", "tearDownModule", "setup_module", "teardown_module",
          "setup_function", "teardown_function", "load_tests"}


def definition_view(path: str, source: str, name: str) -> str:
    """An exact named definition, explicitly not a transitive dependency excerpt."""
    nodes = [node for qual, node, _ in definitions(ast.parse(source)) if qual == name]
    if len(nodes) != 1:
        raise ValueError(f"Ambiguous or missing definition: {path}::{name}")
    node = nodes[0]
    start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
    text = "".join(source.splitlines(keepends=True)[start - 1:node.end_lineno])
    return (f"# PARTIAL CONTEXT: {path}::{name}, lines {start}-{node.end_lineno}.\n"
            "# Exact definition with decorators; surrounding class/module state and helpers "
            "are omitted. Request further path.py::Symbol reads if needed.\n" + text)


def excerpt(path: str, source: str, names: set[str]) -> str:
    """Keep requested definitions and conservative local support, preserving their bytes.

    Class requests retain the entire class. Method requests retain its class state and
    all non-test methods; unrelated test methods can be omitted. Imports, module state,
    fixtures and lifecycle hooks remain. Reflection, ambiguous definitions and syntax
    outside this small static model fall back to the original source. This function
    does not produce executable code or claim a complete Python dependency analysis.
    """
    if not names or not path.endswith(".py"):
        return source
    try:
        tree = ast.parse(source)
        found = definitions(tree)
    except (SyntaxError, ValueError, RecursionError):
        return source
    nodes = {name: node for name, node, _ in found}
    if len(nodes) != len(found) or not names <= nodes.keys():
        return source
    fixture_names = {"fixture", "yield_fixture"}
    for node in ast.walk(tree):
        annotation = (node.annotation if isinstance(node, (ast.arg, ast.AnnAssign)) else
                      node.returns if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                      else None)
        if annotation is not None and any(
            isinstance(part, ast.Constant) and isinstance(part.value, str)
            for part in ast.walk(annotation)
        ):
            return source
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if (isinstance(node, ast.ImportFrom) and node.module
                    and node.module.split(".")[0] in _DYNAMIC):
                return source
            for alias in node.names:
                if alias.name == "*" or set(alias.name.split(".")) & _DYNAMIC:
                    return source
                if alias.name in fixture_names:
                    fixture_names.add(alias.asname or alias.name)
        if (
            isinstance(node, ast.Name) and node.id in _DYNAMIC
            or isinstance(node, ast.Attribute) and node.attr in _DYNAMIC
            or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in _DYNAMIC
        ):
            return source
    # Conditional definitions/registration are beyond this deliberately small slicer.
    if any(not isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign,
                                 ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
           and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
           for node in tree.body):
        return source

    keep = set(names)
    whole = {name for name in names if isinstance(nodes[name], ast.ClassDef)}
    support: list[ast.AST] = [node for node in tree.body if not isinstance(node, Definition)]
    for name, node, _ in found:
        if (name in _SETUP or (node.decorator_list and not node.name.startswith("test"))) or any(
            isinstance(part, ast.Name) and part.id in fixture_names
            or isinstance(part, ast.Attribute) and part.attr in fixture_names
            for decorator in node.decorator_list for part in ast.walk(decorator)
        ):
            keep.add(name)

    scanned: set[str] = set()
    while support or keep - scanned:
        for name in sorted(keep - scanned):
            node = nodes[name]
            scanned.add(name)
            if isinstance(node, ast.ClassDef):
                methods = {qual for qual in nodes if qual.startswith(name + ".")}
                keep.update(methods if name in whole else {
                    qual for qual in methods if not qual.split(".")[-1].startswith("test")
                })
                support.extend([
                    *node.bases, *node.decorator_list, *node.keywords, *node.type_params,
                ])
                support.extend(part for part in node.body if not isinstance(part, Definition))
                # Nested classes may carry inherited or registered behavior.
                if any(isinstance(part, ast.ClassDef) for part in node.body):
                    return source
            else:
                support.append(node)
                if "." in name:
                    keep.add(name.split(".")[0])
        pending, support = support, []
        for root in pending:
            for node in ast.walk(root):
                if isinstance(node, ast.Name) and node.id in nodes:
                    keep.add(node.id)
                    if isinstance(nodes[node.id], ast.ClassDef) and node.id not in whole:
                        whole.add(node.id)
                        scanned.discard(node.id)
                elif isinstance(node, ast.Attribute):
                    # Receiver types are unknown: retaining all matching local methods
                    # is safer than guessing which class owns a helper call.
                    keep.update(name for name in nodes if name.endswith("." + node.attr))

    omitted: set[int] = set()
    for name, node, _ in found:
        if name not in keep:
            start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            omitted.update(range(start - 1, node.end_lineno or node.lineno))
    if not omitted:
        return source
    lines = source.splitlines(keepends=True)
    return (
        "# PARTIAL CONTEXT: omitted unrelated definitions; not a replacement file.\n"
        + "".join(line for index, line in enumerate(lines) if index not in omitted)
    )
