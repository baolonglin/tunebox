import AVFoundation
import os

/// Owns this app's audio session — which is the entire reason the wrapper exists.
///
/// The web app on its own could not hold one. Measured in the car on 2026-11-07, from the service
/// log on the host: the page played for 79 seconds, its position record stopped advancing at 59
/// seconds (it is written every five), and the shell was fetched again twenty seconds after that.
/// Nothing had failed on the network — 17 media requests, every one a `206`, no `4xx` or `5xx` all
/// day, and after the relaunch it streamed for seventeen minutes over mobile data and a VPN with
/// four track boundaries +1 s, +2 s, +0 s and +1 s late. What went wrong was the *claim*: Apple
/// Music had been interrupted when tunebox started, iOS had marked it resumable, and the moment the
/// page's session lapsed the system handed the car's speakers back to it.
///
/// A page cannot win that argument and no amount of JavaScript changes it. An app can, by holding
/// the session itself:
///
/// * `.playback` is the category that means "this is the audio the user came for": it keeps playing
///   when the screen locks and it interrupts whatever was playing before.
/// * **`setActive(false)` is never called while a track is loaded**, and never with
///   `.notifyOthersOnDeactivation` at all. That option is precisely the signal that tells an
///   interrupted app to resume, so calling it would re-create the bug on purpose. The session is
///   taken once, at launch, and kept.
/// * An interruption — a phone call, Siri — is the one thing that can take it away. iOS gives it
///   back with `.shouldResume` when the call ends, and that is the only moment this type asks for
///   the session again.
enum AudioSessionKeeper {
    private static let log = Logger(subsystem: "io.tunebox.app", category: "audio")

    /// What to do when an interruption ends and the session is ours again.
    ///
    /// Injected rather than reached for, because the only thing that knows whether a track was
    /// playing is the page, and the only thing that can ask it is the web host.
    static var onInterruptionEnded: (() -> Void)?

    static func begin() {
        let session = AVAudioSession.sharedInstance()
        do {
            // `.longFormAudio` is deliberately not used: it is the right description of a music
            // player, and it also makes the system route-picker behave as one — but it can be
            // refused when another long-form app holds the route, and a session we failed to take
            // is the failure this whole file exists to prevent. `.default` cannot be refused for
            // that reason.
            try session.setCategory(.playback, mode: .default, policy: .default)
            try session.setActive(true)
            log.info("audio session active: category=playback")
        } catch {
            // Logged and not surfaced: the app still plays audio without this, it just loses the
            // argument with whatever was playing before, which is what the web app already did.
            log.error("could not take the audio session: \(error.localizedDescription, privacy: .public)")
        }

        NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: session,
            queue: .main
        ) { note in handle(interruption: note) }
    }

    private static func handle(interruption note: Notification) {
        guard
            let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
            let type = AVAudioSession.InterruptionType(rawValue: raw)
        else { return }

        switch type {
        case .began:
            // Nothing to do. The system has already stopped our audio, and asking the page to
            // pause as well would leave it paused after the call rather than resuming.
            log.info("interrupted")
        case .ended:
            let options = (note.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt).map(
                AVAudioSession.InterruptionOptions.init(rawValue:)
            )
            do {
                try AVAudioSession.sharedInstance().setActive(true)
            } catch {
                log.error("could not retake the session: \(error.localizedDescription, privacy: .public)")
                return
            }
            log.info("interruption ended, shouldResume=\(options?.contains(.shouldResume) == true)")
            // Only when the system says so. Resuming anyway would start the music in somebody's ear
            // after a call they took for a reason.
            if options?.contains(.shouldResume) == true { onInterruptionEnded?() }
        @unknown default:
            break
        }
    }
}
