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
SCRIPTS = (INSTALL, OWNTONE)


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
        assert f"ReadWritePaths={tmp_path / 'music'}" in out
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
