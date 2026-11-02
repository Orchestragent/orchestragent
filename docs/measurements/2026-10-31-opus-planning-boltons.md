# 恢复 Opus 规划、DSF 实施：Boltons 匹配对照前的冻结改动

起点 `284b53a08b987a2ff19fca51548438d426b95da5`。本轮只做离线代码与定向检查，
**没有调用任何模型，没有运行 Boltons**；不能据此宣称 Boltons 已通过、缓存已改善或更快。
真实对照由 controller 在 Mac 用同一冻结提交执行。

## 架构纠正：哪条是正式路径

- `orch.plan(distributed=True)` 与 `scripts/orch_repository_probe.py --distributed` 的默认值
  一直是 Opus 5.5/high 起草边界（`purpose=plan`，`CLAUDE_PLAN`），DSF 只做测试分片与实现。
  `reviewed_boundary` 默认 False，策略/调优不会打开它。
- `--reviewed-boundary`（DSF 起草、Opus 审核）只是显式实验入口。2026-10-31 timeflow 集群
  （`r05a2ec23`）和 kernel 的审核实验用的是它，因此那两次失败**不是**正式路径的结果。
- 新增定向回归固定路线：规划回执只有 `claude/claude-opus-5-5 high plan` 与
  `dsh/deepseek-flash test-design`，没有 `boundary-draft`/`boundary-review`；会话记录
  planner/designer；执行调用全部为 DSF（本固定响应用例未触发 Opus 恢复档位，真实运行的
  patch/review/thicken 仍属既有确定性恢复阶梯，未改动）。
- probe 报告新增 `planning_session`（不含 spec 全文），汇总新增 `planning_mode`，可直接
  核对 `opus-boundary-dsf-tests` 与实验路径。

## Opus 缓存：证据、可行改动与未改原因

本轮冻结引擎**没有新的缓存机制**，只有规划输出减负；以下是是否值得改缓存协议的评估，
不是缓存已改善的结论。原始依据：`2026-10-30-{boltons-cache-rollout,dsf-1m-pilot,
dsf-1m-fresh,exception-context-first,exception-context-final}.json`、
`2026-10-31-kernel-default.json`，探针 `2026-10-30-exception-cache-{schema-switch,
shared-schema}.json` 与 `2026-10-30-claude-cache-{pilot,directory,block}.json`。

规划调用（每次全新运行仅一次）：

| 运行引擎 | plan 缓存读 | plan 缓存写 | plan 输出 | plan 秒数 |
|---|---:|---:|---:|---:|
| `6a4387a3` | 0 | 11,874 | 5,325 | 48.3 |
| `12d50f1b` | 11,163 | 720 | 9,108 | 84.3 |
| `7f42c7f5` | 11,163 | 886 | 5,573 | 49.4 |
| `c88ee1fb` | 11,163 | 1,043 | 4,794 | 42.0 |
| `a288de5b` | 0 | 12,253 | 10,156 | 94.3 |

读到 11,163 的三次共用前缀 `49d2eb20e7d3`，来自此前同输入运行留下的缓存温度，不是代码效果。
规划调用只占 Opus 未缓存输入的 0.8%（热）到 15.5%（冷，kernel-default）；主体在执行期角色。

执行期 Opus（patch/review/thicken/replan）：

| 运行 | 执行期未缓存输入 | 执行期缓存读 | 同任务跨角色后继 | 后继调用缓存写之和 |
|---|---:|---:|---:|---:|
| `7f42c7f5` fresh | 108,641 | 56,740 | 6 | 43,476 |
| `158036b3` first | 97,387 | 43,152 | 3 | 26,506 |
| `c88ee1fb` final | 122,506 | 60,471 | 5 | 62,243 |
| `a288de5b` kernel | 66,560 | 18,067 | 2 | 15,080 |

- 后继的判定：先前请求已结束后才启动，且距先前请求**开始**不超过 300 秒（供应商 TTL 从读/写
  缓存的请求开始计，生成时间也消耗 TTL）。300 秒只是 API 默认 TTL 的保守假设，不是实测的
  Claude Code 配置，也不保证命中；后续读取可能刷新更早前缀，但没有完整前缀相同的证据不推测。
  上表各对的请求年龄为 11.6–101.0 秒，按此时钟重算计数不变。
- 后继多为 patch 输出 TEST_DISPUTE 后立即发起的同任务 review（完成后间隔约 0 秒）。现在 patch 用
  `PATCH_SCHEMA` 与项目前缀，review/thicken 用 `EXCEPTION_SCHEMA` 与异常前缀，同任务的
  源码/测试/证据都在用户消息里，因此后继调用重新写入。
- 机制证据：同 schema、同系统前缀、只换用户消息中的操作时，第三次调用读 13,119/13,849
  （94.7%）；只换 schema 时仅读 972。所以“统一严格 schema + 同任务材料进入共享段 + 操作留在
  用户消息”在机制上可行，并且可沿用 review/thicken 已有的“共享线格式、运行时逐操作校验”做法，
  不放松任何验证。
- 代价：原生 CLI 现有路径只有一个追加系统文件。把任务材料放进共享段后，换任务时公共部分
  （规格、项目地图）是否还能命中没有证据；若不能，现在执行期跨任务读到的 18K–60K 会大量
  转为写入，与上表 15K–62K 的潜在收益同一量级，净效果符号未知。后继写入里还有 dispute/证据等
  动态部分，因此上表只是上界。实现要改 patch 上下文包、review 快照格式（含旧快照恢复兼容）
  和 patch 输出校验，属于评测引擎的重要改动，没有机制实测就冻结进对照不合适。

