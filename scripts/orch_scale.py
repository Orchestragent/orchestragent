"""Scale benchmark: synthetic wide plans through the real scheduler, git and sandbox.

No model is called and nothing is billed: SyntheticBuilder answers every call after a
fixed latency that stands in for a provider, gets a share of first attempts wrong and
rate-limits a few. Each size runs plan -> execute (with mutation scoring) -> audit in a
fresh repository and the results go to docs/orch-scale.md and docs/orch-scale.json.

    PYTHONPATH=src uv run python scripts/orch_scale.py            # 50, 100 and 200 tasks
    PYTHONPATH=src uv run python scripts/orch_scale.py --tasks 30 --serial
"""

import argparse
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from orchestragent.live_workspace import LiveWorkspace
from orchestragent.orch_audit import audit
from orchestragent.orch_plan import tasks_from
from orchestragent.orch_run import Orchestrator, default_verify_jobs
from orchestragent.orch_synthetic import Shape, SyntheticBuilder, synthetic_plan
from orchestragent.platform import sandbox_name
from orchestragent.serialization import dumps
from orchestragent.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]


def depth(plan: dict[str, object]) -> int:
    tasks = {task.id: task for task in tasks_from(plan)}
    memo: dict[str, int] = {}

    def chain(task_id: str) -> int:
        if task_id not in memo:
            memo[task_id] = 1 + max((chain(d) for d in tasks[task_id].depends_on), default=0)
        return memo[task_id]

    return max(chain(task_id) for task_id in tasks)


