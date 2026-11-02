import SwiftUI

private let ink = Color(red: 0.11, green: 0.12, blue: 0.14)
private let muted = Color(red: 0.44, green: 0.47, blue: 0.53)
private let accent = Color(red: 0.24, green: 0.36, blue: 0.85)
private let paper = Color(red: 0.99, green: 0.99, blue: 1.0)
private let hairline = Color.black.opacity(0.07)

struct KimiModel: Decodable, Identifiable {
    let id: String
    let label: String
    let context: Int
    let efforts: [String]
    let defaultEffort: String
    let isDefault: Bool
    enum CodingKeys: String, CodingKey {
        case id, label, context, efforts
        case defaultEffort = "default_effort"
        case isDefault = "default"
    }
    var detail: String {
        let size = context >= 1_000_000 ? "\(context / 1_000_000)M" : "\(context / 1000)K"
        return "\(size) · \(defaultEffort.isEmpty ? "—" : defaultEffort)"
    }
}

private struct KimiCatalog: Decodable { let models: [KimiModel] }

struct KimiTurn: Codable, Identifiable {
    let id: UUID
    let role: String
    let text: String
}

@MainActor
final class KimiChatModel: ObservableObject {
    @Published private(set) var turns: [KimiTurn] = []
    @Published private(set) var models: [KimiModel] = []
    @Published var draft = ""
    @Published var model = ""
    @Published var auto = false
    @Published private(set) var busy = false
    @Published var error: String?
    private let api = APIClient()
    private let key = "kimiTurns"

    init() {
        if let data = UserDefaults.standard.data(forKey: key),
           let saved = try? JSONDecoder().decode([KimiTurn].self, from: data) {
            turns = saved
        }
    }

    func loadModels() async {
        if let catalog: KimiCatalog = try? await api.request("/kimi/models", timeout: 20) {
            models = catalog.models
            if model.isEmpty, let preset = models.first(where: { $0.isDefault }) { model = preset.id }
        }
    }

    func send() async {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !busy else { return }
        draft = ""
        busy = true
        error = nil
        turns.append(KimiTurn(id: UUID(), role: "user", text: text))
        persist()
        defer { busy = false }
        struct Body: Encodable { let prompt: String; let auto: Bool; let model: String }
        struct Reply: Decodable { let ok: Bool; let text: String; let message: String }
        do {
            let reply: Reply = try await api.request("/kimi/ask", method: "POST",
                body: JSONEncoder().encode(Body(prompt: text, auto: auto, model: model)),
                timeout: 240)
            turns.append(KimiTurn(id: UUID(), role: "kimi",
                                  text: reply.ok ? reply.text : "（未完成）\(reply.message)"))
        } catch {
            turns.append(KimiTurn(id: UUID(), role: "kimi",
                                  text: "（失败）\(error.localizedDescription)"))
        }
        persist()
    }

    private func persist() {
        if let data = try? JSONEncoder().encode(turns.suffix(60)) {
            UserDefaults.standard.set(data, forKey: key)
        }
    }
}

struct KimiAskView: View {
    @Environment(\.dismiss) private var dismiss
    @StateObject private var model = KimiChatModel()
    @FocusState private var typing: Bool

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 16) {
                            if model.turns.isEmpty { hint }
                            ForEach(model.turns) { turn in bubble(turn) }
                            if model.busy {
                                HStack(spacing: 8) {
                                    ProgressView().controlSize(.small)
                                    Text("Kimi 正在执行…").font(.system(size: 12)).foregroundStyle(muted)
                                }
                            }
                        }
                        .padding(18)
                    }
                    .onChange(of: model.turns.count) { _, _ in
                        if let last = model.turns.last {
                            withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                        }
                    }
                }
                if let error = model.error {
                    Text(error).font(.system(size: 11)).foregroundStyle(.orange)
                        .padding(.horizontal, 18)
                }
                composer
            }
            .background(paper)
            .foregroundStyle(ink)
            .navigationTitle("Kimi · 手动对话")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Menu {
                        Picker("模型", selection: $model.model) {
                            Text("默认").tag("")
                            ForEach(model.models) { option in
                                Text("\(option.label) · \(option.detail)").tag(option.id)
                            }
                        }
                        Toggle("自动执行（--auto）", isOn: $model.auto)
                    } label: { Image(systemName: "slider.horizontal.3") }
                }
                ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } }
            }
            .task { await model.loadModels() }
        }
        .tint(accent)
    }

    private var hint: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("和 Kimi 直接对话").font(.system(size: 20, weight: .semibold))
            Text("每条都是你亲手发起的单次调用：可以用 K3 的 1M 上下文和自动执行，不排队、不并发。")
                .font(.system(size: 13)).foregroundStyle(muted)
        }
        .padding(.top, 24)
    }

    private func bubble(_ turn: KimiTurn) -> some View {
        HStack {
            if turn.role == "user" { Spacer(minLength: 40) }
            Text(turn.text)
                .font(.system(size: 14))
                .lineSpacing(2)
                .padding(.horizontal, 13).padding(.vertical, 9)
                .background(turn.role == "user" ? accent.opacity(0.10) : Color.white,
                            in: RoundedRectangle(cornerRadius: 15))
                .overlay(RoundedRectangle(cornerRadius: 15)
                    .stroke(turn.role == "user" ? Color.clear : hairline))
                .textSelection(.enabled)
            if turn.role != "user" { Spacer(minLength: 40) }
        }
        .id(turn.id)
    }

    private var composer: some View {
        HStack(alignment: .bottom, spacing: 8) {
            TextField("问 Kimi…", text: $model.draft, axis: .vertical)
                .font(.system(size: 14)).lineLimit(1...6)
                .focused($typing)
                .padding(.horizontal, 14).padding(.vertical, 10)
                .background(Color.white, in: RoundedRectangle(cornerRadius: 20))
                .overlay(RoundedRectangle(cornerRadius: 20).stroke(hairline))
            Button {
                typing = false
                Task { await model.send() }
            } label: {
                Image(systemName: "arrow.up")
                    .font(.system(size: 14, weight: .bold)).foregroundStyle(.white)
                    .frame(width: 32, height: 32)
                    .background(model.draft.isEmpty ? Color.gray.opacity(0.35) : accent, in: Circle())
            }
            .disabled(model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
    }
}
