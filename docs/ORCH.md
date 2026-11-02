# orch：多 agent 开发编排 CLI

`orch` 在一个本地 Git 仓库上执行“规划 → 并行实现 → 确定性验证 → 合并”。调度层全部是
确定性代码，LLM 只在叶子节点工作；任务“完成”只由冻结测试在集成分支上通过来判定。

## 产品方向与交付标准（2026-10-30）

Orch 是 BYOK agent 集群框架，以简洁实用为设计基调。主场景是大型系统的实现、
跨模块修改与部署；固定标杆为具备完整工具和迭代能力的 Opus 5.5 单体 agent。
目标是在同等交付质量下，通过精细拆分和调度实现数倍端到端加速，并降低总 token
成本。目标尚未被现有小题评测证明；不保证某个模型或并发数天然产生收益。

- 拆分围绕模块边界、依赖和关键路径；独立工作并行，强耦合工作合并，避免碎片化任务。
- 上下文按任务提供完整必要代码和版本化接口，避免反复传递全仓库及全部会话历史。
  稳定前缀复用，动态内容后置；缓存收益同时看总输入、未缓存输入和费用，不单看命中率。
- 自动化覆盖从需求到集成、部署和关键验收；失败局部恢复，避免反复全局重规划。
- 自优化调整任务粒度、路由、并发及上下文选择，使用真实项目结果形成小型可回滚策略。
  不堆积技能文件，不把增加规则数量或未验证的代理分数当作能力增长。
- 复用现有调度、文件所有权、工作区和账本。新机制必须解决实际项目中观察到的问题。

对照双方使用相同初始代码、需求、可用工具、环境与验收；允许 Opus 单体正常探索和
迭代，不人为裁剪其上下文或锁死调用次数。记录各自预算及实际配置，比较最终完成度、
端到端耗时、总 token/费用和人工介入；集群包含规划、协调、失败、修复及集成开销。
当前 `single-opus` 是受限 JSON 生成对照，不能代表完整 Opus 单体 agent。
第一项系统交付可用持久化 DAG、资源调度、多个 worker、租约恢复及部署组成的分布式
任务执行系统牵引。先实现并跑通，再做必要对照；小题仅作回归，测试数量不作进度指标。

现有代码允许任务通过 `tasks[].files` 认领已有非测试文件；禁止的是规划器在
`plan.files` 中覆盖已有文件。已有模块修改不需要另写执行内核。仍需在真实项目中验证
规划质量、接口协调、依赖刷新和集成效果，且分层规划目前限制新建包。

## 快速体验

```sh
scripts/login.command            # 选 DSH，输入 key（Claude 未登录则再选 Claude）
scripts/orch doctor              # 逐项检查：Python、沙箱、Claude 登录、DSF key 与余额（Jev 可选）
scripts/orch_demo.sh             # 在 ~/orch-demo 新建仓库，跑完整 plan → run（examples/orch-demo/spec.md）
```

演示大约花 1 次 Claude 规划调用和若干次 DSF 调用（每次几分钱）。结束后报告在
`~/orch-demo/.git/orch/<run>/REPORT.md`，结果在 `orch/<run>` 分支。

Linux 上自带密钥（BYOK）直接用：下载 `dist/orch-linux.tar.gz`，解包后 `./install.sh`、
`./orch keys dsh`、`./orch doctor`，详见包里的 `QUICKSTART.md`（源文件 `packaging/linux/`）。
没装 `dsh` CLI 时，DSF 直接调用 DeepSeek 的 API（同一模型、同一把 key、同一花费账本，
每次一个请求）；`ORCHESTRA_DSF_TRANSPORT=cli|api` 可指定。

保存 DSF key 只表示本地配置完成。`doctor` 默认要求这把 key 在当前模型和通道上有真实
调用成功的记录；首次使用会显示 `authentication unverified` 并返回非零。明确要发起
首次付费任务时，可用 `doctor --allow-unverified-auth` 检查其他前提，示例脚本采用此模式。
它不跳过已知认证拒绝或缺失凭据，也不会主动发送模型请求。换 key 后旧验证
失效；成功推理才会记录 verified，服务端 401/403 会撤销对应记录。

## 命令

```sh
scripts/orch keys dsh                           # 自带 DeepSeek key，不设本地额度（Jev：keys jev）
scripts/orch --repo ~/code/app doctor           # 体检；每项失败都给出修复办法
scripts/orch --repo ~/code/app plan spec.md     # Claude 规划，lint 把关，提交骨架+冻结测试
scripts/orch --repo ~/code/app plan --resume RUN_ID # 恢复中断的 distributed 规划
scripts/orch --repo ~/code/app run --jobs 4     # 按 DAG 并行执行（可重复执行 = resume）
scripts/orch --repo ~/code/app resume           # 同 run，强调从事件日志恢复
scripts/orch --repo ~/code/app status           # 任务状态、PARKED 汇总与失败证据
scripts/orch --repo ~/code/app context T-001 --json # 查看任务拥有的文件、所需接口和冻结测试
scripts/orch --repo ~/code/app why R3           # 需求 → 测试 → 任务 → commit
scripts/orch --repo ~/code/app trace adder.py:6 # 或符号名；git blame → commit → trailer
scripts/orch --repo ~/code/app cost --by model  # token、缓存命中率、DSF 人民币结算
scripts/orch --repo ~/code/app cost --task T-001 --calls  # 任务内每次调用的 token 与证据
scripts/orch --repo ~/code/app cost --json      # 导出完整调用账本（含共享规划开销）
scripts/orch --repo ~/code/app progress         # 集群交付、等待、恢复与分阶段消耗
scripts/orch --repo ~/code/app progress --run ID --json  # 当前 DAG、调用和候选引用
scripts/orch --repo ~/code/app retry T-004      # 处理完 PARKED 原因后，重置阶梯再排队
scripts/orch --repo ~/code/app rule             # 学到的 builder/planner 规则及其 A/B 统计
scripts/orch --repo ~/code/app jev              # Jev 分诊/预筛与规则的对比（校准用）
scripts/orch --repo ~/code/app index [符号]     # 代码索引统计；给符号则列出定义和调用方
scripts/orch --repo ~/code/app mutate [T-003]   # 用 AST 变异体给已合并任务的测试打分（不调模型）
scripts/orch --repo ~/code/app harden [T-003]   # Claude 补测试杀死存活变异体，验证通过才冻结
scripts/orch --repo ~/code/app evolve [p2]      # 离线回放历史 spec，评估 planner 候选规则
scripts/orch --repo ~/code/app evolve --check   # planner 代理指标与真实结果的相关性
scripts/orch --repo ~/code/app audit [run]      # 检查一个 run 的全部不变量和测试
scripts/orch --repo ~/code/app watch            # 终端实时架构视图（只读；--web 为可选浏览器视图）
```

## 实时观察：orch watch

`orch watch` 只读地展示一个 run 的整体架构和消息流：You/CLI → Orchestrator → Opus planner、
每个 DSF test-design 分片和构建任务（T-001…）→ DSH guard → DeepSeek API、Claude API、
bwrap 沙箱验证器和 git merge 队列。节点显示状态（颜色区分 running/verifying/merging/done/
failed/parked）、耗时、token 与花费；边上的符号按类型着色移动（请求、结果、重试、NEED_FILES
补读、测试失败回到 builder、验证、合并）；下方是按通道排列的调用时间线（Gantt）、并行度曲线和
近似关键路径。同一任务的单元测试设计分片（`design T-004`）与集成测试设计分片
（`design T-004 ∫`，ASCII 下为 `design T-004 int`）是两个独立节点和两条时间线通道。
节点、表头和纯文本行中的 token 数一律是输出 token（详情中另列 `in … / out …`），
tok/s = 输出 token ÷ 调用耗时；费用口径不变。在途调用由“有 `started_at` 无 `finished_at`
的账本记录”及“无结果的 `attempt.called`”推出，显示已用时间；已完成调用显示 tok/s。
本版不新增任何事件埋点。

```sh
scripts/orch --repo ~/code/app watch                    # 终端 UI（默认，curses，标准库）
scripts/orch --repo ~/code/app watch --run r8eb1c429    # 固定一个 run；默认跟随最新 run
scripts/orch --repo ~/code/app watch --replay --speed 16  # 按事件时间顺序 16 倍速回放
scripts/orch --repo ~/code/app watch --plain            # 每个事件一行纯文本（无 curses）
scripts/orch --repo ~/code/app watch --ascii            # ASCII 线框（非 UTF-8 终端）
scripts/orch watch --db path/to/state.sqlite            # 直接读某个状态库（如评测目录）
```

终端 UI 是主体验，SSH 上任何终端（含 tmux/screen，Linux 与 macOS）都可用：Tab/方向键
选择节点，Enter 打开详情（目的、attempt、结果、token、耗时；从不显示 prompt 或密钥），
`p` 暂停，`+`/`-` 调整回放速度，`r` 重新回放，`q` 退出。能力自动降级：256 色 + Unicode
线框 → 8 色 → 单色（遵守 `NO_COLOR`）→ ASCII（编码非 UTF-8 或 `--ascii`）；终端放不下架构图时
改为紧凑列表并在窗口大小变化时重新布局；没有 TTY、`TERM=dumb`、输出被管道接走或 curses
初始化失败时自动改为 `--plain` 文本流。网页视图只是可选项，不会作为回退。

可选网页视图（标准库 HTTP + Server-Sent Events，约 0.5 秒轮询，无 CDN、无构建步骤）：

