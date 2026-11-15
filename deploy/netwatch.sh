#!/usr/bin/env bash
#
# tunebox-netwatch — reboot the host when its network has gone and is not coming back.
#
# WHY THIS EXISTS, measured rather than assumed. On 2026-11-14 at 23:24:57 this household's
# Raspberry Pi 3B logged:
#
#     kernel: ieee80211 phy0: brcmf_fw_crashed: Firmware has halted or crashed
#     kernel: mmc1: error -22 whilst initialising SDIO card          (four attempts, all failed)
#     NetworkManager: device (wlan0): activated -> unmanaged
#                     (reason 'unmanaged-link-not-init', managed-type: 'removed')
#
# The Wi-Fi chip's firmware crashed, the driver could not re-initialise the SDIO bus the chip hangs
# off, and the kernel removed wlan0 as a device. For the next twenty hours the machine ran perfectly
# — the hourly cron kept writing to the journal — and was unreachable, until somebody noticed and
# pulled the plug. In those twenty hours there were *zero* network log lines: nothing was retrying,
# because there was nothing left to retry on.
#
# That measurement is what shapes this script, and it rules out the gentler things first:
#
#   * restarting NetworkManager or wpa_supplicant cannot work. The interface was gone. NetworkManager
#     retries continuously on its own anyway, so a failure that is still a failure fifteen minutes
#     later is not one more `nmcli` call away from fixed;
#   * reloading the brcmfmac module is the same bet — the SDIO re-init is the step that failed;
#   * the firmware cannot be fixed. It is a closed Cypress blob, already the newest the Raspberry Pi
#     archive offers, and the newest was built in July 2021.
#
# So a reboot is the only recovery, and the only question worth asking is how to reboot rarely and
# for the right reason.
#
#   * The probe is the **default gateway**, not the internet. The thing being guarded is this host's
#     own link. A house whose broadband is out should not reboot the music server: tunebox is a
#     LAN-only service and works perfectly with no internet at all.
#   * Three consecutive failures, five minutes apart, before anything happens. One missed ping is a
#     collision, not an outage.
#   * At most one watchdog reboot an hour. Without that, a router being rebooted — or a power cut
#     that takes the router and not the Pi — becomes a Pi that reboots every fifteen minutes.
#   * Nothing happens in the first few minutes after boot, because Wi-Fi association takes time and
#     "not up yet" is not "never coming back".
#   * Before rebooting it writes down why, into a journal that is now persistent, because the next
#     question after "it rebooted itself" is always "what did it see".
#
# The probe and the reboot are both injectable, which is not a testing nicety: a recovery path that
# could only be exercised by crashing a Wi-Fi chip is a recovery path nobody has ever run, and this
# project has the canary and the yt-dlp rollback for exactly that reason.
set -euo pipefail

STATE_DIR="${TUNEBOX_NETWATCH_STATE_DIR:-/var/lib/tunebox-netwatch}"
RUN_DIR="${TUNEBOX_NETWATCH_RUN_DIR:-/run/tunebox-netwatch}"
THRESHOLD="${TUNEBOX_NETWATCH_THRESHOLD:-3}"
MIN_REBOOT_INTERVAL="${TUNEBOX_NETWATCH_MIN_REBOOT_INTERVAL:-3600}"
MIN_UPTIME="${TUNEBOX_NETWATCH_MIN_UPTIME:-180}"
REBOOT_CMD="${TUNEBOX_NETWATCH_REBOOT_CMD:-systemctl reboot}"
CHECK_INTERVAL="${TUNEBOX_NETWATCH_INTERVAL:-5min}"

UNIT=/etc/systemd/system/tunebox-netwatch.service
TIMER=/etc/systemd/system/tunebox-netwatch.timer
INSTALLED=/usr/local/sbin/tunebox-netwatch

FAILURES="$RUN_DIR/failures"
LAST_REBOOT="$STATE_DIR/last-reboot"

log() { printf 'tunebox-netwatch: %s\n' "$*"; }

now() { date +%s; }

# Injectable, and with a fallback that does not block: /proc/uptime is Linux's, and the suite that
# exercises this runs on macOS. A guard that cannot read the clock must not be the reason a machine
# with a dead network never recovers — the rate limit below is the protection that has to hold
# everywhere, and it reads a file this script wrote itself.
uptime_seconds() {
  if [ -n "${TUNEBOX_NETWATCH_UPTIME:-}" ]; then printf '%s' "$TUNEBOX_NETWATCH_UPTIME"; return; fi
  if [ -r /proc/uptime ]; then cut -d. -f1 /proc/uptime; else printf 999999; fi
}

