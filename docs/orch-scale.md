# orch 大规模压测结果

由 `scripts/orch_scale.py` 生成；不调用任何模型、不产生费用。合成计划经过真实的调度器、
git worktree、合并闸门、代码索引、变异测试和 OS 沙箱；`SyntheticBuilder` 代替模型，
每次调用延迟 1 秒，15% 的任务首次实现是错的，
3% 的任务第一次调用被限流（429）。每个任务 4 个变异体。

环境：Linux x86_64，4 个验证槽，Python 3.13.17，沙箱 bwrap，git version 2.43.0。

| tasks | depth | jobs | status | plan s | execute s | tasks/min | path floor s | attempts | failed | 429s | gate run | gate skipped | wt new | wt reused | peak gen | peak verify | blobs parsed | events | state KiB | mutation | audit |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 50 | 10 | 1 | done | 1.5 | 85.6 | 35.1 | 10.0 | 59 | 9 | 1 | 454 | 821 | 5 | 156 | 1 | 1 | 119 | 618 | 272 | 1.0 | 0 |
| 50 | 10 | 16 | done | 1.5 | 26.3 | 114.1 | 10.0 | 59 | 9 | 1 | 618 | 657 | 34 | 127 | 16 | 4 | 119 | 618 | 272 | 1.0 | 0 |
| 100 | 11 | 16 | done | 2.2 | 37.6 | 159.6 | 11.0 | 115 | 15 | 3 | 1272 | 3778 | 38 | 281 | 16 | 4 | 235 | 1212 | 484 | 1.0 | 0 |
| 200 | 10 | 16 | done | 3.8 | 70.4 | 170.5 | 10.0 | 226 | 26 | 6 | 1991 | 18109 | 38 | 595 | 16 | 4 | 466 | 2390 | 896 | 1.0 | 0 |

列说明：depth 是 DAG 最长依赖链；path floor 是 depth × 调用延迟，即并发再高也省不掉的
生成时间；gate run / skipped 是合并闸门实际跑的、以及被测试影响分析跳过的已合并测试数；
wt new / reused 是新建与复用的 worktree；blobs parsed 是索引解析过的 git blob 数（增量，
不重复解析）；audit 是 `orch audit` 的发现数（0 表示事件链、状态迁移、trailer、tag、
worktree 和全部测试都一致）。
