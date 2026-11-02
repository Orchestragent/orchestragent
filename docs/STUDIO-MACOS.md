# Orch Studio for macOS — 前端规格

本文是 macOS 原生客户端的完整规格。后端接口见 [STUDIO.md](STUDIO.md)（以它为准；两份文档冲突时，接口以 STUDIO.md 为准，交互以本文为准）。

## 0. 分工与约定

- **你（前端）**：`apps/macos/**` 和 `.github/workflows/macos-app.yml`，只改这两处。
- **我（后端）**：`orch studio` 服务、`--demo` 演示模式、接口实现与测试，以及 `docs/STUDIO.md`。需要改接口时开 issue 或在 PR 里提，我来改后端和文档。
- **分支**：从 `native` 拉 `macos-app`，PR 指向 `native`。
- **开发阶段**：先用 `orch studio --demo` 开发，不需要 key，也不花钱；真实后端就绪后切换，接口不变。

## 1. 产品目标

一句话：**看清每个节点在干什么、卡在哪、为什么卡；只在那一个节点里出手，不打乱整个网络。**

1. **全局可见**：一张节点图，每个节点的状态、所处阶梯、模型、花费、卡住原因（自己 / 上游 / 测试 / 环境）一眼可见。
2. **局部可控**：点进节点只看到它自己的信息；在节点对话框里给这个节点的 agent 发指引，权限只覆盖这个节点（它自己的文件）。
3. **改动可追踪**：节点改动后通知下游，下游自动判断是否受影响（重跑冻结测试），受影响的才重做；界面上要看得见这条涟漪。
4. **模型可选**：每个节点可以单独选模型和推理强度，候选只来自已配置的 key 或订阅。
5. **开箱即用**：app 内填 key、订阅登录、选项目、新建运行，全程不需要命令行。

## 2. 技术选型

- **语言与框架**：Swift 5.10+，SwiftUI 为主，必要处用 AppKit（`NSViewRepresentable`、`NSOpenPanel`、`NSWorkspace`）。
- **最低系统**：macOS 14 Sonoma（用 `@Observable`、`MagnifyGesture`、`Inspector`、String Catalog）。
- **工程**：Xcode 工程 `apps/macos/OrchStudio.xcodeproj`（或 SwiftPM + 生成脚本，二选一，CI 能 `xcodebuild` 即可）。
- **Bundle id**：`dev.orchestragent.studio`；显示名 "Orch Studio"。
- **依赖**：不引入第三方依赖。SSE 用 `URLSession.bytes(for:)` 按行解析即可。
- **沙盒**：不开 App Sandbox。要启动 `orch` 子进程，还要读写用户选的仓库；Hardened Runtime 可以开。
- **签名**：CI 用 ad-hoc（`codesign -s -`）；正式发布再说。

## 3. 架构

```
OrchStudioApp (SwiftUI App)
 ├─ ServerController   启动/停止 `orch studio` 子进程，或连接远程 URL
 ├─ APIClient          REST（async/await）+ EventStream（SSE）
 ├─ Stores (@Observable)
 │   ├─ AppStore       boot 信息、runs 列表、当前项目、连接状态
 │   ├─ RunStore       当前 run 的 graph（nodes/edges）、选中节点、布局
 │   ├─ NodeStore      当前打开节点的详情（/api/node），发送中的消息
 │   └─ SettingsStore  providers/roles/routes
 └─ Views
     ├─ MainWindow: NavigationSplitView(sidebar: RunsSidebar, content: GraphCanvas) + .inspector(NodeInspector)
     ├─ NewRunSheet
     └─ Settings scene（Cmd+,）
```

### 3.1 启动本地服务（ServerController）

