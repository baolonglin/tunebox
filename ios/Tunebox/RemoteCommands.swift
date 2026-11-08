import MediaPlayer
import os

/// The lockscreen, the steering wheel, a headphone button and CarPlay, wired to native code.
///
/// This is the second half of what the wrapper buys. The web app's Media Session handlers only run
/// while the page is still being scheduled, which is what made the lockscreen's own play button
/// silent after a pause: a remote command arrived at a page iOS had stopped running. These handlers
/// are in the app, so they run even when the web view is throttled — and a remote command can wake
/// the app from suspended, which is the mechanism "car connects, music plays" needs and which no
/// page can be on the receiving end of.
///
/// Each command is forwarded as a **synthetic tap on the page's own button** rather than by calling
/// into its code. That is not a shortcut: `app.js` is an IIFE, so none of `localToggle` and friends
/// are reachable from `evaluateJavaScript`, and the alternative — exporting a bridge — would be a
/// second entry point into the transport that could drift from what a thumb does. A `.click()` on
/// `#btn-toggle` is the same path as the thumb, and the web app needs no change at all.
enum RemoteCommands {
    private static let log = Logger(subsystem: "io.tunebox.app", category: "remote")

    /// The page's own transport buttons. Ids, because that is what the markup commits to and what
    /// `tests/test_ui.py` already cross-checks against `app.js`.
    enum Button: String {
        case toggle = "btn-toggle"
        case next = "btn-next"
        case previous = "btn-prev"
        case stop = "btn-stop"
    }

    static func wire(_ tap: @escaping (Button) -> Void) {
        let centre = MPRemoteCommandCenter.shared()

        // `play` and `pause` both go to the toggle, for the same reason the page's own Media Session
        // handlers do: one function means the lockscreen and the page cannot drift apart. The page
        // is the only thing that knows which of the two the button currently means.
        centre.playCommand.addTarget { _ in forward(.toggle, "play", tap) }
        centre.pauseCommand.addTarget { _ in forward(.toggle, "pause", tap) }
        centre.togglePlayPauseCommand.addTarget { _ in forward(.toggle, "toggle", tap) }
        centre.nextTrackCommand.addTarget { _ in forward(.next, "next", tap) }
        centre.previousTrackCommand.addTarget { _ in forward(.previous, "previous", tap) }
        centre.stopCommand.addTarget { _ in forward(.stop, "stop", tap) }

        // Seeking and scrubbing are deliberately left off. The page owns the progress bar and the
        // position it persists; a second way to move the playhead would be a second source of truth
        // for the one number this device writes to its own storage every five seconds.
        centre.changePlaybackPositionCommand.isEnabled = false
        centre.seekForwardCommand.isEnabled = false
        centre.seekBackwardCommand.isEnabled = false
    }

    private static func forward(
        _ button: Button,
        _ name: String,
        _ tap: @escaping (Button) -> Void
    ) -> MPRemoteCommandHandlerStatus {
        log.info("remote command \(name, privacy: .public) -> #\(button.rawValue, privacy: .public)")
        tap(button)
        return .success
    }
}
