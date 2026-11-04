import Foundation
import CoreGraphics
import CryptoKit

/// Stable dependency layers. Strongly connected nodes share a layer, so malformed
/// cyclic input cannot recurse forever or move between refreshes.
enum GraphLayout {
    static let cardSize = CGSize(width: 240, height: 104)

    static func positions(nodes: [StudioNode]) -> [String: CGPoint] {
        let ids = Array(Set(nodes.map(\.id))).sorted()
        let known = Set(ids)
        let dependencies = Dictionary(nodes.map { ($0.id, $0.dependsOn.filter(known.contains).sorted()) },
                                      uniquingKeysWith: { first, _ in first })
        var nextIndex = 0
        var indices: [String: Int] = [:]
        var low: [String: Int] = [:]
        var stack: [String] = []
        var stacked = Set<String>()
        var components: [[String]] = []
        func visit(_ id: String) {
            indices[id] = nextIndex
            low[id] = nextIndex
            nextIndex += 1
            stack.append(id)
            stacked.insert(id)
            for dependency in dependencies[id, default: []] {
                if indices[dependency] == nil {
                    visit(dependency)
                    low[id] = min(low[id, default: 0], low[dependency, default: 0])
                } else if stacked.contains(dependency) {
                    low[id] = min(low[id, default: 0], indices[dependency, default: 0])
                }
            }
            if low[id] == indices[id] {
                var component: [String] = []
                while let last = stack.popLast() {
                    stacked.remove(last)
                    component.append(last)
                    if last == id { break }
                }
                components.append(component.sorted())
            }
        }
        for id in ids where indices[id] == nil { visit(id) }
        var componentOf: [String: Int] = [:]
        for (index, component) in components.enumerated() {
            for id in component { componentOf[id] = index }
        }
        var depths: [Int: Int] = [:]
        func depth(_ index: Int) -> Int {
            if let cached = depths[index] { return cached }
            let upstream = Set(components[index].flatMap { dependencies[$0, default: []] }
                .compactMap { componentOf[$0] }).subtracting([index])
            let value = upstream.map { depth($0) + 1 }.max() ?? 0
            depths[index] = value
            return value
        }
        var rows: [Int: Int] = [:]
        var result: [String: CGPoint] = [:]
        for id in ids {
            let layer = depth(componentOf[id, default: 0])
            let row = rows[layer, default: 0]
            result[id] = CGPoint(x: CGFloat(layer) * 280 + 120, y: CGFloat(row) * 140 + 52)
            rows[layer] = row + 1
        }
        return result
    }

    static func bounds(_ positions: [String: CGPoint]) -> CGRect {
        guard let first = positions.values.first else { return CGRect(x: 0, y: 0, width: 240, height: 104) }
        return positions.values.reduce(CGRect(origin: first, size: .zero)) { $0.union(CGRect(origin: $1, size: .zero)) }
            .insetBy(dx: -cardSize.width / 2, dy: -cardSize.height / 2)
    }
}

/// Only coordinates are persisted; no goals, provider information, or credentials.
enum CanvasLayoutPersistence {
    private struct Point: Codable { var x: Double; var y: Double }

    private static func file(repo: String, run: String) throws -> URL {
        let root = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                               appropriateFor: nil, create: true)
        let hash = SHA256.hash(data: Data(repo.utf8)).map { String(format: "%02x", $0) }.joined()
        let safeRun = run.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? "run"
        return root.appendingPathComponent("OrchStudio/layouts/\(hash)", isDirectory: true)
            .appendingPathComponent("\(safeRun).json")
    }

    static func load(repo: String, run: String) -> [String: CGPoint] {
        guard let url = try? file(repo: repo, run: run),
              let data = try? Data(contentsOf: url),
              let points = try? JSONDecoder().decode([String: Point].self, from: data) else { return [:] }
        return points.compactMapValues { point in
            guard point.x.isFinite, point.y.isFinite else { return nil }
            return CGPoint(x: point.x, y: point.y)
        }
    }

    static func save(_ positions: [String: CGPoint], repo: String, run: String) throws {
        let url = try file(repo: repo, run: run)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let points = positions.mapValues { Point(x: $0.x, y: $0.y) }
        try JSONEncoder().encode(points).write(to: url, options: .atomic)
    }
}
