# DSF 执行阻塞：证据、根因与本轮引擎修复

依据 controller 提供的 `d3da63d` 失败运行 `r7cb63157` 诊断包（报告、事件、控制台、生成的冻结测试、
规划与审核 journal；不含隐藏验收）和已入库的历史回执。本轮只做离线诊断和引擎修复，**没有调用
任何模型或 DSF**，不能据此宣称 Boltons 已跑通、DSF 更稳或更快。

## 先回答：窗口、上限、思考模式（事实、源码预期、缺失分开）

事实（回执与配置）：

- 配置记录的上下文容量是 1,048,576 token（输入加输出）。本轮 DSF 最大归一化输入 43,375，
  请求最大 174,374 字节。**未见输入触顶证据**；归一化输入不包含服务端模板与输出，不能据此
  排除所有容量或输出因素。
- 本轮 DSF 最大输出 6,154，失败那次 2,875，回执里 `reasoning_tokens` 均为空。
- 引擎对 DSF 路线传 `effort=""`。当 `DshClient._patch` 生效于 API key 插件时，它写入
  `maxTokens: 8192`、`thinking: "disabled"`、`reasoningEffort: "off"`。这个设置自 `eff7061`
  （2026-10-21）起未变，仓库里没有记录原因或测量。这是请求设置事实，不反映模型能力，也不代表
  谁的主观判断。

源码预期（Mac 上已安装的官方公开 JS，controller 只读提取，未发请求）：

- `dsh-llm-deepseek.js` 构造请求体：`max_tokens = options.maxTokens ?? model.maxTokens ??
  connection.maxTokens`，`thinking.type` 由 effort 决定，effort 为 `off` 时为 `disabled` 且不带
  `output_config`。`dsh-headless.js` 创建 agent 时只传 provider/model，所以 API key 路径预期
  发送 `max_tokens: 8192`、`thinking: {type: "disabled"}`。
- account 路径（`ORCHESTRA_DSH_ACCOUNT=1` 且 `~/.dsh` 存在）由 `dsh-llm-deepseek-account` 插件
  承担，`_patch` 不配置它。按源码，其默认 `maxTokens` 为 256,000（与模型列出的输出上限取小），
  thinking 未设置时 effort 默认 `high`，也就是开启思考。
- `max_tokens` 停止被映射为 turn_end `{kind: "max-tokens"}`，headless 仍输出最后一条 assistant
  消息拼接后的文本作为 final；思考内容只流向 stderr，不进入 final。
- 旧解码器在 final 解析失败时先返回 `Invalid DSH JSON`，丢掉了 turn_end 停止原因；本轮已修。

缺失（d3da63d 无法确认）：

- 本次运行实际用的是 API key、account 还是直接 API 路径，报告与 manifest 都没有记录，因此
  **实际发送的 `max_tokens` 与 thinking 设置未知**，上面的配置只是源码预期，不是 wire 证据。
- 每次调用的 `stop_reason`/turn_end 原因、原始最终答复都没有保存。

## 阻塞类别证据表

| 来源 | 阶段 | 实际现象 | 输入/输出 token | 原始答复 | 根因 |
|---|---|---|---:|---|---|
| distributed-v1 T-004 | 测试设计 | 失败 | 8,673 / **8,192** | 未保存 | 强疑似截断：输出恰等于预期上限，但无 stop 原因，缺失 |
| cache-rollout T-001 | 测试设计 | settled invalid JSON，分片重试后成功 | 1,434 / 1,804 | 未保存 | 待证 |
| dsf-1m-fresh T-004 a1 | 实现 | failed，运行最终 8/8 | 3,571 / 3,141 | 未保存 | 待证 |
| kernel-reviewed 草案 1 | 实验路径边界草案 | failed | 5,991 / 2,847 | 未保存 | 待证 |
| d3da63d T-004 a4 | 实现（thicken 后） | `Expecting ',' delimiter`，char 9237 | 38,895 / 2,875 | **未保存** | 待证：转义错误、提前闭合与 max-tokens 截断在旧消息里都无法区分；实际 max_tokens 未知 |
| d3da63d T-001 a6 | 实现 | 编辑后编译失败：line 188 unterminated string literal | 3,083 / 927 | 未保存 | 待证 |
| d3da63d T-004 a1 | 实现 | edit 失败（签名 `edit:a0d411739c3f`） | 13,555 / 3,139 | 未保存，细节被后续错误覆盖 | 待证 |
| d3da63d T-004 a2 | 实现 | 接口签名被改动/丢失 | 23,620 / 783 | — | 生成代码问题 |
| d3da63d T-001、T-002 | 测试设计 | 冻结断言错误（队列移除数/`_cull` 状态；手数字节偏移），Opus 复核后修正 | — | 有 review journal | 测试设计推理错误 |
| d3da63d T-004 a5 | 重新拆分 | `T-005: needs reqs, files and tests`，直接 PARK | Opus 5,790 输出 | 未保存 | 提示没说明每个替换任务须有测试，且没有修订机会 |

