// Menu bar monitor for agent-matrix, seen from the owner's side: what needs me, what is moving,
// what happened today. System problems only appear when there is one, in plain words with the
// fix as a button. Reads `python -m agent_matrix.monitor --json` every 15 s. No model calls.
import SwiftUI
import AppKit
import WebKit
import UserNotifications

/// The 心响 main window: the local client served by the always-on listener (127.0.0.1 only).
@MainActor
final class CenterWindow: NSObject, NSWindowDelegate, WKNavigationDelegate {
    static let shared = CenterWindow()
    private var window: NSWindow?
    private var web: WKWebView?
    private var clientURL: URL?
    private var retries = 0
    private var links: ClientLinks?

    private func address(_ root: String) -> URL {
        // Never read project files on the UI thread: an unavailable volume can
        // block open() indefinitely, freezing every click and even window closing.
        if let text = Bundle.main.object(forInfoDictionaryKey: "AgentMatrixAppURL") as? String,
           let url = URL(string: text), url.scheme == "http",
           ["127.0.0.1", "localhost"].contains(url.host ?? "") { return url }
        return URL(string: "http://127.0.0.1:47865/")!
    }

    func show(_ root: String, item: Int? = nil) {
        clientURL = address(root)
        let fresh = window == nil || window?.isVisible == false
        if let item, item > 0, !fresh, let web {
            // already showing 心响: just open that item, no reload
            web.evaluateJavaScript("location.hash='item-\(item)'", completionHandler: nil)
        }
        if let item, item > 0, fresh, let base = clientURL,
           var parts = URLComponents(url: base, resolvingAgainstBaseURL: false) {
            parts.fragment = "item-\(item)"
            clientURL = parts.url ?? base
        }
        if window == nil {
            let web = WKWebView(frame: NSRect(x: 0, y: 0, width: 1180, height: 800), configuration: WKWebViewConfiguration())
            web.autoresizingMask = [.width, .height]
            web.navigationDelegate = self
            let links = ClientLinks(clientURL: clientURL!)
            self.links = links  // WKWebView keeps its UI delegate weakly.
            web.uiDelegate = links
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 800),
                             styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
            w.title = "心响"; w.contentView = web; w.isReleasedWhenClosed = false; w.delegate = self
            w.minSize = NSSize(width: 720, height: 520)
            if !w.setFrameUsingName("XinxiangMain") { w.center() }
            w.setFrameAutosaveName("XinxiangMain")
            window = w; self.web = web
            reload()
        } else if window?.isVisible == false {
            reload()
        }
        NSApp.setActivationPolicy(.regular)
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func reload() {
        guard let clientURL else { return }
        web?.load(URLRequest(url: clientURL, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 12))
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        decisionHandler(links?.policy(for: navigationAction, in: webView) ?? .cancel)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if webView.url?.host == clientURL?.host { retries = 0 }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        recover(webView)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        if (error as NSError).code != NSURLErrorCancelled { recover(webView) }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        if (error as NSError).code != NSURLErrorCancelled { recover(webView) }
    }

    private func recover(_ webView: WKWebView) {
        // Do not leave a blank window when the local service or WebKit restarts.
        let url = clientURL?.absoluteString ?? "http://127.0.0.1:47865/"
        webView.loadHTMLString("""
        <meta name="viewport" content="width=device-width"><style>:root{color-scheme:light dark}body{font:16px -apple-system;padding:32px;line-height:1.8}a{color:inherit}</style>
        <h2>正在重新连接心响</h2><p>你的事项保存在本机和飞书，正在恢复页面。</p><a href="\(url)">重新加载</a>
        """, baseURL: nil)
        guard retries < 3 else { return }
        retries += 1
        Task { @MainActor [weak self] in
            try? await Task.sleep(for: .seconds(1))
            self?.reload()
        }
    }

    func windowWillClose(_ notification: Notification) { NSApp.setActivationPolicy(.accessory) }
}

/// Alerts: a macOS notification when the system allows this app to send one; otherwise (an app without a
/// developer signature is often never registered) 心响 shows its own banner, which always works.
final class Notifier: NSObject, UNUserNotificationCenterDelegate, @unchecked Sendable {
    static let shared = Notifier()
    let root = Bundle.main.object(forInfoDictionaryKey: "AgentMatrixRoot") as? String ?? ""
    private var allowed = false

