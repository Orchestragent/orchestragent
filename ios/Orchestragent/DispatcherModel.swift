import Foundation
import SwiftUI

@MainActor
final class DispatcherModel: ObservableObject {
    @Published var tasks: [DispatchTask] = []
    @Published var selectedID: String?
    @Published var events: [TaskEvent] = []
    @Published var draft: String = "" { didSet { defaults.set(draft, forKey: "draft") } }
    @Published var scenario = "happy"
    @Published var selectedMode = "mock"
    @Published private(set) var serverMode = "mock"
    @Published private(set) var pending: Submission?
    @Published private(set) var connected = false
    @Published private(set) var lastSynced: Date?
    @Published private(set) var busy = false
    @Published var error: String?
    @Published private(set) var connectionError: String?
    private let api: APIClient
    private let defaults: UserDefaults
    private var cursor = 0
    private var hasLoadedHealth = false
    var selectedTask: DispatchTask? { tasks.first { $0.id == selectedID } }
    var submissionMode: String { pending?.mode ?? (scenario == "happy" ? selectedMode : "mock") }
    var displayMode: String { selectedTask?.executionMode ?? serverMode }

    init(api: APIClient = APIClient(), defaults: UserDefaults = .standard) {
        self.api = api
        self.defaults = defaults
        draft = defaults.string(forKey: "draft") ?? ""
        if let saved = defaults.data(forKey: "pendingSubmission") {
            pending = try? JSONDecoder().decode(Submission.self, from: saved)
        }
    }

    func select(_ id: String?) {
        guard selectedID != id else { return }
        selectedID = id
        cursor = 0
        events = []
    }

    func chooseMode(_ mode: String) {
        selectedMode = mode
        hasLoadedHealth = true
    }

    func poll() async {
        while !Task.isCancelled {
            await refresh()
            do { try await Task.sleep(for: .seconds(connected ? 1 : 3)) }
            catch { return }
        }
    }

    func refresh() async {
        do {
            let health: ServerHealth = try await api.request("/health")
            serverMode = health.mode
            if !hasLoadedHealth {
                selectedMode = health.mode
                hasLoadedHealth = true
            }
            let received = try await api.tasks()
            guard !Task.isCancelled else { return }
            // A poll started before a mutation must never overwrite its newer response.
            let receivedIDs = Set(received.map(\.id))
            let retained = tasks.filter { !receivedIDs.contains($0.id) }
            tasks = (received.map { remote in
                if let local = tasks.first(where: { $0.id == remote.id }), local.updatedAt > remote.updatedAt { return local }
                return remote
            } + retained).sorted { $0.createdAt > $1.createdAt }
            if let pending, let found = tasks.first(where: { $0.requestID == pending.requestID }) {
                finishSubmission(found)
            }
            if selectedID == nil, let first = tasks.first { select(first.id) }
            if let selectedID {
                let page = try await api.events(taskID: selectedID, after: cursor)
                guard self.selectedID == selectedID, !Task.isCancelled else { return }
                let known = Set(events.map(\.seq))
                events.append(contentsOf: page.events.filter { !known.contains($0.seq) })
                cursor = max(cursor, page.cursor)
            }
            connected = true
            connectionError = nil
            lastSynced = Date()
        } catch {
            guard !Task.isCancelled else { return }
            connected = false
            connectionError = "连接中断，正在重连。\(error.localizedDescription)"
        }
    }

    func submit() async {
        guard !busy else { return }
        error = nil
        if pending == nil {
            let prompt = draft.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !prompt.isEmpty else { return }
            let submission = Submission(prompt: prompt, scenario: scenario, mode: submissionMode)
            do {
                let data = try JSONEncoder().encode(submission)
                guard data.count <= 16 * 1024 else { throw APIClient.Failure.tooLarge }
                // Save the exact payload and id before sending; retries retain both.
                defaults.set(data, forKey: "pendingSubmission")
                pending = submission
            } catch { self.error = error.localizedDescription; return }
        }
        guard let pending else { return }
        busy = true
        defer { busy = false }
        do { finishSubmission(try await api.submit(pending)) }
        catch {
            if self.pending?.requestID == pending.requestID {
                self.error = "提交尚未确认。重试会复用同一个请求编号。\(error.localizedDescription)"
            }
        }
    }

    private func finishSubmission(_ task: DispatchTask) {
        update(task)
        select(task.id)
        if draft.trimmingCharacters(in: .whitespacesAndNewlines) == pending?.prompt { draft = "" }
        pending = nil
        defaults.removeObject(forKey: "pendingSubmission")
        error = nil
    }

    private func update(_ task: DispatchTask) {
        if let index = tasks.firstIndex(where: { $0.id == task.id }) {
            if task.updatedAt >= tasks[index].updatedAt { tasks[index] = task }
        }
        else { tasks.insert(task, at: 0) }
    }

    func discardPending() {
        guard !busy else { return }
        pending = nil
        defaults.removeObject(forKey: "pendingSubmission")
        error = nil
    }

    func act(_ action: String) async {
        guard !busy, let id = selectedID else { return }
        busy = true
        error = nil
        defer { busy = false }
        do { update(try await api.action(action, taskID: id)) }
        catch { self.error = "操作未确认，请同步状态后重试。\(error.localizedDescription)" }
    }

    func decide(_ decision: String, taskID: String, approval: Approval) async {
        guard !busy else { return }
        busy = true
        error = nil
        defer { busy = false }
        do { update(try await api.decide(decision, taskID: taskID, approval: approval)) }
        catch { self.error = "审批未确认，请同步后检查审批内容。\(error.localizedDescription)" }
    }
}
