#!/usr/bin/env bash
#
# Build and install OwnTone from source on Raspberry Pi OS / Debian, with Chromecast enabled.
#
#   ./deploy/owntone-build.sh              build, install, register the systemd unit
#   ./deploy/owntone-build.sh --dry-run    print every step, change nothing
#   ./deploy/owntone-build.sh --version 29.4
#
# Why this exists at all, when README.md says "Debian: a package":
#
#   measured on this host, Raspberry Pi OS trixie (2026-10)
#     apt-cache policy owntone       -> (no such package)
#     apt-cache policy forked-daapd  -> Candidate: (none)
#     sources: deb.debian.org/debian, debian-security, archive.raspberrypi.com
#
# There is no OwnTone in Debian trixie or in the Raspberry Pi archive, so `apt install owntone` is
# not a route on this hardware and a source build is the only one. That also settles the question
# README.md left open ("whether OwnTone's Debian build enables Chromecast cannot be established
# from here"): there is no Debian build, so we pass --enable-chromecast ourselves and *assert* it
# afterwards, because a daemon built without it fails at the only thing this project needs it for
# and the failure looks like a speaker that is simply never listed.
#
# What it does that `make install` does not, and why each one is load-bearing:
#
#   * **Sets `general.uid`.** The shipped conf says `uid = "owntone"` and a source build creates no
#     such user, so the daemon exits at startup. The Debian package would have made the user; we
#     run as the invoking user instead, which is also what README.md's owntone.conf example shows
#     and what guarantees read access to a music root under that user's home.
#   * **Owns the state directories.** `make install` leaves /usr/local/var/{log,cache,run} as root,
#     but the daemon drops privileges to `uid` and then has to write its log and its song database.
#   * **Installs the generated systemd unit.** `make install` does not, so without this step the
#     daemon has no supervisor: it dies with the terminal that started it and does not come back
#     after a reboot. The unit is OwnTone's own (owntone.service.in, configured with our prefix) —
#     not one written here — and it carries upstream's MemoryMax=256M, whose comment says the value
#     was "derived from observations on rpi3 under load". Installing it as `owntone.service` is also
#     exactly what deploy/owntone-setup.sh's restart_owntone() looks for, so that script then works
#     on Linux with no changes.
#
# It deliberately does **not** start the daemon. The conf still points at OwnTone's default
# /srv/music at this stage; pointing it at the real library, shutting its authorisation down to
# localhost and finding the speaker are deploy/owntone-setup.sh's job, and that script restarts the
# daemon itself once the config is right. Starting it here would index the wrong directory first.
#
set -euo pipefail

VERSION="1"

# ---------------------------------------------------------------------------- defaults

OWNTONE_VERSION_DEFAULT="29.3"
# Recorded from the download this script's first run made, and checked on every run afterwards:
# trust-on-first-use rather than a published checksum, because the project publishes none. It still
# turns "re-downloaded on another host" and "mirror served something else" into an error instead of
# a build. --sha256 replaces it; --no-verify skips it when bumping --version.
OWNTONE_SHA256_DEFAULT="aa0cbfc5651aa65776a8bd2112c9e0a904b2185c837900d3f6494b9ec6e8c547"
PREFIX_DEFAULT="/usr/local"

OWNTONE_VERSION="$OWNTONE_VERSION_DEFAULT"
OWNTONE_SHA256="$OWNTONE_SHA256_DEFAULT"
PREFIX="$PREFIX_DEFAULT"
RUN_UID=""
JOBS=""
BUILD_DIR=""
DO_DEPS=1
DO_SERVICE=1
VERIFY=1
DRY_RUN=0