# The gateway this host is configured to reach, or nothing. "Nothing" is itself a failure: a machine
# with no default route has no working network, whatever the interfaces say.
gateway() { ip route show default 2>/dev/null | awk '/default/ {print $3; exit}'; }

# Succeeds when the network is usable. Overridable as one command so a test can drive every row of
# the decision table without a network to break.
probe() {
  if [ -n "${TUNEBOX_NETWATCH_PROBE:-}" ]; then
    sh -c "$TUNEBOX_NETWATCH_PROBE"
    return
  fi
  local gw
  gw="$(gateway)" || return 1
  [ -n "$gw" ] || return 1
  ping -c 2 -W 2 "$gw" >/dev/null 2>&1
}

read_count() { [ -r "$FAILURES" ] && cat "$FAILURES" 2>/dev/null || printf 0; }

# What the journal gets before the machine goes down. Everything here is cheap and local; none of it
# needs the network that has just been established to be missing.
diagnose() {
  log "--- state at the moment of the decision ---"
  log "interfaces: $(ip -br link show 2>/dev/null | awk '{printf "%s=%s ", $1, $2}')"
  log "addresses:  $(ip -br addr show scope global 2>/dev/null | awk '{printf "%s=%s ", $1, $3}')"
  log "routes:     $(ip route show default 2>/dev/null | paste -sd'; ' - || echo none)"
  if command -v nmcli >/dev/null 2>&1; then
    log "nmcli:      $(nmcli -t -f DEVICE,STATE device status 2>/dev/null | paste -sd' ' -)"
  fi
  # The line that named the cause last time. Worth capturing even when it is zero, because a zero
  # next time is what would say this is a different fault.
  #
  # Assigned rather than inlined: `grep -c` prints a count *and* exits 1 when the count is zero, so
  # an inline `|| printf '?'` appends to the number instead of replacing it, and grep's trailing
  # newline then splits the log line in two. Command substitution strips that newline here.
  local fw
  fw="$(journalctl -b 0 --no-pager 2>/dev/null |
        grep -cE 'brcmf_fw_crashed|whilst initialising SDIO' || true)"
  log "firmware:   ${fw:-0} crash/SDIO line(s) this boot"
}

check() {
  mkdir -p "$RUN_DIR" "$STATE_DIR"

  local up
  up="$(uptime_seconds)"
  if [ "$up" -lt "$MIN_UPTIME" ]; then
    log "up for ${up}s, less than ${MIN_UPTIME}s — too early to judge, not checking"
    return 0
  fi

  if probe; then
    if [ "$(read_count)" != 0 ]; then
      log "network is back after $(read_count) failed check(s)"
    fi
    printf 0 >"$FAILURES"
    return 0
  fi

  local count
  count=$(( $(read_count) + 1 ))
  printf '%s' "$count" >"$FAILURES"
  log "no route to the gateway (consecutive failures: $count of $THRESHOLD)"
  [ "$count" -ge "$THRESHOLD" ] || return 0

  # Rate limit. Checked here rather than before the probe so that the failures are still counted and
  # still logged while the limit holds — a silent watchdog is indistinguishable from a broken one.
  if [ -r "$LAST_REBOOT" ]; then
    local last since
    last="$(cat "$LAST_REBOOT" 2>/dev/null || printf 0)"
    since=$(( $(now) - last ))
    if [ "$since" -lt "$MIN_REBOOT_INTERVAL" ]; then
      log "threshold reached, but the last watchdog reboot was ${since}s ago" \
          "(minimum ${MIN_REBOOT_INTERVAL}s) — not rebooting"
      return 0
    fi
  fi

  log "threshold reached; the network has been gone for at least" \
      "$(( THRESHOLD - 1 )) check interval(s). Rebooting."
  diagnose
  now >"$LAST_REBOOT"
  # Zeroed before the reboot rather than after: /run is cleared at boot anyway, and if the reboot
  # does not happen (an injected command in a test, a refusal) the count must not stay at the
  # threshold and fire again on the very next tick.
  printf 0 >"$FAILURES"
  # shellcheck disable=SC2086  # deliberately word-split: this is a command line, not one path
  $REBOOT_CMD
}

