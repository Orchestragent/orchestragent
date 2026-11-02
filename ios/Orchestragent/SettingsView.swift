import SwiftUI

struct ModelOption: Decodable, Identifiable {
    let id: String
    let label: String
}

private struct ModelCatalog: Decodable { let models: [String: [ModelOption]] }

struct ProviderPreference: Codable, Equatable, Identifiable {
    let id: String
    var model: String
    var instructions: String
    var effort: String = ""
    enum CodingKeys: String, CodingKey { case id, model, instructions, effort }
    init(id: String, model: String, instructions: String, effort: String = "") {
        self.id = id
        self.model = model
        self.instructions = instructions
        self.effort = effort
    }
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        model = try container.decodeIfPresent(String.self, forKey: .model) ?? ""
        instructions = try container.decodeIfPresent(String.self, forKey: .instructions) ?? ""
        effort = try container.decodeIfPresent(String.self, forKey: .effort) ?? ""
    }
    var name: String {
        ["codex": "Codex", "claude": "Claude Code", "dsh": "DeepSeek Harness", "cursor": "Cursor", "kimi": "Kimi"][id] ?? id
    }
}

struct DispatchPolicy: Codable, Equatable {
    var schedulerProvider: String
    var schedulerModel: String
    var instructions: String
    var providers: [ProviderPreference]
    var implementationProvider: String = ""
    var reviewProvider: String = ""
    var chatModel: String = ""
    var chatEffort: String = ""
    var requireAcceptance: Bool = false
    var maxReworks: Int = 3
    var maxTaskCents: Int = 0
    var manualReviewProvider: String = ""
    enum CodingKeys: String, CodingKey {
        case instructions, providers
        case schedulerProvider = "scheduler_provider", schedulerModel = "scheduler_model"
        case implementationProvider = "implementation_provider"
        case reviewProvider = "review_provider", chatModel = "chat_model"
        case chatEffort = "chat_effort"
        case requireAcceptance = "require_acceptance"
        case maxReworks = "max_reworks"
        case maxTaskCents = "max_task_cents"
        case manualReviewProvider = "manual_review_provider"
    }
    init(schedulerProvider: String, schedulerModel: String, instructions: String,
         providers: [ProviderPreference], implementationProvider: String = "",
         reviewProvider: String = "", chatModel: String = "", chatEffort: String = "",
         requireAcceptance: Bool = false, maxReworks: Int = 3, maxTaskCents: Int = 0,
         manualReviewProvider: String = "") {
        self.schedulerProvider = schedulerProvider
        self.schedulerModel = schedulerModel
        self.instructions = instructions
        self.providers = providers
        self.implementationProvider = implementationProvider
        self.reviewProvider = reviewProvider
        self.chatModel = chatModel
        self.chatEffort = chatEffort
        self.requireAcceptance = requireAcceptance
        self.maxReworks = maxReworks
        self.maxTaskCents = maxTaskCents
        self.manualReviewProvider = manualReviewProvider
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        schedulerProvider = try container.decode(String.self, forKey: .schedulerProvider)
        schedulerModel = try container.decode(String.self, forKey: .schedulerModel)
        instructions = try container.decodeIfPresent(String.self, forKey: .instructions) ?? ""
        providers = try container.decode([ProviderPreference].self, forKey: .providers)
        implementationProvider = try container.decodeIfPresent(String.self, forKey: .implementationProvider) ?? ""
        reviewProvider = try container.decodeIfPresent(String.self, forKey: .reviewProvider) ?? ""
        chatModel = try container.decodeIfPresent(String.self, forKey: .chatModel) ?? ""
        chatEffort = try container.decodeIfPresent(String.self, forKey: .chatEffort) ?? ""
        requireAcceptance = try container.decodeIfPresent(Bool.self, forKey: .requireAcceptance) ?? false
        maxReworks = try container.decodeIfPresent(Int.self, forKey: .maxReworks) ?? 3
        maxTaskCents = try container.decodeIfPresent(Int.self, forKey: .maxTaskCents) ?? 0
        manualReviewProvider = try container.decodeIfPresent(String.self, forKey: .manualReviewProvider) ?? ""
    }

    static let initial = DispatchPolicy(schedulerProvider: "codex", schedulerModel: "gpt-6-astra",
                                       instructions: "",
                                       providers: ["codex", "claude", "dsh", "cursor", "kimi"].map {
        ProviderPreference(id: $0, model: "", instructions: "")
    })
}

struct DispatcherSettings: Codable {
    let revision: Int
    let mode: String
    let policy: DispatchPolicy
}

struct SettingsProposal: Codable, Identifiable {
    let id: String
    let baseRevision: Int
    let mode: String
    let summary: String
    let policy: DispatchPolicy
    enum CodingKeys: String, CodingKey {
        case id, mode, summary, policy
        case baseRevision = "base_revision"
    }
}

