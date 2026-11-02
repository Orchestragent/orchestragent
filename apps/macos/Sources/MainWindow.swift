import SwiftUI
import AppKit
import UniformTypeIdentifiers

@MainActor
struct MainWindow: View {
    @Bindable var store: AppStore
    @State private var showNewRun = false
    @State private var showShortcuts = false
    @State private var cancelID: String?
    @State private var didStart = false
    @State private var columns = NavigationSplitViewVisibility.all

    var body: some View {
        NavigationSplitView(columnVisibility: $columns) {
            sidebar.navigationSplitViewColumnWidth(min: 190, ideal: 216, max: 290)
        } detail: {
            VStack(spacing: 0) {
                if !store.connected && (store.boot != nil || store.connecting) { connectionBanner }
                if store.boot == nil {
                    welcome
                } else {
                    GraphCanvas(store: store)
                    statusBar
                }
            }
            .inspector(isPresented: $store.inspectorVisible) {
                NodeInspector(store: store)
                    .inspectorColumnWidth(min: 380, ideal: 410, max: 560)
            }
            .navigationTitle(store.boot?.repoName ?? "Orch Studio")
            .navigationSubtitle(store.graph?.run.title ?? L("A clear view of every agent", "看清每个节点，只在需要的地方出手"))
            .toolbar { toolbar }
        }
        .sheet(isPresented: $showNewRun) { NewRunSheet(store: store) }
        .sheet(isPresented: $showShortcuts) { shortcuts }
        .alert(L("Something needs attention", "需要处理"), isPresented: Binding(get: { store.error != nil }, set: { if !$0 { store.error = nil } })) {
            Button(L("OK", "好")) { store.error = nil }
            Button(L("Show Log", "查看日志")) {
                NSWorkspace.shared.open(FileManager.default.homeDirectoryForCurrentUser.appending(path: "Library/Logs/OrchStudio/server.log"))
            }
        } message: { Text(store.error ?? "") }
        .confirmationDialog(L("Cancel this run?", "取消本次运行？"), isPresented: Binding(get: { cancelID != nil }, set: { if !$0 { cancelID = nil } })) {
            Button(L("Cancel Run", "取消运行"), role: .destructive) {
                if let id = cancelID { Task { await store.cancelRun(id) } }
                cancelID = nil
            }
        } message: { Text(L("Active work will stop. You can resume the run later.", "将停止正在执行的工作，之后可以恢复运行。")) }
        .overlay(alignment: .top) {
            if let toast = store.toast {
                Text(toast).font(.callout).padding(.horizontal, 16).padding(.vertical, 10)
                    .background(.regularMaterial, in: Capsule()).shadow(color: .black.opacity(0.08), radius: 12, y: 5)
                    .padding(.top, 12).allowsHitTesting(false)
                    .task(id: toast) {
                        try? await Task.sleep(for: .seconds(5))
                        if store.toast == toast { store.toast = nil }
                    }
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: Notification.Name("studioNewRun"))) { _ in showNewRun = true }
        .onReceive(NotificationCenter.default.publisher(for: Notification.Name("studioOpenProject"))) { _ in chooseProject() }
        .onReceive(NotificationCenter.default.publisher(for: Notification.Name("studioShortcuts"))) { _ in showShortcuts = true }
        .task {
            guard !didStart else { return }; didStart = true
            guard ProcessInfo.processInfo.environment["ORCH_STUDIO_TESTING"] != "1", store.boot == nil, !store.connecting else { return }
            if ProcessInfo.processInfo.arguments.contains("--demo") {
                await store.connect(repo: nil, demo: true)
            } else if UserDefaults.standard.string(forKey: "serverMode") == "remote",
                      let address = UserDefaults.standard.string(forKey: "remoteURL"), let url = URL(string: address),
                      let token = Keychain.read("remoteToken"), !token.isEmpty {
                await store.connectRemote(url: url, token: token)
            } else if let index = ProcessInfo.processInfo.arguments.firstIndex(of: "--repo"),
                      ProcessInfo.processInfo.arguments.indices.contains(index + 1) {
                await store.connect(repo: ProcessInfo.processInfo.arguments[index + 1])
            } else if let recent = store.recentProjects.first {
                await store.connect(repo: recent)
            } else {
                chooseProject()
            }
        }
    }

