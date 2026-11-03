import SwiftUI
import AppKit

@MainActor
struct GraphCanvas: View {
    @Bindable var store: AppStore
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var colorScheme
    @AppStorage("animationsOff") private var animationsOff = false
    @State private var positions: [String: CGPoint] = [:]
    @State private var zoom: CGFloat = 1
    @State private var pan: CGSize = .zero
    @State private var panStart: CGSize?
    @State private var dragStarts: [String: CGPoint] = [:]
    @State private var magnifyStart: CGFloat?
    @State private var viewport: CGSize = .zero
    @State private var minimapExpanded = true
    @State private var legendExpanded = false
    @State private var persistenceError: String?
    @FocusState private var canvasFocused: Bool

    private var nodes: [StudioNode] { store.graph?.nodes ?? [] }
    private var motion: Bool { !reduceMotion && !animationsOff && nodes.count <= 60 }
    private var selected: StudioNode? { nodes.first { $0.id == store.selectedNodeID } }
    private var blamed: Set<String> { Set(selected?.diagnosis.blamed ?? []) }
    private var layoutIdentity: String { "\(store.boot?.repo ?? "")|\(store.graph?.run.id ?? "")" }

    var body: some View {
        GeometryReader { geometry in
            ZStack {
                canvasBackground
                if nodes.isEmpty {
                    emptyState
                } else {
                    edges(size: geometry.size)
                        .allowsHitTesting(false)
                    ForEach(nodes) { node in
                        nodeCard(node, size: geometry.size)
                    }
                }
                VStack(spacing: 0) {
                    canvasHeader
                    Spacer()
                    HStack(alignment: .bottom, spacing: 16) {
                        VStack(alignment: .leading, spacing: 8) {
                            legend
                            controls
                        }
                        Spacer(minLength: 8)
                        minimap(size: geometry.size)
                    }
                    .padding(20)
                }
                .allowsHitTesting(true)
            }
            .clipped()
            .background(GraphScrollInput(onPan: { delta in
                pan.width += delta.width
                pan.height += delta.height
            }, onZoom: { factor in changeZoom(zoom * factor) }))
            .focusable()
            .focusEffectDisabled()
            .focused($canvasFocused)
            .onKeyPress { key in handleKey(key) }
            .simultaneousGesture(MagnifyGesture().onChanged { value in
                if magnifyStart == nil { magnifyStart = zoom }
                changeZoom((magnifyStart ?? zoom) * value.magnification)
            }.onEnded { _ in magnifyStart = nil })
            .onAppear { viewport = geometry.size; synchronize(reset: true) }
            .onChange(of: geometry.size) { previous, size in
                viewport = size
                if previous.width < 120 || previous.height < 200 { fit() }
            }
            .onChange(of: layoutIdentity) { _, _ in synchronize(reset: true) }
            .onChange(of: nodes.map(\.id)) { _, _ in synchronize(reset: false) }
            .onChange(of: store.selectedNodeID) { _, id in
                if let id, let point = positions[id], !visible(point) {
                    withAnimation(motion ? .easeInOut(duration: 0.25) : nil) {
                        pan = CGSize(width: -point.x * zoom, height: -point.y * zoom)
                    }
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: .init("studioZoomIn"))) { _ in changeZoom(zoom * 1.2) }
            .onReceive(NotificationCenter.default.publisher(for: .init("studioZoomOut"))) { _ in changeZoom(zoom / 1.2) }
            .onReceive(NotificationCenter.default.publisher(for: .init("studioFit"))) { _ in fit() }
            .onReceive(NotificationCenter.default.publisher(for: .init("studioRelayout"))) { _ in relayout() }
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("task-graph")
        }
    }

