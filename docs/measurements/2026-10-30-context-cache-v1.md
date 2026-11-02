# 集群上下文与缓存：实现报告

实现提交：`2dd79080b7881f2be9b6a320ad1bbe3f00df3a82`。范围只有底层与 CLI，未推送或部署。

## 已交付

1. **节点按需补读**：builder 可用 `NEED_FILES` 请求文件或 `path.py::Symbol`，读取固定在
   当前候选的 base revision。最多 3 轮、每轮 8 个、累计 16 个选择器，跨重试和恢复
   持久化；每次调用独立记账。只读申请不扩大写入所有权，不提高失败阶梯；超预算或
   超轮次停下，不靠不断扩大上下文继续烧 token。
2. **细粒度上下文**：相关接口、测试方法按符号节选，保留 imports、模块状态、fixture、
   setup 和辅助函数。 owned 文件仍完整提供；实际验证仍执行原测试。动态行为或无法
   确定的依赖保守回退整文件，节选明确标记不能当整文件替换内容。
3. **按真实回执调度缓存**：取消首请求固定串行预热。关键路径优先，同优先级才使用
   provider/model/实际上下文前缀的缓存亲和度。缺失或异常回执不作证据；需要足够
   可比的冷热耗时样本才主动延后，策略上限 250ms，无关任务可用其余槽位。
   `--cache-mode off` 可关闭提示。最多 128 条历史、300 秒证据窗口均为本地策略，
   不是厂商缓存 TTL。

复用了既有索引、源码定义分析、上下文打包、调用账本、状态事务、工作树和调度器。
新增两个领域模块 `orch_excerpt` / `orch_cache`，没有增加通用序列化或原子写入工具。

## 实际证据

测量脚本 `scripts/orch_context_probe.py` 先随实现提交，再运行。原始结果见
[JSON](2026-10-30-context-cache-v1.json)。固定单任务计划明确要求先补读，再实现
`solver.py` 调用已有 `helper.offset()`；这是协议冒烟，不是自主规划或大项目能力评测。

| 项目 | 观察结果 |
| --- | --- |
| 执行结果 | DONE，沙箱验证和最终合并验证通过 |
| 模型调用 | 2 次 DSF；没有 Claude/Jev 调用 |
| 补读 | 1 次，实际请求 `helper.py`，固定 base revision |
| 构建阶梯 | attempts=1，failure_rung=0 |
| 总输入 / 输出 | 1,575 / 177 token |
| 总 token | 1,752 |
| 缓存读取 | 384 token；两次合计输入命中率 24.4% |
| 保守账本结算 | ¥0.02；不是厂商最终账单 |
| 缓存调度主动等待 | 0 次；该单任务不验证并行调度收益 |

首调用为冷请求；第二次输入 803 token，其中缓存读取 384（47.8%）。两个调用前缀
相同，但命中数据以厂商回执为准。样本太小，不能据此声称普遍缓存命中率或性能提升。
凭据与预算配置未变，历史预留未清除。

在同一提交的真实仓库文件上测量源码节选：

| 选择器 | 整文件字节 | 节选字节 | 减少 |
| --- | ---: | ---: | ---: |
| `tests/test_orch_context.py::test_requested_symbols_are_pinned_and_can_expand_to_full_files` | 8,788 | 1,830 | 79.2% |
| `tests/test_orch_run.py::test_context_read_is_pinned_billed_and_does_not_spend_a_failure_rung` | 38,990 | 38,990 | 0%，保守回退 |

这是两个指定选择器的**源码字节**，不包含完整 prompt/schema，也不是 token 节省率。
未对整个仓库随机抽样，不能外推平均节省比例。

## 验证与使用

本轮全量 pytest：1,310 passed、5 skipped、1 failed。失败是预算测试仍假定首调用
串行、只能产生一个本地拒绝回执。取消串行预热后多个节点可被本地预算门拒绝，实际
转发仍只有 planner，DSF 转发为零。已修正为检查所有拒绝回执与总费用，两个相关
并发预算测试复验通过。回执异常和模型不匹配的定向检查另有 15 项通过。
为避免重复空转，没有在这个断言修正后再跑全量；因此不把结果写成一次全绿全量运行。
Ruff、mypy（113 个源文件及独立测量脚本）通过，重复工具扫描无新增重复实现。

```sh
scripts/orch --repo /path/to/project context T-001 --run RUN_ID --show --json
scripts/orch --repo /path/to/project cache --run RUN_ID --json
scripts/orch --repo /path/to/project cost --run RUN_ID --json
scripts/orch --repo /path/to/project run RUN_ID --jobs 4 --cache-mode auto
```

前两个 CLI 已在本次真实 run 上读回核验。`context` 展示当前已验证版本的选材和补读
选择器，不是完整历史 prompt；调用账本保留 prompt hash、base revision、prefix hash、
call ID、token 与费用。`cache` 显示最近一次执行保存的策略摘要，不是实时缓存查询。

## 尚未证明的部分

本轮完成三条底层链路，**尚未证明复杂大项目比全工具 Opus 5.5 单体更快、更便宜或
质量更高**。缓存等待策略很保守，混合 token 大小的历史通常不会触发等待；Python
动态代码仍可能回退整文件。下一阶段应直接在有跨模块依赖的真实大项目上比较端到端
交付时间、验收结果、总 token 和重工量，不用这个单任务冒烟代替集群能力成绩。