    private var sidebar: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: "point.3.connected.trianglepath.dotted").font(.title3).foregroundStyle(Palette.accent)
                Text("Orch Studio").font(.system(size: 15, weight: .semibold))
                Spacer()
            }.padding(.horizontal, 16).padding(.top, 18).padding(.bottom, 22)
            HStack {
                Text(L("WORKSPACE", "工作区")).font(.system(size: 10, weight: .semibold)).tracking(1.5).foregroundStyle(.secondary)
                Spacer()
                Button(action: chooseProject) { Image(systemName: "folder.badge.plus").font(.system(size: 12)) }
                    .buttonStyle(.plain).help(L("Open Project", "打开项目"))
            }.padding(.horizontal, 16).padding(.bottom, 8)
            if let boot = store.boot {
                Label(boot.repoName, systemImage: "folder").font(.system(size: 12, weight: .medium))
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 16).padding(.bottom, 20)
            }
            HStack {
                Text(L("RUNS", "运行记录")).font(.system(size: 10, weight: .semibold)).tracking(1.5).foregroundStyle(.secondary)
                Spacer()
                Text("\(store.runs.count)").font(.system(size: 10, design: .monospaced)).foregroundStyle(.tertiary)
            }.padding(.horizontal, 16)
            List(selection: Binding(get: { store.selectedRunID }, set: { id in if let id { Task { await store.selectRun(id) } } })) {
                ForEach(store.runs) { run in
                    RunRow(run: run).tag(run.id)
                        .contextMenu {
                            Button(L("Resume", "恢复")) { Task { await store.resumeRun(run.id) } }
                                .disabled(!store.connected || run.live || run.status == "done")
                            Button(L("Cancel Run…", "取消运行…"), role: .destructive) { cancelID = run.id }
                                .disabled(!store.connected || run.status == "done" || run.status == "cancelled")
                            Button(L("Copy Run ID", "复制运行 ID")) { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(run.id, forType: .string) }
                        }
                }
            }.listStyle(.sidebar)
            if store.runs.isEmpty {
                Text(L("No runs yet. Turn a numbered spec into a network of focused agents.", "还没有运行。写下编号需求，让 orch 拆分任务、冻结测试、并行构建。"))
                    .font(.caption).foregroundStyle(.secondary).padding(16)
            }
            Spacer(minLength: 0)
            Divider()
            Button { showNewRun = true } label: {
                Label(L("New Run", "新建运行"), systemImage: "plus").font(.system(size: 12, weight: .medium))
                    .frame(maxWidth: .infinity, alignment: .leading).padding(12)
            }.buttonStyle(.plain).disabled(!store.connected)
            HStack(spacing: 6) {
                Circle().fill(store.connected ? Palette.accent : .orange).frame(width: 5, height: 5)
                Text(store.connected ? L("Connected", "已连接") : L("Offline", "未连接"))
                Spacer()
                if store.boot?.demo == true { Text("DEMO").fontWeight(.semibold) }
                SettingsLink { Image(systemName: "gearshape") }.buttonStyle(.plain)
            }.font(.system(size: 10)).foregroundStyle(.secondary).padding(.horizontal, 16).padding(.bottom, 12)
        }
    }

    @ToolbarContentBuilder private var toolbar: some ToolbarContent {
        ToolbarItem(placement: .navigation) {
            if store.boot?.demo == true {
                Text("DEMO").font(.system(size: 9, weight: .bold)).tracking(1).padding(.horizontal, 7).padding(.vertical, 4)
                    .foregroundStyle(Palette.accent).background(Palette.accent.opacity(0.1), in: Capsule())
            }
        }
        ToolbarItemGroup(placement: .primaryAction) {
            if let run = store.graph?.run {
                TimelineView(.periodic(from: .now, by: 1)) { context in
                    HStack(spacing: 7) {
                        Circle().fill(Palette.run(run.status)).frame(width: 6, height: 6)
                        Text(phaseTitle(run.phase ?? run.status)).fontWeight(.medium)
                        Text(elapsed(run, now: context.date)).monospacedDigit().foregroundStyle(.secondary)
                    }.font(.system(size: 11)).padding(.horizontal, 9).padding(.vertical, 5)
                        .background(.quaternary.opacity(0.5), in: Capsule())
                }
                Text(String(format: "$%.2f · ¥%.2f", run.cost.usd, run.cost.cny)).font(.system(size: 11, design: .monospaced))
                    .help(L("Claude and other USD providers · DeepSeek in CNY", "Claude 等按美元计 · DeepSeek 按人民币计"))
                if !run.live && run.status != "done" {
                    Button { Task { await store.resumeRun(run.id) } } label: { Image(systemName: "play") }
                        .disabled(!store.connected).help(L("Resume", "恢复"))
                }
                Button { cancelID = run.id } label: { Image(systemName: "stop") }
                    .disabled(!store.connected || ["done", "cancelled"].contains(run.status)).help(L("Cancel Run", "取消运行"))
            }
            Button { Task { await store.refresh() } } label: { Image(systemName: "arrow.clockwise") }
                .disabled(store.connecting).help(L("Refresh", "刷新"))
            Button { store.inspectorVisible.toggle() } label: { Image(systemName: "sidebar.right") }
                .help(L("Toggle Inspector", "切换检查器"))
            Menu { Picker(L("Appearance", "外观"), selection: appearanceBinding) {
                Text(L("System", "跟随系统")).tag("system")
                Text(L("Light", "浅色")).tag("light")
                Text(L("Dark", "深色")).tag("dark")
            } } label: { Image(systemName: "circle.lefthalf.filled") }
            SettingsLink { Image(systemName: "gearshape") }.help(L("Settings", "设置"))
        }
    }
    private var appearanceBinding: Binding<String> {
        Binding(get: { UserDefaults.standard.string(forKey: "appearance") ?? "system" }, set: { UserDefaults.standard.set($0, forKey: "appearance") })
    }
    private var connectionBanner: some View {
        HStack(spacing: 8) {
            Image(systemName: "wifi.exclamationmark")
            Text(store.connecting ? L("Connecting to orch…", "正在连接 orch…") : L("Connection lost. Reconnecting…", "连接中断，正在重连…"))
            Spacer()
        }.font(.caption).padding(10).background(Color.orange.opacity(0.12))
    }
    private var statusBar: some View {
        HStack(spacing: 16) {
            let nodes = store.graph?.nodes ?? []
            Label("\(nodes.filter { $0.status == .done }.count) " + L("done", "已完成"), systemImage: "checkmark.circle")
            Label("\(nodes.filter { $0.status.active }.count) " + L("active", "执行中"), systemImage: "circle.dotted")
            Label("\(nodes.filter { $0.status == .parked }.count) " + L("need attention", "待处理"), systemImage: "exclamationmark.circle")
            Spacer()
            Circle().fill(store.connected ? Palette.accent : .orange).frame(width: 5, height: 5)
            Text(store.connected ? L("Live updates", "实时更新") : L("Reconnecting", "正在重连"))
        }.font(.system(size: 10)).foregroundStyle(.secondary).padding(.horizontal, 18).padding(.vertical, 9)
            .background(.bar).overlay(alignment: .top) { Divider() }
    }
    private var welcome: some View {
        VStack(spacing: 18) {
            Image(systemName: "point.3.connected.trianglepath.dotted").font(.system(size: 52, weight: .ultraLight)).foregroundStyle(Palette.accent)
            Text(L("Your agents. One clear picture.", "每个节点，一目了然。")).font(.system(size: 26, weight: .medium))
            Text(store.connecting ? L("Starting your local orch service…", "正在启动本机 orch 服务…") : L("Open a repository to build, or explore a live demo without keys.", "打开仓库开始构建，或通过无需 key 的演示体验完整流程。"))
                .foregroundStyle(.secondary).multilineTextAlignment(.center)
            HStack {
                Button(L("Open Project…", "打开项目…"), action: chooseProject).buttonStyle(.borderedProminent)
                Button(L("Explore Demo", "体验演示")) { Task { await store.connect(repo: nil, demo: true) } }
            }.disabled(store.connecting)
            Divider().frame(width: 280).padding(.vertical, 8)
            Text(L("Need orch? Install it once:", "还没有 orch？安装一次即可：")).font(.caption).foregroundStyle(.secondary)
            HStack {
                Text("brew install Orchestragent/tap/orch").font(.system(size: 11, design: .monospaced)).textSelection(.enabled)
                Button { NSPasteboard.general.clearContents(); NSPasteboard.general.setString("brew install Orchestragent/tap/orch", forType: .string) } label: { Image(systemName: "doc.on.doc") }
                    .buttonStyle(.plain).help(L("Copy Install Command", "复制安装命令"))
            }
            Button(L("Detect Again", "重新检测")) { Task { await store.connect(repo: store.repoPath.isEmpty ? nil : store.repoPath) } }.disabled(store.connecting)
        }.padding(40).frame(maxWidth: .infinity, maxHeight: .infinity)
    }
    private var shortcuts: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(L("Keyboard Shortcuts", "快捷键")).font(.title2)
            ForEach([(L("New Run", "新建运行"), "⌘N"), (L("Open Project", "打开项目"), "⌘O"), (L("Refresh", "刷新"), "⌘R"), (L("Fit Graph", "适配节点图"), "⌘0"), (L("Zoom", "缩放"), "⌘+ / ⌘−"), (L("Relayout", "重新布局"), "⇧⌘L"), (L("Inspector", "检查器"), "⌥⌘I"), (L("Conversation", "对话"), "⌘L"), (L("Send", "发送"), "⌘↩"), (L("Retry Node", "重试节点"), "⇧⌘R")], id: \.0) { item in
                HStack { Text(item.0); Spacer(); Text(item.1).foregroundStyle(.secondary).monospaced() }
            }
            Button(L("Done", "完成")) { showShortcuts = false }.keyboardShortcut(.defaultAction).frame(maxWidth: .infinity, alignment: .trailing)
        }.padding(28).frame(width: 380)
    }
    private func elapsed(_ run: RunSummary, now: Date) -> String {
        let seconds = max(0, Int(run.live ? now.timeIntervalSince1970 - (run.started ?? run.created) : run.elapsedS ?? 0))
        return String(format: "%02d:%02d", seconds / 60, seconds % 60)
    }
    private func chooseProject() {
        if NSEvent.modifierFlags.contains(.option) { Task { await store.connect(repo: nil, demo: true) }; return }
        let panel = NSOpenPanel(); panel.canChooseDirectories = true; panel.canChooseFiles = false
        panel.prompt = L("Open Project", "打开项目")
        guard panel.runModal() == .OK, let url = panel.url else { return }
        guard FileManager.default.fileExists(atPath: url.appending(path: ".git").path) else {
            store.error = L("Choose a Git repository (a directory containing .git).", "请选择 Git 仓库目录（包含 .git 文件或目录）。"); return
        }
        Task { await store.connect(repo: url.path) }
    }
}

