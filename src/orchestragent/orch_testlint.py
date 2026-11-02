"""Static checks for generated frozen-test scaffolds; no source execution."""

import ast
import symtable

UNKNOWN = object()


def _literal(node: ast.AST, shadowed: set[str]) -> object:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, RecursionError):
        pass
    if isinstance(node, ast.IfExp):
        condition = _literal(node.test, shadowed)
        if condition is not UNKNOWN:
            return _literal(node.body if condition else node.orelse, shadowed)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "len" and "len" not in shadowed
            and len(node.args) == 1 and not node.keywords):
        value = _literal(node.args[0], shadowed)
        if value is not UNKNOWN:
            try:
                return len(value)  # type: ignore[arg-type]
            except TypeError:
                pass
    return UNKNOWN


def fixture_errors(source: str) -> list[str]:
    """Check test bindings, explicit loop captures and constant-only assertion padding.

    Python's symbol table supplies scope binding, including comprehension scopes;
    this does not attempt to emulate Python or prove a test oracle's correctness.
    Immediate lambda calls are safe. Other callbacks created in loops must bind
    loop values explicitly, even when a caller might happen to run synchronously.
    """
    tree = ast.parse(source)
    root = symtable.symtable(source, "<frozen tests>", "exec")
    scopes: dict[tuple[int, str], list[symtable.SymbolTable]] = {}
    pending = list(root.get_children())
    while pending:
        table = pending.pop()
        scopes.setdefault((table.get_lineno(), table.get_name()), []).append(table)
        pending.extend(table.get_children())
    globals_bound = {symbol.get_name() for symbol in root.get_symbols()
                     if symbol.is_assigned() or symbol.is_imported()}
    errors: list[str] = []

    class Fixtures(ast.NodeVisitor):
        loops: set[str] = set()
        shadowed: set[str] = globals_bound

        def namespace(self, node: ast.Module | ast.ClassDef) -> None:
            # Only unconditional direct bindings: do not guess which runtime branch wins.
            protected = ({"setUpModule", "tearDownModule", "load_tests"}
                         if isinstance(node, ast.Module) else
                         {"setUp", "tearDown", "setUpClass", "tearDownClass", "asyncSetUp",
                          "asyncTearDown"})
            protected.update(item.name for item in node.body if isinstance(item, ast.ClassDef)
                             and any(isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                                     and method.name.startswith("test") for method in item.body))
            seen: dict[str, int] = {}
            for item in node.body:
                names: list[str] = []
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names = [item.name]
                elif isinstance(item, (ast.Import, ast.ImportFrom)):
                    names = [alias.asname or alias.name.split(".")[0] for alias in item.names]
                elif isinstance(item, (ast.Assign, ast.Delete, ast.AnnAssign, ast.AugAssign)):
                    targets = (item.targets if isinstance(item, (ast.Assign, ast.Delete)) else
                               [] if isinstance(item, ast.AnnAssign) and item.value is None else
                               [item.target])
                    names = [name.id for target in targets for name in ast.walk(target)
                             if isinstance(name, ast.Name)
                             and isinstance(name.ctx, (ast.Store, ast.Del))]
                for name in names:
                    if name in seen and (name.startswith("test") or name in protected):
                        errors.append(f"line {item.lineno}: test namespace binding {name!r} "
                                      f"replaces line {seen[name]}; give checks and fixtures "
                                      "distinct names instead of overwriting them")
                    seen[name] = item.lineno
            self.generic_visit(node)

        def visit_Module(self, node: ast.Module) -> None:
            self.namespace(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.namespace(node)

        def function(
            self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
            *, deferred: bool = True,
        ) -> None:
            defaults = [*node.args.defaults, *(n for n in node.args.kw_defaults if n is not None)]
            decorators = node.decorator_list if not isinstance(node, ast.Lambda) else []
            for expression in [*decorators, *defaults]:
                self.visit(expression)
            arguments = {arg.arg for arg in [*node.args.posonlyargs, *node.args.args,
                                             *node.args.kwonlyargs]}
            if node.args.vararg:
                arguments.add(node.args.vararg.arg)
            if node.args.kwarg:
                arguments.add(node.args.kwarg.arg)
            name = "lambda" if isinstance(node, ast.Lambda) else node.name
            matches = [table for table in scopes.get((node.lineno, name), [])
                       if {s.get_name() for s in table.get_symbols() if s.is_parameter()}
                       == arguments]
            local = set().union(*({s.get_name() for s in table.get_symbols() if s.is_local()}
                                  for table in matches))
            captured = self.loops - arguments
            for table in matches:
                captured &= {s.get_name() for s in table.get_symbols()
                             if s.is_free() or s.is_global()}
            if not matches:
                captured.clear()  # ambiguous scope information cannot justify a rejection
            if deferred and captured:
                errors.append(f"line {node.lineno}: callback implicitly captures loop variables "
                              f"{sorted(captured)}; bind their values in default arguments")
            loops, shadowed = self.loops, self.shadowed
            self.loops, self.shadowed = loops - local, shadowed | local
            body = [node.body] if isinstance(node, ast.Lambda) else node.body
            for statement in body:
                self.visit(statement)
            self.loops, self.shadowed = loops, shadowed

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.function(node)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            self.function(node)

        def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
            self.visit(node.iter)
            loops = self.loops
            self.loops = loops | {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}
            for statement in node.body:
                self.visit(statement)
            self.loops = loops
            for statement in node.orelse:
                self.visit(statement)

        def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
            self.visit_For(node)

        def comprehension(
            self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
        ) -> None:
            loops, shadowed = self.loops, self.shadowed
            for generator in node.generators:
                self.visit(generator.iter)
                targets = {n.id for n in ast.walk(generator.target) if isinstance(n, ast.Name)}
                self.loops = self.loops | targets
                self.shadowed = self.shadowed | targets
                for condition in generator.ifs:
                    self.visit(condition)
            if isinstance(node, ast.DictComp):
                self.visit(node.key)
                self.visit(node.value)
            else:
                self.visit(node.elt)
            self.loops, self.shadowed = loops, shadowed

        def visit_ListComp(self, node: ast.ListComp) -> None:
            self.comprehension(node)

        def visit_SetComp(self, node: ast.SetComp) -> None:
            self.comprehension(node)

        def visit_DictComp(self, node: ast.DictComp) -> None:
            self.comprehension(node)

        def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
            self.comprehension(node)

        def visit_Call(self, node: ast.Call) -> None:
            if (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"):
                comparisons = {"assertEqual", "assertNotEqual", "assertIs", "assertIsNot",
                               "assertIn", "assertNotIn", "assertLess", "assertLessEqual",
                               "assertGreater", "assertGreaterEqual"}
                unary = {"assertTrue", "assertFalse", "assertIsNone", "assertIsNotNone"}
                required = (2 if node.func.attr in comparisons else
                            1 if node.func.attr in unary else 0)
                if required and len(node.args) >= required and all(
                    _literal(arg, self.shadowed) is not UNKNOWN for arg in node.args[:required]
                ):
                    errors.append(f"line {node.lineno}: constant-only assertion does not "
                                  "exercise a fixture or the implementation")
            if isinstance(node.func, ast.Lambda):
                self.function(node.func, deferred=False)
                for expression in [*node.args, *(kw.value for kw in node.keywords)]:
                    self.visit(expression)
            else:
                self.generic_visit(node)

        def visit_Assert(self, node: ast.Assert) -> None:
            values = ([node.test.left, *node.test.comparators]
                      if isinstance(node.test, ast.Compare) else [node.test])
            if all(_literal(value, self.shadowed) is not UNKNOWN for value in values):
                errors.append(f"line {node.lineno}: constant-only assertion does not "
                              "exercise a fixture or the implementation")
            self.generic_visit(node)

    Fixtures().visit(tree)
    return errors
