"""Builder contract: a byte-stable context pack in, search/replace edits out.

Models never touch the filesystem; `apply_edits` is the only writer and it accepts
edits to owned files only. Error signatures are hashes of normalized failures so the
failure ladder can tell "same error again" from progress.
"""

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import cast

from orchestragent.codeindex import Definition, definitions, signatures
from orchestragent.envelope import digest
from orchestragent.live import TEXT, object_schema
from orchestragent.live_workspace import safe_relative
from orchestragent.orch_index import is_stub
from orchestragent.orch_state import OrchTask
from orchestragent.serialization import dumps

SIGNALS = ("DONE", "BLOCKED", "TEST_DISPUTE", "NEED_FILES")
EDIT_SCHEMA = object_schema(
    {
        "status": {"type": "string", "enum": list(SIGNALS)},
        "summary": TEXT,
        "reason": TEXT,
        "need_files": {"type": "array", "items": TEXT},
        "edits": {
            "type": "array",
            "items": object_schema({"path": TEXT, "search": TEXT, "replace": TEXT}),
        },
    }
)
# Claude's patch also teaches: a lesson that may become a builder rule (orch_rules).
PATCH_SCHEMA = object_schema(
    {**cast(dict[str, object], EDIT_SCHEMA["properties"]), "lesson": TEXT}
)
# Layer 1: never changes, so every builder request shares this prefix byte for byte.
SPEC_AUTHORITY = (
    "The ORIGINAL SPECIFICATION is authoritative. Derived requirements, contracts and "
    "generated tests may contain mistakes; never change required behaviour just to pass a "
    "contradictory test.\n"
)
BUILDER_INSTRUCTIONS = (
    "You implement exactly one task of a larger build. " + SPEC_AUTHORITY
    + "Rules:\n"
    "- Edit ONLY the files listed under OWNED FILES. Tests and every other file are frozen.\n"
    "- Keep every signature from the interface stubs exactly; replace the stub bodies.\n"
    "- Use stdlib and explicitly listed pinned dependencies only. "
    "No network, subprocesses or file access at import.\n"
    "- Never special-case test inputs or hard-code expected outputs.\n"
    "- Answer with search/replace edits: `search` is an exact, unique snippet of the "
    "current file and `replace` its new text. An empty `search` replaces (or creates) "
    "the whole file with `replace`.\n"
    "- A failed attempt's diff is diagnostic only: it has NOT been applied to OWNED FILES. "
    "Copy search snippets from OWNED FILES, never from the previous diff.\n"
    "- status DONE with edits when the task can be done inside the owned files; "
    "BLOCKED with a reason if it needs a change to a file you do not own; "
    "TEST_DISPUTE with edits=[] and a reason quoting the conflicting specification and "
    "test expectation if tests contradict the original specification or each other.\n"
    "- For missing context return status NEED_FILES, edits=[], and need_files containing "
    "tracked Python paths or path.py::Symbol (up to 8 per request). Reads are read-only "
    "and pinned to this candidate revision; they never grant edit ownership. At most "
    "3 read requests per task, including retries. Other statuses require need_files=[]. "
    "Marked source excerpts omit unrelated definitions; request the full file if needed.\n"
    "A partial OWNED FILE view permits only search/replace on the shown code. "
    "Never replace the entire file from an excerpt; request the full path first.\n"
    "Oversized full-file reads become signature catalogs; refine them with path.py::Symbol "
    "instead of repeating the full-file request.\n"
    "Return one JSON object only.\n"
)
MAX_FILE_BYTES = 80_000


class EditError(ValueError):
    pass


class ContextTooLarge(ValueError):
    pass


