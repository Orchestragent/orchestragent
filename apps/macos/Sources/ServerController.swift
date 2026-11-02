import Foundation
import Observation
import Darwin

@MainActor @Observable
final class ServerController {
    private(set) var executablePath: String?
    private(set) var version: String?
    private(set) var logs: [String] = []
    private(set) var isRunning = false
    var ownedProcessID: Int32? { process?.isRunning == true ? process?.processIdentifier : nil }
    let logURL: URL
    @ObservationIgnored private var process: Process?
    @ObservationIgnored private var input: Pipe?
    @ObservationIgnored private var output: Pipe?
    @ObservationIgnored private var errors: Pipe?
    @ObservationIgnored private var outputBuffer = Data()
    @ObservationIgnored private var errorBuffer = Data()
    @ObservationIgnored private var handshake: MachineHandshake?
    @ObservationIgnored private var launchError: Error?
    @ObservationIgnored private var secret = ""
    @ObservationIgnored private var launchID = UUID()

    init() {
        let directory = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/OrchStudio", isDirectory: true)
        logURL = directory.appendingPathComponent("server.log")
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    static func discoverExecutable() async -> String? {
        let manager = FileManager.default
        let explicit = ProcessInfo.processInfo.environment["ORCH_STUDIO_EXECUTABLE"] ?? UserDefaults.standard.string(forKey: "orchPath")
        if let explicit, !explicit.isEmpty {
            let expanded = (explicit as NSString).expandingTildeInPath
            if manager.isExecutableFile(atPath: expanded) { return expanded }
        }
        let shellPath: String? = await Task.detached {
            let shell = Process(); let pipe = Pipe()
            shell.executableURL = URL(fileURLWithPath: "/bin/zsh")
            shell.arguments = ["-lc", "command -v orch"]
            shell.standardOutput = pipe; shell.standardError = FileHandle.nullDevice
            do {
                try shell.run()
                // A shell startup file must not keep the connection screen waiting forever.
                let deadline = Date().addingTimeInterval(3)
                while shell.isRunning && Date() < deadline { try? await Task.sleep(for: .milliseconds(20)) }
                if shell.isRunning { shell.terminate(); return nil }
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                return String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
            } catch { return nil }
        }.value
        let candidates = [shellPath, "/opt/homebrew/bin/orch", "/usr/local/bin/orch", manager.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin/orch").path]
        return candidates.compactMap { $0 }.first { !$0.isEmpty && manager.isExecutableFile(atPath: $0) }
    }

    func start(repo: String?, demo: Bool) async throws -> MachineHandshake {
        shutdown()
        let id = UUID(); launchID = id
        handshake = nil; launchError = nil; logs = []; outputBuffer = Data(); errorBuffer = Data(); secret = ""
        guard let path = await Self.discoverExecutable() else {
            throw APIError(code: "orch_missing", message: L("orch was not found. Install it with: brew install Orchestragent/tap/orch, or choose its path in Settings.", "未找到 orch。请运行 brew install Orchestragent/tap/orch 安装，或在设置中选择路径。"), status: 0)
        }
        guard launchID == id, !Task.isCancelled else { throw CancellationError() }
        executablePath = path
        let child = Process(); let stdinPipe = Pipe(); let stdoutPipe = Pipe(); let stderrPipe = Pipe()
        child.executableURL = URL(fileURLWithPath: path)
        // --repo is a global CLI option and must precede the studio subcommand.
        child.arguments = (!demo ? repo.map { ["--repo", $0] } ?? [] : []) + ["studio", "--port", "0", "--machine"]
        if demo { child.arguments?.append("--demo") }
        child.standardInput = stdinPipe; child.standardOutput = stdoutPipe; child.standardError = stderrPipe
        process = child; input = stdinPipe; output = stdoutPipe; errors = stderrPipe
        stdoutPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil }
            Task { @MainActor in self?.consumeOutput(data, id: id) }
        }
        stderrPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil }
            Task { @MainActor in self?.consumeError(data, id: id) }
        }
        child.terminationHandler = { [weak self] _ in
            Task { @MainActor in
                guard let self, self.launchID == id else { return }
                self.isRunning = false
                if self.handshake == nil { self.launchError = self.failure(L("orch exited before the startup handshake.", "orch 在启动握手前退出。")) }
            }
        }
        do { try child.run(); isRunning = true } catch { shutdown(); throw error }
        let deadline = Date().addingTimeInterval(15)
        do {
            while Date() < deadline {
                try Task.checkCancellation()
                guard launchID == id else { throw CancellationError() }
                if let launchError { throw launchError }
                if let handshake {
                    guard handshake.api == 1 else { throw failure(L("orch API version is incompatible. Upgrade with: brew upgrade orch", "orch API 版本不兼容，请运行 brew upgrade orch 升级。")) }
                    guard ["http", "https"].contains(handshake.url.scheme ?? ""), handshake.url.host != nil,
                          !handshake.token.isEmpty else { throw failure(L("orch returned an invalid startup handshake.", "orch 返回了无效的启动握手。")) }
                    version = handshake.version
                    return handshake
                }
                try await Task.sleep(for: .milliseconds(50))
            }
            throw failure(L("orch did not start within 15 seconds.", "orch 未能在 15 秒内启动。"))
        } catch {
            if launchID == id { shutdown() }
            throw error
        }
    }

    private func consumeOutput(_ data: Data, id: UUID) {
        guard id == launchID, handshake == nil, !data.isEmpty else { return }
        outputBuffer.append(data)
        guard outputBuffer.count < 65_536 else { launchError = failure(L("orch returned an oversized startup handshake.", "orch 返回的启动握手过大。")); return }
        guard let newline = outputBuffer.firstIndex(of: 10) else { return }
        do {
            let value = try JSONDecoder().decode(MachineHandshake.self, from: outputBuffer[..<newline])
            secret = value.token; handshake = value; outputBuffer = Data()
        } catch { launchError = failure(L("orch returned an invalid startup handshake.", "orch 返回了无效的启动握手。")) }
    }
    private func consumeError(_ data: Data, id: UUID) {
        guard id == launchID else { return }
        errorBuffer.append(data)
        while let newline = errorBuffer.firstIndex(of: 10) {
            let line = String(decoding: errorBuffer[..<newline], as: UTF8.self)
            errorBuffer.removeSubrange(...newline)
            appendLog(line)
        }
        if errorBuffer.count > 32_768 { errorBuffer = Data(); appendLog("[oversized log line omitted]") }
        if data.isEmpty, !errorBuffer.isEmpty {
            appendLog(String(decoding: errorBuffer, as: UTF8.self)); errorBuffer = Data()
        }
    }
    /// Same secret, URL and control-character patterns as orch_watch.clean, adapted to Foundation.
    private func appendLog(_ text: String) {
        var line = text
        if !secret.isEmpty { line = line.replacingOccurrences(of: secret, with: "[redacted]") }
        let rules = [
            (#"(?i)\b[a-z][a-z0-9+.-]{1,15}://\S*"#, "[url]"),
            (#"(?i)\b(?:sk|pk|rk|ghp|gho|xox[abp])[-_][A-Za-z0-9_-]{8,}|bearer\s+[A-Za-z0-9._~+/=-]+|(?:api[_-]?key|token|secret|password|passwd|authorization|cookie)\s*[:=]\s*\S+"#, "[redacted]"),
            (#"[\x00-\x08\x0b-\x1f\x7f]"#, " ")]
        for (pattern, replacement) in rules { line = line.replacingOccurrences(of: pattern, with: replacement, options: .regularExpression) }
        line = String(line.prefix(8192)); logs.append(line)
        if logs.count > 40 { logs.removeFirst(logs.count - 40) }
        if let size = try? logURL.resourceValues(forKeys: [.fileSizeKey]).fileSize, size > 5 * 1024 * 1024 {
            let previous = logURL.appendingPathExtension("1")
            try? FileManager.default.removeItem(at: previous)
            try? FileManager.default.moveItem(at: logURL, to: previous)
        }
        if !FileManager.default.fileExists(atPath: logURL.path) { FileManager.default.createFile(atPath: logURL.path, contents: nil) }
        if let handle = try? FileHandle(forWritingTo: logURL) {
            defer { try? handle.close() }
            _ = try? handle.seekToEnd(); try? handle.write(contentsOf: Data((line + "\n").utf8))
        }
    }
    private func failure(_ message: String) -> APIError {
        APIError(code: "launch", message: message + (logs.isEmpty ? "" : "\n\n" + logs.joined(separator: "\n")), status: 0)
    }
    func shutdown() {
        launchID = UUID()
        let owned = process
        output?.fileHandleForReading.readabilityHandler = nil
        errors?.fileHandleForReading.readabilityHandler = nil
        try? input?.fileHandleForWriting.close()
        if let owned, owned.isRunning {
            owned.terminate()
            // The retained Process object identifies our child; never act on a handshake PID.
            Task { @MainActor in
                try? await Task.sleep(for: .seconds(3))
                if owned.isRunning { Darwin.kill(owned.processIdentifier, SIGKILL) }
            }
        }
        process = nil; input = nil; output = nil; errors = nil; isRunning = false
    }
}
