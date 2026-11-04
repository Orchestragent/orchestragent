import SwiftUI

/// Path entry keeps project selection usable when a system File Provider stalls.
@MainActor
struct ProjectPicker: View {
    var store: AppStore
    var browse: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var path = ""
    @State private var opening = false
    @State private var failure: String?
    @FocusState private var pathFocused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Image(systemName: "folder.badge.gearshape").font(.system(size: 28, weight: .light)).foregroundStyle(Palette.accent)
            Text(L("Open a workspace", "打开工作区")).font(.system(size: 24, weight: .medium))
            Text(L("Enter a Git repository path, or choose a folder.", "输入 Git 仓库路径，或浏览文件夹。")).foregroundStyle(.secondary)
            HStack {
                TextField("~/Projects/my-project", text: $path).textFieldStyle(.roundedBorder)
                    .focused($pathFocused).onSubmit { connect(demo: false) }
                    .accessibilityLabel(L("Repository path", "仓库路径"))
                Button(L("Browse…", "浏览…"), action: browse).disabled(opening)
            }
            if let failure { Text(failure).font(.callout).foregroundStyle(.orange).textSelection(.enabled) }
            Divider()
            HStack {
                Button(L("Explore Demo", "体验演示")) { connect(demo: true) }.disabled(opening)
                Spacer()
                Button(L("Cancel", "取消")) { dismiss() }.keyboardShortcut(.cancelAction).disabled(opening)
                Button(opening ? L("Opening…", "正在打开…") : L("Open Project", "打开项目")) { connect(demo: false) }
                    .buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                    .disabled(opening || path.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }.padding(28).frame(width: 520).task { pathFocused = true }
    }
    private func connect(demo: Bool) {
        guard !opening, demo || !path.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        opening = true; failure = nil
        let expanded = (path.trimmingCharacters(in: .whitespacesAndNewlines) as NSString).expandingTildeInPath
        Task { @MainActor in
            await store.connect(repo: demo ? nil : expanded, demo: demo)
            opening = false
            if store.connected { dismiss() }
            else { failure = store.error; store.error = nil }
        }
    }
}
