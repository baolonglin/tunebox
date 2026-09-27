#!/usr/bin/env bash
#
# tunebox installer — one command, no configuration, macOS or Raspberry Pi OS / Debian.
#
#   ./deploy/install.sh              install (or upgrade) and start
#   ./deploy/install.sh --dry-run    print every step without doing any of it
#   ./deploy/install.sh --uninstall  stop and remove the service; keep the music and the config
#
# What "no configuration" means here, precisely: the three things that normally have to be decided
# are decided by looking. The token is generated, the listen address is read off the default route,
# and the JS runtime is whichever of deno/node/quickjs/bun the host turns out to have (see
# tunebox.config.DEFAULT_JS_RUNTIMES). Everything else already has a working default. The speaker's
# name is the one thing that cannot be guessed from nothing, so `deploy/owntone-setup.sh` discovers
# it from the player daemon instead of asking.
#
# Two rules this script follows, because an installer that is run twice is the normal case:
#
#   * **Idempotent.** Re-running upgrades the code and leaves the config alone. In particular it
#     never regenerates the token — that would silently log out every phone in the house — and never
#     overwrites a value someone has edited by hand. Missing keys are added; present keys are kept.
#   * **It says what it is about to do to the system before doing it.** Package installs and the
#     system-level systemd unit are the only steps that need root, they are listed up front, and
#     --no-deps / --no-service turn each of them off.
#
set -euo pipefail

VERSION="1"

# ---------------------------------------------------------------------------- defaults

# XDG-ish, and deliberately all under $HOME: PLAN.md 3.2 wants this running as an unprivileged user
# with write access to the music directory and nothing else.
TUNEBOX_HOME_DEFAULT="${XDG_DATA_HOME:-$HOME/.local/share}/tunebox"
CONFIG_DIR_DEFAULT="${XDG_CONFIG_HOME:-$HOME/.config}/tunebox"
MUSIC_ROOT_DEFAULT="$HOME/Music/tunebox"
PORT_DEFAULT="8080"

TUNEBOX_HOME="$TUNEBOX_HOME_DEFAULT"
CONFIG_DIR="$CONFIG_DIR_DEFAULT"
MUSIC_ROOT=""
API_HOST=""
API_PORT="$PORT_DEFAULT"
DO_DEPS=1
DO_SERVICE=1
DRY_RUN=0
UNINSTALL=0
PURGE=0

SERVICE_NAME="tunebox"
LAUNCHD_LABEL="io.tunebox.serve"

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

# ------------------------------------------------------------------------------- output

# Colour only when stdout is a terminal: these lines end up in a pipe or a log often enough.
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

# Every state-changing action goes through this, which is what makes --dry-run trustworthy rather
# than a separate code path that drifts.
run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '    %s[dry-run]%s %s\n' "$DIM" "$N" "$*"
    return 0
  fi
  "$@"
}

usage() {
  cat <<EOF
tunebox installer (v$VERSION)

Usage: deploy/install.sh [options]

  --music-root DIR   where the library lives (default: $MUSIC_ROOT_DEFAULT)
                     On a Pi, point this at the USB SSD, not the SD card.
  --host ADDR        address to listen on (default: this host's LAN address)
  --port N           port to listen on (default: $PORT_DEFAULT)
  --prefix DIR       venv and wrapper location (default: $TUNEBOX_HOME_DEFAULT)
  --config-dir DIR   env file location (default: $CONFIG_DIR_DEFAULT)
  --no-deps          do not install ffmpeg or a JS runtime (assume they are there)
  --no-service       install, but do not register or start a background service
  --dry-run          print what would happen and change nothing
  --uninstall        stop and remove the service and the venv
  --purge            with --uninstall, also delete the config (NOT the music)
  -h, --help         this

Needs root only for: installing packages, and the systemd unit on Linux. Both are skippable.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --music-root) MUSIC_ROOT="${2:?--music-root needs a directory}"; shift 2 ;;
    --host)       API_HOST="${2:?--host needs an address}"; shift 2 ;;
    --port)       API_PORT="${2:?--port needs a number}"; shift 2 ;;
    --prefix)     TUNEBOX_HOME="${2:?--prefix needs a directory}"; shift 2 ;;
    --config-dir) CONFIG_DIR="${2:?--config-dir needs a directory}"; shift 2 ;;
    --no-deps)    DO_DEPS=0; shift ;;
    --no-service) DO_SERVICE=0; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --uninstall)  UNINSTALL=1; shift ;;
    --purge)      PURGE=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *)            usage >&2; die "unknown option: $1" ;;
  esac