本轮 19 次 DSF 调用里只有 1 次是 JSON 解析失败。其余阻塞是测试设计错误、编辑应用失败、
生成代码不合法和接口冲突，不能统称“解析失败”。

## 已由源码确认的引擎缺陷

1. **一次可修复的格式错误直接烧掉 Opus 投入。** 执行期 DSF 返回已结算但无法解析的 JSON 时，
   `_generate` 把它当作 `provider` 失败，消耗一个阶梯档位。T-004 已用过 0/1/3/4 档，于是
   直接进入 5 档 replan，刚花掉的 12,827 token Opus thicken 契约只被 DSF 用了一次。规划阶段对
   同类失败早有局部重试，执行阶段没有。
2. **编辑应用失败同样直接消耗档位，且证据不足。** `apply_edits` 在写入前先编译，失败时只报
   行号和错误类型，模型看不到自己造成的那一行。
3. **replan 规则与 lint 不一致，且没有修订。** lint 要求每个任务都有 reqs、files、tests，
   replan 提示没说；一次被拒立即 PARK，错误里也不说缺的是哪一项。
4. **诊断缺失。** DSH/直接 API 解码失败时只保留前 80 个字符，不知道 JSON 对象长度和出错位置
   的关系，也不保存原文，无法离线复现。

编辑路径本身已核查：DSH final 文本经 `cls_text`（只剥代码围栏、截取首个 `{` 到最后一个 `}`）
后 `json.loads`，再进入 `apply_edits`。精确匹配走 `str.replace`；只有宽松匹配会给替换行补缩进。
没有二次反转义或正则改写代码，因此 T-001 的未闭合字符串来自解码后的替换文本本身；是模型转义
错误还是别的原因，需要原文才能判定。

## 本轮修改

- **DSF 在同一次尝试内自修一次。** 已结算的不可解析答复或无法应用的编辑，会把解析位置、
  原文附近片段或编辑错误交还 DSF，在同一档位内再请求一次完整答复（`MAX_FORMAT_REPAIRS = 1`，
  事件 `attempt.format_repair`）。这次请求同样经过 `_call_model` 的预算、取消与准入；未结算或
  未知结果不进入此路径，也不重放。第二次仍失败，才按原阶梯处理。Opus patch 不走这条路径。
- **诊断与受控旁路文件。** 解码失败消息补充 `final_chars`、`object_chars`、`error_char`、
  `trailing_chars`，可区分中途转义错误与提前闭合。非 completed 的 turn_end 原因写为 `stop=`，
  final 解析失败时也保留。
  `NativeResult.text` 只在已结算格式失败时保存原始最终答复（不含思维链）。执行期每个不可用的
  DSF 答复写入 `run_dir/responses/<call_id>.json`。规划 journal 原样保存这一字段，用于复用。
  编译失败附上出错的结果行。
- **JSON 转义说明。** DSF 答复契约增加一句：字符串里的源码先保留自身转义，再做 JSON 转义；
  明确 UTF-8 合法。没有加 ASCII 限制。Opus 本次自学的 “plain ASCII summary” 候选规则
  不采纳，出错位置在 9,237 字符处，远在 summary 之后。
- **replan 有限修订。** 提示写明每个替换任务至少有一个 req、一个文件和一个能证明其 req 的
  原冻结测试。被拒时把控制器错误和原提案交回作者修订一次（`MAX_REPLAN_REVISIONS = 1`，事件
  `task.replan_rejected`）。lint、frontier、容量、测试集合一致、ID 唯一等校验都没有放松；
  返回空任务仍立即 PARK。lint 消息写明缺失项，例如 `(missing tests)`。

