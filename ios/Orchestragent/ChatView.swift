import SwiftUI

private let ink = Color(red: 0.11, green: 0.12, blue: 0.14)
private let muted = Color(red: 0.44, green: 0.47, blue: 0.53)
private let accent = Color(red: 0.24, green: 0.36, blue: 0.85)
private let paper = Color(red: 0.99, green: 0.99, blue: 1.0)
private let hairline = Color.black.opacity(0.07)

struct ChatMessage: Decodable, Identifiable, Equatable {
    let seq: Int
    let role: String
    let content: String
    let status: String
    let taskID: String?
    var usage: [String: JSONValue]? = nil
    var id: Int { seq }
    enum CodingKeys: String, CodingKey {
        case seq, role, content, status, usage
        case taskID = "task_id"
    }
}

struct ChatThread: Decodable, Identifiable, Equatable {
    let id: String
    let title: String
    let count: Int
    var contextBytes: Int = 0
    var contextPaths: String = ""

    enum CodingKeys: String, CodingKey {
        case id, title, count
        case contextBytes = "context_bytes"
        case contextPaths = "context_paths"
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(String.self, forKey: .id)
        title = try values.decode(String.self, forKey: .title)
        count = try values.decodeIfPresent(Int.self, forKey: .count) ?? 0
        contextBytes = try values.decodeIfPresent(Int.self, forKey: .contextBytes) ?? 0
        contextPaths = try values.decodeIfPresent(String.self, forKey: .contextPaths) ?? ""
    }
}

private struct ThreadList: Decodable { let conversations: [ChatThread] }
private struct Renamed: Decodable { let id: String; let title: String }

private struct ChatPage: Decodable {
    let messages: [ChatMessage]
    let cursor: Int?
}

private struct TaskPage: Decodable { let tasks: [DispatchTask] }

struct ChatSubmission: Codable, Equatable {
    let requestID: String
    let message: String
    let conversation: String
    enum CodingKeys: String, CodingKey {
        case message, conversation
        case requestID = "request_id"
    }
}

extension Int { var isLocal: Bool { self < 0 } }

@MainActor
final class ChatModel: ObservableObject {
    @Published private(set) var messages: [ChatMessage] = []
    @Published private(set) var tasks: [String: DispatchTask] = [:]
    @Published var draft = ""
    @Published private(set) var editingMessage: Int?
    @Published private var pendingSends: [String: ChatSubmission] = [:]
    private var sending: Set<String> = []
    private var drafts: [String: String] = [:]
    var pendingSend: ChatSubmission? { pendingSends[conversation] }
    @Published private(set) var busy = false
    @Published var error: String?
    @Published var deletionError: String?
    @Published private(set) var threads: [ChatThread] = []
    @Published private(set) var conversation: String
    private let api: APIClient
    private let defaults: UserDefaults
    private var cursor = 0
    private var refreshing = false

    init(api: APIClient = APIClient(), defaults: UserDefaults = .standard) {
        self.api = api
        self.defaults = defaults
        conversation = defaults.string(forKey: "chatThread") ?? "default"
        if let saved = defaults.data(forKey: "chatPendingSends"),
           let restored = try? JSONDecoder().decode([String: ChatSubmission].self, from: saved) {
            pendingSends = restored
        }
    }

    var conversationTitle: String {
        threads.first { $0.id == conversation }?.title ?? "默认对话"
    }

    func loadThreads() async {
        if let list: ThreadList = try? await api.request("/chat/conversations", timeout: 20) {
            if threads != list.conversations { threads = list.conversations }
        }
    }

    func switchTo(_ id: String) async {
        guard id != conversation else { return }
        drafts[conversation] = draft
        conversation = id
        draft = drafts[id] ?? ""
        editingMessage = nil
        defaults.set(id, forKey: "chatThread")
        messages = []
        tasks = [:]
        busy = false
        error = nil
        cursor = 0
        await refresh()
    }

    func renameCurrent(_ title: String) async {
        struct Body: Encodable { let title: String }
        _ = try? await api.request(
            "/chat/conversations/\(conversation)/rename", method: "POST",
            body: JSONEncoder().encode(Body(title: title)), timeout: 20) as Renamed
        await loadThreads()
    }

