import AppKit
import UserNotifications

@MainActor
final class StudioNotifications: NSObject, UNUserNotificationCenterDelegate {
    var onSelect: ((String, String?) -> Void)?
    private var requestedPermission = false

    override init() {
        super.init()
        // UNUserNotificationCenter requires an app bundle; command-line tests have none.
        if Bundle.main.bundleIdentifier != nil { UNUserNotificationCenter.current().delegate = self }
    }
    func update(previous: Graph?, current: Graph) {
        let parked = current.nodes.filter { $0.status == .parked }.count
        NSApplication.shared.dockTile.badgeLabel = parked == 0 ? nil : String(parked)
        guard let previous, previous.run.id == current.run.id else { return }
        let old = Dictionary(uniqueKeysWithValues: previous.nodes.map { ($0.id, $0.status) })
        if enabled("notifiedParked") {
            for node in current.nodes where node.status == .parked && old[node.id] != .parked {
                notify(title: L("\(node.id) needs guidance", "\(node.id) 需要指引"), body: node.diagnosis.summary, run: current.run.id, node: node.id)
            }
        }
        guard previous.run.status != current.run.status else { return }
        switch current.run.status {
        case "done" where enabled("notifiedDone"):
            notify(title: L("Run completed", "运行完成"), body: current.run.title, run: current.run.id, node: nil)
        case "failed" where enabled("notifiedFailed"):
            notify(title: L("Run failed", "运行失败"), body: current.run.title, run: current.run.id, node: nil)
        case "parked" where enabled("notifiedParked"):
            notify(title: L("Run needs guidance", "运行需要指引"), body: current.run.title, run: current.run.id, node: nil)
        default: break
        }
    }
    func clearBadge() { NSApplication.shared.dockTile.badgeLabel = nil }
    private func enabled(_ key: String) -> Bool {
        UserDefaults.standard.object(forKey: key) == nil || UserDefaults.standard.bool(forKey: key)
    }
    private func notify(title: String, body: String, run: String, node: String?) {
        guard Bundle.main.bundleIdentifier != nil,
              ProcessInfo.processInfo.environment["ORCH_STUDIO_TESTING"] != "1" else { return }
        Task {
            let center = UNUserNotificationCenter.current()
            if !requestedPermission {
                requestedPermission = true
                guard (try? await center.requestAuthorization(options: [.alert, .badge, .sound])) == true else { return }
            }
            let content = UNMutableNotificationContent()
            content.title = title; content.body = body; content.sound = .default
            content.userInfo = ["run": run]
            if let node { content.userInfo["node"] = node }
            let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
            try? await center.add(request)
        }
    }
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse, withCompletionHandler completionHandler: @escaping () -> Void) {
        let run = response.notification.request.content.userInfo["run"] as? String
        let node = response.notification.request.content.userInfo["node"] as? String
        Task { @MainActor [weak self] in
            NSApplication.shared.activate(ignoringOtherApps: true)
            NSApplication.shared.windows.first { $0.canBecomeMain }?.makeKeyAndOrderFront(nil)
            if let run { self?.onSelect?(run, node) }
        }
        completionHandler()
    }
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification, withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }
}