```sh
scripts/orch --repo ~/code/app watch --web              # 默认只监听 127.0.0.1:8780，并打印下面这行
ssh -L 8780:localhost:8780 user@server                  # 在笔记本上执行，然后打开 localhost:8780
scripts/orch --repo ~/code/app watch --web --bind 0.0.0.0   # 非回环地址必须带 token（自动生成）
scripts/orch --repo ~/code/app watch --web --replay --speed 8
scripts/orch --repo ~/code/app watch --export run.html  # 单个自包含 HTML，双击即可离线回放
```

- 回环监听不需要 token，但只接受 `localhost`/回环地址的 Host 头（防 DNS rebinding）。
- `--bind` 指向任何非回环地址时必须有 token：未给 `--token` 就用 `secrets.token_urlsafe`
  生成并打印一次。每个请求都要验证：`?token=` 只用一次，换成 HttpOnly/SameSite=Strict
  cookie 后重定向去掉 URL 中的 token；也可用 `Authorization: Bearer <token>`；常数时间比较。
  共享网络上仍推荐 `ssh -L`（HANDOFF 不变量 6）。
- `--export` 的文件内嵌已脱敏的模型数据，不含任何 `http://`/`https://` 外部引用。
- 数据源是 `<repo>/.git/orch/state.sqlite`，以 SQLite `mode=ro` 打开，与正在执行的 run 并发
  读取，不写入、不加锁。只输出白名单摘要（状态、用途、attempt、结果、token、耗时、费用、
  短 commit），规格原文、prompt、模型回复、错误全文和测试报告都不会进入界面或导出文件；
  文本中形如密钥或 URL 的片段会被替换。

`--bind`/`--port`/`--token` 只用于 `--web`。下一阶段（未实现）：调用中的实时 tok/s（需要
新的流式事件埋点）、多 run 对比和跨机部署视图。

## Opus + DSF 评测（第一版）

`bench` 固定复用当前 Opus 5.5 规划／修复和 DSF 实现流程。每个用例、每次重复都从
独立的新 Git 仓库开始；外部验收测试只在生成结束后的临时验证工作区中加入，不进入
模型上下文或实现仓库历史。只有内核 run 完成且独立验收通过，才计作评测成功。

```sh
# 默认 Mock；无需凭据，不创建 provider client。示例是合成算术题，不是能力评测。
scripts/orch bench examples/orch-bench/suite.json --output .data/bench/mock-001

# 已完成或中断的报告可重读，不重新发起模型调用。
scripts/orch bench --output .data/bench/mock-001 --report --json

# 显式真实调用：沿用既有 Claude 订阅与 DSF 授权，Jev 暂不参与本版评测。
# 以下命令会产生真实 API 费用。请换成描述完整需求的评测用例。
scripts/orch bench cases.json --output .data/bench/real-001 --live \
  --max-calls 20 --jobs 2 --repeats 1
```

suite JSON 为 `schema_version: 1` 和 `cases` 数组。每个 case 包含唯一 `id`、完整需求
文本 `spec`、初始文件映射 `seed`（相对路径→文本）、独立测试映射 `acceptance`
（根级 `test_*.py` 文件名→源码）。可选 `mock_shape` 配置现有合成模型的
`tasks/packages/fan_in/seed`；只有示例 Mock 使用它。初始文件与验收仍限标准库 Python。

输出目录必须是新的。`bench.sqlite` 在任何模型调用前保存用例清单、输入快照和执行配置，
包括固定模型路由、运行时环境、输入／验收指纹；每个用例仓库保留自己的调用账本和结果
提交。报告包括规划失败、全部实现尝试和未选候选的 token；缺失用量仍为未知。
Mock 报告明确标记 synthetic，其 token 可能是模拟值，不用来评价模型智力或真实费用。

没有本地额度闸门：只有 DeepSeek 自己报告余额不足（HTTP 402 → `quota_exhausted`）才停止派发。
本地只记录花费用于报告。

`--max-calls` 是整个 suite 的派发次数上限，包含规划和修复；`--max-cny` 是可选的整个 suite
DSF 花费停止点（默认 0 = 关闭）；设置后，并行调用暂时占满它时会等待在途调用结算或释放。并行在途调用先预留，
未返回结算的调用保留预留；本地拒绝派发的回执明确记零消耗。触及预算、额度、登录或取消
条件时停止继续用例，剩余用例保持 queued，成功率显示 incomplete。订阅调用不会虚构
人民币成本。不同 suite 的限额独立，跨 suite 的夜间总预算仍须由执行方累计控制。

`--strategy single-opus --baseline-attempts 2` 提供单模型 Opus 的“生成完整代码与测试→
沙箱验证→失败修复”对照，复用同一 suite 的独立验收、调用上限和 token 账本。它不调用
DSF，不等同于带全部交互工具的 Claude Code。两组对照须使用同一 suite、重复次数及
引擎版本；报告同时列出路由、重试、Opus 接管和源码指纹。内置 `real-suite.json` 是六道
小型人工设计题，不能代表复杂项目或通用智力。真实调用合成题仍明确标为 synthetic cases。

## 固定 pytest 环境与项目验收

```sh
# 复制本机已安装的 pytest；不安装或下载软件。
scripts/orch env create .data/environments/pytest-v1
# 需要第三方包或 src 布局时，显式保存依赖和导入根。
scripts/orch env create .data/environments/project-v2 \
  --package typing_extensions --source-root src
scripts/orch env inspect .data/environments/pytest-v1
scripts/orch --repo ~/code/app verify --environment .data/environments/pytest-v1 \
  --revision HEAD --json
# 也可在末尾选择 test_api.py::test_boundary 等 pytest nodeid。

# 将原项目 pytest 验收接入一次编排任务；run/resume 自动使用保存的环境指纹。
scripts/orch --repo ~/code/app plan spec.md --environment .data/environments/pytest-v1
scripts/orch --repo ~/code/app run
```

验证使用固定 Git 提交的临时 worktree，保留原工作区的未提交修改，结束或失败时清理。
依赖内容、版本和指纹在每次运行前校验；pytest 禁止自动插件、上级配置和外部 conftest，
项目内 fixture/conftest 仍在无网络、无主目录读取、无子进程的 OS 沙箱中执行。
零测试、提前退出、超时、全跳过不会算通过。报告记录提交和环境指纹。
节点 unittest 契约门默认 25 秒墙钟、15 秒 CPU；全仓 pytest 回归单独使用 600 秒墙钟、
300 秒 CPU，仍经过同一无网络沙箱及验证槽。两类上限写入审计行；仓库对照同时绑定全仓
预算，避免把较短预算导致的提前终止与完整回归混作速度比较。

环境入口支持 macOS 上已安装的纯 Python 分发及其必要依赖，按版本约束和环境 marker
解析；缺包、版本冲突、native 扩展、editable 启动钩子或二进制数据会明确失败，不自动安装。
每个复制文件都纳入指纹，插件不会自动启用。`--source-root` 固定仓库内导入目录；仓库根
始终排在第一位。运行器与 AST 索引使用相同导入根，重叠模块名会拒绝，索引复用已有 blob
解析。此版本不执行通用构建命令，也不支持非 Python 工具链。

显式使用 `plan --environment` 时，先在原提交跑完整 pytest，未通过不调用规划模型。
规划、节点 unittest、合并门、最终 pytest、恢复、审计及变异验证使用同一固定依赖环境。
模型收到允许的 import 名和版本，静态导入门也使用此清单。未固定的第三方包依旧不可用；
候选不能通过碰巧安装在开发机上的包蒙混过关。环境描述纳入稳定前缀和请求容量检查。
项目模式冻结所有 `conftest.py`，不允许新建或修改它来改变测试收集。最终项目回归会停为
parked；缺失／变化的环境会暂停。恢复时使用原环境指纹；若中断导致基线证据未写全，会
先在原提交重新验证，成功后才继续模型调用。`audit` 也检查并重跑这道项目验收。
项目模式暂不支持 `harden`，它会在模型调用前明确拒绝；只读的 `mutate` 评分仍可用。

