import Foundation
import Observation

@MainActor @Observable
final class AppStore {
    var boot: Boot?
    var runs: [RunSummary] = []
    var graph: Graph?
    var detail: NodeDetail?
    var settings: StudioSettings?
    var selectedRunID: String?
    var selectedNodeID: String?
    var connected = false
    var connecting = false
    var error: String?
    var toast: String?
    var repoPath = ""
    var recentProjects: [String]
    var inspectorVisible = false
    var focusComposerRequested = false
    var isLocalConnection: Bool { local }
    let server = ServerController()
    @ObservationIgnored private var api: APIClient?
    @ObservationIgnored private var streamTask: Task<Void, Never>?
    @ObservationIgnored private var changeTask: Task<Void, Never>?
    @ObservationIgnored private var recoveryTask: Task<Void, Never>?
    @ObservationIgnored private var recoveryID = UUID()
    @ObservationIgnored private var connectionID = UUID()
    @ObservationIgnored private var selectionID = UUID()
    @ObservationIgnored private var nodeSelectionID = UUID()
    @ObservationIgnored private var graphRequestID = UUID()
    @ObservationIgnored private var nodeRequestID = UUID()
    @ObservationIgnored private var local = true
    @ObservationIgnored private var demoMode = false
    @ObservationIgnored private var retriedUnauthorized = false
    @ObservationIgnored private let notifications = StudioNotifications()

    init() {
        recentProjects = UserDefaults.standard.stringArray(forKey: "recentProjects") ?? []
        notifications.onSelect = { [weak self] run, node in
            Task { @MainActor in
                guard let self else { return }
                await self.selectRun(run)
                if let node { await self.selectNode(node) }
            }
        }
    }

    func connect(repo: String?, demo: Bool = false) async {
        cancelRecovery()
        _ = await connectLocal(repo: repo, demo: demo, resetRetry: true)
    }
    private func connectLocal(repo: String?, demo: Bool, resetRetry: Bool, recovering: Bool = false) async -> Bool {
        disconnect()
        local = true; demoMode = demo
        if resetRetry { retriedUnauthorized = false }
        UserDefaults.standard.set("local", forKey: "serverMode")
        connecting = true; error = nil
        let id = connectionID
        do {
            if !demo {
                guard let repo, FileManager.default.fileExists(atPath: URL(fileURLWithPath: repo).appendingPathComponent(".git").path) else {
                    throw APIError(code: "not_repository", message: L("Choose a Git repository directory.", "请选择 Git 仓库目录。"), status: 0)
                }
                repoPath = repo
            }
            let handshake = try await server.start(repo: repo, demo: demo)
            guard id == connectionID else { return false }
            api = APIClient(baseURL: handshake.url, token: handshake.token)
            try await finishConnection(id: id)
            if let repo, !demo {
                recentProjects = Array(([repo] + recentProjects.filter { $0 != repo }).prefix(10))
                UserDefaults.standard.set(recentProjects, forKey: "recentProjects")
                UserDefaults.standard.set(repo, forKey: "lastProject")
            }
            return connected
        } catch {
            guard id == connectionID else { return false }
            connecting = recovering; connected = false
            if !recovering { handle(error, initialConnection: true) }
            return false
        }
    }
    func connectRemote(url: URL, token: String) async {
        cancelRecovery()
        disconnect(); local = false; demoMode = false; retriedUnauthorized = false
        connecting = true; error = nil
        let id = connectionID
        do {
            guard !token.isEmpty else { throw APIError(code: "token_missing", message: L("Enter the remote server token.", "请输入远程服务 token。"), status: 0) }
            try Keychain.save(token, account: "remoteToken")
            UserDefaults.standard.set(url.absoluteString, forKey: "remoteURL")
            UserDefaults.standard.set("remote", forKey: "serverMode")
            api = APIClient(baseURL: url, token: token)
            try await finishConnection(id: id)
        } catch {
            guard id == connectionID else { return }
            connecting = false; connected = false; handle(error, initialConnection: true)
        }
    }
    private func finishConnection(id: UUID) async throws {
        guard let api else { return }
        let info: Boot = try await api.get("api/boot")
        guard id == connectionID else { return }
        guard info.api == 1 else { throw APIError(code: "api_version", message: L("orch API version is incompatible. Upgrade orch and reconnect.", "orch API 版本不兼容，请升级 orch 后重新连接。"), status: 0) }
        boot = info; runs = info.runs; repoPath = info.repo; demoMode = info.demo
        connected = true; connecting = false; error = nil
        await selectRun(info.runs.first?.id)
        await loadSettings()
        if id == connectionID { startStream() }
    }
    private func disconnect() {
        connectionID = UUID(); selectionID = UUID(); nodeSelectionID = UUID()
        streamTask?.cancel(); streamTask = nil; changeTask?.cancel(); changeTask = nil
        api?.invalidate(); api = nil; server.shutdown()
        connected = false; connecting = false
        boot = nil; runs = []; graph = nil; detail = nil; settings = nil
        selectedRunID = nil; selectedNodeID = nil; notifications.clearBadge()
    }
    private func cancelRecovery() {
        recoveryID = UUID(); recoveryTask?.cancel(); recoveryTask = nil
    }
    func shutdown() { cancelRecovery(); disconnect() }

