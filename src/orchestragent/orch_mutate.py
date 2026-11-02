"""AST mutation testing: would the frozen tests notice if the code were wrong?

Each mutant changes exactly one operator, constant, condition, return value or loop
jump inside the functions a task wrote, by splicing source text (everything else
stays byte-identical). The task's frozen tests run against it in the sandbox; a mutant
they do not kill marks behaviour no test pins down. Scores feed the run report, the
rule A/B (passes on weak tests are not evidence), planner lessons and `orch harden`.
"""

import ast
import math
from collections.abc import Iterator
from dataclasses import dataclass, field

from orchestragent.codeindex import definitions
from orchestragent.envelope import digest
from orchestragent.live import TEXT, object_schema
from orchestragent.native import ContextPrompt
from orchestragent.orch_build import SPEC_AUTHORITY, render_files, spec_context
from orchestragent.orch_index import is_stub
from orchestragent.orch_state import OrchTask
from orchestragent.serialization import dumps

WEAK_TESTS = 0.6  # below this score a task's tests do not count as A/B evidence
MAX_SURVIVORS = 12  # shown in events, reports and the harden prompt
ARITHMETIC: dict[type[ast.operator], str] = {
    ast.Add: "-", ast.Sub: "+", ast.Mult: "/", ast.Div: "*", ast.FloorDiv: "/",
    ast.Mod: "//", ast.Pow: "*",
}
RELATIONAL: dict[type[ast.cmpop], str] = {
    ast.Lt: "<=", ast.LtE: "<", ast.Gt: ">=", ast.GtE: ">", ast.Eq: "!=", ast.NotEq: "==",
    ast.In: "not in", ast.NotIn: "in", ast.Is: "is not", ast.IsNot: "is",
}
AUGMENTED: dict[type[ast.operator], str] = {
    ast.Add: "-=", ast.Sub: "+=", ast.Mult: "/=", ast.Div: "*=",
}
Located = ast.expr | ast.stmt
# What a surviving mutant of each operator says the tests are missing.
GAPS = {
    "AOR": "arithmetic results are not asserted exactly",
    "ROR": "boundaries are untested: no case sits exactly on a comparison's limit",
    "LCR": "compound conditions are untested: no case makes only one side true",
    "UOI": "negations and signs are untested",
    "CRP": "constants are untested: an off-by-one or changed literal goes unnoticed",
    "RET": "return values are not asserted",
    "COND": "a branch is never exercised both ways",
    "AUG": "accumulation is untested",
    "JMP": "loop exits are untested",
}


# The planning rule each kind of survivor teaches: weak tests are the planner's gap.
PLANNER_LESSONS = {
    "AOR": "Assert exact arithmetic results on inputs other than 0 and 1.",
    "ROR": "For every comparison in a contract, test the limit itself and one value on "
           "each side of it.",
    "LCR": "Test each side of a compound condition with a case where only that side holds.",
    "UOI": "Test negated and negative cases explicitly.",
    "CRP": "Assert exact values, not just truthiness or type, so a changed constant fails.",
    "RET": "Assert the return value of every function a test calls.",
    "COND": "Cover both branches of every condition the contract names.",
    "AUG": "Test accumulation over several elements, not just one.",
    "JMP": "Test loops that must stop early as well as loops that must run to the end.",
}


@dataclass(frozen=True)
class Mutant:
    path: str
    symbol: str  # enclosing function or method
    line: int
    col: int
    operator: str  # a GAPS key
    before: str
    after: str
    source: str = field(repr=False)  # the whole mutated file

    @property
    def key(self) -> str:
        return f"{self.path}:{self.line}:{self.col}:{self.operator}:{self.after}"

    def brief(self) -> dict[str, object]:
        return {"key": self.key, "path": self.path, "symbol": self.symbol, "line": self.line,
                "operator": self.operator, "before": self.before[:120],
                "after": self.after[:120]}