done

ENV_FILE="$CONFIG_DIR/tunebox.env"
VENV="$TUNEBOX_HOME/venv"
WRAPPER="$TUNEBOX_HOME/bin/tunebox-serve"

# --------------------------------------------------------------------------- platform

detect_platform() {
  case "$(uname -s)" in
    Darwin) PLATFORM="macos"; SERVICE_KIND="launchd" ;;
    Linux)
      PLATFORM="linux"
      command -v systemctl >/dev/null 2>&1 || die \
        "this Linux has no systemctl. Use --no-service and run '$WRAPPER' from whatever supervisor you do have"
      SERVICE_KIND="systemd"
      ;;
    *) die "unsupported OS: $(uname -s). tunebox is developed on macOS and deployed on Raspberry Pi OS" ;;
  esac
  ARCH="$(uname -m)"
}

# ------------------------------------------------------------------- system packages

install_deps_macos() {
  command -v brew >/dev/null 2>&1 || die \
    "Homebrew is not installed, so packages cannot be installed automatically.
    Install ffmpeg and deno yourself, then re-run with --no-deps:  https://brew.sh"
  local wanted=()
  need_cmd ffmpeg || wanted+=(ffmpeg)
  # ffprobe ships with ffmpeg, so it is checked but never asked for separately.
  have_js_runtime || wanted+=(deno)
  if [ ${#wanted[@]} -eq 0 ]; then
    ok "system packages already present"
    return
  fi
  say "installing with Homebrew: ${wanted[*]}"
  run brew install "${wanted[@]}"
}

install_deps_linux() {
  command -v apt-get >/dev/null 2>&1 || die \
    "no apt-get. Install ffmpeg and a JS runtime (deno, node 22+, quickjs or bun) with your own
    package manager, then re-run with --no-deps"

  local wanted=()
  need_cmd ffmpeg || wanted+=(ffmpeg)
  need_cmd python3 || wanted+=(python3)
  python3 -c 'import venv' >/dev/null 2>&1 || wanted+=(python3-venv)

  if ! have_js_runtime; then
    # Debian ships quickjs, which yt-dlp supports and which exists for **every** Raspberry Pi,
    # including 32-bit armv7 where there is no deno build at all and Debian's nodejs is far too old
    # (yt-dlp needs node 22+). It is the boring answer that always works; a faster runtime can be
    # installed by hand afterwards and tunebox will prefer it automatically.
    wanted+=(quickjs)
  fi

  if [ ${#wanted[@]} -eq 0 ]; then
    ok "system packages already present"
    return
  fi
  say "installing with apt-get: ${wanted[*]}"
  info "this needs root"
  run sudo apt-get update
  run sudo apt-get install -y "${wanted[@]}"
}

need_cmd() { command -v "$1" >/dev/null 2>&1; }

have_js_runtime() {
  # Presence only — whether the *version* is good enough is yt-dlp's opinion, and `tunebox doctor`
  # asks it directly at the end of this script rather than duplicating the thresholds here.
  need_cmd deno || need_cmd node || need_cmd qjs || need_cmd bun
}

# -------------------------------------------------------------------------- the venv

find_python() {
  local candidate
  for candidate in python3.13 python3.12 python3.11 python3; do
    if need_cmd "$candidate" && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      printf '%s' "$(command -v "$candidate")"
      return 0
    fi
  done
  die "no Python 3.11+ found. Raspberry Pi OS (bookworm) ships 3.11; on older releases, upgrade the OS"
}

install_tunebox() {
  say "installing tunebox into $VENV"
  info "using $PYTHON ($("$PYTHON" -V 2>&1))"
  run mkdir -p "$TUNEBOX_HOME/bin"
  if [ ! -x "$VENV/bin/python" ]; then
    run "$PYTHON" -m venv "$VENV"
  else
    info "re-using the existing venv"
  fi  # --upgrade so re-running is an upgrade. Not editable: the checkout should be free to move or be
  # deleted once this has run, which is what makes "git clone, install, done" true.
  run "$VENV/bin/python" -m pip install --quiet --upgrade pip
  run "$VENV/bin/python" -m pip install --quiet --upgrade "$REPO_ROOT"
}

# ------------------------------------------------------------------------- the config

lan_address() {
  local addr=""
  case "$PLATFORM" in
    macos)
      # The interface behind the default route, rather than guessing en0 — a Mac mini on Ethernet
      # and a laptop on Wi-Fi disagree about which that is.
      local iface
      iface="$(route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}')" || true
      if [ -n "$iface" ]; then
        addr="$(ipconfig getifaddr "$iface" 2>/dev/null)" || addr=""
      fi
      ;;
    linux)
      # `ip route get` to a routable address reports the source address the kernel would use, which
      # is exactly the one a phone on the same LAN can reach. No packet is sent.
      addr="$(ip -4 route get 192.0.2.1 2>/dev/null | awk '{for (i=1;i<NF;i++) if ($i=="src") {print $(i+1); exit}}')" || addr=""
      if [ -z "$addr" ]; then
        addr="$(hostname -I 2>/dev/null | awk '{print $1}')" || addr=""
      fi
      ;;
  esac
  printf '%s' "$addr"
}

generate_token() {
  # The interpreter found at startup, not the venv's: this also has to work during --dry-run, and
  # ``secrets`` is in the standard library either way.
  "$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(32))'
}

# Read one key out of the env file, ignoring comments. Used to keep an existing value.
env_value() {
  [ -f "$ENV_FILE" ] || return 1
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -1
}

# Same, but with a fallback — because a key that is *absent* from a file that exists reads as an
# empty string, not as a failure, and an empty listen address would be the wildcard.
env_value_or() {
  local found=""
  found="$(env_value "$1")" || found=""
  if [ -n "$found" ]; then printf '%s' "$found"; else printf '%s' "$2"; fi
}

write_config() {
  say "configuration"
  run mkdir -p "$CONFIG_DIR"
  run chmod 700 "$CONFIG_DIR"

  local host="$API_HOST" token music
  if [ -z "$host" ]; then
    host="$(lan_address)"
    if [ -z "$host" ]; then
      # Loopback still gives a working install; it just cannot be reached from a phone. Better than
      # refusing, and far better than binding the wildcard, which PLAN.md 3.2 forbids outright.
      host="127.0.0.1"
      warn "could not determine this host's LAN address; listening on 127.0.0.1 only."
      warn "re-run with --host <address> to make the remote reachable from a phone."
    else
      info "LAN address: $host"
    fi
  fi

  music="${MUSIC_ROOT:-$(env_value_or TUNEBOX_MUSIC_ROOT "$MUSIC_ROOT_DEFAULT")}"

  if [ -f "$ENV_FILE" ]; then
    # The token is the one value that must survive an upgrade: regenerating it would log out every
    # phone that has the app installed, silently, and the only symptom would be a token box.
    token="$(env_value TUNEBOX_TOKEN)" || token=""
    if [ -z "$token" ]; then
      warn "$ENV_FILE has no TUNEBOX_TOKEN; generating one"
      token="$(generate_token)"
      set_env TUNEBOX_TOKEN "$token"
    else
      info "keeping the existing token and settings in $ENV_FILE"
    fi
    # Only re-asserted when asked for explicitly, so a hand-edited file survives a re-run.
    if [ -n "$API_HOST" ]; then set_env TUNEBOX_API_HOST "$API_HOST"; fi
    if [ "$API_PORT" != "$PORT_DEFAULT" ]; then set_env TUNEBOX_API_PORT "$API_PORT"; fi
    if [ -n "$MUSIC_ROOT" ]; then set_env TUNEBOX_MUSIC_ROOT "$MUSIC_ROOT"; fi
    CONFIGURED_HOST="$(env_value_or TUNEBOX_API_HOST "$host")"
    CONFIGURED_PORT="$(env_value_or TUNEBOX_API_PORT "$API_PORT")"
    CONFIGURED_MUSIC="$(env_value_or TUNEBOX_MUSIC_ROOT "$music")"
    return
  fi

  if [ "$DRY_RUN" = 1 ]; then
    info "would write $ENV_FILE (mode 600) with a freshly generated token"
    CONFIGURED_HOST="$host"
    CONFIGURED_PORT="$API_PORT"
    CONFIGURED_MUSIC="$music"
    return
  fi

  token="$(generate_token)"
  info "writing $ENV_FILE (mode 600)"
  # umask first, so the token is never briefly world-readable between create and chmod.
  ( umask 077
    cat >"$ENV_FILE" <<EOF
# tunebox configuration — written by deploy/install.sh, safe to edit.
# Every value here has a documented default in README.md; this file only sets what a deployment
# genuinely has to decide. Restart the service after changing it.

# Shared secret, checked on every API route (PLAN.md 3.2). Generated once and never rewritten by
# the installer: changing it logs out every phone that has the remote installed.
TUNEBOX_TOKEN=$token

# The LAN address, never 0.0.0.0 — this service runs yt-dlp and ffmpeg on request and must not be
# reachable from outside the house.
TUNEBOX_API_HOST=$host
TUNEBOX_API_PORT=$API_PORT

# The library. On a Raspberry Pi this belongs on a USB SSD, not the SD card: it is the only
# irreplaceable state in the system.
TUNEBOX_MUSIC_ROOT=$music

# The speaker to keep selected. deploy/owntone-setup.sh fills in TUNEBOX_OUTPUT_ID once the player
# daemon can see it, which is more reliable than a name somebody may rename in the Google Home app.
#TUNEBOX_OUTPUT=Living Room speaker
#TUNEBOX_OUTPUT_ID=

# OwnTone's admin_password, if this host is not in its trusted_networks. Not needed on loopback.
#TUNEBOX_PLAYER_PASSWORD=
EOF
  )
  chmod 600 "$ENV_FILE"
  CONFIGURED_HOST="$host"
  CONFIGURED_PORT="$API_PORT"
  CONFIGURED_MUSIC="$music"
}

# Add or replace one key, preserving everything else and the file's mode.
set_env() {
  local key="$1" value="$2"
  if [ "$DRY_RUN" = 1 ]; then
    printf '    %s[dry-run]%s set %s in %s\n' "$DIM" "$N" "$key" "$ENV_FILE"
    return
  fi
  local tmp; tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
  chmod 600 "$tmp"
  if [ -f "$ENV_FILE" ]; then
    grep -v "^${key}=" "$ENV_FILE" >"$tmp" || true
  fi
  printf '%s=%s\n' "$key" "$value" >>"$tmp"
  mv "$tmp" "$ENV_FILE"
}

write_wrapper() {
  # Both service managers exec this rather than the venv's entry point directly. launchd cannot read
  # an environment file at all, and systemd's EnvironmentFile= would leave two places that know how
  # config is loaded. One wrapper means the env file is the single source of truth on both systems,
  # and it can be run by hand to reproduce exactly what the service does.
  say "writing $WRAPPER"
  if [ "$DRY_RUN" = 1 ]; then
    printf '    %s[dry-run]%s write %s\n' "$DIM" "$N" "$WRAPPER"
    return
  fi
  mkdir -p "$(dirname "$WRAPPER")"
  cat >"$WRAPPER" <<EOF
#!/usr/bin/env bash
# Written by tunebox's deploy/install.sh. Runs the service exactly as the supervisor does.
set -euo pipefail
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi
exec "$VENV/bin/tunebox" serve "\$@"
EOF
  chmod 755 "$WRAPPER"
}

# ------------------------------------------------------------------------- the service

LAUNCH_AGENT="$HOME/Library/LaunchAgents/$LAUNCHD_LABEL.plist"
SYSTEMD_UNIT="/etc/systemd/system/$SERVICE_NAME.service"

install_service_launchd() {
  say "registering the launchd agent $LAUNCHD_LABEL"
  info "$LAUNCH_AGENT"
  if [ "$DRY_RUN" = 0 ]; then
    mkdir -p "$(dirname "$LAUNCH_AGENT")" "$TUNEBOX_HOME/log"
    cat >"$LAUNCH_AGENT" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LAUNCHD_LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$WRAPPER</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <!-- Homebrew's bin dirs, because launchd starts with a minimal PATH and ffmpeg lives in one of
       them. Without this the service starts and every download fails. -->
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StandardOutPath</key><string>$TUNEBOX_HOME/log/tunebox.log</string>
  <key>StandardErrorPath</key><string>$TUNEBOX_HOME/log/tunebox.log</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
EOF
  fi
  # Reloading, not restarting: the plist may have changed (a new prefix, a new PATH), and
  # `launchctl kickstart -k` would re-exec the *old* loaded definition.
  #
  # The wait is not defensive padding. `bootout` returns 0 immediately and unloads asynchronously, so
  # an unconditional bootout-then-bootstrap pair races itself: measured on this host, bootstrap then
  # fails with "Bootstrap failed: 5: Input/output error" and the agent is left **not loaded at all**
  # — the service stops and does not come back. That is the worst possible outcome for an upgrade,
  # and it is silent, because bootout succeeded.
  if launchctl print "gui/$(id -u)/$LAUNCHD_LABEL" >/dev/null 2>&1; then
    run launchctl bootout "gui/$(id -u)/$LAUNCHD_LABEL" || true
    if [ "$DRY_RUN" = 0 ]; then
      local waited=0
      while launchctl print "gui/$(id -u)/$LAUNCHD_LABEL" >/dev/null 2>&1; do
        if [ "$waited" -ge 20 ]; then
          die "the old launchd agent did not unload; 'launchctl bootout gui/$(id -u)/$LAUNCHD_LABEL' by hand, then re-run"
        fi
        sleep 0.5
        waited=$((waited + 1))
      done
    fi
  fi
  run launchctl bootstrap "gui/$(id -u)" "$LAUNCH_AGENT"
  if [ "$DRY_RUN" = 0 ]; then ok "agent loaded; it will start again at login"; fi
}

install_service_systemd() {
  say "installing the systemd unit $SERVICE_NAME.service"
  info "$SYSTEMD_UNIT — this needs root"
  local unit; unit="$(mktemp)"
  cat >"$unit" <<EOF
[Unit]
Description=tunebox — music ingest and playback remote
# The player daemon is optional and may not be installed: Wants, never Requires, so tunebox still
# starts and still ingests on a host with no OwnTone (PLAN.md 3.1).
Wants=network-online.target owntone.service
After=network-online.target owntone.service

[Service]
Type=simple
User=$(id -un)
Group=$(id -gn)
ExecStart=$WRAPPER
Restart=on-failure
RestartSec=5
# The queue survives a restart, and a SIGTERM lets the worker finish what it is doing first.
KillSignal=SIGTERM
TimeoutStopSec=30

# PLAN.md 3.2: "Run the worker as an unprivileged user with write access to the music directory and
# nothing else." These turn that from an instruction into a property.
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=read-only
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
LockPersonality=yes
# ReadWritePaths is the whole of the write surface: the library (which holds the queue database
# too) and nothing else. ProtectHome=read-only above makes the rest of \$HOME unwritable.
ReadWritePaths=$CONFIGURED_MUSIC

[Install]
WantedBy=multi-user.target
EOF
  if [ "$DRY_RUN" = 1 ]; then
    printf '    %s[dry-run]%s install unit:\n' "$DIM" "$N"
    sed 's/^/        /' "$unit"
    rm -f "$unit"
    return
  fi
  sudo install -m 0644 "$unit" "$SYSTEMD_UNIT"
  rm -f "$unit"
  sudo systemctl daemon-reload
  sudo systemctl enable "$SERVICE_NAME"
  sudo systemctl restart "$SERVICE_NAME"
  # A system unit, not `systemd --user`, on purpose: a Pi is headless and reboots unattended, and a
  # user service would need loginctl lingering to come back without somebody logging in.
  ok "unit enabled; it will start at boot"
}

remove_service() {
  case "$SERVICE_KIND" in
    launchd)
      say "removing the launchd agent"
      run launchctl bootout "gui/$(id -u)/$LAUNCHD_LABEL" 2>/dev/null || true
      run rm -f "$LAUNCH_AGENT"
      ;;
    systemd)
      say "removing the systemd unit (needs root)"
      run sudo systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
      run sudo rm -f "$SYSTEMD_UNIT"
      run sudo systemctl daemon-reload
      ;;
  esac
}