@dataclass(frozen=True)
class Pack:
    shared: str  # stable instructions, rules and repository map
    task: str  # selected interfaces, contract, owned files and frozen tests
    retry: str  # layer 7: previous diff and failure, only on a retry
    partial_reads: tuple[str, ...] = ()  # full-file selectors delivered as catalogs
    partial_owned: dict[str, str] = field(default_factory=dict, repr=False)
    requested: tuple[str, ...] = ()  # read selectors rendered under REQUESTED context

    @property
    def text(self) -> str:
        return self.shared + self.task + self.retry

    @property
    def prefix_hash(self) -> str:
        return digest(self.shared)[:12]


def render_files(title: str, files: dict[str, str]) -> str:
    body = "".join(f"--- {path} ---\n{files[path]}\n" for path in sorted(files))
    return f"\n## {title}\n{body or '(none)\n'}"


def spec_context(spec: str) -> str:
    """Preserve the original requirement text, independently of generated contracts."""
    return f"\n## ORIGINAL SPECIFICATION\n{spec}\n" if spec else ""


def shared_context(repo_map: str, rules: str, original_spec: str = "",
                   language_note: str = "") -> str:
    """Project prefix: selected task dependencies must not fragment this boundary."""
    return (BUILDER_INSTRUCTIONS
            + (f"\n## LANGUAGE\n{language_note}\nThe stdlib-only rule above means: use "
               "only dependencies the project already declares. need_files takes any tracked "
               "path or path::Symbol.\n" if language_note else "")
            + (f"\n## LEARNED RULES\n{rules}" if rules else "")
            + spec_context(original_spec)
            + f"\n## REPO MAP\n{repo_map or '(omitted)\n'}")


