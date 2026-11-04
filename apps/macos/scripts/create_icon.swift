#!/usr/bin/env swift
import AppKit

// A code-native mark: three local nodes connected around one focused task.
let root = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent().deletingLastPathComponent()
let folder = root.appendingPathComponent("Resources/Assets.xcassets/AppIcon.appiconset")
try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
var entries: [[String: String]] = []
var rendered = Set<Int>()
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let pixels = size * scale
        let filename = "icon-\(pixels).png"
        entries.append(["idiom": "mac", "size": "\(size)x\(size)", "scale": "\(scale)x", "filename": filename])
        guard rendered.insert(pixels).inserted else { continue }
        let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
        let side = CGFloat(pixels)
        let inset = side * 0.06
        let rect = NSRect(x: inset, y: inset, width: side - 2 * inset, height: side - 2 * inset)
        NSColor(calibratedRed: 0.12, green: 0.32, blue: 0.29, alpha: 1).setFill()
        NSBezierPath(roundedRect: rect, xRadius: side * 0.20, yRadius: side * 0.20).fill()
        let points = [NSPoint(x: side * 0.28, y: side * 0.68), NSPoint(x: side * 0.28, y: side * 0.32), NSPoint(x: side * 0.70, y: side * 0.50)]
        NSColor(calibratedRed: 0.62, green: 0.83, blue: 0.73, alpha: 1).setStroke()
        for point in points.prefix(2) {
            let path = NSBezierPath(); path.move(to: point)
            path.curve(to: points[2], controlPoint1: NSPoint(x: side * 0.52, y: point.y), controlPoint2: NSPoint(x: side * 0.45, y: side * 0.50))
            path.lineWidth = max(1, side * 0.035); path.stroke()
        }
        for (index, point) in points.enumerated() {
            let radius = side * (index == 2 ? 0.085 : 0.064)
            NSColor(calibratedWhite: index == 2 ? 0.98 : 0.83, alpha: 1).setFill()
            NSBezierPath(ovalIn: NSRect(x: point.x - radius, y: point.y - radius, width: radius * 2, height: radius * 2)).fill()
        }
        NSGraphicsContext.restoreGraphicsState()
        guard let data = bitmap.representation(using: .png, properties: [:]) else { fatalError("Unable to render app icon") }
        try data.write(to: folder.appendingPathComponent(filename))
    }
}
let catalog: [String: Any] = ["images": entries, "info": ["author": "xcode", "version": 1]]
try JSONSerialization.data(withJSONObject: catalog, options: [.sortedKeys, .prettyPrinted]).write(to: folder.appendingPathComponent("Contents.json"))