# ---------------------------------------------------------------------------- verify

wait_for_health() {
  local url="http://$CONFIGURED_HOST:$CONFIGURED_PORT/healthz" token
  token="$(env_value TUNEBOX_TOKEN || true)"
  [ -n "$token" ] || return 1
  # The loop variable is deliberately unused: this is a bounded retry, not an enumeration.
  for _ in $(seq 1 20); do
    if curl -fsS -m 3 -H "X-Tunebox-Token: $token" "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

# ------------------------------------------------------------------------------- main

detect_platform
# Resolved before anything else so that "no usable Python" is the first thing reported rather than a
# failure three steps in, and so --dry-run has an interpreter to talk about.
PYTHON="$(find_python)"

if [ "$UNINSTALL" = 1 ]; then
  say "uninstalling tunebox ($PLATFORM)"
  remove_service
  run rm -rf "$VENV" "$TUNEBOX_HOME/bin"
  if [ "$PURGE" = 1 ]; then
    warn "purging $CONFIG_DIR — the token goes with it, so every phone will need the new one"
    run rm -rf "$CONFIG_DIR"
  else
    info "keeping $ENV_FILE (use --purge to remove it)"
  fi
  # Never the music. It is the only irreplaceable state in the system (PLAN.md 3.1).
  info "the music library was not touched"
  ok "done"
  exit 0
fi

say "tunebox installer — $PLATFORM/$ARCH"
if [ "$DRY_RUN" = 1 ]; then info "${DIM}dry run: nothing will be changed${N}"; fi
info "source:  $REPO_ROOT"
info "prefix:  $TUNEBOX_HOME"
info "config:  $ENV_FILE"
if [ "$DO_DEPS" = 1 ] || [ "$DO_SERVICE" = 1 ]; then
  info ""
  info "steps that change the system:"
  if [ "$DO_DEPS" = 1 ]; then
    if [ "$PLATFORM" = linux ]; then
      info "  - install missing packages (ffmpeg, a JS runtime) via sudo apt-get"
    else
      info "  - install missing packages (ffmpeg, a JS runtime) via brew"
    fi
  fi
  if [ "$DO_SERVICE" = 1 ]; then
    case "$SERVICE_KIND" in
      launchd) info "  - write $LAUNCH_AGENT and load it" ;;
      systemd) info "  - write $SYSTEMD_UNIT via sudo, enable and start it" ;;
    esac
  fi
  info ""
