# tunebox, as an iOS app

A ~200-line wrapper around the web remote, built for **one** reason: so that iOS's audio session
belongs to an app instead of to a web page. Everything on screen is still `src/tunebox/web/` — the
same markup, CSS and script a browser gets, from the same host. The queue state machine, the
shuffle, the prefetch, the position record and the four panels are not rewritten here, because none
of them were what went wrong.

## The measurement that put this directory here

In the car on 2026-11-07: Bluetooth connects, Apple Music starts itself, tunebox is opened and plays,
the phone switches to navigation — and Apple Music starts again. Read off the service log on the
host afterwards:

| What the log says | Time |
|---|---|
| playback starts (甲乙丙丁) | 07:35:31 |
| the position record stops advancing, at **59 s** into the track (it is written every 5 s) | ~07:36:30 |
| the shell is fetched again — a **new page**; the old one is gone | 07:36:50 |
| the same track resumes from 59 s | 07:36:56 |

And the part that ruled out every other explanation: **nothing had failed.** Seventeen media
requests that day, every one a `206`, no `4xx` or `5xx` at all — and after the relaunch it streamed
for seventeen minutes over mobile data and a VPN with four track boundaries **+1 s, +2 s, +0 s and
+1 s** late. The network was fine. The prefetch was fine. What failed was the *claim*: Apple Music
had been interrupted when tunebox started, iOS had marked it resumable, and the moment the
backgrounded page's audio session lapsed the system handed the car's speakers back to it.

A page cannot win that argument. The looping silence in `src/tunebox/web/silence.m4a` buys a track
boundary — measured, eight of them — but it is still WebKit's session, and WebKit's session does not
outrank a native app's `shouldResume`. No amount of JavaScript changes that, which is why this is a
wrapper and not another fix in `app.js`.

## What the wrapper does, and nothing else

- **`UIBackgroundModes: audio`** in `Info.plist`. The declaration that stops iOS suspending the app.
- **`AudioSessionKeeper`** takes `AVAudioSession` with the `.playback` category at launch and keeps
  it. `setActive(false)` is never called while a track is loaded, and
  `.notifyOthersOnDeactivation` is never passed at all — that option *is* the signal that tells an
  interrupted app to resume, so using it would recreate the bug on purpose. A phone call is the one
  thing that can take the session away, and iOS gives it back with `.shouldResume`, which is the
  only moment the app asks for it again.
- **`RemoteCommands`** puts the lockscreen, a headphone button, the steering wheel and CarPlay onto
  native handlers. This is the half that makes "car connects, music plays" reachable: a remote
  command can wake an app from suspended, and no page can be on the receiving end of that. Each
  command is forwarded as a **synthetic tap on the page's own button** (`#btn-toggle` and friends)
  rather than by calling into `app.js` — whose functions live in an IIFE and are not reachable from
  `evaluateJavaScript` anyway. The button is the same path a thumb takes, and the web app needs no
  change at all.