@MainActor
private struct RunRow: View {
    var run: RunSummary
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @AppStorage("animationsOff") private var animationsOff = false
    @State private var pulse = false
    var body: some View {
        HStack(alignment: .top, spacing: 9) {
            Circle().fill(Palette.run(run.status)).frame(width: 6, height: 6).padding(.top, 5)
                .opacity(run.live && pulse && !reduceMotion && !animationsOff ? 0.35 : 1)
                .animation(run.live && !reduceMotion && !animationsOff ? .easeInOut(duration: 1.4).repeatForever(autoreverses: true) : nil, value: pulse)
            VStack(alignment: .leading, spacing: 5) {
                Text(run.title).font(.system(size: 12, weight: .medium)).lineLimit(1)
                HStack {
                    Text(Date(timeIntervalSince1970: run.created), style: .relative).lineLimit(1)
                    Spacer(minLength: 4)
                    Text("\(run.done)/\(run.tasks)").monospacedDigit()
                }.font(.system(size: 10)).foregroundStyle(.secondary)
            }
        }.padding(.vertical, 8).help(run.title).onAppear { pulse = true }
    }
}

@MainActor
private struct NewRunSheet: View {
    var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var spec = ""
    @State private var starting = false
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(L("Start something new", "开始新的构建")).font(.system(size: 24, weight: .medium))
            Text(L("Describe the outcome. Orch will plan the work, freeze checks, and build in parallel.", "写下目标，orch 会拆分任务、冻结检查并行构建。")).foregroundStyle(.secondary)
            ZStack(alignment: .topLeading) {
                TextEditor(text: $spec).font(.system(size: 12, design: .monospaced)).padding(8)
                if spec.isEmpty {
                    Text("R1. api/ledger (Go): ParseCSV(r io.Reader) ([]Entry, error) …\nR2. web/src/ledger.ts (TypeScript): parseExport(json) …")
                        .font(.system(size: 12, design: .monospaced)).foregroundStyle(.tertiary).padding(14).allowsHitTesting(false)
                }
            }.frame(height: 260).overlay(RoundedRectangle(cornerRadius: 8).stroke(.quaternary))
            Text(L("Include file or package paths and languages. Install dependencies first (npm ci / go mod download).", "写清文件或包路径及语言，先安装依赖（npm ci / go mod download）。"))
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Button(L("Import File…", "从文件导入…"), action: importSpec)
                Spacer()
                Button(L("Cancel", "取消")) { dismiss() }.keyboardShortcut(.cancelAction)
                Button(starting ? L("Starting…", "正在启动…") : L("Start Run", "开始运行")) {
                    starting = true
                    Task { if await store.startRun(spec: spec) { dismiss() }; starting = false }
                }.buttonStyle(.borderedProminent).keyboardShortcut(.return, modifiers: .command)
                    .disabled(starting || !store.connected || spec.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }.padding(28).frame(width: 620)
    }
    private func importSpec() {
        let panel = NSOpenPanel(); panel.allowedContentTypes = [.plainText, UTType(filenameExtension: "md") ?? .plainText]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do { spec = try String(contentsOf: url, encoding: .utf8) } catch { store.error = error.localizedDescription }
    }
}
