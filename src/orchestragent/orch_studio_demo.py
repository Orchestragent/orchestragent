"""`orch studio --demo`: a scripted in-memory project for building and testing clients.

Nothing is called or spent. A Go + TypeScript ledger run moves on its own (nodes build,
fail, park and merge) and every POST of docs/STUDIO.md changes it the way the real engine
would: guidance unparks a node and it then succeeds, `rework` rebuilds a DONE node and
ripples to its dependents, a started spec becomes a new run that builds itself.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from orchestragent.orch_studio_server import StudioError

Step = tuple[float, Callable[["DemoNode"], None]]  # (seconds after the previous step, action)
VENDORS = {"claude": "anthropic", "dsh": "deepseek", "codex": "openai", "cursor": "cursor",
           "human": "person"}
NAMES = {"claude-opus-5-5": "Claude Opus 5.5", "claude-sonnet-5-5": "Claude Sonnet 5.5",
         "deepseek-flash": "DeepSeek V4.1 Flash", "gpt-6-astra": "GPT-6 Astra",
         "composer-2.5": "Cursor Composer 2.5"}
ROUTES = ["dsh/deepseek-flash:off", "dsh/deepseek-flash:low", "dsh/deepseek-flash:high",
          "claude/claude-opus-5-5:low", "claude/claude-opus-5-5:medium",
          "claude/claude-opus-5-5:high", "claude/claude-sonnet-5-5:medium",
          "codex/gpt-6-astra:medium", "codex/gpt-6-astra:high", "cursor/composer-2.5"]
VERIFIED = {"dsh/deepseek-flash:low", "claude/claude-opus-5-5:medium"}
BUILDER = "dsh/deepseek-flash:low"
PATCH = "claude/claude-opus-5-5:medium"
RUNGS = ["first try", "retry", "samples", "Claude patch", "thicken", "replan"]


def route(label: str, override: bool = False) -> dict[str, Any]:
    provider, _, rest = label.partition("/")
    model, _, effort = rest.partition(":")
    name = NAMES.get(model, model) + (f" · {effort}" if effort else "")
    return {"label": label, "provider": provider, "model": model, "effort": effort,
            "vendor": VENDORS.get(provider, "other"), "name": name,
            "verified": label in VERIFIED, **({"override": True} if override else {})}


@dataclass
class DemoNode:
    run: DemoRun
    id: str
    goal: str
    files: dict[str, str]
    reqs: dict[str, str]
    depends_on: list[str]
    contract: str
    checks: list[dict[str, Any]]
    status: str = "PLANNED"
    since: float = 0.0
    level: int = 0
    route_override: str = ""
    diagnosis: dict[str, Any] = field(default_factory=dict)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    thread: list[dict[str, Any]] = field(default_factory=list)
    usd: float = 0.0
    cny: float = 0.0
    messages: int = 0
    ripple: str = ""
    file_state: str = "stub"
    program: list[Step] = field(default_factory=list)
    next_at: float = 0.0

    # --- helpers used by scripted steps ---------------------------------------------
    def set(self, status: str) -> None:
        self.status, self.since = status, self.run.clock()

    def say(self, author: str, kind: str, text: str, attempt: int | None = None) -> None:
        self.thread.append({"id": f"{self.id}-{len(self.thread) + 1}", "at": self.run.clock(),
                            "author": author, "kind": kind, "text": text,
                            **({"attempt": attempt} if attempt is not None else {})})

    def label(self) -> str:
        return self.route_override or (PATCH if self.level == 3 else BUILDER)

    def start(self, _: DemoNode | None = None) -> None:
        n = len(self.attempts) + 1
        self.attempts.append({"n": n, "label": f"a{n}", "route": route(self.label()),
                              "rung": RUNGS[self.level], "started": self.run.clock(),
                              "ended": None, "outcome": "running", "category": "",
                              "report": "", "diff": "", "summary": ""})
        self.diagnosis = {}
        self.set("RUNNING")
        self.say("orch", "started", f"attempt a{n} → {route(self.label())['name']} "
                 f"({RUNGS[self.level]})", n)

    def verify(self, _: DemoNode | None = None) -> None:
        self.set("VERIFYING")

    def agent(self, summary: str, diff: str) -> Callable[[DemoNode], None]:
        def act(node: DemoNode) -> None:
            attempt = node.attempts[-1]
            attempt["summary"], attempt["diff"] = summary, diff
            node.file_state = "candidate"
            cost = 0.18 if attempt["route"]["provider"] == "claude" else 0.0
            node.usd += cost
            node.cny += 0.0 if cost else 0.06
            node.run.usd += cost
            node.run.cny += 0.0 if cost else 0.06
            node.say("agent", "summary", summary, attempt["n"])
        return act

    def fail(self, category: str, report: str, diagnosis: dict[str, Any],
             park: bool = False) -> Callable[[DemoNode], None]:
        def act(node: DemoNode) -> None:
            attempt = node.attempts[-1]
            attempt.update(ended=node.run.clock(), outcome="failed", category=category,
                           report=report)
            node.diagnosis = diagnosis
            node.say("orch", "failed", f"a{attempt['n']} failed ({category}): "
                     + diagnosis.get("summary", ""), attempt["n"])
            if park:
                node.set("PARKED")
                node.say("orch", "parked", "Parked: waiting for a person. Send guidance "
                         "below, retry, or switch the model.")
                node.run.notify_parked(node)
            else:
                node.level = min(node.level + 1, 5)
                node.set("READY")
        return act

    def merge(self, _: DemoNode | None = None) -> None:
        self.set("MERGING")

    def done(self, _: DemoNode | None = None) -> None:
        attempt = self.attempts[-1]
        attempt.update(ended=self.run.clock(), outcome="passed")
        self.file_state = "built"
        self.diagnosis = {}
        self.set("DONE")
        self.say("orch", "merged", f"a{attempt['n']} passed its frozen checks and merged")
        self.run.after_done(self)


def first_program(node: DemoNode) -> list[Step]:
    """A node's first build in the live demo: T-005 trips over upstream T-003, T-006 over a
    missing tool; everything else succeeds."""
    path = next(iter(node.files))
    built = _diff(path, "\tpanic(\"not implemented\")", ["\t// implementation", "\treturn nil"])
    if node.id == "T-005":
        return [(0.5, node.start), (5.0, node.agent("Wired the Go exporter into the test", built)),
                (1.0, node.verify),
                (4.0, node.fail("test", "FAIL test/T005I_ledger.integration.test.ts\n"
                                "SyntaxError: Unexpected token < in JSON at position 41\n"
                                "  exporter output: {\"balances\":{\"caf\\u00e9\":…\n"
                                "  (api/ledger/export.go writes \\u escapes the reader rejects)",
                                {"kind": "upstream", "summary": "the failure points at "
                                 "api/ledger/export.go, owned by T-003", "blamed": ["T-003"],
                                 "checks": [node.checks[0]["id"]]}, park=True))]
    if node.id == "T-006":
        return [(0.5, node.start), (4.0, node.agent("Added the export subcommand", built)),
                (1.0, node.verify),
                (3.0, node.fail("environment", "sh: 1: go: not found\n(exit 127)",
                                {"kind": "environment", "summary": "the check could not run: "
                                 "`go` was not found in the sandbox (exit 127)", "blamed": [],
                                 "checks": [node.checks[0]["id"]]}, park=True))]
    return success(node, f"Implemented {node.goal.lower()}", built)


def success(node: DemoNode, summary: str, diff: str, think: float = 6.0) -> list[Step]:
    return [(0.5, node.start), (think, node.agent(summary, diff)), (1.0, node.verify),
            (3.0, node.merge), (2.0, node.done)]


GO_EXPORT = """package ledger