def context_pack(
    task: OrchTask,
    requirements: dict[str, str],
    repo_map: str,
    stubs: dict[str, str],
    owned: dict[str, str],
    tests: dict[str, str],
    budget: int,
    retry: tuple[str, str] = ("", ""),
    hint: str = "",
    rules: str = "",
    trial: str = "",
    dependencies: dict[str, str] | None = None,
    requested: dict[str, str] | None = None,
    *,
    original_spec: str = "",
    dependency_contracts: dict[str, object] | None = None,
    language_note: str = "",
) -> Pack:
    """Assemble layers in fixed order, retaining whole dependencies in priority order.

    Shed optional dependency implementations and the shared map before failing.
    Selected interface contracts, owned files and tests must remain complete.
    `dependencies` is merged code the task calls, ranked by orch_index.dependency_code.
    """
    contract = {
        "task": task.id,
        "goal": task.goal,
        "requirements": {rid: requirements.get(rid, "") for rid in task.reqs},
        "contract": task.contract,
        "tests_to_pass": task.tests,
        "owned_files": task.files,
    }
    if dependency_contracts:
        contract["dependency_contracts"] = dependency_contracts
    common = {path: text for path, text in stubs.items() if path not in task.files}
    own_interfaces = {path: text for path, text in stubs.items() if path in task.files}
    requested = {name: text for name, text in (requested or {}).items()
                 if name.split("::", 1)[0] not in owned}

    def task_layer(code: dict[str, str]) -> str:
        return (
            render_files("SHARED DEPENDENCY INTERFACES", common)
            + render_files("OWNED INTERFACES (frozen signatures)", own_interfaces)
            + f"\n## TASK\n{dumps(contract)}\n"
            + render_files("OWNED FILES (current content; missing files are new)", owned)
            + render_files("FROZEN TESTS", tests)
            + (render_files("REQUESTED READ-ONLY CONTEXT (pinned revision)", requested)
               if requested else "")
            + (render_files("CODE YOU CAN CALL (merged, read-only)", code) if code else "")
            + (f"\n## APPROACH\n{hint}\n" if hint else "")
            + (f"\n## EXTRA RULE (on trial)\n- {trial}\n" if trial else "")
        )

    diff, error = retry
    retry_layer = (
        f"\n## PREVIOUS ATTEMPT FAILED\n### diff\n{diff[-4000:]}\n### failure\n{error[-3000:]}\n"
        if error
        else ""
    )
    # dependency_code uses path::symbol keys. A symbol read covers that symbol and
    # its children, never sibling definitions; a signature catalog covers no bodies.
    code = {
        name: text for name, text in (dependencies or {}).items()
        if name.split("::", 1)[0] not in owned and not any(
            (text == body and name.split("::", 1)[0] == selector.split("::", 1)[0])
            or name == selector
            or ("::" in selector and name.startswith(selector + "."))
            or (selector == name.split("::", 1)[0]
                and not body.startswith("# PARTIAL CONTEXT:"))
            for selector, body in requested.items()
            if not body.startswith("# PARTIAL CONTEXT: full file")
        )
    }
    candidates: tuple[tuple[str, dict[str, str]], ...] = (
        (repo_map, code),
        (repo_map, {}),
        ("", {}),
    )
    for map_text, called in candidates:
        shared = shared_context(map_text, rules, original_spec, language_note)
        pack = Pack(shared, task_layer(called), retry_layer, requested=tuple(requested))
        if len(pack.text.encode()) <= budget:
            return pack
        retained: dict[str, str] = {}
        for name, content in called.items():
            candidate = Pack(shared, task_layer({**retained, name: content}), retry_layer,
                             requested=tuple(requested))
            if len(candidate.text.encode()) <= budget:
                retained[name] = content
        if retained:
            return Pack(shared, task_layer(retained), retry_layer, requested=tuple(requested))
    raise ContextTooLarge(
        f"{task.id}: specification, interfaces, owned files and tests require "
        f"{len(pack.text.encode())} bytes; builder budget is {budget} bytes"
    )


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def substitute(content: str, search: str, replace: str, path: str) -> str:
    """Replace the unique occurrence of `search`.

    Exact text first. Otherwise whole lines are compared ignoring trailing spaces, then
    ignoring a uniform missing indent (re-applied to `replace`); still exactly one
    match is required, so a tolerant match can never pick between candidates.
    """
    found = content.count(search)
    if found == 1:
        return content.replace(search, replace, 1)
    if found > 1:
        raise EditError(f"search text found {found} times in {path}; must be unique")
    lines = content.splitlines(keepends=True)
    wanted = search.strip("\n").splitlines()
    size = len(wanted)
    for loose in (False, True):
        hits: list[tuple[int, str]] = []
        for start in range(len(lines) - size + 1 if size else 0):
            block = lines[start : start + size]
            have_indent, want_indent = _indent(block[0]), _indent(wanted[0])
            if loose and not have_indent.endswith(want_indent):
                continue
            pad = have_indent[: len(have_indent) - len(want_indent)] if loose else ""
            if all(
                have.rstrip() == ((pad + want) if want.strip() else want).rstrip()
                for have, want in zip(block, wanted, strict=True)
            ):
                hits.append((start, pad))
        if len(hits) > 1:
            raise EditError(f"search text found {len(hits)} times in {path}; must be unique")
        if hits:
            start, pad = hits[0]
            body = "".join(
                (pad + line) if line.strip() else line
                for line in replace.strip("\n").splitlines(keepends=True)
            ).rstrip("\n")
            if body and lines[start + size - 1].endswith("\n"):
                body += "\n"
            return "".join(lines[:start]) + body + "".join(lines[start + size :])
    raise EditError(f"search text found 0 times in {path}; copy it exactly from the file")


