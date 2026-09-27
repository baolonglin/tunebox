"""``tunebox update`` — the canary-before-and-after decision table, and the rollback.

This is the file that would have caught the bug the naive design has. The obvious implementation is
*update, canary, roll back on failure*, and it does the wrong thing in the one situation that matters
most: when extraction is **already** broken, the canary fails after the update too, so the obvious
logic reverts the release that was the fix. Every row of the table in ``tunebox/ops/update.py`` has a
test here, and :meth:`TestAlreadyBroken.test_it_does_not_roll_back_a_fix_away` is that one.

Nothing installs anything. ``FakePip`` records the argv it was given and swaps a version number, so
"pip downgraded yt-dlp to the known-good version" is an assertion rather than an act of faith — and
the *rollback path*, which is the part of a system like this that is normally never executed until
the night it has to work, is exercised on every run of the suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tunebox.config import Config
from tunebox.ops import update as update_module
from tunebox.ops.canary import CanaryResult
from tunebox.ops.state import OpsState
from tunebox.ops.update import (
    FAILED,
    KEPT_BROKEN,
    REAPPLIED,
    RECOVERED,
    REFUSED,
    ROLLED_BACK,
    SKIPPED_KNOWN_BAD,
    UNCHANGED,
    UPDATED,
    reapply_known_good,
    update_ytdlp,
)

OLD = "2026.8.19"
NEW = "2026.9.30"


def passing(version: str = OLD) -> CanaryResult:
    return CanaryResult(ok=True, url="u", stage="verify", ytdlp_version=version, size=99999)


def failing(version: str = OLD, error: str = "HTTP Error 403: Forbidden") -> CanaryResult:
    return CanaryResult(ok=False, url="u", stage="fetch", ytdlp_version=version, error=error)


class FakePip:
    """Records pip invocations and moves a version number, so nothing is installed."""

    def __init__(
        self,
        *,
        versions: list[str],
        fail_on: str | None = None,
        dry_run_version: str | None = None,
    ) -> None:
        #: Popped in order as each ``install`` runs; the last one sticks.
        self.versions = versions
        self.fail_on = fail_on
        #: What ``pip install --dry-run`` should claim it *would* install.
        self.dry_run_version = dry_run_version
        self.calls: list[list[str]] = []
        self.current = versions[0] if versions else OLD

    def __call__(self, argv):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        if self.fail_on and self.fail_on in joined:
            return 1, "ERROR: Could not find a version that satisfies the requirement"
        if "--dry-run" in argv:
            # Real pip prints a "Would install <pkg>-<version>" summary and changes nothing.
            version = self.dry_run_version or self.current
            return 0, f"Would install yt-dlp-{version}"
        if "==" in joined:
            # A pinned install: that exact version is what is now installed.
            self.current = joined.split("==", 1)[1].split()[0]
        elif len(self.versions) > 1:
            self.versions.pop(0)
            self.current = self.versions[0]
        return 0, "Successfully installed yt-dlp"

    @property
    def installs(self) -> list[list[str]]:
        """Calls that would really have changed the environment — dry runs excluded."""
        return [call for call in self.calls if "--dry-run" not in call]

    @property
    def pinned(self) -> list[str]:
        return [
            argument.split("==", 1)[1]
            for call in self.calls
            for argument in call
            if argument.startswith("yt-dlp==")
        ]


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(music_root=tmp_path / "music")


@pytest.fixture(autouse=True)
def in_a_venv(monkeypatch):
    """The suite runs inside a venv anyway; asserted explicitly so the refusal test is the only one
    that has to think about it."""
    monkeypatch.setattr(update_module, "in_virtualenv", lambda: True)


def versions(monkeypatch, pip: FakePip) -> None:
    """Make ``ytdlp_version`` report whatever the fake pip last installed."""
    monkeypatch.setattr(update_module, "ytdlp_version", lambda config, argv=None: pip.current)


class TestTheHappyPath:
    def test_a_good_update_is_kept_and_recorded(self, config: Config, monkeypatch):
        pip = FakePip(versions=[OLD, NEW])
        versions(monkeypatch, pip)
        result = update_ytdlp(config, pip=pip, canary=lambda cfg: passing(pip.current))

        assert result.ok
        assert result.action == UPDATED
        assert (result.before, result.after) == (OLD, NEW)
        assert pip.pinned == [], "a successful update never pins a version"
        assert OpsState(config.ops_state_path).section("ytdlp")["known_good"] == NEW

    def test_nothing_to_do_is_not_an_event(self, config: Config, monkeypatch):
        pip = FakePip(versions=[OLD])
        versions(monkeypatch, pip)
        result = update_ytdlp(config, pip=pip, canary=lambda cfg: passing())
        assert result.ok
        assert result.action == UNCHANGED
        assert "is current" in result.summary

    def test_the_canary_runs_twice_only_when_something_changed(self, config: Config, monkeypatch):
        """Two canaries cost two downloads of a 19-second video, which is nothing — but a second one
        after an update that installed nothing would be pure waste, every night."""
        pip = FakePip(versions=[OLD])
        versions(monkeypatch, pip)
        runs: list[str] = []

        def canary(cfg):
            runs.append(pip.current)
            return passing(pip.current)

        update_ytdlp(config, pip=pip, canary=canary)
        assert runs == [OLD]

    def test_the_channel_chooses_the_pip_flags(self, config: Config, monkeypatch):
        """yt-dlp publishes nightly builds to PyPI as pre-releases, so ``--pre`` is the whole
        difference — no second index, no URL, no GitHub token."""
        from dataclasses import replace

        pip = FakePip(versions=[OLD])
        versions(monkeypatch, pip)
        update_ytdlp(config, pip=pip, canary=lambda cfg: passing())
        assert "--pre" not in pip.calls[0]

        nightly = FakePip(versions=[OLD])
        versions(monkeypatch, nightly)
        update_ytdlp(
            replace(config, ytdlp_channel="nightly"),
            pip=nightly,
            canary=lambda cfg: passing(),
        )
        assert "--pre" in nightly.calls[0]

    def test_a_dry_run_installs_nothing_but_still_canaries(self, config: Config, monkeypatch):
        pip = FakePip(versions=[OLD, NEW])
        versions(monkeypatch, pip)
        result = update_ytdlp(config, pip=pip, canary=lambda cfg: passing(), dry_run=True)
        assert pip.calls == []
        assert result.canary_before is not None and result.canary_before.ok
        assert "dry run" in " ".join(result.messages)


class TestTheRegression:
    """Row two: it worked before the update and does not now."""

    def test_it_rolls_back_to_the_known_good_version(self, config: Config, monkeypatch):
        pip = FakePip(versions=[OLD, NEW])
        versions(monkeypatch, pip)

        def canary(cfg):
            return passing(OLD) if pip.current == OLD else failing(NEW)

        result = update_ytdlp(config, pip=pip, canary=canary)

        assert result.action == ROLLED_BACK
        assert result.after == NEW
        assert pip.pinned == [OLD], "the rollback must pin the exact previous version"
        assert pip.current == OLD

    def test_the_rollback_is_verified_rather_than_assumed(self, config: Config, monkeypatch):
        """The question an operator asks first is "did the rollback work?", and nothing else can
        answer it. So a third canary runs after the downgrade."""
        pip = FakePip(versions=[OLD, NEW])
        versions(monkeypatch, pip)
        seen: list[str] = []

        def canary(cfg):
            seen.append(pip.current)
            return passing(OLD) if pip.current == OLD else failing(NEW)

        result = update_ytdlp(config, pip=pip, canary=canary)
        assert seen == [OLD, NEW, OLD]
        assert result.canary_restored is not None and result.canary_restored.ok
        assert result.ok, "a verified rollback is a successful outcome, not a failure"
        assert "rollback was verified" in result.summary

    def test_a_rollback_that_does_not_help_is_not_reported_as_fine(
        self, config: Config, monkeypatch
    ):
        """The nastiest case: the update was a regression *and* going back does not restore service
        (YouTube changed something in the same window). That must reach a human."""
        pip = FakePip(versions=[OLD, NEW])
        versions(monkeypatch, pip)
        calls = {"n": 0}

        def canary(cfg):
            calls["n"] += 1
            if calls["n"] == 1:
                return passing(OLD)
            return failing(pip.current)

        result = update_ytdlp(config, pip=pip, canary=canary)
        assert result.action == ROLLED_BACK
        assert not result.ok
        assert "needs a human" in (result.error or "")
        assert "DID NOT RESTORE" in result.summary

    def test_the_bad_version_is_remembered(self, config: Config, monkeypatch):
        """Without this, a bad release is installed, canaried, rolled back — and installed again
        tomorrow, forever, with an alert every night."""
        pip = FakePip(versions=[OLD, NEW])
        versions(monkeypatch, pip)
        update_ytdlp(
            config,
            pip=pip,
            canary=lambda cfg: passing(OLD) if pip.current == OLD else failing(NEW),
        )
        blocked = OpsState(config.ops_state_path).section("ytdlp")["blocked"]
        assert NEW in blocked
        assert "403" in blocked[NEW]

    def test_a_known_bad_version_is_reverted_without_downloading_again(
        self, config: Config, monkeypatch
    ):
        pip = FakePip(versions=[OLD, NEW])
        versions(monkeypatch, pip)
        OpsState(config.ops_state_path).save_section(
            "ytdlp", {"known_good": OLD, "blocked": {NEW: "canary failed at fetch"}}
        )
        canaries: list[str] = []

        def canary(cfg):
            canaries.append(pip.current)
            return passing(OLD)

        result = update_ytdlp(config, pip=pip, canary=canary)
        assert result.action == SKIPPED_KNOWN_BAD
        assert canaries == [OLD], "no canary for a version already known to be bad"
        assert pip.pinned == [OLD]

    def test_a_known_bad_version_is_not_downloaded_again(self, config: Config, monkeypatch):
        """Asked before acting: pip's ``--dry-run`` says what an upgrade *would* install, so the
        release this host has already rejected is never fetched a second time — and is never briefly
        installed, which is what a nightly job killed during the revert used to leave behind."""
        pip = FakePip(versions=[OLD], dry_run_version=NEW)
        versions(monkeypatch, pip)
        OpsState(config.ops_state_path).save_section(
            "ytdlp", {"known_good": OLD, "blocked": {NEW: "canary failed at fetch"}}
        )
        result = update_ytdlp(config, pip=pip, canary=lambda cfg: passing(OLD))

        assert result.action == SKIPPED_KNOWN_BAD
        assert result.ok, "nothing is broken; the newest release is simply not usable here"
        assert pip.installs == [], "no install, and therefore nothing to revert"
        assert any("not downloading it again" in message for message in result.messages)

    def test_a_pip_too_old_for_dry_run_still_recovers(self, config: Config, monkeypatch):
        """``--dry-run`` arrived in pip 22.2 and a Raspberry Pi OS older than that is plausible. When
        it is refused, the install-then-revert path still catches the bad version after the fact."""
        pip = FakePip(versions=[OLD, NEW], fail_on="--dry-run")
        versions(monkeypatch, pip)
        OpsState(config.ops_state_path).save_section(
            "ytdlp", {"known_good": OLD, "blocked": {NEW: "canary failed at fetch"}}
        )
        result = update_ytdlp(config, pip=pip, canary=lambda cfg: passing(OLD))
        assert result.action == SKIPPED_KNOWN_BAD
        assert pip.pinned == [OLD], "reverted the expensive way"

    def test_it_stays_broken_out_loud_when_the_only_newer_release_is_known_bad(
        self, config: Config, monkeypatch
    ):
        """Reverting cleanly is not the same as being healthy. If extraction is already broken and
        the only newer version is one that failed its canary, nothing can be fixed tonight and the
        report has to say so rather than report a tidy revert."""
        pip = FakePip(versions=[OLD], dry_run_version=NEW)
        versions(monkeypatch, pip)
        OpsState(config.ops_state_path).save_section(
            "ytdlp", {"known_good": OLD, "blocked": {NEW: "canary failed at fetch"}}
        )
        result = update_ytdlp(config, pip=pip, canary=lambda cfg: failing(OLD))
        assert result.action == SKIPPED_KNOWN_BAD
        assert not result.ok

    def test_with_nothing_to_roll_back_to_it_names_the_command(self):
        """A host where no version has ever passed a canary has no rollback destination. Saying
        "rolled back" there would be a lie, so the error names the one command that fixes it."""
        pip = FakePip(versions=[NEW])
        error = update_module._install_version(pip, None)
        assert error is not None
        assert "no known-good version" in error
        assert "pip install 'yt-dlp==" in error
        assert pip.calls == []

    def test_an_unreadable_version_cannot_be_called_an_update(
        self, config: Config, monkeypatch
    ):
        """If ``yt-dlp --version`` does not answer, "did anything change?" is unanswerable — so the
        run reports ``unchanged`` and still fails on the canary, rather than inventing a version
        number and a rollback to go with it."""
        pip = FakePip(versions=[NEW])
        monkeypatch.setattr(update_module, "ytdlp_version", lambda config, argv=None: None)
        result = update_ytdlp(config, pip=pip, canary=lambda cfg: failing(NEW))
        assert result.action == UNCHANGED
        assert not result.ok, "the canary failed, so this is not a healthy run"
        assert pip.pinned == []


class TestAlreadyBroken:
    """Rows three and four: extraction was broken *before* the update."""

    def test_an_update_that_fixes_extraction_is_reported_as_a_recovery(
        self, config: Config, monkeypatch
    ):
        pip = FakePip(versions=[OLD, NEW])
        versions(monkeypatch, pip)
        result = update_ytdlp(
            config,
            pip=pip,
            canary=lambda cfg: failing(OLD) if pip.current == OLD else passing(NEW),
        )
        assert result.action == RECOVERED
        assert result.ok
        assert "fixed it" in result.summary
        assert pip.pinned == []

    def test_it_does_not_roll_back_a_fix_away(self, config: Config, monkeypatch):
        """The bug the obvious design has. If the canary fails both before and after, rolling back
        would restore a version that is *equally* broken and would throw away the newer extractor —
        which is the one yt-dlp's next release will be built on."""
        pip = FakePip(versions=[OLD, NEW])
        versions(monkeypatch, pip)
        result = update_ytdlp(config, pip=pip, canary=lambda cfg: failing(pip.current))

        assert result.action == KEPT_BROKEN
        assert not result.ok, "still broken, so it still alerts"
        assert pip.current == NEW
        assert pip.pinned == [], "nothing was rolled back"
        assert "equally broken" in " ".join(result.messages)

    def test_a_still_broken_version_is_not_recorded_as_known_good(
        self, config: Config, monkeypatch
    ):
        pip = FakePip(versions=[OLD, NEW])
        versions(monkeypatch, pip)
        update_ytdlp(config, pip=pip, canary=lambda cfg: failing(pip.current))
        assert "known_good" not in OpsState(config.ops_state_path).section("ytdlp")


