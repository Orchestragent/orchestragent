"""Synthetic wide runs for scale tests and the scale benchmark; no model is involved.

`synthetic_plan` builds a lint-clean plan of `tasks` one-function modules spread over
`packages` packages. Task N implements `fN(x) = x + N + sum(fD(x) for each dependency
D)`, depends on up to `fan_in` earlier tasks, and has one frozen test class, so a wrong
dependency shows up in its own test. `SyntheticBuilder` is a stand-in for every model
route: it answers builder calls with the right body, except for a deterministic share
of first attempts that are wrong, rate limited or (Claude only) asked to thicken.
"""

import random
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from orchestragent.envelope import digest
from orchestragent.native import NativeResult


@dataclass(frozen=True)
class Shape:
    tasks: int = 60
    packages: int = 6
    fan_in: int = 2
    seed: int = 7


def _value(index: int, deps: dict[int, list[int]], x: int, memo: dict[tuple[int, int], int]
           ) -> int:
    key = (index, x)
    if key not in memo:
        memo[key] = x + index + sum(_value(d, deps, x, memo) for d in deps[index])
    return memo[key]


def synthetic_plan(shape: Shape) -> tuple[dict[str, object], dict[str, tuple[str, str]]]:
    """(plan, task id -> (path, correct body)) for a DAG of the requested shape."""
    if not 1 <= shape.tasks <= 999:
        raise ValueError("synthetic plans hold 1-999 tasks (T-001..T-999)")
    rng = random.Random(shape.seed)
    deps: dict[int, list[int]] = {}
    package = {n: (n - 1) % shape.packages for n in range(1, shape.tasks + 1)}
    for n in range(1, shape.tasks + 1):
        earlier = list(range(max(1, n - 3 * shape.packages), n))
        deps[n] = sorted(rng.sample(earlier, min(len(earlier), rng.randint(0, shape.fan_in))))
    memo: dict[tuple[int, int], int] = {}
    files: list[dict[str, str]] = []
    tests: dict[int, list[str]] = {p: [] for p in range(shape.packages)}
    bodies: dict[str, tuple[str, str]] = {}
    tasks: list[dict[str, object]] = []
    for n in range(1, shape.tasks + 1):
        path = f"pkg{package[n]}/f{n}.py"
        imports = "".join(f"from pkg{package[d]}.f{d} import f{d}\n" for d in deps[n])
        calls = "".join(f" + f{d}(x)" for d in deps[n])
        files.append({"path": path, "content": (
            f'"""Synthetic task {n}."""\n\n{imports}\n\ndef f{n}(x: int) -> int:\n'
            f'    """x + {n}{calls.replace(" + ", " plus ")}."""\n    raise NotImplementedError\n'
        )})
        bodies[f"T-{n:03d}"] = (path, f"return x + {n}{calls}")
        tests[package[n]].append(
            f"class F{n}Test(unittest.TestCase):\n    def test_r{n}_value(self) -> None:\n"
            f"        self.assertEqual(f{n}(2), {_value(n, deps, 2, memo)})\n"
            f"        self.assertEqual(f{n}(-3), {_value(n, deps, -3, memo)})\n"
        )
        tasks.append({
            "id": f"T-{n:03d}", "goal": f"implement f{n}", "reqs": [f"R{n}"],
            "files": [path], "depends_on": [f"T-{d:03d}" for d in deps[n]],
            "tests": [f"tests.test_pkg{package[n]}.F{n}Test.test_r{n}_value"],
            "contract": f"f(x) = x + {n}{calls}",
        })
    for p in range(shape.packages):
        members = [n for n in range(1, shape.tasks + 1) if package[n] == p]
        header = "import unittest\n\n" + "".join(
            f"from pkg{p}.f{n} import f{n}\n" for n in members
        )
        files.append({"path": f"pkg{p}/__init__.py", "content": ""})
        files.append({"path": f"tests/test_pkg{p}.py",
                      "content": header + "\n\n" + "\n\n".join(tests[p])})
    plan: dict[str, object] = {
        "summary": f"synthetic {shape.tasks} tasks over {shape.packages} packages",
        "requirements": [{"id": f"R{n}", "text": f"f{n} value"}
                         for n in range(1, shape.tasks + 1)],
        "files": files,
        "tasks": tasks,
    }
    return plan, bodies


@dataclass
class SyntheticBuilder:
    """Answers every model route of a synthetic run (see module docstring)."""

    plan: dict[str, object]
    bodies: dict[str, tuple[str, str]]
    wrong: float = 0.0  # share of tasks whose first DSF attempt is wrong
    limited: float = 0.0  # share of tasks whose first call is rate limited
    latency: float = 0.0  # seconds per call, to model a real provider
    calls: dict[str, int] = field(default_factory=dict)
    answers: dict[str, int] = field(default_factory=dict)  # DSF answers that were not 429
    lock: threading.Lock = field(default_factory=threading.Lock)
    sleep: Callable[[float], None] = time.sleep

    def _share(self, task: str, salt: str, share: float) -> bool:
        return int(digest(f"{salt}/{task}")[:8], 16) / 0xFFFFFFFF < share

    def __call__(
        self, provider: str, model: str, prompt: str, schema: dict[str, object], run_id: str,
        cancelled: Callable[[], bool], *, effort: str = "",
    ) -> NativeResult:
        if self.latency:
            self.sleep(self.latency)
        found = re.search(r"T-[0-9]{3}", run_id)
        if found is None:
            return NativeResult("ok", self.plan)
        task = found.group(0)
        with self.lock:
            seen = self.calls.get(task, 0)
            self.calls[task] = seen + 1
        usage: dict[str, object] = {
            "input_tokens": len(prompt) // 4, "output_tokens": 60,
            "cache_read_input_tokens": len(prompt) // 5, "settled_cents": 1,
        }
        properties = schema.get("properties", {})
        assert isinstance(properties, dict)
        path, body = self.bodies[task]
        if "-thicken-" in run_id:
            return NativeResult("ok", {"verdict": "contract", "explanation": "",
                                      "contract": body, "edits": [], "lesson": ""}, usage=usage)
        if provider == "dsh" and seen == 0 and self._share(task, "limited", self.limited):
            return NativeResult("rate_limited", {}, message="429 (synthetic)", usage={})
        if provider == "dsh":
            with self.lock:
                first = self.answers.get(task, 0) == 0
                self.answers[task] = self.answers.get(task, 0) + 1
            if first and self._share(task, "wrong", self.wrong):
                body = body.replace("return x + ", "return x - ", 1)
        return NativeResult("ok", {
            "status": "DONE", "summary": "", "reason": "", "lesson": "",
            "edits": [{"path": path, "search": "raise NotImplementedError", "replace": body}],
        }, usage=usage)
