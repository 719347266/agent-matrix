import AppKit
import WebKit

/// Keep the local client in its window; use the user's browser/app for linked documents.
@MainActor
final class ClientLinks: NSObject, WKUIDelegate {
    private let clientURL: URL
    private let openURL: (URL) -> Bool

    init(clientURL: URL, openURL: @escaping (URL) -> Bool = { NSWorkspace.shared.open($0) }) {
        self.clientURL = clientURL
        self.openURL = openURL
    }

    private func isInternal(_ url: URL) -> Bool {
        func port(_ url: URL) -> Int? { url.port ?? (url.scheme == "https" ? 443 : url.scheme == "http" ? 80 : nil) }
        return url.scheme == clientURL.scheme && url.host == clientURL.host && port(url) == port(clientURL)
    }

    func policy(for action: WKNavigationAction, in webView: WKWebView) -> WKNavigationActionPolicy {
        guard let url = action.request.url else { return .cancel }
        if isInternal(url) { return .allow }
        // loadHTMLString uses about:blank for the local recovery screen.
        if url.absoluteString == "about:blank", action.navigationType != .linkActivated { return .allow }
        // New-window requests are handled only by createWebView below, once.
        if action.targetFrame == nil { return .allow }
        // External links in this window also go to the browser. An automatic
        // redirect or subframe load must not launch another application.
        if action.navigationType == .linkActivated {
            open(url, in: webView)
        }
        return .cancel
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        // Handles target=_blank / named windows and window.open. Never make a blank WKWebView.
        guard navigationAction.targetFrame == nil, let url = navigationAction.request.url else { return nil }
        if isInternal(url) {
            webView.load(navigationAction.request)
        } else {
            open(url, in: webView)
        }
        return nil
    }

    private func open(_ url: URL, in webView: WKWebView) {
        guard ["http", "https", "mailto", "feishu", "lark"].contains(url.scheme?.lowercased() ?? "") else {
            show("暂不支持这类链接，可右键复制链接。", in: webView)
            return
        }
        if !openURL(url) {
            show("链接未能打开，可右键复制链接后在浏览器中打开。", in: webView)
        }
    }

    private func show(_ message: String, in webView: WKWebView) {
        // Use the existing page toast; WKWebView's JavaScript alert/confirm is not configured.
        guard let data = try? JSONSerialization.data(withJSONObject: [message]),
              let argument = String(data: data, encoding: .utf8) else { return }
        webView.evaluateJavaScript("if (typeof window.toast === 'function') window.toast(\(argument)[0]);", completionHandler: nil)
    }
}
