"""The supervisor: one judgment call when a task's cheap attempts fail.

A builder gets one attempt and one cheap retry with its failure. After that, or when it
reports BLOCKED or TEST_DISPUTE, the evidence goes to a strong model which chooses one
action from a closed menu. The situations are open-ended and read from the evidence; the
menu is small and mechanical to apply, so a new kind of failure needs no new code:

- retry: guidance for the next attempt of the same builder;
- escalate: the next attempt by the strong model, with guidance;
- revise_tests: edits to this task's test files, each justified by the specification, or
  to the command that runs them (and the component's test command) when the runner is wrong;
- revise_contract: a clearer contract for this task (dependents are refreshed by it);
- split: replacement tasks for this one (and the tasks that depend on it);
- environment: the checks cannot run here; a person must fix the machine, not the code;
- give_up: nothing automatic is left; a person decides.
"""

from __future__ import annotations

import re
from typing import cast

from orchestragent.native import ContextPrompt
from orchestragent.orch_build import SPEC_AUTHORITY, render_files, spec_context
from orchestragent.orch_plan import PLAN_SCHEMA
from orchestragent.orch_state import OrchTask
from orchestragent.serialization import TEXT, dumps, object_schema

ACTIONS = ("investigate", "retry", "escalate", "revise_tests", "revise_contract", "split",
           "environment", "give_up")
MAX_SUPERVISIONS = 3  # per task: the one spend bound that remains
MAX_INVESTIGATIONS = 2  # sandbox commands the supervisor may run before each decision

SUPERVISOR_SCHEMA = object_schema({
    "action": {"type": "string", "enum": list(ACTIONS)},
    "explanation": TEXT,
    "command": TEXT,
    "guidance": TEXT,
    "test_edits": {"type": "array",
                   "items": object_schema({"path": TEXT, "search": TEXT, "replace": TEXT})},
    "command_edits": {"type": "array",
                      "items": object_schema({"search": TEXT, "replace": TEXT})},
    "contract": TEXT,
    "tasks": cast(dict[str, object], PLAN_SCHEMA["properties"])["tasks"],
})

MENU = (
    "You supervise one task of a parallel build whose cheap attempts failed. Read the "
    "evidence and choose exactly one action; explain the decision in two or three sentences "
    "that name what you saw in the evidence.\n"
    "- investigate: the evidence does not show the cause (a test hid stderr, an output was "
    "cut, you need to see a file or run the program): give one shell `command`, run from the "
    "repository root in the same offline sandbox on the latest candidate; its output comes "
    "back and you decide again. Prefer this over guessing between environment and code.\n"
    "- retry: the code is wrong and the failure says how; give `guidance` for the same builder.\n"
    "- escalate: the task needs a stronger model; give `guidance`.\n"
    "- revise_tests: a test contradicts the ORIGINAL SPEC or is broken (wrong value, fixture, "
    "name, timeout, placement); return minimal `test_edits` to the test files below and, in "
    "`explanation`, quote the specification clause each change follows. Keep every test's "
    "coverage; never weaken a test to match the code. A test file of another task may be "
    "edited too when the evidence shows it breaks this task's checks (they share a build or "
    "a package); every task whose tests change must still fail on the stubs. When the "
    "command that runs the tests "
    "is what is wrong (a missing classpath or flag, a wrong directory: the `tests` entries "
    "of TASK show it), return `command_edits`: search/replace pairs applied to this task's "
    "check commands and to its component's test command where they match.\n"
    "- revise_contract: the contract misled the builder; return the corrected `contract` in "
    "the spec's own terms (tasks that depend on this one see the change).\n"
    "- split: the task is too large or wrongly cut; return replacement `tasks` (new ids from "
    "FIRST NEW ID) that together own exactly the replaced tasks' files, carry exactly their "
    "tests, depend only on each other or on tasks outside REPLACE, and each list at least one "
    "requirement.\n"
    "- environment: the check could not run here (a missing tool, a sandbox limit) and no "
    "code change would help.\n"
    "- give_up: none of the above; say what a person should look at.\n"
    "Fields not used by the action are empty (\"\", [] ). Return JSON only.\n"
)


def _history(attempts: list[dict[str, object]]) -> str:
    lines = []
    for item in attempts[-6:]:
        lines.append(f"- {item.get('label')}: {item.get('route')} ({item.get('rung')}) -> "
                     f"{item.get('outcome')} {item.get('category', '')}".rstrip())
        summary = str(item.get("summary", "")).strip()
        if summary:
            lines.append(f"  builder: {summary[:300]}")
    return "\n".join(lines)


def supervise_prompt(
    *, spec: str, task: OrchTask, requirements: dict[str, str], tests: dict[str, str],
    owned: dict[str, str], attempts: list[dict[str, object]], failure: str, diff: str,
    signal: str, all_tasks: list[OrchTask], chosen: list[str], first_id: int,
    investigations: list[tuple[str, str]] | None = None,
) -> str:
    brief = {"task": task.id, "goal": task.goal, "contract": task.contract,
             "requirements": {rid: requirements.get(rid, "") for rid in task.reqs},
             "files": task.files, "tests": task.tests, "depends_on": task.depends_on}
    view = [{**other.entry(), "status": other.status} for other in all_tasks
            if other.status != "REPLANNED"]
    # The spec and the menu are the cached prefix shared by every supervision of a run;
    # the task's tests and evidence differ per task and stay in the dynamic part.
    return ContextPrompt(
        SPEC_AUTHORITY + MENU + spec_context(spec),
        render_files("TESTS (owned by the test designer)", tests)
        + f"\n## TASK\n{dumps(brief)}\n"
        + render_files("OWNED FILES (latest attempt)", owned)
        + f"\n## ATTEMPTS\n{_history(attempts)}\n"
        + (f"\n## BUILDER'S REPORT\n{signal[:3000]}\n" if signal else "")
        + f"\n## LAST FAILURE\n### diff\n{diff[-6000:]}\n### output\n{failure[-6000:]}\n"
        + f"\n## ALL TASKS\n{dumps(view)}\n## REPLACE (for split)\n{dumps(chosen)}\n"
        f"## FIRST NEW ID\nT-{first_id:03d}\n"
        + (f"\n## GUIDANCE ALREADY GIVEN\n{task.guidance}\n" if task.guidance else "")
        + "".join(f"\n## INVESTIGATION {n}\n$ {command}\n{output[-6000:]}\n"
                  for n, (command, output) in enumerate(investigations or [], 1)),
    )


def next_task_id(tasks: list[OrchTask]) -> int:
    numbers = [int(match.group(1)) for t in tasks if (match := re.fullmatch(r"T-(\d+)", t.id))]
    return max(numbers, default=0) + 1


def subtree(tasks: list[OrchTask], failing: str, owners: list[str]) -> list[str]:
    """The failing task, PLANNED owners it needs files from, and all their dependents."""
    by_id = {task.id: task for task in tasks}
    chosen = [failing] + [o for o in owners if o != failing and by_id[o].status == "PLANNED"]
    changed = True
    while changed:
        changed = False
        for task in tasks:
            if task.id not in chosen and set(task.depends_on) & set(chosen):
                chosen.append(task.id)
                changed = True
    return chosen