- **`WebHost`** is a `WKWebView` with three settings that matter:
  `mediaTypesRequiringUserActionForPlayback = []` (a page may start audio without a tap — not
  available in Safari at any price, which is what made README's "it cannot start the music when you
  get in the car" true), `allowsInlineMediaPlayback`, and the **default**, persistent website data
  store so `localStorage` survives a relaunch.

Deliberately absent: scrubbing and seek commands, picture-in-picture, any native UI, and any native
copy of the token. The page's own gate fills the token in once and keeps it in the web view's
storage, which survives an upgrade-install — so there is no second place for the shared secret to
live. `Info.plist` allows plain HTTP to **one** address and nothing else: no
`NSAllowsArbitraryLoads`, no wildcard, no subdomains. One address is enough because the router's VPN
puts the phone *on* the home network rather than giving the host a second name — which is why
`10.6.0.2` reached `192.168.50.93` from the car with nothing special configured.

## The seven-day problem, and what can be done about it

A **free** Apple ID signs an app for seven days. After that it will not open until it is rebuilt and
reinstalled. That is Apple's limit and nothing here can extend it — only refresh it.

```bash
./ios/build-and-install.sh                  # build, install to the phone it finds
./ios/build-and-install.sh --dry-run        # print every step, change nothing
./ios/build-and-install.sh --install-agent  # let launchd do it, daily
```

`--install-agent` writes `~/Library/LaunchAgents/io.tunebox.resign.plist` alongside the three the
rest of this project installs. It runs **daily** rather than weekly, so six consecutive missed runs
are survivable, and it exits `0` doing nothing when the phone is not in sight — a scheduled job that
failed every day is a scheduled job somebody switches off. Reinstalling the same bundle id is an
*upgrade*, so the data container survives: this device's queue, its place in the track, and the
token are all in the web view's storage and an uninstall would be the only copy gone. The log is
`~/Library/Logs/tunebox/resign.log`.

Two honest limits on the automation:

- **If this Mac does not see the phone for seven days, the app stops opening** — and that is most
  likely to happen while away from home, which is exactly when it is wanted. Charging the phone from
  the Mac is enough to count as seeing it.
- **`-allowProvisioningUpdates` is not unconditionally unattended.** It renews the profile using the
  Apple ID already in Xcode's accounts, but a 2FA prompt, a password change or an expired session
  will want a human. The agent will log the failure and try again tomorrow.

The alternative to all of this is a paid Apple Developer account, which turns seven days into a year
and allows installing over the air through TestFlight. For one household's music remote that is a
subscription to avoid a daily background job, which is why it is not what this does.

## First run — the three things only you can do

The build itself is scripted; these are not, because they need a person.

1. **Open `ios/Tunebox.xcodeproj` in Xcode once**, select the `Tunebox` target → *Signing &
   Capabilities* → **Team**, and pick the Personal Team for your Apple ID. This is what creates the
   free signing certificate, and Apple requires a human to agree to it. It writes `DEVELOPMENT_TEAM`
   into the project, after which `build-and-install.sh` needs no Xcode.
2. **Developer Mode on the phone**: Settings → Privacy & Security → Developer Mode → on, then
   reboot. `xcrun devicectl list devices --json-output -` reports whether it is already done
   (`developerModeStatus`), which is worth checking first — the `connected (no DDI)` state in the
   device list is the disk image being mounted on demand and not evidence that it is off.

   On a phone an employer manages, this is the setting most likely to be restricted, and the
   `developerModeStatus` reading is how to tell rather than guess. Measured on this household's
   phone, which carries a company MDM profile: `enabled`, so the restriction was not in force.
   Worth being clear about the other half of that, though — a *supervised* device lets the MDM
   enumerate installed apps, so a personal app on a work phone is a thing the employer can see, and
   whether that is allowed is a policy question and not a technical one.
3. **Trust the certificate. This one is required**, and an earlier draft of this file guessed it
   was not — on the theory that a development profile naming the device's own UDID is enough once
   Developer Mode is on. Measured on iOS 26.7.1 with a free Personal Team: the first launch is
   refused and the account is listed as *not trusted*.

   Settings → General → VPN & Device Management → under **DEVELOPER APP**, your Apple ID → *Trust*.
   Once per certificate, not once per install, so the seven-day reinstalls do not ask again. The
   phone needs working internet the first time, because iOS checks the certificate with Apple
   rather than taking the profile's word for it.

   **On a phone an employer manages, this is where it can stop.** Measured on the first phone tried
   here, which carries a company MDM profile: the Trust button is not greyed out, it is *absent*,
   and iOS says why in a sentence on that same screen — "Some restrictions are enforced by
   «the organisation»". The `allowEnterpriseAppTrust` restriction removes the affordance rather
   than refusing the tap.

   Everything else had worked: the app was signed, installed, and listed on that screen as
   **Verified** — the bundle was never the problem, only the permission to trust its developer. Two
   restrictions are worth telling apart here, because the first one being absent says nothing about
   the second: Developer Mode was `enabled` on that phone, and trusting a developer certificate was
   still forbidden.

   This is a device-management policy on a device somebody else administers, and it is a wall rather
   than an obstacle — not something to work around. The phone used for the rest of this file is a
   personal one.

Then, on the phone: the page's own token gate appears once — paste the token (`tunebox pair` prints
it as a QR code) — and that is the last time.

## How to tell whether it worked

The web app is untouched, so the comparison is direct. In the car, with Apple Music having
auto-started on connect: open tunebox, play something, switch to navigation. **Apple Music must not
come back.** The host's log is the second half of the answer, and the idiom for reading it is in the
main README — a `POST /library/tickets` with no `GET /media/` after it is a URL minted ahead of time,
so a healthy track boundary is an audio request with no fetch in front of it.

If it works, three things in `src/tunebox/web/` become dead weight and should go: `silence.m4a`, the
`audioSession()` three-state function, and the five-minute grace period after a pause. They exist to
work around a session the app now owns outright. **That is the test of whether this was the right
fix** — the code gets smaller, rather than one more layer being added to it.
