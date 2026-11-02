import AppKit
import SwiftUI

struct NodeInspector: View {
    let store: AppStore
    @State private var tab = InspectorTab.conversation
    @State private var drafts: [String: String] = [:]
    @State private var reworkNodes: Set<String> = []
    @State private var sending: Set<String> = []
    @State private var expandedAttempts: Set<Int> = []
    @State private var focusedAttempt: Int?
    @FocusState private var composerFocused: Bool

    private var draftID: String { "\(store.selectedRunID ?? "")/\(store.selectedNodeID ?? "")" }
    private var detail: NodeDetail? {
        guard let detail = store.detail, detail.node.id == store.selectedNodeID else { return nil }
        return detail
    }

    var body: some View {
        Group {
            if let detail {
                VStack(spacing: 0) {
                    header(detail)
                    Divider()
                    Picker(L("Node details", "节点详情"), selection: $tab) {
                        ForEach(InspectorTab.allCases) { item in Text(item.title).tag(item) }
                    }
                    .pickerStyle(.segmented).labelsHidden().padding(12)
                    switch tab {
                    case .conversation: conversation(detail)
                    case .attempts: attempts(detail)
                    case .tests: tests(detail)
                    case .files: files(detail)
                    case .spec: specification(detail)
                    }
                }
            } else if store.selectedNodeID != nil {
                VStack(alignment: .leading, spacing: 16) {
                    RoundedRectangle(cornerRadius: 5).fill(.quaternary).frame(width: 110, height: 22)
                    ForEach(0..<5) { _ in RoundedRectangle(cornerRadius: 5).fill(.quaternary).frame(height: 16) }
                    Spacer()
                }.padding(24).accessibilityLabel(L("Loading node", "正在加载节点"))
            } else {
                ContentUnavailableView(L("Select a node", "选择节点"), systemImage: "sidebar.right", description: Text(L("Inspect its conversation, attempts and files.", "查看节点对话、尝试和文件。")))
            }
        }
        .frame(minWidth: 380, idealWidth: 450, maxWidth: .infinity, maxHeight: .infinity)
        .background(.background)
        .onReceive(NotificationCenter.default.publisher(for: Notification.Name("studioFocusComposer"))) { _ in
            tab = .conversation
            composerFocused = true
        }
        .task(id: "\(store.focusComposerRequested)-\(detail?.node.id ?? "")") {
            if store.focusComposerRequested, detail != nil {
                tab = .conversation; composerFocused = true; store.focusComposerRequested = false
            }
        }
        .onChange(of: store.selectedNodeID) { _, _ in expandedAttempts = []; focusedAttempt = nil }
    }

