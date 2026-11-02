import SwiftUI

private let ink = Color(red: 0.11, green: 0.12, blue: 0.14)
private let muted = Color(red: 0.44, green: 0.47, blue: 0.53)
private let accent = Color(red: 0.24, green: 0.36, blue: 0.85)
private let hairline = Color.black.opacity(0.07)

struct ProviderHealthRow: Decodable, Identifiable {
    let provider: String
    let at: Double
    let ok: Bool
    let auth: String
    let latencyMS: Int
    let detail: String
    var id: String { provider }
    enum CodingKeys: String, CodingKey {
        case provider, at, ok, auth, detail
        case latencyMS = "latency_ms"
    }
}

struct Metrics: Decodable {
    struct Tasks: Decodable { let total: Int; let byStatus: [String: Int]
        enum CodingKeys: String, CodingKey { case total; case byStatus = "by_status" } }
    struct Tokens: Decodable { let input: Int; let output: Int; let cache: Int }
    struct Stage: Decodable, Identifiable {
        let provider: String; let stage: String; let samples: Int
        let p50: Double; let maxSeconds: Double
        var id: String { "\(provider)-\(stage)" }
        enum CodingKeys: String, CodingKey {
            case provider, stage, samples
            case p50 = "p50_seconds", maxSeconds = "max_seconds"
        }
    }
    struct Memory: Decodable { let notes: Int; let projects: Int }
    let tasks: Tasks
    let tokens: [String: Tokens]
    let stages: [Stage]
    let health: [ProviderHealthRow]
    let memory: Memory
}

@MainActor
final class DashboardModel: ObservableObject {
    @Published private(set) var metrics: Metrics?
    @Published private(set) var threads: [ChatThread] = []
    let project: String
    @Published private(set) var recentMemory: [MemoryNote] = []
    @Published var memoryShown = false
    @Published var error: String?
    private let api = APIClient()

    init(project: String) { self.project = project }

    func poll() async {
        while !Task.isCancelled {
            await load()
            do { try await Task.sleep(for: .seconds(3)) } catch { return }
        }
    }

    func loadThreads() async {
        struct ThreadList: Decodable { let conversations: [ChatThread] }
        if let list: ThreadList = try? await api.request("/chat/conversations", timeout: 20) {
            threads = list.conversations
        }
    }

    func load() async {
        do {
            let suffix = project.isEmpty ? "" : "?project=\(project)"
            metrics = try await api.request("/metrics\(suffix)", timeout: 20)
            let memorySuffix = project.isEmpty ? "" : "?limit=3&project=\(project)"
            if let list: MemoryList = try? await api.request("/memory\(memorySuffix)", timeout: 20) {
                recentMemory = list.notes
            }
            error = nil
        } catch {
            if metrics == nil { self.error = "看板读取失败：\(error.localizedDescription)" }
        }
    }
}

struct DashboardView: View {
    @StateObject private var model: DashboardModel
    @Environment(\.dismiss) private var dismiss