// ExportJSON returns canonical JSON: balances sorted by account, entries in input order.
func ExportJSON(entries []Entry) ([]byte, error) {
\tpanic("not implemented")
}
"""
TS_LEDGER = """export interface Ledger { balances: Record<string, number>; entries: Entry[] }

export function parseExport(json: string): Ledger {
  throw new Error("not implemented");
}
"""


def _diff(path: str, removed: str, added: list[str]) -> str:
    plus = "\n".join("+" + line for line in added)
    return (f"--- a/{path}\n+++ b/{path}\n@@ -1,4 +1,{len(added) + 3} @@\n"
            f"-{removed}\n{plus}\n")


class DemoRun:
    def __init__(self, backend: DemoBackend, run_id: str, title: str, spec: str,
                 created: float) -> None:
        self.backend, self.id, self.title, self.spec = backend, run_id, title, spec
        self.created = created
        self.clock = backend.clock
        self.nodes: dict[str, DemoNode] = {}
        self.usd = 0.0
        self.cny = 0.0
        self.cancelled = False
        self.phase = "building"

    def add(self, node_id: str, goal: str, files: dict[str, str], reqs: dict[str, str],
            deps: list[str], contract: str, test_path: str, command: str) -> DemoNode:
        node = DemoNode(self, node_id, goal, files, reqs, deps, contract, [{
            "id": f"cmd:{{\"command\":\"{command}\",\"files\":[\"{test_path}\"]}}",
            "command": command,
            "files": [{"path": test_path, "content": f"// frozen tests for {node_id}\n"
                       f"// {'; '.join(reqs)}\n"}]}])
        node.since = self.clock()
        self.nodes[node_id] = node
        return node

    def schedule(self, node: DemoNode, steps: list[Step]) -> None:
        node.program = list(steps)
        node.next_at = self.clock() + (steps[0][0] if steps else 0)

    def tick(self) -> bool:
        """Run every due step; True when something changed."""
        if self.cancelled:
            return False
        changed = False
        now = self.clock()
        for node in list(self.nodes.values()):
            while node.program and node.next_at <= now:
                _, action = node.program.pop(0)
                action(node)
                changed = True
                node.next_at = now + (node.program[0][0] if node.program else 0)
        return changed

    def after_done(self, finished: DemoNode) -> None:
        """Start dependents whose dependencies are all DONE; ripple after a rework."""
        for node in self.nodes.values():
            if finished.id not in node.depends_on:
                continue
            if node.status == "PLANNED" and all(
                    self.nodes[dep].status == "DONE" for dep in node.depends_on) \
                    and not node.program:
                self.schedule(node, first_program(node))
            upstream_blamed = (node.status == "PARKED"
                               and finished.id in node.diagnosis.get("blamed", []))
            if finished.ripple == "source" and (node.status == "DONE" or upstream_blamed):
                if node.id == "T-005" or upstream_blamed:
                    node.ripple = "affected"
                    node.say("orch", "ripple", f"Upstream {finished.id} changed and 1 of its "
                             "checks now fails; reopened to rebuild against the new code.")
                    node.level = 0
                    node.set("READY")
                    self.schedule(node, success(node, "Adapted to the new export format",
                                                _diff("web/test/T005I_agree.test.ts",
                                                      "old", ["new"]), 4.0))
                else:
                    node.ripple = "unaffected"
                    node.say("orch", "ripple", f"Upstream {finished.id} changed; this node's "
                             "checks still pass, nothing to do.")
        if finished.ripple == "source":
            finished.ripple = ""
        if all(node.status in {"DONE", "REPLANNED"} for node in self.nodes.values()):
            self.phase = "finished"
            self.backend.notify_runs()

    def notify_parked(self, node: DemoNode) -> None:
        self.backend.notify_runs()

    # --- views -----------------------------------------------------------------------
    def status(self) -> str:
        states = [node.status for node in self.nodes.values()]
        if self.cancelled:
            return "cancelled"
        if not states:
            return "planning"
        if all(state in {"DONE", "REPLANNED"} for state in states):
            return "done"
        if any(state == "PARKED" for state in states) and not any(
                node.program for node in self.nodes.values()):
            return "parked"
        return "running"

    def live(self) -> bool:
        return not self.cancelled and any(node.program for node in self.nodes.values())

    def summary(self) -> dict[str, Any]:
        nodes = list(self.nodes.values())
        return {"id": self.id, "title": self.title, "status": self.status(), "live": self.live(),
                "created": self.created, "tasks": len(nodes),
                "done": sum(node.status == "DONE" for node in nodes),
                "parked": sum(node.status == "PARKED" for node in nodes),
                "cost": {"usd": round(self.usd, 4), "cny": round(self.cny, 4)}}

    def diagnosis(self, node: DemoNode) -> dict[str, Any]:
        if node.status == "DONE":
            return {"kind": "done", "summary": "merged; its checks pass", "blamed": [],
                    "checks": []}
        if node.status in {"RUNNING", "VERIFYING", "MERGING"}:
            text = {"RUNNING": "building", "VERIFYING": "running its frozen checks",
                    "MERGING": "merging; every merged node's checks run"}[node.status]
            return {"kind": "running", "summary": text, "blamed": [], "checks": []}
        if node.status == "PARKED" or (node.status == "READY" and node.diagnosis):
            return {"blamed": [], "checks": [], **node.diagnosis}
        waiting = [dep for dep in node.depends_on if self.nodes[dep].status != "DONE"]
        if waiting:
            return {"kind": "waiting", "summary": "waiting on " + ", ".join(waiting),
                    "blamed": waiting, "checks": []}
        return {"kind": "queued", "summary": "ready; waiting for a worker slot", "blamed": [],
                "checks": []}

    def node_view(self, node: DemoNode) -> dict[str, Any]:
        return {"id": node.id, "goal": node.goal, "status": node.status, "since": node.since,
                "attempts": len(node.attempts), "rung": RUNGS[node.level],
                "route": route(node.label(), bool(node.route_override)),
                "route_override": bool(node.route_override), "files": list(node.files),
                "reqs": list(node.reqs), "depends_on": node.depends_on,
                "diagnosis": self.diagnosis(node),
                "cost": {"usd": round(node.usd, 4), "cny": round(node.cny, 4)},
                "person": {"messages": node.messages, "taken": False}, "ripple": node.ripple,
                "replaced_by": []}


class DemoBackend:
    demo = True

    def __init__(self, clock: Callable[[], float] = time.time, *, tick: float = 0.25,
                 start: bool = True) -> None:
        self.clock = clock
        self.lock = threading.RLock()
        self.runs_list: dict[str, DemoRun] = {}
        self._seq: dict[str, int] = {}
        self._runs_seq = 0
        self.ids = itertools.count(1)
        self.keys: set[str] = {"dsh"}
        self.roles = {"planner": PATCH, "test_designer": BUILDER, "builder": BUILDER,
                      "reviewer": ""}
        self._seed()
        if start:
            threading.Thread(target=self._loop, args=(tick,), daemon=True).start()

    # --- scenario ---------------------------------------------------------------------
    def _seed(self) -> None:
        now = self.clock()
        old = DemoRun(self, "rdemo0001", "Add CSV import to the ledger CLI", "", now - 86400)
        for i, goal in enumerate(["Parse CSV rows", "Validate amounts", "Write report"], 1):
            node = old.add(f"T-00{i}", goal, {f"cli/step{i}.go": "package cli\n"},
                           {f"R{i}": goal}, [f"T-00{i - 1}"] if i > 1 else [], goal,
                           f"cli/step{i}_test.go", f"cd cli && go test -run '^TestT00{i}_'")
            node.status, node.file_state = "DONE", "built"
            node.attempts.append({"n": 1, "label": "a1", "route": route(BUILDER),
                                  "rung": "first try", "started": now - 86000,
                                  "ended": now - 85900, "outcome": "passed",
                                  "category": "", "report": "", "diff": "", "summary": goal})
        old.cny = 1.42
        self.runs_list[old.id] = old
        self._live_run(now)

    def _live_run(self, now: float) -> None:
        run = DemoRun(self, "rdemo0002", "Polyglot ledger: Go service + TypeScript client",
                      "R1. api/ledger (Go): ParseCSV …\nR2. …", now - 420)
        self.runs_list[run.id] = run
        t1 = run.add("T-001", "ParseCSV reads date,account,amount_cents,memo rows",
                     {"api/ledger/parse.go": "package ledger\n"},
                     {"R1": "ParseCSV(r io.Reader) ([]Entry, error); any invalid row returns "
                      "an error starting \"line N: \"."}, [],
                     "ParseCSV uses encoding/csv; header must be exact; …",
                     "api/ledger/t001_parse_test.go",
                     "cd api && go test ./ledger -run '^TestT001_' -count=1")
        t2 = run.add("T-002", "Balances sums amounts per account",
                     {"api/ledger/balances.go": "package ledger\n"},
                     {"R2": "Balances(entries) returns one key per account …"}, ["T-001"],
                     "Balances returns a new non-nil map …", "api/ledger/t002_balances_test.go",
                     "cd api && go test ./ledger -run '^TestT002_' -count=1")
        t3 = run.add("T-003", "ExportJSON writes canonical JSON",
                     {"api/ledger/export.go": GO_EXPORT},
                     {"R3": "ExportJSON(entries) returns canonical JSON with sorted balances, "
                      "no whitespace, version 1."}, ["T-001", "T-002"],
                     "ExportJSON validates each entry, then writes {\"balances\":…,"
                     "\"entries\":…,\"version\":1} with no whitespace …",
                     "api/ledger/t003_export_test.go",
                     "cd api && go test ./ledger -run '^TestT003_' -count=1")
        t4 = run.add("T-004", "parseExport reads the export in TypeScript",
                     {"web/src/ledger.ts": TS_LEDGER},
                     {"R4": "parseExport(json) throws 'invalid ledger: …' on anything else.",
                      "R5": "amount_cents must be a safe integer."}, [],
                     "parseExport runs JSON.parse; exactly the keys balances, entries, version …",
                     "web/src/T004_ledger.test.ts",
                     "cd web && ./node_modules/.bin/vitest run src/T004_ledger.test.ts")
        t5 = run.add("T-005", "Go export and TypeScript reader agree byte for byte",
                     {"web/test/T005I_agree.test.ts": "// owned integration glue\n"},
                     {"R7": "parseExport(ExportJSON(x)) round-trips every valid ledger."},
                     ["T-003", "T-004"], "Runs the Go exporter and parses its output …",
                     "web/test/T005I_ledger.integration.test.ts",
                     "cd web && ./node_modules/.bin/vitest run test/T005I_ledger.integration"
                     ".test.ts")
        t6 = run.add("T-006", "CLI wrapper prints the export",
                     {"api/cmd/ledger/main.go": "package main\n"},
                     {"R6": "`ledger export FILE` prints ExportJSON of the parsed file."},
                     ["T-003"], "main reads the CSV path from argv …",
                     "api/cmd/ledger/t006_main_test.go",
                     "cd api && go test ./cmd/ledger -run '^TestT006_' -count=1")
        for node, minutes in ((t1, 6.5), (t2, 5.0)):
            node.start()
            node.attempts[-1]["started"] = now - minutes * 60
            node.agent(f"Implemented {node.goal}", _diff(next(iter(node.files)),
                       "\tpanic(\"not implemented\")", ["\t// implementation", "\treturn nil"])
                       )(node)
            node.done()
        t2.thread.insert(0, {"id": "T-002-0", "at": now - 330, "author": "orch",
                             "kind": "failed", "text": "a1 failed (test): 1 check failed",
                             "attempt": 1})
        # T-004: parked on its own failing checks; waits for a person.
        t4.start()
        t4.agent("Parsed with JSON.parse and checked the three keys",
                 _diff("web/src/ledger.ts", "  throw new Error(\"not implemented\");",
                       ["  const value = JSON.parse(json);", "  return value as Ledger;"]))(t4)
        t4.fail("test", "FAIL src/T004_ledger.test.ts > T004_R5_unsafe_integer\n"
                "AssertionError: expected [Function] to throw 'invalid ledger: …'\n"
                "  ❯ src/T004_ledger.test.ts:41:32\n\nTests  1 failed | 11 passed (12)",
                {"kind": "own", "summary": "1 of its own checks fails: T004_R5_unsafe_integer "
                 "(amount_cents beyond 2^53 is accepted)", "blamed": [],
                 "checks": [t4.checks[0]["id"]]})(t4)
        t4.level = 3
        t4.start()
        t4.agent("Still returns the value without checking Number.isSafeInteger",
                 _diff("web/src/ledger.ts", "  return value as Ledger;",
                       ["  validate(value);", "  return value as Ledger;"]))(t4)
        t4.fail("test", "Tests  1 failed | 11 passed (12)", {
            "kind": "own", "summary": "1 of its own checks fails: T004_R5_unsafe_integer",
            "blamed": [], "checks": [t4.checks[0]["id"]]}, park=True)(t4)
        # T-003 builds now: fails once, then a Claude patch passes.
        run.schedule(t3, [
            (1.0, t3.start),
            (5.0, t3.agent("Wrote balances with fmt.Sprintf", _diff(
                "api/ledger/export.go", "\tpanic(\"not implemented\")",
                ["\tvar b strings.Builder", "\t// …", "\treturn []byte(b.String()), nil"]))),
            (1.0, t3.verify),
            (3.0, t3.fail("test", "--- FAIL: TestT003_R3_escapes_html (0.00s)\n"
                          "    export_test.go:88: got \"\\u003c\" want \"<\"\nFAIL",
                          {"kind": "own", "summary": "TestT003_R3_escapes_html fails: HTML "
                           "characters are escaped", "blamed": [], "checks": []})),
            (2.0, lambda node: setattr(node, "level", 3)),
            (0.5, t3.start),
            (8.0, t3.agent("Used an encoder with SetEscapeHTML(false)", _diff(
                "api/ledger/export.go", "\tvar b strings.Builder",
                ["\tenc := json.NewEncoder(&buf)", "\tenc.SetEscapeHTML(false)"]))),
            (1.0, t3.verify), (3.0, t3.merge), (2.0, t3.done)])
        # T-005 waits on T-003 and T-004; T-006 on T-003 (see first_program).
        del t5, t6

    # --- loop -----------------------------------------------------------------------
    def _loop(self, tick: float) -> None:
        while True:
            time.sleep(tick)
            self.advance()

    def advance(self) -> None:
        with self.lock:
            for run in self.runs_list.values():
                if run.tick():
                    self._seq[run.id] = self._seq.get(run.id, 0) + 1

    def notify_runs(self) -> None:
        self._runs_seq += 1

    def _bump(self, run: DemoRun) -> None:
        self._seq[run.id] = self._seq.get(run.id, 0) + 1

    def _run(self, run_id: str) -> DemoRun:
        if run_id not in self.runs_list:
            raise StudioError("not_found", f"no run {run_id}")
        return self.runs_list[run_id]

    def _node(self, run_id: str, task: str) -> tuple[DemoRun, DemoNode]:
        run = self._run(run_id)
        if task not in run.nodes:
            raise StudioError("not_found", f"no node {task} in {run_id}")
        return run, run.nodes[task]

    # --- Backend ----------------------------------------------------------------------
    def boot(self) -> dict[str, Any]:
        return {"version": "demo", "repo": "/demo/polyglot-ledger", "repo_name": "polyglot-ledger",
                "platform": "darwin", "demo": True, "runs": self.runs()}

    def runs(self) -> list[dict[str, Any]]:
        with self.lock:
            return sorted((run.summary() for run in self.runs_list.values()),
                          key=lambda item: -item["created"])

    def seq(self, run: str) -> int:
        return self._seq.get(run, 0)

    def runs_seq(self) -> int:
        return self._runs_seq

    def graph(self, run_id: str) -> dict[str, Any]:
        with self.lock:
            run = self._run(run_id)
            head = run.summary()
            head.update(started=run.created, elapsed_s=round(self.clock() - run.created, 1),
                        phase=run.phase)
            return {"run": head, "nodes": [run.node_view(node) for node in run.nodes.values()],
                    "edges": [{"from": dep, "to": node.id} for node in run.nodes.values()
                              for dep in node.depends_on]}

    def node(self, run_id: str, task: str) -> dict[str, Any]:
        with self.lock:
            run, node = self._node(run_id, task)
            return {"node": run.node_view(node), "contract": node.contract,
                    "requirements": [{"id": rid, "text": text} for rid, text in node.reqs.items()],
                    "files": [{"path": path, "content": content, "state": node.file_state}
                              for path, content in node.files.items()],
                    "checks": node.checks, "attempts": node.attempts, "thread": node.thread,
                    "routes": [route(label) for label in ROUTES
                               if label.split("/")[0] in self.keys | {"claude"}]}

    def message(self, run_id: str, task: str, text: str, rework: bool) -> dict[str, Any]:
        with self.lock:
            run, node = self._node(run_id, task)
            if run.cancelled or node.status == "REPLANNED":
                raise StudioError("conflict", f"{task} can no longer change")
            node.messages += 1
            node.say("you", "message", text.strip())
            if node.status == "PARKED" or (node.status == "DONE" and rework):
                if node.status == "DONE":
                    node.ripple = "source"
                node.level = 0
                node.diagnosis = {}
                node.set("READY")
                path = next(iter(node.files))
                run.schedule(node, success(node, "Followed your guidance: " + text.strip()[:80],
                                           _diff(path, "  return value as Ledger;",
                                                 ["  if (!Number.isSafeInteger(x)) throw …",
                                                  "  return value as Ledger;"]), 5.0))
            self._bump(run)
            return run.node_view(node)

    def route(self, run_id: str, task: str, label: str) -> dict[str, Any]:
        with self.lock:
            run, node = self._node(run_id, task)
            if label and label not in ROUTES:
                raise StudioError("invalid", f"unknown route {label}")
            node.route_override = label
            node.say("orch", "route", ("next attempts use " + route(label)["name"]) if label
                     else "back to the role default model")
            self._bump(run)
            return run.node_view(node)

    def retry(self, run_id: str, task: str) -> dict[str, Any]:
        with self.lock:
            run, node = self._node(run_id, task)
            if node.status != "PARKED":
                raise StudioError("conflict", f"{task} is {node.status}, not PARKED")
            node.level, node.diagnosis = 0, {}
            node.set("READY")
            node.say("orch", "note", "retry requested: fresh ladder")
            steps = success(node, "Retried from scratch", "")
            steps[1] = (5.0, node.fail("test", "Tests  1 failed | 11 passed (12)", {
                "kind": "own", "summary": "the same check still fails", "blamed": [],
                "checks": []}, park=True))
            run.schedule(node, steps[:2])
            self._bump(run)
            return run.node_view(node)

    def start(self, spec: str) -> dict[str, Any]:
        with self.lock:
            run_id = f"rdemo{next(self.ids) + 100:04d}"
            title = next((line.strip() for line in spec.splitlines() if line.strip()),
                         "New run")[:80]
            run = DemoRun(self, run_id, title, spec, self.clock())
            run.phase = "planning"
            self.runs_list[run_id] = run
            reqs = [line.strip() for line in spec.splitlines() if line.strip()][:6] or ["R1."]
            previous = ""
            for i, text in enumerate(reqs, 1):
                node = run.add(f"T-{i:03d}", text[:60], {f"src/part{i}.txt": "stub\n"},
                               {f"R{i}": text}, [previous] if previous and i % 2 else [],
                               text, f"tests/t{i:03d}_test.sh", f"sh tests/t{i:03d}_test.sh")
                previous = node.id
            run.phase = "building"
            for node in run.nodes.values():
                if not node.depends_on:
                    run.schedule(node, success(node, f"Implemented {node.goal}", ""))
            self.notify_runs()
            return run.summary()

    def resume(self, run_id: str) -> dict[str, Any]:
        with self.lock:
            run = self._run(run_id)
            run.cancelled = False
            self._bump(run)
            self.notify_runs()
            return run.summary()

    def cancel(self, run_id: str) -> dict[str, Any]:
        with self.lock:
            run = self._run(run_id)
            run.cancelled = True
            self._bump(run)
            self.notify_runs()
            return run.summary()

    def settings(self) -> dict[str, Any]:
        providers = [
            {"name": "claude", "vendor": "anthropic", "title": "Claude (Claude Code subscription)",
             "kind": "subscription", "configured": True, "detail": "logged in",
             "login": {"command": "claude", "then": "/login"}},
            {"name": "dsh", "vendor": "deepseek", "title": "DeepSeek (API key)", "kind": "key",
             "configured": "dsh" in self.keys, "detail": "key saved" if "dsh" in self.keys
             else "no key", "login": None},
            {"name": "codex", "vendor": "openai", "title": "Codex (ChatGPT subscription)",
             "kind": "subscription", "configured": "codex" in self.keys,
             "detail": "logged in" if "codex" in self.keys else "not logged in",
             "login": {"command": "codex login", "then": ""}},
            {"name": "cursor", "vendor": "cursor", "title": "Cursor (subscription)",
             "kind": "subscription", "configured": False, "detail": "CLI not installed",
             "login": {"command": "cursor-agent login", "then": ""}},
        ]
        roles = [{"role": role, "title": role.replace("_", " "),
                  "description": {"planner": "splits the spec into tasks and interfaces",
                                  "test_designer": "writes each task's frozen tests",
                                  "builder": "implements tasks against the frozen tests",
                                  "reviewer": "checks each test shard (off by default)"}[role],
                  "route": route(label) if label else None,
                  "default": route({"planner": PATCH, "reviewer": ""}.get(role, BUILDER))
                  if role != "reviewer" else None}
                 for role, label in self.roles.items()]
        return {"providers": providers, "roles": roles,
                "routes": [route(label) for label in ROUTES]}

    def set_key(self, provider: str, key: str) -> dict[str, Any]:
        if provider not in {"dsh", "codex"}:
            raise StudioError("invalid", f"{provider} does not take a key here")
        with self.lock:
            self.keys.add(provider)
            self.notify_runs()
        return next(item for item in self.settings()["providers"] if item["name"] == provider)

    def set_role(self, role: str, label: str) -> dict[str, Any]:
        if role not in self.roles:
            raise StudioError("invalid", f"unknown role {role}")
        if label and label not in ROUTES:
            raise StudioError("invalid", f"unknown route {label}")
        with self.lock:
            self.roles[role] = label
        return {}