@dataclass(frozen=True)
class Report:
    task: str
    total: int
    killed: int
    timeouts: int
    survivors: list[dict[str, object]]

    @property
    def score(self) -> float:
        return self.killed / self.total if self.total else 1.0

    def operators(self) -> list[str]:
        """Survivor operators, most frequent first."""
        counts: dict[str, int] = {}
        for item in self.survivors:
            counts[str(item["operator"])] = counts.get(str(item["operator"]), 0) + 1
        return [op for op, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]

    def gaps(self) -> list[str]:
        """Distinct test gaps the survivors point to, most frequent first."""
        return [GAPS[op] for op in self.operators()]


def _span(node: Located) -> tuple[int, int, int, int]:
    end_line = node.end_lineno if node.end_lineno is not None else node.lineno
    end_col = node.end_col_offset if node.end_col_offset is not None else node.col_offset
    return node.lineno - 1, node.col_offset, end_line - 1, end_col


def _splice(lines: list[bytes], node: Located, text: str) -> str:
    """Replace `node`'s exact source span (ast offsets are UTF-8 byte columns)."""
    first, start, last, end = _span(node)
    head, tail = lines[first][:start], lines[last][end:]
    return b"".join([*lines[:first], head, text.encode(), tail, *lines[last + 1 :]]).decode()


def _segment(lines: list[bytes], node: Located) -> str:
    first, start, last, end = _span(node)
    if first == last:
        return lines[first][start:end].decode()
    return b"".join([lines[first][start:], *lines[first + 1 : last], lines[last][:end]]).decode()


def _constant(value: object) -> str | None:
    if isinstance(value, bool):
        return repr(not value)
    if isinstance(value, int):
        return repr(1 if value == 0 else 0 if value == 1 else value + 1)
    if isinstance(value, float) and math.isfinite(value):
        return repr(value + 1.0)
    if isinstance(value, str):
        return repr("" if value else "mutant")
    return None


def _skipped(function: ast.AST) -> set[int]:
    """Nodes never mutated: docstrings, annotations, decorators and f-string parts."""
    skip: set[int] = set()
    for node in ast.walk(function):
        hidden: list[ast.AST] = []
        body = getattr(node, "body", None)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and body and (
            isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
        ):
            hidden.append(body[0].value)  # docstrings, nested ones too: no test reads them
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            hidden += [*node.decorator_list, *([node.returns] if node.returns else [])]
            hidden += [arg.annotation for arg in [*node.args.args, *node.args.kwonlyargs,
                                                 *node.args.posonlyargs] if arg.annotation]
        elif isinstance(node, ast.AnnAssign):
            hidden.append(node.annotation)
        elif isinstance(node, ast.JoinedStr):
            hidden.append(node)
        for item in hidden:
            skip.update(id(inner) for inner in ast.walk(item))
    return skip


def _sites(function: ast.AST, lines: list[bytes]) -> Iterator[tuple[str, Located, str]]:
    """(operator, node to replace, replacement text) for one function, in source order."""
    skip = _skipped(function)

    def seg(node: Located) -> str:
        return _segment(lines, node)

    for node in ast.walk(function):
        if id(node) in skip or not isinstance(node, (ast.expr, ast.stmt)):
            continue
        if isinstance(node, ast.BinOp) and type(node.op) in ARITHMETIC:
            # Outer parentheses keep the swapped operator's precedence local: `c / a ** b`
            # becomes `c / ((a) * (b))`, the mutant the report names.
            operator = ARITHMETIC[type(node.op)]
            yield "AOR", node, f"(({seg(node.left)}) {operator} ({seg(node.right)}))"
        elif isinstance(node, ast.Compare) and len(node.ops) == 1:
            swapped = RELATIONAL.get(type(node.ops[0]))
            if swapped:
                yield "ROR", node, f"({seg(node.left)}) {swapped} ({seg(node.comparators[0])})"
        elif isinstance(node, ast.BoolOp):
            joiner = " or " if isinstance(node.op, ast.And) else " and "
            yield "LCR", node, joiner.join(f"({seg(value)})" for value in node.values)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Not, ast.USub)):
            yield "UOI", node, f"({seg(node.operand)})"
        elif isinstance(node, ast.Constant):
            text = _constant(node.value)
            if text is not None:
                yield "CRP", node, text
        elif isinstance(node, ast.Return) and node.value is not None and not (
            isinstance(node.value, ast.Constant) and node.value.value is None
        ):
            yield "RET", node, "return None"
        elif isinstance(node, (ast.If, ast.While)):
            yield "COND", node.test, f"not ({seg(node.test)})"
        elif isinstance(node, ast.AugAssign) and type(node.op) in AUGMENTED:
            yield "AUG", node, f"{seg(node.target)} {AUGMENTED[type(node.op)]} {seg(node.value)}"
        elif isinstance(node, ast.Break):
            yield "JMP", node, "continue"
        elif isinstance(node, ast.Continue):
            yield "JMP", node, "break"


