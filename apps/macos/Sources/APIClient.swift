import Foundation

struct APIError: LocalizedError {
    let code: String
    let message: String
    let status: Int
    var errorDescription: String? { message }
}

struct SSEEvent: Equatable {
    let event: String
    let data: String
}

/// Stateful SSE framing, including multiline data, comments, and empty event resets.
struct SSEParser {
    private var event = ""
    private var data: [String] = []
    mutating func consume(_ line: String) -> SSEEvent? {
        let line = line.hasSuffix("\r") ? String(line.dropLast()) : line
        if line.isEmpty {
            defer { event = ""; data.removeAll(keepingCapacity: true) }
            guard !data.isEmpty else { return nil }
            return SSEEvent(event: event.isEmpty ? "message" : event, data: data.joined(separator: "\n"))
        }
        guard !line.hasPrefix(":") else { return nil }
        let parts = line.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
        let field = String(parts[0])
        var value = parts.count == 2 ? String(parts[1]) : ""
        if value.hasPrefix(" ") { value.removeFirst() }
        switch field {
        case "event": event = value
        case "data": data.append(value)
        default: break
        }
        return nil
    }
}

struct APIClient {
    let baseURL: URL
    private let token: String
    private let session: URLSession
    init(baseURL: URL, token: String, session: URLSession? = nil) {
        self.baseURL = baseURL
        self.token = token
        if let session { self.session = session } else {
            let configuration = URLSessionConfiguration.ephemeral
            configuration.timeoutIntervalForRequest = 30
            configuration.timeoutIntervalForResource = 24 * 60 * 60
            configuration.urlCache = nil
            self.session = URLSession(configuration: configuration)
        }
    }
    func invalidate() { session.invalidateAndCancel() }

    func get<T: Decodable>(_ path: String, query: [String: String] = [:]) async throws -> T {
        try await request(path, method: "GET", query: query)
    }
    func post<T: Decodable>(_ path: String, body: [String: Any]) async throws -> T {
        try await request(path, method: "POST", body: body)
    }
    private func makeRequest(_ path: String, method: String, query: [String: String]) throws -> URLRequest {
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false),
              ["http", "https"].contains(components.scheme?.lowercased() ?? ""), components.host != nil else {
            throw APIError(code: "invalid_url", message: L("Enter a valid HTTP or HTTPS server URL.", "请输入有效的 HTTP 或 HTTPS 服务地址。"), status: 0)
        }
        components.path = "/" + path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        components.queryItems = query.isEmpty ? nil : query.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) }
        components.fragment = nil
        guard let url = components.url else { throw URLError(.badURL) }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        return request
    }
    private func request<T: Decodable>(_ path: String, method: String, query: [String: String] = [:], body: [String: Any]? = nil) async throws -> T {
        var request = try makeRequest(path, method: method, query: query)
        if let body {
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        do { return try decoder.decode(T.self, from: data) } catch {
            throw APIError(code: "decode", message: L("The server returned an incompatible response. Check the orch version.", "服务响应与客户端不兼容，请检查 orch 版本。"), status: (response as? HTTPURLResponse)?.statusCode ?? 0)
        }
    }
    private func validate(_ response: URLResponse, data: Data) throws {
        guard let response = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        guard (200..<300).contains(response.statusCode) else {
            struct Envelope: Decodable { struct BackendError: Decodable { var code: String; var message: String }; var error: BackendError }
            let backend = try? JSONDecoder().decode(Envelope.self, from: data).error
            throw APIError(code: backend?.code ?? "http", message: backend?.message ?? L("Server request failed (HTTP \(response.statusCode)).", "服务请求失败（HTTP \(response.statusCode)）。"), status: response.statusCode)
        }
    }
    /// URLSession's inactivity timeout is reset by the server's ten-second pings.
    func events(run: String?, receive: (SSEEvent) async -> Void) async throws {
        var request = try makeRequest("api/events", method: "GET", query: run.map { ["run": $0] } ?? [:])
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        let (bytes, response) = try await session.bytes(for: request)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            var data = Data()
            for try await byte in bytes { data.append(byte); if data.count >= 16_384 { break } }
            try validate(response, data: data)
        }
        var parser = SSEParser()
        var line = Data()
        // AsyncBytes.lines omits blank lines on supported Foundation versions.
        // SSE blank lines delimit frames, so preserve them explicitly.
        for try await byte in bytes {
            try Task.checkCancellation()
            if byte == 10 {
                if let event = parser.consume(String(decoding: line, as: UTF8.self)) { await receive(event) }
                line.removeAll(keepingCapacity: true)
            } else {
                line.append(byte)
                guard line.count <= 1_048_576 else { throw APIError(code: "event_too_large", message: L("The server sent an oversized event.", "服务器事件过大。"), status: 0) }
            }
        }
        throw URLError(.networkConnectionLost)
    }
}