`plan --distributed --test-jobs 4` 启用分工规划：复用版本化索引，把需求中命名的路径、
符号及相邻依赖优先放入最多 12 KB 的接口地图；没有精确锚点时明确退回紧凑全局地图。
Opus 只生成需求、接口骨架、文件归属、契约和依赖；已有源码由执行节点修改，规划输出
不能覆盖它。DSF 按任务并行生成独立的冻结测试，控制器限定每个分片的文件名和需求归属，
复用完整计划校验与沙箱执行。某分片格式不合格只重试该分片；失败或取消后不启动排队分片，
已发出的调用等待回执并记账。`cost --by purpose --calls` 可区分 plan、test-design、
integration-test-design 和 build。
新 distributed 计划默认对真正汇合多个独立输入的节点，以及整合至少两个上游任务的终端
节点增加独立集成设计。计算输入前沿时去掉已包含在另一输入中的祖先，避免把重复依赖
当成新的汇合。中间节点不会因仍有下游消费者而失去这道验证。
第二个设计者只看原需求、冻结边界和固定版本的既有源码，不看实现或第一份测试；
输出独占文件，验证组合操作、不同失败层以及规范要求的恢复轨迹。两份测试一起冻结，
任何一份失败都阻止该候选合入。每个符合条件的任务至多增加一个分片，共用现有
并发、调用数、预算和时限；`--no-integration-checks` 可关闭。该选择写入规划会话，
恢复时不能改变；旧会话缺少 integration_checks 时保持关闭。已开启的旧会话缺少
integration_scope 时仍为 terminal，新会话显式保存 joins，不在恢复时增加收费分片。
生成测试在冻结前还会经过 AST/符号表检查：回调在循环中引用循环变量时必须显式绑定，
恒定表达式断言不能用于填充覆盖。修正已有测试时同样检查新写入内容，保留原方法和断言
数量约束。静态检查不执行源码，也不能证明预期值正确。
此入口支持 Opus/DSF 与 Sonnet/DSF，暂不与多样本、分层规划同用；Opus/DSF 支持冻结
策略，Sonnet/DSF 暂不支持。冻结的 planner 规则随边界请求提供，builder 使用同版本规则。
目前必须等全部契约测试校验后才执行。中断后使用 `plan --resume RUN_ID`，复用已完成的
边界及测试分片；需求、仓库提交、模型路线、并发和环境指纹固定为原会话值。响应与调用
回执在同一 SQLite 事务落盘，复用前重新校验精确请求及结果。请求模板或 schema 改变时
拒绝混用，需新建规划。已提交但尚未写入 run 的检查点也能恢复；用户改动会保留并停止。
未确认的调用先阻止重发：查看 `cost --run RUN_ID --calls` 后，如明确接受可能重复计费，
可传 `plan --resume RUN_ID --ack-unknown-call CALL_ID`。原未知回执保留，不伪装为已结算。
只有 distributed 入口保存这种规划会话；旧的普通、分层及多样本规划不提供此恢复保证。

正式的 distributed 路径固定为 Opus 5.5/high 起草边界（需求拆分、接口、归属、契约和依赖），
DSF 只设计冻结测试并实现；`--reviewed-boundary` 是另一条显式实验路径，不是默认值。
边界作者、可选审核者和控制器错误共用同一份 `BOUNDARY_RULES`：每个 R 恰有一个验收任务，
任务列出的文件和每个新 stub 文件恰有一个写入任务（未触及的基线文件不需要 owner），
每个任务至少一个 R，依赖无环；stub 真实 import 的模块的写入任务须在其直接或间接依赖中。验收负责人、文件写入者与依赖是三件事：共享 helper 可由上游任务写入，
验收该需求的下游任务依赖它即可；需求文字互相提及本身不构成依赖。普通全量 lint 允许
多个任务列同一个 R，thin 边界要求唯一验收负责人，因为冻结测试与失败按 R 路由。
重复验收的错误会写明需求和任务，例如 `duplicate-acceptance R8: T-008,T-009`。

原需求中以 `R<N>. ` 开头的段落由控制器逐字抽取（到下一个标签或规格末尾，最后一段可能
多带尾部说明，但不截断）；作者可对保持不变的需求返回空 text，控制器原样补入，省去复述
原文的输出。只支持保守子集：规格含代码围栏、首个标签后出现任何 `#` 开头的行（小标题或
注释）、或标签重复时整体返回 unsupported，不补入任何需求，作者照常写全文。结构错误和审核意见在后续
修订中一并保留。与已被拒草案完全相同的修订停在 `repeated_boundary`，相同结构错误
指纹（忽略任务编号与顺序）再次出现停在 `repeated_structural_error`，次数用尽则为
`structural_budget` 或 `review_budget`。停止原因写入 `planning/stop.json`，异常为
`plan failed lint (<code>)`；恢复只重放已结算回执，到达同一停止点，不补满预算。

同一轮至少两个不同补丁的合法候选具有完整且共同的测试失败证据时，控制器先尝试现有
独立测试审查，沿用同一冻结版本的次数限制。候选一致只是审查线索。审查认可测试则保留
原始失败和代码 diff 继续修复，修正测试后先复验已保存候选；相同补丁和不完整报告不会
触发这条额外审查。事件 `task.failure_agreement` 表示发现共错，实际调用以账本为准。

执行阶段的测试审查会保存不可变输入快照：原需求、相关需求条目、任务契约、完整冻结
测试文件及当时的争议证据。任务的 `pending_review` 指向这项操作，重启先恢复它，
不重新调用实现节点。同一请求的响应与回执共用原子事务；已结算的响应直接复用，
没有新 token，也不重复记费用。未知回执继续阻止重发；只在检查 `orch reconcile` 后，
用既有 `retry TASK --ack-unknown` 显式授权可能的重复请求，原回执保留。

审查认可测试时保存已应用标记，但指针直到修复阶梯的状态转移提交后才清除。在这两步
之间崩溃，恢复只消费原决定，不重复增加争议计数或契约说明。验证器发起的审查同时保存
原失败类别、报告和候选 diff，恢复后仍用这些证据进入修复。此类审查统一在实现工作树
释放后执行，不再由采样分支嵌套调用。
定位失败、删除断言或夹具 lint 等确定性改写拒绝也随失败状态转移结束审查，不能将同一
无效提案当成待恢复请求反复处理；提供商故障仍使用原有的回执与重试上限。

应用审查结论前重新核对输入，测试改写还经过事务中的计划、任务与审查指针检查。
并发修订使输入过期时，旧结论既不修改新测试，也不产生学习规则；只将本节点回到 READY，
契约不变时保留代码工件供新版本测试复验。无关的计划摘要变更不使审查失效。
连续三次输入失效或同一快照三次真实提供商请求后停放，防止反复刷新；未确认的调用
不因输入过期而自动获得重发授权。无效审查仍停放保留证据。

审查和契约加厚将相同版本的冻结测试放入共享缓存前缀，任务和争议留在动态区；不同
冻结文件集产生不同前缀，尚无新的真实命中率数据。`progress` 显示 `reviewing`/
`review_ready` 与审查 ID，复用事件关联原调用 ID；审查仍属于 execution，包含在
`cost --scope online`。恢复预算从账本读取，结果事件缺失不能隐藏已结算费用。

重规划必须保留原拥有文件。每个替换节点需要拥有原输出，或沿依赖边向原输出的拥有者
提供实现；后置的报告、测试审查等操作由控制器处理。控制器预检和事务状态门共用这条
图约束，拒绝提案不会新增任务或覆盖已有状态。
每个替换任务至少列出一个 req、一个拥有文件和一个能证明其 req 的原冻结测试。提案被控制器拒绝时，
错误与原提案交回作者修订一次（事件 `task.replan_rejected`），校验不放宽；仍不合法或返回空任务
则停放。

DSF 返回已结算但无法解析的答复，或编辑无法应用（搜索文本不唯一或不存在、越权路径、编辑后
无法编译）时，会在同一次尝试、同一档位内把解析位置和原文片段，或编辑错误和出错行交还 DSF，
再请求一次完整答复（事件 `attempt.format_repair`）。这次请求照常计入预算、调用数和取消；
未结算或未知的结果不走这条路径。每个不可用的已结算答复保存在
`.git/orch/<run>/responses/<call_id>.json`（probe 复制到输出目录 `responses/`），供审计和离线复现。
DSF 正式路线以 high 思考运行（`max_tokens=32768`，与 max 档同额度；`thinking: enabled`，
`output_config.effort=high`）。DSH CLI 的 API key 插件与直接 API 共用 `DshClient.output_settings`；
cli-account 插件另有配置，本仓库不修改它，也未实测。测试设计分片的格式修复与 lint 修订分开
计数：已结算的不可解析答复有 `MAX_FORMAT_REPAIRS` 次独立修复，语法错误附上一份草案出错行，
同一错误重复则停在 `repeated_lint_error`，修订用尽停在 `lint_budget`。
DSF 回执另记 wire 形状：guard 读取实际转发请求的 `max_tokens`、`thinking.type`、
`output_config.effort`，响应的最后 `stop_reason`，事件流的 turn_end 种类，以及使用的传输
（`cli`、`cli-account`、`api`）。只保留 `orch_usage.WIRE_LABELS` 声明的枚举和正整数，不记录正文、
headers、密钥或思考文本。

`plan --distributed --reviewed-boundary` 可选择 DSF 起草、原规划器审核的路径。DSF 先输出
边界，控制器通过既有 lint、索引与容量检查后，Opus（Sonnet 策略则为 Sonnet）只检查
需求是否遗漏、契约是否正确和跨模块拆分是否合理。强模型输出通过或最多 8 条修改意见，
不再重写整个边界。结构错误直接交还 DSF；语义拒绝携带原草案与意见，由 DSF 局部修订。
格式/结构失败与语义拒绝分别受规划重试上限约束，两类计数从原回执恢复；起草次数不超过
两类限额之和，仍受总调用数、费用和活动时限限制。未确认调用仍停止。起草与审核分别记为 `boundary-draft`
和 `boundary-review`；已完成响应随规划会话恢复。原强模型直接起草路径仍是默认，
当前中型仓库实测未能通过语义审核，尚未证明收益，详见
[真实对照](measurements/2026-10-31-kernel-comparison.md)。额外审核回合也可能抵消收益。

这是显式试验入口，原 `plan` 行为保留作对照，尚未承诺真实提速或更高准确率。
这覆盖使用 pytest 的标准库 Python 项目，不代表任意第三方依赖、插件或项目配置已支持。

边界生成后先检查原始需求、接口和所需拥有文件视图能否装入 builder 预算；无法装入时，
在已有规划重试上限内反馈给规划器，不先花费 DSF 测试设计调用。每个测试分片随后单独检查
加入测试后的容量，超限只重试该分片，已通过的其他分片保留。边界与结构合法的分片响应
保存在运行目录 `planning/`，包含容量拒绝原因；这些 JSON 是诊断工件，恢复依据为事务账本。