    func setup() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound]) { _, _ in
            center.getNotificationSettings { s in
                self.allowed = s.authorizationStatus == .authorized || s.authorizationStatus == .provisional
            }
        }
    }

    @MainActor func post(_ title: String, _ body: String, item: Int?) {
        if allowed {
            let content = UNMutableNotificationContent()
            content.title = title; content.body = body; content.sound = .default
            content.userInfo = ["n": item ?? -1]
            UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
        } else {
            Banner.shared.show(title: title, text: body, item: item, root: root)
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        let root = self.root, n = response.notification.request.content.userInfo["n"] as? Int ?? -1
        Task { @MainActor in CenterWindow.shared.show(root, item: n > 0 ? n : nil) }
        done()
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .sound])
    }
}

/// 心响's own notification: a small card at the top right, with a sound; click opens the item.
@MainActor
final class Banner {
    static let shared = Banner()
    private var panels: [NSPanel] = []
    private let size = NSSize(width: 360, height: 86)

    func show(title: String, text: String, item: Int?, root: String) {
        guard let screen = NSScreen.main else { return }
        let panel = NSPanel(contentRect: NSRect(origin: .zero, size: size), styleMask: [.nonactivatingPanel, .borderless],
                            backing: .buffered, defer: false)
        panel.level = .statusBar; panel.isFloatingPanel = true; panel.hidesOnDeactivate = false
        panel.backgroundColor = .clear; panel.isOpaque = false; panel.hasShadow = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        let close: () -> Void = { [weak self, weak panel] in if let self, let panel { self.dismiss(panel) } }
        panel.contentView = NSHostingView(rootView: BannerView(title: title, text: text, open: {
            CenterWindow.shared.show(root, item: item); close()
        }, close: close))
        panels.append(panel)
        place(screen)
        panel.orderFrontRegardless()
        NSSound(named: "Glass")?.play()
        Task { @MainActor in try? await Task.sleep(for: .seconds(12)); close() }
    }

    private func dismiss(_ panel: NSPanel) {
        panel.orderOut(nil)
        panels.removeAll { $0 === panel }
        if let screen = NSScreen.main { place(screen) }
    }

    private func place(_ screen: NSScreen) {
        let area = screen.visibleFrame, gap: CGFloat = 10
        for (k, p) in panels.enumerated() {
            p.setFrameOrigin(NSPoint(x: area.maxX - size.width - gap, y: area.maxY - (size.height + gap) * CGFloat(k + 1)))
        }
    }
}

struct BannerView: View {
    let title: String; let text: String; let open: () -> Void; let close: () -> Void
    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(nsImage: NSApp.applicationIconImage).resizable().frame(width: 38, height: 38)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.system(size: 13, weight: .semibold)).lineLimit(1)
                Text(text).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(2)
            }
            Spacer(minLength: 0)
            Button(action: close) { Image(systemName: "xmark").font(.system(size: 10, weight: .bold)) }
                .buttonStyle(.plain).foregroundStyle(.secondary)
        }
        .padding(12)
        .frame(width: 360, height: 86, alignment: .topLeading)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color.primary.opacity(0.08)))
        .contentShape(Rectangle())
        .onTapGesture(perform: open)
    }
}

