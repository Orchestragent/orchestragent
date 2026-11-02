import Foundation

// API field names follow docs/STUDIO.md; unknown string values remain displayable.
struct Cost: Codable, Equatable { var usd: Double; var cny: Double }
struct Route: Codable, Identifiable, Equatable {
    var label: String; var provider: String; var model: String; var effort: String
    var vendor: String; var name: String; var verified: Bool
    var id: String { label }
}
struct RunSummary: Codable, Identifiable, Equatable {
    var id: String; var title: String; var status: String; var live: Bool
    var created: Double; var tasks: Int; var done: Int; var parked: Int; var cost: Cost
    var started: Double?; var elapsedS: Double?; var phase: String?
}
struct Boot: Codable {
    var api: Int?; var version: String; var repo: String; var repoName: String
    var platform: String; var demo: Bool; var runs: [RunSummary]
}
struct Diagnosis: Codable, Equatable {
    var kind: String; var summary: String; var blamed: [String]; var checks: [String]
}
struct Person: Codable, Equatable { var messages: Int; var taken: Bool }
enum NodeStatus: Equatable, Codable {
    case planned, ready, running, verifying, merging, done, parked, replanned, unknown(String)
    init(rawValue: String) {
        switch rawValue {
        case "PLANNED": self = .planned
        case "READY": self = .ready
        case "RUNNING": self = .running
        case "VERIFYING": self = .verifying
        case "MERGING": self = .merging
        case "DONE": self = .done
        case "PARKED": self = .parked
        case "REPLANNED": self = .replanned
        default: self = .unknown(rawValue)
        }
    }
    var rawValue: String {
        switch self {
        case .planned: "PLANNED"
        case .ready: "READY"
        case .running: "RUNNING"
        case .verifying: "VERIFYING"
        case .merging: "MERGING"
        case .done: "DONE"
        case .parked: "PARKED"
        case .replanned: "REPLANNED"
        case .unknown(let value): value
        }
    }
    init(from decoder: Decoder) throws { self.init(rawValue: try decoder.singleValueContainer().decode(String.self)) }
    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer(); try container.encode(rawValue)
    }
    var active: Bool { self == .running || self == .verifying || self == .merging }
}
struct StudioNode: Codable, Identifiable, Equatable {
    var id: String; var goal: String; var status: NodeStatus; var since: Double
    var attempts: Int; var rung: String; var route: Route; var routeOverride: Bool
    var files: [String]; var reqs: [String]; var dependsOn: [String]; var diagnosis: Diagnosis
    var cost: Cost; var person: Person; var ripple: String; var replacedBy: [String]
}
struct Edge: Codable, Hashable { var from: String; var to: String }
struct Graph: Codable { var run: RunSummary; var nodes: [StudioNode]; var edges: [Edge] }
struct OwnedFile: Codable, Identifiable {
    var path: String; var content: String; var state: String?
    var id: String { path }
}
struct FrozenCheck: Codable, Identifiable { var id: String; var command: String; var files: [OwnedFile] }
struct Requirement: Codable, Identifiable { var id: String; var text: String }
struct Attempt: Codable, Identifiable {
    var n: Int; var label: String; var route: Route; var rung: String; var started: Double
    var ended: Double?; var outcome: String; var category: String; var report: String
    var diff: String; var summary: String
    var id: Int { n }
}
struct ThreadItem: Codable, Identifiable {
    var id: String; var at: Double; var author: String; var kind: String
    var text: String; var attempt: Int?
}
struct NodeDetail: Codable {
    var node: StudioNode; var contract: String; var requirements: [Requirement]
    var files: [OwnedFile]; var checks: [FrozenCheck]; var attempts: [Attempt]
    var thread: [ThreadItem]; var routes: [Route]
}
struct ProviderLogin: Codable { var command: String; var then: String }
struct Provider: Codable, Identifiable {
    var name: String; var vendor: String; var title: String; var kind: String
    var configured: Bool; var detail: String; var login: ProviderLogin?
    var id: String { name }
}
struct Role: Codable, Identifiable {
    var role: String; var title: String; var description: String; var route: Route?; var defaultRoute: Route?
    var id: String { role }
    enum CodingKeys: String, CodingKey {
        case role, title, description, route
        case defaultRoute = "default"
    }
}
struct StudioSettings: Codable { var providers: [Provider]; var roles: [Role]; var routes: [Route] }
struct MachineHandshake: Codable { var url: URL; var token: String; var pid: Int; var version: String; var api: Int }

// Shared by the composer and its tests. An unknown state is deliberately read-only.
enum MessagePolicy {
    static func canSend(status: NodeStatus, cancelled: Bool) -> Bool {
        guard !cancelled else { return false }
        switch status {
        case .replanned, .unknown: return false
        default: return true
        }
    }
    static func rework(status: NodeStatus, requested: Bool) -> Bool { status == .done && requested }
}
