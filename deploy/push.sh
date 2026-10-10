#!/usr/bin/env bash
#
# Push this checkout to the Raspberry Pi and run the installer there.
#
#   ./deploy/push.sh                     copy, then install (or upgrade) and start
#   ./deploy/push.sh --dry-run           print every step without doing any of it
#   ./deploy/push.sh --no-install        copy only; run the installer by hand afterwards
#   ./deploy/push.sh -- --no-deps        everything after `--` is passed to deploy/install.sh
#
# Why this exists, when `git clone` on the Pi would also work: the working tree is what gets
# deployed, so a change that is not committed yet is still testable on the real hardware — which is
# the whole point of having a Pi on the desk. It also keeps git, credentials and SSH keys off the
# Pi entirely.
#
# Why a tarball over scp rather than rsync: rsync is not guaranteed on a minimal Raspberry Pi OS
# image (this host's trixie does have it, older Lite images did not). `deploy/install.sh` installs
# it because Phase 5's backup needs it, but that happens *after* this transfer, so depending on it
# here would risk failing on exactly the first run that matters. tar and gzip are on every image.
# The tree is ~500 KB once ignored paths are dropped, so incremental transfer buys nothing.
#
# Why the staging directory is replaced rather than extracted over: install.sh pip-installs from the
# checkout and is deliberately not editable ("so the clone can be deleted afterwards"), so a file
# deleted locally but left behind on the Pi would be packaged into the venv and run. Replacing the
# directory makes the deployed tree equal to the local one rather than the union of every push.
#
set -euo pipefail

VERSION="1"

# ---------------------------------------------------------------------------- defaults

# The ssh alias or user@host of the Pi. A name from ~/.ssh/config is the intended form: it keeps the
# key, the username and the hostname in the one place ssh already looks.
HOST_DEFAULT="${TUNEBOX_PI_HOST:-tunebox}"
# Relative to the remote home, which is resolved over ssh rather than assumed: ~ cannot be used here
# because the path is shell-quoted before it is sent, and /home/<user> is a guess that is wrong on
# any host where it is wrong.
SRC_NAME_DEFAULT="tunebox-src"

HOST="$HOST_DEFAULT"
SRC_NAME="$SRC_NAME_DEFAULT"
SRC_DIR=""
DO_INSTALL=1
DRY_RUN=0
INSTALL_ARGS=()

# Only used when this is not a git checkout — see the pack step, which prefers git's own idea of
# what is ignored. Build artefacts, caches, and the two directories that are large and meaningless
# on another machine: .venv holds absolute paths and macOS binaries, music/ is a local library.
EXCLUDES=(
  ".git"
  ".venv"
  "venv"
  "music"
  ".tunebox-tmp"
  "__pycache__"
  "*.pyc"
  ".pytest_cache"
  ".mypy_cache"
  ".ruff_cache"
  "*.egg-info"
  "dist"
  "build"
  ".DS_Store"
)

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

# ------------------------------------------------------------------------------- output

# Colour only when stdout is a terminal, matching deploy/install.sh: these lines end up in a pipe or
# a log often enough.
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
tunebox push-to-Pi (v$VERSION)

Usage: deploy/push.sh [options] [-- installer options]

  --host HOST        ssh target: an alias from ~/.ssh/config, or user@host
                     (default: $HOST_DEFAULT, or \$TUNEBOX_PI_HOST)
  --src-dir NAME     staging directory under the remote home (default: $SRC_NAME_DEFAULT)
  --music-root DIR   passed to the installer; on a Pi point this at the USB SSD
  --api-host ADDR    passed to the installer as --host: the address tunebox binds on the Pi.
                     Omit it and the installer reads the Pi's own default route, which is right
                     unless that address can change under you
  --no-install       copy the tree and stop
  --dry-run          print every step, change nothing
  -h, --help         this

Anything after -- is handed to deploy/install.sh verbatim:

  deploy/push.sh -- --no-deps --no-maintenance

Needs key-based ssh to the Pi already working (ssh-copy-id), because the transfer and the
install are several ssh calls and none of them should ask for a password.
EOF
}

# --------------------------------------------------------------------------- arguments