# Verified present on trixie arm64, and this is the set that makes ./configure pass: the iteration
# that produced it needed exactly one package beyond the obvious list — libxml2-dev, because 29.3
# parses XML with libxml2 where older releases used mxml. libprotobuf-c-dev and libgnutls28-dev are
# the two that --enable-chromecast specifically needs; without them configure succeeds and Cast
# support silently is not built.
BUILD_DEPS=(
  build-essential pkg-config gperf bison flex
  libconfuse-dev libunistring-dev libsqlite3-dev libxml2-dev
  libavcodec-dev libavformat-dev libavfilter-dev libswscale-dev libavutil-dev
  libasound2-dev libpulse-dev libcurl4-gnutls-dev
  libprotobuf-c-dev libgnutls28-dev
  libplist-dev libsodium-dev libgcrypt20-dev
  libjson-c-dev libwebsockets-dev libevent-dev
  libavahi-client-dev zlib1g-dev
)

# ------------------------------------------------------------------------------- output

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

run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '    %s[dry-run]%s %s\n' "$DIM" "$N" "$*"
    return 0
  fi
  "$@"
}

usage() {
  cat <<EOF
OwnTone source build for Debian / Raspberry Pi OS (v$VERSION)

Usage: deploy/owntone-build.sh [options]

  --version VER    OwnTone release to build (default: $OWNTONE_VERSION_DEFAULT)
  --prefix DIR     install prefix (default: $PREFIX_DEFAULT; the conf then lands in
                   DIR/etc/owntone.conf, which is where deploy/owntone-setup.sh looks first)
  --uid USER       user the daemon drops privileges to (default: the invoking user)
  --jobs N         parallel compile jobs (default: nproc)
  --build-dir DIR  where to unpack and build (default: \$HOME/build)
  --sha256 SUM     expected tarball digest; use with --version
  --no-verify      skip the digest check
  --no-deps        do not apt-get the build dependencies
  --no-service     build and install, but register no systemd unit
  --dry-run        print every step, change nothing
  -h, --help       this

Needs sudo for the dependency install, 'make install' and the systemd unit. Afterwards run
deploy/owntone-setup.sh, which points the daemon at the music root, restricts its authorisation
to localhost, generates its admin password and finds the speaker.
EOF
}

# --------------------------------------------------------------------------- arguments

while [ $# -gt 0 ]; do
  case "$1" in
    --version)    OWNTONE_VERSION="${2:?--version needs a release}"; shift 2 ;;
    --prefix)     PREFIX="${2:?--prefix needs a directory}"; shift 2 ;;
    --uid)        RUN_UID="${2:?--uid needs a username}"; shift 2 ;;
    --jobs)       JOBS="${2:?--jobs needs a number}"; shift 2 ;;
    --build-dir)  BUILD_DIR="${2:?--build-dir needs a directory}"; shift 2 ;;
    --sha256)     OWNTONE_SHA256="${2:?--sha256 needs a digest}"; shift 2 ;;
    --no-verify)  VERIFY=0; shift ;;
    --no-deps)    DO_DEPS=0; shift ;;
    --no-service) DO_SERVICE=0; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *)            printf '%serror:%s unknown option: %s\n' "$RED" "$N" "$1" >&2
                  printf 'Try: deploy/owntone-build.sh --help\n' >&2
                  exit 1 ;;
  esac
done

# A --version bump with the pinned digest left in place would fail the check for the wrong reason,
# so say which flag to reach for rather than just reporting a mismatch later.
if [ "$OWNTONE_VERSION" != "$OWNTONE_VERSION_DEFAULT" ] && [ "$OWNTONE_SHA256" = "$OWNTONE_SHA256_DEFAULT" ] && [ "$VERIFY" = 1 ]; then
  die "the pinned digest is for $OWNTONE_VERSION_DEFAULT. Pass --sha256 SUM for $OWNTONE_VERSION, or --no-verify"
fi

: "${RUN_UID:=$(id -un)}"
: "${JOBS:=$(nproc 2>/dev/null || echo 2)}"
: "${BUILD_DIR:=$HOME/build}"

TARBALL="owntone-$OWNTONE_VERSION.tar.xz"
SRC_DIR="$BUILD_DIR/owntone-$OWNTONE_VERSION"
URL="https://github.com/owntone/owntone-server/releases/download/$OWNTONE_VERSION/$TARBALL"
CONF="$PREFIX/etc/owntone.conf"
BIN="$PREFIX/sbin/owntone"
UNIT="/etc/systemd/system/owntone.service"