## 有版本的策略学习

```sh
scripts/orch --repo ~/code/app policy enable          # 将当前 active 规则冻结为基线
scripts/orch --repo ~/code/app policy status          # 当前内容及只追加的激活/回滚历史
scripts/orch --repo ~/code/app policy show VERSION_ID # 查看某次任务使用的准确版本
scripts/orch --repo ~/code/app policy snapshot --include r2 p1  # 使用 rule 命令中的实际候选 ID
# snapshot 只保存候选；可用 --exclude r1 评估移除旧规则的效果。
scripts/orch --repo ~/code/app bench cases.json --output .data/bench/policy-base \
  --policy BASE_ID --live --repeats 2 --max-calls 40 --max-cny 10
scripts/orch --repo ~/code/app bench cases.json --output .data/bench/policy-candidate \
  --policy CANDIDATE_ID --live --repeats 2 --max-calls 40 --max-cny 10
scripts/orch --repo ~/code/app policy promote CANDIDATE_ID \
  --baseline .data/bench/policy-base --candidate .data/bench/policy-candidate
scripts/orch --repo ~/code/app policy rollback BASE_ID
```

系统调度参数也保存在同一版本库中：

```sh
# 新策略库可连同默认调度参数启用基线；已有基线用 snapshot 创建候选。
scripts/orch --repo ~/code/app policy enable --system
scripts/orch --repo ~/code/app policy snapshot --jobs 8 --test-jobs 4 --verify-jobs 4
scripts/orch --repo ~/code/app policy suggest RUN_ID
# 对照仍显式触发；--jobs 为评测允许的并发上限，须覆盖两份策略的 jobs。
scripts/orch --repo ~/code/app bench heldout.json --output .data/bench/tuning-base \
  --distributed --policy BASE_ID --jobs 16 --live --repeats 2 --max-calls 80 --max-cny 10
```

`snapshot` 和 `suggest` 只保存候选，不调用模型、不自动激活。`suggest` 读取当前策略的
一次完整运行：重复 DSF 限流可提出降低生成并发，生成槽已满且有 READY 积压可提出提高
并发；证据不足、恢复过或有未知回执时拒绝猜测。它一次只修改生成并发，保留来源任务、
事件和调用 ID。手动候选还可调整测试设计并发、验证并发及 `--cache-mode auto|off`。
所有候选仍通过上面的 promote/rollback 流程；这些是待验证的系统参数假设。

规划会话固定策略 ID，重启后使用原规则和测试设计设置，旧会话保持未启用策略的状态。
执行与恢复默认使用该 run 保存的策略；显式 `run/resume --jobs`、`--verify-jobs` 或
`--cache-mode` 可覆盖本次执行，实际值记入 run.started。评测使用冻结参数，超出评测
并发上限在调用前拒绝；与保存策略不符的执行记录不能用于调度参数晋升。

这些真实评测命令会使用订阅及 API 额度，预算须累计管理。策略保存于仓库的 SQLite 中，
不生成 SKILL 文件，也不修改模型权重。启用后，在线经验仍生成候选，但代理分数不再直接
激活规则；每个已规划 run 固定使用原策略，恢复时不会悄悄换成新版本。

晋升要求同一引擎／suite／配置的真实完整对照，至少 12 对试验、6 个不同留出需求；
已通过的独立验收不能退步，总 token 增幅不超过 10%，且成功数增加或 token 至少减少 5%。
API 台账必须无未结清敞口，费用增加上限为原费用的 10% 与 ¥0.01 中较大者。
含调度参数的候选还要求父策略和候选的所有独立验收均通过、每项实际执行参数及耗时完整；
总耗时增加不超过 10%，并且 token 减少至少 5% 或总耗时减少至少 10%。这道门检查实际
用量和完整交付，不以队列积压等候选生成指标代替收益；当前没有通过它的真实调度新策略。
缺失用量、Mock、合成题、基础设施错误、训练需求重复或旧父版本都拒绝晋升。回滚只允许
曾经被接受的版本。该门槛是试点决策规则，尚不构成统计显著性保证；历史未记录的需求及
语义近似的数据污染仍需维护评测集时人工审查。失败规划也记录需求指纹，避免其经验泄漏
到之后的留出评测。真实策略评测开始时就在来源仓库登记需求；成功晋升也原子登记该批
评测需求，后续候选须更换留出集。先固定父版本和候选，再评测二者；不要看完父版本成绩
才创建候选。长期能力提升仍须更多独立真实数据证明。

大项目：`plan --modules`（先定模块边界，再并行子规划）、`plan --samples 3`（多份计划按分数择优）、
`run --jobs 16 --verify-jobs 8 --rpm 120 --max-cny 20`、`status --all`。

`plan`/`run` 结束时把状态和费用写入 `.git/orch/<run>/REPORT.md`。
每次模型调用默认最长 900 秒（随 effort 放大；可用环境变量 `ORCHESTRA_NATIVE_TIMEOUT` 覆盖），
超时按该级失败处理。按 Ctrl-C 会取消在途调用，任务回到 READY，run 暂停，之后可以 resume。
`run` 结束时默认给每个已合并任务跑 12 个变异体（`--mutants 0` 关闭），结果写进 REPORT。
退出码：0 全部合并，1 环境或输入错误（`audit` 有发现时也是 1），3 有 PARKED，
4 暂停（额度/登录/限流/中断/花费上限），5 规划未通过 lint。
不传 `--repo` 时使用当前目录，但拒绝在 Orchestragent 自身的仓库里 plan/run。

## 角色与路由

| 角色 | 路由 | 说明 |
| --- | --- | --- |
| Planner、例外 patch | `claude/claude-opus-5-5` 原生订阅 | 规划 effort=high，patch effort=medium |
| Builder（DSF） | `dsh/deepseek-flash`（V4.1 Flash 1017） | 写死在 `orch_run.DSF`，不可配置 |
| 分诊、预筛（Jev） | `vercel/typesafe-ai/jev`，经 Vercel AI Gateway 的 API 预算 | 可选；未配置时用确定性规则 |

DSF 模型锁定有两层：调度器只构造 `dsh/deepseek-flash` 路由；DSH 本地计数代理
（`dsh_guard`）检查每个请求体的 `model` 字段，不是 `deepseek-flash` 就本地返回 400，
请求不会到达 DeepSeek，也不占用单次请求额度。代理还记录 DeepSeek 返回的模型名
（`usage.served_model`）。API 只暴露别名 `deepseek-flash`；如果 DeepSeek 把别名指向
新版本，只能从返回的模型名发现，无法在请求侧锁定 1017。

DSF 费用：每次调用仍先预留 ¥3 做额度检查，成功后按返回的 token 用保守峰值价结算
（`providers.settled_cents`：输入 ¥2.5/M、缓存命中 ¥0.05/M、输出与推理各 ¥10/M，
取整到分，最少 1 分，不超过预留）。拿不到 usage 时按预留全额扣。

## 流程

1. **plan**：Claude 输出 requirements、接口 stub、冻结 unittest 文件和任务 DAG。
   测试方法名 `test_r<N>_...` 是 `@req("R<N>")` 的标准库写法。确定性 lint 检查：
   需求都有测试和任务、测试都有归属且证明本任务的需求、文件所有权不相交、
   不拥有测试文件、依赖存在且无环、含 `NotImplementedError` 的 stub 都有所有者、
   不覆盖已有文件、Python 可解析，以及静态依赖：stub 导入了别的任务的模块，就必须依赖那个任务。
   不通过就带着 lint 错误重拆，最多 3 次。通过后在 `orch/<run>` 分支提交
   checkpoint 并打 tag `orch/<run>/plan`；在 stub 上试跑全部冻结测试（一次沙箱运行），
   已经通过的任务标记为 `plan.vacuous_tests`。`--samples N` 并行要 N 份计划（每份带不同思路
   提示），各自过 lint，按确定性分数取最高（见“Planner 进化”）。
2. **run**：就绪任务按最长依赖链优先并发派发。`interface_depends_on` 可将 `depends_on`
   的一部分显式声明为仅需接口即可开始实现的依赖，缺省为空。控制器校验它是无重复子集，
   其他依赖仍须完成。提前构建的候选先过结构门，再保存 commit 等待依赖的真实实现；
   等待不消耗失败阶梯、不占生成槽，依赖就绪后重放已有代码并验证，不重新调用模型。
   每次尝试在独立 detached worktree 中进行：
   构造上下文（含沿调用图打包的、已合并依赖的实现代码）→ 调用模型 → `apply_edits`（只允许改本任务
   拥有的文件，结果必须能解析；search 先精确匹配，再容忍行尾空格和整体缺失的缩进，但始终要求唯一匹配）
   → 带 trailer 提交 → 验证（改动范围、stub 签名未变、导入检查、本任务测试在沙箱内通过）
   → 合并队列：cherry-pick 到集成分支，再跑本任务的测试和“这次改动可能影响到的”已合并测试
   （测试影响分析，见“代码索引”），失败则回滚。
3. 全部完成后打 tag `orch/<run>/done`。用户自己的分支和远端都不碰。

## 状态机与失败阶梯