    func deleteThread(_ id: String) async {
        struct Empty: Decodable { let deleted: String }
        deletionError = nil
        do {
            _ = try await api.request(
                "/chat/conversations/\(id)", method: "DELETE", timeout: 20) as Empty
            pendingSends.removeValue(forKey: id)
            savePending()
            threads.removeAll { $0.id == id }
            if id == conversation, let first = threads.first {
                await switchTo(first.id)
            }
            await loadThreads()
        } catch {
            deletionError = "删除失败：\(error.localizedDescription)"
        }
    }

    func createThread(_ title: String) async {
        struct Body: Encodable { let title: String }
        if let created: ChatThread = try? await api.request(
            "/chat/conversations", method: "POST",
            body: JSONEncoder().encode(Body(title: title)), timeout: 20) {
            await loadThreads()
            await switchTo(created.id)
        }
    }

    func poll() async {
        while !Task.isCancelled {
            await refresh()
            do { try await Task.sleep(for: .seconds(1)) } catch { return }
        }
    }

    func refresh() async {
        guard !refreshing else { return }
        refreshing = true
        defer { refreshing = false }
        let project = conversation
        do {
            let page: ChatPage = try await api.request(
                "/chat?conversation=\(project)&after=\(cursor)")
            guard project == conversation, !Task.isCancelled else { return }
            if !page.messages.isEmpty {
                let known = Set(messages.map(\.seq))
                let updated = messages.filter { !$0.seq.isLocal }
                    + page.messages.filter { !known.contains($0.seq) }
                if updated != messages { messages = updated }
                cursor = page.cursor ?? cursor
            }
            let listing: TaskPage = try await api.request("/tasks?project=\(project)")
            guard project == conversation, !Task.isCancelled else { return }
            let updated = Dictionary(uniqueKeysWithValues: listing.tasks.map { ($0.id, $0) })
            if tasks != updated { tasks = updated }
            let waiting = sending.contains(project) || (messages.last.map {
                $0.role == "user" && ["pending", "running", "local"].contains($0.status)
            } ?? false)
            if busy != waiting { busy = waiting }
        } catch {
            if project == conversation, messages.isEmpty {
                self.error = "连不上服务器：\(error.localizedDescription)"
            }
        }
    }

    private func savePending() {
        defaults.set(try? JSONEncoder().encode(pendingSends), forKey: "chatPendingSends")
    }

    func edit(_ message: ChatMessage) {
        guard message.role == "user", pendingSend == nil else { return }
        draft = message.content
        editingMessage = message.seq
    }

    func cancelEditing() { editingMessage = nil }

    func retrySource(for message: ChatMessage) -> ChatMessage? {
        guard message.taskID == nil else { return nil }
        if message.role == "user" { return message }
        return messages.last { $0.role == "user" && $0.seq < message.seq }
    }

    func retry(_ message: ChatMessage) async {
        guard !busy, let source = retrySource(for: message) else { return }
        if let pending = pendingSend {
            await transmit(pending)
        } else {
            await transmit(ChatSubmission(requestID: UUID().uuidString,
                message: source.content, conversation: conversation))
        }
    }

    func retryPending() async {
        guard let pending = pendingSend else { return }
        await transmit(pending)
    }

    func send() async {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !busy else { return }
        guard pendingSend == nil else {
            error = "上一条消息的发送结果尚未确认，请先点“确认发送结果”。"
            return
        }
        await transmit(ChatSubmission(requestID: UUID().uuidString,
            message: text, conversation: conversation))
    }