因此本轮不改缓存协议，只把评估做成可执行：

1. 用本冻结提交跑 fresh pair，从新回执读 `stages` 与新增的 `claude_cross_role_followups`，
   不额外调用模型。
2. 若后继写入占执行期未缓存输入的比例仍高，另行授权后运行已提交的
   `scripts/orch_claude_cache_probe.py --task-prefix --live`（4 次 Opus medium 订阅调用，只用
   2.1.280 已在用的 `--append-system-prompt-file`）：第 2 次对第 1 次衡量同任务换操作的复用，
   第 3 次衡量换任务后公共前缀还剩多少。
3. 仅当第 3 次保留公共前缀，或按 pair 回执重算收益大于损失时，才实现：patch/review/thicken
   共用一个严格线格式并逐操作校验，同任务材料进入共享段，review 快照加版本、旧快照原样重放；
   定向测试覆盖 patch→review 共享段逐字节相同、跨操作字段被拒、旧快照恢复。
4. 新冻结、Mac 全量门、新的 fresh pair；同任务后继的缓存读没有上升，就不把差异归因于代码。

## 减轻 Opus 一次性规划输出

证据：已保存的 timeflow 三份边界草案（同一 schema/提示）中，`requirements` 占规划 JSON
22.4%、29.1%、26.3%（6,554/6,423/6,423 字节），几乎逐字复述原需求。Boltons 的 Opus 规划
响应正文未入库，因此本轮不能给出 Boltons 的同类比例。

改动：`spec_requirements` 从原需求逐字抽取 `R<N>. ` 段落（到下一个标签或规格末尾，最后一段
可能多带尾部说明，但不截断），作者对保持不变的需求可返回空 text，`fill_requirements` 在 lint
前原样补入；规划日志仍保存原始响应。只支持保守子集：含代码围栏、首个标签后出现任何 `#` 行
（小标题或注释）、或标签重复时整体返回 `unsupported`，一条也不补入（timeflow 的折行 `R1.`
即此情况）。Boltons checkpoint 与 buildgraph 仍得到全部标签。Opus 是否采用、能省多少输出 token，需要真实回执确认。
Opus 仍负责需求拆分、接口、归属、契约与依赖；没有把这部分交给 DSF，也没有压缩规格、
截断上下文或降低 DSF 窗口。

## 边界反馈、停止和恢复

- `BOUNDARY_RULES` 由作者提示、可选审核提示与控制器错误共用；明确验收负责人、文件写入者
  与依赖分开，共享 helper 可由上游写入、下游验收；文字提及不算依赖。写入者规则只针对任务列出
  的文件和新 stub，未触及的基线文件不需要 owner；stub import 只要求写入任务在直接或间接依赖中，
  与 `_static_dependencies` 的依赖闭包一致。没有增加 schema 字段。
- 重复验收错误列出全部需求与任务（`duplicate-acceptance R8: T-007,T-009`）；空需求文字的
  错误写明 ID。普通 lint 允许多任务列同一 R、thin 边界要求唯一验收负责人，二者语义未改。
- 结构错误与审核意见在后续修订中一并保留。停止编码按优先级：`repeated_boundary`、
  `repeated_structural_error`（指纹为掩码任务编号后的排序错误摘要）、`structural_budget`、
  `review_budget`；写 `planning/stop.json`。恢复只重放已结算回执，停在同一编码。
- 离线重放 timeflow 已保存草案：草案 1、3 分别报 R8 的 T-007/T-009、T-008/T-009，
  指纹相同；草案 2 结构通过。这是已见过的回归样本，不是独立留出。
- 行为变化：审核拒绝后提交完全相同的草案，不再买第二次审核（实验路径 4 次调用变 3 次）。
  原有两个测试用例改为提交真实修订（摘要不同），调用计数不变。

## 测量与报告

`report_overnight.py --repository` 每个试验新增 `stages`：按“路线:用途”（如
`claude/claude-opus-5-5:plan`、`:patch`、`:review`、`:thicken`、`:replan`，
`dsh/deepseek-flash:test-design`、`:build`）给出调用数、输入/输出、缓存读/写、未缓存输入、
命中率、回执覆盖、回执耗时之和（有未完成回执时为 null）与首尾跨度、effort、未结算数；
另有 `claude_cross_role_followups`（同任务跨角色后继及其缓存写，只是机会上界）。已用上述
历史回执离线核对。可得：规划输出 token、规划与测试设计阶段时间、各阶段回合数。
仍缺：TTFT 与生成速度（CLI 不报告）、推理 token、基线测试单独耗时（只在
`plan_including_baseline_s` 内）、单体会话内部逐轮用量。

## 已知局限

- Boltons checkpoint 已用于此前框架迭代，不是独立留出；一次对照只是单样本观测。
- 旧 8/8（`7f42c7f5`、`c88ee1fb`）和 kernel 7/8 只是历史参考；其后引擎有大量改动，
  正式路径在 Boltons 上没有新的实测。
- `scripts/report_overnight.py` 在起点已有一处 mypy 可空索引错误，本轮未扩大改动范围。
