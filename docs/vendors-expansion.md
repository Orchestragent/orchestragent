# DSH、Cursor、Kimi 接入证据（2026-10-21）

本文件只记录官方公开文档、官方源码与安装脚本的只读研究。研究没有安装、登录、推理、读取凭据或改变账户收费方式。命令是实现参考，不能视为已经执行或获得消费授权。账号实际套餐、额度、额外消费开关均未知。

| 厂商 | 官方交互形态 | Orchestragent 接入结论 |
| --- | --- | --- |
| DSH | 桌面/Web、headless JSONL；官方账户登录与 API key 是不同认证路径 | 可研究原生 headless，但默认仍有工具；须由实现关闭所有工具并限制输出、重试和成本。它不是已证实的固定费率订阅服务。 |
| Cursor | 本机交互 CLI、print、ACP、cloud/worker | print 是官方自动化入口。Ask 仍能读文件；deny 规则不是已证实的全工具、hooks、MCP 启动关闭开关。满足边界后才可开放调用。 |
| Kimi Code | 新原生 CLI 交互、单次 `-p`、ACP、Web | **订阅仍禁止非交互自动化，后台路由保持禁用。** 技术能力与订阅许可必须分别判断。 |

## Kimi：技术更新不取消订阅限制

现行[社区规范](https://www.kimi.com/code/docs/en/kimi-code/community-guidelines.html)仍明确将订阅限定为个人交互使用，并禁止非交互自动化，举例包括脚本批处理与数据标注流水线。兼容 agent 框架并不解除这个限制。让用户在手机上点击批准，然后启动后台 `-p`，不能被描述为官方交互使用。面向产品/企业的集成应另行核实 Kimi Platform 方案，不得静默切换成 API 付费路径。

[总用户协议](https://www.kimi.com/user/agreement/modelUse?version=v2)与[付费协议](https://www.kimi.com/user/agreement/paidServiceAgreement?version=v2)当前生效日期均为 2026-08-31；具体产品规范仍适用。此研究没有证明任何允许 Orchestragent 订阅后台调度的例外。

旧 Python [`MoonshotAI/kimi-cli`](https://github.com/MoonshotAI/kimi-cli/blob/main/README.md) 已归档，并明确要求迁移到 [`MoonshotAI/kimi-code`](https://github.com/MoonshotAI/kimi-code/blob/main/README.md)。PyPI 的同名包不是新的原生 CLI；不能继续把旧版 Python 的参数和配置当作当前规范。

官方安装和登录参考（本研究均未执行）：

```sh
curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash
# 官方替代安装方式：brew install kimi-code
# 官方替代安装方式：npm install -g @moonshot-ai/kimi-code
kimi login
```

`kimi login` 使用官方设备授权流程，在终端给出 URL 和用户代码，由原生 CLI 保存授权；不要提取或复制 OAuth 文件。交互界面 `/login` 也提供 Kimi Code OAuth 与 Moonshot Open Platform API key，后者是不同收费路线。迁移旧配置不会迁移登录授权。[命令参考](https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/reference/kimi-command.md)、[Provider 配置](https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/configuration/providers.md)

新原生 CLI 技术上支持以下单次调用，但**此例不适用于受上述限制的 Kimi Code 订阅后台路由**：

```sh
kimi --agent-file /absolute/path/output-only.md \
  --model MODEL_ALIAS --prompt 'PROMPT' --output-format stream-json
```

官方自定义 agent 的空工具列表会从模型暴露和执行校验两处禁用工具：

```markdown
---
name: orchestragent-output
description: Return the requested structured answer
tools: []
subagents: []
---
Return only the requested answer. Follow the supplied output schema.
```

`disallowedTools: ["*"]` **不能替代** `tools: []`。不插入 `${base_prompt}` 或 `${plugin_sections}` 的正文自行拥有系统提示。`--prompt` 默认自动权限，并不等于禁工具；JSONL 仅是输出传输格式，没有在该命令参考中发现任意 JSON Schema 的强制输出参数。[Agent 规范](https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/customization/agents.md)

`KIMI_CODE_HOME` 可把配置、会话、日志和 OAuth 状态放入指定目录，之后必须在该目录使用官方登录，不能复制令牌。可考虑 `KIMI_CODE_NO_AUTO_UPDATE=1`、`KIMI_DISABLE_TELEMETRY=1`、`KIMI_DISABLE_CRON=1`、`KIMI_LOOP_MAX_ATTEMPTS_PER_STEP=1`、`KIMI_CODE_INFINITE_RETRY=0`、`KIMI_LOG_LEVEL=off`。但用户通用 `~/.agents/agents/` 搜索仍可能保留；配置 hooks 会在提示或回合生命周期执行，独立于工具列表。工具为空不证明所有启动副作用消失。[环境变量](https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/configuration/env-vars.md)、[Hooks](https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/customization/hooks.md)

收费前置条件：当前[会员说明](https://www.kimi.com/code/docs/kimi-code/membership.html)区分新旧套餐；新套餐不再有周额度，但仍有 5 小时窗口和月额度，旧套餐还可能有周额度。各终端和关联 API key 共享限额。额外用量加油包可在订阅额度不足时继续扣钱包，月上限可能未设置，因此不能把 OAuth 登录等同于无额外费用。不得替用户开启加油包、充值或转换 Open Platform。[FAQ](https://www.kimi.com/code/docs/kimi-code/faq.html)

## Cursor：官方安装、登录、模型与工具边界

官方安装命令为：

```sh
curl https://cursor.com/install -fsS | bash
```

[CLI 概览](https://cursor.com/docs/cli/overview)与[官方安装脚本](https://cursor.com/install)已核实。以 curl User-Agent 只读取得的脚本当前固定版本为 `2026.10.19-6bbf2c4`，安装至 `~/.local/share/cursor-agent/versions/<version>`，创建 `~/.local/bin/agent` 与 `~/.local/bin/cursor-agent` 链接。**脚本没有 PREFIX 或 INSTALL_DIR 参数。** 浏览器 User-Agent 请求该 URL 可能得到网页，不应把 HTML 当安装脚本执行。

Apple Silicon 官方包地址由该脚本直接给出：

```text
https://downloads.cursor.com/lab/2026.10.19-6bbf2c4/darwin/arm64/agent-cli-package.tar.gz
```

将该官方包手动解压到项目 `.tools/cursor-agent/2026.10.19-6bbf2c4/` 并通过绝对路径调用，是项目可采用的本地部署选择，**不是官方 installer 的 prefix 功能**。应先核对包内容和 `--version/--help`。不要为重定位安装修改 `HOME`，不要复用编辑器的 `cursor` launcher 充当 Agent CLI，也不要运行会写全局 shell 配置的 integration 命令。

官方认证及状态入口：

```sh
NO_OPEN_BROWSER=1 agent login
agent status --format json
agent models
# agent --list-models 亦为官方列举入口
```

登录由用户完成官方页面，CLI 自行保管授权。`status` JSON 可能包含账号信息，对应用只保留最少就绪状态；不要保存原始输出。模型 ID 应从实际账号的 `models` 结果取得，示例文档的旧模型名不是当前可用保证。[认证](https://cursor.com/docs/cli/reference/authentication)、[参数](https://cursor.com/docs/cli/reference/parameters)

技术调用候选形态（待本机安全审计，不是已验证可开放命令）：

```sh
agent --print --output-format json --model MODEL_ID \
  --mode ask --sandbox enabled --workspace /absolute/empty/workspace 'PROMPT'
```

`--print` 明确仍具有写文件与 shell 工具；Ask 是只读探索，不是无工具模式。`--output-format json` 是响应包格式，不是任意业务 Schema 的强制输出。不要使用 `--force`、`--yolo`、`--approve-mcps`，不要为了避开工作区信任检查直接添加 `--trust`。[Headless](https://cursor.com/docs/cli/headless)、[参数](https://cursor.com/docs/cli/reference/parameters)

全局配置默认 `~/.cursor/cli-config.json`，可通过 `CURSOR_CONFIG_DIR` 改变；项目 `<workspace>/.cursor/cli.json` **只支持 permissions**。`sandbox.readBoundary` 默认 `system`，应改为 `workspace`。下面是依据文档语法组合的候选拒绝配置；尚未证明覆盖该版本的每个内建工具或所有绝对路径变体：

```json
{
  "version": 1,
  "editor": {"vimMode": false},
  "approvalMode": "allowlist",
  "sandbox": {"readBoundary": "workspace"},
  "permissions": {
    "allow": [],
    "deny": [
      "Shell(*)", "Read(**)", "Read(/**)",
      "Write(**)", "Write(/**)", "WebFetch(*)", "Mcp(*:*)"
    ]
  }
}
```

文档保证 deny 优先于 allow，允许 shell、文件、域名、MCP glob 匹配，但没有声明以上组合是完整无工具安全模式。空 allow 列表单独也不是拒绝所有行为。[权限](https://cursor.com/docs/cli/reference/permissions)、[配置](https://cursor.com/docs/cli/reference/configuration)

`agent mcp disable IDENTIFIER` 文档承诺该服务器不加载、不再请求批准，但没有在参数页发现一键禁止全部 MCP 加载的开关。`CURSOR_CONFIG_DIR` 不移动所有认证存储、全局 MCP 和第三方 hooks；具体版本源码核验见下节。不要读取这些文件来寻找凭据。

[Hooks 文档](https://cursor.com/docs/hooks)存在用户/项目/plugins 来源及 sessionStart、beforeSubmitPrompt 等生命周期执行入口，独立于模型工具调用。当前 CLI 参数页未给出 `--disable-hooks`、`--tools none` 或完整隔离语义。必须在具体已安装版本核验；若无法证明阻止启动脚本与工具，当前适配器应返回具名阻塞，而不是将 Ask 宣称为安全的无工具调用。

收费不能仅由登录方式推断：当前[模型及价格说明](https://cursor.com/docs/models-and-pricing)将新套餐自有模型的额度，与 Claude/GPT/Gemini 等第三方模型区分；新套餐的第三方模型按 API 标价从首个请求扣预付额度，旧套餐可能保留不同的月额度池与按量规则。CLI 与其他 Cursor 终端共享账户用量。用户实际属于何套餐、是否开启自动补充或额外消费，研究未读取账户，均未知。不得静默改模型、开启充值或承诺所有模型都包含在订阅中。

### 已安装 Cursor 版本的进一步审计

controller 已将上述官方包放在 `.tools/cursor-agent/2026.10.19-6bbf2c4/dist-package/`。本次检查仅查看公开 bundle 和运行 `--help`；help 使用临时配置、内存 credential store、禁 project config/自动更新与共享 `NativeClients._run`，退出码为 0。没有真实 status、登录或模型请求。

关键模块证据（文件均来自该官方包）：

- `index.js` 的 `../cli-credentials/dist/index.js`：默认 macOS 路径使用原生 Keychain；`AGENT_CLI_CREDENTIAL_STORE=file` 则使用真实 HOME 的 `.cursor/auth.json`，不读取 `CURSOR_CONFIG_DIR`。**官方 CLI 使用自己的默认 Keychain 是允许的**，应用不得提取、复制或解析令牌。认证不迁移到项目目录本身不是禁用理由。
- `index.js` 的 `../keychain/dist/index.js`：官方库通过 `/usr/bin/security` 子进程读写其自身 Keychain 项。因此“禁止全部非 CLI 子进程”与当前原生登录机制不兼容；若设计沙箱必须有明确的原生认证例外。
- `6121.index.js` 的 hooks 配置路径包含真实 HOME `.cursor/hooks.json`、`.claude/settings.json`、项目文件、系统 enterprise 配置，并支持 team/plugin hooks。`CURSOR_CONFIG_DIR` 不能代替这些路径的隔离。
- `7629.index.js` 的 `./src/local-agent-profile.ts`：正常 remote profile 固定开启 `enableTeamHooks`、`enableWorkspaceOpenHook`、`enableManagedSkillsSync`、marketplace/user/first-party plugins 与 `enableMcp`。authless/local profile 部分关闭并不能作为官方订阅线路的替代；其中 MCP 仍然开启。
- `index.js` 注册了隐藏内部参数 `--allowed-tools`、`--exclude-tools`、`--exclude-workspace-context`、`--disable-project-configs`、`--disable-auto-update`。`544.index.js` 的 `exclude-tools.ts` 和 `exclude-tools-headers.ts` 表明空 allowed-tools 形成空列表，并写入服务请求 header `x-cursor-agent-allowed-tools`；此次没有找到足以证明所有本地工具执行都受其约束的保证。不能把内部请求 header 冒充公开、完整的无工具模式。
- CLI wrapper 会默认将 Node compile cache 写入用户缓存目录；只读 help 检查使用临时 `NODE_COMPILE_CACHE` 与 `NODE_DISABLE_COMPILE_CACHE=1`。bundle 也有 `DIRENV_DISABLE=1` 检查；环境清理应复用现有 NativeClients，而不是继承账户密钥、代理或任意 endpoint。

macOS Seatbelt 的纯本地 canary（`tests/test_cursor.py`）已验证指定文件读取与 `/bin/echo` 子进程能被阻止，使用本机 Node、测试文本和 `NativeClients._run`，没有网络或凭据。该测试只证明这两个内核限制生效，**不证明完整 Cursor 沙箱已建立**。尝试仅允许 CLI 包、系统运行库和设备节点的全面读取白名单时，Node 在启动阶段中止；该配置尚不能投入实际客户端。即使文件隔离完成，也仍需解决允许正常 Cursor 后端连接时团队 prompt hooks/受管 skills 的关闭问题。

因此 `CursorClient.status()` 只报告安装存在，分别给出 auth unknown、billing unconfirmed、isolation unsupported；当前 `call()` 在任何 billing consent 下都不会启动进程。具名阻塞是 `cursor_native_isolation_unsupported`，含义为工具与扩展隔离未得到验证。代码没有加入一条未经证明的推理路径或对应的虚假 JSON 成功测试。

## DSH：headless 与认证并非同一配置

本机已有 `/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh`，controller 只读报告版本 `0.2.1-rc.2`。不需要为研究再安装。官方公开仓库仍标记 developer preview；[`README`](https://github.com/deepseek-ai/deepseek-harness/blob/master/README.md)给出的 `npx @deepseek-ai/dsh web` 是另一种运行途径，不能因此引入重复安装。

官方 [headless README](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/headless/README.md)说明：

```sh
DSH_HOME=/absolute/project/.data/dsh-cli \
  '/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh' \
  --profile headless --json 'PROMPT'
```

无位置参数或参数 `-` 时可读取 stdin；`--session-id` 恢复会话。`--json` 是 JSONL 事件流，不能当业务结果 Schema 保证；必须同时验证最终事件、turn_end 状态与进程退出结果，不能仅因看到 final 文本就视为成功。headless 继承基础模型/工具配置，本身不禁工具。本研究未验证完整无工具 patch 的插件清单，该项由 controller 的已安装 bundle 检查确定。

headless 默认关闭 `credentials`、`authorization`、`deepseek-account`，所以桌面已登录不保证 headless 已登录。默认 API key 来源为环境、调用目录 `.env`、DSH_HOME `.env`，不能依赖意外继承的密钥。原生保存授权路径可通过显式 profile patch 重新启用上述三个插件；不要复制或解析密钥文件。恢复 managed credentials 后，原生保存的凭据与环境存在优先级，必须明确最终走哪种认证。

官方 [`deepseek-account` README](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/credentials/deepseek-account/README.md)确有浏览器账号登录、取消登录、退出功能；“DSH 只能使用 API key”并不准确。但该组件暴露充值钱包与赠送钱包余额，不证明固定价订阅、免费推理或零边际收费。若用户选 API key，应只在原生凭据界面自行输入，应用不读取 key。余额查询失败必须显示未知，不能当作余额零或可用预算。

## DeepSeek API 定价与预算前置条件

以下来自 2026-10-21 阅读的[官方人民币定价](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)；**不是汇率换算**。单位为每百万 tokens，高峰价可作保守估算：

| 模型 | 输入缓存命中 | 输入未命中 | 输出 |
| --- | ---: | ---: | ---: |
| `deepseek-flash` / V4.1-Flash-1017 | ¥0.04 | ¥2.00 | ¥8.00 |
| `deepseek-v4-pro` / V4-Pro-0813 | ¥0.30 | ¥9.00 | ¥27.00 |

空闲时段价格均为高峰的一半。高峰为北京时间周一至周五（不含中国法定节假日）09:00–12:00、14:00–18:00。上下文 1M、最大输出 384K；该上限不是合理默认输出预算。旧 `deepseek-v4-flash` / `deepseek-v4-flash-vision-exp` 名称仍接受，但已转向当前 Flash 版本及价格。

[美元页](https://api-docs.deepseek.com/quick_start/pricing)高峰价分别为 Flash $0.006/$0.30/$1.20，Pro $0.044/$1.32/$3.96。账本必须保存原币种，不能把人民币预算与美元 costUSD 字段直接混算。

Search 与 Fetch 各按每 1,000 次成功请求 ¥5（美元页 $0.75）独立收费；DSH 服务端工具已有此调用，目前豁免到北京时间 2026-11-01 00:00，之后收费。因此模型预算不能假设工具费用永远为零。模型调用从赠送/充值余额扣减，优先赠送余额。

可靠的本地预算至少需要：用户确认币种和消费上限、固定模型费率版本、限制全部输入和输出及回合数、关闭工具和自动重试、调用前保守预留、并发扣账互斥、失败/超时保留可能已消费的预留。仅在调用结束后读取 usage 是记录，不是硬上限。此研究未证明 DSH 存在服务端账户总消费硬限或 CLI 金额上限；实现不能宣称本地估算等于厂商最终账单。

## 实现复用与验证范围

此研究没有新增通用工具，也没有改序列化、子进程、认证或 schema 代码。后续实现复用 `serialization.dumps`、`NativeClients._run/status`、`live.object_schema`、`SettingsStore.save_proposal`。文档以 ego-browser TaskSpace 13 与官方 GitHub connector 只读核实；TaskSpace 按 controller 要求保留供后续官方登录交接。未执行任何厂商推理验证，未做账号套餐推断。

## 执行上限审查结果

当前 DSH API 调用保持 `policy_blocked`。本机官方安装包的
`dsh-tools/lib/index.js:3403` 将未知工具转成普通错误结果；
`dsh-agent-loop/lib/index.js:1166` 执行该结果后可在 953/1007 继续下一次请求。
没有工具并不保证只有一次推理。未找到官方 maxSteps/maxTurns 配置；
需在第二次请求发出前强制阻断并验证，才可启用按单请求预留的预算策略。
此检查只读官方安装包，没有实际 API 调用。
