import SwiftUI
import UIKit

/// tunebox, as an app, for exactly one reason: so the audio session belongs to it.
///
/// `ios/README.md` has the measurement that put this directory here. In one line: a web page's
/// claim on iOS's audio session does not outrank a native app's, so Apple Music — interrupted when
/// tunebox started, and marked resumable by the system — took the car's speakers back the moment
/// the backgrounded page stopped being scheduled. Everything on screen is still the web app.
@main
struct TuneboxApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        WindowGroup {
            WebHostView()
                // The page already handles the notch and the home indicator itself: its floating
                // header carries `env(safe-area-inset-top)` and the body carries the other three.
                // Insetting the web view as well would be the same gap twice.
                .ignoresSafeArea()
        }
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions options: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        // Before any web content loads, so the session is ours from the first frame rather than
        // from the first time the page happens to play something.
        AudioSessionKeeper.begin()
        return true
    }
}