    private var canvasBackground: some View {
        Canvas { context, size in
            let fill = colorScheme == .dark ? Color(red: 0.105, green: 0.112, blue: 0.12) : Color(red: 0.968, green: 0.963, blue: 0.947)
            context.fill(Path(CGRect(origin: .zero, size: size)), with: .color(fill))
            let step = max(12, 20 * zoom)
            let origin = CGPoint(x: (size.width / 2 + pan.width).truncatingRemainder(dividingBy: step),
                                 y: (size.height / 2 + pan.height).truncatingRemainder(dividingBy: step))
            var dots = Path()
            for x in stride(from: origin.x - step, through: size.width, by: step) {
                for y in stride(from: origin.y - step, through: size.height, by: step) {
                    dots.addEllipse(in: CGRect(x: x, y: y, width: 1.5, height: 1.5))
                }
            }
            context.fill(dots, with: .color(.secondary.opacity(colorScheme == .dark ? 0.22 : 0.16)))
        }
        .contentShape(Rectangle())
        .onTapGesture { canvasFocused = true; Task { await store.selectNode(nil) } }
        .gesture(DragGesture(minimumDistance: 3).onChanged { value in
            canvasFocused = true
            if panStart == nil { panStart = pan }
            pan = CGSize(width: (panStart?.width ?? 0) + value.translation.width,
                         height: (panStart?.height ?? 0) + value.translation.height)
        }.onEnded { _ in panStart = nil })
    }

