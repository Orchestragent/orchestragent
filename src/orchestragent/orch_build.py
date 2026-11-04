"""Builder contract: a byte-stable context pack in, search/replace edits out.

Models never touch the filesystem; `apply_edits` is the only writer and it accepts
edits to owned files only. Error signatures are hashes of normalized failures so the
failure ladder can tell "same error again" from progress.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from orchestragent.envelope import digest
from orchestragent.live_workspace import safe_relative
from orchestragent.orch_repo import REQUEST_RULES, REQUEST_SCHEMA_ITEMS
from orchestragent.orch_state import OrchTask
from orchestragent.serialization import TEXT, dumps, object_schema

SIGNALS = ("DONE", "BLOCKED", "TEST_DISPUTE", "NEED_CONTEXT")
EDIT_SCHEMA = object_schema(
    {
        "status": {"type": "string", "enum": list(SIGNALS)},
        "summary": TEXT,
        "reason": TEXT,
        "requests": REQUEST_SCHEMA_ITEMS,
        "edits": {
            "type": "array",
            "items": object_schema({"path": TEXT, "search": TEXT, "replace": TEXT}),
        },
    }
)
# Claude's patch may also carry a one-line lesson (recorded with its summary).
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
    "You implement one task of a larger build. " + SPEC_AUTHORITY
    + "Edit only the files listed under OWNED FILES, in their own languages, keeping every "
    "interface the stubs declare. Tests and other tasks' files are not yours: when a test "
    "contradicts the specification or another test, answer TEST_DISPUTE (edits=[]) quoting "
    "the clause and the expectation instead of coding to the test; when the task needs a "
    "change in a file you do not own, answer BLOCKED with the reason. Never special-case "
    "test inputs or hard-code expected outputs. Use only what the project has installed; "
    "nothing can be downloaded.\n"
    "Answer with search/replace edits: `search` is an exact, unique snippet of the current "
    "file and `replace` its new text; an empty `search` replaces (or creates) the whole "
    "file. A failed attempt's diff is diagnostic only and was not applied: copy search "
    "snippets from OWNED FILES. " + REQUEST_RULES + " At most 3 such rounds per task; other "
    "statuses carry requests=[]. Return one JSON object only.\n"
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
    requested: tuple[str, ...] = ()  # the requests answered under CONTEXT YOU ASKED FOR

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


def shared_context(repo_map: str, original_spec: str = "",
                   language_note: str = "") -> str:
    """Project prefix: selected task dependencies must not fragment this boundary."""
    return (BUILDER_INSTRUCTIONS
            + (f"\n## LANGUAGE\n{language_note}\n" if language_note else "")
            + spec_context(original_spec)
            + f"\n## REPOSITORY (tracked files and sizes)\n{repo_map or '(omitted)\n'}")


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
    requested: dict[str, str] | None = None,
    *,
    original_spec: str = "",
    dependency_contracts: dict[str, object] | None = None,
    language_note: str = "",
) -> Pack:
    """Assemble the layers in a fixed order; only the repository listing is shed before
    the pack is refused. Spec, contract, owned files, tests and answered requests stay
    complete.
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
    requested = dict(requested or {})

    def task_layer(code: dict[str, str]) -> str:
        return (
            render_files("SHARED DEPENDENCY INTERFACES", common)
            + render_files("OWNED INTERFACES (frozen signatures)", own_interfaces)
            + f"\n## TASK\n{dumps(contract)}\n"
            + render_files("OWNED FILES (current content; missing files are new)", owned)
            + render_files("FROZEN TESTS", tests)
            + ("\n## CONTEXT YOU ASKED FOR (this revision, read-only)\n"
               + "\n\n".join(requested.values()) + "\n" if requested else "")
            + (f"\n## APPROACH\n{hint}\n" if hint else "")
        )

    diff, error = retry
    retry_layer = (
        f"\n## PREVIOUS ATTEMPT FAILED\n### diff\n{diff[-4000:]}\n### failure\n{error[-3000:]}\n"
        if error
        else ""
    )
    for map_text in (repo_map, ""):
        shared = shared_context(map_text, original_spec, language_note)
        pack = Pack(shared, task_layer({}), retry_layer, requested=tuple(requested))
        if len(pack.text.encode()) <= budget:
            return pack
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


def apply_edits(root: Path, owned: list[str], edits: object) -> list[str]:
    """Apply edits under `root`; return the changed paths. Raises EditError."""
    if not isinstance(edits, list) or not edits:
        raise EditError("no edits returned")
    root = root.resolve()
    contents: dict[str, str] = {}
    limits: dict[str, int] = {}
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
        contents[path] = (
            replace if not search else substitute(contents[path], search, replace, path)
        )
    for path, text in contents.items():
        if len(text.encode()) > limits[path]:
            raise EditError(f"{path} exceeds the {MAX_FILE_BYTES}-byte per-batch growth limit")
    for path, text in contents.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    return sorted(contents)


def failed_tests(report: str, tests: list[str] | None = None) -> list[str]:
    """Checks a verification report marks as failed (orch_verify), in report order."""
    out = []
    for line in report.splitlines():
        head, _, rest = line.partition(": ")
        if head.startswith(("FAIL", "TIMEOUT")) and rest.startswith("cmd:") and rest not in out:
            if tests is None or rest in tests:
                out.append(rest)
    return out


def common_failures(reports: list[str], tests: list[str]) -> list[str]:
    """Checks that failed in every report; agreement alone never changes tests."""
    if len(reports) < 2:
        return []
    common = set(tests)
    for report in reports:
        failed = failed_tests(report, tests)
        if not failed:
            return []
        common.intersection_update(failed)
    return [test for test in tests if test in common]


VOLATILE = re.compile(r"/\S*?/\.git/orch/\S*|0x[0-9a-fA-F]+|\d+(?:\.\d+)?")


def signature(category: str, text: str) -> str:
    """Stable id of a failure in any language: the failing checks plus the tail of their
    output with paths, addresses and numbers normalized away."""
    failing = failed_tests(text)
    tail = [line.strip() for line in text.splitlines()
            if line.strip() and not line.startswith(("PASS:", "FAIL", "TIMEOUT", "issuer=",
                                                       "mode=", "revision=", "checks=",
                                                       "wall_limit_s="))]
    basis = "\n".join([*failing, *(VOLATILE.sub("N", line) for line in tail[-20:])])
    return f"{category}:{digest(basis)[:12]}"


REQUIRED_TRAILERS = (
    "Orch-Run", "Orch-Task", "Orch-Req", "Orch-Attempt", "Orch-Plan", "Orch-Contract",
    "Orch-Model",
)


def contract_hash(task: OrchTask) -> str:
    """What a builder was asked to do: goal, contract, requirements, owned files and the
    test files it must satisfy. The command that runs those tests is not part of it, so
    a saved candidate survives the supervisor repairing a broken check command."""
    from orchestragent.orch_verify import files_of  # orch_verify imports this module

    fields: list[object] = [task.goal, task.contract, task.reqs, task.files,
                            files_of(list(task.tests))]
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
    inputs: str = "",
) -> str:
    values = (
        run_id, task.id, ", ".join(task.reqs), attempt, plan, contract_hash(task),
        model,
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