@MainActor
final class SettingsModel: ObservableObject {
    @Published var policy = DispatchPolicy.initial
    @Published var instruction = ""
    @Published private(set) var saved: DispatcherSettings?
    @Published var proposal: SettingsProposal?
    @Published private(set) var busy = false
    @Published private(set) var error: String?
    @Published private(set) var message: String?
    @Published private(set) var providers: [String: ProviderReadiness] = [:]
    @Published private(set) var models: [String: [ModelOption]] = [:]
    private let api: APIClient
    var hasChanges: Bool { saved.map { $0.policy != policy } ?? false }
    var isLive: Bool { saved?.mode == "live" }

    init(api: APIClient = APIClient()) { self.api = api }

    struct Update: Encodable {
        let baseRevision: Int
        let policy: DispatchPolicy
        enum CodingKeys: String, CodingKey { case policy; case baseRevision = "base_revision" }
    }
    struct Instruction: Encodable { let instruction: String }
    struct EmptyBody: Encodable {}

    private func accept(_ settings: DispatcherSettings) {
        saved = settings
        policy = settings.policy
        proposal = nil
    }

    func loadCatalog() async {
        if let catalog: ModelCatalog = try? await api.request("/providers/models", timeout: 30) {
            models = catalog.models
        }
    }

    func load() async {
        guard !busy else { return }
        busy = true
        error = nil
        message = nil
        defer { busy = false }
        do {
            accept(try await api.request("/settings"))
            providers = (try? await api.request("/providers")) ?? [:]
        }
        catch { self.error = error.localizedDescription }
    }

    func save() async {
        guard !busy, let saved else { return }
        busy = true
        error = nil
        message = nil
        defer { busy = false }
        do {
            let settings: DispatcherSettings = try await api.request(
                "/settings", method: "POST",
                body: JSONEncoder().encode(Update(baseRevision: saved.revision, policy: policy)),
                timeout: 45)
            accept(settings)
            message = "已保存为版本 \(settings.revision)，将在新任务中使用。"
        } catch {
            // A lost response does not mean a lost write: re-read and compare revisions.
            if let fresh: DispatcherSettings = try? await api.request("/settings", timeout: 20),
               fresh.revision > saved.revision {
                accept(fresh)
                message = "已保存为版本 \(fresh.revision)。"
            } else {
                self.error = "保存失败：\(error.localizedDescription)"
            }
        }
    }

    func propose() async {
        guard !busy, !hasChanges, !instruction.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        busy = true
        error = nil
        message = nil
        defer { busy = false }
        do {
            proposal = try await api.request("/settings/proposals", method: "POST",
                                            body: JSONEncoder().encode(Instruction(instruction: instruction)), timeout: 200)
        } catch { self.error = error.localizedDescription }
    }

    func apply(_ shownProposal: SettingsProposal) async {
        guard !busy else { return }
        busy = true
        error = nil
        message = nil
        defer { busy = false }
        do {
            let settings: DispatcherSettings = try await api.request("/settings/proposals/\(shownProposal.id)/apply", method: "POST",
                                                                    body: JSONEncoder().encode(EmptyBody()))
            accept(settings)
            instruction = ""
            message = "已应用预览，当前版本 \(settings.revision)。"
        } catch { self.error = "预览未应用。\(error.localizedDescription)" }
    }
}