    private var canvasHeader: some View {
        HStack(alignment: .top) {
            HStack(spacing: 7) {
                Text(L("Task graph", "任务图")).font(.system(size: 15, weight: .semibold))
                if store.boot?.demo == true {
                    Text("DEMO").font(.system(size: 9, weight: .bold, design: .monospaced))
                        .padding(.horizontal, 6).padding(.vertical, 3).background(.orange.opacity(0.12), in: Capsule()).foregroundStyle(.orange)
                }
            }
            Spacer()
            if let persistenceError {
                Label(L("Layout not saved", "布局未保存"), systemImage: "exclamationmark.triangle")
                    .font(.caption).foregroundStyle(.orange).help(persistenceError)
            }
            if !nodes.isEmpty {
                Text("\(nodes.count) " + L("nodes", "节点"))
                    .font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary)
                    .padding(.horizontal, 10).padding(.vertical, 6).background(.background.opacity(0.8), in: Capsule())
            }
        }
        .padding(22)
        .allowsHitTesting(persistenceError != nil)
    }

    @ViewBuilder private var emptyState: some View {
        VStack(spacing: 16) {
            Image(systemName: "point.3.connected.trianglepath.dotted")
                .font(.system(size: 38, weight: .ultraLight)).foregroundStyle(.secondary)
            Text(store.graph?.run.status == "planning" ? L("Planning the path ahead", "正在规划任务路径") : L("A clear view of the work ahead", "清晰掌握接下来的工作"))
                .font(.system(size: 24, weight: .medium, design: .serif))
            Text(store.graph?.run.status == "planning" ? L("Opus is breaking your requirements into focused tasks…", "Opus 正在将需求拆分为独立任务…") : L("Select a run, or start with a new requirement.", "选择一次运行，或从新需求开始。"))
                .font(.callout).foregroundStyle(.secondary)
            if store.graph?.run.status == "planning" {
                HStack(spacing: 8) {
                    ForEach(0..<3) { index in
                        RoundedRectangle(cornerRadius: 3).fill(.secondary.opacity(0.12 + Double(index) * 0.035)).frame(width: 66, height: 6)
                    }
                }
                .accessibilityLabel(L("Planning in progress", "规划进行中"))
            } else if store.selectedRunID != nil && store.graph == nil {
                HStack(spacing: 12) {
                    ForEach(0..<3) { _ in RoundedRectangle(cornerRadius: 12).fill(.secondary.opacity(0.08)).frame(width: 100, height: 50) }
                }
                .accessibilityLabel(L("Loading graph", "正在加载任务图"))
            }
        }
        .padding(40)
        .allowsHitTesting(false)
    }

    private func nodeCard(_ node: StudioNode, size: CGSize) -> some View {
        GraphNodeCard(node: node, selected: node.id == store.selectedNodeID, blamed: blamed.contains(node.id), motion: motion)
            .frame(width: 240, height: 104)
            .scaleEffect(zoom)
            .position(screen(positions[node.id] ?? .zero, size: size))
            .onTapGesture { canvasFocused = true; Task { await store.selectNode(node.id) } }
            .onTapGesture(count: 2) { open(node.id) }
            .gesture(DragGesture(minimumDistance: 3).onChanged { value in
                canvasFocused = true
                if dragStarts[node.id] == nil { dragStarts[node.id] = positions[node.id] ?? .zero }
                let start = dragStarts[node.id] ?? .zero
                var point = CGPoint(x: start.x + value.translation.width / zoom, y: start.y + value.translation.height / zoom)
                if NSEvent.modifierFlags.contains(.shift) {
                    point.x = (point.x / 20).rounded() * 20
                    point.y = (point.y / 20).rounded() * 20
                }
                positions[node.id] = point
            }.onEnded { _ in dragStarts[node.id] = nil; persist() })
            .contextMenu { nodeMenu(node) }
            .focusable()
            .focusEffectDisabled()
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("\(node.id), \(node.goal), \(statusTitle(node.status)), \(node.diagnosis.summary), \(node.route.name)")
            .accessibilityHint(L("Open to inspect this task", "打开以检查此任务"))
            .accessibilityAddTraits(.isButton)
            .accessibilityAction { open(node.id) }
    }

    @ViewBuilder private func nodeMenu(_ node: StudioNode) -> some View {
        Button(L("Open", "打开")) { open(node.id) }
        Button(L("Message…", "发送消息…")) {
            Task {
                await store.selectNode(node.id)
                store.inspectorVisible = true
                store.focusComposerRequested = true
            }
        }
        Divider()
        Button(L("Retry", "重试")) {
            let run = store.selectedRunID
            Task {
                await store.selectNode(node.id)
                guard store.selectedRunID == run, store.selectedNodeID == node.id else { return }
                await store.retryNode()
            }
        }.disabled(!store.connected || node.status != .parked)
        Menu(L("Switch Model", "切换模型")) {
            Button(L("Role default", "角色默认模型")) { switchRoute("", node: node.id) }
            ForEach(availableRoutes(for: node)) { route in
                Button(route.name + (route.verified ? "" : " · " + L("unverified", "未验证"))) { switchRoute(route.label, node: node.id) }
            }
        }.disabled(!store.connected || node.status == .replanned || store.graph?.run.status == "cancelled")
        Divider()
        Button(L("Copy Node ID", "复制节点编号")) { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(node.id, forType: .string) }
        Button(L("Reveal Files in Finder", "在访达中显示文件")) { reveal(node) }
            .disabled(store.localFileURLs(node.files).isEmpty)
    }

    private func availableRoutes(for node: StudioNode) -> [Route] {
        if store.detail?.node.id == node.id { return (store.detail?.routes ?? []).sorted(by: routeOrder) }
        return (store.settings?.routes ?? []).sorted(by: routeOrder)
    }

    private func switchRoute(_ label: String, node: String) {
        let run = store.selectedRunID
        Task {
            await store.selectNode(node)
            guard store.selectedRunID == run, store.selectedNodeID == node else { return }
            await store.setNodeRoute(label)
        }
    }

    private func reveal(_ node: StudioNode) {
        let urls = store.localFileURLs(node.files)
        guard !urls.isEmpty else { return }
        NSWorkspace.shared.activateFileViewerSelecting(urls)
    }

    private func edges(size: CGSize) -> some View {
        TimelineView(.animation(minimumInterval: 1 / 30, paused: !motion || blamed.isEmpty)) { timeline in
            Canvas { context, _ in
                let lookup = Dictionary(nodes.map { ($0.id, $0.status) }, uniquingKeysWith: { first, _ in first })
                for edge in store.graph?.edges ?? [] {
                    guard let start = positions[edge.from], let end = positions[edge.to] else { continue }
                    let from = screen(CGPoint(x: start.x + 120, y: start.y), size: size)
                    let to = screen(CGPoint(x: end.x - 120, y: end.y), size: size)
                    let implicated = blamed.contains(edge.from) || blamed.contains(edge.to)
                    let highlighted = edge.from == store.selectedNodeID || edge.to == store.selectedNodeID || implicated
                    let color: Color = implicated ? .red : highlighted ? .accentColor : .secondary.opacity(0.38)
                    let tangent = max(40 * zoom, abs(to.x - from.x) * 0.5)
                    var path = Path()
                    path.move(to: from)
                    path.addCurve(to: to, control1: CGPoint(x: from.x + tangent, y: from.y), control2: CGPoint(x: to.x - tangent, y: to.y))
                    let dashed = lookup[edge.from] != .done || implicated
                    let phase = implicated && motion ? -CGFloat(timeline.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 10)) * 16 : 0
                    context.stroke(path, with: .color(color), style: StrokeStyle(lineWidth: (highlighted ? 2.4 : 1.4) * zoom,
                                   lineCap: .round, dash: dashed ? [5 * zoom, 4 * zoom] : [], dashPhase: phase))
                    var arrow = Path()
                    arrow.move(to: CGPoint(x: to.x - 7 * zoom, y: to.y - 4 * zoom))
                    arrow.addLine(to: to)
                    arrow.addLine(to: CGPoint(x: to.x - 7 * zoom, y: to.y + 4 * zoom))
                    context.stroke(arrow, with: .color(color), style: StrokeStyle(lineWidth: 1.6 * zoom, lineCap: .round, lineJoin: .round))
                }
            }
        }
    }

    private var controls: some View {
        HStack(spacing: 0) {
            Button { changeZoom(zoom / 1.2) } label: { Image(systemName: "minus").frame(width: 30, height: 30) }.help(L("Zoom out · ⌘−", "缩小 · ⌘−"))
            Text(zoom, format: .percent.precision(.fractionLength(0))).font(.system(size: 10, design: .monospaced)).frame(width: 42)
            Button { changeZoom(zoom * 1.2) } label: { Image(systemName: "plus").frame(width: 30, height: 30) }.help(L("Zoom in · ⌘=", "放大 · ⌘="))
            Divider().frame(height: 15).padding(.horizontal, 4)
            Button { fit() } label: { Image(systemName: "arrow.up.left.and.arrow.down.right").frame(width: 32, height: 30) }.help(L("Fit · ⌘0", "适配窗口 · ⌘0"))
            Button { relayout() } label: { Image(systemName: "square.3.layers.3d").frame(width: 32, height: 30) }.help(L("Relayout · ⇧⌘L", "重新布局 · ⇧⌘L"))
        }
        .buttonStyle(.plain).foregroundStyle(.secondary)
        .padding(4).background(.regularMaterial, in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(.primary.opacity(0.08)))
        .disabled(nodes.isEmpty)
    }

    private var legend: some View {
        VStack(alignment: .leading, spacing: 10) {
            Button { legendExpanded.toggle() } label: {
                HStack(spacing: 7) {
                    Image(systemName: "circle.grid.2x2"); Text(L("Legend", "图例")); Image(systemName: legendExpanded ? "chevron.down" : "chevron.up").font(.system(size: 8, weight: .bold))
                }.font(.system(size: 11, weight: .medium))
            }.buttonStyle(.plain).foregroundStyle(.secondary)
            if legendExpanded {
                LazyVGrid(columns: [GridItem(.fixed(85), alignment: .leading), GridItem(.fixed(85), alignment: .leading)], alignment: .leading, spacing: 8) {
                    ForEach([NodeStatus.planned, .ready, .running, .verifying, .merging, .done, .parked, .replanned], id: \.rawValue) { status in
                        HStack(spacing: 6) { Circle().fill(Palette.status(status)).frame(width: 6, height: 6); Text(statusTitle(status)).font(.system(size: 10)) }
                    }
                }
                Divider()
                ForEach(Array(Set(nodes.map { $0.route.vendor })).sorted(), id: \.self) { vendor in
                    HStack(spacing: 6) { Circle().fill(Palette.vendor(vendor)).frame(width: 6, height: 6); Text(vendor.capitalized).font(.system(size: 10)) }
                }
            }
        }
        .padding(12).background(.regularMaterial, in: RoundedRectangle(cornerRadius: 10))
    }

    private func minimap(size: CGSize) -> some View {
        VStack(alignment: .trailing, spacing: 8) {
            Button { minimapExpanded.toggle() } label: {
                Label(L("Overview", "概览"), systemImage: minimapExpanded ? "chevron.down" : "map")
                    .font(.system(size: 10, weight: .medium))
            }.buttonStyle(.plain).foregroundStyle(.secondary)
            if minimapExpanded && !nodes.isEmpty {
                let bounds = GraphLayout.bounds(positions).insetBy(dx: -40, dy: -40)
                let scale = min(180 / max(bounds.width, 1), 105 / max(bounds.height, 1))
                Canvas { context, _ in
                    for node in nodes {
                        guard let point = positions[node.id] else { continue }
                        let rect = CGRect(x: (point.x - 120 - bounds.minX) * scale, y: (point.y - 52 - bounds.minY) * scale,
                                          width: 240 * scale, height: 104 * scale)
                        let path = Path(roundedRect: rect, cornerRadius: 2)
                        context.fill(path, with: .color(Palette.status(node.status).opacity(0.8)))
                        if node.id == store.selectedNodeID { context.stroke(path, with: .color(.primary), lineWidth: 1.5) }
                    }
                    let visible = CGRect(x: (-size.width / 2 - pan.width) / zoom, y: (-size.height / 2 - pan.height) / zoom,
                                         width: size.width / zoom, height: size.height / zoom)
                    let rect = CGRect(x: (visible.minX - bounds.minX) * scale, y: (visible.minY - bounds.minY) * scale,
                                      width: visible.width * scale, height: visible.height * scale)
                    context.stroke(Path(rect), with: .color(.primary.opacity(0.5)), lineWidth: 1)
                }
                .frame(width: 180, height: 105).clipped().contentShape(Rectangle())
                .gesture(DragGesture(minimumDistance: 0).onChanged { value in
                    let world = CGPoint(x: value.location.x / scale + bounds.minX, y: value.location.y / scale + bounds.minY)
                    pan = CGSize(width: -world.x * zoom, height: -world.y * zoom)
                })
                .accessibilityLabel(L("Graph overview. Click to navigate.", "任务图概览，点击以导航。"))
            }
        }
        .padding(12).background(.regularMaterial, in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(.primary.opacity(0.06)))
    }

    private func screen(_ point: CGPoint, size: CGSize) -> CGPoint {
        CGPoint(x: size.width / 2 + pan.width + point.x * zoom, y: size.height / 2 + pan.height + point.y * zoom)
    }

    private func visible(_ point: CGPoint) -> Bool {
        let center = screen(point, size: viewport)
        return CGRect(origin: .zero, size: viewport).insetBy(dx: 130, dy: 80).contains(center)
    }

    private func changeZoom(_ value: CGFloat) {
        let bounded = min(2, max(0.25, value))
        let ratio = bounded / zoom
        pan = CGSize(width: pan.width * ratio, height: pan.height * ratio)
        zoom = bounded
    }

    private func fit() {
        guard !positions.isEmpty, viewport.width > 120, viewport.height > 200 else { return }
        let bounds = GraphLayout.bounds(positions)
        withAnimation(motion ? .easeInOut(duration: 0.3) : nil) {
            zoom = min(2, max(0.25, min(max(120, viewport.width - 120) / bounds.width,
                                      max(100, viewport.height - 200) / bounds.height)))
            pan = CGSize(width: -bounds.midX * zoom, height: -bounds.midY * zoom)
        }
    }

    private func relayout() {
        withAnimation(motion ? .spring(duration: 0.4) : nil) { positions = GraphLayout.positions(nodes: nodes) }
        fit(); persist()
    }

    private func synchronize(reset: Bool) {
        let defaults = GraphLayout.positions(nodes: nodes)
        if reset {
            persistenceError = nil
            let loaded = CanvasLayoutPersistence.load(repo: store.boot?.repo ?? "", run: store.graph?.run.id ?? "")
            positions = defaults.merging(loaded.filter { defaults[$0.key] != nil }) { _, saved in saved }
        } else {
            positions = positions.filter { defaults[$0.key] != nil }
            for node in nodes where positions[node.id] == nil {
                if let replaced = nodes.first(where: { $0.replacedBy.contains(node.id) }), let point = positions[replaced.id] {
                    let index = replaced.replacedBy.firstIndex(of: node.id) ?? 0
                    positions[node.id] = CGPoint(x: point.x + 40, y: point.y + CGFloat(index + 1) * 140)
                } else { positions[node.id] = defaults[node.id] }
            }
        }
        if reset { fit() } else { persist() }
    }

    private func persist() {
        guard let run = store.graph?.run.id, let repo = store.boot?.repo else { return }
        do { try CanvasLayoutPersistence.save(positions, repo: repo, run: run); persistenceError = nil }
        catch { persistenceError = error.localizedDescription }
    }

    private func open(_ id: String) {
        canvasFocused = true
        Task { await store.selectNode(id); store.inspectorVisible = true }
    }

    private func handleKey(_ key: KeyPress) -> KeyPress.Result {
        if key.key == .escape {
            if store.inspectorVisible { store.inspectorVisible = false }
            else { Task { await store.selectNode(nil) } }
            return .handled
        }
        if key.key == .return, let id = store.selectedNodeID { open(id); return .handled }
        let ids = nodes.map(\.id).sorted()
        guard !ids.isEmpty else { return .ignored }
        if key.key == .tab {
            let increment = key.modifiers.contains(.shift) ? -1 : 1
            let current = store.selectedNodeID.flatMap { ids.firstIndex(of: $0) } ?? (increment > 0 ? -1 : 0)
            let next = ids[(current + increment + ids.count) % ids.count]
            Task { await store.selectNode(next) }
            return .handled
        }
        let direction: CGPoint
        switch key.key {
        case .leftArrow: direction = CGPoint(x: -1, y: 0)
        case .rightArrow: direction = CGPoint(x: 1, y: 0)
        case .upArrow: direction = CGPoint(x: 0, y: -1)
        case .downArrow: direction = CGPoint(x: 0, y: 1)
        default: return .ignored
        }
        guard let id = store.selectedNodeID, let origin = positions[id] else {
            Task { await store.selectNode(ids[0]) }; return .handled
        }
        var nearest: String?
        var bestDistance = CGFloat.greatestFiniteMagnitude
        // Explicit scalar steps also keep Swift 5.10's type checker bounded.
        for candidate in ids where candidate != id {
            guard let point = positions[candidate] else { continue }
            let dx: CGFloat = point.x - origin.x
            let dy: CGFloat = point.y - origin.y
            guard dx * direction.x + dy * direction.y > 0 else { continue }
            let distance: CGFloat = dx * dx + dy * dy
            if distance < bestDistance {
                bestDistance = distance
                nearest = candidate
            }
        }
        if let nearest { Task { await store.selectNode(nearest) } }
        return .handled
    }
}

