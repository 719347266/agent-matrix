// Menu bar monitor for agent-matrix, seen from the owner's side: what needs me, what is moving,
// what happened today. System problems only appear when there is one, in plain words with the
// fix as a button. Reads `python -m agent_matrix.monitor --json` every 15 s. No model calls.
import SwiftUI
import AppKit

struct Item: Identifiable { let id = UUID(); let n: Int; let label: String; let tone: String; let title: String; let note: String; let who: String; let due: String; let reply: String; let url: String?; let cmd: String?; let cmdLabel: String }

/// Numbers may arrive as 16 or "16"; never fall back to 0 silently.
func number(_ v: Any?) -> Int { if let i = v as? Int { return i }; if let s = v as? String, let i = Int(s) { return i }; return -1 }
struct Line: Identifiable { let id = UUID(); let time: String; let kind: String; let title: String; let ok: Bool }
struct Problem: Identifiable { let id = UUID(); let level: String; let title: String; let what: String; let done: String; let you: String; let action: String?; let urls: [String] }

@MainActor
final class Monitor: ObservableObject {
    @Published var level = "unknown"
    @Published var headline = "读取中…"
    @Published var summary = ""
    @Published var need: [Item] = []
    @Published var moving: [Item] = []
    @Published var backlog = 0
    @Published var done: [Item] = []
    @Published var timeline: [Line] = []
    @Published var problems: [Problem] = []
    @Published var updated = ""
    @Published var busy = ""
    var boardURL: String?
    var pagePath: String?
    let root: String
    private var lastSeen: Int
    private var lastRestart = Date.distantPast
    private var timer: Timer?

    init() {
        root = Bundle.main.object(forInfoDictionaryKey: "AgentMatrixRoot") as? String ?? ""
        lastSeen = UserDefaults.standard.integer(forKey: "lastSeenEvent")
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { _ in Task { @MainActor in self.refresh() } }
    }

