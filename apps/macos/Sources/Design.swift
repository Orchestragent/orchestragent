import SwiftUI

func L(_ english: String, _ chinese: String) -> String {
    let language = UserDefaults.standard.string(forKey: "language") ?? "system"
    let effective = language == "system" ? Locale.preferredLanguages.first ?? "en" : language
    if let path = Bundle.main.path(forResource: effective.hasPrefix("zh") ? "zh-Hans" : "en", ofType: "lproj"),
       let bundle = Bundle(path: path) {
        return bundle.localizedString(forKey: english, value: effective.hasPrefix("zh") ? chinese : english, table: nil)
    }
    return effective.hasPrefix("zh") ? chinese : english
}

enum Palette {
    static let accent = Color(red: 0.17, green: 0.43, blue: 0.40)
    static func status(_ status: NodeStatus) -> Color {
        switch status {
        case .planned: Color("Planned")
        case .ready: Color("Ready")
        case .running: Color("Running")
        case .verifying: Color("Verifying")
        case .merging: Color("Merging")
        case .done: Color("Done")
        case .parked: Color("Parked")
        case .replanned, .unknown: Color("Replanned")
        }
    }
    static func vendor(_ vendor: String) -> Color {
        switch vendor {
        case "anthropic": Color(red: 0.80, green: 0.40, blue: 0.28)
        case "deepseek": Color(red: 0.30, green: 0.42, blue: 0.95)
        case "openai": Color(red: 0.06, green: 0.57, blue: 0.44)
        case "cursor": Color.primary
        case "google": Color(red: 0.26, green: 0.52, blue: 0.96)
        case "moonshot": Color.indigo
        case "qwen": Color.purple
        case "person": Color.pink
        default: Color.secondary
        }
    }
    static func run(_ status: String) -> Color {
        switch status {
        case "done": statusColor(.done)
        case "parked", "failed": statusColor(.parked)
        case "running", "planning": statusColor(.running)
        default: .secondary
        }
    }
    private static func statusColor(_ state: NodeStatus) -> Color { status(state) }
}

func statusTitle(_ status: NodeStatus) -> String {
    switch status {
    case .planned: L("Planned", "等待")
    case .ready: L("Ready", "排队")
    case .running: L("Running", "生成中")
    case .verifying: L("Verifying", "验证中")
    case .merging: L("Merging", "合并中")
    case .done: L("Done", "已完成")
    case .parked: L("Parked", "待处理")
    case .replanned: L("Replanned", "已重新规划")
    case .unknown(let value): value
    }
}
func phaseTitle(_ phase: String) -> String {
    switch phase {
    case "planning": L("Planning", "规划中")
    case "test design": L("Designing tests", "设计测试")
    case "building": L("Building", "构建中")
    case "final gate": L("Final checks", "最终检查")
    case "finished": L("Finished", "已结束")
    default: phase.capitalized
    }
}
func rungTitle(_ rung: String) -> String {
    switch rung {
    case "first try": L("First try", "首次尝试")
    case "retry": L("Retry", "重试")
    case "samples": L("Samples", "多候选")
    case "Claude patch": L("Claude patch", "Claude 修复")
    case "thicken": L("Thicken contract", "补充契约")
    case "replan": L("Replan", "重新规划")
    default: rung
    }
}
struct StatusBadge: View {
    var status: NodeStatus
    var body: some View {
        HStack(spacing: 5) {
            Circle().fill(Palette.status(status)).frame(width: 6, height: 6)
            Text(statusTitle(status)).font(.system(size: 10, weight: .semibold))
        }
        .padding(.horizontal, 8).padding(.vertical, 4)
        .foregroundStyle(Palette.status(status)).background(Palette.status(status).opacity(0.1), in: Capsule())
    }
}
struct RouteChip: View {
    var route: Route
    var overridden: Bool = false
    var body: some View {
        HStack(spacing: 4) {
            if overridden { Image(systemName: "pin.fill").font(.system(size: 8)) }
            Circle().fill(Palette.vendor(route.vendor)).frame(width: 5, height: 5)
            Text(route.name).lineLimit(1)
        }.font(.system(size: 10, weight: .medium))
            .foregroundStyle(Palette.vendor(route.vendor))
            .padding(.horizontal, 7).padding(.vertical, 4)
            .background(Palette.vendor(route.vendor).opacity(0.08), in: RoundedRectangle(cornerRadius: 5))
            .help(route.label)
    }
}

func routeOrder(_ lhs: Route, _ rhs: Route) -> Bool {
    let efforts = ["": 0, "none": 0, "off": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5, "max": 6, "ultra": 7]
    let left = efforts[lhs.effort, default: 99], right = efforts[rhs.effort, default: 99]
    return left == right ? lhs.name.localizedStandardCompare(rhs.name) == .orderedAscending : left < right
}