def measure(size: int, args: argparse.Namespace, jobs: int) -> dict[str, object]:
    shape = Shape(tasks=size, packages=max(2, size // 10), fan_in=2, seed=size)
    plan, bodies = synthetic_plan(shape)
    builder = SyntheticBuilder(plan, bodies, wrong=args.wrong, limited=args.limited,
                               latency=args.latency)
    with tempfile.TemporaryDirectory(prefix="orch-scale-") as scratch:
        repo = Path(scratch) / "repo"
        repo.mkdir()
        Workspace.git(repo, "init", "-b", "main")
        (repo / "README.md").write_text("scale benchmark\n")
        Workspace.git(repo, "add", "README.md")
        Workspace.git(repo, "commit", "-m", "seed")
        orchestrator = Orchestrator(repo, builder, LiveWorkspace.run_unittests,
                                    notify=lambda line: None)
        started = time.monotonic()
        run = orchestrator.plan("synthetic benchmark")
        planned = time.monotonic()
        run = orchestrator.execute(run.id, jobs=jobs, mutants=args.mutants)
        executed = time.monotonic()
        with orchestrator.exclusive(run.id):
            findings = audit(orchestrator, run.id)
        audited = time.monotonic()
        events = orchestrator.state.events(run.id)
        tasks = orchestrator.state.tasks(run.id)
        metrics = next(e.data for e in events if e.kind == "run.metrics")
        gates = [e.data for e in events if e.kind == "task.gate"]
        scores = [t.mutation for t in tasks if t.mutation >= 0]
        size_bytes = (orchestrator.home / "state.sqlite").stat().st_size
        minutes = (executed - planned) / 60
        return {
            "tasks": size, "packages": shape.packages, "dag_depth": depth(plan), "jobs": jobs,
            "status": run.status,
            "plan_s": round(planned - started, 1), "execute_s": round(executed - planned, 1),
            "audit_s": round(audited - executed, 1),
            "tasks_per_min": round(size / minutes, 1) if minutes else None,
            "critical_path_floor_s": round(depth(plan) * args.latency, 1),
            "attempts": sum(t.attempts for t in tasks),
            "failed_attempts": sum(e.kind == "task.failed" for e in events),
            "rate_limit_deferrals": sum(e.kind == "task.deferred" for e in events),
            "gate_tests_run": sum(int(str(g["tests"])) for g in gates),
            "gate_tests_skipped": sum(int(str(g["skipped"])) for g in gates),
            "worktrees_created": metrics.get("worktrees_created", 0),
            "worktrees_reused": metrics.get("worktrees_reused", 0),
            "peak_generating": metrics.get("peak_generating", 0),
            "peak_verifying": metrics.get("peak_verifying", 0),
            "blobs_parsed": orchestrator.index.parsed,
            "events": len(events),
            "state_kib": size_bytes // 1024,
            "mutation_avg": round(sum(scores) / len(scores), 2) if scores else None,
            "audit_findings": len(findings),
            "first_finding": findings[0] if findings else "",
        }


COLUMNS = [
    ("repeat", "repeat"),
    ("tasks", "tasks"), ("dag_depth", "depth"), ("jobs", "jobs"), ("status", "status"),
    ("plan_s", "plan s"), ("execute_s", "execute s"), ("tasks_per_min", "tasks/min"),
    ("critical_path_floor_s", "path floor s"), ("attempts", "attempts"),
    ("failed_attempts", "failed"), ("rate_limit_deferrals", "429s"),
    ("gate_tests_run", "gate run"), ("gate_tests_skipped", "gate skipped"),
    ("worktrees_created", "wt new"), ("worktrees_reused", "wt reused"),
    ("peak_generating", "peak gen"), ("peak_verifying", "peak verify"),
    ("blobs_parsed", "blobs parsed"), ("events", "events"), ("state_kib", "state KiB"),
    ("mutation_avg", "mutation"), ("audit_findings", "audit"),
]


def markdown(rows: list[dict[str, object]], args: argparse.Namespace) -> str:
    git = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False)
    lines = [
        "# orch 大规模压测结果",
        "",
        "由 `scripts/orch_scale.py` 生成；不调用任何模型、不产生费用。合成计划经过真实的调度器、",
        "git worktree、合并闸门、代码索引、变异测试和 OS 沙箱；`SyntheticBuilder` 代替模型，",
        f"每次调用延迟 {args.latency:g} 秒，{args.wrong:.0%} 的任务首次实现是错的，",
        f"{args.limited:.0%} 的任务第一次调用被限流（429）。每个任务 {args.mutants} 个变异体。",
        "",
        f"环境：{platform.system()} {platform.machine()}，{default_verify_jobs()} 个验证槽，"
        f"Python {platform.python_version()}，沙箱 {sandbox_name()}，{git.stdout.strip()}。",
        "",
        "| " + " | ".join(title for _, title in COLUMNS) + " |",
        "| " + " | ".join("---" for _ in COLUMNS) + " |",
    ]
    lines += ["| " + " | ".join(str(row.get(key, "")) for key, _ in COLUMNS) + " |"
              for row in rows]
    lines += [
        "",
        "列说明：depth 是 DAG 最长依赖链；path floor 是 depth × 调用延迟，即并发再高也省不掉的",
        "生成时间；gate run / skipped 是合并闸门实际跑的、以及被测试影响分析跳过的已合并测试数；",
        "wt new / reused 是新建与复用的 worktree；blobs parsed 是索引解析过的 git blob 数（增量，",
        "不重复解析）；audit 是 `orch audit` 的发现数（0 表示事件链、状态迁移、trailer、tag、",
        "worktree 和全部测试都一致）。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", default="50,100,200")
    parser.add_argument("--jobs", type=int, default=16)
    parser.add_argument("--latency", type=float, default=1.0)
    parser.add_argument("--wrong", type=float, default=0.15)
    parser.add_argument("--limited", type=float, default=0.03)
    parser.add_argument("--mutants", type=int, default=4)
    parser.add_argument("--serial", action="store_true",
                        help="also run the smallest size with --jobs 1 for comparison")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "orch-scale.md")
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error("--repeats must be between 1 and 10")
    if Workspace.git(ROOT, "status", "--porcelain", "--", "src", "scripts/orch_scale.py"):
        parser.error("commit measured source and script before running")
    revision = Workspace.git(ROOT, "rev-parse", "HEAD")
    sizes = [int(item) for item in args.tasks.split(",") if item]
    rows = []
    for repeat in range(1, args.repeats + 1):
        configurations = ([(min(sizes), 1)] if args.serial else []) + [
            (size, args.jobs) for size in sizes]
        for size, jobs in configurations:
            rows.append({**measure(size, args, jobs), "repeat": repeat,
                         "engine_revision": revision, "synthetic": True,
                         "latency": args.latency, "wrong": args.wrong,
                         "limited": args.limited, "mutants": args.mutants,
                         "python": platform.python_version(), "platform": platform.platform()})
            print(dumps(rows[-1]), flush=True)
    args.out.write_text(markdown(rows, args))
    args.out.with_suffix(".json").write_text(dumps(rows) + "\n")
    return 0 if all(row["audit_findings"] == 0 and row["status"] == "done" for row in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
