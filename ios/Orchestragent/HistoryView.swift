import SwiftUI

private let ink = Color(red: 0.11, green: 0.12, blue: 0.14)
private let muted = Color(red: 0.44, green: 0.47, blue: 0.53)
private let accent = Color(red: 0.24, green: 0.36, blue: 0.85)
private let paper = Color(red: 0.99, green: 0.99, blue: 1.0)
private let hairline = Color.black.opacity(0.07)

@MainActor
final class HistoryModel: ObservableObject {
    @Published private(set) var tasks: [DispatchTask] = []
    @Published var error: String?
    let project: String
    private let api = APIClient()

    init(project: String = "") { self.project = project }

    func load() async {
        do {
            let suffix = project.isEmpty ? "" : "?project=\(project)"
            let page: Page = try await api.request("/tasks\(suffix)")
            tasks = page.tasks.filter { $0.executionMode == "live" }
                .sorted { $0.createdAt > $1.createdAt }
            error = nil
        } catch { self.error = "读取失败：\(error.localizedDescription)" }
    }

    func act(_ task: DispatchTask, _ action: String) async {
        struct Body: Encodable { let action: String }
        do {
            let _: DispatchTask = try await api.request("/tasks/\(task.id)/actions", method: "POST",
                                                         body: JSONEncoder().encode(Body(action: action)))
            await load()
        } catch { self.error = error.localizedDescription }
    }

    func decide(_ task: DispatchTask, _ approval: Approval, approve: Bool) async {
        struct Body: Encodable {
            let decision: String
            let digest: String
            enum CodingKeys: String, CodingKey { case decision; case digest = "operation_digest" }
        }
        do {
            let _: DispatchTask = try await api.request(
                "/tasks/\(task.id)/approvals/\(approval.id)", method: "POST",
                body: JSONEncoder().encode(Body(decision: approve ? "approve" : "deny",
                                                 digest: approval.operationDigest)))
            await load()
        } catch { self.error = error.localizedDescription }
    }

    struct Page: Decodable { let tasks: [DispatchTask] }
}

private func statusName(_ status: String) -> String {
    ["queued": "排队中", "running": "执行中", "completed": "已完成", "failed": "失败",
     "paused": "已暂停", "cancelled": "已取消", "rate_limited": "限流",
     "auth_required": "需要登录", "policy_blocked": "策略阻止",
     "quota_exhausted": "额度不足", "waiting_approval": "等待审批"][status] ?? status
}

private func statusColor(_ status: String) -> Color {
    switch status {
    case "completed": return .green
    case "failed", "auth_required", "policy_blocked", "quota_exhausted": return .orange
    case "running": return accent
    case "waiting_approval": return .purple
    default: return .gray
    }
}

private func roleName(_ role: String) -> String {
    ["plan": "规划", "implement": "实现", "review": "审查", "test": "测试",
     "summarize": "汇总"][role] ?? role
}

struct HistoryView: View {
    @StateObject private var model: HistoryModel

    init(project: String = "") {
        _model = StateObject(wrappedValue: HistoryModel(project: project))
    }
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        List {
            if let error = model.error {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(.system(size: 12)).foregroundStyle(.orange)
            }
            ForEach(model.tasks) { task in
                NavigationLink {
                    TaskDetailView(task: task, model: model)
                } label: {
                    row(task)
                }
            }
        }
        .listStyle(.plain)
        .navigationTitle("历史与审批")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } }
        }
        .task { await model.load() }
        .refreshable { await model.load() }
        .tint(accent)
    }

    private func row(_ task: DispatchTask) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 7) {
                Circle().fill(statusColor(task.status)).frame(width: 6, height: 6)
                Text(statusName(task.status)).font(.system(size: 11)).foregroundStyle(muted)
                if task.approval?.status == "pending" {
                    Text("待审批").font(.system(size: 10, weight: .semibold))
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(Color.purple.opacity(0.12), in: Capsule())
                        .foregroundStyle(.purple)
                }
                Spacer()
                if !task.isTerminal {
                    Text(roleName(task.stage)).font(.system(size: 10)).foregroundStyle(muted)
                }
                Text(Date(timeIntervalSince1970: task.updatedAt), style: .relative)
                    .font(.system(size: 10)).foregroundStyle(muted)
            }
            HStack(spacing: 6) {
                if task.executionMode == "mock" {
                    Text("Mock").font(.system(size: 9, weight: .semibold))
                        .padding(.horizontal, 5).padding(.vertical, 1)
                        .background(Color.gray.opacity(0.12), in: Capsule())
                        .foregroundStyle(muted)
                }
                Text(task.prompt).font(.system(size: 14)).lineLimit(2)
            }
        }
        .padding(.vertical, 4)
    }
}

