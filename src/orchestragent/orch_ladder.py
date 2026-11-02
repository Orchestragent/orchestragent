"""Failure ladder policy and the Claude exception prompts it escalates to.

Rungs: 0 first DSF attempt, 1 DSF retry with the error, 2 k parallel DSF samples,
3 Claude patch, 4 Claude-thickened contract then DSF, 5 subtree replan, 6 PARKED.
Each rung is used at most once per task, so every task's spend is bounded.
"""

import math
import re
from typing import cast

from orchestragent.native import ContextPrompt
from orchestragent.orch_build import SPEC_AUTHORITY, render_files, spec_context
from orchestragent.orch_plan import PLAN_SCHEMA
from orchestragent.orch_state import OrchTask
from orchestragent.serialization import TEXT, dumps, object_schema

PARK = 6
SAMPLING_FLOOR = 0.3  # below this pass rate, sampling is replaced by a thicker contract
TARGET = 0.9
MAX_SAMPLES = 4
MAX_GENERATION = 2  # tasks born from a replan may be replanned once more, then park
# Variation goes in the task layer, so every sample keeps the shared cached prefix.
HINTS = (
    "",
    "Prefer the simplest direct implementation that satisfies the contract.",
    "Handle every boundary case named in the contract before the main logic.",
    "Read the previous failure first and fix its root cause, not the symptom.",
)


def next_level(
    used: list[int], repeated: bool, pass_rate: float, *, sampleable: bool = True,
) -> int:
    """The next rung after a failure; a repeated failure skips straight to Claude."""
    if repeated and 3 not in used:
        return 3
    order = (1, 2, 3, 4, 5) if pass_rate >= SAMPLING_FLOOR else (1, 4, 3, 5)
    for level in order:
        if (level not in used and not (repeated and level in (1, 2))
                and (level != 2 or sampleable)):
            return level
    return PARK


def samples(pass_rate: float) -> int:
    """k = ceil(ln(1 - target) / ln(1 - p)), clamped to [2, MAX_SAMPLES]."""
    if pass_rate >= 0.99:
        return 2
    if pass_rate <= 0:
        return MAX_SAMPLES
    k = math.ceil(math.log(1 - TARGET) / math.log(1 - pass_rate))
    return max(2, min(MAX_SAMPLES, k))


def _evidence(task: OrchTask) -> str:
    return (
        f"\n## LAST FAILURE\n### diff\n{task.last_diff[-4000:]}\n"
        f"### error\n{task.last_error[-3000:]}\n"
    )


# The native client injects structured-output schemas into the cached prefix.
# These two exception operations share a small wire format, not their authority.
EXCEPTION_SCHEMA = object_schema(
    {
        "verdict": {"type": "string", "enum": ["tests_correct", "tests_wrong", "contract"]},
        "explanation": TEXT,
        "edits": {
            "type": "array",
            "items": object_schema({"path": TEXT, "search": TEXT, "replace": TEXT}),
        },
        "lesson": TEXT,
        "contract": TEXT,
    }
)
EXCEPTION_CONTEXT = (
    "Resolve a failed implementation against the original specification. The current user "
    "message selects either independent test review or contract clarification. Never mix "
    "the operations: only review can propose test edits; clarification cannot change tests.\n"
    + SPEC_AUTHORITY
)


def _brief(task: OrchTask, requirements: dict[str, str]) -> str:
    return dumps(
        {
            "task": task.id,
            "goal": task.goal,
            "contract": task.contract,
            "requirements": {rid: requirements.get(rid, "") for rid in task.reqs},
            "tests_to_pass": task.tests,
        }
    )


def thicken_prompt(
    task: OrchTask, requirements: dict[str, str], tests: dict[str, str], owned: dict[str, str],
    *, original_spec: str = "",
) -> str:
    return ContextPrompt(
        EXCEPTION_CONTEXT + spec_context(original_spec) + render_files("FROZEN TESTS", tests),
        "A cheaper builder failed this task repeatedly. Rewrite its contract so the next "
        "attempt cannot misread it: step-by-step pseudo-code, every boundary case with "
        "its exact expected result, and counter-examples naming the wrong behaviour seen "
        "in the failure. If a test conflicts with the original specification, identify the "
        "conflict in the contract and tell the builder to return TEST_DISPUTE. "
        "Return verdict=contract, the clarified contract, explanation, edits=[], lesson=''. "
        "Return JSON only.\n"
        + f"\n## TASK\n{_brief(task, requirements)}\n"
        + render_files("OWNED FILES", owned) + _evidence(task),
    )


# Replans and wrong tests are planning mistakes; their lesson may become a planner rule.
LESSON_ASK = (
    "Also return `lesson`: one imperative sentence a planner should follow in future "
    "plans to avoid this mistake, or an empty string.\n"
)


def review_prompt(
    task: OrchTask, requirements: dict[str, str], tests: dict[str, str], reason: str,
    *, original_spec: str = "",
) -> str:
    return ContextPrompt(
        EXCEPTION_CONTEXT + spec_context(original_spec) + render_files("FROZEN TESTS", tests),
        "Independently review the disputed generated tests against the original specification. "
        "Do not assume the test author or builder is correct.\n"
        "Check all supplied tests for independent contradictions and broken fixtures, "
        "not only the first reported failure; correct those together.\n"
        "- tests_correct: cite the relevant requirement and explain how the tests satisfy it "
        "(no edits).\n"
        "- tests_wrong: return minimal search/replace edits to the test files below. Keep "
        "every test method name and assertion coverage; fix expectations or broken fixtures "
        "that contradict the requirements. Always set contract='' in review mode.\n"
        + LESSON_ASK
        + "Return JSON only.\n"
        + f"\n## TASK\n{_brief(task, requirements)}\n"
        + f"\n## BUILDER'S DISPUTE\n{reason[:2000]}\n" + _evidence(task),
    )


