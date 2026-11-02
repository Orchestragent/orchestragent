import AppKit
import SwiftUI

@MainActor
struct StudioSettingsView: View {
    let store: AppStore
    @State private var section = SettingsSection.providers
    @State private var remoteToken = ""
    @State private var serverError: String?
    @AppStorage("serverMode") private var serverMode = "local"
    @AppStorage("remoteURL") private var remoteURL = "http://localhost:8790/"
    @AppStorage("orchPath") private var orchPath = ""
    @AppStorage("appearance") private var appearance = "system"
    @AppStorage("language") private var language = "system"
    @AppStorage("animationsOff") private var animationsOff = false
    @AppStorage("notifiedParked") private var notifiedParked = true
    @AppStorage("notifiedDone") private var notifiedDone = true
    @AppStorage("notifiedFailed") private var notifiedFailed = true

    var body: some View {
        NavigationSplitView {
            List(SettingsSection.allCases, selection: $section) { item in
                Label(item.title, systemImage: item.icon).tag(item)
            }.listStyle(.sidebar).navigationSplitViewColumnWidth(160)
        } detail: {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    HStack {
                        Text(section.title).font(.title2.weight(.semibold))
                        Spacer()
                        if section == .providers || section == .roles {
                            Button(L("Refresh", "刷新"), systemImage: "arrow.clockwise") { Task { await store.loadSettings() } }
                                .disabled(!store.connected)
                        }
                    }
                    if let error = serverError ?? store.error {
                        Label(error, systemImage: "exclamationmark.triangle").font(.callout).foregroundStyle(.orange).textSelection(.enabled)
                    }
                    switch section {
                    case .providers: providers
                    case .roles: roles
                    case .server: server
                    case .appearance: appearanceSettings
                    case .notifications: notificationSettings
                    }
                }.padding(24).frame(maxWidth: .infinity, alignment: .leading)
            }.background(.background)
        }
        .frame(minWidth: 740, idealWidth: 820, minHeight: 540, idealHeight: 620)
        .task { if store.connected { await store.loadSettings() } }
        .onChange(of: section) { _, next in if next != .server { remoteToken = "" } }
    }

    private var providers: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(L("Connect your API keys or existing subscriptions. Credentials are never displayed after saving.", "配置 API key 或已有订阅。保存后不会回显凭据。"))
                .font(.callout).foregroundStyle(.secondary)
            if let settings = store.settings {
                ForEach(settings.providers) { provider in
                    ProviderSettingsRow(provider: provider, store: store, local: store.isLocalConnection)
                }
                if settings.providers.isEmpty { unavailable(L("No providers available", "暂无可用提供商")) }
            } else { unavailable(L("Connect to a server to load providers.", "连接服务器后加载提供商。")) }
        }
    }

    private var roles: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(L("Choose the model for each role. Node overrides take precedence on their next attempt.", "选择每个角色的模型。节点覆盖设置会在下次尝试时优先使用。"))
                .font(.callout).foregroundStyle(.secondary)
            if let settings = store.settings {
                ForEach(settings.roles) { role in
                    VStack(alignment: .leading, spacing: 8) {
                        Text(roleTitle(role)).font(.headline)
                        Text(role.description).font(.callout).foregroundStyle(.secondary)
                        Menu {
                            Button(L("Role default", "角色默认") + (role.defaultRoute.map { " · " + $0.name + ($0.verified ? L(" · verified", " · 已验证") : "") } ?? "")) {
                                Task { await store.setRole(role: role.role, route: "") }
                            }
                            Divider()
                            ForEach(Array(Set(settings.routes.map(\.vendor))).sorted(), id: \.self) { vendor in
                                Section(vendor.capitalized) {
                                    ForEach(settings.routes.filter { $0.vendor == vendor }.sorted(by: routeOrder)) { route in
                                        Button(route.name + (route.verified ? L(" · verified", " · 已验证") : L(" · unverified", " · 未经验证"))) {
                                            Task { await store.setRole(role: role.role, route: route.label) }
                                        }
                                    }
                                }
                            }
                        } label: {
                            HStack {
                                if let route = role.route ?? role.defaultRoute { RouteChip(route: route) }
                                else { Text(L("Role default", "角色默认")) }
                                Spacer()
                                Image(systemName: "chevron.up.chevron.down").font(.caption)
                            }
                        }.disabled(!store.connected)
                        if let route = role.route ?? role.defaultRoute, !route.verified {
                            Label(L("Not benchmark verified", "未经基准验证"), systemImage: "exclamationmark.triangle.fill")
                                .font(.caption).foregroundStyle(.orange)
                        }
                    }.padding(14).background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: 10))
                }
            } else { unavailable(L("Connect to a server to load roles.", "连接服务器后加载角色。")) }
        }
    }

    private var server: some View {
        VStack(alignment: .leading, spacing: 18) {
            Picker(L("Server", "服务器"), selection: $serverMode) {
                Text(L("Local", "本机")).tag("local")
                Text(L("Remote", "远程")).tag("remote")
            }.pickerStyle(.segmented)
            if serverMode == "remote" {
                GroupBox {
                    VStack(alignment: .leading, spacing: 10) {
                        TextField(L("Server URL", "服务器 URL"), text: $remoteURL).textFieldStyle(.roundedBorder)
                        SecureField(L("Token (leave empty to use saved token)", "Token（留空使用已保存凭据）"), text: $remoteToken).textFieldStyle(.roundedBorder)
                        Text(L("For a remote host, create an SSH tunnel first:", "访问远程主机前，先建立 SSH 隧道：")).font(.caption).foregroundStyle(.secondary)
                        Text("ssh -L 8790:localhost:8790 host").font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                        Button(L("Save & Connect", "保存并连接")) { connectRemote() }
                            .buttonStyle(.borderedProminent).disabled(store.connecting)
                        Text(L("Tokens are stored in macOS Keychain. Local file actions and terminal login are unavailable for remote servers.", "Token 存于 macOS 钥匙串。远程连接不提供本地文件操作和终端登录。"))
                            .font(.caption).foregroundStyle(.secondary)
                    }.padding(8).frame(maxWidth: .infinity, alignment: .leading)
                }
            } else {
                GroupBox {
                    VStack(alignment: .leading, spacing: 10) {
                        Text(L("orch executable", "orch 可执行文件")).font(.headline)
                        HStack {
                            TextField(L("Detect automatically", "自动检测"), text: $orchPath).textFieldStyle(.roundedBorder)
                            Button(L("Choose…", "选择…")) { chooseExecutable() }
                        }
                        Text(L("Leave empty to search your login shell PATH and standard install locations.", "留空时会搜索登录 shell 的 PATH 和常用安装位置。"))
                            .font(.caption).foregroundStyle(.secondary)
                        HStack {
                            Button(L("Reconnect local server", "重新连接本机服务")) {
                                Task { await store.connect(repo: store.repoPath.isEmpty ? nil : store.repoPath, demo: store.boot?.demo == true) }
                            }.buttonStyle(.borderedProminent).disabled(store.connecting)
                            Button(L("Launch demo", "启动演示")) { Task { await store.connect(repo: nil, demo: true) } }
                                .disabled(store.connecting)
                        }
                    }.padding(8).frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            HStack {
                Circle().fill(store.connected ? .green : .orange).frame(width: 7, height: 7)
                Text(store.connecting ? L("Connecting…", "连接中…") : store.connected ? L("Connected", "已连接") : L("Disconnected", "未连接"))
                Spacer()
                if let boot = store.boot { Text("orch \(boot.version)").foregroundStyle(.secondary) }
            }.font(.callout)
            if let boot = store.boot {
                LabeledContent(L("Project", "项目"), value: boot.demo ? L("Demo · no paid calls", "演示 · 无付费调用") : boot.repo)
                    .font(.callout).textSelection(.enabled)
            }
        }
    }

    private var appearanceSettings: some View {
        Form {
            Picker(L("Appearance", "外观"), selection: $appearance) {
                Text(L("System", "跟随系统")).tag("system")
                Text(L("Light", "浅色")).tag("light")
                Text(L("Dark", "深色")).tag("dark")
            }
            Picker(L("Language", "语言"), selection: $language) {
                Text(L("System", "跟随系统")).tag("system")
                Text("English").tag("en")
                Text("简体中文").tag("zh-Hans")
            }
            Toggle(L("Disable animations", "关闭动画"), isOn: $animationsOff)
            Text(L("System Reduce Motion is always respected.", "始终遵循系统“减弱动态效果”设置。"))
                .font(.caption).foregroundStyle(.secondary)
        }.formStyle(.grouped)
    }

    private var notificationSettings: some View {
        Form {
            Toggle(L("A node needs attention (PARKED)", "节点卡住（PARKED）"), isOn: $notifiedParked)
            Toggle(L("Run completed", "运行完成"), isOn: $notifiedDone)
            Toggle(L("Run failed", "运行失败"), isOn: $notifiedFailed)
            Text(L("macOS will ask for permission when the first notification is needed. Change system permissions in System Settings → Notifications → Orch Studio.", "首次需要通知时，macOS 会请求权限。可在系统设置 → 通知 → Orch Studio 中调整系统权限。"))
                .font(.caption).foregroundStyle(.secondary)
        }.formStyle(.grouped)
    }

    private func unavailable(_ text: String) -> some View {
        Label(text, systemImage: "network").foregroundStyle(.secondary).padding(.vertical, 24)
    }
    private func chooseExecutable() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false; panel.allowsMultipleSelection = false
        panel.message = L("Choose the orch executable", "选择 orch 可执行文件")
        panel.begin { response in
            guard response == .OK else { return }
            Task { @MainActor in if let url = panel.url { orchPath = url.path } }
        }
    }
    private func connectRemote() {
        serverError = nil
        let token = remoteToken.isEmpty ? (Keychain.read("remoteToken") ?? "") : remoteToken
        remoteToken = ""
        guard let url = URL(string: remoteURL.trimmingCharacters(in: .whitespacesAndNewlines)),
              ["http", "https"].contains(url.scheme?.lowercased() ?? ""),
              url.host != nil, url.user == nil, url.password == nil, url.query == nil, url.fragment == nil else {
            serverError = L("Enter an http(s) server URL without credentials, query or fragment.", "请输入不含凭据、查询参数或片段的 http(s) 服务器 URL。")
            return
        }
        guard !token.isEmpty else { serverError = L("Enter the server token.", "请输入服务器 token。"); return }
        do { try Keychain.save(token, account: "remoteToken") }
        catch { serverError = L("Could not save the token in Keychain.", "无法将 token 保存到钥匙串。"); return }
        Task { await store.connectRemote(url: url, token: token) }
    }
}