    /// Resolve a backend-owned file only within an actual local, non-demo repository.
    func localFileURLs(_ paths: [String]) -> [URL] {
        guard isLocalConnection, let boot, boot.platform == "darwin", !boot.demo else { return [] }
        let repository = URL(fileURLWithPath: boot.repo, isDirectory: true).standardizedFileURL.resolvingSymlinksInPath()
        let prefix = repository.path.hasSuffix("/") ? repository.path : repository.path + "/"
        return paths.compactMap { path in
            let candidate = (path.hasPrefix("/") ? URL(fileURLWithPath: path) : repository.appendingPathComponent(path))
                .standardizedFileURL.resolvingSymlinksInPath()
            guard candidate.path.hasPrefix(prefix), FileManager.default.fileExists(atPath: candidate.path) else { return nil }
            return candidate
        }
    }
    private func beginLocalRecovery(singleAttempt: Bool = false) {
        guard local, recoveryTask == nil else { return }
        let repo = repoPath; let demo = demoMode; let run = selectedRunID; let node = selectedNodeID
        let recovery = UUID(); recoveryID = recovery
        connected = false; connecting = true
        recoveryTask = Task { [weak self] in
            guard let self else { return }
            defer { if self.recoveryID == recovery { self.recoveryTask = nil } }
            var delay = 0.5
            while !Task.isCancelled, self.recoveryID == recovery {
                do { try await Task.sleep(for: .seconds(delay)) } catch { return }
                let success = await self.connectLocal(repo: repo, demo: demo, resetRetry: false, recovering: true)
                guard !Task.isCancelled, self.recoveryID == recovery else { return }
                if success {
                    if let run, self.runs.contains(where: { $0.id == run }) {
                        await self.selectRun(run)
                        if let node, self.graph?.nodes.contains(where: { $0.id == node }) == true { await self.selectNode(node) }
                    }
                    return
                }
                if singleAttempt {
                    self.connecting = false
                    self.error = L("The local service could not reconnect. Try opening the project again.", "无法重新连接本机服务，请尝试重新打开项目。")
                    return
                }
                delay = min(delay * 2, 8)
            }
        }
    }

