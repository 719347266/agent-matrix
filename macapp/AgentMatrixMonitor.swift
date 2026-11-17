// Menu bar monitor for agent-matrix. Reads status from `python -m agent_matrix.monitor --json`
// every 15 s, shows health at a glance, and posts a macOS notification for every message the
// system sends you and every error. No network access of its own; no model calls.
import SwiftUI
import AppKit

struct SendItem: Identifiable { let id: Int; let time: String; let title: String; let ok: Bool }
struct Issue: Identifiable { let id = UUID(); let text: String; let level: String }
struct Component: Identifiable { let id = UUID(); let name: String; let minutesAgo: Double?; let summary: String }

@MainActor
final class Monitor: ObservableObject {
    @Published var level = "unknown"
    @Published var headline = "读取中…"
    @Published var issues: [Issue] = []
    @Published var sends: [SendItem] = []
    @Published var components: [Component] = []
    @Published var queues: [(String, Int)] = []
    @Published var streams: [(String, String)] = []
    @Published var updated = ""
    @Published var busy = ""
    var boardURL: String?
    var pagePath: String?
    let root: String
    private var lastSeen: Int
    private var timer: Timer?

    init() {
        root = Bundle.main.object(forInfoDictionaryKey: "AgentMatrixRoot") as? String ?? ""
        lastSeen = UserDefaults.standard.integer(forKey: "lastSeenEvent")
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { _ in
            Task { @MainActor in self.refresh() }
        }
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

    private func apply(_ data: Data?) {
        guard let data, let s = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            level = "error"; headline = "读不到状态（监控命令失败）"; return
        }
        level = s["level"] as? String ?? "unknown"
        headline = s["headline"] as? String ?? ""
        updated = String((s["generated_at"] as? String ?? "").dropFirst(11).prefix(5))
        boardURL = s["board_url"] as? String
        pagePath = s["page"] as? String
        issues = (s["issues"] as? [[String: Any]] ?? []).map { Issue(text: $0["text"] as? String ?? "", level: $0["level"] as? String ?? "") }
        sends = (s["sends_today"] as? [[String: Any]] ?? []).map {
            SendItem(id: $0["id"] as? Int ?? 0, time: $0["time"] as? String ?? "", title: $0["title"] as? String ?? "", ok: $0["ok"] as? Bool ?? false)
        }.reversed()
        components = (s["components"] as? [[String: Any]] ?? []).map {
            Component(name: $0["name"] as? String ?? "", minutesAgo: $0["minutes_ago"] as? Double, summary: $0["summary"] as? String ?? "")
        }
        queues = ((s["queues"] as? [String: Int]) ?? [:]).sorted { $0.key < $1.key }.map { ($0.key, $0.value) }
        streams = (s["streams"] as? [[String: Any]] ?? []).map { ($0["name"] as? String ?? "", $0["state"] as? String ?? "") }
        notifyNew(s["events_today"] as? [[String: Any]] ?? [], latest: s["latest_event_id"] as? Int ?? 0)
    }

    /// One macOS notification per new send / error since last time. The very first run only
    /// remembers where it is, so installing the app does not replay the whole day.
    private func notifyNew(_ events: [[String: Any]], latest: Int) {
        if lastSeen == 0 { lastSeen = latest; UserDefaults.standard.set(latest, forKey: "lastSeenEvent"); return }
        let fresh = events.filter { ($0["id"] as? Int ?? 0) > lastSeen }.sorted { ($0["id"] as? Int ?? 0) < ($1["id"] as? Int ?? 0) }
        for e in fresh.prefix(5) {
            let ok = e["ok"] as? Bool ?? true, kind = e["kind"] as? String ?? "", title = e["title"] as? String ?? ""
            if kind == "send" { post(ok ? "已发到飞书" : "飞书发送失败", title) }
            else if !ok { post("事项中心出错", title) }
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
        busy = label + "…"
        let root = self.root
        Task.detached {
            _ = Monitor.python(root, args)
            await MainActor.run { self.busy = ""; self.refresh() }
        }
    }

    func restartListener() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = ["kickstart", "-k", "gui/\(getuid())/com.agent-matrix.listen"]
        try? p.run(); p.waitUntilExit()
        busy = "已重启监听"
        Task { try? await Task.sleep(for: .seconds(20)); self.busy = ""; self.refresh() }
    }

    var color: Color { level == "ok" ? .green : level == "warn" ? .orange : level == "error" ? .red : .gray }
    var symbol: String { level == "ok" ? "checkmark.circle" : level == "warn" ? "exclamationmark.triangle" : level == "error" ? "xmark.octagon" : "circle.dashed" }
}

struct Panel: View {
    @ObservedObject var m: Monitor
    func ago(_ v: Double?) -> String { guard let v else { return "从未" }; return v < 1 ? "刚刚" : v < 120 ? "\(Int(v)) 分钟前" : String(format: "%.1f 小时前", v / 60) }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Circle().fill(m.color).frame(width: 10, height: 10)
                Text(m.headline).font(.headline)
                Spacer()
                Text(m.busy.isEmpty ? "更新 \(m.updated)" : m.busy).font(.caption).foregroundStyle(.secondary)
            }
            ForEach(m.issues.prefix(4)) { i in
                Text(i.text).font(.caption).foregroundStyle(i.level == "error" ? .red : .orange).fixedSize(horizontal: false, vertical: true)
            }
            Divider()
            Text("今天发给你的消息（\(m.sends.count)）").font(.subheadline.bold())
            if m.sends.isEmpty { Text("还没有").font(.caption).foregroundStyle(.secondary) }
            ForEach(m.sends.prefix(6)) { s in
                HStack(alignment: .top) {
                    Text(s.time).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    Text(s.title).font(.caption).lineLimit(1)
                    Spacer()
                    Text(s.ok ? "✓" : "✗ 失败").font(.caption).foregroundStyle(s.ok ? .green : .red)
                }
            }
            Divider()
            Text("各环节最后一次运行").font(.subheadline.bold())
            ForEach(m.components) { c in
                HStack { Text(c.name).font(.caption); Spacer(); Text(ago(c.minutesAgo)).font(.caption).foregroundStyle(.secondary) }
            }
            HStack(spacing: 12) {
                ForEach(m.queues, id: \.0) { q in Text("\(q.0) \(q.1)").font(.caption2).foregroundStyle(.secondary) }
            }
            HStack(spacing: 10) {
                ForEach(m.streams, id: \.0) { s in
                    Text("\(s.0)·\(s.1)").font(.caption2).foregroundStyle(s.1 == "已连接" ? .green : .orange)
                }
            }
            Divider()
            HStack {
                Button("监控页面") { if let p = m.pagePath { NSWorkspace.shared.open(URL(fileURLWithPath: p)) } }
                Button("飞书看板") { if let u = m.boardURL, let url = URL(string: u) { NSWorkspace.shared.open(url) } }
                Button("立即检查") { m.run("检查中", ["agent_matrix.reply", "poll"]) }
            }
            HStack {
                Button("发条测试消息") { m.run("发送中", ["agent_matrix.monitor", "--test-send"]) }
                Button("重启监听") { m.restartListener() }
                Spacer()
                Button("退出") { NSApp.terminate(nil) }
            }
        }
        .padding(14)
        .frame(width: 360)
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
