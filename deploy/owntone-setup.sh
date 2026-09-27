#!/usr/bin/env bash
#
# Point an existing OwnTone at tunebox's library, close the hole in its default configuration, and
# discover the speaker so nobody has to type its name.
#
#   ./deploy/owntone-setup.sh              configure, restart, discover
#   ./deploy/owntone-setup.sh --dry-run    show the diff it would apply
#   ./deploy/owntone-setup.sh --discover   only find the speaker, change no config
#
# This does **not** install OwnTone. Installing it is the one step that genuinely differs per
# platform and per architecture — a source build on macOS (see README), a package on Debian — and a
# script that guessed wrong would leave a half-built daemon behind. It checks, and tells you.
#
# Three changes are made to owntone.conf, and the second is the one that matters:
#
#   library.directories   -> tunebox's music root, or the daemon never sees an ingested track.
#   general.trusted_networks -> { "localhost" }. OwnTone ships { "lan" }, which *skips its own
#                            password for every device on the home network* — measured, PLAN.md 3.2:
#                            `curl -X PUT http://host:3689/api/player/stop` from the IoT device
#                            nobody is proud of returns 204. tunebox keeps working because it talks
#                            to loopback.
#   general.admin_password -> generated, because trusted_networks alone would lock tunebox's own
#                            LAN-side web UI link out with no way back in.
#
set -euo pipefail

DRY_RUN=0
DISCOVER_ONLY=0
CONF=""
PLAYER_URL=""

CONFIG_DIR_DEFAULT="${XDG_CONFIG_HOME:-$HOME/.config}/tunebox"
ENV_FILE="${TUNEBOX_ENV_FILE:-$CONFIG_DIR_DEFAULT/tunebox.env}"

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
Configure an existing OwnTone for tunebox.

Usage: deploy/owntone-setup.sh [options]

  --conf FILE      owntone.conf (default: found among the usual locations)
  --env-file FILE  tunebox env file to update (default: $ENV_FILE)
  --url URL        player API base (default: TUNEBOX_PLAYER_URL or http://127.0.0.1:3689)
  --discover       only look for the speaker; do not touch owntone.conf
  --dry-run        print the changes without applying them
  -h, --help       this
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --conf)     CONF="${2:?--conf needs a file}"; shift 2 ;;
    --env-file) ENV_FILE="${2:?--env-file needs a file}"; shift 2 ;;
    --url)      PLAYER_URL="${2:?--url needs a URL}"; shift 2 ;;
    --discover) DISCOVER_ONLY=1; shift ;;
    --dry-run)  DRY_RUN=1; shift ;;
    -h|--help)  usage; exit 0 ;;
    *)          usage >&2; die "unknown option: $1" ;;
  esac
done

[ -f "$ENV_FILE" ] || die "no tunebox config at $ENV_FILE — run deploy/install.sh first"

env_value() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -1; }

set_env() {
  local key="$1" value="$2"
  if [ "$DRY_RUN" = 1 ]; then
    info "${DIM}[dry-run]${N} would set $key=$value in $ENV_FILE"
    return
  fi
  local tmp; tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
  chmod 600 "$tmp"
  # Drop both the live key and the commented placeholder the installer wrote, so re-running does not
  # leave two contradictory-looking lines in a file people read.
  grep -v -e "^${key}=" -e "^#${key}=" "$ENV_FILE" >"$tmp" || true
  printf '%s=%s\n' "$key" "$value" >>"$tmp"
  mv "$tmp" "$ENV_FILE"
  info "set $key in $ENV_FILE"
}

MUSIC_ROOT="$(env_value TUNEBOX_MUSIC_ROOT)"
[ -n "$MUSIC_ROOT" ] || die "$ENV_FILE has no TUNEBOX_MUSIC_ROOT"
if [ -z "$PLAYER_URL" ]; then
  PLAYER_URL="$(env_value TUNEBOX_PLAYER_URL)" || PLAYER_URL=""
fi
[ -n "$PLAYER_URL" ] || PLAYER_URL="http://127.0.0.1:3689"
PLAYER_URL="${PLAYER_URL%/}"

command -v owntone >/dev/null 2>&1 || command -v owntone-server >/dev/null 2>&1 || \
  warn "no 'owntone' binary on PATH. If the daemon runs anyway (a service, a container), carry on."

# --------------------------------------------------------------------------- the conf

find_conf() {
  local candidate
  for candidate in \
    /usr/local/etc/owntone.conf \
    /opt/homebrew/etc/owntone.conf \
    /etc/owntone.conf \
    /usr/local/etc/forked-daapd.conf \
    /etc/forked-daapd.conf
  do
    if [ -f "$candidate" ]; then printf '%s' "$candidate"; return 0; fi
  done
  return 1
}