    private func header(_ detail: NodeDetail) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(detail.node.id).font(.system(.title3, design: .monospaced).weight(.semibold))
                StatusBadge(status: detail.node.status)
                Spacer()
                Button { copy(detail.node.id) } label: { Image(systemName: "doc.on.doc") }
                    .buttonStyle(.borderless).help(L("Copy node ID", "复制节点 ID"))
                Button { reveal(detail.files.map(\.path)) } label: { Image(systemName: "folder") }
                    .buttonStyle(.borderless).disabled(store.localFileURLs(detail.files.map(\.path)).isEmpty)
                    .help(L("Reveal owned files in Finder", "在 Finder 中显示拥有的文件"))
            }
            Text(detail.node.goal).font(.headline).lineLimit(4).help(detail.node.goal).textSelection(.enabled)
            if !detail.node.diagnosis.summary.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Label(detail.node.diagnosis.summary, systemImage: diagnosisIcon(detail.node.diagnosis.kind))
                        .font(.callout).fixedSize(horizontal: false, vertical: true)
                    if !detail.node.diagnosis.blamed.isEmpty {
                        HStack {
                            Text(L("Related:", "相关节点：")).foregroundStyle(.secondary)
                            ForEach(detail.node.diagnosis.blamed, id: \.self) { id in
                                Button(id) { Task { await store.selectNode(id) } }.buttonStyle(.link)
                            }
                        }.font(.caption)
                    }
                }.padding(10).frame(maxWidth: .infinity, alignment: .leading)
                    .background(Palette.status(detail.node.status).opacity(0.09), in: RoundedRectangle(cornerRadius: 8))
            }
            HStack {
                Menu {
                    Button(L("Role default", "角色默认") + (store.settings?.roles.first(where: { $0.role == "builder" })?.defaultRoute.map { " · " + $0.name } ?? "")) { Task { await store.setNodeRoute("") } }
                    Divider()
                    ForEach(Array(Set(detail.routes.map(\.vendor))).sorted(), id: \.self) { vendor in
                        Section(vendor.capitalized) {
                            ForEach(detail.routes.filter { $0.vendor == vendor }.sorted(by: routeOrder)) { route in
                                Button(route.name + (route.verified ? "" : L(" · unverified", " · 未经验证"))) {
                                    Task { await store.setNodeRoute(route.label) }
                                }
                            }
                        }
                    }
                } label: { RouteChip(route: detail.node.route, overridden: detail.node.routeOverride) }
                .menuStyle(.borderlessButton).disabled(!store.connected || detail.node.status == .replanned || store.graph?.run.status == "cancelled")
                Spacer()
                if detail.node.status == .parked {
                    Button(L("Retry", "重试"), systemImage: "arrow.clockwise") { Task { await store.retryNode() } }
                        .disabled(!store.connected || store.graph?.run.status == "cancelled")
                }
            }
        }.padding(16)
    }

    private func conversation(_ detail: NodeDetail) -> some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        if detail.thread.isEmpty {
                            Text(L("No messages yet. Give this node a specific instruction.", "尚无消息。可向此节点提供具体指引。"))
                                .font(.callout).foregroundStyle(.secondary).padding(.vertical, 24)
                        }
                        ForEach(detail.thread) { item in threadItem(item, detail: detail).id(item.id) }
                        Color.clear.frame(height: 1).id("thread-bottom")
                    }.padding(16)
                }
                .onAppear { proxy.scrollTo("thread-bottom", anchor: .bottom) }
                .onChange(of: detail.thread.count) { _, _ in proxy.scrollTo("thread-bottom", anchor: .bottom) }
            }
            Divider()
            composer(detail)
        }
    }

    private func threadItem(_ item: ThreadItem, detail: NodeDetail) -> some View {
        HStack(alignment: .top, spacing: 8) {
            if item.author == "you" { Spacer(minLength: 28) }
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 5) {
                    Text(item.author == "you" ? L("You", "你") : item.author == "orch" ? "orch" : L("Agent", "智能体"))
                        .fontWeight(.semibold)
                    if let number = item.attempt { Text("· a\(number)") }
                    if item.author == "agent", let attempt = detail.attempts.first(where: { $0.n == item.attempt }) {
                        Text(attempt.route.name).lineLimit(1)
                    }
                    Spacer(minLength: 0)
                    Text(Date(timeIntervalSince1970: item.at), style: .time)
                }.font(.caption2).foregroundStyle(.secondary)
                Text(item.text).font(item.author == "orch" ? .callout : .body).textSelection(.enabled)
                if item.kind == "failed", let number = item.attempt {
                    Button(L("View attempt a\(number)", "查看尝试 a\(number)")) {
                        expandedAttempts.insert(number); focusedAttempt = number; tab = .attempts
                    }.buttonStyle(.link).font(.caption)
                }
            }
            .padding(item.author == "orch" ? 8 : 12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(item.author == "you" ? Color.accentColor.opacity(0.12) : Color.primary.opacity(item.author == "orch" ? 0.025 : 0.045), in: RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(item.author == "agent" ? Palette.vendor(detail.attempts.first(where: { $0.n == item.attempt })?.route.vendor ?? detail.node.route.vendor).opacity(0.35) : .clear))
            if item.author == "agent" { Spacer(minLength: 20) }
        }
    }

    private func composer(_ detail: NodeDetail) -> some View {
        let id = draftID
        let cancelled = store.graph?.run.status == "cancelled"
        let allowed = MessagePolicy.canSend(status: detail.node.status, cancelled: cancelled)
        let working = sending.contains(id)
        let rework = reworkNodes.contains(id)
        return VStack(alignment: .leading, spacing: 9) {
            Text(L("Scope: only \(detail.node.id) and its files", "作用范围：仅 \(detail.node.id) 及其拥有的文件") + (detail.node.files.isEmpty ? "" : " · " + detail.node.files.joined(separator: ", ")))
                .font(.caption).foregroundStyle(.secondary).lineLimit(2).help(detail.node.files.joined(separator: ", "))
            if detail.node.status == .replanned {
                Text(L("This node has been replaced.", "此节点已被替换。"))
                ForEach(detail.node.replacedBy, id: \.self) { replacement in
                    Button(replacement) { Task { await store.selectNode(replacement) } }.buttonStyle(.link)
                }
            } else {
                TextEditor(text: Binding(get: { drafts[id] ?? "" }, set: { drafts[id] = $0 }))
                    .font(.body).scrollContentBackground(.hidden).frame(minHeight: 64, maxHeight: 125)
                    .padding(6).background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 7))
                    .focused($composerFocused).disabled(!allowed || working)
                    .accessibilityLabel(L("Guidance for this node", "给此节点的指引"))
                if detail.node.status == .done {
                    Toggle(L("Rebuild now and notify downstream", "立即按此重做，并通知下游"), isOn: Binding(get: { reworkNodes.contains(id) }, set: { if $0 { reworkNodes.insert(id) } else { reworkNodes.remove(id) } }))
                        .font(.caption).toggleStyle(.checkbox).disabled(working)
                }
                HStack {
                    if !allowed { Text(L("This run is read-only.", "此运行当前只读。")).font(.caption).foregroundStyle(.secondary) }
                    Spacer()
                    if working { ProgressView().controlSize(.small) }
                    Button(sendTitle(status: detail.node.status, rework: rework)) { send(detail, id: id) }
                        .keyboardShortcut(.return, modifiers: .command).buttonStyle(.borderedProminent)
                        .disabled(!allowed || !store.connected || working || (drafts[id] ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }.padding(14)
    }

    private func send(_ detail: NodeDetail, id: String) {
        let runID = store.selectedRunID
        let nodeID = detail.node.id
        let text = drafts[id] ?? ""
        let rework = MessagePolicy.rework(status: detail.node.status, requested: reworkNodes.contains(id))
        guard !sending.contains(id), store.connected,
              MessagePolicy.canSend(status: detail.node.status, cancelled: store.graph?.run.status == "cancelled"),
              !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        sending.insert(id)
        Task { @MainActor in
            defer { sending.remove(id) }
            guard store.selectedRunID == runID, store.selectedNodeID == nodeID else { return }
            if await store.sendMessage(text: text, rework: rework) {
                if drafts[id] == text { drafts[id] = "" }
                reworkNodes.remove(id)
            }
        }
    }

    private func attempts(_ detail: NodeDetail) -> some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    if detail.attempts.isEmpty { empty(L("No attempts yet", "尚无尝试")) }
                    ForEach(detail.attempts.reversed()) { attempt in
                        DisclosureGroup(isExpanded: Binding(get: { expandedAttempts.contains(attempt.n) }, set: { if $0 { expandedAttempts.insert(attempt.n) } else { expandedAttempts.remove(attempt.n) } })) {
                            VStack(alignment: .leading, spacing: 10) {
                                if !attempt.summary.isEmpty { Text(attempt.summary).textSelection(.enabled) }
                                if !attempt.report.isEmpty { InspectorCode(title: L("Report", "报告"), content: attempt.report) }
                                if !attempt.diff.isEmpty { InspectorCode(title: L("Diff", "变更"), content: attempt.diff, diff: true) }
                            }.padding(.top, 8)
                        } label: {
                            VStack(alignment: .leading, spacing: 5) {
                                HStack {
                                    Text(attempt.label).font(.system(.headline, design: .monospaced))
                                    Text(attempt.outcome).font(.caption).foregroundStyle(attempt.outcome == "passed" ? .green : attempt.outcome == "failed" ? .orange : .secondary)
                                    Spacer()
                                    Text(String(format: "%.1fs", max(0, (attempt.ended ?? Date().timeIntervalSince1970) - attempt.started))).font(.caption).foregroundStyle(.secondary)
                                }
                                RouteChip(route: attempt.route)
                                Text(rungTitle(attempt.rung) + " · " + attempt.category).font(.caption).foregroundStyle(.secondary)
                            }
                        }.padding(12).background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 9)).id(attempt.n)
                    }
                }.padding(14)
            }.onAppear { if let focusedAttempt { proxy.scrollTo(focusedAttempt, anchor: .top) } }
        }
    }

    private func tests(_ detail: NodeDetail) -> some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 16) {
                Label(L("Tests are frozen and read-only. Explain a suspected test issue in the conversation; the review process decides disputes.", "测试已冻结且只读。如认为测试有误，请在对话中说明，由审查流程裁决。"), systemImage: "lock.shield")
                    .font(.callout).foregroundStyle(.secondary)
                if detail.checks.isEmpty { empty(L("No frozen checks", "暂无冻结检查")) }
                ForEach(detail.checks) { check in
                    VStack(alignment: .leading, spacing: 10) {
                        Text(check.id).font(.headline)
                        InspectorCode(title: L("Command", "命令"), content: check.command)
                        ForEach(check.files) { file in InspectorCode(title: file.path, content: file.content, numbered: true) }
                    }
                }
            }.padding(16)
        }
    }

    private func files(_ detail: NodeDetail) -> some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 16) {
                if detail.files.isEmpty { empty(L("No owned files", "暂无拥有的文件")) }
                ForEach(detail.files) { file in
                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            Text(file.path).font(.system(.callout, design: .monospaced)).textSelection(.enabled)
                            Spacer()
                            if let state = file.state { Text(state).font(.caption).foregroundStyle(.secondary) }
                        }
                        HStack {
                            Button(L("Reveal in Finder", "在 Finder 中显示"), systemImage: "folder") { reveal([file.path]) }
                            Button(L("Open in Editor", "在编辑器中打开"), systemImage: "arrow.up.forward.app") {
                                if let url = store.localFileURLs([file.path]).first { NSWorkspace.shared.open(url) }
                            }
                        }.controlSize(.small).disabled(store.localFileURLs([file.path]).isEmpty)
                        InspectorCode(title: L("Read-only preview", "只读预览"), content: file.content, numbered: true)
                    }
                }
            }.padding(16)
        }
    }

    private func specification(_ detail: NodeDetail) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text(L("Requirements", "需求")).font(.headline)
                ForEach(detail.requirements) { requirement in
                    VStack(alignment: .leading, spacing: 5) {
                        Text(requirement.id).font(.system(.headline, design: .monospaced)).foregroundStyle(.tint)
                        Text(requirement.text).textSelection(.enabled)
                    }
                }
                Divider()
                Text(L("Contract", "契约")).font(.headline)
                Text(detail.contract.isEmpty ? L("No contract supplied.", "暂无契约。") : detail.contract)
                    .font(.system(.callout, design: .monospaced)).textSelection(.enabled)
            }.frame(maxWidth: .infinity, alignment: .leading).padding(16)
        }
    }

    private func reveal(_ paths: [String]) { NSWorkspace.shared.activateFileViewerSelecting(store.localFileURLs(paths)) }
    private func empty(_ title: String) -> some View { Text(title).foregroundStyle(.secondary).frame(maxWidth: .infinity).padding(.vertical, 30) }
    private func copy(_ text: String) { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(text, forType: .string) }
    private func sendTitle(status: NodeStatus, rework: Bool) -> String {
        if status == .parked { return L("Send & Retry", "发送并重试") }
        if MessagePolicy.rework(status: status, requested: rework) { return L("Send & Rebuild", "发送并重做") }
        return L("Send", "发送")
    }
    private func diagnosisIcon(_ kind: String) -> String {
        switch kind {
        case "done": "checkmark.circle"
        case "running", "queued": "clock"
        case "waiting", "upstream": "arrow.triangle.branch"
        case "environment": "wrench.and.screwdriver"
        case "replanned": "arrow.triangle.swap"
        default: "exclamationmark.triangle"
        }
    }
}