class TestRefusals:
    def test_it_refuses_to_install_outside_a_virtualenv(self, config: Config, monkeypatch):
        """On Debian — which is what a Raspberry Pi runs — pip into the system interpreter is
        blocked by PEP 668, and forcing it is a way to break apt."""
        monkeypatch.setattr(update_module, "in_virtualenv", lambda: False)
        monkeypatch.setattr(update_module, "ytdlp_version", lambda config, argv=None: OLD)
        pip = FakePip(versions=[OLD])
        result = update_ytdlp(config, pip=pip)
        assert result.action == REFUSED
        assert not result.ok
        assert pip.calls == []
        assert "PEP 668" in (result.error or "")

    def test_allow_system_is_the_deliberate_way_to_say_so(self, config: Config, monkeypatch):
        monkeypatch.setattr(update_module, "in_virtualenv", lambda: False)
        pip = FakePip(versions=[OLD])
        versions(monkeypatch, pip)
        result = update_ytdlp(
            config, pip=pip, canary=lambda cfg: passing(), allow_system=True
        )
        assert result.action == UNCHANGED
        assert pip.calls

    def test_a_pip_failure_is_reported_not_raised(self, config: Config, monkeypatch):
        pip = FakePip(versions=[OLD], fail_on="--upgrade")
        versions(monkeypatch, pip)
        result = update_ytdlp(config, pip=pip, canary=lambda cfg: passing())
        assert result.action == FAILED
        assert "pip install failed" in (result.error or "")
        # It still reports the canary it managed to run, and says which half is broken: "the updater
        # cannot reach PyPI" and "music stopped working" are different problems, and an alert that
        # conflated them would send somebody to the wrong place.
        assert result.canary_before is not None and result.canary_before.ok
        assert any("still working" in message for message in result.messages)
        # And it is a *failure*, which it was not in the first version of this code: a host whose
        # updater silently never runs has lost the protection this whole phase is about, so it must
        # not exit 0 and must not leave the nightly job green.
        assert not result.ok

    def test_a_foreign_ytdlp_is_flagged(self, config: Config, monkeypatch):
        """``TUNEBOX_YTDLP`` can point anywhere. Updating this venv then succeeds while the yt-dlp
        that actually runs is untouched — a silent no-op is the worst outcome for a nightly job."""
        from dataclasses import replace

        pip = FakePip(versions=[OLD])
        versions(monkeypatch, pip)
        result = update_ytdlp(
            replace(config, ytdlp=("/usr/local/bin/yt-dlp",)),
            pip=pip,
            canary=lambda cfg: passing(),
        )
        assert any("pip cannot manage it" in message for message in result.messages)