# Set `key = value` inside a top-level `section { ... }` block, whether or not the key is there
# already, and whether or not it is commented out. Creates the section if it is missing.
#
# In awk rather than a sed one-liner for two reasons that are both real in this file: the same key
# name appears in more than one section of owntone.conf (`name` is in `library` *and* in `audio`), so
# a global substitution edits the wrong block; and a value may legitimately span lines
# (`directories = {` ... `}`), so brace depth has to be tracked rather than assumed.
apply_conf() {
  local file="$1" section="$2" key="$3" value="$4" tmp
  tmp="$(mktemp)"
  awk -v section="$section" -v key="$key" -v value="$value" '
    BEGIN { depth = 0; in_section = 0; done = 0; skipping = 0; indent = "\t" }

    # Leading whitespace of a line, ignoring a comment marker: a disabled setting written as
    # "#\tadmin_password = ..." must come back as "\tadmin_password = ...", not flush left.
    # Written with index()/substr() rather than a backreference, because POSIX awk has no \1.
    function indent_of(text) {
      hash = index(text, "#")
      if (hash > 0 && substr(text, 1, hash - 1) ~ /^[[:space:]]*$/) {
        before = substr(text, 1, hash - 1)
        return (before == "") ? indent : before
      }
      match(text, /^[[:space:]]*/)
      return substr(text, 1, RLENGTH)
    }

    {
      line = $0
      # gsub returns the number of matches and the replacement is a no-op; counted on a copy,
      # because awk mutates the subject.
      copy = line; opens = gsub(/\{/, "{", copy)
      copy = line; closes = gsub(/\}/, "}", copy)
      next_depth = depth + opens - closes

      # Dropping the tail of a multi-line value we have just replaced. Must come first: those lines
      # contain the brace that would otherwise look like the end of the section.
      if (skipping) {
        depth = next_depth
        if (depth <= 1) { skipping = 0 }
        next
      }

      if (depth == 0 && !in_section && line ~ "^[[:space:]]*" section "[[:space:]]*\\{") {
        in_section = 1
        print
        depth = next_depth
        next
      }

      # Only at the top level of the block: a brace-delimited *value* sits deeper and must not be
      # mistaken for a setting.
      if (in_section && depth == 1 && line ~ "^[[:space:]]*#?[[:space:]]*" key "[[:space:]]*=") {
        indent = indent_of(line)
        printf "%s%s = %s\n", indent, key, value
        done = 1
        depth = next_depth
        # `directories = {` on its own line: the rest of the old value goes with it, or the file is
        # left with orphaned entries and an unbalanced brace.
        if (depth > 1) { skipping = 1 }
        next
      }

      # Match the file own indentation when inserting a key it does not have yet.
      if (in_section && depth == 1 && line ~ /^[[:space:]]*[A-Za-z_]+[[:space:]]*=/) {
        indent = indent_of(line)
      }

      # The block ends where the depth returns to zero — not at the first closing brace, which may
      # be closing a multi-line value.
      if (in_section && next_depth == 0) {
        if (!done) { printf "%s%s = %s\n", indent, key, value; done = 1 }
        in_section = 0
      }

      print
      depth = next_depth
    }

    END { if (!done) { printf "\n%s {\n%s%s = %s\n}\n", section, indent, key, value } }
  ' "$file" >"$tmp"
  # An empty result would mean overwriting owntone.conf with nothing, which is the one outcome worse
  # than not editing it at all.
  if [ ! -s "$tmp" ]; then
    rm -f "$tmp"
    die "failed to rewrite $section.$key — refusing to touch the config"
  fi
  printf '%s' "$tmp"
}

configure_owntone() {
  if [ -z "$CONF" ]; then
    CONF="$(find_conf)" || die \
      "could not find owntone.conf. Pass --conf /path/to/owntone.conf, or install OwnTone first:
    macOS:  build from source with --enable-chromecast (see README.md)
    Debian: the OwnTone project publishes packages; see https://owntone.github.io/owntone-server/"
  fi
  say "configuring $CONF"

  local password
  password="$(env_value TUNEBOX_PLAYER_PASSWORD)" || password=""
  if [ -z "$password" ]; then
    password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
    info "generated an admin_password"
  else
    info "re-using the admin_password already in $ENV_FILE"
  fi

  local work="$CONF"
  local next
  for pair in \
    "library:directories:{ \"$MUSIC_ROOT\" }" \
    "general:trusted_networks:{ \"localhost\" }" \
    "general:admin_password:\"$password\""
  do
    local section="${pair%%:*}" rest="${pair#*:}"
    local key="${rest%%:*}" value="${rest#*:}"
    next="$(apply_conf "$work" "$section" "$key" "$value")"
    if [ "$work" != "$CONF" ]; then rm -f "$work"; fi
    work="$next"
  done

  if [ "$DRY_RUN" = 1 ]; then
    say "changes that would be applied to $CONF"
    diff -u "$CONF" "$work" || true
    rm -f "$work"
    return
  fi

  # A dated backup rather than a single .bak: this file may already have been hand-tuned, and one
  # careless re-run must not be able to eat the only copy.
  local backup
  backup="$CONF.tunebox-$(date +%Y%m%d%H%M%S).bak"
  if [ -w "$CONF" ]; then
    cp -p "$CONF" "$backup"
    cat "$work" >"$CONF"
  else
    info "needs root to write $CONF"
    sudo cp -p "$CONF" "$backup"
    # The redirect reads $work as *us* (we created it) and only the write goes through sudo,
    # which is the intended split.
    # shellcheck disable=SC2024
    sudo tee "$CONF" >/dev/null <"$work"
  fi
  rm -f "$work"
  ok "backed up to $backup"

  # Written only after the daemon has actually been given the password, so the two cannot disagree.
  set_env TUNEBOX_PLAYER_PASSWORD "$password"
  restart_owntone
}