    nonisolated static func python(_ root: String, _ args: [String]) -> Data? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: root + "/.venv/bin/python")
        p.arguments = ["-m"] + args
        p.currentDirectoryURL = URL(fileURLWithPath: root)
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = (env["PATH"] ?? "") + ":" + NSHomeDirectory() + "/.npm-global/bin:" + NSHomeDirectory() + "/.local/bin:/opt/homebrew/bin"
        p.environment = env
        let out = Pipe(); p.standardOutput = out; p.standardError = Pipe()
        do { try p.run() } catch { return nil }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return data
    }

    func refresh() {
        let root = self.root
        Task.detached {
            let data = Monitor.python(root, ["agent_matrix.monitor", "--json", "--write-html"])
            await MainActor.run { self.apply(data) }
        }
    }

    private func items(_ raw: Any?) -> [Item] {
        (raw as? [[String: Any]] ?? []).map {
            Item(n: number($0["n"]), label: $0["label"] as? String ?? "", tone: $0["tone"] as? String ?? "gray",
                 title: $0["title"] as? String ?? "", note: $0["note"] as? String ?? "", who: $0["who"] as? String ?? "",
                 due: $0["due"] as? String ?? "", reply: $0["reply"] as? String ?? "", url: $0["url"] as? String,
                 cmd: $0["cmd"] as? String, cmdLabel: $0["cmd_label"] as? String ?? "完成")
        }
    }

    private func apply(_ data: Data?) {
        guard let data, let s = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            level = "error"; headline = "读不到状态"; summary = "监控命令没有返回结果，点 ⋯ → 重启监听"; return
        }
        level = s["level"] as? String ?? "unknown"
        headline = s["headline"] as? String ?? ""
        summary = s["summary"] as? String ?? ""
        updated = String((s["generated_at"] as? String ?? "").dropFirst(11).prefix(5))
        boardURL = s["board_url"] as? String
        pagePath = s["page"] as? String
        need = items(s["need_you"]); moving = items(s["in_progress"]); done = items(s["done_recent"])
        backlog = s["backlog_count"] as? Int ?? 0
        timeline = (s["timeline"] as? [[String: Any]] ?? []).map {
            Line(time: $0["time"] as? String ?? "", kind: $0["kind"] as? String ?? "", title: $0["title"] as? String ?? "", ok: $0["ok"] as? Bool ?? true)
        }
        problems = (s["issues"] as? [[String: Any]] ?? []).map {
            Problem(level: $0["level"] as? String ?? "", title: $0["title"] as? String ?? "", what: $0["what"] as? String ?? "",
                    done: $0["done"] as? String ?? "", you: $0["you"] as? String ?? "", action: ($0["action"] as? [String: Any])?["kind"] as? String,
                    urls: ($0["action"] as? [String: Any])?["urls"] as? [String] ?? [])
        }
        // Self-repair: a stopped listener is restarted without asking (at most every 5 minutes).
        if (s["auto_restart_listener"] as? Bool ?? false) && Date().timeIntervalSince(lastRestart) > 300 {
            lastRestart = Date(); restartListener(quiet: true)
        }
        notifyNew(s["notify"] as? [[String: Any]] ?? [], latest: s["latest_event_id"] as? Int ?? 0)
    }

    private func notifyNew(_ events: [[String: Any]], latest: Int) {
        if lastSeen == 0 || latest < lastSeen { lastSeen = latest; UserDefaults.standard.set(latest, forKey: "lastSeenEvent"); return }
        let fresh = events.filter { ($0["id"] as? Int ?? 0) > lastSeen }.sorted { ($0["id"] as? Int ?? 0) < ($1["id"] as? Int ?? 0) }
        for e in fresh.prefix(4) {
            let ok = e["ok"] as? Bool ?? true, kind = e["kind"] as? String ?? "", title = e["title"] as? String ?? ""
            switch (kind, ok) {
            case ("send", true): post("已发到你的飞书", title)
            case ("send", false): post("飞书消息没发出去", title)
            case ("prework", _): post("AI 有进展", title)
            default: if !ok { post("事项中心出了点问题", title) }
            }
        }
        if latest > lastSeen { lastSeen = latest; UserDefaults.standard.set(latest, forKey: "lastSeenEvent") }
    }

    private func post(_ title: String, _ body: String) {
        let esc = { (s: String) in s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        p.arguments = ["-e", "display notification \"\(esc(body))\" with title \"\(esc(title))\""]
        try? p.run()
    }

    func run(_ label: String, _ args: [String]) {
        busy = label
        let root = self.root
        Task.detached {
            let data = Monitor.python(root, args)
            // Show what happened (e.g. "#1 → 完成 ✓，交付文件没找到…") for a few seconds.
            let said = String(data: data ?? Data(), encoding: .utf8)?
                .split(separator: "\n").first.map(String.init) ?? ""
            await MainActor.run {
                self.busy = said.hasPrefix("{") ? "" : said
                self.refresh()
            }
            try? await Task.sleep(for: .seconds(10))
            await MainActor.run { if self.busy == said { self.busy = "" } }
        }
    }

    func restartListener(quiet: Bool = false) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = ["kickstart", "-k", "gui/\(getuid())/com.agent-matrix.listen"]
        try? p.run(); p.waitUntilExit()
        if !quiet { busy = "正在重启监听…" }
        Task { try? await Task.sleep(for: .seconds(20)); self.busy = ""; self.refresh() }
    }

    func open(_ s: String?) { if let s, let u = URL(string: s) { NSWorkspace.shared.open(u) } }
    func perform(_ p: Problem) {
        switch p.action {
        case "restart_listener": restartListener()
        case "subscribe": p.urls.forEach { open($0) }  // links come from the local listener, never from the code
        default: break
        }
    }

    var symbol: String { level == "error" ? "xmark.octagon" : !need.isEmpty ? "tray.full" : level == "warn" ? "exclamationmark.triangle" : "checkmark.circle" }
}

func toneColor(_ t: String) -> Color {
    switch t { case "red": return .red; case "orange": return .orange; case "purple": return .purple; case "blue": return .blue; default: return .gray }
}

struct Badge: View {
    let text: String; let tone: String
    var body: some View {
        Text(text).font(.system(size: 10, weight: .semibold)).foregroundStyle(.white)
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(toneColor(tone), in: Capsule())
    }
}

struct Row: View {
    let item: Item; let m: Monitor
    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Badge(text: item.label, tone: item.tone).frame(width: 64, alignment: .leading)
            VStack(alignment: .leading, spacing: 1) {
                Text(item.n >= 0 ? "#\(item.n) \(item.title)" : item.title).font(.system(size: 12, weight: .medium)).lineLimit(1)
                let meta = [item.note, item.due].filter { !$0.isEmpty }.joined(separator: " · ")
                if !meta.isEmpty { Text(meta).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1) }
                if !item.reply.isEmpty { Text("回复：\(item.reply)").font(.system(size: 10)).foregroundStyle(.tertiary).lineLimit(1) }
            }
            Spacer(minLength: 4)
            if let u = item.url { Button("打开") { m.open(u) }.buttonStyle(.link).font(.system(size: 11)) }
            if let c = item.cmd {
                // Same as replying "16 过" / "1 完成": written to the table, read back, then confirmed on Feishu.
                Button("✓ " + item.cmdLabel) { m.run("处理中…", ["agent_matrix.reply", "--text", c]) }
                    .controlSize(.mini)
            }
        }
    }
}

struct Section<Content: View>: View {
    let title: String; @ViewBuilder let content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title).font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
            content
        }
    }
}

