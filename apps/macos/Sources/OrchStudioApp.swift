import SwiftUI
import AppKit

@main
@MainActor
struct OrchStudioApp: App {
    @NSApplicationDelegateAdaptor(StudioAppDelegate.self) private var delegate
    @State private var store = AppStore()
    @AppStorage("appearance") private var appearance = "system"
    @AppStorage("language") private var language = "system"

    var body: some Scene {
        WindowGroup("Orch Studio", id: "studio") {
            MainWindow(store: store)
                .id(language)
                .tint(Palette.accent)
                .preferredColorScheme(appearance == "system" ? nil : appearance == "dark" ? .dark : .light)
                .frame(minWidth: 1000, minHeight: 640)
                .onAppear { delegate.store = store }
        }
        .defaultSize(width: 1440, height: 900)
        .commands { StudioCommands(store: store) }
        Settings {
            StudioSettingsView(store: store)
                .id(language).tint(Palette.accent)
                .preferredColorScheme(appearance == "system" ? nil : appearance == "dark" ? .dark : .light)
        }
    }
}

@MainActor
final class StudioAppDelegate: NSObject, NSApplicationDelegate {
    var store: AppStore?
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) { store?.shutdown() }
}

@MainActor
struct StudioCommands: Commands {
    var store: AppStore
    private func post(_ name: String) { NotificationCenter.default.post(name: Notification.Name(name), object: nil) }
    var body: some Commands {
        CommandGroup(replacing: .newItem) {
            Button(L("New Run…", "新建运行…")) { post("studioNewRun") }.keyboardShortcut("n")
            Button(L("Open Project…", "打开项目…")) { post("studioOpenProject") }.keyboardShortcut("o")
            Menu(L("Open Recent", "打开最近项目")) {
                ForEach(store.recentProjects, id: \.self) { path in
                    Button(URL(fileURLWithPath: path).lastPathComponent) {
                        Task { await store.connect(repo: path) }
                    }.help(path)
                }
            }
            Divider()
            Button(L("Open Demo", "打开演示")) { Task { await store.connect(repo: nil, demo: true) } }
        }
        CommandMenu(L("Run", "运行")) {
            Button(L("Refresh", "刷新")) { Task { await store.refresh() } }.keyboardShortcut("r")
            Button(L("Retry Selected Node", "重试选中节点")) { Task { await store.retryNode() } }
                .keyboardShortcut("r", modifiers: [.command, .shift])
                .disabled(!store.connected || store.detail?.node.status != .parked)
            Button(L("Focus Conversation", "聚焦对话")) {
                store.inspectorVisible = true; store.focusComposerRequested = true
            }.keyboardShortcut("l").disabled(store.selectedNodeID == nil)
        }
        CommandGroup(after: .sidebar) {
            Button(L("Toggle Inspector", "切换检查器")) { store.inspectorVisible.toggle() }
                .keyboardShortcut("i", modifiers: [.command, .option])
            Divider()
            Button(L("Zoom In", "放大")) { post("studioZoomIn") }.keyboardShortcut("=")
            Button(L("Zoom Out", "缩小")) { post("studioZoomOut") }.keyboardShortcut("-")
            Button(L("Fit Graph", "适配节点图")) { post("studioFit") }.keyboardShortcut("0")
            Button(L("Relayout", "重新布局")) { post("studioRelayout") }
                .keyboardShortcut("l", modifiers: [.command, .shift])
        }
        CommandGroup(replacing: .help) {
            Button(L("Orch Studio Help", "Orch Studio 帮助")) { open("https://github.com/Orchestragent/orchestragent#readme") }
            Button(L("Keyboard Shortcuts", "快捷键")) { post("studioShortcuts") }.keyboardShortcut("/")
            Button(L("Show Server Log", "显示服务器日志")) {
                NSWorkspace.shared.open(FileManager.default.homeDirectoryForCurrentUser.appending(path: "Library/Logs/OrchStudio/server.log"))
            }
            Button(L("Report an Issue", "报告问题")) { open("https://github.com/Orchestragent/orchestragent/issues/new") }
        }
    }
    private func open(_ address: String) { if let url = URL(string: address) { NSWorkspace.shared.open(url) } }
}
