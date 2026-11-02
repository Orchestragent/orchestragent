"""Summarize saved measurements without provider calls; commit this script before use."""

import argparse
import json
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from orchestragent.envelope import digest
from orchestragent.orch_bench import read_report
from orchestragent.orch_state import OrchState
from orchestragent.orch_usage import Call, tokens, unresolved
from orchestragent.orch_usage import summary as usage_summary
from orchestragent.serialization import dumps
from orchestragent.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ScaleGroup:
    tasks: int
    jobs: int
    repeats: int
    seconds: list[float]
    median_seconds: float
    median_tasks_per_min: float
    audit_findings: int
    done: int
    revisions: list[str]


def uncached_input(calls: list[Call]) -> int | None:
    """Input not read from cache (writes included); null unless every call reported both."""
    totals = usage_summary(calls)
    known, coverage = (cast(dict[str, int], totals[key])
                       for key in ("known_tokens", "reported_calls"))
    if coverage["input_tokens"] == coverage["cache_read_tokens"] == len(calls):
        return known["input_tokens"] - known["cache_read_tokens"]
    return None


def receipt_time(calls: list[Call]) -> dict[str, Any]:
    """Summed receipt seconds, null unless every receipt finished; never a guessed zero."""
    durations = [call.finished_at - call.started_at for call in calls
                 if call.finished_at is not None and call.finished_at >= call.started_at]
    return {"timed_calls": len(durations),
            "receipt_seconds": sum(durations) if len(durations) == len(calls) else None}


def stage_usage(calls: list[Call]) -> dict[str, dict[str, Any]]:
    """Provider-reported usage per route and purpose, e.g. Opus plan versus Opus review.

    receipt_seconds sums each receipt; span_seconds is first start to last finish, so
    concurrent shards are not double counted. Unresolved receipts stay visible.
    """
    stages: dict[str, list[Call]] = {}
    for call in calls:
        stages.setdefault(f"{call.provider}/{call.model}:{call.purpose}", []).append(call)
    result = {}
    for key, items in sorted(stages.items()):
        finished = [c for c in items if c.finished_at is not None]
        result[key] = {
            **usage_summary(items), "uncached_input_tokens": uncached_input(items),
            "efforts": sorted({c.effort for c in items}),
            "scopes": sorted({c.scope for c in items}),
            **receipt_time(items),
            "span_seconds": (max(cast(float, c.finished_at) for c in finished)
                             - min(c.started_at for c in finished)) if finished else None,
            "unresolved_calls": sum(unresolved(c.outcome, c.usage) for c in items),
        }
    return result


# Conservative window from the API's default 5-minute TTL. Provider TTL runs from the
# START of the request that read or wrote the cache, generation time included. Not a
# measured Claude Code setting and not a hit guarantee.
CACHE_TTL_S = 300


def cross_role_followups(calls: list[Call]) -> dict[str, Any]:
    """Claude execution calls following a different role on the same task.

    The earlier request must have finished before the follow-up starts, and the follow-up
    must start within CACHE_TTL_S of the earlier request's start (request_age_s). Later
    reads may have refreshed older prefixes, but without proof of an identical request
    prefix that is not assumed. Cache writes of the follow-ups bound what a shared
    same-task prefix could address: an opportunity count, not a measured saving.
    """
    claude = sorted((c for c in calls if c.provider == "claude" and c.scope != "planning"
                     and c.task_id), key=lambda c: c.started_at)
    pairs = []
    for index, call in enumerate(claude):
        previous = [p for p in claude[:index] if p.task_id == call.task_id
                    and p.purpose != call.purpose and p.finished_at is not None
                    and p.finished_at <= call.started_at
                    and call.started_at - p.started_at <= CACHE_TTL_S]
        if previous:
            earlier = previous[-1]
            pairs.append({"task": call.task_id, "from": earlier.purpose,
                          "to": call.purpose, "call_id": call.id,
                          "gap_s": call.started_at - cast(float, earlier.finished_at),
                          "request_age_s": call.started_at - earlier.started_at,
                          "later_cache_write_tokens":
                              tokens(call.provider, call.usage).cache_write_tokens})
    known = [p["later_cache_write_tokens"] for p in pairs
             if p["later_cache_write_tokens"] is not None]
    return {"pairs": pairs, "ttl_assumption_s": CACHE_TTL_S,
            "ttl_clock": "earlier request start",
            "later_cache_write_tokens": sum(known) if len(known) == len(pairs) else None}


