#!/usr/bin/env bash
#
# Start everything, on this Mac, and say whether the speaker is really reachable.
#
#   ./deploy/start.sh            start both, then prove the Cast session works
#   ./deploy/start.sh status     what is running; changes nothing
#   ./deploy/start.sh stop       stop both
#   ./deploy/start.sh restart    stop, then start
#
# Hard-coded for this host on purpose: OwnTone here is a source build under ~/owntone_data, on no
# PATH and known to no package manager. deploy/owntone-setup.sh is the general, portable version.
#
# ---------------------------------------------------------------------------------------------------
# Why OwnTone is started from your session and *not* from a launchd agent, which would be the obvious
# way to make it survive a reboot:
#
# macOS 26 gates access to the local network per process (Privacy & Security -> Local Network). A
# launchd agent is nobody's child, gets no consent dialog, and is simply denied — and the denial does
# not look like a permissions error. Measured on this host: mDNS discovery still works, because that
# goes through mDNSResponder out of process, so OwnTone *finds* the speaker and lists it; the direct
# TCP connection to it then fails with
#
#     cast: Could not connect to Living Room speaker
#     misc: Could not connect to 'Chomecast control' at 192.168.50.13 (port 8009): No route to host
#
# while `nc -z 192.168.50.13 8009` from a shell succeeds. The player reports state=play and the
# progress bar advances, and no sound comes out of anything: the worst failure shape available.
#
# Started from a terminal the daemon inherits that terminal's local-network grant and the session
# establishes ("cast: Session ready"). So this script starts it the way that works and *verifies* the
# Cast path afterwards rather than trusting an exit code.
#
# The cost is that nothing restarts it after a reboot; running this script is the restart. To get
# supervision back, grant the permission by hand — System Settings -> Privacy & Security -> Local
# Network, enable `owntone` — and then use `./deploy/owntone-setup.sh --supervise`, which installs the
# launchd agent. Re-check with `./deploy/start.sh status`, because an ungranted agent looks healthy.
# ---------------------------------------------------------------------------------------------------
#
set -euo pipefail

OWNTONE_PREFIX="$HOME/owntone_data"
OWNTONE_BIN="$OWNTONE_PREFIX/usr/sbin/owntone"
OWNTONE_CONF="$OWNTONE_PREFIX/etc/owntone.conf"
OWNTONE_LOG="$OWNTONE_PREFIX/var/log/owntone.log"
OWNTONE_PID="$OWNTONE_PREFIX/var/run/owntone.pid"
OWNTONE_LABEL="io.tunebox.owntone"
SERVE_LABEL="io.tunebox.serve"
SERVE_AGENT="$HOME/Library/LaunchAgents/$SERVE_LABEL.plist"
ENV_FILE="${TUNEBOX_ENV_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/tunebox/tunebox.env}"
PLAYER_URL="http://127.0.0.1:3689"

if [ -t 1 ]; then
  B=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; YEL=$'\033[33m'; GRN=$'\033[32m'; N=$'\033[0m'
else
  B=""; DIM=""; RED=""; YEL=""; GRN=""; N=""