class TestReapply:
    """What ``deploy/install.sh`` calls, so that re-running the installer cannot undo an update."""

    def test_it_reinstalls_the_known_good_version_after_an_installer_downgrade(
        self, config: Config, monkeypatch
    ):
        OpsState(config.ops_state_path).save_section("ytdlp", {"known_good": NEW})
        pip = FakePip(versions=[OLD])  # the installer's pinned install put OLD back
        versions(monkeypatch, pip)
        result = reapply_known_good(config, pip=pip)
        assert result.action == REAPPLIED
        assert pip.pinned == [NEW]
        assert result.ok

    def test_it_does_nothing_when_they_match(self, config: Config, monkeypatch):
        OpsState(config.ops_state_path).save_section("ytdlp", {"known_good": OLD})
        pip = FakePip(versions=[OLD])
        versions(monkeypatch, pip)
        result = reapply_known_good(config, pip=pip)
        assert result.action == UNCHANGED
        assert pip.calls == []

    def test_it_does_nothing_on_a_host_that_has_never_updated(self, config: Config, monkeypatch):
        pip = FakePip(versions=[OLD])
        versions(monkeypatch, pip)
        result = reapply_known_good(config, pip=pip)
        assert result.ok
        assert pip.calls == []
        assert "nothing recorded" in " ".join(result.messages)

    def test_it_needs_no_network(self, config: Config, monkeypatch):
        """No canary here: that version already passed one, and the installer must be able to finish
        on a host whose uplink is down."""
        OpsState(config.ops_state_path).save_section("ytdlp", {"known_good": NEW})
        pip = FakePip(versions=[OLD])
        versions(monkeypatch, pip)

        def explode(*args, **kwargs):  # pragma: no cover - must never be called
            raise AssertionError("reapply must not run a canary")

        monkeypatch.setattr(update_module, "run_canary", explode)
        assert reapply_known_good(config, pip=pip).ok


class TestSerialisation:
    def test_the_result_is_json_serialisable(self, config: Config, monkeypatch):
        pip = FakePip(versions=[OLD, NEW])
        versions(monkeypatch, pip)
        result = update_ytdlp(
            config, pip=pip, canary=lambda cfg: passing(OLD) if pip.current == OLD else failing(NEW)
        )
        payload = json.loads(json.dumps(result.as_dict()))
        assert payload["action"] == ROLLED_BACK
        assert payload["canary_after"]["ok"] is False
        assert payload["canary_restored"]["ok"] is True