    func refresh() async {
        guard let api else { return }
        let id = connectionID
        async let list: Void = refreshRuns(api: api, id: id)
        async let current: Void = refreshSelection()
        _ = await (list, current)
    }
    private func refreshRuns(api: APIClient, id: UUID) async {
        do {
            let updated: [RunSummary] = try await api.get("api/runs")
            guard id == connectionID else { return }
            runs = updated
        } catch { if id == connectionID { handle(error) } }
    }
    func selectRun(_ id: String) async { await selectRun(Optional(id)) }
    private func selectRun(_ id: String?) async {
        selectionID = UUID(); nodeSelectionID = UUID()
        selectedRunID = id; selectedNodeID = nil; graph = nil; detail = nil
        changeTask?.cancel(); changeTask = nil
        startStream()
        await refreshSelection()
    }
    func selectNode(_ id: String?) async {
        nodeSelectionID = UUID(); selectedNodeID = id; detail = nil
        if id != nil { inspectorVisible = true }
        await refreshNode()
    }
    private func refreshSelection() async {
        async let graphRefresh: Void = refreshGraph()
        async let nodeRefresh: Void = refreshNode()
        _ = await (graphRefresh, nodeRefresh)
    }
    private func refreshGraph() async {
        guard let api, let run = selectedRunID else { return }
        let connection = connectionID; let selection = selectionID
        let request = UUID(); graphRequestID = request
        do {
            let updated: Graph = try await api.get("api/graph", query: ["run": run])
            guard connection == connectionID, selection == selectionID, request == graphRequestID else { return }
            notifications.update(previous: graph, current: updated); graph = updated
            if let index = runs.firstIndex(where: { $0.id == run }) { runs[index] = updated.run }
        } catch {
            if connection == connectionID, selection == selectionID, request == graphRequestID { handle(error) }
        }
    }
    private func refreshNode() async {
        guard let api, let run = selectedRunID, let node = selectedNodeID else { return }
        let connection = connectionID; let selection = selectionID; let nodeSelection = nodeSelectionID
        let request = UUID(); nodeRequestID = request
        do {
            let updated: NodeDetail = try await api.get("api/node", query: ["run": run, "task": node])
            guard connection == connectionID, selection == selectionID, nodeSelection == nodeSelectionID, request == nodeRequestID else { return }
            detail = updated
        } catch {
            if connection == connectionID, selection == selectionID, nodeSelection == nodeSelectionID, request == nodeRequestID { handle(error) }
        }
    }
    private func startStream() {
        streamTask?.cancel(); changeTask?.cancel(); changeTask = nil
        guard let api else { return }
        let connection = connectionID; let selection = selectionID; let run = selectedRunID
        streamTask = Task { [weak self] in
            var delay = 0.5
            var reconnect = false
            while !Task.isCancelled {
                guard let self, self.connectionID == connection, self.selectionID == selection else { return }
                do {
                    try await api.events(run: run) { [weak self] event in
                        guard let self, !Task.isCancelled, self.connectionID == connection, self.selectionID == selection else { return }
                        if event.event == "hello" {
                            self.connected = true; self.connecting = false; self.error = nil
                            if reconnect { await self.refresh() }
                            delay = 0.5
                        } else if event.event == "changed" {
                            self.scheduleChanged(connection: connection, selection: selection)
                        } else if event.event == "runs" {
                            await self.refreshRuns(api: api, id: connection)
                        }
                    }
                } catch {
                    guard !Task.isCancelled, self.connectionID == connection, self.selectionID == selection else { return }
                    self.connected = false; self.connecting = true
                    if self.local, !self.server.isRunning {
                        self.beginLocalRecovery()
                        return
                    }
                    self.handle(error, transient: true)
                    if (error as? APIError)?.status == 401 { return }
                }
                reconnect = true
                do { try await Task.sleep(for: .seconds(delay)) } catch { return }
                delay = min(delay * 2, 8)
            }
        }
    }
    private func scheduleChanged(connection: UUID, selection: UUID) {
        // Coalescing, rather than a resettable debounce: continuous changes still refresh.
        guard changeTask == nil else { return }
        changeTask = Task { [weak self] in
            do { try await Task.sleep(for: .milliseconds(250)) } catch { return }
            guard let self, self.connectionID == connection, self.selectionID == selection else { return }
            self.changeTask = nil
            await self.refreshSelection()
        }
    }
    private func handle(_ failure: Error, transient: Bool = false, initialConnection: Bool = false) {
        guard !(failure is CancellationError), (failure as? URLError)?.code != .cancelled else { return }
        if let apiError = failure as? APIError {
            if [409, 422].contains(apiError.status) { toast = apiError.message; return }
            if apiError.status == 401 {
                connected = false; connecting = false
                if local, !retriedUnauthorized {
                    retriedUnauthorized = true
                    beginLocalRecovery(singleAttempt: true)
                } else {
                    error = local ? apiError.message : L("The remote server token is invalid or expired. Update it in Settings.", "远程服务 token 无效或已过期，请在设置中更新。")
                }
                return
            }
        }
        if transient || failure is URLError && !initialConnection {
            connected = false; connecting = true
            return
        }
        error = failure.localizedDescription
    }
    private func requireConnection() -> APIClient? {
        guard connected, let api else { error = L("Connect to the server before making changes.", "请先连接服务再进行操作。"); return nil }
        return api
    }
    private struct OK: Decodable { var ok: Bool }
    private struct RunResult: Decodable { var ok: Bool; var run: RunSummary }