fi

if [ "$DO_DEPS" = 1 ]; then
  case "$PLATFORM" in
    macos) install_deps_macos ;;
    linux) install_deps_linux ;;
  esac
else
  info "skipping dependency installation (--no-deps)"
fi

install_tunebox
write_config
run mkdir -p "$CONFIGURED_MUSIC"
write_wrapper

say "checking dependencies"
if [ "$DRY_RUN" = 1 ]; then
  info "${DIM}[dry-run] would run tunebox doctor${N}"
else
  # Through the env file, so this is the same configuration the service will run with — a doctor
  # that checked a *different* music root would be worse than no check.
  # shellcheck source=/dev/null  # written by write_config above; its path is not a constant
  if ! ( set -a; . "$ENV_FILE"; set +a; "$VENV/bin/tunebox" doctor ); then
    warn "doctor reported a problem above. tunebox is installed; fix the '!!' rows, then re-run."
  fi
fi

if [ "$DO_SERVICE" = 1 ]; then
  case "$SERVICE_KIND" in
    launchd) install_service_launchd ;;
    systemd) install_service_systemd ;;
  esac

  if [ "$DRY_RUN" = 0 ]; then
    say "waiting for the service to answer"
    if wait_for_health; then
      ok "tunebox is serving on http://$CONFIGURED_HOST:$CONFIGURED_PORT"
    else
      warn "the service did not answer /healthz within 20s. Logs:"
      case "$SERVICE_KIND" in
        launchd) warn "  tail -n 50 $TUNEBOX_HOME/log/tunebox.log" ;;
        systemd) warn "  sudo journalctl -u $SERVICE_NAME -n 50 --no-pager" ;;
      esac
    fi
  fi
