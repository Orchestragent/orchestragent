# Orchestragent

原生 iPhone 调度客户端 + Mac 持久化任务服务。一句话提交需求，后台封装任务拆分、
厂商派发、独立审查与测试。默认折叠执行细节，失败、审批和费用状态始终可见。

## 在 Mac 运行

运行时只用 Python 3.12+ 标准库和 Git。开发依赖为 pytest、ruff、mypy；iOS 需要 Xcode。

```sh
uv sync --locked --group dev
PYTHONPATH=src uv run python -m orchestragent.server --live
```

省略 `--live` 是零消耗 Mock。服务监听 `127.0.0.1:8765`，数据保存在被 Git 忽略的
`.data/`。打开 `ios/Orchestragent.xcodeproj` 在 iPhone 模拟器运行即可连接。
服务独立于客户端；App 退出不会停止任务。Mac 需保持开机且服务进程存活。

### 开机自启（launchd）

已安装用户级守护，登录即启动、崩溃自动拉起，不修改系统安全设置：

```
~/Library/LaunchAgents/local.orchestragent.server.plist
```

```sh
launchctl print gui/$(id -u)/local.orchestragent.server | head   # 状态与进程
launchctl kickstart -k gui/$(id -u)/local.orchestragent.server    # 重启
launchctl bootout gui/$(id -u)/local.orchestragent.server         # 停止
rm ~/Library/LaunchAgents/local.orchestragent.server.plist        # 卸载
```

日志写在 `.data/logs/server.log` 与 `.data/logs/server.err.log`。守护进程以
`--live --remote-port 8766 --remote-bind 0.0.0.0` 启动，`PYTHONPATH` 指向仓库 `src`，
DSH 的 API key 从登录钥匙串读取，因此密码不会落盘。首次读取时 macOS 可能请求
钥匙串授权，选择“始终允许”后不再询问。

回环口 `127.0.0.1:8765` 仍无认证，只给本机工具。真机走独立的令牌口：
`--remote-port 8766 --remote-bind 0.0.0.0`，每个请求都要求 `Authorization: Bearer <token>`
（令牌自动生成在 `.data/remote-token`）。局域网用 `http://<Mac名>.local:8766`，
外网用 Tailscale serve 的 HTTPS，不开放公网。实体 iPhone 已用开发证书安装；
部署与公网访问仍未做。

## 权限与验收模型

- 模型只能返回 JSON（规划、文件、审查结论），**没有 shell、文件系统或工具权限**；
  所有文件由控制器校验路径后写入隔离 worktree。
- 命令只有调度器能发：测试阶段由控制器在 macOS 沙箱内运行 `unittest`，
  并在 `integration-tests.txt` 末尾写入 `issuer=controller` 的命令审计。
- 产出必须经**独立审查者**（厂商不能与实现者相同）和沙箱测试两道门；
  开启 `require_acceptance` 后，还要**你本人**在 App 里按“接受交付”才算完成，
  打回则带着原因回到实现阶段。
- 设置里的“测试后人工复核”（Kimi）是可选的第三道人工门。

## 真实执行范围

- Codex 使用本机官方 CLI 的 ChatGPT 登录；默认调度模型为账号实际提供的 `gpt-6-astra`。
- Claude Code 使用官方 CLI 的订阅登录，作为独立审查端。未登录会停在 `auth_required`。
- Cursor 使用 Ultra 订阅登录，只允许订阅池模型（Composer、Grok），以 ask 模式在一次性
  空工作区运行，绝不传 `--force`；hooks 仍会加载，属于已知残余面。
- DSH 走 API（V4.1-Flash-1017），每次运行只发一次模型请求：本地计数代理放行首个
  `POST /v1/messages`，后续返回 402 且不建立上游连接。代理还拒绝请求体里不是本次所选
  模型的请求。每次先预留单次上限；成功后 Flash 按返回 token 以保守峰值价结算
  （不超过预留），没有 usage 或没有价目的模型按预留全额计。
- Kimi Max 仅人工交互：对话里以 `/kimi` 开头触发一次调用，或任务测试后的人工复核闸门。
  不排队、不并发、不后台重试。