    private func transmit(_ submission: ChatSubmission) async {
        let project = submission.conversation
        guard !sending.contains(project) else { return }
        guard let body = try? JSONEncoder().encode(submission), body.count <= 16 * 1024 else {
            error = APIClient.Failure.tooLarge.localizedDescription
            return
        }
        sending.insert(project)
        defer {
            sending.remove(project)
            if project == conversation, messages.last?.role == "assistant" { busy = false }
        }
        pendingSends[project] = submission
        savePending()
        if project == conversation {
            busy = true
            error = nil
            if draft == submission.message { draft = "" }
            editingMessage = nil
            messages.removeAll { $0.seq.isLocal }
            messages.append(ChatMessage(seq: -1, role: "user", content: submission.message,
                                        status: "local", taskID: nil))
        }
        do {
            let page: ChatPage = try await api.request("/chat", method: "POST", body: body)
            pendingSends.removeValue(forKey: project)
            savePending()
            guard project == conversation else { return }
            // POST is idempotent and returns the authoritative conversation, including
            // a completed reply if this is recovery from a lost acknowledgement.
            messages = page.messages
            cursor = page.messages.last?.seq ?? cursor
            busy = page.messages.last?.role == "user"
            await refresh()
        } catch {
            // A definite rejection was not accepted; transport loss is ambiguous and
            // keeps the original UUID for a safe retry, including after app restart.
            if let failure = error as? APIClient.Failure,
               case .server(let code, _) = failure, (400..<500).contains(code), code != 408 {
                pendingSends.removeValue(forKey: project)
                savePending()
            }
            guard project == conversation else { return }
            busy = false
            messages.removeAll { $0.seq.isLocal }
            messages.append(ChatMessage(seq: -1, role: "user", content: submission.message,
                                        status: "failed", taskID: nil))
            if pendingSend == nil { draft = submission.message }
            self.error = "发送失败：\(error.localizedDescription)"
        }
    }

    func reset() async {
        guard let body = "{}".data(using: .utf8) else { return }
        _ = try? await api.request("/chat/reset", method: "POST", body: body) as ChatPage
        messages = []
        cursor = 0
    }

    func accept(_ task: DispatchTask, approved: Bool) async {
        struct Body: Encodable { let approved: Bool; let note: String }
        do {
            let _: DispatchTask = try await api.request(
                "/tasks/\(task.id)/accept", method: "POST",
                body: JSONEncoder().encode(Body(approved: approved, note: "")), timeout: 60)
            await refresh()
        } catch { self.error = "验收提交失败：\(error.localizedDescription)" }
    }

    func manualReview(_ task: DispatchTask) async {
        struct Body: Encodable { let auto: Bool; let model: String }
        do {
            let _: DispatchTask = try await api.request(
                "/tasks/\(task.id)/manual", method: "POST",
                body: JSONEncoder().encode(Body(auto: false, model: "")), timeout: 240)
            await refresh()
        } catch { self.error = "Kimi 复核失败：\(error.localizedDescription)" }
    }

    var activeTask: DispatchTask? {
        tasks.values
            .filter { ["running", "queued", "waiting_manual", "waiting_acceptance"].contains($0.status) }
            .max { $0.updatedAt < $1.updatedAt }
    }

    func togglePause() async {
        guard let task = activeTask else { return }
        await act(task, action: task.status == "paused" ? "resume" : "pause")
    }

    func act(_ task: DispatchTask, action: String) async {
        struct Body: Encodable { let action: String }
        do {
            let _: DispatchTask = try await api.request("/tasks/\(task.id)/actions", method: "POST",
                                                         body: JSONEncoder().encode(Body(action: action)))
            await refresh()
        } catch { self.error = error.localizedDescription }
    }
}

struct ChatView: View {
    @StateObject private var model = ChatModel()
    @State private var historyShown = false
    @State private var dashboardShown = false
    @State private var kimiShown = false
    @State private var detailTask: DispatchTask?
    @State private var renameShown = false
    @State private var renameDraft = ""
    @State private var sidebarShown = false
    @State private var contextShown = false
    @State private var pendingDelete: ChatThread?
    @State private var settingsShown = false
    @State private var copiedMessage: Int?
    @State private var nearBottom = true
    @FocusState private var typing: Bool

    var body: some View {
        ZStack(alignment: .leading) {
            content
            if sidebarShown {
                Color.black.opacity(0.25).ignoresSafeArea()
                    .onTapGesture { withAnimation(.snappy) { sidebarShown = false } }
                sidebar
                    .transition(.move(edge: .leading))
            }
        }
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("项目").font(.title3.weight(.semibold))
                Spacer()
                Button {
                    withAnimation(.snappy) { sidebarShown = false }
                } label: {
                    Image(systemName: "xmark").font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(muted)
                }
            }
            .padding(.horizontal, 18).padding(.top, 18).padding(.bottom, 10)