# ------------------------------------------------------------------------------ preflight

say "preflight"
[ "$(uname -s)" = "Linux" ] || die "this is the Debian/Raspberry Pi route. On macOS see README.md:
    a Homebrew or source build with libinotify-kqueue, and CFLAGS=-Wno-error=unguarded-availability-new on macOS 26"
command -v apt-get >/dev/null 2>&1 || die "no apt-get: install the equivalents of ${BUILD_DEPS[*]} yourself, then re-run with --no-deps"
command -v sudo >/dev/null 2>&1 || die "sudo is required for the dependency install, make install and the unit"
command -v curl >/dev/null 2>&1 || die "curl is required to fetch the release tarball"
id "$RUN_UID" >/dev/null 2>&1 || die "no such user: $RUN_UID (--uid)"
info "version:  $OWNTONE_VERSION"
info "prefix:   $PREFIX"
info "uid:      $RUN_UID"
info "jobs:     $JOBS"
ok "$(uname -m) $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")"

# ------------------------------------------------------------------------- dependencies

if [ "$DO_DEPS" = 1 ]; then
  say "installing build dependencies (${#BUILD_DEPS[@]} packages)"
  info "this needs root"
  run sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
  run sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${BUILD_DEPS[@]}"
  ok "dependencies present"
else
  say "skipping build dependencies (--no-deps)"
fi

# --------------------------------------------------------------------------------- fetch

say "fetching $TARBALL"
run mkdir -p "$BUILD_DIR"
if [ -f "$BUILD_DIR/$TARBALL" ]; then
  info "already downloaded"
else
  run curl -fsSL --retry 3 -o "$BUILD_DIR/$TARBALL" "$URL"
fi
if [ "$VERIFY" = 1 ] && [ "$DRY_RUN" = 0 ]; then
  actual="$(sha256sum "$BUILD_DIR/$TARBALL" | awk '{print $1}')"
  [ "$actual" = "$OWNTONE_SHA256" ] || die "digest mismatch for $TARBALL
    expected $OWNTONE_SHA256
    got      $actual
    Delete $BUILD_DIR/$TARBALL and retry, or pass --sha256 if you are bumping --version."
  info "sha256 matches the pin"
fi
ok "source fetched"

# ----------------------------------------------------------------------------- configure

say "unpacking and configuring"
# Replaced rather than reused: a tree left over from a different --prefix or a failed build carries
# a config.status that quietly wins over the arguments given here.
run rm -rf "$SRC_DIR"
run tar xf "$BUILD_DIR/$TARBALL" -C "$BUILD_DIR"
if [ "$DRY_RUN" = 0 ]; then
  cd "$SRC_DIR"
  # The release tarball ships a configure script, which is why no autotools or ANTLR bootstrap is
  # needed here — a git checkout would need autoreconf and considerably more packages.
  [ -x ./configure ] || die "$SRC_DIR has no configure script; this is not a release tarball"
  if ! ./configure --prefix="$PREFIX" --enable-chromecast >"$BUILD_DIR/configure.log" 2>&1; then
    tail -20 "$BUILD_DIR/configure.log" >&2
    die "configure failed; full log in $BUILD_DIR/configure.log"
  fi
  # The assertion the whole script exists for. --enable-chromecast is accepted even when the Cast
  # dependencies are absent, and the result is a daemon that runs fine and never lists the speaker.
  grep -q '^#define CHROMECAST 1' config.h \
    || die "configure succeeded but Chromecast support is NOT enabled.
    libprotobuf-c-dev and libgnutls28-dev are what it needs; see $BUILD_DIR/configure.log"
  ok "configured, with CHROMECAST 1"
else
  info "${DIM}[dry-run]${N} ./configure --prefix=$PREFIX --enable-chromecast, then assert CHROMECAST 1"
fi

# --------------------------------------------------------------------------------- build