private enum InspectorTab: String, CaseIterable, Identifiable {
    case conversation, attempts, tests, files, spec
    var id: String { rawValue }
    var title: String {
        switch self {
        case .conversation: L("Chat", "对话")
        case .attempts: L("Attempts", "尝试")
        case .tests: L("Tests", "测试")
        case .files: L("Files", "文件")
        case .spec: L("Spec", "契约")
        }
    }
}

private struct InspectorCode: View {
    var title: String
    var content: String
    var numbered = false
    var diff = false
    @State private var expanded = false
    private var lines: [String] { content.components(separatedBy: "\n") }
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(title).font(.caption.weight(.medium)).foregroundStyle(.secondary)
                Spacer()
                Button(L("Copy", "复制"), systemImage: "doc.on.doc") {
                    NSPasteboard.general.clearContents(); NSPasteboard.general.setString(content, forType: .string)
                }.buttonStyle(.borderless).font(.caption)
            }
            ScrollView(.horizontal) {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(lines.prefix(expanded ? lines.count : 2_000).enumerated()), id: \.offset) { index, line in
                        HStack(alignment: .top, spacing: 10) {
                            if numbered { Text(String(index + 1)).foregroundStyle(.tertiary).frame(width: 38, alignment: .trailing) }
                            Text(line.isEmpty ? " " : line).foregroundStyle(lineColor(line)).textSelection(.enabled)
                            Spacer(minLength: 0)
                        }.font(.system(size: 11, design: .monospaced))
                            .padding(.vertical, 1).frame(maxWidth: .infinity, alignment: .leading)
                            .background(lineBackground(line))
                    }
                }.padding(8)
            }.background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 6))
            if lines.count > 2_000 {
                Button(expanded ? L("Show first 2,000 lines", "显示前 2,000 行") : L("\(lines.count) lines · Show all", "共 \(lines.count) 行 · 展开全部")) { expanded.toggle() }
                    .font(.caption).buttonStyle(.link)
            }
        }
    }
    private func lineColor(_ line: String) -> Color {
        guard diff else { return .primary }
        if line.hasPrefix("+++") || line.hasPrefix("---") || line.hasPrefix("@@") { return .secondary }
        if line.hasPrefix("+") { return .green }
        if line.hasPrefix("-") { return .red }
        return .primary
    }
    private func lineBackground(_ line: String) -> Color {
        guard diff else { return .clear }
        if line.hasPrefix("+") && !line.hasPrefix("+++") { return .green.opacity(0.07) }
        if line.hasPrefix("-") && !line.hasPrefix("---") { return .red.opacity(0.07) }
        return .clear
    }
}