            ScrollView {
                VStack(spacing: 2) {
                    ForEach(model.threads) { thread in
                        Button {
                            Task { await model.switchTo(thread.id) }
                            withAnimation(.snappy) { sidebarShown = false }
                        } label: {
                            HStack(spacing: 9) {
                                Image(systemName: thread.id == model.conversation
                                      ? "bubble.left.fill" : "bubble.left")
                                    .font(.system(size: 12)).foregroundStyle(muted)
                                Text(thread.title).font(.system(size: 14))
                                    .foregroundStyle(thread.id == model.conversation ? ink : muted)
                                    .lineLimit(1)
                                Spacer()
                                if thread.id == model.conversation {
                                    Image(systemName: "checkmark")
                                        .font(.system(size: 10, weight: .semibold))
                                        .foregroundStyle(accent)
                                }
                            }
                            .padding(.horizontal, 14).padding(.vertical, 10)
                            .background(thread.id == model.conversation
                                        ? Color.black.opacity(0.045) : Color.clear,
                                        in: RoundedRectangle(cornerRadius: 9))
                        }
                        .buttonStyle(.plain)
                        .contextMenu {
                            Button(role: .destructive) {
                                pendingDelete = thread
                            } label: { Label("删除对话", systemImage: "trash") }
                        }
                    }
                }
                .padding(.horizontal, 8)
            }