# Parsed before anything else happens, so --help and a bad flag touch nothing at all.
while [ $# -gt 0 ]; do
  case "$1" in
    --host)       HOST="${2:?--host needs an ssh target}"; shift 2 ;;
    --src-dir)    SRC_NAME="${2:?--src-dir needs a name}"; shift 2 ;;
    --music-root) INSTALL_ARGS+=(--music-root "${2:?--music-root needs a directory}"); shift 2 ;;
    --api-host)   INSTALL_ARGS+=(--host "${2:?--api-host needs an address}"); shift 2 ;;
    --no-install) DO_INSTALL=0; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    --)           shift; while [ $# -gt 0 ]; do INSTALL_ARGS+=("$1"); shift; done ;;
    *)            printf '%serror:%s unknown option: %s\n' "$RED" "$N" "$1" >&2
                  printf 'Try: deploy/push.sh --help\n' >&2
                  exit 1 ;;
  esac
done

case "$SRC_NAME" in
  # A path would escape the remote home, and this directory gets removed and recreated on every
  # push — so it is a single component by construction rather than by trust.
  */*|""|.|..) die "--src-dir takes a single directory name, not a path: $SRC_NAME" ;;
esac

# ------------------------------------------------------------------------------ helpers

# Shell-quote one argument for the remote shell. Every value interpolated into a remote command goes
# through this: a music root with a space in it is ordinary, and "Backup Disk" is why
# deploy/install.sh quotes its systemd directives too.
q() { printf '%q' "$1"; }

ssh_quiet() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" "$@"; }

# ------------------------------------------------------------------------------ preflight

say "checking the local checkout"
[ -f "$REPO_ROOT/pyproject.toml" ] || die "$REPO_ROOT is not a tunebox checkout (no pyproject.toml)"
[ -x "$REPO_ROOT/deploy/install.sh" ] || die "$REPO_ROOT/deploy/install.sh is missing or not executable"
for tool in ssh scp tar; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed locally"
done
ok "$REPO_ROOT"

say "checking $HOST"
REMOTE_HOME=""
REMOTE_ARCH=""
REMOTE_PY=""
REMOTE_OS=""
probe=""
# One round trip for everything worth knowing. Single quotes on purpose: these expand on the Pi.
# shellcheck disable=SC2016
if probe="$(ssh_quiet 'printf "%s\n%s\n%s\n%s\n" "$HOME" "$(uname -m)" "$(python3 -V 2>&1 || echo none)" "$(uname -s)"' 2>&1)"; then
  REMOTE_HOME="$(printf '%s\n' "$probe" | sed -n 1p)"
  REMOTE_ARCH="$(printf '%s\n' "$probe" | sed -n 2p)"
  REMOTE_PY="$(printf '%s\n' "$probe" | sed -n 3p)"
  REMOTE_OS="$(printf '%s\n' "$probe" | sed -n 4p)"
  info "home:   $REMOTE_HOME"
  info "arch:   $REMOTE_ARCH ($REMOTE_OS)"
  info "python: $REMOTE_PY"
  case "$REMOTE_ARCH" in
    armv6l|armv7l|arm)
      # README: there is no official deno build for 32-bit ARM, so the installer falls back to
      # QuickJS — an interpreter with no JIT, running yt-dlp's signature deciphering on every
      # download. A Pi 3 is ARMv8 and can run the 64-bit image instead.
      warn "$REMOTE_ARCH is a 32-bit userland: no deno build exists, so yt-dlp will use QuickJS."
      warn "Re-imaging with 64-bit Raspberry Pi OS is the fix if downloads feel slow."
      ;;
  esac
  [ "$REMOTE_OS" = "Linux" ] || warn "$HOST reports $REMOTE_OS, not Linux — the installer's Linux branch will not be used"
  ok "reachable over ssh with a key"
else
  if [ "$DRY_RUN" = 1 ]; then
    warn "cannot reach $HOST over ssh; continuing because this is a dry run"
    info "$probe"
    REMOTE_HOME="/home/UNKNOWN"
  else
    printf '%s\n' "$probe" >&2
    die "cannot reach $HOST with key-based ssh. Fix that first:
    ssh-copy-id -i ~/.ssh/id_ed25519_tunebox.pub $HOST
    then re-run. A password prompt here would appear several times and break the install."
  fi
fi

SRC_DIR="$REMOTE_HOME/$SRC_NAME"
case "$SRC_DIR" in
  /|"$REMOTE_HOME") die "refusing to use $SRC_DIR as the staging directory: it is removed on every push" ;;
esac

# --------------------------------------------------------------------------------- pack

say "packing the working tree"
STAGE=""
cleanup() { [ -n "$STAGE" ] && rm -rf "$STAGE"; }
trap cleanup EXIT
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/tunebox-push.XXXXXX")"
TARBALL="$STAGE/tunebox.tar.gz"
LISTFILE="$STAGE/files.z"

# --no-xattrs because GNU tar on the Pi prints a warning per file for macOS's
# com.apple.provenance attribute, which is 100 lines of noise around a successful extract.
# --no-mac-metadata drops the AppleDouble ._ files with it, and is bsdtar-only.
tar_common=(--no-xattrs)
case "$(tar --version 2>&1 | head -1)" in
  *bsdtar*) tar_common+=(--no-mac-metadata) ;;
esac

if git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  # `--cached --others --exclude-standard` is exactly "the working tree, minus what .gitignore
  # excludes": tracked files plus untracked ones, which keeps uncommitted work deployable while
  # honouring the ignore rules the repo already has. That matters for more than tidiness — the
  # checkout's `music/` is a real local library of test tracks, and a static exclude list would
  # have to be kept in step with .gitignore by hand. Measured: 15 MB of tarball becomes 500 KB.
  file_count=0
  while IFS= read -r -d '' path; do
    # A tracked file deleted but not yet committed is still listed, and tar would fail on it.
    [ -e "$REPO_ROOT/$path" ] || continue
    printf '%s\0' "$path"
    file_count=$((file_count + 1))
  done < <(git -C "$REPO_ROOT" ls-files -z --cached --others --exclude-standard) >"$LISTFILE"
  [ "$file_count" -gt 0 ] || die "git listed no files to deploy from $REPO_ROOT"
  info "$file_count files, per git (ignored paths such as music/ are not sent)"
  run tar czf "$TARBALL" "${tar_common[@]}" -C "$REPO_ROOT" --null -T "$LISTFILE"
else
  # A tarball of the checkout, or an export with no .git: workable, but the ignore rules are then
  # this list rather than the repo's, so it is the fallback and says so.
  warn "$REPO_ROOT is not a git checkout; falling back to a static exclude list"
  tar_args=()
  for pattern in "${EXCLUDES[@]}"; do
    tar_args+=(--exclude "$pattern")
  done
  # -C so the archive holds plain relative paths and extracts into any directory. No leading
  # component to strip, which is one fewer thing to get wrong on the far end.
  run tar czf "$TARBALL" "${tar_common[@]}" "${tar_args[@]}" -C "$REPO_ROOT" .
fi
if [ "$DRY_RUN" = 0 ]; then
  info "$(du -h "$TARBALL" | awk '{print $1}') compressed"
fi
ok "packed"

# --------------------------------------------------------------------------------- copy

REMOTE_TAR="$SRC_DIR.tar.gz"

say "copying to $HOST:$SRC_DIR"
run scp -q "$TARBALL" "$HOST:$REMOTE_TAR"
# Replace rather than merge — see the header. The rm is scoped to a directory this script owns and
# whose name cannot contain a path separator.
run ssh "$HOST" "set -e
  rm -rf $(q "$SRC_DIR")
  mkdir -p $(q "$SRC_DIR")
  tar xzf $(q "$REMOTE_TAR") -C $(q "$SRC_DIR")
  rm -f $(q "$REMOTE_TAR")"
ok "copied"

if [ "$DO_INSTALL" = 0 ]; then
  say "not installing (--no-install)"
  info "run it yourself with:  ssh $HOST 'cd $SRC_NAME && ./deploy/install.sh'"
  exit 0
fi

# ------------------------------------------------------------------------------ install

# -t because the installer uses sudo for apt-get and the systemd unit, and sudo needs a terminal to
# prompt on. Without it the install stalls invisibly waiting for a password nobody can type.
install_cmd="cd $(q "$SRC_DIR") && ./deploy/install.sh"
if [ ${#INSTALL_ARGS[@]} -gt 0 ]; then
  for arg in "${INSTALL_ARGS[@]}"; do
    install_cmd="$install_cmd $(q "$arg")"
  done
fi

say "running the installer on $HOST"
info "$install_cmd"
run ssh -t "$HOST" "$install_cmd"

# ---------------------------------------------------------------------------- afterwards

if [ "$DRY_RUN" = 1 ]; then
  exit 0
fi

say "what the Pi ended up with"
# Only these three keys are read back. TUNEBOX_TOKEN is in the same file and is deliberately not
# printed: this script's output is the kind of thing that gets pasted into a chat window.
if settings="$(ssh_quiet "grep -E '^TUNEBOX_(API_HOST|API_PORT|MUSIC_ROOT)=' ~/.config/tunebox/tunebox.env 2>/dev/null" || true)"; then
  [ -n "$settings" ] && printf '    %s\n' "$settings"
fi
info "pair a phone:  ssh -t $HOST '~/.local/share/tunebox/venv/bin/tunebox pair'"
info "check it:      ssh -t $HOST '~/.local/share/tunebox/venv/bin/tunebox doctor'"
ok "done"
