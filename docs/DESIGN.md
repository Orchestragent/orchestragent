# How orch is built, and why it is built this way

orch plans a build with a strong model, implements it in parallel with cheap models, and
merges only code that passes tests the implementer did not write. This document is about
the one design rule that holds it together, because the first versions broke it and paid
for it.

## The rule: code for invariants, models for judgment

Early orch encoded judgment as code. When a build failed it climbed a fixed ladder (retry,
samples, stronger model, thicker contract, replan, park) whatever the failure was. When
independently written tests collided it detected the collision, asked for renames, and
retried a bounded number of rounds. When a replan looked wrong it was rejected by a lint
that judged "contribution". Every failed run taught us a new case and we added a rule,
a check or a prompt sentence for it. Each addition fixed that run and made the system
rigid in a new way: the prompt for test designers grew to nineteen incident-shaped
sentences, the engine to 3,500 lines and forty tunable limits, and a five-language
project still failed where a single strong model succeeded. That is overfitting: one
parameter per training example.

The cause was structural. Nothing in the system could *look at a situation and decide*.
The planner judged once, at the start; afterwards everything was mechanical, so every
deviation from the plan had to be anticipated in code. Code generalizes only as far as
its author's imagination; a strong model reading evidence generalizes much further.

So orch now splits every behaviour into one of two kinds:

**Invariants** are mechanical truths, enforced in code, few in number:

- the sandbox: checks run offline, isolated, with read-only toolchains, from a fresh
  `$TMPDIR` seeded only with what a check produced on the stubs before any builder existed,
  and that `$TMPDIR` is short, because programs bind Unix sockets under it and a socket
  path has a 104-108 byte limit the worktree's own path would exceed;
- ownership: a builder's change may touch only the files its task owns; a person's
  `orch submit` is held to the same rule;
- merge on green: a task merges only when its checks and those its change could break
  pass; the final gate runs every check and each component's own test command; a
  regression is bisected to its commit and reverted, and a component suite that fails on
  the finished result goes, with the output, to the supervisor of the task whose tests it
  names (a test that only holds under its own check command is a test problem, not a park);
- red on stubs: a test that passes before any implementation exists tests nothing and is
  refused — this is a real oracle, not a rule of thumb;
- the record: every call, state change and cost is durable; a run resumes without
  guessing which call was billed;
- a command that cannot run (exit 126/127) is an environment problem, never the code's.

**Judgment** is everything else, and lives in models with as little scaffolding as the
invariants need:

- the planner splits the spec into components and tasks, writes contracts in the spec's
  words, declares each component's normal test command, and writes the integration tests
  where tasks must agree — the author of a contract is the one who tests that both sides
  honour it;
- one test designer per component writes all of that component's tests, so they cohere
  with each other and with the component's runner; it reads principles (the spec is the
  only authority; prefer oracles the spec states, properties and relations, real
  dependencies, in that order; tests are part of the project), not a list of past mishaps;
- a builder implements one task in its owned files, and may dispute a test against the
  spec instead of coding to it;
- the supervisor is called when cheap attempts fail or a builder reports a dispute. It
  reads the evidence — spec, contract, tests, the attempts and their output — and picks
  one action from a closed menu: retry with guidance, escalate to a stronger model, revise
  the tests (they must still fail on the stubs), revise the contract, split the task, call
  it an environment problem, or give up to a person. The situations are open; only the
  menu is code. A new kind of failure needs no new code.

Only these park a task, and a new one needs the same justification: a person chose a
model that cannot be used; a provider call's outcome (and bill) is unknown; saved code
fails its provenance check; the supervisor answered environment or give up, or used its
three decisions; an infrastructure fault; a person took the task over. Everything else
that goes wrong (tests, merges, an answer cut at the output limit, a spent context budget,
a test revision that breaks merged work) is an attempt's failure, and the cheap retry and
the supervisor see it like any other. There is no rule that parks healthy tasks because
their neighbours failed.

Nothing is frozen any more. Tests are *owned* by their designer and may be revised with
evidence; builders simply cannot edit them. That keeps the one property freezing was for
(the implementer does not grade its own work) without the property that sank it (a wrong
test becomes law).

## What a model sees, and how it sees more

Earlier versions compiled the repository into a symbol map (tree-sitter grammars, an import
graph, an impact analysis) and handed models the digest. That is a compiler's view, built
on guesses about what the model would need; it cost seven grammar dependencies, 600 lines,
and was never measured. Models read code the way people do: list, find, grep, open.

So the repository is served, not pre-digested. Every model gets a directory listing with
sizes and the files the spec names outright, plus three requests it can make at any point
— `find` (a path pattern), `search` (a word or regular expression, with neighbouring
lines), `read` (a file or a line range) — all answered from git at the exact revision it is
working on. The planner looks around before planning; a builder asks mid-attempt
(`NEED_CONTEXT`); the supervisor runs a command in the sandbox (`investigate`). A regular
expression chosen by the model for one situation is not a rule baked into orch; nothing
here is tuned to a language or a project. Semantic search is not built in: for the
repositories orch targets, exact search is enough, and adding an index must again earn
its place in the benchmark.

## The discipline that keeps it this way

- No sentence in a base prompt describes one past run. A failure becomes a case in the
  benchmark suite (`scripts/orch_gate.py`, `examples/orch-bench/`); the question is then
  whether the supervisor handles it from evidence. Only a missing *principle* justifies a
  prompt change.
- Every change runs the whole suite; a case whose pass count drops blocks the change.
- After a redesign, a project the design never saw must pass with zero code changes.
- Roles stay switchable so alternatives can be measured rather than argued: the verifier
  role can be turned off to compare "builders test themselves plus the planner's
  integration checks" against independent design.

## What a person sees

`orch studio` and the macOS app show each task as a node with its state, model and a
diagnosis (own failure, upstream, test dispute, environment, waiting). A person can give a
node guidance, switch its model, take it over and submit, or rework a finished node — and
only that node's dependents are rebuilt, only if their checks now fail. The same engine
and gates serve the CLI, MCP and the studio; nothing a person does bypasses a check.