`PLANNED → READY → RUNNING → VERIFYING → MERGING → DONE`，另有 `PARKED` 和 `REPLANNED`
（任务已交给重规划产生的新任务，视同完成）。状态只由调度器写入，每次状态变化和事件
在同一 SQLite 事务里（`<repo>/.git/orch/state.sqlite`）。进程被杀后，`run`/`resume`
会把 RUNNING、VERIFYING、MERGING 的任务放回 READY。同一个 run 有文件锁，不能并发执行两次。

阶梯（`orch_ladder.py`），每级对每个任务最多用一次，所以单个任务的花费有上限：

| 级 | 做法 |
| --- | --- |
| 0 | DSF 首次实现 |
| 1 | DSF 带上次 diff 和报错重试（新会话） |
| 2 | DSF 并行 k 路采样：`k = ⌈ln(1−0.9)/ln(1−p)⌉`，限制在 2–4；p 是本仓库历史上 DSF 首次通过率（拉普拉斯平滑）。各路只在任务层附加不同的思路提示，共享前缀不变。全部跑完再验证（中途取消的 DSH 调用会按 ¥3 记账，跑完反而更便宜）；所有通过的候选都保留，合并闸门拒绝第一个时自动换下一个 |
| 3 | Claude 直接 patch，同样走完整 verifier，只能改本任务文件 |
| 4 | Claude 加厚契约（伪代码、边界用例及期望值、反例），再交给 DSF |
| 5 | Claude 对子树局部重规划：失败任务、它需要的 PLANNED 文件所有者以及所有下游任务，被替换为新任务；替换结果必须带着完全相同的冻结测试并通过同一套 plan lint |
| 6 | PARKED：不依赖它的任务继续执行，依赖它的任务保持 PLANNED，status 中显示 “blocked by” |

跳级规则（代替 Jev 分诊的确定性规则）：
- 同一错误签名或同一 diff 再次出现时，跳过 1、2 级，直接交 Claude（第 3 级）。
- p < 0.3 时不做采样，第 2 级换成第 4 级（加厚契约）。
- builder 报告 `BLOCKED`（需要越界修改）时，直接进入第 5 级。
- 报告 `TEST_DISPUTE` 时由 Claude 复审测试：
  - 独立对照原始需求，生成的契约与测试不能覆盖原始需求。无效或自相矛盾的复审结果会 PARK，
    不会默认为“测试正确”。这是输入与流程约束，不保证模型能识别所有语义冲突。
  - 判定测试正确：把解释写进契约，按一次失败继续爬阶梯。
  - 判定测试有误：Claude 给出对测试文件的最小修改，经 lint 和已合并任务测试把关后，
    作为新的冻结测试提交，该任务从第 0 级重新开始。新测试版本如果暴露另一处独立错误，
    可再次复审；每个任务最多 3 次。被驳回且未修改的测试版本不反复复审，恢复运行也保留计数。
- 重规划产生的任务最多还能再重规划一次，之后直接 PARKED。

厂商返回 `quota_exhausted`、`auth_required`、`rate_limited`、`waiting_approval`、
`policy_blocked` 或被取消时，不算任务失败：任务回到 READY，不消耗尝试次数，
run 暂停（退出码 4）。处理原因后执行 `orch resume`；不会切换厂商、账号或计费方式。
合并冲突不交给 LLM：任务带着新 base 回到 READY，最多 3 次。

## 并发、熔断与回滚

- 并发数 = min(就绪任务数, AIMD 上限, 剩余 DSF 预算 ÷ ¥3)。AIMD 上限从 `--jobs` 起步：
  限流（429）或合并冲突时减半，限流还会退避 2、4、8……秒（最多 60 秒）；
  每合并一个任务加 1。连续限流超过 6 次才暂停 run。预算不足一次预留时，不再派发并暂停。
- 模块熔断：同一目录下 3 个任务 PARKED（不含熔断本身造成的），该目录剩余任务一并 PARKED，
  其他目录照常执行。`orch retry` 可逐个恢复。
- 最终闸门：规划时记录基线，即仓库原有的、在 base 上通过的 unittest 用例。全部任务完成后，
  在集成分支上一次跑基线加全部已合并任务的测试（合并闸门只跑了受影响的测试，这里兜底）。
  基线不通过就在任务 commit 上二分；某个已合并任务的测试不通过，就从该任务自己的合并 commit
  （它的闸门证明那里是绿的）往后二分（等价于 `git bisect run`，用同一个沙箱运行器）。
  找到第一个坏 commit 后，`git revert` 该任务及依赖它的已完成任务，把罪魁任务连同失败证据
  放回 READY（从第 1 级开始），依赖它的任务回到 PLANNED。最多 2 轮，仍不通过则 run 以 parked 结束。
- 生成与验证分开：只有还在调用模型的任务占 AIMD 名额；进入验证或排队合并的任务不占。
  所有沙箱运行（验证、闸门、探测、变异）走一个独立的有界池（`--verify-jobs`，默认 CPU 数）。
- 缓存只在同等关键路径优先级下影响顺序；没有实测依据时，冷请求直接并发，不额外调用模型预热。
- `--rpm N`：全局令牌桶，限制每分钟发起的 builder 调用数。
- `--max-cny X`：可选；本 run 的模型花费达到 X 元就暂停（resume 继续累计）。默认 0 = 关闭，没有其他本地额度。
- worktree 池：同类 worktree 复用（强制 checkout + `git clean`），命令结束时全部清理。

## Jev：分诊与预筛

Jev（TypeSafe AI 的决策模型）经 Vercel AI Gateway 调用：`POST https://ai-gateway.vercel.sh/v1/evaluate`，
模型 `typesafe-ai/jev`。它只回答选择题，输入只有契约、diff 和报错。

- **配置**：`scripts/login.command` 选 5。输入人民币上限（建议 ¥10），输入 YES，再输入
  Vercel AI Gateway key（不回显，只存本机钥匙串）。不经过服务器，也不进仓库、日志或聊天。
- **计费**：每次调用先从 Jev 预算预留 ¥0.02，按网关返回的 `providerMetadata.gateway.cost`（美元，
  按 7.5 换算后向上取整，最少 1 分）结算。被拒的请求（401/402/429/5xx）释放预留；
  网络不可达时按预留扣。不重试，不回退其他模型。
- **分诊**：任务失败后，Jev 在仍可用的阶梯级（retry、sample、claude_patch、thicken、replan）
  中选一个。概率 ≥ 0.6 才采用，否则用确定性规则。
- **预筛**：DSF 候选通过测试后，Jev 判断 diff 是否针对测试输入写特例或硬编码期望值。
  概率 ≥ 0.85 时按 `suspect` 失败处理，进入阶梯。
- **模式**：`orch run --jev auto|on|shadow|off`。`auto` 表示已配置就开启。`shadow` 只记录
  Jev 的判断、不执行，用于先在自己的数据上校准阈值。Jev 返回登录、预算类错误时，
  本次 run 自动关闭 Jev，继续用规则。
- **校准**：`orch jev` 显示调用数、花费，分诊与规则的一致率、高置信数、采用数，
  以及预筛命中情况；`orch cost --by model` 里有单独一行 `vercel/typesafe-ai/jev`。

## 自进化：builder 规则

规则生命周期（`orch_rules.py`）：lesson → candidate → A/B → active → retired。

- **产生**：Claude 的 patch（第 3 级）成功合并时，附带的一句 lesson 成为候选规则（重复的去重）。
- **A/B**：每次单路 DSF 尝试（第 0、1、4 级）选一条试验最少的候选规则。按 `规则/任务/尝试`
  的哈希确定性分组，一半在任务层加入该规则，一半不加。规则放在任务层，共享前缀的缓存不受影响。
  合并成功或失败都计入该规则对应组的统计。
- **生效**：只在每次 run 开始（checkpoint 边界）评估。每组各至少 6 次、通过率提升 ≥ 0.15、
  单侧 z ≥ 1 时提升为 active；下降 ≥ 0.10 时退役。active 规则按增益排序放进共享前缀的
  “LEARNED RULES” 区，总量不超过 1200 字节，放不下的退役。commit trailer `Orch-Rules`
  记录当次 prompt 里的规则。
- **命令**：`orch rule` 列表，`orch rule r3` 查看，`--promote`/`--retire` 手动干预，
  `orch cost --by rule` 按规则组合统计花费。

弱测试上的通过不算证据：变异分数低于 0.6 的任务，其 A/B 观测会被撤回（`rule.discounted`），
防止规则学成“专门过测试”。

## Planner 进化

planner 规则（id 为 `p*`）与 builder 规则共用生命周期，规则区放在规划 prompt 里（1500 字节）。

- **来源**：每类 lint 拒绝对应一条确定性教训（例如“每个需求至少一个冻结测试”）；变异测试的
  存活算子对应一条测试写法教训（例如 ROR 存活 →“比较的边界值本身和两侧都要测”）；
  子树重规划和判定 `tests_wrong` 的测试复审，都会让 Claude 附一句 lesson。
- **在线 A/B**：每次 `plan` 把试验最少的 planner 候选按 run 确定性分组，一半 run 显示它。
  plan 的 checkpoint commit 记录 `Orch-Rules`。
- **代理指标**（`plan.proxy`，规划时立即得到）：首稿即过 lint、没有空转测试、没有把 git 历史上
  强共变的文件拆给互不依赖的任务。还有一个连续分数（并行度、每个需求的测试数、空转测试、
  拆散的耦合、lint 返工次数），用来在 `--samples` 的多份计划里择优。