    func sendMessage(text: String, rework: Bool) async -> Bool {
        guard let api = requireConnection(), let run = selectedRunID, let node = selectedNodeID else { return false }
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { error = L("Enter a message first.", "请先输入消息。"); return false }
        if let detail, !MessagePolicy.canSend(status: detail.node.status, cancelled: graph?.run.status == "cancelled") {
            error = L("This node cannot receive messages in its current state.", "该节点当前状态无法接收消息。"); return false
        }
        let id = connectionID; let selectedRun = selectionID; let selectedNode = nodeSelectionID
        do {
            let _: OK = try await api.post("api/node/message", body: ["run": run, "task": node, "text": text, "rework": rework && detail?.node.status == .done])
            guard id == connectionID, selectedRun == selectionID, selectedNode == nodeSelectionID else { return false }
            await refresh(); return true
        } catch { if id == connectionID { handle(error) }; return false }
    }
    func retryNode() async { await nodeAction("retry", extra: [:]) }
    func setNodeRoute(_ label: String) async { await nodeAction("route", extra: ["route": label]) }
    private func nodeAction(_ action: String, extra: [String: Any]) async {
        guard let api = requireConnection(), let run = selectedRunID, let node = selectedNodeID else { return }
        let id = connectionID
        do {
            let _: OK = try await api.post("api/node/\(action)", body: ["run": run, "task": node].merging(extra) { _, new in new })
            guard id == connectionID else { return }
            toast = action == "route" ? L("\(node): model selection applies from the next attempt.", "\(node)：模型选择将从下次尝试生效。") : L("\(node) queued for retry.", "\(node) 已加入重试队列。")
            await refresh()
        } catch { if id == connectionID { handle(error) } }
    }
    func startRun(spec: String) async -> Bool {
        guard let api = requireConnection() else { return false }
        guard !spec.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { error = L("Enter a numbered specification first.", "请先输入带编号的需求。"); return false }
        let id = connectionID
        do {
            let result: RunResult = try await api.post("api/run/start", body: ["spec": spec])
            guard id == connectionID else { return false }
            await refreshRuns(api: api, id: id)
            await selectRun(result.run.id); return true
        } catch { if id == connectionID { handle(error) }; return false }
    }
    func resumeRun(_ id: String) async { await runAction("resume", run: id) }
    func cancelRun(_ id: String) async { await runAction("cancel", run: id) }
    private func runAction(_ action: String, run: String) async {
        guard let api = requireConnection() else { return }
        let id = connectionID
        do {
            let _: OK = try await api.post("api/run/\(action)", body: ["run": run])
            if id == connectionID { await refresh() }
        } catch { if id == connectionID { handle(error) } }
    }
    func loadSettings() async {
        guard let api else { return }
        let id = connectionID
        do {
            let value: StudioSettings = try await api.get("api/settings")
            if id == connectionID { settings = value }
        } catch { if id == connectionID { handle(error) } }
    }
    func saveKey(provider: String, key: String) async -> Bool {
        guard let api = requireConnection() else { return false }
        let id = connectionID
        do {
            let _: OK = try await api.post("api/settings/key", body: ["provider": provider, "key": key])
            guard id == connectionID else { return false }
            toast = L("Key saved.", "已保存 key。"); await loadSettings(); return true
        } catch { if id == connectionID { handle(error) }; return false }
    }
    func setRole(role: String, route: String) async {
        guard let api = requireConnection() else { return }
        let id = connectionID
        do {
            let _: OK = try await api.post("api/settings/role", body: ["role": role, "route": route])
            if id == connectionID { await loadSettings() }
        } catch { if id == connectionID { handle(error) } }
    }
}
