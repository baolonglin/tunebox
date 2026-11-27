import SwiftUI
import WebKit
import os

/// The one page, in a web view that an audio session belongs to.
///
/// Everything on screen is `src/tunebox/web/` — the same markup, CSS and script the browser gets,
/// served by the same host. That is the whole point of a wrapper rather than a rewrite: the queue
/// state machine, the shuffle, the prefetch, the position record and the four panels are already
/// written and already tested, and none of them were what went wrong in the car.
///
/// Three settings on the configuration are load-bearing:
///
/// * `mediaTypesRequiringUserActionForPlayback = []` — a page may start audio without a tap. In
///   Safari this is not available at any price, which is why README's "it cannot start the music
///   when you get in the car" was true: iOS has no per-site autoplay permission, so a Shortcuts
///   automation could open the page and never press play. In an app it is a property we set.
/// * `allowsInlineMediaPlayback = true` — otherwise iOS wants to take audio full-screen.
/// * the **default** website data store, not an ephemeral one, so `localStorage` survives a
///   relaunch. That is where this device's queue and its place in the track live, and it is also
///   where the token goes once the page's own gate has been filled in — which is why a reinstall
///   must be an upgrade (same bundle id) and never an uninstall.
struct WebHostView: UIViewRepresentable {
    /// Where the service is. **Change this to your host's LAN address** — the one
    /// `deploy/install.sh` printed — and the matching `NSExceptionDomains` key in `Info.plist`.
    /// Those two are the only edits a fresh clone needs.
    ///
    /// One address, not two, because the router's VPN puts the phone *on* this network rather than
    /// giving the host a second name: measured in the car on 2026-11-07, the phone on its VPN
    /// address fetched from this LAN address with nothing special configured.
    static let serviceURL = URL(string: "http://192.168.1.100:8080/")!

    private static let log = Logger(subsystem: "io.tunebox.app", category: "web")

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.allowsInlineMediaPlayback = true
        configuration.mediaTypesRequiringUserActionForPlayback = []
        configuration.websiteDataStore = .default()
        // The page draws its own now-playing screen and its own lockscreen metadata; the system's
        // picture-in-picture would be a second player for audio that has no video.
        configuration.allowsPictureInPictureMediaPlayback = false

        let view = WKWebView(frame: .zero, configuration: configuration)
        view.isOpaque = false
        view.backgroundColor = UIColor(red: 0.067, green: 0.078, blue: 0.102, alpha: 1)  // --bg
        view.scrollView.backgroundColor = view.backgroundColor
        // No rubber-banding past the top: the page's own floating header is what should be against
        // the status bar, and a bounce reveals the app's background behind it.
        view.scrollView.bounces = false
        view.allowsLinkPreview = false

        context.coordinator.view = view
        AudioSessionKeeper.onInterruptionEnded = { [weak view] in
            // The system said the interruption is over and the music should come back. The page is
            // the only thing that knows whether it was playing, so it gets asked rather than told:
            // tapping a paused transport would start music nobody asked for after a phone call.
            Self.tapIfPlaying(in: view)
        }
        RemoteCommands.wire { button in
            Self.tap(button, in: context.coordinator.view)
        }

        view.load(URLRequest(url: Self.serviceURL))
        return view
    }

    func updateUIView(_ view: WKWebView, context: Context) {}

    final class Coordinator {
        weak var view: WKWebView?
    }

    // ------------------------------------------------------------------ talking to the page

    /// Tap one of the page's transport buttons.
    ///
    /// `click()` and not a call into `app.js`: its functions live inside an IIFE and are not
    /// reachable from here, and the button is the same path a thumb takes. Guarded on the element
    /// existing, because the page shows a token gate before the transport is in the document at
    /// all — a remote command arriving then must do nothing rather than throw.
    private static func tap(_ button: RemoteCommands.Button, in view: WKWebView?) {
        let script = """
        (() => {
          const node = document.getElementById("\(button.rawValue)");
          if (!node || node.disabled) return "absent";
          node.click();
          return "tapped";
        })()
        """
        run(script, in: view, what: button.rawValue)
    }

    /// Resume only if the page considers itself playing-but-stopped — see `onInterruptionEnded`.
    private static func tapIfPlaying(in view: WKWebView?) {
        // The button's own accessible name is the page's statement of what it will do next, and it
        // is kept in step with the icon by `renderPlayer`. "Play" means it is paused, so this is the
        // one case where resuming is what the system asked for.
        let script = """
        (() => {
          const node = document.getElementById("btn-toggle");
          if (!node || node.disabled) return "absent";
          if (node.getAttribute("aria-label") !== "Play") return "already playing";
          node.click();
          return "resumed";
        })()
        """
        run(script, in: view, what: "resume-after-interruption")
    }

    private static func run(_ script: String, in view: WKWebView?, what: String) {
        guard let view else { return }
        DispatchQueue.main.async {
            view.evaluateJavaScript(script) { result, error in
                if let error {
                    log.error("\(what, privacy: .public): \(error.localizedDescription, privacy: .public)")
                } else {
                    log.info("\(what, privacy: .public): \(String(describing: result), privacy: .public)")
                }
            }
        }
    }
}