def planning_mode(saved: dict[str, Any]) -> str:
    if saved.get("single_agent"):
        return "single-agent"
    if saved.get("reviewed_boundary"):
        return "experimental-dsf-draft-opus-review"
    return "opus-boundary-dsf-tests" if saved.get("distributed") else "opus-full-plan"


def repository_summary(paths: list[Path]) -> dict[str, Any]:
    """Compare saved repository trials, keeping incomplete work and interventions visible."""
    trials = []
    inputs = []
    for path in paths:
        saved = json.loads(path.read_text())
        calls = [Call(**{key: value for key, value in row.items() if key != "tokens"})
                 for row in saved["usage"]["calls"]]
        totals = usage_summary(calls)
        tree = Workspace.git(path.parent / "project", "rev-parse",
                             f"{saved['evaluation_base']}^{{tree}}")
        if saved.get("evaluation_tree", tree) != tree:
            raise ValueError("Evaluation tree changed")
        inputs.append({**{k: saved[k] for k in (
            "source_revision", "spec_digest", "acceptance_digests", "sandbox_overlay_digest",
            "environment_fingerprint")}, "evaluation_tree": tree,
                       "sandbox_canary_digest": saved.get("sandbox_canary_digest"),
                       "reference_digests": saved.get("reference_digests", {}),
                       "project_test_wall_limit_s": saved.get("project_test_wall_limit_s", 25),
                       "project_test_cpu_limit_s": saved.get("project_test_cpu_limit_s", 15)})
        trials.append({"path": str(path), "report_digest": digest(path.read_text()),
                       "engine_revision": saved["engine_revision"],
                       "resume_engine_revision": saved.get("resume_engine_revision"),
                       "single_agent": saved.get("single_agent", False),
                       "reviewed_boundary": saved.get("reviewed_boundary", False),
                       "planning_mode": planning_mode(saved),
                       "planning_session": saved.get("planning_session"),
                       "integration_scope": saved.get("integration_scope"),
                       "status": saved["status"],
                       "acceptance_passed": saved.get("acceptance_passed"),
                       "wall_s": saved["wall_s"],
                       "active_s": saved.get("active_wall_s", saved["wall_s"]),
                       "resume_history": saved.get("resume_history", []),
                       "phases": saved.get("phases", {}), "usage": totals,
                       "uncached_input_tokens": uncached_input(calls),
                       "stages": stage_usage(calls),
                       "claude_cross_role_followups": cross_role_followups(calls),
                       "by_provider": {provider: usage_summary([c for c in calls
                                                                 if c.provider == provider])
                                       for provider in sorted({c.provider for c in calls})},
                       "event_counts": saved.get("event_counts", {}),
                       "later_calls": saved.get("calls_after_first_attempt"),
                       "identical_request_repeats": saved.get("identical_request_repeats"),
                       "errors": saved.get("errors", []),
                       "unresolved_call_ids": saved.get("unresolved_call_ids", [])})
    matched = all(item == inputs[0] for item in inputs)
    baseline = next((t for t in trials if t["single_agent"]), None)
    for trial in trials:
        valid = (matched and baseline and baseline["status"] == trial["status"] == "done"
                 and baseline["acceptance_passed"] is True and trial["acceptance_passed"] is True
                 and not trial["resume_history"] and not trial["unresolved_call_ids"]
                 and trial["wall_s"] > 0 and not trial["single_agent"])
        trial["observed_wall_speedup"] = baseline["wall_s"] / trial["wall_s"] if valid else None
    return {"script_revision": Workspace.git(ROOT, "rev-parse", "HEAD"),
            "matched_inputs": matched, "input_snapshots": inputs, "trials": trials,
            "limits": "Historical single-agent baseline; one sample per route, uncontrolled "
            "provider cache and load. No causal or general speedup claim. CLI estimates are "
            "not subscription bills; CNY settlements are not invoices. Missing metrics stay null."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", type=Path)
    parser.add_argument("--context", type=Path)
    parser.add_argument("--repository", type=Path, action="append", default=[])
    parser.add_argument("--bench", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.with_suffix(".json").exists():
        parser.error("Use a new output path; preserve previous measurement reports")
    if Workspace.git(ROOT, "status", "--porcelain", "--", "scripts/report_overnight.py"):
        parser.error("Commit report script before use")
    if args.repository:
        if args.scale or args.context or args.bench:
            parser.error("Repository summaries cannot mix with synthetic/suite inputs")
        result = repository_summary(args.repository)
        args.output.write_text(dumps(result) + "\n")
        return
    if args.scale is None or args.context is None:
        parser.error("Provide --scale and --context, or --repository reports")
    rows = json.loads(args.scale.read_text())
    context = json.loads(args.context.read_text())
    if not rows or not all(row["synthetic"] is True for row in rows):
        parser.error("Expected explicitly synthetic scale results")
    groups = []
    for tasks, jobs in sorted({(row["tasks"], row["jobs"]) for row in rows}):
        selected = [row for row in rows if (row["tasks"], row["jobs"]) == (tasks, jobs)]
        seconds = [row["execute_s"] for row in selected]
        groups.append(ScaleGroup(
            tasks, jobs, len(selected), seconds, statistics.median(seconds),
            statistics.median(row["tasks_per_min"] for row in selected),
            sum(row["audit_findings"] for row in selected),
            sum(row["status"] == "done" for row in selected),
            sorted({row["engine_revision"] for row in selected})))
    benchmarks: list[dict[str, Any]] = []
    for path in args.bench:
        report = cast(dict[str, Any], read_report(path))
        scheduler_metrics: dict[str, dict[str, object] | None] = {}
        for trial in report["trials"]:
            state = Path(trial["repo"]) / ".git" / "orch" / "state.sqlite"
            events = (OrchState(state).events(trial["run_id"])
                      if state.is_file() and trial["run_id"] else [])
            scheduler_metrics[trial["id"]] = next(
                (event.data for event in reversed(events) if event.kind == "run.metrics"), None)
        calls = [Call(**{key: value for key, value in call.items() if key != "tokens"})
                 for trial in report["trials"] for call in trial["usage"]["calls"]]
        provider_usage = {}
        for provider in sorted({call.provider for call in calls}):
            selected = [call for call in calls if call.provider == provider]
            provider_usage[provider] = {**usage_summary(selected), **receipt_time(selected)}
        benchmarks.append({"path": str(path), "suite_hash": report["suite_hash"],
                           "mode": report["mode"], "config": report["config"],
                           "case_kind": report.get("case_kind", "synthetic" if all(
                               case.get("mock_shape") is not None
                               for case in report["suite_snapshot"]["cases"]) else "curated"),
                           "passed_trials": report["passed_trials"],
                           "expected_trials": report["expected_trials"],
                           "success_rate": report["success_rate"], "totals": report["totals"],
                           "provider_usage": provider_usage,
                           "api_accounting": report["api_accounting"],
                           "trials": [{**{key: trial[key] for key in (
                               "id", "status", "elapsed_seconds", "base_revision",
                               "result_revision", "input_hash", "acceptance_hash", "dispatches",
                               "usage")}, "scheduler_metrics": scheduler_metrics[trial["id"]]}
                                      for trial in report["trials"]]})
    summary: dict[str, Any] = {
        "script_revision": Workspace.git(ROOT, "rev-parse", "HEAD"),
        "source_hashes": {str(path): digest(path.read_text()) for path in
                          (args.scale, args.context)},
        "scale": [asdict(group) for group in groups], "context": context,
        "benchmarks": benchmarks,
    }
    speedups = []
    for group in groups:
        serial = next((item for item in groups if item.tasks == group.tasks and item.jobs == 1),
                      None)
        if (serial is not None and group.jobs > 1 and group.median_seconds > 0
                and serial.revisions == group.revisions):
            speedups.append({"tasks": group.tasks, "jobs": group.jobs,
                             "ratio_of_medians": serial.median_seconds / group.median_seconds})
    summary["synthetic_parallel_speedups"] = speedups
    lines = ["# Overnight measurements", "",
             "Synthetic scheduler throughput, deterministic context packing and real model",
             "task acceptance are separate measurements. No universal intelligence claim.", "",
             "## Synthetic scheduler", "",
             "Provider behavior is simulated; Git, sandbox gates and audit are real.",
             "", "| Tasks | Jobs | Repeats | Execution seconds | Median seconds | Audit findings |",
             "| --- | --- | --- | --- | --- | --- |"]
    lines += [f"| {g.tasks} | {g.jobs} | {g.repeats} | {g.seconds} | "
              f"{g.median_seconds:.2f} | {g.audit_findings} |" for g in groups]
    commits = sorted({sha for g in groups for sha in g.revisions})
    lines += ["", *[f"{row['tasks']} tasks, {row['jobs']} slots: "
                    f"{row['ratio_of_medians']:.2f}x serial speed (ratio of medians, synthetic)."
                    for row in speedups]]
    lines += ["", "Source commits: " + ", ".join(commits),
              "", "## Context packing", "",
              f"Baseline `{context['baseline']}`; candidate `{context['candidate']}`.",
              "This measures source retention in bytes, not token savings or task success.", "",
              "| Budget | Retry | Old snippets | New snippets | Old/new bytes | Same prefix |",
              "| --- | --- | --- | --- | --- | --- |"]
    for row in context["rows"]:
        before, after = row["baseline"], row["candidate"]
        lines.append(f"| {row['budget_bytes']} | {row['retry']} | {before['retained_snippets']} | "
                     f"{after['retained_snippets']} | {before['prompt_bytes']}/"
                     f"{after['prompt_bytes']} | {row['same_prefix']} |")
    lines += ["", "## Real model receipts", "",
              "Incomplete suites have no success rate. Unknown usage stays unknown; known totals",
              "are partial when coverage is incomplete. API ledger settlements/reservations are",
              "conservative accounting, not provider invoices. Claude estimates are not extra",
              "subscription charges. Receipts include locally rejected dispatch attempts.",
              "Full configurations and call receipts are in the paired JSON.", "",
              "Times below sum per-trial wall time; they are not provider-only latency.",
              "Strategy/concurrency comparisons require matching suites and engine hashes.",
              "Before/after implementation comparisons must identify the changed engine hashes.",
              "Consider differences in routes, effort, jobs and limits. Independently planned runs",
              "do not isolate",
              "the causal effect of parallelism; task plans may differ.", "",
              "| Run | Strategy | Context | Jobs | Passed/expected | Complete | "
              "Trial seconds | Tokens |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for report in benchmarks:
        lines.append(f"| {report['path']} | {report['config'].get('strategy', 'opus-dsf')} | "
                     f"{report['config'].get('context_mode', 'full')} | "
                     f"{report['config']['jobs']} | "
                     f"{report['passed_trials']}/{report['expected_trials']} | "
                     f"{report['success_rate'] is not None} | "
                     f"{sum(trial['elapsed_seconds'] for trial in report['trials']):.2f} | "
                     f"{report['totals']['known_tokens']['total_tokens']} |")
    for report in benchmarks:
        lines += ["", f"### {report['path']}", "",
                  f"Suite `{report['suite_hash']}`; mode={report['mode']}; "
                  f"case kind={report['case_kind']}.",
                  "Engine source hash `"
                  f"{report['config'].get('engine_source_hash', 'unrecorded')}`.",
                  f"Acceptance {report['passed_trials']}/{report['expected_trials']}; "
                  f"success rate={report['success_rate']}.", "",
                  "Context mode: " + str(report['config'].get('context_mode', 'full'))
                  + "; configured scoped request limit: "
                  + str(report['config'].get('context_bytes')) + " bytes.",
                  "Ledger request bytes below measure the canonical prompt/schema envelope; "
                  "this differs from provider task-text bytes and is not a token count.", "",
                  "| Trial | Status | Seconds | Receipts | Known total tokens | Token coverage | "
                  "Max ledger request bytes |",
                  "| --- | --- | --- | --- | --- | --- | --- |"]
        for trial in report["trials"]:
            totals = trial["usage"]["totals"]
            largest = max((call['request_bytes'] for call in trial['usage']['calls']), default=0)
            lines.append(f"| {trial['id']} | {trial['status']} | {trial['elapsed_seconds']:.2f} | "
                         f"{totals['calls']} | {totals['known_tokens']['total_tokens']} | "
                         f"{totals['reported_calls']['total_tokens']}/{totals['calls']} | "
                         f"{largest} |")
        totals = report["totals"]
        lines += ["", "Known token totals: `" + dumps(totals["known_tokens"]) + "`.",
                  "Per-field receipt coverage: `" + dumps(totals["reported_calls"]) + "`.",
                  f"Cache hit rate: {totals['cache_hit_rate']} (null means unavailable).",
                  "", "| Trial | Planner | Test design | Builder | Builder retries | "
                  "Opus interventions | Sonnet interventions | "
                  "Single-model | Local rejections |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for trial in report["trials"]:
            calls = trial["dispatches"]
            lines.append(f"| {trial['id']} | " + " | ".join(str(calls[key]) for key in (
                "planner", "test_design", "builder", "builder_retries", "opus_interventions",
                "sonnet_interventions", "single_model",
                "locally_rejected")) + " |")
        lines += ["", "Observed scheduler metrics (null means no recorded event; not zero):", ""]
        for trial in report["trials"]:
            lines.append(f"- {trial['id']}: `{dumps(trial['scheduler_metrics'])}`")
        lines += ["", "Provider totals use the same receipt accounting as `orch cost`.", "",
                  "Receipt time includes admission waits and adapter work; "
                  "parallel intervals overlap.",
                  "", "| Provider | Receipts | Input | Output | Total | Token coverage | "
                  "Cache hit | Receipt seconds | Estimated USD (coverage) | "
                  "Settled CNY (coverage) |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for provider, usage in report["provider_usage"].items():
            tokens = usage["known_tokens"]
            hit = usage["cache_hit_rate"]
            hit_text = "unknown" if hit is None else f"{100 * hit:.2f}%"
            seconds = usage["receipt_seconds"]
            seconds_text = "unknown" if seconds is None else f"{seconds:.2f}"
            money = []
            for field, factor in (("provider_estimate_usd", 1), ("settled_cents", 0.01)):
                cost = usage["costs"][field]
                money.append(f"{cost['known_sum'] * factor:.6g} "
                             f"({cost['reported_calls']}/{usage['calls']})"
                             if cost["reported_calls"] else "unreported")
            lines.append(f"| {provider} | {usage['calls']} | {tokens['input_tokens']} | "
                         f"{tokens['output_tokens']} | {tokens['total_tokens']} | "
                         f"{usage['reported_calls']['total_tokens']}/{usage['calls']} | "
                         f"{hit_text} | {seconds_text} | {money[0]} | {money[1]} |")
        lines += ["", "API accounting: `" + dumps(report["api_accounting"]) + "`."]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines) + "\n")
    args.output.with_suffix(".json").write_text(dumps(summary) + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