1. 找 `orch` 可执行文件，依次尝试：用户在设置里指定的路径 → 登录 shell 的 PATH（`/bin/zsh -lc 'command -v orch'`）→ `/opt/homebrew/bin/orch` → `/usr/local/bin/orch` → `~/.local/bin/orch`。
2. 找不到时显示引导页：说明 + 一键复制 `brew install Orchestragent/tap/orch` + "重新检测"按钮。
3. 用 `Process` 启动 `orch studio --repo <DIR> --port 0 --machine`，stdin 接一个 `Pipe` 并保持打开（服务在 stdin EOF 时退出，app 崩溃也不会留下孤儿进程）。
4. 读 stdout 第一行 JSON：`{"url","token","pid","version","api"}`。超过 15 秒没有输出，或进程提前退出，就显示错误页：附 stderr 最后 40 行，提供"重试"和"打开日志"。
5. `api != 1` 时提示"orch 版本与 app 不匹配"，附升级命令。
6. 退出 app 或切换项目时：关闭 stdin，然后 `terminate()`，等 3 秒，必要时 `kill`。
7. stderr 持续写入 `~/Library/Logs/OrchStudio/server.log`（滚动保留 5 MB），菜单 Help ▸ Show Server Log 可以打开。

### 3.2 连接远程服务

设置 ▸ 服务器：选"本机"（默认）或"远程"。远程模式需要填 URL 和 token，token 存钥匙串，不存 UserDefaults；页面上提示用 `ssh -L 8790:localhost:8790 host` 建隧道。远程模式不启动子进程，其余功能完全一样。

### 3.3 项目选择

- 首次启动弹出 `NSOpenPanel` 选择 git 仓库目录，非 git 目录要提示。
- 最近项目记在 UserDefaults（最多 10 个，存 security-scoped bookmark 也可以，因为不开沙盒）。
- File ▸ Open Project…（Cmd+O）、File ▸ Open Recent。
- 切换项目等于重启服务。
- 窗口标题显示仓库名；副标题显示当前 run 的标题。

### 3.4 APIClient

- 所有请求都带 `Authorization: Bearer <token>`。
- `async throws` 接口，解码失败、HTTP 错误统一成 `APIError(code, message, status)`。
  - `message` 来自后端 `error.message`，可以直接展示给用户。
  - 401：重新拉起本地服务；远程模式下提示 token 失效。
  - 409/422：toast 显示 message。
  - 网络错误：顶部出现"连接中断，正在重连…"横幅。
- **EventStream**：`GET /api/events?run=ID`。
  - 收到 `changed` 后节流 250 ms，再并发刷新 graph 和当前打开的 node。
  - 收到 `runs` 时刷新 runs 列表。
  - 断线指数退避重连（0.5 s → 8 s 封顶），重连成功立即全量刷新一次。
- 所有 Codable 模型按 STUDIO.md 定义，未知字段忽略，枚举遇到未知值落到 `.unknown(String)`，不能崩。