restart_owntone() {
  say "restarting OwnTone"
  if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files owntone.service >/dev/null 2>&1; then
    sudo systemctl restart owntone
  elif command -v brew >/dev/null 2>&1 && brew services list 2>/dev/null | grep -q '^owntone'; then
    brew services restart owntone
  else
    warn "could not work out how OwnTone is supervised — restart it yourself, then re-run with --discover"
    return 1
  fi
}

# ------------------------------------------------------------------------ discovery

api() {
  local path="$1" password
  password="$(env_value TUNEBOX_PLAYER_PASSWORD)" || password=""
  if [ -n "$password" ]; then
    # The Basic-auth username is hard-coded to "admin" in the daemon and is not configurable.
    curl -fsS -m 5 -u "admin:$password" "$PLAYER_URL$path"
  else
    curl -fsS -m 5 "$PLAYER_URL$path"
  fi
}

discover_output() {
  say "looking for the speaker"
  local outputs=""
  # OwnTone needs a few seconds after a restart before mDNS has turned up a Cast device, and a
  # script that asked once would report "no speaker" on a perfectly working system.
  # The loop variable is deliberately unused: a bounded retry, not an enumeration.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if outputs="$(api /api/outputs 2>/dev/null)"; then
      if printf '%s' "$outputs" | grep -q '"id"'; then break; fi
    fi
    sleep 2
  done
  [ -n "$outputs" ] || die "the player is not answering at $PLAYER_URL. Is OwnTone running?"

  # python3 rather than jq: jq is not installed on a fresh Raspberry Pi OS and python3 is, and this
  # script already depends on python3 for the password.
  local chosen
  chosen="$(printf '%s' "$outputs" | python3 -c '
import json, sys

try:
    payload = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(2)
outputs = payload.get("outputs") or []
if not outputs:
    sys.exit(3)

# A Chromecast speaker is the one this design targets (PLAN.md 3.1: the soundbar offers no AirPlay),
# so it wins over, say, the local ALSA device a Pi also advertises.
cast = [o for o in outputs if str(o.get("type", "")).lower() in ("chromecast", "cast")]
pool = cast or outputs
if len(pool) > 1:
    print("MANY", file=sys.stderr)
    for o in pool:
        print("  {} (id {}, {})".format(o.get("name"), o.get("id"), o.get("type")), file=sys.stderr)
    sys.exit(4)
one = pool[0]
print("{}\t{}\t{}".format(one.get("id"), one.get("name"), one.get("type")))
')" || {
    local code=$?
    case $code in
      3) die "OwnTone sees no outputs at all. A Cast speaker only appears while it is powered and on the network" ;;
      4) warn "more than one candidate (listed above). Put the one you want in $ENV_FILE:"
         warn "    TUNEBOX_OUTPUT_ID=<id>"
         exit 1 ;;
      *) die "could not read the output list from $PLAYER_URL" ;;
    esac
  }

  local id name type
  id="$(printf '%s' "$chosen" | cut -f1)"
  name="$(printf '%s' "$chosen" | cut -f2)"
  type="$(printf '%s' "$chosen" | cut -f3)"
  ok "found $name (id $id, $type)"

  # The id, not the name: an id is stable, a name is what somebody renames in the Google Home app.
  # The name goes in too, as a comment's worth of readability for whoever opens this file next.
  set_env TUNEBOX_OUTPUT_ID "$id"
  set_env TUNEBOX_OUTPUT "$name"
}

# ----------------------------------------------------------------------------- main

if [ "$DISCOVER_ONLY" = 0 ]; then
  configure_owntone
fi
discover_output

if [ "$DRY_RUN" = 1 ]; then
  say "dry run complete — nothing was changed"
  exit 0
fi

say "restarting tunebox so it picks up the new settings"
if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files tunebox.service >/dev/null 2>&1; then
  sudo systemctl restart tunebox
elif launchctl print "gui/$(id -u)/io.tunebox.serve" >/dev/null 2>&1; then
  launchctl kickstart -k "gui/$(id -u)/io.tunebox.serve"
else
  warn "tunebox does not seem to be running as a service; restart it yourself"
fi

cat <<EOF

$B OwnTone is configured.$N

  library:   $MUSIC_ROOT
  speaker:   $(env_value TUNEBOX_OUTPUT) (id $(env_value TUNEBOX_OUTPUT_ID))
  api:       $PLAYER_URL — password required from anywhere but this host

  The output keeper in 'tunebox serve' will now select that speaker on startup and re-select it
  after a power-cycle. Check it with:

      tunebox doctor
      tunebox player status
EOF