REPLAN_SCHEMA = object_schema(
    {"summary": TEXT, "tasks": cast(dict[str, object], PLAN_SCHEMA["properties"])["tasks"],
     "lesson": TEXT}
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


def replan_prompt(
    plan: dict[str, object],
    tasks: list[OrchTask],
    chosen: list[str],
    first_id: int,
    failing: OrchTask,
    reason: str,
    *, original_spec: str = "",
    errors: list[str] | None = None, previous: object = None,
) -> str:
    view = [
        {**task.entry(), "status": task.status}
        for task in tasks
        if task.status != "REPLANNED"
    ]
    return ContextPrompt(
        SPEC_AUTHORITY + spec_context(original_spec)
        + "You are re-planning part of a running build after a task failed or reported it "
        "needs files it does not own. Replace exactly the tasks listed under REPLACE.\n"
        "Rules: the replacement tasks together must carry exactly the same tests as the "
        "replaced ones (frozen tests and requirement markers never change); files may be "
        "re-split among them or added as new non-test files, but never take a file owned "
        "by a task outside REPLACE; depend only on replacement tasks or on tasks outside "
        "REPLACE; use the specified FIRST NEW ID; prefer smaller tasks with explicit "
        "contracts that avoid the recorded failure. Preserve every original owned file. "
        "Every replacement must own an original file or feed an owner of an original file "
        "through dependency edges. Each replacement task lists at least one requirement, at "
        "least one owned file and at least one of the replaced frozen tests that proves one "
        "of its requirements; a helper with no test of its own must be merged into a task "
        "that has one. Test review, verification and dispute reports are controller "
        "operations; never create post-implementation report tasks to work around frozen tests.\n"
        + LESSON_ASK
        + "Return JSON only.\n"
        f"\n## REQUIREMENTS\n{dumps(plan['requirements'])}\n",
        f"\n## FIRST NEW ID\nT-{first_id:03d}\n"
        f"\n## ALL TASKS\n{dumps(view)}\n"
        f"\n## REPLACE\n{dumps(chosen)}\n"
        f"\n## WHY\n{reason[:2000]}\n"
        + _evidence(failing)
        + ("\n## PREVIOUS REPLAN REJECTED BY THE CONTROLLER\n" + dumps(previous)
           + "\n## CONTROLLER ERRORS\n" + dumps(errors)
           + "\nReturn the complete corrected replacement satisfying every rule above.\n"
           if errors else "")
    )


# --- Jev: multiple-choice decisions that deterministic rules cannot make ------
# Jev only ever sees the contract, the diff and the failure, and only ever chooses
# among rungs the deterministic policy would still allow; below the confidence
# threshold the deterministic rung stands.
JEV_CONFIDENCE = 0.6
CHEAT_FLAG = 0.85
RUNGS: dict[int, tuple[str, str]] = {
    1: ("retry", "a small slip the builder fixes by reading the error once more"),
    2: ("sample", "the approach is right but brittle; independent attempts will find one"),
    3: ("claude_patch", "a localized fix that needs a stronger model"),
    4: ("thicken", "the builder misread the contract; it needs pseudo-code and edge cases"),
    5: ("replan", "mis-scoped: needs files it does not own or should be split"),
}
CHEAT_QUESTION: dict[str, object] = {
    "cheat": {
        "type": "boolean",
        "instructions": "Does this diff special-case particular test inputs or hard-code "
        "expected outputs instead of implementing the contract in general?",
        "criteria": {
            "true": "branches on literal test values or returns canned answers",
            "false": "a general implementation of the contract",
        },
    }
}


def triage_options(used: list[int], pass_rate: float) -> list[int]:
    """Rungs still open to a failed task, in policy order."""
    return [
        level for level in (1, 2, 3, 4, 5)
        if level not in used and not (level == 2 and pass_rate < SAMPLING_FLOOR)
    ]


def triage_question(options: list[int]) -> dict[str, object]:
    return {
        "rung": {
            "type": "choice",
            "instructions": "Which recovery step will most likely get this failed coding "
            "attempt to pass its frozen tests?",
            "criteria": {RUNGS[level][0]: RUNGS[level][1] for level in options},
        }
    }


def jev_state(task: OrchTask, category: str, detail: str, diff: str) -> dict[str, object]:
    """Contract, diff and failure only, bounded to stay far inside Jev's context."""
    return {
        "goal": task.goal[:1000],
        "contract": task.contract[:4000],
        "failure": category,
        "error": detail[-6000:],
        "diff": diff[-10000:],
    }


def pick(answer: object, options: list[int]) -> tuple[int | None, float, str]:
    """(rung if confident, confidence, chosen name) from a Jev choice answer."""
    if not isinstance(answer, dict):
        return None, 0.0, ""
    name = str(answer.get("choice", ""))
    probabilities = answer.get("probabilities")
    confidence = probabilities.get(name) if isinstance(probabilities, dict) else None
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        return None, 0.0, name
    level = next((lvl for lvl in options if RUNGS[lvl][0] == name), None)
    if level is None or confidence < JEV_CONFIDENCE:
        return None, float(confidence), name
    return level, float(confidence), name


def cheat_probability(answer: object) -> float | None:
    if not isinstance(answer, dict):
        return None
    value = answer.get("probability")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1:
        return float(value)
    return None