## 4. 主窗口

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ ● ● ●  ledger-demo — Add CSV import            [Run ▸ running 08:42] [$0.84 ¥1.97] [⟳][⏸][⚙]│  ← Toolbar
├────────────┬───────────────────────────────────────────────┬─────────────────┤
│ RUNS       │                                               │  T-003  PARKED  │
│ ● r7313…   │     ┌───────┐        ┌───────┐                │  upstream T-001 │
│ ✓ r2b4b…   │     │ T-001 │───────▶│ T-003 │─┐              │  [model chip ▾] │
│ ✓ raa86…   │     └───────┘   ┌───▶└───────┘ │  ┌───────┐   │ ─────────────── │
│            │     ┌───────┐   │              └─▶│ T-005 │   │ Conversation |  │
│ + New run  │     │ T-002 │───┘    ┌───────┐    └───────┘   │ Attempts | Tests│
│            │     └───────┘        │ T-004 │──────▲         │ …               │
│            │                      └───────┘                │ [composer]      │
│            │  [−][+][fit][relayout]              [minimap]  │                 │
└────────────┴───────────────────────────────────────────────┴─────────────────┘
│ status bar: 3 done · 1 running · 1 parked · live ●   last event 2 s ago        │
```

### 4.1 工具栏

- **左侧**：run 状态胶囊（颜色见 §6）+ 阶段文字（planning / test design / building / final gate / finished）+ 已用时间（实时走秒，`elapsed_s` 加本地计时）。
- **花费**：`$usd · ¥cny`，悬停显示说明"Claude 等按美元计，DeepSeek 按人民币计"。
- **按钮**：
  - Resume（run 非 live 且未完成时可用）；
  - Cancel（需要确认对话框）；
  - Settings；
  - 外观切换（跟随系统 / 浅色 / 深色）。

### 4.2 Runs 侧边栏

- **列表**：每行是状态点 + 标题（单行截断）+ 相对时间 + `done/tasks` 小计；live 的 run 状态点有呼吸动画（尊重"减弱动态效果"设置）。
- **右键菜单**：Resume、Cancel、Copy run id。
- **底部**："+ New run"（Cmd+N）。
- **空状态**："还没有运行。写下编号需求（R1. … R2. …），orch 会拆分任务、冻结测试、并行构建。" + 按钮"新建运行"。

### 4.3 新建运行（Sheet）

- 等宽字体的多行编辑器，占位文本示例：
  ```
  R1. api/ledger (Go): ParseCSV(r io.Reader) ([]Entry, error) …
  R2. web/src/ledger.ts (TypeScript): parseExport(json) …
  ```
- 下方提示："写清楚放在哪个文件或包里、用什么语言；依赖要先装好（npm ci / go mod download）"。
- 可选："从文件导入…"（.md/.txt）。
- 按钮：Cancel / Start（Cmd+Enter）。`POST /api/run/start` 成功后关闭 sheet，选中新 run。

## 5. 节点图（GraphCanvas）

### 5.1 布局

- **默认分层布局**：x = 依赖深度 × 280pt，同一层按 id 排序，y 间距 140pt；整体居中。
- **拖拽节点**：可以拖动，按住 Shift 拖动吸附到 20pt 网格。
- **位置持久化**：保存在 `~/Library/Application Support/OrchStudio/layouts/<repo-hash>/<run>.json`。
- **重新布局**：工具按钮 "Relayout"（Cmd+Shift+L），带动画回到分层布局。
- **新节点**（replan 产生）：放在被替换节点附近，不打乱已有位置。

### 5.2 节点卡片（240 × 104pt，圆角 12）

```
┌───────────────────────────────────────┐
│ ▍T-003  ExportJSON writes canonical…  │  ← 左侧 4pt 色条 = 状态色
│ ● PARKED · replan · 4 attempts        │
│ ⚠ upstream T-001 implicated            │  ← 诊断行（kind ≠ done/running 时）
│ [◆ DeepSeek Flash · low]   $0.21 ¥0.35 │  ← 模型 chip（厂商色）
└───────────────────────────────────────┘
```

- **running/verifying/merging**：状态点旋转或脉冲；verifying、merging 显示小字 "verifying…" / "merging…"。
- **person**：有人类消息或被接管时，右上角显示 💬 n / 👤 角标。
- **ripple**：
  - `affected`：橙色描边闪烁 3 次，再加持续角标 "上游变更 · 重做中"；
  - `unaffected`：绿色描边闪 1 次，角标 "上游变更 · 仍通过"，10 秒后淡出。
- **route_override**：模型 chip 左侧加一个小图钉，表示人为指定。
- **悬停**：tooltip 显示完整 goal + diagnosis.summary。
- **选中**：外圈 2pt 强调色描边；上游边和下游边加粗。
- **blamed 高亮**：选中节点时，`diagnosis.blamed` 里的节点外圈变红色虚线，连到它们的边变红色并有流动动画。

### 5.3 边

三次贝塞尔曲线，从左节点右侧中点到右节点左侧中点，带箭头。颜色规则：
- 默认：中性灰；
- 上游已完成：实线；未完成：虚线；
- 当前选中节点相关的边：高亮。

### 5.4 视图操作

- **平移**：拖动空白处，或触控板双指滚动。
- **缩放**：捏合、Cmd+滚轮、Cmd+= / Cmd+-，范围 25%–200%。
- **适配窗口**：Fit（Cmd+0）。
- **小地图**：右下角，可折叠。
- **键盘**：方向键在节点间移动选中（按几何最近邻），Return 打开 inspector，Esc 取消选中或关闭 inspector，Tab / Shift+Tab 按 id 顺序切换。
- **右键菜单**：Open、Message…（打开 inspector 并聚焦输入框）、Retry（仅 PARKED）、Switch Model ▸（子菜单列出 routes）、Copy Node ID、Reveal Files in Finder。
- **无障碍**：每个节点是一个可聚焦元素，VoiceOver 读出"T-003，已暂停，上游 T-001 可能有问题，模型 DeepSeek Flash low"。

## 6. 颜色

### 6.1 状态色（浅/深色模式各一套，用 Asset Catalog）

| 状态 | 颜色 | 说明 |
|---|---|---|
| PLANNED | 灰蓝 #56637A | 等待 |
| READY | 淡蓝 #7D93C4 | 排队 |
| RUNNING | 天蓝 #38BDF8 | 生成中，动画 |
| VERIFYING | 紫 #A78BFA | 跑测试 |
| MERGING | 琥珀 #F59E0B | 合并中 |
| DONE | 绿 #34D399 | 完成 |
| PARKED | 橙 #FB923C | 卡住，需要人 |
| REPLANNED | 石板灰 #94A3B8 | 已被替换，卡片半透明 |

### 6.2 厂商色（模型 chip、节点图例）

| vendor | 颜色 | 图标建议 |
|---|---|---|
| anthropic | 陶土橙 #D97757 | ✳ |
| deepseek | 蓝 #4D6BFE | ◆ |
| openai | 青绿 #10A37F | ○ |
| cursor | 黑/白（随外观） | ▲ |
| google | 蓝 #4285F4 | ✦ |
| moonshot | 靛 #6366F1 | ☾ |
| qwen | 紫 #7C3AED | ◎ |
| person | 品红 #DB2777 | 👤 |
| other | 灰 #8B95A5 | ● |

图例放在画布左下角，可折叠。

## 7. 节点检查器（Inspector，右侧，宽 380–560pt 可拖）

只显示该节点自己的信息（`GET /api/node`）；打开时拉取，`changed` 事件时刷新。

### 7.1 头部

- 第一行：id + 状态胶囊；第二行：goal（可多行）。
- 诊断卡片：图标 + `diagnosis.summary`；`blamed` 里的节点 id 是可点击的链接，点击后跳到那个节点（画布滚动并选中）。
- **模型选择**：chip + 下拉菜单，内容是 `routes`，按厂商分组，每组里按 effort 排序；未验证的选项标"unverified"；第一项"Role default (…)"表示清除覆盖。选中后 `POST /api/node/route`，toast"T-003 下次尝试起使用 …"。
- **操作按钮**：Retry（仅 PARKED）、Copy ID、Reveal in Finder（打开它拥有文件的所在目录）。

### 7.2 Tabs

1. **Conversation（默认）**
   - 时间线显示 `thread`：
     - `you`：右侧气泡，强调色；
     - `agent`：左侧气泡，厂商色细边，标注模型和第几次尝试；
     - `orch`：居中的小号系统提示（started / failed / passed / merged / parked / ripple / route）；其中 failed 可展开报告摘要，点"查看"跳到 Attempts tab 对应条目。
   - 底部输入框：
     - 多行，Cmd+Enter 发送，Enter 换行；
     - 输入框上方固定一行小字："作用范围：仅 T-003 及其拥有的文件（api/ledger/export.go）"。
   - 发送规则：

     | 节点状态 | 行为 |
     |---|---|
     | PARKED | 发送 = 指引 + 自动回到 READY，按钮文字"发送并重试" |
     | READY/RUNNING/… | 发送 = 指引，下一次尝试生效，按钮"发送" |
     | DONE | 多一个开关"立即按此重做（会通知下游）"，默认关；开时按钮"发送并重做"，`rework:true` |
     | REPLANNED | 输入框禁用，提示"此节点已被 T-006 替换"，提供跳转 |

   - 发送中按钮显示进度，失败时保留输入内容并 toast 错误信息。
2. **Attempts**
   - 列表，每次尝试一行：`a3` · 模型 chip · rung · 结果图标 · 耗时；
   - 展开后显示 summary、report（等宽字体，可复制，长内容折叠）、diff（等宽字体，+ 行绿底、- 行红底，带文件头）。
3. **Tests**
   - 冻结检查列表：命令（等宽字体，可复制）+ 测试文件（语法高亮可选，至少等宽字体加行号）；
   - 顶部说明："测试已冻结，节点不能修改。如果认为测试有错，在对话里说明，agent 会发起测试争议，由审查流程裁决。"
4. **Files**
   - 拥有的文件，标注 stub / built / candidate，内容只读预览；
   - "Reveal in Finder"、"Open in Editor"（`NSWorkspace.open`）。
5. **Spec**
   - requirements（R 编号 + 原文）和 contract 文本。

## 8. 设置（Settings scene，Cmd+,）

1. **Providers**
   - 每个 provider 一行：厂商图标 + 标题 + 状态（已配置 ✓ / 未配置 / CLI 未安装）。
   - `kind: key`：SecureField + Save。保存后清空输入框，显示"已保存"。
     - 永远不回显 key，也不在任何日志里打印。
     - 无论保存成功还是失败，都不能把 key 留在内存模型里。
   - `kind: subscription`：按钮"在终端登录"。用 `NSAppleScript` 或临时 `.command` 文件打开 Terminal，执行 `login.command`，并提示下一步（`login.then`，例如 `/login`）。完成后用户回到 app 点"刷新"。
2. **Roles**
   - 四个角色（planner、test designer、builder、reviewer）各一行：说明 + 下拉选择（routes），默认项标"verified"；
   - 选非 verified 项时出现黄色提示"未经基准验证"。
3. **Server**
   - 本机 / 远程（URL + token）、orch 路径（自动检测 / 手动选择）、显示当前 orch 版本。
4. **Appearance**
   - 外观：跟随系统 / 浅色 / 深色；
   - 语言：跟随系统 / English / 简体中文；
   - 动画：跟随系统"减弱动态效果"，也可以强制关闭。
5. **Notifications**
   - 开关，以下三项都默认开：节点卡住（PARKED）、run 完成、run 失败。

## 9. 通知与 Dock

- 首次需要发通知时请求权限，用 `UNUserNotificationCenter`。
- 触发条件：在两次 graph 刷新之间比较节点状态，某节点进入 PARKED，或 run 进入 done / parked / failed。
- 点击通知：激活窗口，选中对应 run 和节点。
- Dock 角标显示 PARKED 节点数，为 0 时清除。

## 10. 菜单与快捷键

| 动作 | 快捷键 |
|---|---|
| 新建运行 | ⌘N |
| 打开项目 | ⌘O |
| 设置 | ⌘, |
| 刷新 | ⌘R |
| 适配窗口 / 放大 / 缩小 | ⌘0 / ⌘= / ⌘- |
| 重新布局 | ⇧⌘L |
| 切换侧边栏 / 检查器 | ⌃⌘S / ⌥⌘I |
| 聚焦对话输入框 | ⌘L（节点打开时） |
| 发送 | ⌘↩ |
| 重试选中节点 | ⌘⇧R |
| 快捷键帮助 | ⌘/ |

Help 菜单：Orch Studio Help（打开 GitHub README）、Show Server Log、Report an Issue。

## 11. 本地化

- String Catalog（`Localizable.xcstrings`），提供 en 和 zh-Hans 两种语言。
- 状态名、诊断类型、rung 名称都要翻译；后端返回的 `summary`、`message` 原样显示（后端用英文）。
- 诊断类型对照：done 已完成 · running 进行中 · queued 排队中 · waiting 等待上游 · own 自身测试失败 · upstream 上游可能有问题 · test 测试有争议 · environment 环境问题 · replanned 已重新规划。
- rung 对照：first try 首次尝试 · retry 重试 · samples 多候选 · Claude patch Claude 修复 · thicken 补充契约 · replan 重新规划。

## 12. 状态与边界情况

- **加载**：用骨架屏，不用转圈遮罩。
- **graph 为空**（planning 阶段）：画布中央显示"规划中：Opus 正在拆分任务…"，加阶段进度。
- **服务断开**：顶部横幅 + 自动重连；断开期间所有写操作按钮禁用。
- **demo 模式**（`boot.demo`）：标题栏显示"DEMO"标签。
- **大图**（>60 个节点）：关闭边的流动动画，小地图默认展开。
- **长文本**：goal 截断加 tooltip；report/diff 超过 2000 行时虚拟化，或者折叠后提示"在编辑器中打开"。

## 13. 开发方式

1. 安装 native 分支的 orch：`uv tool install --force "git+https://github.com/Orchestragent/orchestragent@native"`（或者克隆后 `uv tool install -e .`）。
2. 演示后端：`orch studio --demo --port 8790`，终端会打印 URL 和 token。app 设置里选"远程"连它，或者 app 直接用 `--demo` 启动子进程（可以加一个隐藏开关：按住 ⌥ 点"打开项目"时进入 demo）。
3. 真实后端：在一个装好依赖的仓库里 `orch studio --repo .`。
4. 接口不清楚或缺字段时，在 PR 里 @ 我；STUDIO.md 由我维护。

## 14. CI

`.github/workflows/macos-app.yml`：

- **触发**：`pull_request` 和 push 到 `main`，限 `apps/macos/**` 路径。
- **运行环境**：`macos-14`。
- **步骤**：
  - `xcodebuild -scheme OrchStudio -configuration Release build`；
  - 跑单元测试（解码、SSE 解析、布局算法、发送规则）；
  - ad-hoc 签名，打包成 zip，作为 `OrchStudio-app` artifact 上传。

## 15. 验收清单

- [ ] 首次启动选择仓库 → 自动拉起 `orch studio`，退出 app 后子进程消失（`pgrep -f "orch studio"` 为空）。
- [ ] 找不到 orch 时有清楚的安装引导。
- [ ] demo 模式下节点状态变化实时出现在画布上（不用手动刷新），PARKED 时收到系统通知、Dock 角标变化。
- [ ] 节点颜色按状态、模型 chip 按厂商着色，图例正确；浅色和深色模式都清晰。
- [ ] 拖动节点后重启 app，位置保持；Relayout 可恢复。
- [ ] 选中卡住的节点，blamed 上游节点和连线被高亮；诊断卡里的链接能跳转。
- [ ] 检查器只展示该节点的信息；对话发送规则符合 §7.2 的表格（PARKED / DONE+重做 / REPLANNED 禁用）。
- [ ] 每个节点可以切换模型，chip 出现图钉；切回 role default 后图钉消失。
- [ ] ripple 的 affected / unaffected 动效和角标正确。
- [ ] 设置里保存 key 后输入框清空；任何界面和日志都看不到 key；订阅登录能打开终端。
- [ ] 所有快捷键可用；VoiceOver 能读出节点信息；"减弱动态效果"开启时没有闪烁和流动动画。
- [ ] 中英文切换完整，没有漏翻的界面文字。
- [ ] 断网或杀掉服务后有横幅提示，恢复后自动重连并全量刷新。
- [ ] 1280×800 和 1728×1117 窗口下没有文字重叠或截断错位。

## 16. 里程碑建议

1. **M1 只读**：服务启动和连接、runs 列表、画布（布局、拖拽、缩放）、检查器的 Attempts / Tests / Files / Spec、SSE 刷新。基于 demo 开发。
2. **M2 交互**：对话发送、模型切换、Retry、新建 / 恢复 / 取消运行、ripple 展示、通知和 Dock 角标。
3. **M3 设置**：providers（key、订阅登录）、roles、远程服务器、外观、语言。
4. **M4 打磨**：快捷键、无障碍、小地图、动画细节、大图性能、CI 产物。
