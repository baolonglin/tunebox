#!/usr/bin/env bash
#
# Build the wrapper and put it on the phone.
#
# Run it by hand, or let launchd run it (--install-agent) so that the seven-day free-provisioning
# window is refreshed whenever the phone and this Mac can see each other. See ios/README.md.
#
#   ./ios/build-and-install.sh                 # build, then install to the first phone it finds
#   ./ios/build-and-install.sh --dry-run       # print every step, change nothing
#   ./ios/build-and-install.sh --device UDID   # a specific one
#   ./ios/build-and-install.sh --install-agent # refresh daily, unattended, while the phone is seen
#   ./ios/build-and-install.sh --uninstall-agent
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$HERE/Tunebox.xcodeproj"
SCHEME="Tunebox"
DERIVED="${TMPDIR:-/tmp}/tunebox-ios-build"
AGENT_LABEL="io.tunebox.resign"
AGENT_PLIST="$HOME/Library/LaunchAgents/$AGENT_LABEL.plist"
LOG_DIR="$HOME/Library/Logs/tunebox"

DRY_RUN=0
DEVICE=""

say() { printf '==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '    would run: %s\n' "$*"
    return 0
  fi
  "$@"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --device)
      shift
      [ $# -gt 0 ] || die "--device needs a UDID"
      DEVICE="$1"
      ;;
    --install-agent) MODE=install-agent ;;
    --uninstall-agent) MODE=uninstall-agent ;;
    -h | --help)
      sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

# ---------------------------------------------------------------------------- the launchd agent

install_agent() {
  say "installing the launchd agent $AGENT_LABEL"
  note "it rebuilds and reinstalls once a day, and does nothing at all when the phone is not there"
  run mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
  if [ "$DRY_RUN" = 1 ]; then
    note "would write $AGENT_PLIST"
  else
    cat >"$AGENT_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$AGENT_LABEL</string>
	<key>ProgramArguments</key>
	<array>
		<string>$HERE/build-and-install.sh</string>
	</array>
	<!-- Daily, not weekly: the profile lasts seven days, so a daily attempt means six missed runs
	     in a row before anything stops working. RunAtLoad so installing it is also a first run. -->
	<key>StartInterval</key>
	<integer>86400</integer>
	<key>RunAtLoad</key>
	<true/>
	<key>StandardOutPath</key>
	<string>$LOG_DIR/resign.log</string>
	<key>StandardErrorPath</key>
	<string>$LOG_DIR/resign.log</string>
	<key>EnvironmentVariables</key>
	<dict>
		<!-- Emptied deliberately: a LIBRARY_PATH inherited from a shell profile made the linker pick
		     up a libSystem.B.tbd out of Emacs.app and fail the build. A supervised build must not
		     depend on whose shell started it. -->
		<key>LIBRARY_PATH</key>
		<string></string>
	</dict>
</dict>
</plist>
PLIST
    note "wrote $AGENT_PLIST"
    launchctl unload "$AGENT_PLIST" 2>/dev/null || true
    launchctl load "$AGENT_PLIST"
    note "loaded. log: $LOG_DIR/resign.log"
  fi
}

uninstall_agent() {
  say "removing the launchd agent $AGENT_LABEL"
  run launchctl unload "$AGENT_PLIST" 2>/dev/null || true
  run rm -f "$AGENT_PLIST"
  note "the app already on the phone is untouched; it stops opening when its profile expires"
}

case "${MODE:-build}" in
  install-agent)
    install_agent
    exit 0
    ;;
  uninstall-agent)
    uninstall_agent
    exit 0
    ;;
esac

# ------------------------------------------------------------------------------------- the build

[ -d "$PROJECT" ] || die "no Xcode project at $PROJECT"

if [ -z "$DEVICE" ]; then
  say "looking for a paired phone"
  # Both reachable states count, and the distinction is worth knowing. `available` is a phone with
  # the developer disk image mounted — ready. `connected (no DDI)` is one this Mac can see but whose
  # Developer Mode is off or not yet trusted; the install then fails with a message that says so,
  # which is more use than this script deciding the phone is absent. Either is attempted; a phone
  # listed as `unavailable` is not.
  DEVICE="$(
    xcrun devicectl list devices 2>/dev/null |
      awk '
        /iPhone/ && !/unavailable/ && (/available/ || /connected/) {
          for (i = 1; i <= NF; i++) if ($i ~ /^[0-9A-F]{8}-[0-9A-F]{4}-/) { print $i; exit }
        }'
  )" || true
  if [ -z "$DEVICE" ]; then
    # Not an error: this is the normal state most of the time the agent fires, and a failing exit
    # status every hour is how a scheduled job gets switched off.
    say "no phone in sight — nothing to do"
    exit 0
  fi
fi
note "device: $DEVICE"

say "building $SCHEME"
# `env -u LIBRARY_PATH` for the reason recorded in the agent above. `-allowProvisioningUpdates` is
# what renews the free seven-day profile without opening Xcode — it needs the Apple ID already in
# Xcode's accounts, and it will occasionally still want a human for a 2FA prompt.
run env -u LIBRARY_PATH -u LD_LIBRARY_PATH -u CPATH \
  xcodebuild -project "$PROJECT" -scheme "$SCHEME" \
  -sdk iphoneos -configuration Debug \
  -derivedDataPath "$DERIVED" \
  -allowProvisioningUpdates \
  build

APP="$DERIVED/Build/Products/Debug-iphoneos/Tunebox.app"
if [ "$DRY_RUN" = 0 ]; then
  [ -d "$APP" ] || die "the build produced no app at $APP"
fi

say "installing to $DEVICE"
# `install app` on an existing bundle id is an *upgrade*: the data container survives, which matters
# more here than it looks. This device's queue, its place in the track and the token all live in the
# web view's storage inside that container — an uninstall would throw away the only copy.
run xcrun devicectl device install app --device "$DEVICE" "$APP"

say "done"
note "the free provisioning profile is good for seven days from now"
note "--install-agent makes that somebody else's problem"