private enum SettingsSection: String, CaseIterable, Identifiable {
    case providers, roles, server, appearance, notifications
    var id: String { rawValue }
    var title: String {
        switch self {
        case .providers: L("Providers", "提供商")
        case .roles: L("Roles", "角色")
        case .server: L("Server", "服务器")
        case .appearance: L("Appearance", "外观")
        case .notifications: L("Notifications", "通知")
        }
    }
    var icon: String {
        switch self {
        case .providers: "key"
        case .roles: "person.3"
        case .server: "network"
        case .appearance: "paintbrush"
        case .notifications: "bell"
        }
    }
}

@MainActor
private struct ProviderSettingsRow: View {
    let provider: Provider
    let store: AppStore
    let local: Bool
    @State private var key = ""
    @State private var saving = false
    @State private var feedback: String?
    @State private var feedbackError = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top) {
                Image(systemName: provider.kind == "key" ? "key.fill" : "terminal.fill")
                    .foregroundStyle(Palette.vendor(provider.vendor)).frame(width: 24)
                VStack(alignment: .leading, spacing: 3) {
                    Text(provider.title).font(.headline)
                    Text(provider.detail).font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Image(systemName: provider.configured ? "checkmark.circle.fill" : "minus.circle")
                    .foregroundStyle(provider.configured ? .green : .secondary)
                    .accessibilityLabel(provider.configured ? L("Configured", "已配置") : L("Not configured", "未配置"))
            }
            if provider.kind == "key" {
                HStack {
                    SecureField(L("API key", "API key"), text: $key).textFieldStyle(.roundedBorder)
                        .disabled(!store.connected || saving).onSubmit { save() }
                    if saving { ProgressView().controlSize(.small) }
                    Button(L("Save", "保存")) { save() }
                        .disabled(!store.connected || saving || key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            } else if let login = provider.login {
                Button(L("Log in with Terminal", "在终端登录"), systemImage: "terminal") { launchLogin(login) }
                    .disabled(!local || !store.connected)
                if !login.then.isEmpty {
                    Text(L("In the terminal, enter: \(login.then). Then return here and refresh.", "在终端输入：\(login.then)。完成后返回此处并刷新。"))
                        .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                }
            }
            if let feedback {
                Label(feedback, systemImage: feedbackError ? "exclamationmark.triangle" : "checkmark.circle")
                    .font(.caption).foregroundStyle(feedbackError ? .orange : .green)
            }
        }.padding(14).background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: 10))
            .onDisappear { key = "" }
    }

    private func save() {
        guard store.connected, !saving, !key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        let credential = key
        key = ""
        saving = true; feedback = nil
        Task { @MainActor in
            let success = await store.saveKey(provider: provider.name, key: credential)
            saving = false
            feedbackError = !success
            feedback = success ? L("Saved", "已保存") : L("Could not save. Enter the key again to retry.", "保存失败。请重新输入 key 后重试。")
        }
    }

    private func launchLogin(_ login: ProviderLogin) {
        guard local, store.connected else { return }
        // Only the documented subscription launchers are allowed. Every argument is
        // quoted separately; backend strings never become executable shell syntax.
        let arguments = login.command.split(whereSeparator: \.isWhitespace).map(String.init)
        let allowed = [["claude"], ["claude", "auth", "login"], ["codex", "login"], ["cursor-agent", "login"], ["gemini"]]
        guard allowed.contains(arguments) else {
            feedbackError = true; feedback = L("Unsupported login command. Use your provider's official login instructions.", "不支持此登录命令。请使用提供商的官方登录说明。")
            return
        }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("OrchStudio-login-" + UUID().uuidString, isDirectory: true)
        let script = directory.appendingPathComponent("Login.command")
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
            let next = login.then.isEmpty ? L("Complete login, then refresh Providers in Orch Studio.", "完成登录后，请在 Orch Studio 刷新提供商。") : L("When prompted, enter \(login.then). Then refresh Providers in Orch Studio.", "出现提示后输入 \(login.then)，随后在 Orch Studio 刷新提供商。")
            let command = "#!/bin/zsh -l\ntrap 'rm -f -- \"$0\"; rmdir -- \"${0:h}\"' EXIT\nprintf '%s\\n' " + shellQuote(next) + "\n" + arguments.map(shellQuote).joined(separator: " ") + "\n"
            try command.write(to: script, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: script.path)
            let configuration = NSWorkspace.OpenConfiguration()
            NSWorkspace.shared.open([script], withApplicationAt: URL(fileURLWithPath: "/System/Applications/Utilities/Terminal.app"), configuration: configuration) { _, error in
                if error != nil {
                    try? FileManager.default.removeItem(at: directory)
                    Task { @MainActor in
                        feedbackError = true
                        feedback = L("Could not open the login terminal.", "无法打开登录终端。")
                    }
                }
            }
            feedbackError = false; feedback = L("Complete login in Terminal, then click Refresh above.", "请在终端完成登录，再点击上方刷新。")
        } catch {
            try? FileManager.default.removeItem(at: directory)
            feedbackError = true; feedback = L("Could not open the login terminal.", "无法打开登录终端。")
        }
    }
    private func shellQuote(_ argument: String) -> String { "'" + argument.replacingOccurrences(of: "'", with: "'\\''") + "'" }
}