- **wire 形状诊断。** 现有 guard 只从实际转发的那一个请求体读取 `max_tokens`（正整数）、
  `thinking.type` 和 `output_config.effort`（枚举，缺省记为 `absent`，非法记为 `invalid`），
  并从响应流取最后一个 `stop_reason`（白名单，其他记为 `other`）。DSH 事件流的 turn_end 种类
  （completed 也记录）和使用的传输（`cli`、`cli-account`、`api`）一并记录。直接 API 记录本客户端
  实际发送的请求体形状和响应 `stop_reason`。这些键在 `orch_usage.WIRE_LABELS` 中声明，经
  `usage_fields` 进入回执；不记录请求正文、headers、密钥或思考文本。下一轮正常调用即可证明
  实际发送的设置和终止原因，不需要额外探针。
- **预算。** 按用户最新意见，probe 接受 `--max-cents 0`，表示不设固定任务费用上限。账户余额准入、
  每次调用预留、调用次数上限、未知结果不重放与实际花费审计照常生效。

## 未证实与仍缺

- 五次失败的 DSF 回执没有一次保存了原文，所以全部是合成回归，**不是精确重放**。
- d3da63d 的实际传输与 wire 设置未知（见上）。
- 自修是否修复同一错误：每次尝试最多一次自修，每档只用一次，持续失败会按阶梯走完并停放，
  已有离线回归；不会无进展地重复。
- 冻结测试推理错误和 12,827 token 的 thicken 仍是真实开销。DSF 本可以发 `TEST_DISPUTE`，
  这次没有。本轮没有改动它们。

## 建议的验证顺序

1. controller 对新 SHA 跑 Mac 全量门，再跑新的 fresh pair（`--max-cents 0`）。读取每个 DSF 回执的
   `dsf_transport`、`wire_max_tokens`、`wire_thinking`、`wire_effort`、`stop_reason`、`turn_end`，
   以及 `attempt.format_repair`、`responses/*.json`、`stages` 与失败消息里的位置字段。不预设收益。
2. 若格式失败仍出现，用旁路原文和 stop 原因判定是转义错误、提前闭合还是输出上限。
3. 思考模式：本轮保持原设定，以便隔离协议修复的效果。只有当 wire 证据确认 API key 路径关闭
   思考，且失败集中在测试设计推理或格式上时，才在同一冻结引擎上把 DSF effort 改为 `high`，做一次
   有界对照，比较测试设计错误、格式失败、时延与结算。

## d016ea1 实测（run `rdfbaa15b`）与本轮修改

事实（来自回执与规划 journal 原文）：

- 运行在测试设计阶段以 `test-designer T-002 failed lint` 结束，用时 80.4 秒，共 7 次调用
  （1 次 Opus，6 次 DSF），DSF 本地结算 24 分，没有未结算调用。尚未执行，也未验收。
- 6 次 DSF 全部是 `dsf_transport=cli`、`wire_max_tokens=8192`、`wire_thinking=disabled`、
  `wire_effort=absent`、`stop_reason=end_turn`、`turn_end=completed`。上一节的源码预期由此得到
  wire 证实：DSF 一直在关闭思考的模式下运行。
- T-002 第一次：输出 2,149 token，正常结束。JSON 共 6,728 字符，在第 6,725 字符处报
  `Expected ':'`：`content` 字符串闭合后又多出一个 `,"\n"` 成员。不是截断。
- T-002 第二次：JSON 合法，输出 1,755 token。测试源码第 110 行 `data.index(b'{"b") - 1`
  的 bytes 字面量少了闭合引号。JSON 解码后即是如此，引擎没有二次反转义。
- 其余 T-001、T-003、T-004 的单元和集成测试设计第一次就通过。

根因（源码确认）：`_draft_test_shards` 让已结算格式失败和可解析后的源码/语义 lint 失败共用
`attempts=2`，一次格式失败加一次语法错误就终止整个规划。语法错误的反馈只有一行错误文字，
没有出错的源码行。执行期上一轮加的独立格式自修没有覆盖测试设计。

修改：

- 测试设计分片的格式修复（`MAX_FORMAT_REPAIRS`）与 lint 修订（`attempts`）分开计数。单分片
  物理调用不超过两者之和，且每次调用的 journal phase 编号唯一。恢复时重放已结算答复，不重新
  购买；计数从重放结果重建，不会被重置；未结算调用仍然停止。