- 首个真实执行范围是 **Python 标准库项目**：模型返回规划、完整文件与审查结果，
  控制器写入隔离 worktree，审查固定提交 SHA，集成后使用 macOS 沙箱运行 unittest。
- 模型没有 shell、MCP、插件等工具权限；不安装模型要求的依赖。此版本尚非任意语言的自主编程沙箱。
- DSH 需要 API 预算授权；会话标题、重试、压缩等附加请求已在补丁中禁用，
  保证“一次运行 = 一次计费请求”。
- Cursor 的后台调用已打开，但隔离只做到“一次性空工作区 + ask 模式”，
  企业级与团队 hooks 无法关闭，任务结束时结果里会标明这一点。
- Kimi 只提供人工通道；任何试图把它接成无人值守循环的做法都不在本项目范围内。
- 不会因额度、登录或失败而切换 API、账号或购买额度。
- 原生模型调用默认不设执行时限（`ORCHESTRA_NATIVE_TIMEOUT=0`），保留输出上限及进程取消。重启暂停未完成真实调用，用户恢复后才重新调用；
  中断前可能已消耗订阅额度。限流、额度用尽、登录失败、审批等待分别保留状态。
- 记录实际会话 ID 和厂商返回的 token 用量；订阅实际费用未知，显示未知而非 $0。

Mock 模式仍使用固定 `add` 示例，不代表真实厂商联调；可免费演示审批、限流、取消等状态。
真实与 Mock 模式会分别标注。

## 编排 CLI（orch）

`scripts/orch --repo <项目> plan spec.md` 让 Claude 规划并冻结测试，`run` 用 DeepSeek
V4.1 Flash 并行实现，确定性校验后合并到本地 `orch/<run>` 分支，不推送。可选的 Jev
（Vercel AI Gateway）负责失败分诊和预筛。代码索引（ast + tree-sitter）、变异测试与
`orch harden`、planner 进化（`orch evolve`）和大项目的分层规划（`plan --modules`）也在其中。
先运行 `scripts/orch doctor`，或直接 `scripts/orch_demo.sh` 跑一遍演示；Linux 上可以直接用
自带密钥的安装包 `dist/orch-linux.tar.gz`（解包后看 `QUICKSTART.md`）。详见
[docs/ORCH.md](docs/ORCH.md)，压测结果见 [docs/orch-scale.md](docs/orch-scale.md)。

## 官方登录

双击 `scripts/login.command`，选择 Claude、Cursor、Kimi 或 DSH。
登录使用各家官方 CLI；不提取令牌。Codex 已登录时无需重复登录。
DSH 密钥只进入运行中服务的内存，重启需重新输入；预算确认不会发起任务。
登录成功不代表所有厂商已完成真实联调。参见 [厂商接入限制](docs/vendors-expansion.md)。

## 自定义调度

设置支持全局要求、调度模型，以及各厂商的模型和要求。真实模式下“一句话设置”调用
当前调度模型生成结构化草案，显示后由用户应用；不会授予额外权限或启用 API 计费。
Mock 模式仅把句子追加到要求中。配置有版本和审计，新任务保存不可变快照。
当前固定阶段为规划 → 实现 → 独立审查 → 测试 → 汇总；实现与审查厂商可通过要求设置，
但必须是不同厂商，且各自通过认证、权限与额度检查。任意动态 DAG 留待后续。

布局参考 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/client/ui-layout/README.md)，
适配为 iPhone 的聊天、历史与折叠详情。

## 代码与验证

`src/orchestragent/` 包含持久状态机、SQLite 队列、统一事件、版本化设置、原生 CLI 适配与沙箱；
`ios/` 是 SwiftUI 客户端；`tests/` 是本地隔离测试。

```sh
uv run pytest -q
uv run ruff check .
uv run mypy
xcodebuild -project ios/Orchestragent.xcodeproj -scheme Orchestragent \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  -derivedDataPath .build/ios CODE_SIGNING_ALLOWED=NO test
```

自动化测试不调用真实厂商。参见 [验证记录](docs/validation.md)、
[真实接口与限制](docs/LIVE_CONTRACT.md)、[基础接口](docs/CONTRACT.md) 和
[厂商官方资料](docs/vendors.md)。