def apply_edits(
    root: Path, owned: list[str], edits: object, *, visible: dict[str, str] | None = None,
) -> list[str]:
    """Apply edits under `root`; return the changed paths. Raises EditError."""
    if not isinstance(edits, list) or not edits:
        raise EditError("no edits returned")
    root = root.resolve()
    contents: dict[str, str] = {}
    limits: dict[str, int] = {}
    views = dict(visible or {})
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"path", "search", "replace"}:
            raise EditError("malformed edit")
        path, search, replace = edit["path"], edit["search"], edit["replace"]
        if not all(isinstance(value, str) for value in (path, search, replace)):
            raise EditError("malformed edit")
        if path not in owned:
            raise EditError(f"edit outside owned files: {path}")
        target = root / path
        if (
            not safe_relative(path)  # ownership, checked above, is the real boundary
            or target.is_symlink()
            or not target.resolve().is_relative_to(root)
            or any(parent.is_symlink() for parent in target.parents
                   if parent.is_relative_to(root))
        ):
            raise EditError(f"unsafe path: {path}")
        if path not in contents:
            if target.is_file():
                with target.open(encoding="utf-8", newline="") as handle:
                    contents[path] = handle.read()
            else:
                contents[path] = ""
            limits[path] = len(contents[path].encode()) + MAX_FILE_BYTES
        if path in views:
            if not search:
                raise EditError(f"{path}: partial owned view cannot replace the full file; "
                                "request it with NEED_FILES first")
            try:
                views[path] = substitute(views[path], search, replace, path)
            except EditError:
                raise EditError(
                    f"{path}: search is not uniquely visible; request the needed definition "
                    "or full file with NEED_FILES") from None
        contents[path] = (
            replace if not search else substitute(contents[path], search, replace, path)
        )
    for path, text in contents.items():
        if len(text.encode()) > limits[path]:
            raise EditError(f"{path} exceeds the {MAX_FILE_BYTES}-byte per-batch growth limit")
        if path.endswith(".py"):
            try:
                compile(text, path, "exec", dont_inherit=True)
            except SyntaxError as error:
                detail = f"syntax error in {path} line {error.lineno}: {error.msg}"
                if error.text:  # the resulting line, so the builder can see its own escape
                    detail += f"; resulting line: {error.text.rstrip()[:240]!r}"
                raise EditError(detail) from None
    for path, text in contents.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    return sorted(contents)


def interface_errors(stub: str, current: str) -> list[str]:
    """Signatures promised by a stub that the implementation dropped or changed."""
    try:
        kept = set(signatures(current))
    except SyntaxError:
        return ["implementation does not parse"]
    return [f"interface changed: {line}" for line in signatures(stub) if line not in kept]


def implementation_errors(before: str, current: str, required: str = "") -> list[str]:
    """Reject definition collisions and provably unfinished promised implementations.

    Compare with the pinned source, so existing abstract/extension points are not
    turned into new work. Overload declarations and property accessors are legal
    repeated names. Frozen concrete stubs, however, must acquire implementations.
    """
    try:
        new, contract = (ast.parse(source) for source in (current, required))
        compile(new, "<candidate>", "exec", dont_inherit=True)
    except SyntaxError as error:
        return [f"invalid Python at line {error.lineno}: {error.msg}"]
    try:
        old = ast.parse(before)
    except SyntaxError:
        old = ast.Module(body=[], type_ignores=[])  # a repair may fix previously broken source

    def concrete(tree: ast.Module) -> dict[str, list[Definition]]:
        # Reuse the shared definition walk; ignore declarative typing/ABC bodies.
        aliases = {"overload", "abstractmethod"}
        protocols = {"Protocol"}
        for node in tree.body:
            if (isinstance(node, ast.ImportFrom)
                    and node.module in {"typing", "typing_extensions", "abc"}):
                aliases.update(item.asname or item.name for item in node.names
                               if item.name in {"overload", "abstractmethod"})
                protocols.update(item.asname or item.name for item in node.names
                                 if item.name == "Protocol")
        result: dict[str, list[Definition]] = {}
        protocol_classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)
                            and any(ast.unparse(base).split("[")[0].split(".")[-1] in protocols
                                    for base in node.bases)}
        for name, node, _ in definitions(tree):
            if name.split(".")[0] in protocol_classes:
                continue
            decorators = [ast.unparse(item).split(".")[-1] for item in node.decorator_list]
            if any(item in aliases | {"getter", "setter", "deleter"} for item in decorators):
                continue
            result.setdefault(name, []).append(node)
        return result

    previous, present, promised = concrete(old), concrete(new), concrete(contract)
    errors = [f"{name}: promised concrete implementation is missing or became declarative"
              for name, nodes in promised.items() if name not in present
              and any(is_stub(node) for node in nodes)]
    collisions = {name for name, nodes in present.items()
                  if len(nodes) > max(1, len(previous.get(name, [])))}
    for name, nodes in present.items():
        if name in collisions and not any(name.startswith(parent + ".") for parent in collisions):
            errors.append(f"{name}: duplicate definitions at lines "
                          f"{', '.join(str(node.lineno) for node in nodes)}; later definitions "
                          "replace earlier ones. Edit the existing definition; do not append.")
        last = nodes[-1]
        if not is_stub(last):
            continue
        old_nodes = previous.get(name, [])
        preexisting_stub = old_nodes and is_stub(old_nodes[-1])
        must_implement = any(is_stub(node) for node in promised.get(name, []))
        if must_implement or (old_nodes and not preexisting_stub):
            errors.append(f"{name}: unimplemented concrete body at line {last.lineno}; "
                          "replace the NotImplementedError stub with the required implementation")
    return errors