def mutants(path: str, source: str) -> list[Mutant]:
    """Every first-order mutant of the implemented (non-stub) functions in a file."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    original = ast.dump(tree)
    lines = source.encode().splitlines(keepends=True)
    found: dict[str, Mutant] = {}
    for qual, node, _ in definitions(tree):
        if isinstance(node, ast.ClassDef) or is_stub(node):
            continue
        for operator, target, text in _sites(node, lines):
            mutated = _splice(lines, target, text)
            try:
                if ast.dump(ast.parse(mutated)) == original:
                    continue
            except SyntaxError:
                continue
            mutant = Mutant(
                path, qual, target.lineno, target.col_offset, operator,
                _segment(lines, target), text, mutated,
            )
            found.setdefault(mutant.source, mutant)
    return sorted(found.values(), key=lambda m: (m.line, m.col, m.operator, m.after))


def select(found: list[Mutant], cap: int, seed: str) -> list[Mutant]:
    """At most `cap` mutants, spread round-robin over functions, stable for a seed."""
    groups: dict[str, list[Mutant]] = {}
    for mutant in found:
        groups.setdefault(f"{mutant.path}::{mutant.symbol}", []).append(mutant)
    queues = [
        sorted(group, key=lambda m: digest(f"{seed}/{m.key}"))
        for _, group in sorted(groups.items())
    ]
    chosen: list[Mutant] = []
    while len(chosen) < cap and any(queues):
        for queue in queues:
            if queue and len(chosen) < cap:
                chosen.append(queue.pop(0))
    return sorted(chosen, key=lambda m: (m.path, m.line, m.col, m.operator))


# --- harden: Claude adds tests that kill the survivors --------------------------------
HARDEN_SCHEMA = object_schema(
    {
        "summary": TEXT,
        "edits": {
            "type": "array",
            "items": object_schema({"path": TEXT, "search": TEXT, "replace": TEXT}),
        },
    }
)


def harden_prompt(
    task: OrchTask, requirements: dict[str, str], tests: dict[str, str],
    code: dict[str, str], survivors: list[dict[str, object]], *, original_spec: str = "",
) -> str:
    brief = {"task": task.id, "goal": task.goal, "contract": task.contract,
             "requirements": {rid: requirements.get(rid, "") for rid in task.reqs}}
    return ContextPrompt(
        SPEC_AUTHORITY + spec_context(original_spec)
        + "Independently strengthen this task's frozen tests. Mutation testing changed its "
        "implemented "
        "code in small ways and every mutant below still PASSES all tests, so the tests do "
        "not pin that behaviour down. Only assert behaviour supported by the original "
        "specification; the current implementation is not an oracle. Do not pin an existing "
        "implementation bug or unspecified incidental behaviour into new tests.\n"
        "Add NEW test methods to the test files below; never change or delete an existing "
        "test. Each new test must pass on the current code and fail on at least one listed "
        "mutant. Name every new method test_r<N>_<what> for one of the task's requirements "
        f"({', '.join(task.reqs)}). Skip a mutant that no input can tell apart from the "
        "code. Standard library and unittest only. Return JSON only: search/replace edits "
        "to the test files.\n"
        f"\n## TASK\n{dumps(brief)}\n"
        + render_files("IMPLEMENTED CODE", code)
        + render_files("FROZEN TESTS (append new methods)", tests),
        f"\n## SURVIVING MUTANTS\n{dumps(survivors)}\n",
    )
