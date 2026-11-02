import Foundation

struct DispatchTask: Codable, Identifiable, Equatable {
    let id: String
    let requestID: String
    let prompt: String
    let scenario: String
    let status: String
    let stage: String
    let createdAt: Double
    let updatedAt: Double
    let provider: String
    let sessionID: String
    let attempt: Int
    let nextRunAt: Double
    let result: String
    let error: String
    let revision: String
    let costUSD: Double?
    let steps: [DispatchStep]
    let approval: Approval?
    let artifacts: [Artifact]
    var policyRevision: Int? = nil
    var mode: String? = nil
    var billing: String? = nil
    var usage: [String: [String: UsageValue]]? = nil
    var executionMode: String { mode ?? "mock" }

    enum CodingKeys: String, CodingKey {
        case id, prompt, scenario, status, stage, provider, attempt, result, error, revision, steps, approval, artifacts, mode, billing, usage
        case requestID = "request_id", createdAt = "created_at", updatedAt = "updated_at"
        case sessionID = "session_id", nextRunAt = "next_run_at", costUSD = "cost_usd"
        case policyRevision = "policy_revision"
    }

    var canResume: Bool {
        ["paused", "rate_limited", "quota_exhausted", "auth_required", "timed_out",
         "failed"].contains(status)
    }
    var canRetry: Bool { status == "failed" }
    var waitingManual: Bool { status == "waiting_manual" }
    var canPause: Bool { ["queued", "running", "rate_limited"].contains(status) }
    var isTerminal: Bool { ["completed", "cancelled", "failed"].contains(status) }
}

enum UsageValue: Codable, Equatable {
    case number(Double)
    case text(String)

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let value = try? container.decode(Double.self) { self = .number(value); return }
        self = .text((try? container.decode(String.self)) ?? "")
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .number(let value): try container.encode(value)
        case .text(let value): try container.encode(value)
        }
    }

    var int: Int? {
        if case .number(let value) = self { return Int(value) }
        return nil
    }
}

struct StepUsage {
    let input: Int
    let output: Int
    let cacheRead: Int
    let cacheWrite: Int
    let total: Int
    let estimateUSD: Double?
    let billing: String?
    let effort: String?

    init(_ raw: [String: UsageValue]) {
        input = raw["input_tokens"]?.int ?? 0
        output = raw["output_tokens"]?.int ?? 0
        cacheRead = raw["cache_read_input_tokens"]?.int ?? raw["cached_input_tokens"]?.int ?? 0
        cacheWrite = raw["cache_creation_input_tokens"]?.int ?? 0
        total = raw["total_tokens"]?.int ?? (input + output)
        if case .number(let value)? = raw["provider_estimate_usd"] { estimateUSD = value } else { estimateUSD = nil }
        if case .text(let value)? = raw["billing"] { billing = value } else { billing = nil }
        if case .text(let value)? = raw["effort"] { effort = value } else { effort = nil }
    }

    var hitRate: Double? {
        let denominator = input + cacheRead
        guard denominator > 0, cacheRead > 0 else { return nil }
        return Double(cacheRead) / Double(denominator)
    }
}

struct DispatchStep: Codable, Identifiable, Equatable {
    let id: String
    let role: String
    let dependsOn: [String]
    let status: String
    let summary: String
    let revision: String
    var provider: String? = nil
    var model: String? = nil
    var sessionID: String? = nil
    enum CodingKeys: String, CodingKey {
        case id, role, status, summary, revision, provider, model
        case dependsOn = "depends_on"
        case sessionID = "session_id"
    }
}

struct Approval: Codable, Identifiable, Equatable {
    let id: String
    let operation: String
    let operationDigest: String
    let revision: String
    let status: String
    enum CodingKeys: String, CodingKey {
        case id, operation, revision, status
        case operationDigest = "operation_digest"
    }
}

struct Artifact: Codable, Identifiable, Equatable {
    let name: String
    let content: String
    var id: String { name }
}

enum JSONValue: Decodable, Equatable {
    case string(String), number(Double), bool(Bool), object([String: JSONValue]), array([JSONValue]), null

    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if value.decodeNil() { self = .null }
        else if let item = try? value.decode(Bool.self) { self = .bool(item) }
        else if let item = try? value.decode(String.self) { self = .string(item) }
        else if let item = try? value.decode(Double.self) { self = .number(item) }
        else if let item = try? value.decode([String: JSONValue].self) { self = .object(item) }
        else { self = .array(try value.decode([JSONValue].self)) }
    }

    var display: String {
        switch self {
        case .string(let value): return value
        case .number(let value): return String(value)
        case .bool(let value): return value ? "true" : "false"
        case .null: return "null"
        case .array(let values): return values.map(\.display).joined(separator: ", ")
        case .object(let values): return values.keys.sorted().map { "\($0): \(values[$0]!.display)" }.joined(separator: " · ")
        }
    }
}

struct TaskEvent: Decodable, Identifiable {
    let seq: Int
    let taskID: String
    let kind: String
    let at: Double
    let data: [String: JSONValue]
    var id: Int { seq }
    var detail: String { JSONValue.object(data).display }
    enum CodingKeys: String, CodingKey {
        case seq, kind, at, data
        case taskID = "task_id"
    }
}

struct Submission: Codable, Equatable {
    let requestID: String
    let prompt: String
    let scenario: String
    let mode: String
    init(requestID: String = UUID().uuidString, prompt: String, scenario: String, mode: String = "mock") {
        self.requestID = requestID
        self.prompt = prompt
        self.scenario = scenario
        self.mode = scenario == "happy" ? mode : "mock"
    }
    enum CodingKeys: String, CodingKey {
        case prompt, scenario, mode
        case requestID = "request_id"
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        requestID = try values.decode(String.self, forKey: .requestID)
        prompt = try values.decode(String.self, forKey: .prompt)
        scenario = try values.decode(String.self, forKey: .scenario)
        let savedMode = try values.decodeIfPresent(String.self, forKey: .mode) ?? "mock"
        mode = scenario == "happy" ? savedMode : "mock"
    }
}

struct ServerHealth: Decodable {
    let mode: String
    let version: Int
}

struct ProviderReadiness: Decodable {
    let ready: Bool
    let auth: String?
    var message: String? = nil
}

enum Labels {
    static func status(_ value: String) -> String {
        ["queued": "排队中", "running": "执行中", "waiting_approval": "等待你批准",
         "rate_limited": "限流等待", "quota_exhausted": "配额耗尽", "auth_required": "需要认证",
         "policy_blocked": "接入受限", "timed_out": "执行超时", "failed": "执行失败", "paused": "已暂停",
         "cancelled": "已取消", "completed": "已完成", "pending": "待执行",
         "approved": "已批准", "denied": "已拒绝" ][value] ?? value
    }
    static func stage(_ value: String) -> String {
        ["plan": "规划", "implement": "实现", "review": "审查", "test": "测试", "summarize": "总结"][value] ?? value
    }
    static let scenarios: [(String, String)] = [
        ("happy", "顺利完成"), ("approval", "审批流程"), ("rate_limit", "请求限流"),
        ("quota", "配额不足"), ("auth", "认证异常"), ("timeout", "执行超时"),
        ("failure", "执行失败"), ("review_failure", "审查失败"), ("test_failure", "测试失败")
    ]
}