struct SettingsView: View {
    @StateObject private var model = SettingsModel()
    @Environment(\.dismiss) private var dismiss
    @State private var discardShown = false
    @State private var reloadShown = false
    @State private var serverURL = ServerConfig.rawURL
    @State private var serverToken = ServerConfig.token
    @State private var serverNotice: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Label(model.isLive ? "LIVE · 调度配置" : "MOCK · 配置偏好", systemImage: "slider.horizontal.3").font(.headline)
                    Text(model.isLive ? "用于新任务的模型与执行要求。" : "仅保存偏好，真实执行未启用。")
                        .font(.caption).foregroundStyle(.secondary)
                    if let saved = model.saved { Text("当前设置版本 \(saved.revision)").font(.caption.monospaced()) }
                }
                Section {
                    TextField("服务地址", text: $serverURL)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                        .keyboardType(.URL)
                    TextField("设备令牌（留空表示本机回环）", text: $serverToken)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    Button("保存并重新连接", systemImage: "antenna.radiowaves.left.and.right") {
                        ServerConfig.save(url: serverURL, token: serverToken)
                        serverURL = ServerConfig.rawURL
                        serverToken = ServerConfig.token
                        serverNotice = "已保存，正在重新连接。"
                        Task { await model.load() }
                    }.disabled(model.busy)
                    if let serverNotice {
                        Text(serverNotice).font(.caption).foregroundStyle(.secondary)
                    }
                } header: { Text("服务器") } footer: {
                    Text("真机请填 Mac 的 Tailscale 地址与设备令牌；模拟器可留 localhost。")
                }
                if model.saved != nil {
                    Section {
                        TextField("例如：Claude 审查时优先检查边界条件", text: $model.instruction, axis: .vertical).lineLimit(2...5)
                        Button("生成配置预览", systemImage: "text.badge.plus") { Task { await model.propose() } }
                            .disabled(model.busy || model.hasChanges || model.instruction.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    } header: { Text("一句话调整") } footer: {
                        Text(model.hasChanges ? "请先保存手动修改。" : (model.isLive ? "调用调度模型解析，预览后应用。" : "Mock 仅追加原句；查看预览后应用。"))
                    }
                    Section {
                        Picker("调度供应商", selection: $model.policy.schedulerProvider) {
                            ForEach(model.policy.providers) { provider in Text(provider.name).tag(provider.id) }
                        }
                        Picker("调度模型", selection: $model.policy.schedulerModel) {
                            ForEach(model.models[model.policy.schedulerProvider] ?? [], id: \.id) { option in
                                Text(option.label).tag(option.id)
                            }
                            if !model.policy.schedulerModel.isEmpty,
                               !(model.models[model.policy.schedulerProvider] ?? []).contains(where: {
                                   $0.id == model.policy.schedulerModel }) {
                                Text("\(model.policy.schedulerModel)（当前）")
                                    .tag(model.policy.schedulerModel)
                            }
                        }
                        Picker("调度推理强度", selection: $model.policy.chatEffort) {
                            Text("跟随供应商").tag("")
                            ForEach(efforts(for: model.policy.schedulerProvider), id: \.self) { value in
                                Text(effortName(value)).tag(value)
                            }
                        }
                        Toggle("测试后人工验收", isOn: $model.policy.requireAcceptance)
                        if model.policy.requireAcceptance {
                            Stepper("最多返工 \(model.policy.maxReworks) 次",
                                    value: $model.policy.maxReworks, in: 1...5)
                        }
                        TextField("全局要求", text: $model.policy.instructions, axis: .vertical).lineLimit(3...9)
                    } header: { Text("调度器") } footer: {
                        Text(model.isLive ? "按厂商权限和已确认额度执行；DSH 单独计费。" : "模型名称仅记录偏好。")
                    }
                    Section("供应商偏好") {
                        ForEach($model.policy.providers) { $provider in
                            DisclosureGroup(provider.name) {
                                if let state = model.providers[provider.id] {
                                    Label(state.ready ? "已就绪" : "未就绪", systemImage: state.ready ? "checkmark.circle" : "exclamationmark.circle")
                                        .font(.caption).foregroundStyle(state.ready ? .blue : .orange)
                                    if let message = state.message { Text(message).font(.caption).foregroundStyle(.secondary) }
                                    else if let auth = state.auth { Text(auth).font(.caption).foregroundStyle(.secondary) }
                                }
                                if !efforts(for: provider.id).isEmpty {
                                Picker("推理强度", selection: $provider.effort) {
                                    Text("默认").tag("")
                                    ForEach(efforts(for: provider.id), id: \.self) { value in
                                        Text(effortName(value)).tag(value)
                                    }
                                }
                                }
                                Picker("模型", selection: $provider.model) {
                                    Text("默认").tag("")
                                    ForEach(model.models[provider.id] ?? [], id: \.id) { option in
                                        Text(option.label).tag(option.id)
                                    }
                                    if !provider.model.isEmpty,
                                       !(model.models[provider.id] ?? []).contains(where: {
                                           $0.id == provider.model }) {
                                        Text("\(provider.model)（当前）").tag(provider.model)
                                    }
                                }
                                TextField("对该供应商的要求", text: $provider.instructions, axis: .vertical).lineLimit(3...8)
                            }
                        }
                    }
                    Section {
                        Picker("实现厂商（固定）", selection: $model.policy.implementationProvider) {
                            Text("由规划模型决定").tag("")
                            ForEach(model.policy.providers) { provider in
                                Text(provider.name).tag(provider.id)
                            }
                        }
                        Picker("审查厂商（固定）", selection: $model.policy.reviewProvider) {
                            Text("由规划模型决定").tag("")
                            ForEach(model.policy.providers) { provider in
                                Text(provider.name).tag(provider.id)
                            }
                        }
                        Picker("对话模型", selection: $model.policy.chatModel) {
                            Text("跟随调度模型").tag("")
                            ForEach(model.models["claude"] ?? [], id: \.id) { option in
                                Text(option.label).tag(option.id)
                            }
                            if !model.policy.chatModel.isEmpty,
                               !(model.models["claude"] ?? []).contains(where: {
                                   $0.id == model.policy.chatModel }) {
                                Text("\(model.policy.chatModel)（当前）")
                                    .tag(model.policy.chatModel)
                            }
                        }
                        Picker("测试后人工复核", selection: $model.policy.manualReviewProvider) {
                            Text("不暂停").tag("")
                            Text("暂停，等我用 Kimi 复核").tag("kimi")
                        }
                        Stepper(value: $model.policy.maxTaskCents, in: 0...100000, step: 500) {
                            Text(model.policy.maxTaskCents == 0
                                 ? "每任务预算：不限"
                                 : String(format: "每任务预算：¥%.0f", Double(model.policy.maxTaskCents) / 100))
                        }
                    } header: { Text("固定分工") } footer: {
                        Text("固定后不再由规划模型决定；实现与审查必须不同厂商。对话模型只影响聊天速度。")
                    }
                    Section {
                        Button("保存手动配置") { Task { await model.save() } }
                            .disabled(model.busy || !model.hasChanges)
                        Button("重新读取已保存配置") {
                            if model.hasChanges { reloadShown = true }
                            else { Task { await model.load() } }
                        }.disabled(model.busy)
                    } footer: { Text("新任务会固定保存当前设置版本，已有任务保持其创建时的配置。") }
                } else {
                    Section {
                        if model.busy { ProgressView("读取配置…") }
                        else { Button("重新连接并读取配置") { Task { await model.load() } } }
                    }
                }
                if let error = model.error {
                    Section { Label(error, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(.orange) }
                }
                if model.busy && model.saved != nil { Section { ProgressView("处理中…") } }
                if let message = model.message {
                    Section { Label(message, systemImage: "checkmark.circle").font(.caption).foregroundStyle(.blue) }
                }
            }
            .disabled(model.busy && model.saved != nil)
            .navigationTitle("调度设置")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("完成") {
                        if model.hasChanges { discardShown = true }
                        else { dismiss() }
                    }.disabled(model.busy)
                }
            }
            .interactiveDismissDisabled(model.hasChanges || model.busy)
            .confirmationDialog("放弃尚未保存的修改？", isPresented: $discardShown, titleVisibility: .visible) {
                Button("放弃修改并关闭", role: .destructive) { dismiss() }
            }
            .confirmationDialog("重新读取会放弃手动修改", isPresented: $reloadShown, titleVisibility: .visible) {
                Button("重新读取", role: .destructive) { Task { await model.load() } }
            }
            .sheet(item: $model.proposal) { proposal in proposalPreview(proposal) }
            .task {
                await model.load()
                await model.loadCatalog()
            }
        }.tint(.blue)
    }

    private func proposalPreview(_ proposal: SettingsProposal) -> some View {
        NavigationStack {
            Form {
                Section {
                    Text(proposal.mode == "live" ? "LIVE · 配置预览" : "MOCK · 确定性追加预览").font(.headline)
                    Text(proposal.mode == "live" ? "由调度模型解析，确认后应用。" : "Mock 追加原句，未进行 AI 语义解析。")
                        .font(.caption).foregroundStyle(.secondary)
                    Text(proposal.summary).font(.subheadline)
                    Text("基于版本 \(proposal.baseRevision)").font(.caption.monospaced())
                }
                Section("调度器与全局要求") {
                    LabeledContent("供应商", value: proposal.policy.schedulerProvider)
                    LabeledContent("模型", value: proposal.policy.schedulerModel)
                    Text(proposal.policy.instructions.isEmpty ? "（全局要求为空）" : proposal.policy.instructions).textSelection(.enabled)
                }
                ForEach(proposal.policy.providers) { provider in
                    Section(provider.name) {
                        LabeledContent("模型", value: provider.model.isEmpty ? "（未指定）" : provider.model)
                        Text(provider.instructions.isEmpty ? "（要求为空）" : provider.instructions).textSelection(.enabled)
                    }
                }
                if let error = model.error {
                    Section { Text(error).font(.caption).foregroundStyle(.orange) }
                }
                Section {
                    Button("确认并应用这份预览") { Task { await model.apply(proposal) } }
                        .disabled(model.busy)
                    Text("预览编号：\(proposal.id)").font(.caption2.monospaced()).textSelection(.enabled)
                }
            }
            .navigationTitle("配置预览").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("返回修改") { model.proposal = nil }.disabled(model.busy) } }
            .interactiveDismissDisabled(model.busy)
        }
    }
}

private func efforts(for provider: String) -> [String] {
    switch provider {
    case "codex": return ["low", "medium", "high", "xhigh", "max"]
    case "claude": return ["low", "medium", "high", "max"]
    case "dsh": return ["off", "low", "high", "max"]
    case "kimi": return ["low", "high", "max"]
    default: return []
    }
}

private func effortName(_ value: String) -> String {
    ["low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh",
     "max": "max（最深思）", "off": "off（关闭思考）"][value] ?? value
}