struct TaskDetailView: View {
    let task: DispatchTask
    @ObservedObject var model: HistoryModel

    var body: some View {
        List {
            Section {
                Text(task.prompt).font(.system(size: 14))
                LabeledContent("状态", value: statusName(task.status))
                LabeledContent("阶段", value: roleName(task.stage))
                if let billing = task.billing {
                    LabeledContent("计费", value: billingLabel(billing))
                }
                if let cost = task.costUSD {
                    LabeledContent("费用", value: String(format: "$%.3f", cost))
                } else {
                    LabeledContent("费用", value: "未知（订阅或 API 未结算）")
                }
                if !task.error.isEmpty {
                    Text(task.error).font(.system(size: 12)).foregroundStyle(.orange)
                }
            }
            if let approval = task.approval, approval.status == "pending" {
                Section("审批") {
                    Text(approval.operation).font(.system(size: 13))
                    Text(approval.operationDigest).font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(muted)
                    HStack {
                        Button("拒绝", role: .destructive) {
                            Task { await model.decide(task, approval, approve: false) }
                        }
                        Spacer()
                        Button("批准") {
                            Task { await model.decide(task, approval, approve: true) }
                        }.buttonStyle(.borderedProminent)
                    }
                }
            }
            Section("步骤") {
                ForEach(task.steps) { step in
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(spacing: 6) {
                            stepIcon(step.status)
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
                        }
                    }
                }
            }
            Section {
                ForEach(actions, id: \.id) { action in
                    Button(action.title) { Task { await model.act(task, action.id) } }
                }
            }
        }
        .navigationTitle(String(task.id.prefix(8)))
        .navigationBarTitleDisplayMode(.inline)
    }

    @ViewBuilder
    private func stepIcon(_ status: String) -> some View {
        let name = status == "completed" ? "checkmark.circle.fill"
            : (status == "running" ? "circle.dotted" : "circle")
        let color: Color = status == "completed" ? .green : muted
        Image(systemName: name).font(.system(size: 11)).foregroundStyle(color)
    }

    private func billingLabel(_ billing: String) -> String {
        billing == "api" || billing == "includes_api" ? "含 API 用量" : "订阅"
    }

    private var actions: [(id: String, title: String)] {
        var items: [(String, String)] = []
        if task.canPause { items.append(("pause", "暂停任务")) }
        if task.canResume { items.append(("resume", "继续任务")) }
        if task.canRetry { items.append(("retry", "重试失败阶段")) }
        if !task.isTerminal { items.append(("cancel", "取消任务")) }
        return items
    }
}

struct TaskRecordView: View {
    let task: DispatchTask
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section("需求") {
                    Text(task.prompt).font(.system(size: 14))
                }
                Section("状态") {
                    LabeledContent("状态", value: statusName(task.status))
                    LabeledContent("阶段", value: roleName(task.stage))
                    LabeledContent("版本", value: task.revision.isEmpty ? "—" : String(task.revision.prefix(8)))
                    if let billing = task.billing { LabeledContent("计费", value: billing) }
                    if !task.error.isEmpty {
                        Text(task.error).font(.system(size: 12)).foregroundStyle(.orange)
                    }
                }
                Section("步骤记录") {
                    ForEach(task.steps) { step in
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
                                Spacer()
                                Text(step.status).font(.system(size: 10)).foregroundStyle(muted)
                            }
                            if !step.summary.isEmpty {
                                Text(step.summary).font(.system(size: 12)).foregroundStyle(muted)
                                    .textSelection(.enabled)
                            }
                        }
                    }
                }
                if !task.artifacts.isEmpty {
                    Section("产物") {
                        ForEach(Array(task.artifacts.enumerated()), id: \.offset) { _, artifact in
                            DisclosureGroup(artifact.name) {
                                Text(artifact.content.prefix(4000))
                                    .font(.system(size: 11, design: .monospaced))
                                    .textSelection(.enabled)
                            }
                        }
                    }
                }
            }
            .navigationTitle(String(task.id.prefix(8)))
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } }
            }
        }
    }
}