struct News: Identifiable { let id: Int; let title: String; let body: String; let n: Int?; let time: String; let read: Bool }

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
    @Published var unread = 0
    // what the one-glance menu shows (主动层)
    @Published var focusTotal = 0
    @Published var focusDetail = ""
    @Published var aiLine = "读取中…"
    @Published var aiBusy = false
    @Published var quietLine = ""
    @Published var recent: [News] = []
    // The AI employee: what it is doing now, what is queued, how the last one ended.
    @Published var workerState = "idle"
    @Published var workerHead = ""
    @Published var workerStep = ""
    @Published var workerQueue = ""
    @Published var workerLast = ""
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
    private var refreshing = false
    private var restarting = false

    init() {
        root = Bundle.main.object(forInfoDictionaryKey: "AgentMatrixRoot") as? String ?? ""
        lastSeen = UserDefaults.standard.integer(forKey: "lastSeenEvent")
        Notifier.shared.setup()
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
        let out = Pipe(); p.standardOutput = out; p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return nil }
        let timeout = DispatchWorkItem { if p.isRunning { p.terminate() } }
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 30, execute: timeout)
        defer { timeout.cancel() }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return data
    }

    func refresh() {
        guard !refreshing else { return }
        refreshing = true
        let root = self.root
        Task.detached {
            let data = Monitor.python(root, ["agent_matrix.monitor", "--json", "--write-html"])
            await MainActor.run { self.refreshing = false; self.apply(data) }
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
        unread = s["unread"] as? Int ?? 0
        updated = String((s["generated_at"] as? String ?? "").dropFirst(11).prefix(5))
        boardURL = s["board_url"] as? String
        pagePath = s["page"] as? String
        need = items(s["need_you"]); moving = items(s["in_progress"]); done = items(s["done_recent"])
        let w = s["worker"] as? [String: Any] ?? [:]
        workerState = w["state"] as? String ?? "idle"
        let wn = number(w["n"]), wt = w["title"] as? String ?? "", step = w["step"] as? String ?? ""
        switch workerState {
        case "working":
            workerHead = "正在做 #\(wn) \(wt)"
            workerStep = "\(step) · 已 \(w["minutes"] as? Int ?? 0) 分钟"
        case "extracting": workerHead = "正在\(step)"; workerStep = wt
        case "checking": workerHead = "在看有没有要做的"; workerStep = ""
        case "error": workerHead = "状态读不到"; workerStep = ""
        default: workerHead = "空闲，有新的事会自动开始"; workerStep = ""
        }
        let q = (w["queue"] as? [[String: Any]] ?? []).map { "#\(number($0["n"])) \($0["title"] as? String ?? "")" }
        workerQueue = q.isEmpty ? "" : "排队：" + q.prefix(4).joined(separator: "、") + (q.count > 4 ? " 等 \(q.count) 件" : "")
        if let l = w["last"] as? [String: Any] {
            workerLast = "上一件 \(l["time"] as? String ?? "")：#\(number(l["n"])) \(l["outcome"] as? String ?? "") · \(l["text"] as? String ?? "")"
        } else { workerLast = "" }
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
        let f = s["focus"] as? [String: Any] ?? [:]
        focusTotal = f["total"] as? Int ?? need.count
        focusDetail = [("验收", f["review"]), ("AI 问你", f["ask"]), ("定谁来做", f["decide"]), ("其他", f["other"])]
            .compactMap { label, v in (v as? Int ?? 0) > 0 ? "\(label) \(v as? Int ?? 0)" : nil }.joined(separator: " · ")
        let ai = s["ai"] as? [String: Any] ?? [:]
        aiLine = ai["line"] as? String ?? ""; aiBusy = ai["busy"] as? Bool ?? false
        recent = (s["recent"] as? [[String: Any]] ?? []).map {
            News(id: $0["id"] as? Int ?? 0, title: $0["title"] as? String ?? "", body: $0["body"] as? String ?? "",
                 n: $0["n"] as? Int, time: $0["time"] as? String ?? "", read: $0["read"] as? Bool ?? true)
        }
        let a = s["alerts"] as? [String: Any] ?? [:]
        quietLine = (a["quiet"] as? Bool ?? false) ? "\(a["why"] as? String ?? "你在开会")，提醒先攒着，散会后一起给你" : ""
        notifyAlerts(a)
    }

    /// Interrupt only for what the 主动层 marked important, never twice, and not while the owner is in a meeting.
    private func notifyAlerts(_ a: [String: Any]) {
        let items = (a["items"] as? [[String: Any]] ?? []).sorted { ($0["id"] as? Int ?? 0) < ($1["id"] as? Int ?? 0) }
        let newest = items.last?["id"] as? Int ?? 0
        var seen = UserDefaults.standard.integer(forKey: "lastAlertId")
        if seen == 0 { UserDefaults.standard.set(max(newest, 1), forKey: "lastAlertId"); return }  // first run: no flood of old news
        if a["quiet"] as? Bool ?? false { return }  // in a meeting: they wait, nothing is lost
        let fresh = items.filter { ($0["id"] as? Int ?? 0) > seen }
        for x in fresh.prefix(3) {
            Notifier.shared.post(x["title"] as? String ?? "心响", x["body"] as? String ?? "", item: x["n"] as? Int)
        }
        if fresh.count > 3 { Notifier.shared.post("心响", "还有 \(fresh.count - 3) 条新消息，打开心响看", item: nil) }
        if let last = fresh.last?["id"] as? Int { seen = last; UserDefaults.standard.set(seen, forKey: "lastAlertId") }
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
        guard !restarting else { return }
        restarting = true
        if !quiet { busy = "正在重启监听…" }
        Task.detached {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
            p.arguments = ["kickstart", "-k", "gui/\(getuid())/com.agent-matrix.listen"]
            do { try p.run(); p.waitUntilExit() } catch {}
            await MainActor.run { self.restarting = false; self.busy = ""; self.refresh() }
        }
    }

    func open(_ s: String?) { if let s, let u = URL(string: s) { NSWorkspace.shared.open(u) } }
    func perform(_ p: Problem) {
        switch p.action {
        case "restart_listener": restartListener()
        case "subscribe": p.urls.forEach { open($0) }  // links come from the local listener, never from the code
        default: break
        }
    }

    var symbol: String { level == "error" ? "exclamationmark.triangle" : "waveform.path.ecg" }
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
            Button { CenterWindow.shared.show(m.root) } label: {
                VStack(alignment: .leading, spacing: 4) {
                    HStack(alignment: .firstTextBaseline) {
                        Text(m.focusTotal > 0 ? "\(m.focusTotal) 件等你" : "没有要你处理的 ✓").font(.system(size: 20, weight: .bold))
                        Spacer()
                        Text("打开心响 →").font(.system(size: 12, weight: .semibold)).foregroundStyle(Color.accentColor)
                    }
                    if m.focusTotal > 0 { Text(m.focusDetail).font(.system(size: 12)).foregroundStyle(.secondary) }
                }
                .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 10).fill(Color.accentColor.opacity(0.10)))
                .contentShape(Rectangle())
            }.buttonStyle(.plain)

            HStack(spacing: 6) {
                Circle().fill(m.aiBusy ? Color.blue : Color.green).frame(width: 7, height: 7)
                Text(m.aiLine).font(.system(size: 12)).lineLimit(1)
            }
            if !m.quietLine.isEmpty { Text(m.quietLine).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(2) }

            if !m.recent.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    Text("最新").font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                    ForEach(m.recent) { x in
                        Button { CenterWindow.shared.show(m.root, item: x.n) } label: {
                            HStack(alignment: .top, spacing: 8) {
                                Text(x.time).font(.system(size: 11).monospacedDigit()).foregroundStyle(.secondary)
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(x.title).font(.system(size: 12, weight: x.read ? .regular : .semibold)).lineLimit(1)
                                    Text(x.body).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
                                }
                                Spacer(minLength: 0)
                                if !x.read { Circle().fill(Color.accentColor).frame(width: 6, height: 6).padding(.top, 5) }
                            }.contentShape(Rectangle())
                        }.buttonStyle(.plain)
                    }
                }
            }

            ForEach(m.problems.filter { $0.level == "error" }) { p in
                HStack {
                    Text("⚠︎ " + p.title).font(.system(size: 12, weight: .semibold)).foregroundStyle(.red).lineLimit(2)
                    Spacer()
                    if p.action == "restart_listener" { Button("修复") { m.perform(p) }.controlSize(.small) }
                }
            }
            if !m.busy.isEmpty { Text(m.busy).font(.system(size: 11)).foregroundStyle(Color.accentColor).lineLimit(2) }

            HStack {
                Spacer()
                Menu {
                    Button("立即检查一次") { m.run("检查中…", ["agent_matrix.reply", "poll"]) }
                    Button("重启监听") { m.restartListener() }
                    Divider()
                    Button("退出心响") { NSApp.terminate(nil) }
                } label: { Image(systemName: "ellipsis.circle") }
                .menuStyle(.borderlessButton).fixedSize()
            }
        }
        .padding(14)
        .frame(width: 320)
    }
}

/// Opening 心响 again (Spotlight, Launchpad, Dock) while it runs in the menu bar shows the main window.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        Task { @MainActor in CenterWindow.shared.show(Notifier.shared.root) }
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        let root = Notifier.shared.root
        Task { @MainActor in CenterWindow.shared.show(root) }
        return true
    }
}

@main
struct AgentMatrixMonitorApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var monitor = Monitor()
    var body: some Scene {
        MenuBarExtra {
            Panel(m: monitor)
        } label: {
            HStack(spacing: 3) {
                Image(systemName: monitor.symbol)
                if monitor.focusTotal > 0 { Text("\(monitor.focusTotal)") }
            }
        }
        .menuBarExtraStyle(.window)
        .commands {
            CommandGroup(after: .newItem) {
                Button("刷新心响") { CenterWindow.shared.reload() }
                    .keyboardShortcut("r", modifiers: .command)
            }
        }
    }
}
