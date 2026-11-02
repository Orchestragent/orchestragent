# 调度台 · iPhone 客户端

原生 SwiftUI，iOS 17+，无第三方依赖。默认 `http://localhost:8765`（模拟器）；真机在
设置 → 服务器 里填 `http://<Mac名>.local:8766` 与设备令牌，首次需允许本地网络。
两个标签页：**对话** 是和 Opus 5.5 调度 agent 的会话，**任务** 是派发后的执行记录。

单一调度器对话、历史、任务控制、审批及可展开详情。顶部按当前任务或服务器显示 LIVE / MOCK。Live 显示订阅调用、实际费用未知；不把真实费用显示为 $0。步骤可查看供应商、模型与会话。

正常任务默认采用服务器启动模式，输入框上方可选择 Mock / Live；异常演示场景始终强制 Mock。所有新提交显式携带模式，旧版待确认请求恢复为 Mock。

调度设置包含模型、全局要求、供应商偏好和就绪状态。一句话调整先生成完整预览，再明确应用：Mock 追加原句，Live 调用真实调度模型解析。保存使用版本检查。客户端不提供登录或账户切换。蓝色强调色及历史／对话／详情分工参照 DeepSeek Harness。

提交前将请求 UUID 与完整请求保存到 UserDefaults；失去响应后可重试同一请求。重新打开应用会恢复待确认请求，历史轮询也会按 request_id 对账。草稿独立保存。应用在前台每秒同步，断线每三秒重连；切换后台或关闭应用仅停止轮询，服务器任务继续。返回前台重新同步。

审批卡展示操作原文、完整 SHA-256 摘要与版本，按钮提交该卡中显示的摘要。网络错误不会被当作批准成功、任务成功或已取消。

## 验证

```sh
xcodebuild -project ios/Orchestragent.xcodeproj -scheme Orchestragent -sdk iphonesimulator -configuration Debug -derivedDataPath .build/ios CODE_SIGNING_ALLOWED=NO build
xcodebuild -project ios/Orchestragent.xcodeproj -scheme Orchestragent -destination 'platform=iOS Simulator,name=iPhone 17 Pro' -derivedDataPath .build/ios CODE_SIGNING_ALLOWED=NO test
```

12 项客户端单元测试使用 URLProtocol，覆盖请求恢复、模式隔离、费用未知、旧协议兼容、列表合并及设置冲突。无需服务端或供应商账户。

另有 2 项 localhost 集成测试：任务始终显式发送 `mode=mock`；配置提案／应用测试仅在 Mock 服务器上运行，并恢复原配置（版本号递增）。离线时跳过集成测试，Live 服务器上跳过配置提案测试，避免真实模型调用。

真机安装已用 `DEVELOPMENT_TEAM=7X966SQ93B` 完成；令牌口局域网可用，Tailscale HTTPS 可选。
推送通知、后台持续联网、App Store 发布与持久启动项尚未实现。