say "compiling with -j$JOBS"
run make -j"$JOBS"
say "installing to $PREFIX"
info "this needs root"
run sudo make install
if [ "$DRY_RUN" = 0 ]; then
  [ -x "$BIN" ] || die "$BIN was not installed"
  info "$("$BIN" -v 2>&1 | head -1)"
fi
ok "installed"

# ------------------------------------------------------------- the things a package would do

say "making a source build runnable"

# general.uid. Only the default is rewritten: a uid somebody has already chosen is theirs, and this
# script is re-run on every upgrade. One sed on a line-anchored match, after asserting the key
# appears exactly once — deploy/owntone-setup.sh owns the general awk-based editor for the four
# settings it manages, and duplicating it here for one key would be the worse trade.
if [ "$DRY_RUN" = 0 ]; then
  [ -f "$CONF" ] || die "$CONF was not installed"
  count="$(grep -cE '^[[:space:]]*uid[[:space:]]*=' "$CONF" || true)"
  [ "$count" = "1" ] || die "expected exactly one uid line in $CONF, found $count — set it by hand"
  current="$(sed -nE 's/^[[:space:]]*uid[[:space:]]*=[[:space:]]*"([^"]*)".*/\1/p' "$CONF" | head -1)"
  if [ "$current" = "$RUN_UID" ]; then
    info "uid is already $RUN_UID"
  elif [ "$current" = "owntone" ] && ! id owntone >/dev/null 2>&1; then
    sudo cp -p "$CONF" "$CONF.bak.$(date +%Y%m%d%H%M%S)"
    sudo sed -i -E "s|^([[:space:]]*uid[[:space:]]*=[[:space:]]*)\"[^\"]*\"|\1\"$RUN_UID\"|" "$CONF"
    info "uid \"owntone\" (no such user) -> \"$RUN_UID\"; backed the conf up first"
  else
    warn "leaving uid = \"$current\" alone; it is not the unusable default"
  fi
else
  info "${DIM}[dry-run]${N} would set general.uid to $RUN_UID in $CONF if it is the default"
fi

# The daemon starts as root and drops to uid, then has to write its log and its song database.
for dir in "$PREFIX/var/log" "$PREFIX/var/cache/owntone" "$PREFIX/var/run"; do
  run sudo mkdir -p "$dir"
  run sudo chown "$RUN_UID" "$dir"
done
info "$PREFIX/var/{log,cache/owntone,run} owned by $RUN_UID"

if [ "$DO_SERVICE" = 1 ]; then
  # OwnTone's own unit, generated by configure with this prefix baked in. Installed under the name
  # deploy/owntone-setup.sh's restart_owntone() probes for (`systemctl list-unit-files
  # owntone.service`), which is what makes that script work unchanged on Linux.
  if [ "$DRY_RUN" = 0 ] && [ ! -f "$SRC_DIR/owntone.service" ]; then
    die "$SRC_DIR/owntone.service was not generated by configure"
  fi
  say "registering the systemd unit"
  run sudo install -m 0644 "$SRC_DIR/owntone.service" "$UNIT"
  run sudo systemctl daemon-reload
  # Enabled, not started: see the header. A reboot before owntone-setup.sh runs then still brings
  # the daemon up, it simply indexes the default directory until the config is written.
  run sudo systemctl enable owntone >/dev/null 2>&1 || run sudo systemctl enable owntone
  info "enabled, deliberately not started"
  ok "$UNIT"
else
  say "no systemd unit (--no-service)"
  info "run it in the foreground with:  $BIN -f"
fi

# ---------------------------------------------------------------------------- afterwards

say "next"
info "1. configure it and find the speaker:   ./deploy/owntone-setup.sh"
info "   (points library.directories at the music root, restricts trusted_networks to localhost,"
info "    generates admin_password, writes TUNEBOX_OUTPUT_ID, and restarts the daemon)"
info "2. check it:                            tunebox doctor"
info "3. and:                                 tunebox player outputs"
ok "OwnTone $OWNTONE_VERSION built with Chromecast support"
