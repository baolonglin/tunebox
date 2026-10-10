"""The deployment scripts — ``deploy/install.sh`` and ``deploy/owntone-setup.sh``.

Shell is the part of this project a Python test suite is least able to check, and also the part with
the worst failure modes: an installer bug is discovered by somebody standing in front of a Raspberry
Pi, and a bad edit to ``owntone.conf`` breaks playback for the whole house. So this file tests the
three things that *can* be tested without a Pi, and they are chosen because each corresponds to a
mistake that is easy to make and expensive to find:

* **The Linux service path is exercised on macOS**, by putting ``uname``/``systemctl``/``sudo`` shims
  on ``PATH`` and running ``--dry-run``. Without this, the systemd unit — the thing that actually
  matters for the Pi — would never be looked at by anything until the Pi existed.
* **``--dry-run`` must change nothing.** It is the only way to read what the installer will do to a
  machine, so a dry run that quietly created a directory would make the whole promise worthless.
* **The ``owntone.conf`` editor is tested against realistic input**, including the two cases that
  broke it while it was being written: a setting that is commented out, and a value that spans lines.
* **The launchd agents are written for real** and their contents asserted, for both tunebox and the
  player daemon, because a ``--dry-run`` cannot show the contents of a file it does not write and the
  contents are the whole point: ``KeepAlive`` is the difference between a daemon that survives a
  reboot and one somebody restarts by hand.

What is deliberately *not* tested here: whether ``apt-get`` has the packages, whether OwnTone builds,
and whether the service survives a reboot. None of those can be established from here, and the
scripts are written to fail loudly rather than guess about them.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy"
INSTALL = DEPLOY / "install.sh"
OWNTONE = DEPLOY / "owntone-setup.sh"
START = DEPLOY / "start.sh"
PUSH = DEPLOY / "push.sh"
OWNTONE_BUILD = DEPLOY / "owntone-build.sh"
SCRIPTS = (INSTALL, OWNTONE, START, PUSH, OWNTONE_BUILD)
# The two that parse tunebox.env with sed, and therefore have to undo its quoting by hand.
SED_READERS = (INSTALL, OWNTONE)


def run(argv, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=180, **kwargs)


def code(script: Path) -> str:
    """A script with its whole-line comments removed.

    Needed rather than tidy, and for the same reason as in ``tests/test_ui.py``: these scripts explain
    themselves at length, and the explanations legitimately name the things being asserted against —
    "systemd's ``EnvironmentFile=`` would leave two places...", "python3 rather than jq...". Matching
    the prose would either fail on documentation or force the documentation to avoid naming what it
    documents. Only full-line comments are dropped, because a ``#`` mid-line appears inside strings
    here (``/#token=``) and is not a comment.
    """
    return "\n".join(
        line
        for line in script.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


# ------------------------------------------------------------------------ hygiene


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
class TestHygiene:
    def test_it_is_executable(self, script: Path):
        """Because the README tells people to run ``./deploy/install.sh``."""
        assert script.stat().st_mode & stat.S_IXUSR

    def test_it_parses(self, script: Path):
        assert run(["bash", "-n", str(script)]).returncode == 0

    def test_it_stops_on_error(self, script: Path):
        """``set -e`` and ``set -u`` in an installer are the difference between "failed" and "half
        installed"; ``pipefail`` because most of the interesting commands are in pipelines."""
        assert "set -euo pipefail" in script.read_text(encoding="utf-8")

    def test_help_works_and_changes_nothing(self, script: Path, tmp_path: Path):
        completed = run([str(script), "--help"], env={**os.environ, "HOME": str(tmp_path)})
        assert completed.returncode == 0
        assert "Usage" in completed.stdout
        assert list(tmp_path.iterdir()) == []

    def test_an_unknown_option_is_refused(self, script: Path, tmp_path: Path):
        """Rather than being ignored, which in an installer means doing the wrong thing silently."""
        completed = run([str(script), "--obliterate"], env={**os.environ, "HOME": str(tmp_path)})
        assert completed.returncode != 0
        assert "unknown option" in completed.stderr

    def test_shellcheck_is_clean(self, script: Path):
        shellcheck = shutil.which("shellcheck")
        if shellcheck is None:
            pytest.skip("shellcheck is not installed")
        completed = run([shellcheck, "--severity=warning", str(script)])
        assert completed.returncode == 0, completed.stdout


# -------------------------------------------------------------------- the installer


def shim(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def linux_path(tmp_path: Path) -> str:
    """A ``PATH`` that makes this machine look like a Raspberry Pi to the installer.

    The point is the systemd unit. It is the only part of the installer that a Pi depends on and a
    Mac never runs, so without a way to reach it from here it would go unread until the hardware
    existed — which is exactly the sort of thing that is then discovered over SSH at the wrong time.
    """
    bin_dir = tmp_path / "shims"
    bin_dir.mkdir()
    shim(bin_dir, "uname", 'if [ "${1:-}" = "-s" ]; then echo Linux; else echo aarch64; fi')
    shim(bin_dir, "systemctl", "exit 0")
    shim(bin_dir, "apt-get", "exit 0")
    # sudo must be a shim too: a --dry-run that prompted for a password would hang the suite.
    shim(bin_dir, "sudo", 'exec "$@"')
    shim(bin_dir, "ip", "exit 1")
    shim(bin_dir, "hostname", "echo 192.168.50.99")
    return f"{bin_dir}{os.pathsep}{os.environ['PATH']}"


def install(tmp_path: Path, *args: str, path: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(tmp_path / "home")}
    if path is not None:
        env["PATH"] = path
    (tmp_path / "home").mkdir(exist_ok=True)
    return run(
        [
            str(INSTALL),
            "--dry-run",
            "--no-deps",
            "--prefix",
            str(tmp_path / "prefix"),
            "--config-dir",
            str(tmp_path / "config"),
            "--music-root",
            str(tmp_path / "music"),
            *args,
        ],
        env=env,
    )


class TestDryRun:
    def test_it_creates_nothing_at_all(self, tmp_path: Path):
        """``--dry-run`` is the only way to read what this will do to a machine before it does it.
        A dry run that created one directory would make that promise untrustworthy."""
        completed = install(tmp_path)
        assert completed.returncode == 0, completed.stderr
        assert not (tmp_path / "prefix").exists()
        assert not (tmp_path / "config").exists()
        assert not (tmp_path / "music").exists()
        assert list((tmp_path / "home").iterdir()) == []

    def test_it_says_what_needs_root_before_doing_anything(self, tmp_path: Path):
        out = install(tmp_path).stdout
        assert "steps that change the system:" in out

    def test_it_prints_no_token(self, tmp_path: Path):
        """Nothing is configured yet, so there is nothing to leak — but a dry run that generated and
        printed a real token would put a secret in whatever the operator piped this into."""
        completed = install(tmp_path)
        assert "TUNEBOX_TOKEN=" not in completed.stdout
        assert "#token=" not in completed.stdout


class TestTheLinuxPath:
    def test_it_installs_a_systemd_unit(self, tmp_path: Path, linux_path: str):
        completed = install(tmp_path, path=linux_path)
        assert completed.returncode == 0, completed.stderr + completed.stdout
        assert "/etc/systemd/system/tunebox.service" in completed.stdout

    def test_the_unit_runs_as_an_unprivileged_user_with_one_writable_path(
        self, tmp_path: Path, linux_path: str
    ):
        """PLAN.md 3.2: *"Run the worker as an unprivileged user with write access to the music
        directory and nothing else."* These directives are what turn that from an instruction into a
        property of the running system."""
        out = install(tmp_path, path=linux_path).stdout
        for directive in (
            "NoNewPrivileges=yes",
            "ProtectSystem=strict",
            "ProtectHome=read-only",
            "PrivateTmp=yes",
            "RestrictSUIDSGID=yes",
        ):
            assert directive in out, directive
        assert f'ReadWritePaths="{tmp_path / "music"}"' in out
        assert f"User={os.environ.get('USER', '')}" in out or "User=" in out

    def test_the_unit_starts_at_boot_without_a_login(self, tmp_path: Path, linux_path: str):
        """A Pi is headless and reboots unattended, so this must be a *system* unit — a
        ``systemd --user`` service would need loginctl lingering to come back on its own."""
        out = install(tmp_path, path=linux_path).stdout
        assert "WantedBy=multi-user.target" in out

    def test_a_missing_player_does_not_stop_tunebox(self, tmp_path: Path, linux_path: str):
        """Ingest works fine with no OwnTone installed (PLAN.md 3.1), so the dependency on it has to
        be Wants, never Requires — otherwise a host with no player has no tunebox either."""
        out = install(tmp_path, path=linux_path).stdout
        assert "Wants=network-online.target owntone.service" in out
        assert "Requires=owntone" not in out

    def test_it_finds_the_lan_address_without_ip_command(self, tmp_path: Path, linux_path: str):
        """The ``ip`` shim fails, so this exercises the ``hostname -I`` fallback."""
        out = install(tmp_path, path=linux_path).stdout
        assert "LAN address: 192.168.50.99" in out


class TestTheMacosPath:
    def test_it_installs_a_launch_agent(self, tmp_path: Path):
        out = install(tmp_path).stdout
        assert "io.tunebox.serve.plist" in out

    def test_the_agent_is_given_a_usable_path(self, tmp_path: Path):
        """launchd starts a job with a minimal PATH, so without this the service comes up and every
        single download fails on a missing ffmpeg."""
        assert "/opt/homebrew/bin:/usr/local/bin" in code(INSTALL)


class TestTheLaunchdUpgradeRace:
    """``bootout`` is asynchronous, and an upgrade that ignores that leaves nothing running.

    This is a real bug, found by running the installer twice on this host, and it is the worst shape a
    bug in an installer can have: ``launchctl bootout`` returns 0 immediately and unloads in the
    background, so the ``bootstrap`` that follows fails with "Bootstrap failed: 5: Input/output error"
    — and the *previous* service has already stopped. The result is a host with no tunebox on it,
    arrived at by running the upgrade command, reported by nothing.

    So the test is behavioural rather than a grep: ``install_service_launchd`` runs against a
    ``launchctl`` that behaves the way the real one does, including staying visible for a while after
    ``bootout`` and refusing ``bootstrap`` while it is.
    """

    @staticmethod
    def harness(tmp_path: Path) -> tuple[Path, Path]:
        source = code(INSTALL)
        start = source.index("install_service_launchd() {")
        end = source.index("\n}\n", start) + 3

        bin_dir = tmp_path / "shims"
        bin_dir.mkdir()
        state = tmp_path / "state"
        # Loaded to begin with: this is the upgrade case, which is the one that broke.
        state.write_text("loaded\n", encoding="utf-8")
        calls = tmp_path / "calls"
        calls.write_text("", encoding="utf-8")
        shim(
            bin_dir,
            "launchctl",
            f"""
STATE={state}
CALLS={calls}
echo "$1" >>"$CALLS"
case "$1" in
  print)
    read -r value <"$STATE" || value=""
    case "$value" in
      loaded) exit 0 ;;
      draining2) echo draining1 >"$STATE"; exit 0 ;;
      draining1) echo gone >"$STATE"; exit 0 ;;
      *) exit 1 ;;
    esac
    ;;
  bootout)   echo draining2 >"$STATE"; exit 0 ;;
  bootstrap)
    read -r value <"$STATE" || value=""
    if [ "$value" != gone ]; then
      echo "Bootstrap failed: 5: Input/output error" >&2
      exit 5
    fi
    echo loaded >"$STATE"
    exit 0
    ;;
esac
exit 0
""",
        )

        script = tmp_path / "harness.sh"
        script.write_text(
            "set -euo pipefail\n"
            "DRY_RUN=0\n"
            "LAUNCHD_LABEL=io.tunebox.test\n"
            f'LAUNCH_AGENT="{tmp_path}/agent.plist"\n'
            'WRAPPER="/usr/bin/true"\n'
            f'TUNEBOX_HOME="{tmp_path}/prefix"\n'
            'say() { echo "==> $*"; }\n'
            'info() { echo "    $*"; }\n'
            'ok() { echo "ok $*"; }\n'
            'die() { echo "die: $*" >&2; exit 1; }\n'
            'run() { "$@"; }\n'
            + source[start:end]
            + "\ninstall_service_launchd\n",
            encoding="utf-8",
        )
        return script, calls

    def shimmed(self, tmp_path: Path) -> dict[str, str]:
        return {**os.environ, "PATH": f"{tmp_path / 'shims'}{os.pathsep}{os.environ['PATH']}"}

    def test_an_upgrade_ends_with_the_agent_loaded(self, tmp_path: Path):
        script, _ = self.harness(tmp_path)
        completed = run(["bash", str(script)], env=self.shimmed(tmp_path))
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "Bootstrap failed" not in completed.stderr
        assert "agent loaded" in completed.stdout

    def test_it_waits_for_the_unload_rather_than_hoping(self, tmp_path: Path):
        """The evidence that the fix is the wait and not luck: ``print`` is polled between the
        ``bootout`` and the ``bootstrap``."""
        script, calls = self.harness(tmp_path)
        run(["bash", str(script)], env=self.shimmed(tmp_path))

        sequence = calls.read_text(encoding="utf-8").split()
        assert sequence[0] == "print", sequence
        bootout = sequence.index("bootout")
        bootstrap = sequence.index("bootstrap")
        assert sequence[bootout + 1 : bootstrap].count("print") >= 2, sequence

    def test_a_first_install_does_not_boot_out_anything(self, tmp_path: Path):
        """Nothing is loaded yet, so there is nothing to unload — and a bootout here would only add a
        failure to explain away."""
        script, calls = self.harness(tmp_path)
        (tmp_path / "state").write_text("gone\n", encoding="utf-8")

        completed = run(["bash", str(script)], env=self.shimmed(tmp_path))

        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "bootout" not in calls.read_text(encoding="utf-8").split()


class TestTheConfigIsHandledSafely:
    def test_the_token_is_written_with_a_restrictive_mode(self):
        """``umask`` before the write, not ``chmod`` after: otherwise the file exists, containing the
        token, world-readable, for as long as it takes to run the next command."""
        source = code(INSTALL)
        assert "umask 077" in source
        assert 'chmod 600 "$ENV_FILE"' in source
        assert 'chmod 700 "$CONFIG_DIR"' in source

    def test_the_token_is_never_regenerated(self):
        """Re-running the installer is the normal way to upgrade. A fresh token would silently log
        out every phone in the house, and the only symptom would be a token prompt."""
        source = code(INSTALL)
        # The generator is reachable from exactly two places: a first install, and a config file
        # that somehow has no token at all.
        assert source.count("$(generate_token)") == 2
        assert "keeping the existing token" in source

    def test_it_never_binds_the_wildcard(self):
        """PLAN.md 3.2 forbids it, and an installer that reached for 0.0.0.0 to make things "just
        work" is the most likely way it would ever happen."""
        source = code(INSTALL)
        assert "0.0.0.0" not in source
        assert "TUNEBOX_ALLOW_ALL_INTERFACES" not in source

    def test_uninstall_keeps_the_music(self):
        """The library is the only irreplaceable state in the system (PLAN.md 3.1): tracks can be
        re-ingested but tags and playlists cannot."""
        source = code(INSTALL)
        assert "the music library was not touched" in source
        assert 'rm -rf "$VENV" "$TUNEBOX_HOME/bin"' in source
        assert 'rm -rf "$CONFIGURED_MUSIC"' not in source
        assert 'rm -rf "$MUSIC_ROOT"' not in source

    def test_the_wrapper_is_the_single_place_config_is_loaded(self):
        """launchd cannot read an environment file and systemd's own directive can, so using the
        wrapper on both keeps one answer to "where does the config come from" — and makes the service
        reproducible by hand."""
        source = code(INSTALL)
        assert "EnvironmentFile=" not in source
        assert "ExecStart=$WRAPPER" in source
        assert "<string>$WRAPPER</string>" in source


# --------------------------------------------------------- Phase 5: the nightly job


class TestTheNightlyJob:
    """The scheduled maintenance job — PLAN.md Phase 5's "yt-dlp auto-update + canary + rollback"
    and "daily health-check ingest, alerting, backups" — has to be *installed* to be any of those.

    One timer rather than three. The order of the steps matters and a scheduler cannot express it
    (the update must precede the day's canary, the backup must follow both), and one bad night should
    produce one alert rather than three.
    """

    def test_the_wrapper_is_written_next_to_the_service_one(self, tmp_path: Path):
        out = install(tmp_path).stdout
        assert "bin/tunebox-maintain" in out

    def test_the_wrapper_loads_the_same_config_as_the_service(self):
        """So the nightly canary downloads with the same yt-dlp and the backup copies the same music
        root. Two wrappers rather than one argument because the two are run by different things with
        different lifetimes — a supervised daemon and a job that exits."""
        source = code(INSTALL)
        assert 'exec "$VENV/bin/tunebox" maintain' in source
        assert source.count('. "$ENV_FILE"') >= 2

    def test_it_can_be_skipped(self, tmp_path: Path):
        out = install(tmp_path, "--no-maintenance").stdout
        assert "skipping the nightly maintenance job" in out
        assert "io.tunebox.maintain.plist (nightly" not in out

    def test_the_installer_cannot_undo_a_canaried_update(self):
        """The interaction that is easy to miss: ``pyproject.toml`` pins yt-dlp deliberately, so
        ``pip install --upgrade "$REPO_ROOT"`` *downgrades* a version that ``tunebox update``
        installed and verified — silently putting back one that may not be able to download
        anything. The installer is the upgrade path, so it must not reintroduce a fixed bug."""
        source = code(INSTALL)
        assert "tunebox\" update --reapply" in source or "tunebox update --reapply" in source
        assert source.index("update --reapply") > source.index("install_tunebox")

    def test_uninstall_removes_the_schedule_too(self, tmp_path: Path):
        """Otherwise a removed tunebox keeps waking up every night to fail."""
        env = {**os.environ, "HOME": str(tmp_path / "home")}
        (tmp_path / "home").mkdir(exist_ok=True)
        completed = run(
            [
                str(INSTALL), "--uninstall", "--dry-run",
                "--prefix", str(tmp_path / "prefix"),
                "--config-dir", str(tmp_path / "config"),
            ],
            env=env,
        )
        assert completed.returncode == 0, completed.stderr
        assert "io.tunebox.maintain" in completed.stdout
        assert "io.tunebox.maintain.plist" in completed.stdout


class TestTheNightlyJobOnLinux:
    def test_it_installs_a_timer_and_a_oneshot_service(self, tmp_path: Path, linux_path: str):
        out = install(tmp_path, path=linux_path).stdout
        assert "/etc/systemd/system/tunebox-maintain.timer" in out
        assert "Type=oneshot" in out
        assert "WantedBy=timers.target" in out

    def test_the_timer_survives_a_machine_that_was_switched_off(
        self, tmp_path: Path, linux_path: str
    ):
        """``Persistent=true`` is the difference between a health check that runs and one that only
        runs on machines that are never unplugged — and a Raspberry Pi is unplugged."""
        out = install(tmp_path, path=linux_path).stdout
        assert "Persistent=true" in out
        assert "OnCalendar=*-*-* 04:17:00" in out
        assert "RandomizedDelaySec=" in out

    def test_the_timer_is_enabled_rather_than_the_service(self):
        """Enabling a ``Type=oneshot`` service would also run it at every boot, which is not what
        "nightly" means."""
        source = code(INSTALL)
        assert 'systemctl enable --now "$MAINTENANCE_NAME.timer"' in source
        assert 'systemctl enable "$MAINTENANCE_NAME"' not in source

    def test_the_update_can_write_the_venv_and_the_service_still_cannot(
        self, tmp_path: Path, linux_path: str
    ):
        """The one place these two units differ, and it is load-bearing in both directions: pip has
        to be able to install a new yt-dlp into the venv, and the long-running service must not be
        able to rewrite its own code."""
        out = install(tmp_path, path=linux_path).stdout
        service, _, maintenance = out.partition("tunebox nightly maintenance")
        assert f'ReadWritePaths="{tmp_path / "music"}"' in service
        assert str(tmp_path / "prefix") not in service.split("ReadWritePaths=")[1].split("\n")[0]
        assert f'ReadWritePaths="{tmp_path / "music"}"' in maintenance
        assert f'ReadWritePaths="{tmp_path / "prefix"}"' in maintenance

    def test_a_path_with_a_space_in_it_is_one_path(self, tmp_path: Path, linux_path: str):
        """systemd splits a directive's value on whitespace unless it is quoted, so an external disk
        called "Backup Disk" would otherwise become two invalid paths and a unit that refuses to
        start — discovered by an audit, and entirely ordinary on macOS."""
        out = install(tmp_path, path=linux_path).stdout
        for line in out.splitlines():
            if "ReadWritePaths=" in line:
                value = line.split("ReadWritePaths=", 1)[1].strip()
                assert value.startswith('"') and value.endswith('"'), line

    def test_the_hardening_is_not_relaxed_for_the_nightly_job(
        self, tmp_path: Path, linux_path: str
    ):
        out = install(tmp_path, path=linux_path).stdout
        _, _, maintenance = out.partition("tunebox nightly maintenance")
        for directive in (
            "NoNewPrivileges=yes",
            "ProtectSystem=strict",
            "ProtectHome=read-only",
            "PrivateTmp=yes",
            "RestrictSUIDSGID=yes",
        ):
            assert directive in maintenance, directive

    def test_a_remote_backup_destination_is_not_a_writable_path(self, tmp_path: Path):
        """``pi@nas:/srv/music`` is rsync-over-ssh: it needs no filesystem permission at all, and
        listing it as one would make systemd refuse to start the unit."""
        source = code(INSTALL)
        start = source.index("maintenance_rw_paths() {")
        end = source.index("\n}\n", start) + 3
        harness = tmp_path / "rw.sh"
        harness.write_text(
            "set -euo pipefail\n"
            f'CONFIGURED_MUSIC="{tmp_path}/music"\n'
            f'TUNEBOX_HOME="{tmp_path}/prefix"\n'
            f'ENV_FILE="{tmp_path}/env"\n'
            "env_value() { sed -n \"s/^$1=//p\" \"$ENV_FILE\" | tail -1; }\n"
            'env_value_or() { local f; f="$(env_value "$1")" || f=""; '
            'if [ -n "$f" ]; then printf %s "$f"; else printf %s "$2"; fi; }\n'
            + source[start:end]
            + "\nmaintenance_rw_paths\n",
            encoding="utf-8",
        )

        for destination, expected in (
            ("", False),
            ("/Volumes/Backup/tunebox", True),
            ("/Volumes/Backup Disk/tunebox", True),
            ("pi@nas:/srv/music", False),
            ("rsync://nas/music", False),
            # "deliberately no backup" grants no write access to anything.
            ("none", False),
        ):
            (tmp_path / "env").write_text(
                f"TUNEBOX_BACKUP_DEST={destination}\n" if destination else "", encoding="utf-8"
            )
            lines = run(["bash", str(harness)]).stdout.splitlines()
            assert (destination in lines) is expected, (destination, lines)
            assert str(tmp_path / "music") in lines
            assert str(tmp_path / "prefix") in lines


class TestTheNightlyJobOnMacos:
    """The plist is written for real here, by running the installer's own function in isolation —
    the same technique as :class:`TestTheLaunchdUpgradeRace`, because a ``--dry-run`` cannot show the
    contents of a file it does not write, and the contents are the whole point."""

    @staticmethod
    def write_plist(tmp_path: Path) -> Path:
        source = code(INSTALL)
        start = source.index("install_maintenance_launchd() {")
        end = source.index("\n}\n", start) + 3

        bin_dir = tmp_path / "shims"
        bin_dir.mkdir()
        calls = tmp_path / "calls"
        calls.write_text("", encoding="utf-8")
        # Nothing loaded, so this is the first-install path: print fails, bootstrap succeeds.
        shim(bin_dir, "launchctl", f'echo "$1" >>"{calls}"\n[ "$1" = print ] && exit 1\nexit 0')

        agent = tmp_path / "io.tunebox.maintain.plist"
        script = tmp_path / "harness.sh"
        script.write_text(
            "set -euo pipefail\n"
            "DRY_RUN=0\n"
            "LAUNCHD_MAINT_LABEL=io.tunebox.maintain\n"
            f'MAINT_AGENT="{agent}"\n'
            f'MAINT_WRAPPER="{tmp_path}/prefix/bin/tunebox-maintain"\n'
            f'TUNEBOX_HOME="{tmp_path}/prefix"\n'
            'MAINTENANCE_HOUR=4\nMAINTENANCE_MINUTE=17\n'
            'say() { echo "==> $*"; }\ninfo() { echo "    $*"; }\nok() { echo "ok $*"; }\n'
            'die() { echo "die: $*" >&2; exit 1; }\nrun() { "$@"; }\n'
            + source[start:end]
            + "\ninstall_maintenance_launchd\n",
            encoding="utf-8",
        )
        completed = run(
            ["bash", str(script)],
            env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        return agent

    def test_it_runs_on_a_calendar_rather_than_an_interval(self, tmp_path: Path):
        """``StartInterval`` drifts, and a job that drifts eventually runs in the middle of the
        evening — during the one activity this machine exists for."""
        plist = self.write_plist(tmp_path).read_text(encoding="utf-8")
        assert "<key>StartCalendarInterval</key>" in plist
        assert "<key>StartInterval</key>" not in plist
        assert "<key>Hour</key><integer>4</integer>" in plist
        assert "<key>Minute</key><integer>17</integer>" in plist

    def test_it_does_not_run_at_load(self, tmp_path: Path):
        """Otherwise every login — and every re-run of the installer — downloads a video and rsyncs
        the whole library."""
        plist = self.write_plist(tmp_path).read_text(encoding="utf-8")
        assert "<key>RunAtLoad</key><false/>" in plist

    def test_it_gets_a_usable_path_and_its_own_log(self, tmp_path: Path):
        plist = self.write_plist(tmp_path).read_text(encoding="utf-8")
        assert "/opt/homebrew/bin:/usr/local/bin" in plist, "ffmpeg lives in one of these"
        assert "log/maintain.log" in plist
        assert "LowPriorityIO" in plist

    def test_it_waits_out_the_same_bootout_race_as_the_service(self, tmp_path: Path):
        """The bug measured in Phase 4.5 applies to every agent, not just the service one: bootout
        returns 0 before the unload finishes, and the bootstrap that follows fails — leaving nothing
        scheduled, silently."""
        source = code(INSTALL)
        start = source.index("install_maintenance_launchd() {")
        end = source.index("\n}\n", start) + 3
        body = source[start:end]
        assert 'launchctl print "gui/$(id -u)/$LAUNCHD_MAINT_LABEL"' in body
        assert body.count("launchctl print") >= 2, "must poll, not bootout-and-hope"
        assert "did not unload" in body


class TestTheConfigTemplate:
    """What a fresh install leaves in ``tunebox.env``. The backup destination is the one thing in
    this project that cannot be discovered by looking, so the file has to ask for it by name."""

    def test_it_names_the_backup_destination_and_says_why(self):
        source = INSTALL.read_text(encoding="utf-8")
        assert "TUNEBOX_BACKUP_DEST=" in source
        assert "only irreplaceable state" in source

    def test_it_offers_both_alert_channels(self):
        source = INSTALL.read_text(encoding="utf-8")
        assert "TUNEBOX_ALERT_COMMAND=" in source
        assert "TUNEBOX_ALERT_WEBHOOK=" in source

    def test_the_summary_tells_the_operator_the_one_manual_step(self, tmp_path: Path):
        """A backup nobody configured is the failure mode that matters most, and the end of a
        successful install is the only moment anybody is definitely reading."""
        source = INSTALL.read_text(encoding="utf-8")
        assert "set TUNEBOX_BACKUP_DEST in" in source
        assert "tunebox exposure" in source


# --------------------------------------------------------------- the owntone.conf editor

#: Shaped like the real thing, including the three details that matter: tab indentation, a
#: *commented-out* ``admin_password``, a value spanning several lines, and the key ``name`` in two
#: different sections.
SAMPLE_CONF = """\
# OwnTone configuration
general {
\tuid = "owntone"
#\tadmin_password = "unsafe"
\ttrusted_networks = { "lan" }
}

library {
\tname = "My Music on %h"
\tdirectories = {
\t\t"/srv/music",
\t\t"/srv/more"
\t}
\tpodcasts = { "/podcasts" }
}

audio {
\tname = "Computer"
}
"""


def apply_conf(tmp_path: Path, conf_text: str, section: str, key: str, value: str) -> str:
    """Call ``apply_conf`` out of the script, without running the rest of it."""
    source = OWNTONE.read_text(encoding="utf-8")
    start = source.index("apply_conf() {")
    end = source.index("\n}\n", start) + 3
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "set -euo pipefail\n"
        'die() { echo "die: $*" >&2; exit 1; }\n'
        + source[start:end]
        + '\nresult="$(apply_conf "$1" "$2" "$3" "$4")"\ncat "$result"\nrm -f "$result"\n',
        encoding="utf-8",
    )
    conf = tmp_path / "owntone.conf"
    conf.write_text(conf_text, encoding="utf-8")
    completed = run(["bash", str(harness), str(conf), section, key, value])
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


class TestOwnToneConf:
    def test_it_replaces_a_setting_in_place(self, tmp_path: Path):
        out = apply_conf(tmp_path, SAMPLE_CONF, "general", "trusted_networks", '{ "localhost" }')
        assert '\ttrusted_networks = { "localhost" }' in out
        assert '"lan"' not in out

    def test_it_uncomments_a_disabled_setting(self, tmp_path: Path):
        """OwnTone ships ``admin_password`` commented out. Adding a second, live copy would leave the
        file with two contradictory-looking lines."""
        out = apply_conf(tmp_path, SAMPLE_CONF, "general", "admin_password", '"s3cr3t"')
        assert '\tadmin_password = "s3cr3t"' in out
        assert "#\tadmin_password" not in out
        assert out.count("admin_password") == 1

    def test_it_keeps_the_file_indentation(self, tmp_path: Path):
        """Cosmetic on its own, but this is a file people hand-edit afterwards, and a flush-left line
        in the middle of a tab-indented block reads like damage."""
        out = apply_conf(tmp_path, SAMPLE_CONF, "general", "admin_password", '"x"')
        assert "\nadmin_password" not in out

    def test_it_replaces_a_value_that_spans_lines(self, tmp_path: Path):
        """The bug this was written to fix: replacing ``directories = {`` emitted the new value and
        left the old entries and their closing brace behind, so the config no longer parsed."""
        out = apply_conf(tmp_path, SAMPLE_CONF, "library", "directories", '{ "/music" }')
        assert '\tdirectories = { "/music" }' in out
        assert "/srv/music" not in out
        assert "/srv/more" not in out
        assert out.count("{") == out.count("}")

    def test_it_edits_only_the_named_section(self, tmp_path: Path):
        """``name`` exists in both ``library`` and ``audio``; a global substitution would rename the
        wrong one, and the symptom would be a library that looks fine and a speaker that does not."""
        out = apply_conf(tmp_path, SAMPLE_CONF, "audio", "name", '"Soundbar"')
        assert '\tname = "My Music on %h"' in out
        assert '\tname = "Soundbar"' in out

    def test_it_adds_a_missing_key(self, tmp_path: Path):
        out = apply_conf(tmp_path, SAMPLE_CONF, "library", "follow_symlinks", "true")
        assert "\tfollow_symlinks = true" in out
        assert out.count("{") == out.count("}")

    def test_it_adds_a_missing_section(self, tmp_path: Path):
        out = apply_conf(tmp_path, SAMPLE_CONF, "airplay_shared", "nickname", '"x"')
        assert "airplay_shared {" in out
        assert out.count("{") == out.count("}")

    def test_it_is_idempotent(self, tmp_path: Path):
        """The installer is re-run to upgrade, so this runs again too."""
        once = apply_conf(tmp_path, SAMPLE_CONF, "general", "trusted_networks", '{ "localhost" }')
        twice = apply_conf(tmp_path, once, "general", "trusted_networks", '{ "localhost" }')
        assert once == twice

    def test_a_multi_line_value_does_not_end_the_section_early(self, tmp_path: Path):
        """Belt and braces on the depth tracking: the key after a multi-line value must still be
        found in place rather than appended to a duplicate section at the end."""
        out = apply_conf(tmp_path, SAMPLE_CONF, "library", "podcasts", '{ "/p" }')
        assert out.count("library {") == 1
        assert '\tpodcasts = { "/p" }' in out

    def test_it_enables_the_queue_setting_shipped_commented_out(self, tmp_path: Path):
        """``clear_queue_on_stop_disable`` is in the shipped file already, disabled, and *after* the
        multi-line ``directories`` value — so this exercises the uncomment path and the depth tracking
        at once. A second live copy of the key would leave OwnTone reading whichever it parsed last.
        """
        conf = SAMPLE_CONF.replace(
            '\tpodcasts = { "/podcasts" }',
            '\tpodcasts = { "/podcasts" }\n#\tclear_queue_on_stop_disable = false',
        )
        out = apply_conf(tmp_path, conf, "library", "clear_queue_on_stop_disable", "true")
        assert "\tclear_queue_on_stop_disable = true" in out
        assert out.count("clear_queue_on_stop_disable") == 1
        assert out.count("library {") == 1


class TestOwnToneSetup:
    def test_it_refuses_to_run_before_the_installer(self, tmp_path: Path):
        completed = run(
            [str(OWNTONE), "--dry-run"],
            env={**os.environ, "HOME": str(tmp_path), "TUNEBOX_ENV_FILE": str(tmp_path / "none")},
        )
        assert completed.returncode != 0
        assert "run deploy/install.sh first" in completed.stderr

    def test_it_writes_the_output_id_not_just_the_name(self):
        """An id is stable; a name is what somebody renames in the Google Home app. The name is
        written too, for whoever reads the file next."""
        source = code(OWNTONE)
        assert "set_env TUNEBOX_OUTPUT_ID" in source
        assert "set_env TUNEBOX_OUTPUT " in source

    def test_it_backs_up_the_config_before_editing(self):
        source = code(OWNTONE)
        assert "tunebox-$(date" in source

    def test_it_closes_the_trusted_networks_hole(self):
        """Measured in PLAN.md 3.2: OwnTone ships ``trusted_networks = { "lan" }``, which skips its
        own password for every device on the home network."""
        source = code(OWNTONE)
        assert 'trusted_networks:{ \\"localhost\\" }' in source
        assert "admin_password" in source

    def test_it_stops_the_queue_emptying_itself_when_the_music_ends(self):
        """OwnTone's default is iTunes': stopping playback — which is what reaching the end of the
        queue does — empties the queue. On the remote that is the list of songs disappearing by itself
        at the exact moment somebody might want to replay it, so the queue stays until it is cleared
        on purpose.

        ``library`` and not ``mpd``: 29.x moved the setting and warns about the old section by name,
        and a deprecated spelling is one release from being ignored without a word.
        """
        source = code(OWNTONE)
        assert "library:clear_queue_on_stop_disable:true" in source
        assert "mpd:clear_queue_on_stop_disable" not in source

    def test_it_needs_no_jq(self):
        """A fresh Raspberry Pi OS has python3 and does not have jq, and asking an operator to
        install a JSON parser to finish an install is how a deployment stalls."""
        assert "jq" not in code(OWNTONE)

    def test_it_waits_for_the_speaker_to_appear(self):
        """A Cast device takes a few seconds to turn up in mDNS after a restart, so asking once would
        report "no speaker" on a perfectly working system."""
        source = code(OWNTONE)
        assert "for _ in 1 2 3" in source
        assert "sleep 2" in source


# ------------------------------------------------------- what gets written into tunebox.env


def discover(tmp_path: Path, outputs: str) -> tuple[subprocess.CompletedProcess, Path]:
    """Run ``owntone-setup.sh --discover`` against a stubbed player, and hand back the env file.

    ``curl`` is shimmed rather than a real socket opened: the assertion is about what the script
    writes, and the JSON is the only thing it needs from the daemon.

    ``systemctl`` is shimmed for a reason that is not cosmetic. The macOS branch of the restart step
    runs ``launchctl print gui/$(id -u)/io.tunebox.serve``, which on a developer's own machine
    matches the *real* loaded agent and would kickstart the service the developer is using. Making
    the systemd branch match first keeps the test off it.
    """
    env_file = tmp_path / "tunebox.env"
    tmp_path.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        f"TUNEBOX_MUSIC_ROOT={tmp_path / 'music'}\nTUNEBOX_PLAYER_URL=http://127.0.0.1:3689\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "shims"
    bin_dir.mkdir(exist_ok=True)
    shim(bin_dir, "curl", f"cat <<'JSON'\n{outputs}\nJSON")
    shim(bin_dir, "systemctl", "exit 0")
    shim(bin_dir, "sudo", 'exec "$@"')
    completed = run(
        [str(OWNTONE), "--discover"],
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "TUNEBOX_ENV_FILE": str(env_file),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        },
    )
    return completed, env_file


def sourced(env_file: Path, key: str) -> subprocess.CompletedProcess:
    """Read one key back the way the service actually reads it: by sourcing the file under ``set -e``.

    This is the whole point. The service wrapper written by ``install.sh`` does ``set -a; . tunebox.env``
    with ``set -euo pipefail`` in force, so a value is shell source code and not a string.
    """
    return run(
        [
            "bash",
            "-c",
            f'set -euo pipefail; set -a; . "{env_file}"; set +a; printf "%s" "${key}"',
        ]
    )


class TestTheEnvFileStaysSourceable:
    """A speaker is called "Living Room speaker", with spaces, because that is what the Google Home
    app suggests. Written bare, ``TUNEBOX_OUTPUT=Living Room speaker`` makes bash run ``Room`` with
    an argument; the source fails, and because the wrapper runs under ``set -e`` it exits *before*
    exec'ing tunebox. The service then never starts and the supervisor restart-loops it — a total
    outage produced by a space, whose only symptom is ``Room: command not found``.
    """

    ONE_CAST = (
        '{"outputs":[{"id":"2080974779","name":"%s","type":"Chromecast",'
        '"selected":true,"volume":20}]}'
    )

    def test_a_name_with_spaces_survives_being_sourced(self, tmp_path: Path):
        completed, env_file = discover(tmp_path, self.ONE_CAST % "Living Room speaker")
        assert completed.returncode == 0, completed.stderr
        assert sourced(env_file, "TUNEBOX_OUTPUT").stdout == "Living Room speaker"

    def test_a_name_with_an_apostrophe_survives_being_sourced(self, tmp_path: Path):
        """``Rob's Kitchen`` is the case that a naive quoting fix reintroduces, because the fix and
        the hazard are the same character."""
        completed, env_file = discover(tmp_path, self.ONE_CAST % "Rob's Kitchen")
        assert completed.returncode == 0, completed.stderr
        assert sourced(env_file, "TUNEBOX_OUTPUT").stdout == "Rob's Kitchen"

    def test_sourcing_it_is_silent(self, tmp_path: Path):
        """The original bug announced itself only as a line number on stderr. Nothing should be
        written there at all."""
        _, env_file = discover(tmp_path, self.ONE_CAST % "Living Room speaker")
        assert sourced(env_file, "TUNEBOX_OUTPUT").stderr == ""

    def test_the_script_reads_back_what_it_wrote(self, tmp_path: Path):
        """``env_value`` is how the script finds the music root and the password it generated on an
        earlier run. Writing quotes without stripping them on read would point the library at
        ``'/srv/my music'`` — quotes included — and the fix for one bug would have created another.
        """
        env_file = tmp_path / "tunebox.env"
        env_file.write_text("TUNEBOX_MUSIC_ROOT='/srv/my music'\n", encoding="utf-8")
        bin_dir = tmp_path / "shims"
        bin_dir.mkdir()
        shim(bin_dir, "curl", f"cat <<'JSON'\n{self.ONE_CAST % 'Living Room speaker'}\nJSON")
        shim(bin_dir, "systemctl", "exit 0")
        shim(bin_dir, "sudo", 'exec "$@"')
        completed = run(
            [str(OWNTONE), "--discover"],
            env={
                **os.environ,
                "HOME": str(tmp_path),
                "TUNEBOX_ENV_FILE": str(env_file),
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            },
        )
        assert completed.returncode == 0, completed.stderr
        assert "library:   /srv/my music" in completed.stdout

    def test_the_commented_template_shows_the_quoted_form(self):
        """The placeholder in ``install.sh`` is what an operator copies when setting the speaker by
        hand, so it has to model quoting rather than demonstrate the bug."""
        assert "#TUNEBOX_OUTPUT='Living Room speaker'" in INSTALL.read_text(encoding="utf-8")

    @pytest.mark.parametrize("script", SED_READERS, ids=lambda p: p.name)
    def test_both_scripts_unquote_on_read(self, script: Path):
        """Both read this file with the same ``sed``, so both need the same inverse. A file written
        by one and read by the other is the normal case, not an edge case.

        ``start.sh`` is deliberately not in this list: it ``source``s the env file the way the service
        wrapper does, so the shell does the unquoting and there is no inverse to get wrong.
        """
        assert "unquote" in code(script)

    def test_a_bare_value_still_reads(self, tmp_path: Path):
        """Files written before quoting existed are on real machines already, and an upgrade that
        could not read them would relocate the library or lose the token."""
        env_file = tmp_path / "tunebox.env"
        env_file.write_text("TUNEBOX_MUSIC_ROOT=/srv/music\n", encoding="utf-8")
        bin_dir = tmp_path / "shims"
        bin_dir.mkdir()
        shim(bin_dir, "curl", "exit 7")
        completed = run(
            [str(OWNTONE), "--discover"],
            env={
                **os.environ,
                "HOME": str(tmp_path),
                "TUNEBOX_ENV_FILE": str(env_file),
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            },
        )
        # It fails, because the stub player is unreachable — but on the player, never on the path,
        # which proves the bare value was read intact.
        assert "not answering" in completed.stderr
        assert "no TUNEBOX_MUSIC_ROOT" not in completed.stderr


# ------------------------------------------------------- supervising the player daemon


class TestSupervisingOwnTone:
    """The daemon has to be *supervised*, not merely started once.

    The bug this covers was found on the macOS host: ``restart_owntone`` knew ``systemctl`` and
    ``brew services`` only, so a source build — which is the documented macOS route, and which
    installs to a ``--prefix`` that is on no ``PATH`` — fell through to "restart it yourself". Nothing
    then supervised the player at all: it died with the terminal that started it and did not come back
    after a reboot, and the symptom is a remote whose buttons all work while no sound comes out.

    Tested here rather than by hand because the interesting properties are in the plist and in the
    order of the launchctl calls, and a ``--dry-run`` cannot show the contents of a file it does not
    write.
    """

    @staticmethod
    def harness(tmp_path: Path, *, already_loaded: bool = False) -> tuple[Path, Path, Path]:
        source = code(OWNTONE)
        start = source.index("install_owntone_agent() {")
        end = source.index("\nrestart_owntone() {", start)

        prefix = tmp_path / "owntone_data"
        (prefix / "usr" / "sbin").mkdir(parents=True)
        binary = prefix / "usr" / "sbin" / "owntone"
        binary.write_text("#!/usr/bin/env bash\nsleep 1\n", encoding="utf-8")
        binary.chmod(0o755)
        (prefix / "etc").mkdir()
        conf = prefix / "etc" / "owntone.conf"
        conf.write_text('general {\n\tuid = "someone"\n}\n', encoding="utf-8")

        bin_dir = tmp_path / "shims"
        bin_dir.mkdir()
        state = tmp_path / "state"
        state.write_text("loaded\n" if already_loaded else "gone\n", encoding="utf-8")
        calls = tmp_path / "calls"
        calls.write_text("", encoding="utf-8")
        shim(
            bin_dir,
            "launchctl",
            f"""
STATE={state}
CALLS={calls}
echo "$1" >>"$CALLS"
case "$1" in
  print)
    read -r value <"$STATE" || value=""
    case "$value" in
      loaded) exit 0 ;;
      draining) echo gone >"$STATE"; exit 0 ;;
      *) exit 1 ;;
    esac
    ;;
  bootout) echo draining >"$STATE"; exit 0 ;;
  bootstrap)
    read -r value <"$STATE" || value=""
    if [ "$value" != gone ]; then
      echo "Bootstrap failed: 5: Input/output error" >&2
      exit 5
    fi
    echo loaded >"$STATE"
    exit 0
    ;;
esac
exit 0
""",
        )

        agent = tmp_path / "Library" / "LaunchAgents" / "io.tunebox.owntone.plist"
        script = tmp_path / "harness.sh"
        script.write_text(
            "set -euo pipefail\n"
            "DRY_RUN=0\n"
            'DIM=""\n'
            'N=""\n'
            f'CONF="{conf}"\n'
            f'OWNTONE_BIN="{binary}"\n'
            "LAUNCHD_OWNTONE_LABEL=io.tunebox.owntone\n"
            f'OWNTONE_AGENT="{agent}"\n'
            'say() { echo "==> $*"; }\n'
            'info() { echo "    $*"; }\n'
            'ok() { echo "ok $*"; }\n'
            'warn() { echo "warn: $*" >&2; }\n'
            'die() { echo "die: $*" >&2; exit 1; }\n'
            "find_conf() { printf '%s' \"$CONF\"; }\n"
            + source[start:end]
            + "\ninstall_owntone_agent\n",
            encoding="utf-8",
        )
        return script, agent, calls

    @staticmethod
    def shimmed(tmp_path: Path) -> dict[str, str]:
        return {**os.environ, "PATH": f"{tmp_path / 'shims'}{os.pathsep}{os.environ['PATH']}"}

    def test_it_writes_and_loads_the_agent(self, tmp_path: Path):
        script, agent, calls = self.harness(tmp_path)
        completed = run(["bash", str(script)], env=self.shimmed(tmp_path))
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert agent.is_file()
        assert "bootstrap" in calls.read_text(encoding="utf-8")

    def test_the_daemon_runs_in_the_foreground(self, tmp_path: Path):
        """``-f`` is load-bearing, not a style choice: launchd supervises the process it spawned, so a
        daemon that forked and exited would be declared dead and restarted forever."""
        script, agent, _ = self.harness(tmp_path)
        run(["bash", str(script)], env=self.shimmed(tmp_path))
        assert "<string>-f</string>" in agent.read_text(encoding="utf-8")

    def test_it_keeps_the_daemon_alive_and_starts_it_at_login(self, tmp_path: Path):
        """The whole point of the change. Without both keys this is just a slower way of starting it
        by hand."""
        script, agent, _ = self.harness(tmp_path)
        run(["bash", str(script)], env=self.shimmed(tmp_path))
        plist = agent.read_text(encoding="utf-8")
        assert "<key>KeepAlive</key><true/>" in plist
        assert "<key>RunAtLoad</key><true/>" in plist

    def test_it_names_the_conf_explicitly(self, tmp_path: Path):
        """A source build's compiled-in default conf path is whatever ``--prefix`` was, and the agent
        must not depend on guessing it right."""
        script, agent, _ = self.harness(tmp_path)
        run(["bash", str(script)], env=self.shimmed(tmp_path))
        assert "owntone.conf" in agent.read_text(encoding="utf-8")

    def test_a_reload_waits_for_the_unload(self, tmp_path: Path):
        """The same asynchronous ``bootout`` that bit the installer: without the wait, ``bootstrap``
        fails and the daemon is left not loaded at all."""
        script, _, calls = self.harness(tmp_path, already_loaded=True)
        completed = run(["bash", str(script)], env=self.shimmed(tmp_path))
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "Bootstrap failed" not in completed.stderr
        sequence = calls.read_text(encoding="utf-8").split()
        assert "bootout" in sequence
        assert sequence.index("bootout") < sequence.index("bootstrap")
        # Polled between the two, rather than hoping a fixed sleep was enough.
        assert "print" in sequence[sequence.index("bootout") : sequence.index("bootstrap")]

    def test_it_stops_an_unsupervised_daemon_first(self, tmp_path: Path):
        """Port 3689 is held by whatever somebody started by hand, and launchd would otherwise spawn a
        second copy that exits on the bind and is restarted forever."""
        script, _, _ = self.harness(tmp_path)
        pidfile = tmp_path / "owntone_data" / "var" / "run" / "owntone.pid"
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        # A process that really is alive and really is ours to signal.
        victim = subprocess.Popen(["sleep", "120"])
        try:
            pidfile.write_text(f"{victim.pid}\n", encoding="utf-8")
            completed = run(["bash", str(script)], env=self.shimmed(tmp_path))
            assert completed.returncode == 0, completed.stdout + completed.stderr
            assert victim.wait(timeout=30) is not None
            assert not pidfile.exists()
        finally:
            if victim.poll() is None:
                victim.kill()
                victim.wait(timeout=10)


class TestFindingASourceBuild:
    """Discovery is what removes ``--conf`` from the command somebody has to remember.

    ``find_conf`` used to know five fixed paths, none of which is where a ``--prefix`` build puts its
    config — so on the macOS host the script had to be told, every time.
    """

    @staticmethod
    def probe(tmp_path: Path, snippet: str) -> subprocess.CompletedProcess:
        prefix = tmp_path / "owntone_data"
        (prefix / "usr" / "sbin").mkdir(parents=True)
        binary = prefix / "usr" / "sbin" / "owntone"
        binary.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        (prefix / "etc").mkdir()
        (prefix / "etc" / "owntone.conf").write_text("general {\n}\n", encoding="utf-8")

        source = code(OWNTONE)
        start = source.index("find_owntone_bin() {")
        end = source.index("\napply_conf() {", start)
        script = tmp_path / "probe.sh"
        script.write_text(
            "set -euo pipefail\n"
            f'HOME="{tmp_path}"\n'
            'info() { :; }\n'
            'warn() { echo "warn: $*" >&2; }\n'
            + source[start:end]
            + f"\n{snippet}\n",
            encoding="utf-8",
        )
        return run(["bash", str(script)], env={**os.environ, "HOME": str(tmp_path)})

    def test_it_finds_a_binary_off_path(self, tmp_path: Path):
        completed = self.probe(tmp_path, 'printf "%s" "$OWNTONE_BIN"')
        assert completed.stdout == str(tmp_path / "owntone_data" / "usr" / "sbin" / "owntone")

    def test_it_finds_the_conf_beside_the_prefix(self, tmp_path: Path):
        """Derived from the binary rather than hard-coded, so any ``--prefix`` works."""
        completed = self.probe(tmp_path, "find_conf")
        assert completed.stdout == str(tmp_path / "owntone_data" / "etc" / "owntone.conf")