@MainActor
private struct GraphNodeCard: View {
    let node: StudioNode
    let selected: Bool
    let blamed: Bool
    let motion: Bool
    @State private var pulse = false
    @State private var rippleOpacity: Double = 0
    @State private var showUnaffected = true

    private var rippleColor: Color { node.ripple == "affected" ? .orange : .green }
    private var diagnostic: Bool { !["done", "running"].contains(node.diagnosis.kind) }

    var body: some View {
        HStack(spacing: 0) {
            Rectangle().fill(Palette.status(node.status)).frame(width: 4)
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    Text(node.id).font(.system(size: 11, weight: .bold, design: .monospaced)).foregroundStyle(.primary)
                    Text(node.goal).font(.system(size: 11, weight: .medium)).lineLimit(1).foregroundStyle(.primary.opacity(0.8))
                    Spacer(minLength: 0)
                    if node.person.taken { Image(systemName: "person.fill").foregroundStyle(Palette.vendor("person")) }
                    else if node.person.messages > 0 {
                        Label("\(node.person.messages)", systemImage: "bubble.left.fill").font(.system(size: 9)).foregroundStyle(Palette.vendor("person"))
                    }
                }
                HStack(spacing: 5) {
                    Circle().fill(Palette.status(node.status)).frame(width: 5, height: 5).opacity(node.status.active && motion && pulse ? 0.35 : 1)
                    Text(statusTitle(node.status).uppercased()).font(.system(size: 9, weight: .bold, design: .monospaced)).foregroundStyle(Palette.status(node.status))
                    Text("· \(rungTitle(node.rung)) · \(node.attempts) " + L("attempts", "次尝试")).font(.system(size: 9)).foregroundStyle(.secondary).lineLimit(1)
                }
                HStack(spacing: 4) {
                    if node.ripple == "affected" {
                        Image(systemName: "arrow.triangle.branch").foregroundStyle(.orange)
                        Text(L("Upstream changed · rebuilding", "上游变更 · 重做中")).foregroundStyle(.orange)
                    } else if node.ripple == "unaffected" && showUnaffected {
                        Image(systemName: "checkmark.circle").foregroundStyle(.green)
                        Text(L("Upstream changed · still passes", "上游变更 · 仍通过")).foregroundStyle(.green)
                    } else if diagnostic {
                        Image(systemName: node.status == .parked ? "exclamationmark.triangle" : "arrow.turn.down.right").foregroundStyle(Palette.status(node.status))
                        Text(node.diagnosis.summary).foregroundStyle(.secondary)
                    } else {
                        Text(node.status == .verifying ? L("Verifying frozen tests…", "正在验证冻结测试…") : node.status == .merging ? L("Merging changes…", "正在合并改动…") : node.diagnosis.summary).foregroundStyle(.secondary)
                    }
                }.font(.system(size: 9)).lineLimit(1).frame(height: 11)
                HStack(spacing: 4) {
                    RouteChip(route: node.route, overridden: node.routeOverride)
                        .lineLimit(1).frame(maxWidth: .infinity, alignment: .leading)
                    Text(String(format: "$%.2f ¥%.2f", node.cost.usd, node.cost.cny))
                        .font(.system(size: 8, weight: .medium, design: .monospaced)).foregroundStyle(.secondary).fixedSize()
                }
            }
            .padding(.horizontal, 11).padding(.vertical, 10)
        }
        .background(.background, in: RoundedRectangle(cornerRadius: 12))
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(.primary.opacity(selected ? 0 : 0.1), lineWidth: 1))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(selected ? Color.accentColor : .clear, lineWidth: 2))
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(blamed ? .red : .clear, style: StrokeStyle(lineWidth: 2, dash: [5, 4])).padding(-3))
        .overlay(RoundedRectangle(cornerRadius: 13).strokeBorder(rippleColor.opacity(rippleOpacity), lineWidth: 2).padding(-1))
        .shadow(color: .black.opacity(selected ? 0.1 : 0.035), radius: selected ? 12 : 5, y: 3)
        .opacity(node.status == .replanned ? 0.55 : 1)
        .help(node.goal + "\n" + node.diagnosis.summary)
        .task(id: "\(node.status.rawValue)-\(motion)") {
            pulse = false
            if node.status.active && motion {
                withAnimation(.easeInOut(duration: 1.2).repeatForever(autoreverses: true)) { pulse = true }
            }
        }
        .task(id: "\(node.ripple)-\(node.since)-\(motion)") {
            showUnaffected = true
            rippleOpacity = 0
            guard !node.ripple.isEmpty else { return }
            if motion {
                for _ in 0..<(node.ripple == "affected" ? 3 : 1) {
                    guard !Task.isCancelled else { return }
                    withAnimation(.easeInOut(duration: 0.25)) { rippleOpacity = 1 }
                    try? await Task.sleep(for: .milliseconds(300))
                    guard !Task.isCancelled else { return }
                    withAnimation(.easeInOut(duration: 0.25)) { rippleOpacity = 0 }
                    try? await Task.sleep(for: .milliseconds(300))
                }
            }
            if node.ripple == "unaffected" {
                try? await Task.sleep(for: .seconds(10))
                guard !Task.isCancelled else { return }
                withAnimation(motion ? .easeOut(duration: 0.4) : nil) { showUnaffected = false }
            }
        }
    }
}