- **真实结果**（`plan.outcome`，run 结束时一次）：原计划任务中被重规划、测试被纠正、
  或因重规划失败而 PARKED 的比例（“BLOCKED 率”）。
- **离线回放**：`orch evolve [p2]` 在历史 spec 的原 base 上，带与不带该候选各规划一次（配对），
  只跑 lint 和 stub 探测，不构建；结果计入代理统计。每次回放用 2 次 Claude 规划调用（订阅）。
- **代理校验**：`orch evolve --check` 计算代理分数与真实成功（1 − BLOCKED 率）的 Spearman 相关。
  不足 5 个 run 时暂时信任代理；之后 ρ < 0.3 就不再信任，planner 规则改按真实结果判定。
- 状态只在 `plan` 开始时变化（planner 规则的 checkpoint 边界）。
- **共变耦合图**：完全来自 git 历史（不花 token），写进规划 prompt，也用于上面的代理指标。

## 上下文与缓存

### Token 账本与调用追踪

`orch cost` 默认按任务汇总；规划、模块规划和规划重试单独归入 `[shared]`，不把它们
重复摊入每个任务。实现、失败重试、全部并行候选（包括未选中者）、Claude 修复／契约
加厚／重规划／测试纠正、Jev 分诊与预筛都记录。`REPORT.md` 自动包含任务及模型汇总。

```sh
scripts/orch cost --run r12345678                  # 指定 run；省略时查看最近调用所属的 run
scripts/orch cost --task T-001 --calls              # 任务的逐次调用
scripts/orch cost --task '[shared]' --calls         # 共享规划开销
scripts/orch cost --call <完整调用ID> --json         # 一次调用的结构化证据
scripts/orch cost --by purpose                     # 按 plan/build/patch/triage 等用途汇总
scripts/orch cost --scope online                   # 规划与执行，不含 evolve/harden
scripts/orch cost --scope evolve --calls           # 单列离线进化开销
scripts/orch cost --scope harden --calls           # 单列测试加固开销
scripts/orch cost --json > usage.json              # 默认包括所有 scope
```

账本存于目标仓库 `.git/orch/state.sqlite` 的 `orch_calls` 表。每次请求发出前先写入
唯一 ID；结束后更新结果。规划还没成功创建 run 就失败，也能用日志中的 run ID 查询。
恢复执行不会覆盖旧调用；同一个任务／attempt 的再次请求具有新 ID。仍为 `pending` 的
记录可能正在运行，也可能进程已中断；它不表示零消耗，不能凭本地记录补出厂商未返回的 usage。

每次记录含 run、任务、attempt、采样号、用途、scope、请求模型、effort、起止时间、
原始 usage 中允许保存的计数字段、会话 ID、base revision、规则 ID、请求指纹和共享前缀
指纹。请求指纹覆盖 prompt 和 schema；不保存 prompt／模型回复正文或凭据。执行结果事件
用 `call_id` 关联账本，可以沿任务／尝试／采样号追到候选提交和最终合并事件。
调用的 `outcome` 表示厂商请求结果；`ok` 不代表产出的代码已通过测试或合并。

统计约定：

- Claude 和当前 DSH／DeepSeek Messages 的 `input_tokens` 不含缓存读取／写入；
  输入总数包含这三项。DSH 返回的 `total_tokens` 也可用来还原完整输入。
- Codex、Jev 的输入已是输入总数；Codex 的缓存读取是其子集。输出中的推理 token
  不再加一次。未知格式不猜测总输入。
- 命中率 = 缓存读取 token ÷ 总输入 token，含缓存写入，不含输出；按 token 加权，
  不是调用次数比。前缀指纹相同不证明缓存命中，以厂商返回的 usage 为证据。
- 缺失字段显示 `?`；`N+?` 表示仅已知部分的合计。JSON 的 `known_tokens` 必须和
  `reported_calls` 一起读取；逐次调用的未知字段为 `null`。字段不足或互相矛盾时不输出
  猜测的总量／命中率；矛盾原因在逐次 `tokens.issues` 中。
- `ledger_CNY` 是现有保守预算结算，不是厂商账单。Jev 返回的 `cost_usd`、原生客户端的
  `provider_estimate_usd` 分别保留在 JSON；订阅调用不虚构 API 花费，未改变预算预留策略。
- 旧版本只保存的 `attempt.result`／`jev.call` 可继续查询，标记 `legacy`；历史规划与
  中断调用无法事后补齐。新旧记录共存时，已关联的事件不会重复计数。

`orch_build.context_pack` 按变化频率排列各层：

1. 固定指令
2. active 规则区（只在 run 开始时变化）、原始需求原文与 repo map 快照（checkpoint 时冻结，签名级）
3. 当前任务声明依赖及导入辅助模块的相关接口
4. 本任务接口、契约、拥有的文件、相关冻结测试，以及沿调用图打包的已合并依赖代码
5. 重试时附上的 diff 和报错

第 1–2 层形成项目公共前缀；第 3–5 层按任务选择，不向每个节点发送全项目骨架。
原生 Claude 的公共前缀放入已有独立系统区段，任务源码、测试与反馈放入用户消息；
不同任务的补丁使用同一个请求 schema 时，有机会复用项目前缀。审查、契约补充同样将任务证据
放在动态部分，各自只承诺上下文分离；不同 schema 之间不假设能共享缓存。
事件 `attempt.called.prefix` 记录实际共享前缀哈希；是否缓存命中以厂商回执为准。
DSF 保留官方 `deepseek-flash` 的 1,048,576 token 窗口（输入与输出合计），
撤销旧的 40 KiB 字节限制与 6 KB 依赖源码预算，保留任务相关的完整依赖实现。
客户端不以字节数冒充 token 数；16 MiB 只是本地文本载荷/内存护栏，
实际 token 上限由厂商执行，输出仍按请求保留 8,192 token（高推理档有独立输出预算）。
容量按需使用，不填充无关内容，也不把整个仓库广播到每个节点。
官方依据：https://api-docs.deepseek.com/quick_start/pricing/ 和
https://api-docs.deepseek.com/api/list-models/ （2026-10-30 核实）。
达到本地载荷护栏时，优先保留能装入的完整依赖实现，再省略可选依赖实现及 repo map。
原始需求完整保留；超过 16 KiB 的拥有文件按冻结测试引用选择定义，保留模块状态与相关辅助代码。
反射、目标不明或无法可靠解析时仍提供完整源码，显式 NEED_FILES 可请求完整拥有文件。
部分拥有文件仅允许修改已展示的源码；整文件替换必须先获取完整文件。已有文件允许每批
最多增加 80 KB，新增文件上限仍为 80 KB，原文件大于该值不再导致小补丁被拒绝。
相关接口与测试按符号、测试方法裁剪后作为必需内容保留，
仍超限则要求拆分任务；map 被省略的
请求会有不同前缀，不能假定所有请求都命中同一缓存。
按需读取的选择器按固定顺序呈现，类定义已包含的方法不重复发送。
本地容量失败进入有上限的重新拆分，不因容量问题自动升级 Opus 补丁，也不训练为模型能力失败。

实现、测试复审和契约补充均接收 `run.spec` 原文。当前方案不压缩或截断原始需求；
规划阶段把这部分纳入上下文容量检查，过长需求可能需要先明确缩小任务范围。
`scripts/orch_repository_probe.py` 将调度器状态单列为 `scheduler_status`；即使内部状态为
`done`，隐藏验收失败时报告顶层 `status=failed`。框架测试通过与真实交付通过分开报告。

`orch context T-001 --run RUN_ID --json` 在 run 的已验证提交上查看拥有的文件、接口
和冻结测试路径、已申请的补读选择器；加 `--show` 查看接口和测试节选。
复用 builder 的同一选择逻辑，不调用模型或读取密钥。它不是完整
请求重放：运行时还会加入按预算选择的依赖实现、规则及重试反馈；也不代表历史调用。

Python 节选保留原始代码行、装饰器、模块状态、fixture、setup 和相关辅助函数，并标明
`PARTIAL CONTEXT`，不能作为整文件替换内容。无法确定符号、动态反射、解析失败等情况
保守退回整文件。裁剪只影响模型输入，不修改源文件，也不改变实际验证执行的测试。

集群 builder 可返回 `status: NEED_FILES`、空 `edits` 和 `need_files`，请求 tracked Python
文件或 `path.py::Symbol`。每次 1–8 个选择器，每任务最多 3 轮、累计 16 个选择器；
无效请求也消耗轮次，计数跨重试和恢复持久化。读取固定在该次构建的 base revision，
不追随工作区临时改动，也不扩大文件写入权限。下一次调用重建上下文，不累积对话全文；
补读是必需内容，超出预算则 PARKED。每次调用单独记账并检查停止条件与预算，
`context.request` 通过 call ID 关联原始请求。补读本身不提高失败阶梯或切换模型。

`run` / `resume` 默认 `--cache-mode auto`；可用 `--cache-mode off` 禁用缓存调度提示。
未知缓存状态直接并行启动，不再固定串行预热首个请求。关键路径优先级不变，
仅同优先级任务按 provider、model 和实际打包后的 prefix hash 做缓存亲和排序。
回执缺失、矛盾、调用失败或已知服务模型不匹配时，不作为缓存证据；旧账本中没有原始
计数有效性标记的记录也不用于恢复缓存状态。最多使用最近 128 条回执，证据有效期
300 秒只是本地保守窗口，不是厂商缓存 TTL。