            Divider()
            Button {
                Task { await model.createThread("项目 \(model.threads.count + 1)") }
                withAnimation(.snappy) { sidebarShown = false }
            } label: {
                Label("新建项目对话", systemImage: "plus.circle")
                    .font(.system(size: 14)).padding(.horizontal, 18).padding(.vertical, 14)
            }
            .buttonStyle(.plain)
            Button {
                renameDraft = model.conversationTitle
                renameShown = true
            } label: {
                Label("重命名当前对话", systemImage: "pencil")
                    .font(.system(size: 14)).padding(.horizontal, 18)
            }
            .buttonStyle(.plain)
            Button {
                sidebarShown = false
                contextShown = true
                Task { await model.loadThreads() }
            } label: {
                Label("任务上下文设置", systemImage: "text.magnifyingglass")
                    .font(.system(size: 14)).padding(.horizontal, 18).padding(.bottom, 18)
            }
            .buttonStyle(.plain)
        }
        .frame(width: 292)
        .frame(maxHeight: .infinity, alignment: .top)
        .background(paper.ignoresSafeArea(edges: [.vertical, .leading]))
        .shadow(color: .black.opacity(0.12), radius: 18, x: 6, y: 0)
    }

    private var content: some View {
        NavigationStack {
            ScrollViewReader { proxy in
                GeometryReader { viewport in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 26) {
                            if model.messages.isEmpty { greeting }
                            ForEach(model.messages) { turn($0) }
                            if model.busy { thinking }
                            Color.clear.frame(height: 1).id("chat-bottom")
                        }
                        .padding(.horizontal, 20)
                        .padding(.top, 12)
                        .padding(.bottom, 24)
                        .background(GeometryReader { content in
                            Color.clear.preference(key: ChatBottomPreference.self,
                                value: content.frame(in: .named("chat-scroll")).maxY)
                        })
                    }
                    .coordinateSpace(name: "chat-scroll")
                    .scrollDismissesKeyboard(.interactively)
                    .onPreferenceChange(ChatBottomPreference.self) { bottom in
                        nearBottom = bottom <= viewport.size.height + 80
                    }
                    .onChange(of: model.messages.count) { oldCount, _ in
                        if nearBottom || oldCount == 0 || model.messages.last?.seq.isLocal == true {
                            scroll(proxy)
                        }
                    }
                    .onChange(of: model.busy) { _, _ in
                        if nearBottom { scroll(proxy) }
                    }
                    .overlay(alignment: .bottomTrailing) {
                        if !nearBottom {
                            Button("回到最新", systemImage: "arrow.down") { scroll(proxy) }
                                .font(.caption).buttonStyle(.borderedProminent)
                                .padding(12)
                        }
                    }
                }
            }
            .safeAreaInset(edge: .bottom, spacing: 0) { composer }
            .background(
                paper.onTapGesture { typing = false }
            )
            .foregroundStyle(ink)

            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button {
                        withAnimation(.snappy) { sidebarShown = true }
                    } label: {
                        Image(systemName: "sidebar.left")
                            .font(.system(size: 16, weight: .regular))
                    }
                    .buttonStyle(.plain)
                }
                ToolbarItem(placement: .principal) {
                    VStack(spacing: 1) {
                        Text(model.conversationTitle).font(.subheadline.weight(.semibold)).lineLimit(1)
                        Text("多厂商后台执行")
                            .font(.system(size: 10)).foregroundStyle(muted)
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        Button("新建项目对话", systemImage: "square.and.pencil") {
                            Task { await model.createThread("项目 \(model.threads.count + 1)") }
                        }
                        Button("问 Kimi（手动单次）", systemImage: "sparkles") { kimiShown = true }
                        Button("运行看板", systemImage: "chart.bar.xaxis") { dashboardShown = true }
                        Button("历史与审批", systemImage: "clock.arrow.circlepath") { historyShown = true }
                        Button("调度设置", systemImage: "slider.horizontal.3") { settingsShown = true }
                    } label: {
                        Image(systemName: "ellipsis.circle").font(.body)
                    }
                }
            }
            .toolbarBackground(paper, for: .navigationBar)
            .toolbarBackground(.visible, for: .navigationBar)
            .sheet(isPresented: $historyShown) {
                NavigationStack { HistoryView(project: model.conversation) }
            }
            .sheet(isPresented: $dashboardShown) { DashboardView(project: model.conversation) }
            .sheet(isPresented: $kimiShown) { KimiAskView() }
            .sheet(isPresented: $settingsShown) { SettingsView() }
            .sheet(isPresented: $contextShown, onDismiss: {
                Task { await model.loadThreads() }
            }) {
                ProjectContextView(
                    conversation: model.conversation,
                    thread: model.threads.first { $0.id == model.conversation }
                )
            }
            .alert(
                "删除「\(pendingDelete?.title ?? "")」？",
                isPresented: Binding(
                    get: { pendingDelete != nil },
                    set: { if !$0 { pendingDelete = nil } }
                ),
                presenting: pendingDelete
            ) { thread in
                Button("删除", role: .destructive) {
                    Task { await model.deleteThread(thread.id) }
                }
                Button("取消", role: .cancel) {}
            } message: { _ in
                Text("对话记录会被删除，项目里的任务和代码不受影响。")
            }
            .alert("删除失败", isPresented: Binding(
                get: { model.deletionError != nil },
                set: { if !$0 { model.deletionError = nil } }
            )) {
                Button("好") { model.deletionError = nil }
            } message: {
                Text(model.deletionError ?? "")
            }
            .alert("重命名对话", isPresented: $renameShown) {
                TextField("项目名称", text: $renameDraft)
                Button("保存") { Task { await model.renameCurrent(renameDraft) } }
                Button("取消", role: .cancel) {}
            }
            .task {
                await model.loadThreads()
                await model.poll()
            }
        }
        .tint(accent)
    }

    private func scroll(_ proxy: ScrollViewProxy) {
        proxy.scrollTo("chat-bottom", anchor: .bottom)
    }

    private var greeting: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("今天要做什么？")
                .font(.system(size: 26, weight: .semibold, design: .rounded))
            Text("直接说需求。需要动手时，我会在后台派给 Codex、Claude、Cursor 或 DSH，进度会出现在这条对话里。任务卡可以点进去看完整记录；待验收时回复「同意」或「打回」即可。")
                .font(.system(size: 15)).foregroundStyle(muted)
                .fixedSize(horizontal: false, vertical: true)
            VStack(alignment: .leading, spacing: 8) {
                ForEach(["写一个把秒数格式化成 hh:mm:ss 的 Python 项目",
                         "给现有代码补一组边界测试",
                         "解释一下上次任务的实现思路"], id: \.self) { sample in
                    Button {
                        model.draft = sample
                        typing = true
                    } label: {
                        HStack(spacing: 8) {
                            Text(sample).font(.system(size: 13)).multilineTextAlignment(.leading)
                            Spacer(minLength: 4)
                            Image(systemName: "arrow.up.left").font(.system(size: 10))
                        }
                        .foregroundStyle(ink)
                        .padding(.horizontal, 12).padding(.vertical, 9)
                        .background(Color.white, in: RoundedRectangle(cornerRadius: 12))
                        .overlay(RoundedRectangle(cornerRadius: 12).stroke(hairline))
                    }
                }
            }
            .padding(.top, 14)
        }
        .padding(.top, 28)
    }

    private var thinking: some View {
        HStack(spacing: 9) {
            ProgressView().controlSize(.small)
            Text("正在回复…").font(.system(size: 13)).foregroundStyle(muted)
        }
    }

    @ViewBuilder
    private func turn(_ message: ChatMessage) -> some View {
        if message.role == "user" {
            HStack {
                Spacer(minLength: 56)
                VStack(alignment: .trailing, spacing: 6) {
                    SelectableText(text: message.content, size: 15, lineSpacing: 2)
                        .padding(.horizontal, 14).padding(.vertical, 10)
                        .background(ink.opacity(0.055), in: RoundedRectangle(cornerRadius: 18))
                    messageActions(message)
                }
            }
            .id(message.seq)
        } else {
            VStack(alignment: .leading, spacing: 8) {
                Text("调度 agent")
                    .font(.system(size: 10, weight: .semibold))
                    .tracking(0.8)
                    .foregroundStyle(muted)
                SelectableText(text: message.content, size: 15, lineSpacing: 3)
                    .fixedSize(horizontal: false, vertical: true)
                if case let .string(effort) = message.usage?["effort"], !effort.isEmpty {
                    Text("推理强度：\(effort)")
                        .font(.system(size: 10)).foregroundStyle(muted)
                }
                if message.status == "failed" {
                    Label("这一轮没有完成，可以重发一次", systemImage: "exclamationmark.triangle.fill")
                        .font(.system(size: 11))
                        .foregroundStyle(Color.orange)
                }
                messageActions(message)
                if let id = message.taskID, let task = model.tasks[id] { activity(task) }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .id(message.seq)
        }
    }

    private func messageActions(_ message: ChatMessage) -> some View {
        HStack(spacing: 18) {
            Button {
                UIPasteboard.general.string = message.content
                copiedMessage = message.seq
            } label: {
                Label(copiedMessage == message.seq ? "已复制" : "复制",
                      systemImage: copiedMessage == message.seq ? "checkmark" : "doc.on.doc")
            }
            .accessibilityLabel("复制消息")
            if message.role == "user" {
                Button("编辑", systemImage: "pencil") {
                    model.edit(message)
                    typing = true
                }
                .disabled(model.pendingSend != nil)
            }
            if model.retrySource(for: message) != nil {
                Button("重试", systemImage: "arrow.clockwise") {
                    Task { await model.retry(message) }
                }
                .disabled(model.busy)
            }
        }
        .font(.system(size: 11))
        .foregroundStyle(muted)
        .buttonStyle(.plain)
        .padding(.vertical, 5)
    }

    private func stepUsage(_ task: DispatchTask, _ step: DispatchStep) -> StepUsage? {
        guard let usage = task.usage else { return nil }
        let key = usage.keys.filter { $0.hasPrefix("\(step.role):") }.sorted().last
        guard let key, let raw = usage[key] else { return nil }
        return StepUsage(raw)
    }

    private func compact(_ value: Int) -> String {
        value >= 1000 ? String(format: "%.1fk", Double(value) / 1000) : "\(value)"
    }

    private func statusName(_ status: String) -> String {
        ["queued": "排队中", "running": "执行中", "completed": "已完成", "failed": "失败",
         "paused": "已暂停", "cancelled": "已取消", "rate_limited": "限流",
         "auth_required": "需要登录", "policy_blocked": "策略阻止",
         "quota_exhausted": "额度不足", "waiting_manual": "等你复核",
         "waiting_acceptance": "等你验收"][status] ?? status
    }

    private func statusColor(_ status: String) -> Color {
        switch status {
        case "completed": return .green
        case "failed", "auth_required", "policy_blocked", "quota_exhausted": return .orange
        case "running": return accent
        default: return .gray
        }
    }

    private func stageName(_ stage: String) -> String { roleName(stage) }

    private func roleName(_ role: String) -> String {
        ["plan": "规划", "implement": "实现", "review": "审查", "test": "测试",
         "summarize": "汇总"][role] ?? role
    }

    private func activity(_ task: DispatchTask) -> some View {
        Button { detailTask = task } label: {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 7) {
                Circle().fill(statusColor(task.status)).frame(width: 6, height: 6)
                Text("任务").font(.system(size: 12, weight: .semibold))
                Text(statusName(task.status)).font(.system(size: 11)).foregroundStyle(muted)
                if !task.isTerminal {
                    Text(roleName(task.stage)).font(.system(size: 11)).foregroundStyle(muted)
                }
                Spacer()
                if task.canPause {
                    Button("暂停") { Task { await model.act(task, action: "pause") } }
                        .font(.system(size: 11))
                } else if task.canRetry {
                    Button("重试这一阶段") {
                        Task { await model.act(task, action: "retry") }
                    }
                    .font(.system(size: 11))
                } else if task.status == "waiting_acceptance" {
                    HStack(spacing: 10) {
                        Button("打回重做") { Task { await model.accept(task, approved: false) } }
                            .font(.system(size: 11))
                        Button("接受交付") { Task { await model.accept(task, approved: true) } }
                            .font(.system(size: 11, weight: .semibold))
                    }
                } else if task.status == "waiting_manual" {
                    Button("用 Kimi 复核") { Task { await model.manualReview(task) } }
                        .font(.system(size: 11))
                } else if task.canResume {
                    Button("继续") { Task { await model.act(task, action: "resume") } }
                        .font(.system(size: 11))
                }
            }
            ForEach(task.steps.filter { $0.status != "pending" }) { step in
                HStack(alignment: .top, spacing: 9) {
                    Image(systemName: step.status == "completed" ? "checkmark.circle.fill"
                          : step.status == "running" ? "circle.dotted" : "exclamationmark.circle")
                        .font(.system(size: 11))
                        .foregroundStyle(step.status == "completed" ? Color.green : muted)
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(spacing: 6) {
                            Text(roleName(step.role)).font(.system(size: 12, weight: .medium))
                            if let provider = step.provider, !provider.isEmpty {
                                Text(provider).font(.system(size: 10, design: .monospaced))
                                    .foregroundStyle(muted)
                            }
                            if let model = step.model, !model.isEmpty {
                                Text(model).font(.system(size: 10, design: .monospaced))
                                    .foregroundStyle(muted)
                            }
                        }
                        if !step.summary.isEmpty {
                            Text(step.summary).font(.system(size: 11)).foregroundStyle(muted)
                                .lineLimit(3)
                        }
                        if let usage = stepUsage(task, step) {
                            HStack(spacing: 9) {
                                Label(compact(usage.input), systemImage: "arrow.down")
                                Label(compact(usage.output), systemImage: "arrow.up")
                                if usage.cacheRead > 0 {
                                    Label("\(compact(usage.cacheRead)) \(usage.hitRate.map { String(format: "(%.0f%%)", $0 * 100) } ?? "")",
                                          systemImage: "bolt.fill")
                                        .foregroundStyle(accent)
                                }
                                if let effort = usage.effort, !effort.isEmpty {
                                    Text(effort).foregroundStyle(accent)
                                }
                                if let estimate = usage.estimateUSD {
                                    Text(String(format: "$%.3f", estimate))
                                } else if usage.billing == "api" {
                                    Text("API 计费")
                                }
                            }
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(muted)
                        }
                    }
                }
            }
        }
        .padding(13)
        .background(Color.white, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(hairline))
        }
        .buttonStyle(.plain)
        .sheet(item: $detailTask) { TaskRecordView(task: $0) }
    }

    private var composer: some View {
        VStack(spacing: 0) {
            Rectangle().fill(hairline).frame(height: 0.5)
            if let active = model.activeTask {
                HStack(spacing: 7) {
                    Circle()
                        .fill(active.status == "paused" ? Color.orange : Color.green)
                        .frame(width: 6, height: 6)
                    Text("\(stageName(active.stage)) · \(statusName(active.status))")
                        .font(.system(size: 11)).foregroundStyle(muted)
                    Spacer()
                    Button(active.status == "paused" ? "继续" : "暂停") {
                        Task { await model.togglePause() }
                    }
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(ink)
                    .padding(.horizontal, 10).padding(.vertical, 4)
                    .background(Color.black.opacity(0.05), in: Capsule())
                }
                .padding(.horizontal, 18).padding(.top, 8)
            }
            if model.editingMessage != nil {
                HStack {
                    Text("编辑后作为新消息发送，原记录保留").font(.caption)
                    Spacer()
                    Button("取消编辑") { model.cancelEditing() }
                }
                .foregroundStyle(muted).padding(.horizontal, 18).padding(.top, 8)
            }
            if model.pendingSend != nil {
                Button("确认发送结果", systemImage: "arrow.clockwise") {
                    Task { await model.retryPending() }
                }
                .font(.caption).padding(.top, 8)
            }
            HStack(alignment: .bottom, spacing: 9) {
                TextField(model.activeTask == nil ? "给调度 agent 发消息…" : "插话，或发新需求…",
                          text: $model.draft, axis: .vertical)
                    .font(.system(size: 15))
                    .lineLimit(1...6)
                    .focused($typing)
                    .padding(.horizontal, 15).padding(.vertical, 11)
                    .background(Color.white, in: RoundedRectangle(cornerRadius: 20))
                    .overlay(RoundedRectangle(cornerRadius: 20)
                        .stroke(Color.black.opacity(0.08), lineWidth: 1))
                Button {
                    typing = false
                    Task { await model.send() }
                } label: {
                    Image(systemName: "arrow.up")
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(.white)
                        .frame(width: 34, height: 34)
                        .background(model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                                    ? Color.black.opacity(0.12) : accent, in: Circle())
                }
                .disabled(model.busy || model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            .padding(.horizontal, 16)
            .padding(.top, 10)
            .padding(.bottom, 6)
            if let error = model.error {
                Text(error).font(.system(size: 11)).foregroundStyle(Color.orange)
                    .padding(.horizontal, 20).padding(.bottom, 8)
            }
        }
        .background(paper)
    }

}

/// A native selectable text view: long-press gives the standard selection handles
/// and copy menu, which SwiftUI's Text does not provide for plain messages.
struct SelectableText: UIViewRepresentable {
    let text: String
    var size: CGFloat = 15
    var lineSpacing: CGFloat = 3

    final class Coordinator {
        var text: String?
        var size: CGFloat = 0
        var spacing: CGFloat = 0
        var measured: (width: CGFloat, size: CGSize)?
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> UITextView {
        let view = UITextView()
        view.isEditable = false
        view.isSelectable = true
        view.isScrollEnabled = false
        view.backgroundColor = .clear
        view.textContainerInset = .zero
        view.textContainer.lineFragmentPadding = 0
        view.adjustsFontForContentSizeCategory = true
        view.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return view
    }

    func updateUIView(_ view: UITextView, context: Context) {
        let cache = context.coordinator
        guard cache.text != text || cache.size != size || cache.spacing != lineSpacing else { return }
        cache.text = text
        cache.size = size
        cache.spacing = lineSpacing
        cache.measured = nil
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineSpacing = lineSpacing
        let attributedText = NSAttributedString(
            string: text,
            attributes: [
                .font: UIFont.systemFont(ofSize: size),
                .foregroundColor: UIColor.label,
                .paragraphStyle: paragraph,
            ]
        )
        guard view.attributedText != attributedText else { return }
        let selection = view.text == text ? view.selectedRange : nil
        view.attributedText = attributedText
        if let selection { view.selectedRange = selection }
    }

    func sizeThatFits(
        _ proposal: ProposedViewSize, uiView: UITextView, context: Context
    ) -> CGSize? {
        guard let width = proposal.width, width > 0, width.isFinite else { return nil }
        if let cached = context.coordinator.measured, cached.width == width { return cached.size }
        let height = uiView.sizeThatFits(
            CGSize(width: width, height: .greatestFiniteMagnitude)
        ).height
        let measured = CGSize(width: width, height: ceil(height))
        context.coordinator.measured = (width, measured)
        return measured
    }
}

private struct ChatBottomPreference: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
}