- 语法错误把上一份草案出错行附近带行号的源码交回（标明是数据，不是指令）。格式失败仍附解析
  位置和片段。同一 lint 错误第二次出现，停在 `repeated_lint_error`；修订用尽停在 `lint_budget`。
  所有原有校验（单文件、需求覆盖、命名、fixture、容量、组装、owner、DAG）都不变。
- 容量拒绝的进度信息不再一律写成 “context too large”。

## DSF 模式：测试设计与实现改用 high 思考

- 决定：正式 DSF 路线改为 `Route("dsh", "deepseek-flash", "high")`。模型固定为 deepseek-flash，
  1M 窗口与 Opus 主规划都不变。DSH CLI 的 API key 插件与直接 API 共用
  `DshClient.output_settings`：high 对应 `max_tokens=32768`（e61037e 时为 16,384，见下节）、
  `thinking: enabled`、`output_config.effort=high`。直接 API 现在显式发送这些字段，空 effort
  时显式发送 `thinking: disabled`，不再静默依赖服务端默认。cli-account 插件另有配置，
  本仓库不修改它，也未实测。
- 理由：本轮两个阻塞都是同一类自洽性错误，即 Python 字面量、JSON 字符串和测试源码之间的
  引号与成员嵌套。此前的阻塞还包括手数字节偏移、队列状态推理等测试设计推理错误。这些都是
  生成前检查就能发现的问题，而 wire 证实 d016ea1 这轮的 6 次 DSF 调用都关闭了思考（更早的
  运行没有 wire 字段，不能据此推断其模式）。思考是该模型的默认
  模式；这是有依据的假设，不是已证明的收益。
- 风险与上界：输出上限同时承载推理和可见答复，超限时记录 `stop_reason=max_tokens` 并走一次
  格式自修。按 deepseek-flash 输出价 1000 分/百万 token，32,768 输出 token 约 33 分；输入费用
  随实际上下文另计。每次调用 300 分的预留、调用次数与账户余额准入都不变。本地账本是保守结算，
  不是发票。若服务端同时报告 reasoning 与 output token，本地账本会把推理部分按输出
  价再计一次，这是保守的多计，不会少计。
- 旧规划会话的设计者 effort 为空，恢复时会以 “Pinned planner route changed” 明确拒绝，
  不会自动替换模型或模式。
- 这一版同时改了恢复闭环和 DSF 模式，一轮结果不能单独归因于思考。下一轮 fresh pair 读取
  每次调用的 `wire_thinking`、`stop_reason`、reasoning/output token 与各阶段耗时。

## e61037e 实测：16,384 不够，high 改用 32,768

run `r2b588c22`（e61037e）的回执：4 个首轮 DSF 单元测试设计都是 `dsf_transport=cli`、
`wire_thinking=enabled`、`wire_effort=high`、`wire_max_tokens=16384`。T-003 输出 11,756 token，
正常 `end_turn`。T-001、T-002、T-004 都恰好输出 16,384 token，`stop_reason=max_tokens`、
`turn_end=max-tokens`。这是实际出站输出上限造成的截断，与窗口或转义无关。T-002 的有界重试
正常完成，输出 10,252 token。该运行仍在进行，没有宣称 8/8。

修改：`DshClient.output_settings` 中 high 档改用已有 max 档的 32,768 额度。思考档位、模型、
1M 窗口、Opus 主规划、传输、账户与 300 分预留均不变。原始回执由 controller 在仓库外保存
（`e61037e-token-stop-evidence.json`）。

## 304183c 实测：32,768 仍截断大分片；改为确认截断后一次放宽

事实（run `rcf517542`，仍在收尾；只引用已结算回执）：

- T-001、T-002、T-003 首次单元测试设计成功，输出 18,790、19,703、15,730 token，均为 `end_turn`。
- （运行中快照）T-004 的单元分片与集成分片都恰好输出 32,768 token 并 `stop_reason=max_tokens`。
  T-004 单元分片第二次用相同参数重试，再次 32,768 / `max_tokens`。当时另有两次调用尚未结算；
  最终数字见下文“304183c 最终报告”。
- 回执没有 `reasoning_tokens`，因此推理与可见答复各占多少无法从回执区分。这是证据缺口，
  不能据此断言超出部分全是推理。

源码核对：

- 公开 SDK 的请求体 `max_tokens` 就是本次请求的输出总上限，思考与最终文本共用；headless 的
  final 只取最后一条 assistant 消息的文本块。
