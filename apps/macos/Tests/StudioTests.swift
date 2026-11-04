import XCTest
import Foundation
import Darwin
@testable import OrchStudio

final class StudioTests: XCTestCase {
    func testUnknownStatusRoundTripsWithoutCrashing() throws {
        for value in ["PARKED", "DONE", "FUTURE_STATE"] {
            let decoded = try JSONDecoder().decode(NodeStatus.self, from: JSONEncoder().encode(value))
            XCTAssertEqual(decoded.rawValue, value)
            XCTAssertEqual(try JSONDecoder().decode(String.self, from: JSONEncoder().encode(decoded)), value)
        }
    }

    func testMessagePolicyMatrix() {
        let rows: [(NodeStatus, Bool, Bool)] = [(.planned, true, false), (.ready, true, false), (.running, true, false), (.verifying, true, false), (.merging, true, false), (.parked, true, false), (.done, true, true), (.replanned, false, false), (.unknown("FUTURE"), false, false)]
        for (status, allowed, rework) in rows {
            XCTAssertEqual(MessagePolicy.canSend(status: status, cancelled: false), allowed, status.rawValue)
            XCTAssertFalse(MessagePolicy.canSend(status: status, cancelled: true), status.rawValue)
            XCTAssertEqual(MessagePolicy.rework(status: status, requested: true), rework, status.rawValue)
            XCTAssertFalse(MessagePolicy.rework(status: status, requested: false), status.rawValue)
        }
    }

    func testSSECommentsMultilineCRLFAndReset() {
        let rows: [([String], SSEEvent?)] = [
            ([": comment", "event: changed", "data: {\"run\":", "data: \"r1\"}", ""], SSEEvent(event: "changed", data: "{\"run\":\n\"r1\"}")),
            (["event: ping\r", "data: {}\r", "\r"], SSEEvent(event: "ping", data: "{}")),
            (["event: forgotten", "", "data: next", ""], SSEEvent(event: "message", data: "next")),
            ([": keepalive", "id: 9", "retry: 500", ""], nil)
        ]
        for (lines, expected) in rows {
            var parser = SSEParser()
            var events: [SSEEvent] = []
            for line in lines { if let event = parser.consume(line) { events.append(event) } }
            XCTAssertEqual(events, expected.map { [$0] } ?? [])
        }
    }

    func testLayeredLayoutStableMissingDependenciesAndCycles() {
        let nodes = [node("A"), node("B", depends: ["A"]), node("C", depends: ["B", "A"]), node("D", depends: ["absent"])]
        let positions = GraphLayout.positions(nodes: nodes)
        XCTAssertEqual(positions, GraphLayout.positions(nodes: nodes.reversed()))
        XCTAssertEqual(positions["A"]?.x, 120)
        XCTAssertEqual(positions["B"]?.x, 400)
        XCTAssertEqual(positions["C"]?.x, 680)
        XCTAssertEqual(positions["D"]?.y, 192)
        let cycle = GraphLayout.positions(nodes: [node("A", depends: ["B"]), node("B", depends: ["A"]), node("C", depends: ["B"])])
        XCTAssertEqual(cycle["A"]?.x, cycle["B"]?.x)
        XCTAssertEqual(cycle["C"]?.x, 400)
        XCTAssertTrue(GraphLayout.positions(nodes: []).isEmpty)
    }

    func testRealJSONFieldDecodingAndNullableRole() throws {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let json = #"{"role":"reviewer","title":"Reviewer","description":"Reviews tests","route":null,"default":null,"new_field":123}"#
        let role = try decoder.decode(Role.self, from: Data(json.utf8))
        XCTAssertNil(role.route); XCTAssertNil(role.defaultRoute)
        let runJSON = #"{"id":"r","title":"Test","status":"future","live":false,"created":1,"tasks":0,"done":0,"parked":0,"cost":{"usd":0,"cny":0},"elapsed_s":42.5,"new_field":true}"#
        XCTAssertEqual(try decoder.decode(RunSummary.self, from: Data(runJSON.utf8)).elapsedS, 42.5)
    }

    func testRouteEffortOrder() {
        let routes = ["high", "off", "low", "medium"].map { effort -> Route in
            var route = node("A").route; route.effort = effort; route.name = effort; return route
        }
        XCTAssertEqual(routes.sorted(by: routeOrder).map(\.effort), ["off", "low", "medium", "high"])
    }

    private func node(_ id: String, depends: [String] = []) -> StudioNode {
        StudioNode(id: id, goal: id, status: .planned, since: 0, attempts: 0, rung: "first try", route: Route(label: "demo/model:low", provider: "demo", model: "model", effort: "low", vendor: "other", name: "Model", verified: true), routeOverride: false, files: [], reqs: [], dependsOn: depends, diagnosis: Diagnosis(kind: "queued", summary: "", blamed: [], checks: []), cost: Cost(usd: 0, cny: 0), person: Person(messages: 0, taken: false), ripple: "", replacedBy: [])
    }
}