    init(project: String) {
        _model = StateObject(wrappedValue: DashboardModel(project: project))
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if let metrics = model.metrics {
                        health(metrics)
                        tokens(metrics)
                        dispatch(metrics)
                        stages(metrics)
                        memoryCard(metrics)
                    } else if let error = model.error {
                        Text(error).font(.system(size: 13)).foregroundStyle(.orange)
                    } else {
                        ProgressView().padding(.top, 60)
                    }
                }
                .padding(18)
            }
            .background(Color(red: 0.98, green: 0.985, blue: 0.99))
            .navigationTitle("运行看板")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } }
            }
            .task {
                await model.loadThreads()
                await model.poll()
            }
        }
    }

    private func card<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title).font(.system(size: 11, weight: .semibold)).tracking(0.8)
                .foregroundStyle(muted)
            content()
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.white, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(hairline))
    }

    private func health(_ metrics: Metrics) -> some View {
        card("模型健康 · 每 60 秒探活") {
            ForEach(metrics.health) { row in
                HStack(spacing: 8) {
                    Circle().fill(row.ok ? Color.green : Color.orange).frame(width: 7, height: 7)
                    Text(row.provider).font(.system(size: 13, weight: .medium))
                    Text(row.auth).font(.system(size: 10, design: .monospaced)).foregroundStyle(muted)
                    Spacer()

                }
            }
        }
    }

    private func tokens(_ metrics: Metrics) -> some View {
        card("Token 消耗") {
            ForEach(metrics.tokens.keys.sorted(), id: \.self) { provider in
                if let bucket = metrics.tokens[provider], bucket.input + bucket.output > 0 {
                    HStack(spacing: 8) {
                        Text(provider).font(.system(size: 13, weight: .medium))
                        Spacer()
                        Text("↓\(bucket.input)").font(.system(size: 10, design: .monospaced))
                        Text("↑\(bucket.output)").font(.system(size: 10, design: .monospaced))
                        if bucket.cache > 0 {
                            Text("⚡\(bucket.cache)").font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(accent)
                        }
                    }
                    .foregroundStyle(ink)
                }
            }
            Text("记忆库：\(metrics.memory.notes) 条笔记 · \(metrics.memory.projects) 个项目")
                .font(.system(size: 10)).foregroundStyle(muted)
        }
    }

    private func dispatch(_ metrics: Metrics) -> some View {
        card("调度情况") {
            HStack(spacing: 14) {
                ForEach(metrics.tasks.byStatus.keys.sorted(), id: \.self) { status in
                    VStack(alignment: .leading, spacing: 2) {
                        Text("\(metrics.tasks.byStatus[status] ?? 0)")
                            .font(.system(size: 20, weight: .semibold, design: .rounded))
                        Text(statusName(status)).font(.system(size: 10)).foregroundStyle(muted)
                    }
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 2) {
                    Text("\(metrics.tasks.total)")
                        .font(.system(size: 20, weight: .semibold, design: .rounded))
                    Text("总计").font(.system(size: 10)).foregroundStyle(muted)
                }
            }
        }
    }

    private func stages(_ metrics: Metrics) -> some View {
        card("阶段耗时（P50）") {
            ForEach(metrics.stages) { row in
                HStack(spacing: 8) {
                    Text(roleName(row.stage)).font(.system(size: 12))
                    Text(row.provider).font(.system(size: 10, design: .monospaced)).foregroundStyle(muted)
                    Spacer()
                    Text(row.p50 > 0 ? String(format: "%.1fs", row.p50) : "—")
                        .font(.system(size: 10, design: .monospaced))
                    Text("×\(row.samples)").font(.system(size: 10)).foregroundStyle(muted)
                }
            }
        }
    }

    private func memoryCard(_ metrics: Metrics) -> some View {
        Button { model.memoryShown = true } label: {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text("项目记忆").font(.system(size: 11, weight: .semibold))
                        .tracking(0.8).foregroundStyle(muted)
                    Spacer()
                    Text("\(metrics.memory.notes) 条").font(.system(size: 10))
                        .foregroundStyle(muted)
                }
                if model.recentMemory.isEmpty {
                    Text("还没有积累记忆；完成任务后会自动写入。")
                        .font(.system(size: 11)).foregroundStyle(muted)
                } else {
                    ForEach(model.recentMemory) { note in
                        Text(note.title).font(.system(size: 12)).lineLimit(1)
                            .foregroundStyle(ink)
                    }
                }
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.white, in: RoundedRectangle(cornerRadius: 14))
            .overlay(RoundedRectangle(cornerRadius: 14).stroke(hairline))
        }
        .buttonStyle(.plain)
        .sheet(isPresented: $model.memoryShown) { MemoryView(project: model.project) }
    }

    private func statusName(_ status: String) -> String {
        ["completed": "已完成", "failed": "失败", "running": "执行中", "queued": "排队",
         "paused": "暂停", "cancelled": "取消", "auth_required": "需登录",
         "policy_blocked": "策略阻止", "rate_limited": "限流",
         "quota_exhausted": "额度不足"][status] ?? status
    }

    private func roleName(_ role: String) -> String {
        ["plan": "规划", "implement": "实现", "review": "审查", "test": "测试",
         "summarize": "汇总"][role] ?? role
    }
}