- 我们的 high 只有 32,768，是本仓库 `output_settings` 自定的档位，不是模型或 SDK 限制。
  官方给出的最大输出为 393,216；公开 SDK 在 account 插件下的默认值为 256,000（本仓库不用
  也不改 account 插件）。
- 准入单位：每次 DSF 调用预留 300 分，本地结算以此封顶。按 deepseek-flash 价格，满 1M 输入约
  250 分，每 1,000 输出 token 1 分。base 32,768 输出加满窗口输入最多约 283 分，在预留之内；
  把所有调用一律放大到 131,072 则不再被预留覆盖。

修改：

- 不再对已确认截断的答复用同一预算重试。provider 报告 `max_tokens` 后，该分片或该次尝试改用
  `DSF_EXTENDED`：同一模型、同样的 high 思考，`max_tokens=131072`（effort 标签 `high-extended`，
  wire 仍记录 `wire_effort=high`、`wire_max_tokens=131072`）。只放宽一次，且只有在 300 分预留仍能
  覆盖最坏情况时才放宽（请求每个字节按一个输入 token 计，加满额输出）；否则停在具名的
  `output_limit`。放宽后再次截断：测试设计停在 `output_limit`；执行期以 `output_limit` 类别失败，
  任务停放，原因写明 `output_limit: provider stopped the answer at the 131072-token output limit
  after the one widening`。放宽不被预留允许时同样停放。停放的任务在再次 execute 或恢复时不会
  重新派发，原阶梯也不会把这个未变的任务降回 32,768 或同额度重试；要继续须由人改变任务或上下文
  （例如 retry/replan）。普通的 JSON 格式错误、编辑、源码与测试失败仍走原有自修和阶梯。
- 未结算或未知的截断不放宽，也不重放。phase 编号确定，恢复时重放已结算的截断答复，并据此
  继续使用放宽后的路线。执行期事件 `attempt.output_escalation` 记录放宽。
- 131,072 的依据：低于官方 393,216；按本路线实测 160–220 token/秒，最长约 10–14 分钟，
  在 Mac 的 1,800 秒 provider 时限内；输出部分最多约 131 分。base 档仍是 32,768，大部分分片
  保持快速。

未验证：131,072 是否足够 T-004 这类大分片；推理与答复的实际分配（需要 provider 报告
reasoning token）；放宽后的耗时。分片本身是否过大（R4 一个需求覆盖整个队列）只在报告中
观察，本轮没有为 Boltons 拆分需求或削减测试。

### 304183c 最终报告（controller 提供）

run `rcf517542` 结束于 `PlanError: test-designer T-004 failed lint (lint_budget)`，682.575641 秒，
10 次调用（9 次 DSF），本地结算 250 分，未结算 0。按最终报告 `usage.calls`：9 次 DSF 中 5 次
`failed` 且 `stop_reason=max_tokens`，均为 32,768 输出（T-004 单元分片 a1、a2；T-004 集成分片
a1、a2、a3）；4 次 `ok` 且 `end_turn`（T-001 18,790、T-002 19,703、T-003 15,730、T-004 单元分片
a3 26,070）。运行中的规划 journal 快照显示单元 a1、集成 a1、集成 a2 为 `final_chars=0`，单元 a2 为
`final_chars=7166`（未闭合的 JSON 与源码字符串）。集成 a3 的 `final_chars=0` 来自最终报告的
`errors` 字段：它是抛出 `PlanError` 的集成分片最后一次反馈；单元分片在 a3 已成功。
`final_chars=0` 只说明没有可见答复，不能换算推理 token；推理 token 数仍未知。
没有运行新的单体对照。上面的放宽与停放逻辑是本次的候选修复，尚未经过实测。

## 5144584 实测：guard 缓冲整段响应，与 300 秒失败高度吻合

事实（run `ra3f5503d`，controller 提供的最终报告）：8 次调用，1 次 Opus、7 次 DSF，总耗时
630.782594 秒。已知 DSF 结算 175 分；另有 1 次未结算调用，其 300 分预留的实际费用未知，
不计入已知结算。三个普通分片在 32,768 内成功。
T-004 单元分片放宽到 131,072 后成功（39,199 输出，`end_turn`，194.5 秒），说明这个调用确实需要
超过 32,768。T-004 集成分片的放宽调用 `orch-ra3f5503d-integration-test-design-T-004-2-1d6542d5d387`
在 300.4499878883362 秒失败：`turn_end=error`、`forwarded=1`、`wire_max_tokens=131072`，没有
token 数与结算，列在 `unresolved_call_ids`。回执没有保留 SDK 错误码或原始错误；当时的
`_decode` 把空 final 报成 `No JSON object`，这个消息有误导性（本轮已修，见下）。不能假定它
未计费，也不能重放。