只有同前缀、同输入输出 token 数的历史中各有至少 3 次冷请求和命中请求，且最慢命中
仍快于最快冷请求，才可能等待一个接近完成的同前缀请求。缓存策略最多主动延后
250ms，之后按正常资源约束派发；无关任务仍可使用其余空槽。混合大小的真实调用
通常不会触发等待，这不是已证明的最优策略或提速保证。
`orch cache --run RUN_ID --json` 查看最近一次执行保存的调度摘要，
`task.cache_wait` / `run.cache` 记录等待依据与计数；它们不是实时厂商缓存查询。

## 溯源

### 通信、恢复与一致性

当前执行边界是**单机、单 run 控制器**：`exclusive` 文件锁阻止两个执行器同时接管同一个
run。不同数据使用不同一致性，不通过模型讨论来决定状态：

| 数据 | 一致性与交接 |
| --- | --- |
| 任务状态、依赖、重试授权 | SQLite 事务内校验和修改，状态与事件一起提交；动态加边复用规划阶段的 DAG 检查，不能并发引入环 |
| 候选代码 | 不可变 Git commit + task/contract/model trailer；SQLite 保存候选指针，依赖就绪后重放并重新验证，不要求模型转述或重新生成 |
| 合并结果 | merge lock 串行化 Git 修改；验证成功后 `DONE` 与 `verified_head` 同一事务持久化，恢复时回滚到已验证提交 |
| 上下文、索引 | 按 revision/blob 固定快照；节选不会追随工作目录变化，索引按内容复用 |
| token/费用 | 每次 call ID 的持久回执；未确认结果保留未知，不能当作零费用或已失败推断 |
| 缓存、状态展示 | 可滞后的观测提示；缓存证据过期后不用，`reconcile` 是观测快照，不参与合并裁决 |

模型主要产生实现和结构化动作。依赖 ID、候选 commit、版本、验证结果、状态事件由
控制器直接处理，节点之间不互发自然语言进度摘要。动态导入依赖由既有 index/AST
发现；任务等待依赖时保存候选、释放执行资源。依赖合入后或进程恢复后，候选通过
cherry-pick 重放并重新走所有权、接口、导入、测试和合并验证。事件
`task.candidate_reused` 记录来源与目标提交；依赖等待不消耗失败阶梯。

接口依赖的契约包含原始仓库版本、需求、接口声明及行为约定，候选的 `Orch-Inputs`
绑定其使用的契约版本。实现提交和测试纠错本身不改变这些版本；契约变更后，旧候选
在恢复或合并时会失效，不能按旧假设继续交付。控制器保留旧候选与 diff，只重排受影响的
消费者；单任务最多自动刷新三个不同契约版本，同一版本不重复派发。超过上限会停放，
事件 `task.inputs_changed` 记录变化的依赖与旧候选。当前按依赖契约粒度传播，尚非符号级
语义影响分析。`attempt.called.dependency_contracts`
保存每项版本用于追溯。既有运行缺少 `interface_depends_on` 时保持原先的完整依赖等待。

候选先过确定性结构门，再跑行为测试。复用 `codeindex.definitions` 和
`orch_index.is_stub`，在固定 commit 上检查编译、冻结接口的残留占位体、新增的同名定义
覆盖、已有实现退化。既有重复定义、overload、Protocol、property 和抽象声明不被一概
判错；新辅助钩子只有在测试实际执行到其纯占位体时才被归为结构错误。
这一检查只覆盖共享 AST 定义遍历和明确的 `NotImplementedError` 占位模式，不能代替
行为验收。只有 `test` 类失败允许多候选采样；决策器不能越过这个资格约束或重选已用阶梯。

冻结测试最内层帧的 NameError/UnboundLocalError 会请求独立复审。普通断言失败保留
结构合格候选的 `failed_candidate`，供之后确认测试错误时复验；失败本身不授权改测试。
测试修正后先校验候选的 run/task/contract/model、单父提交和文件所有权，再复用原路线
候选；无法复用则记录 `task.candidate_discarded` 并回到正常构建。测试计划、已验证 HEAD、
任务状态和纠错事件在同一 SQLite 事务更新，旧验证失效，代码必须重新通过测试与合并门。
`--discard-candidate` 同时清理待验与此前失败的候选指针。

普通恢复中，待验候选丢失或契约/模型不匹配会 PARKED，不偷偷重新调用模型；重放发生冲突或验证失败
才进入已有修复阶梯。候选只在落盘后可恢复，模型返回到候选保存之前的崩溃窗口不能
凭空重建响应。这里没有承诺厂商推理的 exactly-once，也没有跨机主控选举或网络分区
共识；跨机部署前仍需独立实现租约与 fencing。

恢复发现本次尝试有未确认回执且没有可用候选时，任务 PARKED。请求发出后超时、取消、
网络失败且不能证明完成/拒绝，同样停止自动重试。仍可用 `orch reconcile --run RUN_ID
--json` 查看未知 call ID、候选、缺失工件、分支与已验证 HEAD 是否一致，以及等待/停放
原因；该命令不调用模型、不改状态、不释放费用预留。
这是非原子观测，运行期间分支和状态可能正在推进；不能仅凭瞬时 HEAD 不同判定损坏。
DSH 与直连传输复用同一份明确拒绝状态列表：429 等确定拒绝可退避/释放预留，
408、未知 4xx、5xx 等不被冒充为零消费。

确认接受潜在重复计费后，显式 `orch retry T-001 --run RUN_ID --ack-unknown` 才会允许
重新运行；它只记录重试授权，不假造厂商结算。若要放弃损坏或过期候选并重新生成，
需明确加 `--discard-candidate`。普通候选恢复保留原模型阶梯，不再额外加一次 thicken。

无进展、资源等待和停止边界：

- 再次请求已经提供的文件/符号会 PARKED；多个并行样本从旧快照请求同一内容时合并
  为一次补读轮次，记录 `context.shared`。整文件已提供时不再重复打包其符号节选。
- 控制器在派发前、限速等待后检查停止条件；取消可中断验证排队和冷却等待。
- 调度器基础设施异常停止派发并取消在途工作；恢复时重建槽位计数，避免遗留“忙槽”空转。
- 深依赖链使用迭代排序，不依赖 Python 递归栈；恢复发现缺失依赖、自依赖或环会拒绝执行。
- orchestration CLI 不继承旧服务的无限模型超时：环境变量值为 0 时，本客户端使用
  900 秒基础超时（原生模型按 effort 应用既有倍率）。服务配置本身不变；正有限显式
  超时仍有效。运行中的沙箱沿用已有超时和进程清理。
- 自定义 `Caller` / `Runner` 插件必须遵守取消与有限超时契约。当前线程执行器不能强制
  杀死无视契约的任意 Python 插件，因此不能把内置传输的验证外推到所有第三方插件。

每个任务 commit 带 trailer：`Orch-Run`、`Orch-Task`、`Orch-Req`、`Orch-Attempt`、
`Orch-Plan`、`Orch-Contract`（契约哈希）、`Orch-Model`、`Orch-Rules`。缺少任何一个
都不合并。事件按 `run → run/task → run/task/aN` 记录父 span，数据库约束拒绝缺父 ID 的事件。
每次尝试的 commit 保留在 `refs/orch/<run>/<task>/aN`，作为失败证据。

## 代码索引（tree-sitter + ast）

`orch_index` 按 git blob 缓存每个文件的索引（`.git/orch/index.sqlite`）：同一个 blob 只解析一次，
每次 merge 后只解析变化的文件，每个 plan checkpoint 存一份快照（与 repo map 冻结时机一致）。

- **两级**：Python 用标准库 `ast`，有语义：解析导入（含相对导入）、每个定义引用的符号。
  其他语言（JavaScript、TypeScript/TSX、Go、Rust、Java）和无法解析的 Python 用 tree-sitter，
  只有语法级定义。tree-sitter 是默认安装的可选依赖组 `index`；没装时 Python 照常工作，
  `orch doctor` 会显示当前后端。没有接 SCIP/语言服务器。
- **repo map**：签名级，按路径排序、字节稳定；超预算时保留被引用最多的文件并注明省略数。
- **依赖代码打包**：DSF 的任务层附上它的 stub 和测试沿调用图（最多两跳）用到的、已合并的实现
  代码（按距离排序，已撤销旧 6 KB 截断）。放在任务层，不影响共享前缀缓存。
- **静态依赖 lint**、**导入检查**（第三方包在沙箱里不可用，直接报清楚；导入了尚未构建、又不是依赖的
  任务的模块时，自动补上依赖、回到 PLANNED 等它合并，不消耗阶梯级，除非那会形成环；导入已合并但
  未声明依赖的任务，合并时自动补进 `depends_on`，回滚链因此完整）。
- **测试影响分析**：合并闸门只跑导入闭包（含沿途各级包的 `__init__.py`）能看到本次改动的已合并
  测试模块；改了非 Python 文件、测试模块不在索引里、或闭包里有动态导入时，一律跑。最终闸门只在
  “该测试在证明它变绿的 commit 上单独通过、在 HEAD 上单独失败”时才二分；只在和其他测试一起跑时
  才失败（测试互相干扰）则暂停 run 并说明，不冤枉任何任务。
- **测试纠错不许削弱**：Claude 修正冻结测试时，任何测试方法的断言数都不能减少。
- **错误签名**用 traceback 里的符号（`path::function`），不含行号和 worktree 路径。
- **`orch trace <符号>`** 对整个定义的 AST 范围做 `git blame -w -M -C`，列出写过它的每个任务；
  函数移动或重新格式化后仍能追到原任务。