struct Panel: View {
    @ObservedObject var m: Monitor
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                HStack {
                    Text(m.headline).font(.system(size: 15, weight: .semibold))
                    Spacer()
                    Menu {
                        Button("立即检查一次") { m.run("检查中…", ["agent_matrix.reply", "poll"]) }
                        Button("给我发条测试消息") { m.run("发送中…", ["agent_matrix.monitor", "--test-send"]) }
                        Button("清掉已处理的提示") { m.run("清理中…", ["agent_matrix.monitor", "--clear-resolved", "--json"]) }
                        Button("重启监听") { m.restartListener() }
                        Divider()
                        Button("退出监控") { NSApp.terminate(nil) }
                    } label: { Image(systemName: "ellipsis.circle") }
                    .menuStyle(.borderlessButton).fixedSize()
                }
                Text(m.busy.isEmpty ? "\(m.summary) · \(m.updated) 更新" : m.busy).font(.system(size: 11))
                    .foregroundStyle(m.busy.isEmpty ? Color.secondary : Color.accentColor).lineLimit(3)
            }

            ForEach(m.problems.filter { $0.level == "error" || $0.level == "warn" }) { p in
                VStack(alignment: .leading, spacing: 3) {
                    Text((p.level == "setup" ? "⚙︎ " : "⚠︎ ") + p.title).font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(p.level == "error" ? .red : p.level == "setup" ? .primary : .orange)
                    Text(p.what).font(.system(size: 11))
                    Text("系统：" + p.done).font(.system(size: 11)).foregroundStyle(.secondary)
                    HStack {
                        if !p.you.isEmpty { Text("你：" + p.you).font(.system(size: 11)) }
                        Spacer()
                        if p.action == "subscribe" && !p.urls.isEmpty { Button("去订阅") { m.perform(p) }.controlSize(.small) }
                        if p.action == "restart_listener" { Button("重启监听") { m.perform(p) }.controlSize(.small) }
                    }
                }
                .padding(8).background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
            }

            Section(title: "需要你（\(m.need.count)）") {
                if m.need.isEmpty { Text("没有要你处理的 ✓").font(.system(size: 12)).foregroundStyle(.secondary) }
                ForEach(m.need.prefix(6)) { Row(item: $0, m: m) }
                if m.need.count > 6 { Text("还有 \(m.need.count - 6) 件，点「完整页面」").font(.system(size: 11)).foregroundStyle(.secondary) }
            }
            Divider()
            Section(title: "正在进行（\(m.moving.count)）") {
                if m.moving.isEmpty { Text("暂无").font(.system(size: 12)).foregroundStyle(.secondary) }
                ForEach(m.moving.prefix(6)) { Row(item: $0, m: m) }
                if m.moving.count > 6 { Text("还有 \(m.moving.count - 6) 件").font(.system(size: 11)).foregroundStyle(.secondary) }
                if m.backlog > 0 { Text("另有 \(m.backlog) 件你的待办没定日期或还没到期").font(.system(size: 11)).foregroundStyle(.secondary) }
            }
            Divider()
            Section(title: "最近完成（\(m.done.count)）") {
                if m.done.isEmpty { Text("最近 7 天还没有完成的事").font(.system(size: 12)).foregroundStyle(.secondary) }
                ForEach(m.done.prefix(4)) { Row(item: $0, m: m) }
            }
            Divider()
            Section(title: "今天") {
                if m.timeline.isEmpty { Text("今天还没有动静").font(.system(size: 12)).foregroundStyle(.secondary) }
                ForEach(m.timeline.prefix(5)) { l in
                    HStack(alignment: .top, spacing: 6) {
                        Text(l.time).font(.system(size: 11).monospacedDigit()).foregroundStyle(.secondary)
                        Text(l.kind).font(.system(size: 11)).foregroundStyle(.secondary)
                        Text(l.title).font(.system(size: 11)).lineLimit(1)
                        if !l.ok { Text("✗").font(.system(size: 11)).foregroundStyle(.red) }
                    }
                }
            }
            ForEach(m.problems.filter { $0.level == "setup" }) { p in
                HStack {
                    Text("⚙︎ " + p.title).font(.system(size: 11)).foregroundStyle(.secondary)
                    Spacer()
                    if !p.urls.isEmpty { Button("去订阅") { m.perform(p) }.controlSize(.mini) }
                }
            }
            HStack {
                Button("完整页面") { if let p = m.pagePath { NSWorkspace.shared.open(URL(fileURLWithPath: p)) } }
                Button("飞书看板") { m.open(m.boardURL) }
                Spacer()
            }.controlSize(.small)
        }
        .padding(14)
        .frame(width: 380)
    }
}

@main
struct AgentMatrixMonitorApp: App {
    @StateObject private var monitor = Monitor()
    var body: some Scene {
        MenuBarExtra {
            Panel(m: monitor)
        } label: {
            Image(systemName: monitor.symbol)
        }
        .menuBarExtraStyle(.window)
    }
}