// Exercises the shipped Swift networking/store against the repository's no-cost backend.
@MainActor
final class DemoIntegrationTests: XCTestCase {
    private func requireOrch() async throws {
        guard await ServerController.discoverExecutable() != nil else { throw XCTSkip("Install orch from this checkout to run the demo integration test.") }
    }
    func testDemoGuidanceLiveSSEAndDownstreamRipple() async throws {
        try await requireOrch()
        let store = AppStore()
        defer { store.shutdown() }
        await store.connect(repo: nil, demo: true)
        XCTAssertTrue(store.connected, store.error ?? "No connection")
        XCTAssertEqual(store.boot?.api, 1)
        XCTAssertEqual(store.graph?.nodes.count, 6)
        guard store.connected else { return }
        await store.selectNode("T-004")
        XCTAssertEqual(store.detail?.node.diagnosis.kind, "own")
        XCTAssertEqual(store.detail?.node.status, .parked)
        let originalRoute = store.detail?.node.route.label
        await store.setNodeRoute("invalid-route-for-test")
        XCTAssertNil(store.error)
        XCTAssertEqual(store.detail?.node.route.label, originalRoute)
        XCTAssertNotNil(store.toast)
        await store.setNodeRoute("dsh/deepseek-flash:high")
        XCTAssertTrue(store.detail?.node.routeOverride == true)
        await store.setNodeRoute("")
        XCTAssertFalse(store.detail?.node.routeOverride ?? true)
        let sent = await store.sendMessage(text: "Validate every amount with Number.isSafeInteger.", rework: false)
        XCTAssertTrue(sent, store.error ?? "Message failed")
        // No explicit refresh: only real server SSE drives these transitions.
        try await waitUntil("Guided node merges through SSE", timeout: 25) { store.graph?.nodes.first { $0.id == "T-004" }?.status == .done }
        try await waitUntil("Integration node diagnoses its upstream", timeout: 45) { store.graph?.nodes.first { $0.id == "T-005" }?.diagnosis.kind == "upstream" }
        XCTAssertEqual(store.graph?.nodes.first { $0.id == "T-006" }?.diagnosis.kind, "environment")
        await store.selectNode("T-005")
        XCTAssertEqual(store.detail?.node.diagnosis.blamed, ["T-003"])
        await store.selectNode("T-003")
        XCTAssertEqual(store.detail?.node.status, .done)
        let rebuilt = await store.sendMessage(text: "Preserve Unicode literally in the export.", rework: true)
        XCTAssertTrue(rebuilt)
        try await waitUntil("Downstream rebuilds after upstream rework", timeout: 40) {
            store.graph?.nodes.first { $0.id == "T-005" }.map { $0.ripple == "affected" && $0.status == .done } ?? false
        }
        XCTAssertNil(store.error)
    }
    func testOwnedServiceRestartsAfterUnexpectedExit() async throws {
        try await requireOrch()
        let store = AppStore()
        defer { store.shutdown() }
        await store.connect(repo: nil, demo: true)
        await store.selectNode("T-004")
        let pid = try XCTUnwrap(store.server.ownedProcessID)
        XCTAssertEqual(kill(pid, SIGKILL), 0)
        try await waitUntil("Unexpected owned-process exit reconnects and restores selection", timeout: 15) {
            store.connected && store.server.ownedProcessID != nil && store.server.ownedProcessID != pid && store.detail?.node.id == "T-004"
        }
        XCTAssertEqual(store.selectedRunID, "rdemo0002")
        XCTAssertNil(store.error)
    }
    func testDemoNewRunCancelResumeAndOwnedChildCleanup() async throws {
        try await requireOrch()
        let server = ServerController()
        let handshake = try await server.start(repo: nil, demo: true)
        let client = APIClient(baseURL: handshake.url, token: handshake.token)
        defer { client.invalidate(); server.shutdown() }
        struct RunResponse: Decodable { var ok: Bool; var run: RunSummary }
        let start: RunResponse = try await client.post("api/run/start", body: ["spec": "R1. src/example.txt: write the example"])
        let cancelled: RunResponse = try await client.post("api/run/cancel", body: ["run": start.run.id])
        XCTAssertEqual(cancelled.run.status, "cancelled")
        let resumed: RunResponse = try await client.post("api/run/resume", body: ["run": start.run.id])
        XCTAssertNotEqual(resumed.run.status, "cancelled")
        let detail: NodeDetail = try await client.get("api/node", query: ["run": start.run.id, "task": "T-001"])
        XCTAssertEqual(detail.node.id, "T-001")
        let settings: StudioSettings = try await client.get("api/settings")
        XCTAssertFalse(settings.providers.isEmpty)
        let wrong = APIClient(baseURL: handshake.url, token: "deliberately-invalid-test-token")
        defer { wrong.invalidate() }
        do {
            let _: Boot = try await wrong.get("api/boot")
            XCTFail("Invalid token was accepted")
        } catch let error as APIError { XCTAssertEqual(error.status, 401); XCTAssertEqual(error.code, "unauthorized") }
        let pid = Int32(handshake.pid)
        server.shutdown()
        try await waitUntil("Owned server exits when app closes stdin", timeout: 5) { kill(pid, 0) == -1 && errno == ESRCH }
    }
    private func waitUntil(_ message: String, timeout: Double, condition: () -> Bool) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition(), Date() < deadline { try await Task.sleep(for: .milliseconds(100)) }
        XCTAssertTrue(condition(), message)
        if !condition() { throw NSError(domain: "StudioTests", code: 1, userInfo: [NSLocalizedDescriptionKey: message]) }
    }
}