缓冲缺陷（源码确认，并已离线独立复现）：`dsh_guard.Handler.do_POST` 用 `response.read()` 读完
上游全部响应后才发送状态行、headers 和正文。本地 SSE 测试证实，旧行为下客户端在上游结束前
收不到任何字节。公开 SDK `dsh-llm-deepseek.js` 默认 `streamIdleTimeoutMs=300000`，用
`idleWatchdog` 在收到数据时复位，超时抛 `DeepSeek Messages stream idle timeout`（`TIMEOUT`）。
因此超过 300 秒的上游生成，在 SDK 看来会是 300 秒没有任何字节。

这次失败的根因是推断：300.45 秒、旧 guard 整段缓冲、SDK 300 秒空闲时限与离线首字节复现高度一致，
但这次调用的真实错误码未保存，不能确认它就是 `TIMEOUT`，也不能排除 provider 侧错误。加大
`native.timeout` 或 SDK 空闲时限只会掩盖缓冲。旧 guard 在客户端断开后仍会继续读上游，直到读完或
180 秒套接字超时，所以该上游请求可能在 DSH 进程结束后继续生成；没有留下逐调用的完成证据，
无法确认它是否真的继续或完成。

修改：

- guard 改为真流式：上游状态行和白名单 headers 一到就转发，正文按最多 64 KiB 的块读取，
  每块立即写出并 flush。未知长度时以连接关闭结束正文。单个物理 POST 上限、模型白名单和拒绝
  行为不变。
- 元数据增量解析：`served_model` 与 `stop_reason` 逐块扫描，只保留 256 字节的跨块衔接，流结束
  即清空，不保存正文、headers、密钥或推理文本，没有无界缓冲。
- headers 已发出后出错（客户端断开、上游中断、声明长度未达到、传输关闭）不再追加第二个 HTTP
  响应，只记 `guard.interrupted`。上游套接字由 guard 自己持有：结束或离开 `bound_transport`
  时 `shutdown` 并关闭，不会留下随下一次试验继续运行的上游请求。headers 发出前失败仍返回 502。
- `_decode`：turn 以 error、aborted 或 blocked 结束时，空的或部分 final 文本不再被当成格式失败，
  而是走原有原生分类。消息保留 `stop=error`，并附上 SDK 的短错误码（如 `error_code=TIMEOUT`，
  非大写短码记为 `OTHER`）；回执新增经过校验的 `turn_error` 码，不记录错误全文。
  `max-tokens` 仍走原有截断判断。无结算的 error turn 仍是 unknown，不被当作已结算格式失败，
  也不重放。

只读核对与仍未知：

- 本地能核对这次调用的只有回执，以及 Mac 上的 `guard.log`。后者每行只有 `path status bytes`，
  没有调用 id、时间戳或 token 数。即使旧 guard 在客户端放弃后补写了一行，也无法关联到这次调用，
  更不能据此结算。该调用的计费只能保持未知，300 分预留不动，本地账本不改。
- 流式修复只做了本地 HTTP/SSE 离线复现与回归，没有真实调用。真实 DSH 输出能否全程保持数据
  不中断、长生成的实际耗时、推理 token 数，仍需下一轮实测。

## 8440f9f 实测之后：首次调用即用预留覆盖的最宽输出额度；只列可运行的测试 id

8440f9f 的配对对照（controller 提供，同引擎、上游、环境、需求与隐藏验收）：Orch 4 个任务完成，
隐藏验收 8/8，项目回归 685 passed、3 skipped，1007.966867 秒；DSF 13 次、本地结算 282 分，
Opus 3 次。单体 Opus 5.5/high 隐藏验收 8/8，项目回归 669 passed、3 skipped，398.539502 秒。
这一轮 Orch 慢 2.53 倍，不能宣称提速或省钱；一个课题一轮也不能证明泛化收益。旧的 514 未知调用
及其 300 分预留仍保留，未清账。

### 1. 输出额度：先烧 32,768 再放宽是浪费