install_units() {
  [ "$(id -u)" = 0 ] || { echo "netwatch.sh --install needs root" >&2; exit 1; }
  install -m 0755 "${BASH_SOURCE[0]}" "$INSTALLED"
  cat >"$UNIT" <<EOF
[Unit]
Description=tunebox network watchdog — reboot if the gateway has been unreachable
Documentation=file://$INSTALLED
# Not a dependency on the network being up: this unit's whole purpose is the case where it is not.
After=network.target

[Service]
Type=oneshot
ExecStart=$INSTALLED check
# No hardening block here, deliberately. Every other unit this project installs is confined as
# tightly as it can be; this one calls systemctl reboot, so confinement would be theatre.
EOF
  cat >"$TIMER" <<EOF
[Unit]
Description=tunebox network watchdog, every $CHECK_INTERVAL

[Timer]
OnBootSec=$CHECK_INTERVAL
OnUnitActiveSec=$CHECK_INTERVAL
# Persistent is deliberately NOT set: a check missed while the machine was off says nothing about
# the network now, and running it immediately at boot would be the one moment it is most likely to
# be wrong.
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now tunebox-netwatch.timer
  log "installed. Next run:"
  systemctl list-timers tunebox-netwatch.timer --no-pager | sed -n 2p
}

uninstall_units() {
  [ "$(id -u)" = 0 ] || { echo "netwatch.sh --uninstall needs root" >&2; exit 1; }
  systemctl disable --now tunebox-netwatch.timer 2>/dev/null || true
  rm -f "$UNIT" "$TIMER" "$INSTALLED"
  systemctl daemon-reload
  # The state directory is left alone: it holds when the last watchdog reboot happened, which is
  # the one thing here worth having after the watchdog is gone.
  log "removed. $STATE_DIR kept."
}

status() {
  # `systemctl is-active` prints a word *and* exits non-zero for anything but "active", so a
  # `|| echo absent` appends a second line rather than substituting one. Same shape as the
  # `grep -c` trap in diagnose() above.
  local timer
  timer="$(systemctl is-active tunebox-netwatch.timer 2>/dev/null || true)"
  echo "timer:       ${timer:-absent}"
  echo "gateway:     $(gateway || echo none)"
  echo "probe:       $(probe && echo reachable || echo UNREACHABLE)"
  echo "failures:    $(read_count) of $THRESHOLD"
  if [ -r "$LAST_REBOOT" ]; then
    echo "last reboot: $(date -d "@$(cat "$LAST_REBOOT")" 2>/dev/null || cat "$LAST_REBOOT") by the watchdog"
  else
    echo "last reboot: never by the watchdog"
  fi
}

usage() {
  cat <<EOF
Usage: netwatch.sh [check | --install | --uninstall | --status]

  check        one probe; counts, and reboots once the threshold is reached (the default)
  --install    install the systemd timer that runs 'check' every $CHECK_INTERVAL (needs root)
  --uninstall  remove it (needs root)
  --status     what it would do right now, changing nothing

Tunable through the environment, which is also how the tests drive it:
  TUNEBOX_NETWATCH_THRESHOLD            consecutive failures before a reboot ($THRESHOLD)
  TUNEBOX_NETWATCH_MIN_REBOOT_INTERVAL  seconds between watchdog reboots ($MIN_REBOOT_INTERVAL)
  TUNEBOX_NETWATCH_MIN_UPTIME           seconds after boot before judging ($MIN_UPTIME)
  TUNEBOX_NETWATCH_UPTIME               seconds since boot, instead of reading /proc/uptime
  TUNEBOX_NETWATCH_INTERVAL             timer period ($CHECK_INTERVAL)
  TUNEBOX_NETWATCH_PROBE                command to use instead of pinging the gateway
  TUNEBOX_NETWATCH_REBOOT_CMD           command to use instead of '$REBOOT_CMD'
EOF
}

die() { printf 'netwatch.sh: %s\n' "$*" >&2; exit 1; }

case "${1:-check}" in
  check)       check ;;
  --install)   install_units ;;
  --uninstall) uninstall_units ;;
  --status)    status ;;
  -h|--help)   usage; exit 0 ;;
  *)           usage >&2; die "unknown option: $1" ;;
esac
