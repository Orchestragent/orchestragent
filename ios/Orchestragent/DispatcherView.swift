import SwiftUI

private let ink = Color(red: 0.13, green: 0.18, blue: 0.24)
private let teal = Color(red: 0.18, green: 0.37, blue: 0.86)

struct DispatcherView: View {
    @StateObject private var model = DispatcherModel()
    @Environment(\.scenePhase) private var scenePhase
    @State private var historyShown = false
    @State private var settingsShown = false
    @State private var cancelShown = false
    @State private var discardPendingShown = false
    @FocusState private var composing: Bool

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                connectionStrip
                ScrollView {
                    VStack(alignment: .leading, spacing: 22) {
                        if let task = model.selectedTask { conversation(task) }
                        else { emptyState }
                        if let error = model.error { notice(error, icon: "exclamationmark.triangle", color: .orange) }
                    }
                    .padding(20)
                }
                .scrollDismissesKeyboard(.interactively)
                .refreshable { await model.refresh() }
                composer
            }
            .background(Color(red: 0.96, green: 0.97, blue: 0.98))
            .foregroundStyle(ink)
            .navigationTitle("调度台")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Text(model.displayMode.uppercased()).font(.caption2.bold()).tracking(1.4).fixedSize()
                        .padding(.horizontal, 9).padding(.vertical, 5)
                        .background(teal.opacity(0.12), in: Capsule()).foregroundStyle(teal)
                        .accessibilityLabel(model.displayMode == "live" ? "真实执行模式" : "Mock 模拟模式")
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { historyShown = true } label: { Image(systemName: "clock.arrow.circlepath") }
                        .accessibilityLabel("任务历史")
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { settingsShown = true } label: { Image(systemName: "slider.horizontal.3") }
                        .accessibilityLabel("调度设置")
                }
            }
            .sheet(isPresented: $historyShown) { history }
            .sheet(isPresented: $settingsShown) { SettingsView() }
            .confirmationDialog("取消这项任务？", isPresented: $cancelShown, titleVisibility: .visible) {
                Button("取消服务器任务", role: .destructive) { Task { await model.act("cancel") } }
            } message: { Text("将停止后续步骤，保留已有记录。") }
            .confirmationDialog("放弃这次待确认提交的重试？", isPresented: $discardPendingShown, titleVisibility: .visible) {
                Button("仅移除本机待确认请求", role: .destructive) { model.discardPending() }
            } message: {
                Text("服务器可能已经创建任务，这不会取消它。请先检查历史记录；重新提交会使用新编号，可能产生重复任务。草稿会保留。")
            }
            .task(id: scenePhase) {
                if scenePhase == .active { await model.poll() }
            }
        }
        .tint(teal)
        .preferredColorScheme(.light)
    }

    private var connectionStrip: some View {
        HStack(spacing: 7) {
            Circle().fill(model.connected ? teal : Color.orange).frame(width: 6, height: 6)
            Text(model.connected ? "本地调度器已连接" : "等待本地调度器 · 自动重连")
            Spacer()
            Text(model.displayMode == "live" ? (model.selectedTask?.billing == "includes_api" ? "含 API · 费用待核对" : "订阅 · 实际费用未知") : "模拟执行 · $0.00").monospacedDigit()
        }
        .font(.caption2).foregroundStyle(.secondary)
        .padding(.horizontal, 20).padding(.vertical, 10)
        .background(.white.opacity(0.7))
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 17) {
            Image(systemName: "point.3.connected.trianglepath.dotted")
                .font(.system(size: 34, weight: .light)).foregroundStyle(teal)
            Text("有什么任务？") .font(.title3.bold())
            Text(model.serverMode == "live" ? "Mac 执行 Python 标准库项目。" : "Mock 演示，暂不调用真实模型。")
                .font(.subheadline).foregroundStyle(.secondary).lineSpacing(4)
            Button("试一个任务") { model.draft = "实现一个加法函数，完成独立审查与测试，并汇总结果。" }
                .font(.subheadline.weight(.medium)).multilineTextAlignment(.leading)
            if let error = model.connectionError {
                Text(error).font(.caption).foregroundStyle(.secondary)
                Text("在 Mac 上启动本地服务：localhost:8765")
                    .font(.caption.monospaced()).foregroundStyle(.secondary)
            }
        }
        .padding(22).frame(maxWidth: .infinity, alignment: .leading)
        .background(.white, in: RoundedRectangle(cornerRadius: 22))
    }

    private func conversation(_ task: DispatchTask) -> some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack {
                Spacer(minLength: 25)
                VStack(alignment: .leading, spacing: 8) {
                    Text("你").font(.caption.bold()).foregroundStyle(teal)
                    Text(task.prompt).font(.body).textSelection(.enabled)
                }
                .padding(17).frame(maxWidth: .infinity, alignment: .leading)
                .background(teal.opacity(0.09), in: RoundedRectangle(cornerRadius: 20))
            }
            VStack(alignment: .leading, spacing: 17) {
                HStack {
                    Image(systemName: "circle.hexagongrid.fill").foregroundStyle(teal)
                    Text("调度器").font(.headline)
                    Spacer()
                    Text(Labels.status(task.status)).font(.caption.bold())
                        .foregroundStyle(task.status == "failed" ? .red : teal)
                }
                if !task.result.isEmpty { Text(task.result).textSelection(.enabled).lineSpacing(5) }
                else {
                    Text(task.status == "waiting_approval" ? "需要你批准下方操作。" : "正在\(Labels.stage(task.stage))…")
                        .font(.subheadline).foregroundStyle(.secondary).lineSpacing(4)
                }
                if !task.error.isEmpty { notice(task.error, icon: "exclamationmark.circle", color: .orange) }
                if task.status == "rate_limited" && task.nextRunAt > Date().timeIntervalSince1970 {
                    Text("预计重试：\(Date(timeIntervalSince1970: task.nextRunAt).formatted(date: .omitted, time: .standard))")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if let approval = task.approval, task.status == "waiting_approval" {
                    approvalCard(approval, taskID: task.id, mode: task.executionMode)
                }
                HStack(spacing: 14) {
                    if task.canPause { Button("暂停", systemImage: "pause") { Task { await model.act("pause") } } }
                    if task.canResume { Button("继续", systemImage: "play") { Task { await model.act("resume") } } }
                    if !task.isTerminal { Button("取消", role: .destructive) { cancelShown = true } }
                    if model.busy { ProgressView().controlSize(.small) }
                }.font(.subheadline.weight(.medium)).disabled(model.busy)
                DisclosureGroup("详情") { details(task).padding(.top, 12) }
            }
            .padding(20).frame(maxWidth: .infinity, alignment: .leading)
            .background(.white, in: RoundedRectangle(cornerRadius: 22))
            if let error = model.connectionError { notice(error, icon: "wifi.slash", color: .orange) }
        }
    }

    private func approvalCard(_ approval: Approval, taskID: String, mode: String) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Label(mode == "live" ? "待批准的操作" : "待批准的模拟操作", systemImage: "hand.raised").font(.subheadline.bold())
            Text(approval.operation).font(.subheadline).textSelection(.enabled)
            DisclosureGroup("操作标识") {
                Text(approval.operationDigest).font(.caption.monospaced()).textSelection(.enabled)
            }.font(.caption)
            if !approval.revision.isEmpty {
                Text("版本：\(approval.revision)").font(.caption.monospaced()).textSelection(.enabled)
            }
            HStack {
                Button("批准此操作") {
                    Task { await model.decide("approve", taskID: taskID, approval: approval) }
                }.buttonStyle(.borderedProminent)
                Button("拒绝", role: .destructive) {
                    Task { await model.decide("deny", taskID: taskID, approval: approval) }
                }.buttonStyle(.bordered)
            }.disabled(model.busy)
        }
        .padding(15).frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.orange.opacity(0.09), in: RoundedRectangle(cornerRadius: 14))
    }

    private func details(_ task: DispatchTask) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Divider()
            DisclosureGroup("执行步骤 · \(task.steps.count)") {
                VStack(alignment: .leading, spacing: 13) {
                    ForEach(task.steps) { step in
                        VStack(alignment: .leading, spacing: 5) {
                            Text("\(Labels.stage(step.role)) · \(Labels.status(step.status))").font(.subheadline.bold())
                            if !step.summary.isEmpty { Text(step.summary).font(.caption).textSelection(.enabled) }
                            if !step.dependsOn.isEmpty { Text("依赖：\(step.dependsOn.joined(separator: ", "))").font(.caption2).foregroundStyle(.secondary) }
                            if !step.revision.isEmpty { Text(step.revision).font(.caption2.monospaced()).textSelection(.enabled) }
                            if let provider = step.provider, !provider.isEmpty {
                                Text([provider, step.model ?? ""].filter { !$0.isEmpty }.joined(separator: " · "))
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                            if let session = step.sessionID, !session.isEmpty {
                                Text("会话：\(session)").font(.caption2.monospaced()).textSelection(.enabled)
                            }
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }
                }.padding(.top, 10)
            }
            DisclosureGroup("事件记录 · \(model.events.count)") {
                LazyVStack(alignment: .leading, spacing: 12) {
                    ForEach(model.events) { event in
                        VStack(alignment: .leading, spacing: 4) {
                            Text("#\(event.seq)  \(event.kind)").font(.caption.bold())
                            Text(event.detail).font(.caption).textSelection(.enabled)
                            Text(Date(timeIntervalSince1970: event.at), style: .time).font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }.padding(.top, 10).frame(maxWidth: .infinity, alignment: .leading)
            }
            DisclosureGroup("交付产物 · \(task.artifacts.count)") {
                VStack(alignment: .leading, spacing: 14) {
                    ForEach(task.artifacts) { artifact in
                        VStack(alignment: .leading, spacing: 7) {
                            Label(artifact.name, systemImage: "doc.text").font(.caption.bold())
                            Text(artifact.content).font(.caption.monospaced()).textSelection(.enabled)
                        }
                    }
                }.padding(.top, 10).frame(maxWidth: .infinity, alignment: .leading)
            }
            DisclosureGroup("用量与任务信息") {
                VStack(alignment: .leading, spacing: 8) {
                    if task.executionMode == "live" {
                        Text("订阅调用 · 实际费用未知")
                        Text("供应商：\(task.provider)")
                    } else if let cost = task.costUSD {
                        Text("Mock 模拟用量：\(cost, format: .currency(code: "USD"))")
                    } else { Text("费用未知") }
                    Text("尝试次数：\(task.attempt)")
                    if let revision = task.policyRevision { Text("设置版本：\(revision)") }
                    Text("任务：\(task.id)").textSelection(.enabled)
                    Text("版本：\(task.revision.isEmpty ? "尚未生成" : task.revision)").textSelection(.enabled)
                    Text("最后更新：\(Date(timeIntervalSince1970: task.updatedAt).formatted())")
                }.font(.caption).foregroundStyle(.secondary).padding(.top, 10)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }.font(.subheadline)
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let pending = model.pending {
                VStack(alignment: .leading, spacing: 4) {
                    Text("有一项提交等待确认 · 点击重试不会重复创建") .font(.caption).foregroundStyle(.orange)
                    Text(pending.prompt).font(.caption).lineLimit(2)
                    Button("放弃重试…") { discardPendingShown = true }.font(.caption).disabled(model.busy)
                }
            }
            HStack {
                Menu {
                    Picker("Mock 演示场景", selection: $model.scenario) {
                        ForEach(Labels.scenarios, id: \.0) { value, title in Text(title).tag(value) }
                    }
                } label: {
                    Label(model.submissionMode == "live" ? "正常任务" : "演示：\(Labels.scenarios.first { $0.0 == model.scenario }?.1 ?? model.scenario)", systemImage: "slider.horizontal.3")
                        .font(.caption)
                }.disabled(model.pending != nil)
                Spacer()
                if model.scenario == "happy" && model.pending == nil {
                    Menu {
                        Button("Mock 演示") { model.chooseMode("mock") }
                        Button("Live 真实执行") { model.chooseMode("live") }
                            .disabled(model.serverMode != "live")
                    } label: {
                        Text(model.submissionMode == "live" ? "LIVE ▾" : "MOCK ▾").font(.caption.bold())
                    }
                } else {
                    Text(model.submissionMode.uppercased()).font(.caption2).foregroundStyle(.secondary)
                }
            }
            HStack(alignment: .bottom, spacing: 12) {
                TextField("描述你想完成的任务…", text: $model.draft, axis: .vertical)
                    .lineLimit(1...5).focused($composing).padding(12)
                    .background(Color.gray.opacity(0.07), in: RoundedRectangle(cornerRadius: 14))
                    .disabled(model.pending != nil)
                Button {
                    composing = false
                    Task { await model.submit() }
                } label: {
                    Image(systemName: model.pending == nil ? "arrow.up" : "arrow.clockwise")
                        .font(.headline).frame(width: 43, height: 43)
                        .foregroundStyle(.white).background(teal, in: RoundedRectangle(cornerRadius: 14))
                }
                .disabled(model.busy || (model.pending == nil && model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty))
                .opacity(model.busy ? 0.5 : 1)
                .accessibilityLabel(model.pending == nil ? "提交任务" : "重试同一项提交")
            }
        }.padding(.horizontal, 20).padding(.top, 14).padding(.bottom, 12).background(.white)
    }

    private var history: some View {
        NavigationStack {
            List {
                if model.tasks.isEmpty { Text("还没有任务。").foregroundStyle(.secondary) }
                ForEach(model.tasks) { task in
                    Button {
                        model.select(task.id)
                        historyShown = false
                        Task { await model.refresh() }
                    } label: {
                        VStack(alignment: .leading, spacing: 8) {
                            Text(task.prompt).font(.body).lineLimit(2).foregroundStyle(ink)
                            HStack {
                                Text(Labels.status(task.status)).foregroundStyle(teal)
                                Text(task.executionMode.uppercased()).foregroundStyle(.secondary)
                                Spacer()
                                Text(Date(timeIntervalSince1970: task.createdAt), style: .date).foregroundStyle(.secondary)
                            }.font(.caption)
                        }.padding(.vertical, 5)
                    }
                }
            }
            .navigationTitle("任务历史")
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { historyShown = false } } }
        }
    }

    private func notice(_ text: String, icon: String, color: Color) -> some View {
        Label(text, systemImage: icon).font(.caption).foregroundStyle(color).textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}