struct MemoryNote: Decodable, Identifiable {
    let kind: String
    let title: String
    let body: String
    let provider: String
    let model: String
    let at: Double
    var id: String { "\(at)-\(title)" }
}

private struct MemoryList: Decodable { let notes: [MemoryNote] }

@MainActor
final class MemoryModel: ObservableObject {
    @Published private(set) var notes: [MemoryNote] = []
    @Published var error: String?
    let project: String
    private let api = APIClient()

    init(project: String) { self.project = project }

    func load() async {
        let suffix = project.isEmpty ? "" : "?limit=100&project=\(project)"
        do {
            let list: MemoryList = try await api.request("/memory\(suffix)", timeout: 20)
            notes = list.notes
        } catch { self.error = error.localizedDescription }
    }
}

struct MemoryView: View {
    @StateObject private var model: MemoryModel
    @Environment(\.dismiss) private var dismiss

    init(project: String) {
        _model = StateObject(wrappedValue: MemoryModel(project: project))
    }

    var body: some View {
        NavigationStack {
            List {
                if let error = model.error {
                    Text(error).font(.system(size: 12)).foregroundStyle(.orange)
                }
                ForEach(model.notes) { note in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack(spacing: 6) {
                            Text(note.title).font(.system(size: 13, weight: .medium)).lineLimit(1)
                            Spacer()
                            if !note.provider.isEmpty {
                                Text(note.provider).font(.system(size: 10, design: .monospaced))
                                    .foregroundStyle(muted)
                            }
                        }
                        if !note.body.isEmpty {
                            Text(note.body).font(.system(size: 11)).foregroundStyle(muted)
                                .lineLimit(6)
                        }
                    }
                }
            }
            .navigationTitle("项目记忆")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } }
            }
            .task { await model.load() }
        }
    }
}

private struct ContextSaved: Decodable { let id: String; let bytes: Int }

struct ProjectContextView: View {
    let conversation: String
    let thread: ChatThread?
    @Environment(\.dismiss) private var dismiss
    @State private var kilobytes: Double = 200
    @State private var paths = ""
    @State private var status = ""
    private let api = APIClient()

    var body: some View {
        NavigationStack {
            Form {
                Section("注入上限") {
                    Slider(value: $kilobytes, in: 8...4096, step: 8)
                    HStack {
                        Text("8 KB").font(.system(size: 11)).foregroundStyle(muted)
                        Spacer()
                        Text("4 MB").font(.system(size: 11)).foregroundStyle(muted)
                    }
                    LabeledContent("当前", value: sizeText)
                    Text("只影响这个项目；审查类默认 200 KB，实现类默认 60 KB，最大 4 MB。")
                        .font(.system(size: 11)).foregroundStyle(muted)
                }
                Section("限定路径（可选）") {
                    TextField("engine/, contracts/", text: $paths)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    Text("逗号分隔，最多 8 个前缀；留空表示整个仓库。")
                        .font(.system(size: 11)).foregroundStyle(muted)
                }
                if !status.isEmpty {
                    Text(status).font(.system(size: 12)).foregroundStyle(accent)
                }
            }
            .navigationTitle("任务上下文")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") { Task { await save() } }
                }
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }
                }
            }
            .onAppear {
                if let thread, thread.contextBytes > 0 {
                    kilobytes = Double(thread.contextBytes) / 1024
                    paths = thread.contextPaths
                }
            }
        }
    }

    private var sizeText: String {
        let bytes = Int(kilobytes * 1024)
        return bytes >= 1_048_576
            ? String(format: "%.1f MB", Double(bytes) / 1_048_576)
            : "\(bytes / 1024) KB"
    }

    private func save() async {
        struct Body: Encodable { let bytes: Int; let paths: [String] }
        let cleaned = paths.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        do {
            _ = try await api.request(
                "/chat/conversations/\(conversation)/context", method: "POST",
                body: JSONEncoder().encode(Body(bytes: Int(kilobytes * 1024), paths: cleaned)),
                timeout: 20) as ContextSaved
            status = "已保存"
            dismiss()
        } catch { status = "保存失败：\(error.localizedDescription)" }
    }
}