事实：T-004 集成测试设计的 base 调用用了 172.01 秒、输出 32,768 后 `max_tokens`，随后放宽调用
以 52,282 token、240.54 秒 `end_turn` 成功。T-004 实现的 base 调用用了 159.13 秒后同样被截断，
放宽后 38,367 token 成功。这两次请求为 82,352 和 108,591 字节；按 `_widened` 的保守算法
（每个请求字节按一个输入 token 计，加满 131,072 输出），最坏约 152 分和 158 分，都在 300 分预留内。

修改：每次 DSF 测试设计与实现调用，都按这次请求的确切字节数，用 `_widened` 选择预留能覆盖的
最宽额度；能覆盖就第一次用 `high-extended`（131,072），覆盖不了就用 base 32,768。额度是上限，
不是要求生成的长度。provider 报告截断时已是该请求能被覆盖的最宽额度，因此测试设计直接停在
`output_limit`，执行期以 `output_limit` 停放，不再先截断后放宽；原 `attempt.output_escalation`
事件随之不再产生。价格、预留、账户、模型、思考档位与 CLI/直接 API 共用的 `output_settings` 都
不变；Opus 调用和其他路线不受影响。每次物理调用仍有唯一 phase，选择只取决于请求字节和常量，
恢复时可确定重放。用 base 额度起草的旧规划会话恢复时，请求不同，会由 journal 明确拒绝，
不会悄悄混用。

未验证：更宽的上限是否改变模型实际推理长度，以及对总耗时的净影响（关键路径未单独测量）。

### 2. 测试 id：mixin 方法不再被列成不可运行的 id

事实：T-001 的原始分片在非 TestCase 的 `_QueueFeatureTests` 中定义 9 个测试，两个具体 TestCase
子类继承它。旧 `unittest_ids` 把方法列在 mixin 上，例如 `test_orch_t_001._QueueFeatureTests.test_r1_...`，
冻结时被当成合法，执行时这些 id 无法运行。两次失败后，才经 Opus patch 和复核纠正。

修改：`unittest_ids(path, source)` 签名不变，只列可运行的类：基类是导入的 unittest
`TestCase`/`IsolatedAsyncioTestCase`，或本文件中更早定义的可运行类。mixin 中用 `def` 定义的测试
列在每个继承它的 TestCase 上，不列在 mixin 上；同名覆盖只保留一个。对原始分片离线复现：
18 个 id（9 个测试乘 2 个具体类），没有 mixin id，没有形状错误。新增 `unittest_shape_errors(source)`
报告无法静态列出的形状：没有任何 TestCase 继承的 mixin 测试、基类来自本文件之外、用赋值而不是
`def` 产生的 test 名（别名或禁用）。lint（与 fixture 检查同一范围）和 `test_shard`（先于覆盖检查，
让设计者看到真正原因）都在冻结前拒绝这些形状，由 DSF 修订。对既有仓库测试（基线），基类来自
别处的类仍按原样列出，不会抛错。不执行候选源码，唯一归属、需求覆盖、DAG 与权限检查不变。

### Opus 缓存（只做观察，本轮不改）

Orch 的 Opus 缓存命中 30.1362%，单体 93.4291%；Orch 只有 3 次 Opus 调用（规划 high 50.86 秒、
patch medium 16.875 秒、review medium 20.202 秒），角色和输入都不同，大多是冷调用，也不是主要
耗时。环境提示是稳定的指纹、导入、版本和源码根，不含绝对路径。之前的 task-prefix 原生探针显示
跨任务会失去大段前缀，因此没有启用。下一步可测的机会只有：同一运行内同角色多次 Opus 调用的
前缀复用，以及跨运行的规划前缀温度；都应先用已有 `stages` 与 `claude_cross_role_followups`
统计，再决定是否改协议。

## 0885237 实测之后：测试设计的关键路径

0885237 的配对对照（controller 提供，同源同环境独立验收）：Orch 与单体都通过 8/8。Orch 585.448049 秒，
其中规划加基线 431.631201 秒、执行 153.740974 秒；单体 320.355986 秒，Orch 仍慢 1.83 倍，
还不能说明更值得使用。Opus 规划一次 61.672664 秒，缓存读取比例约 98.5%，但热缓存没有受控，
不能归因于代码。DSF 11 次、本地结算 247 分。已跑通版本和所有失败报告都保留。