fi
say()  { printf '%s==>%s %s\n' "$B" "$N" "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '%s !! %s%s\n' "$YEL" "$*" "$N" >&2; }
die()  { printf '%serror:%s %s\n' "$RED" "$N" "$*" >&2; exit 1; }
ok()   { printf '%s ok %s%s\n' "$GRN" "$*" "$N"; }

usage() {
  cat <<EOF
Start tunebox and its player daemon on this Mac.

Usage: deploy/start.sh [start | stop | restart | status]

  start      start both, then check the speaker really answers (the default)
  status     what is running; changes nothing
  stop       stop both
  restart    stop, then start

OwnTone is started from your session rather than from a launchd agent, because macOS 26 denies a
launchd agent access to the local network and the symptom is silent: the player says it is playing
and no sound comes out. The header of this script has the measurements.
EOF
}

[ "$(uname -s)" = "Darwin" ] || die "this script is for macOS. On Debian: sudo systemctl start owntone tunebox"

GUI="gui/$(id -u)"
loaded() { launchctl print "$GUI/$1" >/dev/null 2>&1; }

# Any HTTP answer proves it is listening. Not -f: OwnTone answers 403 to a caller it does not trust.
player_up() { curl -s -m 2 -o /dev/null "$PLAYER_URL/api/config"; }

envget() {
  [ -f "$ENV_FILE" ] || return 1
  # The env file is shell source code — values are quoted — which is how the service wrapper reads it.
  # shellcheck source=/dev/null
  ( set -a; . "$ENV_FILE"; set +a; printf '%s' "${!1:-}" )
}

api_call() {
  local path="$1" token host port
  token="$(envget TUNEBOX_TOKEN)" || return 1
  host="$(envget TUNEBOX_API_HOST)"; port="$(envget TUNEBOX_API_PORT)"
  curl -s -m 5 -H "X-Tunebox-Token: $token" "http://${host:-127.0.0.1}:${port:-8080}$path"
}
api_up() { api_call /healthz >/dev/null 2>&1 && [ -n "$(api_call /healthz)" ]; }

# --------------------------------------------------------------------------------- owntone

owntone_running() { pgrep -qf "$OWNTONE_BIN"; }

start_owntone() {
  [ -x "$OWNTONE_BIN" ] || die "no owntone binary at $OWNTONE_BIN"
  [ -f "$OWNTONE_CONF" ] || die "no owntone.conf at $OWNTONE_CONF"

  # A supervised copy cannot reach the speaker (see the note at the top) and it holds port 3689, so it
  # has to go before we can start one that can. Unloading rather than killing: KeepAlive would win.
  if loaded "$OWNTONE_LABEL"; then
    say "unloading $OWNTONE_LABEL — a launchd agent cannot reach the speaker on macOS 26"
    launchctl bootout "$GUI/$OWNTONE_LABEL" 2>/dev/null || true
    local waited=0
    while loaded "$OWNTONE_LABEL" && [ "$waited" -lt 20 ]; do sleep 0.5; waited=$((waited + 1)); done
  fi

  if owntone_running && player_up; then
    info "OwnTone already answering at $PLAYER_URL"
    return 0
  fi

  # Stale pidfile from a previous run, or a half-dead daemon holding the port.
  if [ -f "$OWNTONE_PID" ]; then
    local pid; pid="$(cat "$OWNTONE_PID" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && ! player_up; then
      info "stopping an unresponsive owntone (pid $pid)"
      kill "$pid" 2>/dev/null || true
      sleep 2
    fi
    rm -f "$OWNTONE_PID"
  fi

  say "starting OwnTone"
  # It daemonises itself; the point is that its parent is this shell, so it inherits this terminal's
  # local-network grant. stderr carries the startup banner and is in the logfile too.
  "$OWNTONE_BIN" -c "$OWNTONE_CONF" -P "$OWNTONE_PID" 2>/dev/null

  local waited=0
  until player_up; do
    if [ "$waited" -ge 40 ]; then
      warn "OwnTone did not answer within 20s — see $OWNTONE_LOG"
      return 1
    fi
    sleep 0.5
    waited=$((waited + 1))
  done
  ok "OwnTone up"
}

# ---------------------------------------------------------------------------------- tunebox

start_tunebox() {
  if api_up; then info "tunebox already answering"; return 0; fi

  # tunebox itself is happy under launchd: it only listens, and it talks to OwnTone over loopback,
  # neither of which the local-network gate applies to.
  if loaded "$SERVE_LABEL"; then
    say "kickstarting $SERVE_LABEL"
    launchctl kickstart -k "$GUI/$SERVE_LABEL"
  elif [ -f "$SERVE_AGENT" ]; then
    say "loading $SERVE_LABEL"
    launchctl bootstrap "$GUI" "$SERVE_AGENT"
  else
    die "tunebox is not installed as a service — run ./deploy/install.sh first"
  fi

  local waited=0
  until api_up; do
    if [ "$waited" -ge 40 ]; then
      warn "tunebox did not answer within 20s — see $HOME/.local/share/tunebox/log/tunebox.log"
      return 1
    fi
    sleep 0.5
    waited=$((waited + 1))
  done
  ok "tunebox up"
}

# ------------------------------------------------------------------- does the speaker answer?

# The check that matters, and the reason this script exists: "the daemon started" and "music can play"
# are different facts. OwnTone only opens the Cast session when it is asked to play, so this asks it
# to select the speaker and then reads back whether the selection stuck.
verify_speaker() {
  local before after
  before="$(wc -l <"$OWNTONE_LOG" 2>/dev/null || echo 0)"

  # Selecting is idempotent and silent, and it is what forces the connection attempt.
  ( cd "$(dirname "${BASH_SOURCE[0]}")/.." && .venv/bin/tunebox player select >/dev/null 2>&1 ) || true
  sleep 3

  after="$(api_call /healthz 2>/dev/null | python3 -c '
import json, sys
try:
    p = json.load(sys.stdin).get("player") or {}
except Exception:
    print("unknown"); raise SystemExit
print("yes" if p.get("output_selected") else "no")
' 2>/dev/null || echo unknown)"

  if [ "$after" = "yes" ]; then
    ok "speaker reachable — the Cast session came up"
    return 0
  fi

  warn "the speaker is visible but the Cast session did not come up"
  if tail -n "+$((before + 1))" "$OWNTONE_LOG" 2>/dev/null | grep -q "No route to host"; then
    cat >&2 <<EOF

    ${B}This is the macOS local-network gate, not a broken speaker.${N} OwnTone can see the device over
    mDNS and cannot open a TCP connection to it. Either it is running as a launchd agent (which is
    never granted), or this terminal has not been granted local network access.

      System Settings -> Privacy & Security -> Local Network
        enable the terminal you are running this from, and 'owntone' if it is listed

    Then: ./deploy/start.sh restart
EOF
  else
    info "recent log lines:"
    tail -n "+$((before + 1))" "$OWNTONE_LOG" 2>/dev/null | grep -iE "cast:|misc: Could not" | tail -5 | sed 's/^/      /'
  fi
  return 1
}

# -------------------------------------------------------------------------------------- stop

stop_all() {
  if loaded "$SERVE_LABEL"; then
    say "stopping $SERVE_LABEL"
    launchctl bootout "$GUI/$SERVE_LABEL" 2>/dev/null || true
  else
    info "$SERVE_LABEL is not loaded"
  fi

  if loaded "$OWNTONE_LABEL"; then
    say "unloading $OWNTONE_LABEL"
    launchctl bootout "$GUI/$OWNTONE_LABEL" 2>/dev/null || true
  fi

  if owntone_running; then
    say "stopping OwnTone"
    pkill -f "$OWNTONE_BIN" 2>/dev/null || true
    local waited=0
    while owntone_running && [ "$waited" -lt 20 ]; do sleep 0.5; waited=$((waited + 1)); done
  fi
  rm -f "$OWNTONE_PID"
  ok "stopped"
}

# ------------------------------------------------------------------------------------ status

status() {
  local player="down" api="down" host port
  player_up && player="up"
  api_up && api="up"
  host="$(envget TUNEBOX_API_HOST 2>/dev/null || true)"
  port="$(envget TUNEBOX_API_PORT 2>/dev/null || true)"

  printf '\n%s  OwnTone%s   %-4s %s\n' "$B" "$N" "$player" "${DIM}$PLAYER_URL$N"
  if loaded "$OWNTONE_LABEL"; then
    printf '              %s\n' "${YEL}under launchd — cannot reach the speaker unless Local Network is granted${N}"
  fi
  printf '%s  tunebox%s   %-4s %s\n\n' "$B" "$N" "$api" "${DIM}http://${host:-127.0.0.1}:${port:-8080}/$N"

  if [ "$api" = "up" ]; then
    api_call /healthz | python3 -c '
import json, sys
try:
    p = json.load(sys.stdin).get("player") or {}
except Exception:
    raise SystemExit
if not p.get("reachable"):
    print("  player: unreachable —", str(p.get("error") or "")[:88])
else:
    print("  speaker: {} ({})".format(
        p.get("output") or "none",
        "selected" if p.get("output_selected") else "NOT selected — no audio will come out"))
    # /healthz reports now_playing already formatted as "artist — title", not as an object.
    now = p.get("now_playing")
    print("  state:   {}{}".format(p.get("state"), "  " + now if now else ""))
' || true
    printf '\n'
  fi
}

# -------------------------------------------------------------------------------------- main

case "${1:-start}" in
  start)
    start_owntone
    start_tunebox
    verify_speaker || true
    status
    ;;
  stop)    stop_all ;;
  restart) stop_all; sleep 2; start_owntone; start_tunebox; verify_speaker || true; status ;;
  status)  status ;;
  -h|--help) usage ;;
  *) usage >&2; die "unknown option: $1 — expected start, stop, restart or status" ;;
esac