FAILED_TEST = re.compile(r"^(FAIL|ERROR): \S+ \(([\w.]+)\)", re.MULTILINE)


def failed_tests(report: str) -> list[str]:
    """Dotted ids of the tests a unittest report marks FAIL or ERROR, in order."""
    return list(dict.fromkeys(match.group(2) for match in FAILED_TEST.finditer(report)))


def failed_in(report: str, tests: list[str]) -> list[str] | None:
    """Which of `tests` failed, or None when the report cannot say for every failure.

    A module that fails to import shows up as `unittest.loader._FailedTest.<name>`;
    it is mapped back to the requested tests of that module. A report whose tail was
    cut (the sandbox keeps the last 16 KB) names fewer failures than its summary
    counts, so it is not trusted.
    """
    failed = failed_tests(report)
    summary = re.search(r"^FAILED \(([^)]*)\)\s*$", report, re.MULTILINE)
    counts = dict(re.findall(r"(\w+)=(\d+)", summary.group(1))) if summary else {}
    expected = int(counts.get("failures", 0)) + int(counts.get("errors", 0))
    if summary is None or len(failed) != expected:
        return None
    found: list[str] = []
    for name in failed:
        if name.startswith("unittest.loader._FailedTest."):
            module = name.rsplit(".", 1)[1]
            found += [t for t in tests if t.rsplit(".", 2)[0].split(".")[-1] == module]
        elif name in tests:
            found.append(name)
        else:
            return None
    return list(dict.fromkeys(found))


def common_failures(reports: list[str], tests: list[str]) -> list[str]:
    """Shared failures need two complete reports; agreement alone never changes tests."""
    if len(reports) < 2:
        return []
    common = set(tests)
    for report in reports:
        failed = failed_in(report, tests)
        if not failed:
            return []
        common.intersection_update(failed)
    return [test for test in tests if test in common]


FRAME = re.compile(r'^\s*File "([^"]+)", line (?P<lineno>\d+), in (?P<function>\S+)')


def frame_path(path: str) -> str:
    """Repository-relative path of a traceback frame; "" for code outside the tree under
    test (the standard library, unittest). Task worktrees and the integration tree live
    under `.git/orch/<run>/`; both sandboxes keep host paths (older Linux reports
    used /work)."""
    if "/.git/orch/" in path:
        parts = path.rsplit("/.git/orch/", 1)[1].split("/")
        if len(parts) > 3 and parts[1] == "work":
            return "/".join(parts[3:])
        return "/".join(parts[2:]) if len(parts) > 2 and parts[1] == "integration" else ""
    if path.startswith("/work/"):
        return path.removeprefix("/work/")
    return "" if path.startswith("/") else path