## 变异测试与 harden

`orch_mutate` 只改已实现函数里的一个结构（算术、比较、逻辑、取反、常量、返回值、条件、
累加、break/continue），其余字节不变，并在每个函数间轮流抽样（按任务确定性）。run 结束后在
沙箱里用任务自己的冻结测试跑这些变异体（超时按基线耗时自动定）：

- 分数进 `status`（`mut=0.83`）、REPORT（最弱的任务和存活变异体）和 `orch mutate`。
- 分数低于 0.6：该任务的 A/B 观测撤回，并给 planner 留下测试写法教训。
- `orch harden`：Claude 只能往任务的测试文件里**新增**测试方法（已有测试方法的 AST 不能变，名字要
  对应任务的需求），新测试必须在已合并代码上通过（和其他测试一起、单独各一次）、不破坏任何已合并
  测试、并且至少杀死一个存活变异体，才作为新的冻结测试提交；done tag 随之前移，任务重新打分。
  被拒绝、中断或出错时，分支上不留下任何未验证的提交。
- 沙箱里的 Python 带 `-B`，改写过的文件不会用到过期字节码。

## 大规模模式

- **分层规划**（`plan --modules`）：一次规划回答装不下几百个任务。顶层只定模块：每个模块的
  公开接口 stub、它独自证明的需求、依赖哪些模块（无环）。然后每个模块在自己的任务号段内并行
  子规划（只能写自己目录和 `tests/<模块>/` 下的文件，接口原样保留）。合并由代码完成：
  stub 导入了谁就依赖谁，测试导入了别的模块就依赖那个文件的所有者。合并结果过完整 lint。
- 并发分离、预热、`--rpm`、`--max-cny`、worktree 池、测试影响分析见上文；计划最多 2000 个
  文件（每个任务仍须放进一个 builder prompt）。
- 超过 30 个任务时 `status` 和 REPORT 显示汇总（各状态计数、阶梯分布、合并速度、花费、
  闸门跑了/跳过了多少测试、在途任务），`status --all` 逐个列出。
- `orch audit`：事件父 span 链、按事件重放的状态迁移是否合法、日志与状态是否一致、分支 /
  verified HEAD / done tag 是否一致、每个合并 commit 的 trailer、遗留 worktree、索引快照，
  以及（默认）在 HEAD 上跑全部已合并测试。
- 压测：`PYTHONPATH=src uv run python scripts/orch_scale.py` 用合成的宽计划（不调模型、
  不花钱）走真实的调度器、git、沙箱、索引和变异测试，结果见 [orch-scale.md](orch-scale.md)。

## DSF 单体执行与按需上下文

```sh
scripts/orch --repo /path/to/project solve task.md --live \
  --max-calls 3 --context-bytes 16000
scripts/orch --repo /path/to/project cost --run single-RUNID --calls
```

`solve` 默认只使用 DSF；`--model sonnet|opus` 是显式选择，不会失败后自动切换。
读取索引选出的相关文件，并允许模型用 `need_files` 请求当前 Git revision 中的 Python 文件。
一次文件请求也消耗一次调用额度；预算是完整请求的 UTF-8 字节数（包含 schema），不是 token。
原有测试和测试配置保持不变；替换已有源码前必须先读取它。完整文件装不进预算时明确停止，
不截断源码后让模型猜测。结果保存在独立 `orch/<run>/single` 分支，当前工作分支不被合并。
目前此入口验证 Python stdlib/unittest 项目，使用已授权的 API 预算；不自动续跑或升级模型。

`bench --strategy single-dsf --context-mode scoped --context-bytes 16000` 使用相同执行路径。
也可对 `single-sonnet` / `single-opus` 使用同一上下文策略。`--context-mode full` 保留旧基线；
它按文件顺序放入最多 32KB 源码，适合做旧行为对照，不应当作大仓库检索能力。
`--baseline-attempts` 是单体每题总调用次数，包含读取请求和修复。

该入口先解决节点便宜、上下文受控的问题；大项目自动拆分并行尚不由 `solve` 提供。

新增 unittest 测试优先放在仓库根目录；若放入子目录，所有父目录必须有
`__init__.py`。提交前会检查这一点，避免测试文件存在却未被 discovery 执行。
应按实际变更依赖图决定并发，不能仅因仓库大就启动多智能体流水线。

## Sonnet + DSF 轻量决策策略（实验）

`plan --strategy sonnet-dsf` 使用 Sonnet 5.5 medium 生成需求、接口 stub 和任务边界，
由 DSF 在实现开始前独立生成冻结 unittest。测试生成方只看需求、接口和原有仓库上下文，
不看后续 builder 的候选实现。组装后的计划仍经过原有 lint、stub 检验、文件归属和沙箱闸门。
实现使用原有 DSF DAG 调度器，失败阶梯中的 Claude 调用改用 Sonnet；该策略禁止 Opus/Jev。
测试生成是额外的一次或多次付费调用，计入 `test-design` 收据、调用上限和 DSF 预算。
策略保存在 run 中，`run` / `resume` 恢复它；默认的 `--jev auto` 在该策略下关闭，显式
要求 Jev 会报错。旧的 `opus-dsf` 仍是默认策略。

```sh
scripts/orch --repo /path/to/project plan spec.md --strategy sonnet-dsf
scripts/orch --repo /path/to/project run --jobs 4 --jev off
scripts/orch bench examples/orch-bench/sonnet-pilot.json \
  --output .data/bench/my-sonnet-pilot --live --strategy sonnet-dsf \
  --jobs 4 --max-calls 30 --max-cny 15
```

基线策略增加 `single-sonnet`（同一 Sonnet/effort）并保留 `single-opus`。
报告记录实际模型、规划、测试生成和模型介入次数。Sonnet 的模型标识已通过本机真实订阅请求
验证；官方标识见 [Claude Code 模型配置](https://support.claude.com/en/articles/11940350-claude-code-model-configuration)。
探针可用 `scripts/native_probe.py` 重复运行，每次使用新的输出目录；只保存过滤后的用量和模型
标识，不保存原始 CLI 输出或凭据。未报告或混合模型标识不会被探针视为验证成功。

目前该策略不支持 `--modules`、多个规划样本、旧的冻结策略或 `evolve`，会在调用模型前拒绝。
这是显式分工实验；是否更快、更便宜、更准确由独立验收与全部调用账本决定。
首次真实评测没有达到目标，生成测试冲突导致大量无效修复，见
[Sonnet 评测](measurements/2026-10-30-sonnet-v1.md) 和 ISSUES.md 第 13 项，不建议作为默认路径。
当前完整规划器与新策略都只允许新增文件：可以在种子项目旁添加功能并运行原有回归测试，
不能把本轮结果解释为修复已有业务文件的能力。单模型基线的文件写入能力更宽，但本轮比较
只使用相同的新增功能任务。

## 本版未实现（按设计文档后续加入）

Jev 参与规划路由；每个模块独立的集成分支与 merge train（现在是一条 merge train 加测试影响分析）；
多机租约队列与容器写时复制；模板金丝雀发布；风险分触发的 LLM 抽样审计；SCIP/语言服务器级引用。

## 限制

- 目标项目限于 Python 标准库加 unittest（沙箱以 `python -I -S` 运行，没有第三方包）。
  仓库原有测试只在最终闸门里跑，不在每个任务的合并闸门里。
- 验证在 macOS `sandbox-exec` 或 Linux `bwrap` 下运行；两者都没有时，任务以
  “No sandbox backend” 进入 PARKED。固定 pytest 环境（现有仓库任务）两端都支持：
  Linux 上环境目录只读挂载，路径保持主机原名；沙箱内的解释器先装 seccomp 过滤器，
  拒绝 fork/exec 与非 AF_UNIX socket（线程与 socketpair 可用），x86_64/aarch64 以外的
  架构直接失败。纯 Python 固定环境版本相同时，Linux 与 macOS 的环境指纹可以一致
  （Boltons 预检实测相同），但沙箱（报告中的 `mode=`）与主机不同；与 Mac 测量对照时
  必须注明平台，不能当作同主机重复。
- 真实调用需要已登录的 Claude 和已授权预算的 DSH（macOS，或装了 `dsh` 的 Linux）；
  自动化测试和压测只用假模型。
- 测试影响分析看的是 Python 导入关系；运行时读文件、`importlib` 这类依赖由最终闸门兜底。

## 本轮新增或扩展的共享工具

`LiveWorkspace.run_unittests`（沙箱 unittest 运行器，可按测试 ID 选择用例）、
`LiveWorkspace.safe_path`、`LiveWorkspace.is_test_file`、`codeindex.signatures`、
`Workspace.git(env=, timeout=)`、`dsh_guard.Guard(models=)`、`providers.settled_cents`、
`orch_run.orch_home`、`jev.JevClient`、`access.BUDGETED`。
本轮再增：`orch_index`（Index、RepoIndex、dependency_code、assertions、cochange）、`orch_mutate`、
`orch_modules`、`orch_pace.TokenBucket`、`orch_audit.audit`、`orch_synthetic`、`Workspace.git_bytes`、
`codeindex.definitions`、`orch_build.failed_tests/frame_path`、`orch_plan.closure/plan_score/lint_lessons`、
`OrchState.discount/plan_outcome/planning_history/spent/runs`、`Orchestrator.verify/mutation/harden/evolve`。