else
  info "skipping the service (--no-service). Run it yourself with: $WRAPPER"
fi

# ------------------------------------------------------------------------- the summary

if [ "$DRY_RUN" = 1 ]; then
  say "dry run complete — nothing was changed"
  exit 0
fi

TOKEN_VALUE="$(env_value TUNEBOX_TOKEN)" || TOKEN_VALUE=""
LOG_HINT="$TUNEBOX_HOME/log/tunebox.log"
if [ "$SERVICE_KIND" = systemd ]; then LOG_HINT="sudo journalctl -u $SERVICE_NAME -f"; fi

printf '\n%s tunebox is installed.%s\n\n' "$B" "$N"
if [ -t 1 ]; then
  # Printed only to a terminal. Piping the installer into a log file is a normal thing to do, and
  # the token is the one string in this project that must not end up in one — the same reason it
  # travels in a URL fragment rather than a query string.
  printf '  Open this on the phone, then use "Add to Home Screen":\n\n'
  printf '      %shttp://%s:%s/#token=%s%s\n\n' "$B" "$CONFIGURED_HOST" "$CONFIGURED_PORT" "$TOKEN_VALUE" "$N"
  printf '  The fragment is never sent to the server, so it never reaches the access log. The phone\n'
  printf '  stores it after the first load.\n\n'
else
  printf '  Open http://%s:%s/ on the phone and paste the token from:\n\n' "$CONFIGURED_HOST" "$CONFIGURED_PORT"
  printf '      %s\n\n' "$ENV_FILE"
  printf '  (the token is not printed here because this output is not a terminal)\n\n'
fi
cat <<EOF
  library:  $CONFIGURED_MUSIC
  config:   $ENV_FILE
  logs:     $LOG_HINT

  Playback needs OwnTone. If it is installed, point it at the library and lock it down with:

      deploy/owntone-setup.sh

  Ingest works now, with or without it.
EOF