/// Keeps native trackpad momentum and command-wheel zoom within the canvas bounds.
/// Returning events outside this view leaves inspector and sidebar scrolling alone.
@MainActor
private struct GraphScrollInput: NSViewRepresentable {
    var onPan: (CGSize) -> Void
    var onZoom: (CGFloat) -> Void

    func makeNSView(context: Context) -> InputView { InputView(onPan: onPan, onZoom: onZoom) }
    func updateNSView(_ nsView: InputView, context: Context) { nsView.onPan = onPan; nsView.onZoom = onZoom }
    static func dismantleNSView(_ nsView: InputView, coordinator: ()) { nsView.removeMonitor() }

    final class InputView: NSView {
        var onPan: (CGSize) -> Void
        var onZoom: (CGFloat) -> Void
        private var monitor: Any?
        init(onPan: @escaping (CGSize) -> Void, onZoom: @escaping (CGFloat) -> Void) {
            self.onPan = onPan; self.onZoom = onZoom
            super.init(frame: .zero)
            monitor = NSEvent.addLocalMonitorForEvents(matching: [.scrollWheel]) { [weak self] event in
                guard let self, let window = self.window, event.window === window,
                      self.bounds.contains(self.convert(event.locationInWindow, from: nil)) else { return event }
                if event.modifierFlags.contains(.command) {
                    self.onZoom(exp(-event.scrollingDeltaY * 0.012))
                } else {
                    let multiplier: CGFloat = event.hasPreciseScrollingDeltas ? 1 : 10
                    self.onPan(CGSize(width: -event.scrollingDeltaX * multiplier, height: -event.scrollingDeltaY * multiplier))
                }
                return nil
            }
        }
        required init?(coder: NSCoder) { nil }
        override func hitTest(_ point: NSPoint) -> NSView? { nil }
        func removeMonitor() { if let monitor { NSEvent.removeMonitor(monitor) }; monitor = nil }
    }
}