测试设计时间线（从首个单元分片开始计时，取自原始回执）：T-003 单元 0 秒起 80.61 秒，T-004 单元
201.52 秒，T-002 单元 129.38 秒，T-001 单元 60.30 秒；T-004 集成 60.72 秒才开始，耗时 289.36 秒；
T-003 语法修订 80.71 秒起，整份重写耗时 182.40 秒。

### A. 调度：独立集成分片与依赖更深的分片先开始

原因（源码）：`pool.map` 按“全部单元分片在前、集成分片在后”的顺序派发，jobs=4、5 个分片时，最重的
集成分片要等第一个单元分片结束才能开始。

修改：同一个线程池、同样的 jobs 和分片，派发顺序改为独立集成分片在前，其余按依赖闭包从大到小，
同级保持原顺序。这是启发式，不保证最优。结果按原分片顺序装配，计划内容与完成顺序无关。若各分片
时长与 0885237 相同，只能说 T-004 集成分片本身会在 0 秒开始、约 289 秒结束，而不是 60.72 秒开始、
约 350 秒结束。整个测试设计阶段仍要等所有单元分片和修订完成：T-003 排在一个约 60 秒的单元分片之后，
它当时 80 秒的设计加 182 秒的修订可能比集成分片更晚结束，所以不能把 289 秒当作整阶段结束时间。
阶段实际耗时是否改善，需要新的实测。调用 phase 由用途、任务和序号决定，与顺序无关，恢复照常重放；基础设施错误仍停止排队分片。

### B. 语法错误的分片：用编辑修订自己的草稿，不整份重写

事实：只读导出的 T-003 原始草稿是完整、可解析的 JSON，只是 Python 不能编译：第 27 行和第 30 行的
bytes 字面量含非 ASCII 字符 `é`（`bytes can only contain ASCII literal characters`，编译器只报第一处）。
离线把这两行改成 `'...'.encode()` 后即可编译，得到 5 个可运行 id，没有形状或 fixture 问题，
81 行中只改 2 行。草稿只做 AST 与编译检查，没有执行。

修改：分片返回完整单文件、只因 `SyntaxError` 被拒时，下一次修订改为局部编辑：

- 请求沿用原分片提示（共享前缀不变），在动态尾部附上这份草稿与带行号的出错上下文，要求按
  `EDIT_SCHEMA` 返回最小的 search/replace，并提醒修好同类的其他位置。用途为 `test-repair`
  （集成分片为 `integration-test-repair`），phase 序号唯一，进入同一个规划 journal。
- 编辑由既有 `apply_edits` 应用到只含这份草稿的临时目录，草稿同时作为可见视图：空 search 不能替换
  整份文件，每个 search 必须唯一匹配，结果必须能编译。设计者看不到其他分片的测试或候选实现。
- 草稿路径先经既有 `LiveWorkspace.safe_path` 与 `is_test_file` 检查，通过后才写入临时目录；
  绝对路径、`../` 或非测试文件路径不会在目录外创建或修改文件。search 等于整份草稿（忽略首尾空白）
  的编辑也被拒绝，局部修订不能借一次 search/replace 变相整份重写或删除草稿。
- 编辑答复本身是已结算的坏 JSON 时，保留同一份草稿和原来的编译错误反馈，用唯一的那次格式自修
  再请求一次局部编辑（新 phase 序号），不会回到整份重写；坏草稿、坏 JSON 编辑、好编辑共 3 次调用，
  用途依次为 test-design、test-repair、test-repair。
- 修订提示明确：局部修订段只替换前文“返回整份文件”的输出要求，原需求、契约、分配的需求与覆盖
  规则仍然有效。
- 修订后的文件仍走同一套 `test_shard`（形状、覆盖、命名、fixture）、容量和装配检查，不放宽任何门槛。
- 它与整份修订共用 `attempts` 次数。局部编辑答复是已结算的坏 JSON 时，按上文保留同一草稿，用既有的
  那一次格式重试；编辑无法应用、答复不是要求的编辑形式（例如不是 DONE），或修订后仍有其他静态问题时，
  下一次回到既有的有界整份重写。同一错误重复仍停在 `repeated_lint_error`；未结算的修订调用在恢复时被
  拒绝，不重放。

未验证：DSF 在真实运行里能否一次给出覆盖全部同类错误的编辑；局部修订的实际耗时；重排后的真实
总耗时。都需要 controller 在冻结后用新的 fresh Orch 与同版本单体测量，不能用旧成绩代替。