def diagnose_failure(
    report: str, frozen_paths: set[str], owned: dict[str, str],
) -> tuple[str, str]:
    """Attribute only directly evidenced failures; everything else stays behavioral.

    A frozen-test name error requests independent review, never automatic edits.
    An executed owned stub is structural, including newly added abstract helpers
    that were correctly allowed by the static contract gate.
    """
    start, origin, lineno = -1, "", 0
    lines = report.splitlines()
    for index, line in enumerate(lines):
        if match := FAILED_TEST.match(line):
            start = index if match.group(1) == "ERROR" else -1
            origin = ""
        elif line.startswith("Traceback "):
            origin = ""
        elif frame := FRAME.match(line):
            origin = frame_path(frame.group(1))
            lineno = int(frame.group("lineno"))
        elif start >= 0:
            exception = line.partition(":")[0]
            if origin in frozen_paths and exception in {"NameError", "UnboundLocalError"}:
                return "fixture", "\n".join(lines[start:index + 1])[-2000:]
            if exception == "NotImplementedError" and origin in owned:
                for node in ast.walk(ast.parse(owned[origin])):
                    if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and node.lineno <= lineno <= (node.end_lineno or node.lineno)
                            and is_stub(node)):
                        trace = "\n".join(lines[start:index + 1])[-2000:]
                        return (
                            "structure", f"{origin}: executed stub {node.name}; fix body\n{trace}",
                        )
    return "test", report


def signature(category: str, text: str) -> str:
    """Stable id of a failure: failing test ids, the code symbols in the traceback
    (`path::function`, never line numbers) and exception types."""
    keys: list[str] = []
    for line in text.splitlines():
        test = FAILED_TEST.match(line)
        if test:
            keys.append(f"{test.group(1)} {test.group(2)}")
            continue
        frame = FRAME.match(line)
        if frame:
            path = frame_path(frame.group(1))
            if path and not PurePosixPath(path).name.startswith("test_"):
                keys.append(f"{path}::{frame.group('function')}")
            continue
        error = re.match(r"^\s*([A-Za-z_][\w.]*(?:Error|Exception|Failure))\b", line)
        if error:
            keys.append(error.group(1))
    basis = "\n".join(keys) if keys else re.sub(r"\d+", "N", text.strip())[-500:]
    return f"{category}:{digest(basis)[:12]}"


REQUIRED_TRAILERS = (
    "Orch-Run", "Orch-Task", "Orch-Req", "Orch-Attempt", "Orch-Plan", "Orch-Contract",
    "Orch-Model", "Orch-Rules",
)


def contract_hash(task: OrchTask) -> str:
    fields: list[object] = [task.goal, task.contract, task.reqs, task.files, task.tests]
    if task.interface_depends_on:
        fields.append(sorted(task.interface_depends_on))
    return digest(dumps(fields))[:12]


def commit_message(
    run_id: str,
    plan: str,
    task: OrchTask,
    attempt: str,
    model: str,
    title: str = "",
    rules: str = "none",
    inputs: str = "",
) -> str:
    values = (
        run_id, task.id, ", ".join(task.reqs), attempt, plan, contract_hash(task),
        model, rules,
    )
    pairs = zip(REQUIRED_TRAILERS, values, strict=True)
    trailers = "".join(f"{key}: {value}\n" for key, value in pairs)
    if inputs:
        trailers += f"Orch-Inputs: {inputs}\n"
    subject = " ".join((title or f"orch {task.id}: {task.goal}").split())[:72]
    return f"{subject}\n\n{trailers}"


def parse_trailers(message: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in message.splitlines():
        key, sep, value = line.partition(": ")
        if sep and key in (*REQUIRED_TRAILERS, "Orch-Inputs"):
            found[key] = value.strip()
    return found
