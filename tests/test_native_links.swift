// Run on macOS:
// swiftc -parse-as-library macapp/ClientLinks.swift tests/test_native_links.swift -o /tmp/test-native-links
// /tmp/test-native-links
import AppKit
import WebKit

@MainActor
final class LinkHarness: NSObject, WKNavigationDelegate {
    let client = URL(string: "http://127.0.0.1:8765/")!
    let web: WKWebView
    var links: ClientLinks!
    var ready = false
    var allowOpen = true
    var opened: [URL] = []
    var policies: [(URL, WKNavigationActionPolicy)] = []

    override init() {
        let configuration = WKWebViewConfiguration()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = true
        web = WKWebView(frame: NSRect(x: 0, y: 0, width: 800, height: 600), configuration: configuration)
        super.init()
        links = ClientLinks(clientURL: client) { [unowned self] url in
            opened.append(url)
            return allowOpen
        }
        web.navigationDelegate = self
        web.uiDelegate = links
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { ready = true }
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        let policy = links.policy(for: action, in: webView)
        if ready, let url = action.request.url { policies.append((url, policy)) }
        // Observe internal navigation policy without loading a real server in this test.
        if ready, action.targetFrame?.isMainFrame == true, policy == .allow {
            decisionHandler(.cancel)
        } else { decisionHandler(policy) }
    }

    func require(_ condition: @autoclosure () -> Bool, _ message: String) {
        guard condition() else { print("FAIL: \(message)"); exit(1) }
    }

    func wait(_ predicate: () -> Bool, _ message: String) {
        let deadline = Date().addingTimeInterval(8)
        while !predicate(), Date() < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.02))
        }
        require(predicate(), message)
    }

    func js(_ source: String) -> Any? {
        var finished = false
        var value: Any?
        web.evaluateJavaScript(source) { result, error in
            self.require(error == nil, "JavaScript failed: \(String(describing: error))")
            value = result
            finished = true
        }
        wait({ finished }, "JavaScript timed out")
        return value
    }

    func click(_ id: String) {
        let count = policies.count
        let openCount = opened.count
        _ = js("document.getElementById('\(id)').click()")
        // window.open can reach WKUIDelegate directly, without navigation policy.
        wait({ policies.count > count || opened.count > openCount }, "No navigation for \(id)")
        // Catch a duplicate open from the second delegate callback.
        RunLoop.current.run(until: Date().addingTimeInterval(0.15))
    }

    func run() {
        web.loadHTMLString("""
        <script>window.notice = ""; window.toast = text => window.notice = text;</script>
        <a id="delivery" href="https://example.com/delivery?version=2" target="_blank">打开交付</a>
        <a id="source" href="https://example.com/source">打开原处</a>
        <a id="named" href="https://example.com/document" target="document">打开文档</a>
        <a id="internal" href="http://127.0.0.1:8765/#detail">本地页面</a>
        <a id="port" href="http://127.0.0.1:9999/">其他服务</a>
        <a id="unsupported" href="unknown-xinxiang://example">无处理程序</a>
        <a id="failed" href="https://example.com/failed" target="_blank">无法打开</a>
        <button id="script" onclick="window.open('https://example.com/script')">脚本链接</button>
        """, baseURL: client)
        wait({ ready }, "Fixture did not load")

        for (id, url) in [
            ("delivery", "https://example.com/delivery?version=2"),
            ("source", "https://example.com/source"),
            ("named", "https://example.com/document"),
            ("port", "http://127.0.0.1:9999/"),
            ("script", "https://example.com/script")
        ] {
            let count = opened.count
            click(id)
            require(opened.count == count + 1, "\(id) must open exactly once")
            require(opened.last?.absoluteString == url, "\(id) opened the wrong URL")
        }
        let count = opened.count
        click("internal")
        require(opened.count == count, "Internal navigation escaped to browser")
        require(policies.last?.1 == .allow, "Local navigation was blocked")
        click("unsupported")
        require(opened.count == count, "Unsupported scheme was opened")
        require((js("window.notice") as? String)?.contains("暂不支持") == true, "Missing unsupported-link feedback")
        allowOpen = false
        click("failed")
        require((js("window.notice") as? String)?.contains("未能打开") == true, "Missing failed-open feedback")
        require(web.url?.host == client.host, "External link replaced the client")
        print("PASS: 8 native WebKit link cases; exact URL, one open, local page retained, failures visible")
    }
}

@main
struct NativeLinkTests {
    @MainActor static func main() {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.prohibited)
        LinkHarness().run()
    }
}
