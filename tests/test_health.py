"""The daily health check, the alerting that hangs off it, and the nightly job that runs both.

Three things are worth testing here and they are all about *behaviour under failure* rather than
about the checks themselves:

* **A check must not lie by omission.** An unmounted music volume presents as a perfectly writable,
  perfectly empty directory — so ingest keeps "working" and the library keeps "existing" while every
  track goes onto the wrong disk. :class:`TestDisk` and :class:`TestLibrary` are about that.

* **Alerting must not become noise.** An identical message every night for a week is a message that
  gets filtered, and the filter is still in place when something new breaks. :class:`TestThrottling`
  pins the state machine: alert on the transition, repeat at most once a day, and say something when
  it recovers.

* **One step failing must not cancel the others.** The backup is the step that protects the only
  irreplaceable thing in the system, and it must still run on a night when the updater threw.
  :class:`TestMaintenance` asserts that, and that a bad night produces exactly one alert.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

from tunebox.config import Config
from tunebox.ops import alerts as alerts_module
from tunebox.ops import exposure as exposure_module
from tunebox.ops import health as health_module
from tunebox.ops import maintain as maintain_module
from tunebox.ops.backup import BackupResult
from tunebox.ops.canary import CanaryResult
from tunebox.ops.report import FAIL, OK, UNKNOWN, WARN
from tunebox.ops.state import OpsState, append_jsonl, read_jsonl
from tunebox.ops.update import ROLLED_BACK, UPDATED, UpdateResult

from fakes import FakePlayer


def passing_canary() -> CanaryResult:
    return CanaryResult(ok=True, url="u", stage="verify", ytdlp_version="2026.8.19", size=99999)


def failing_canary() -> CanaryResult:
    return CanaryResult(
        ok=False, url="u", stage="fetch", ytdlp_version="2026.8.19",
        error="HTTP Error 403: Forbidden",
    )


@pytest.fixture
def config(tmp_path: Path) -> Config:
    root = tmp_path / "music"
    (root / "Kraftwerk" / "Singles").mkdir(parents=True)
    (root / "Kraftwerk" / "Singles" / "Autobahn.m4a").write_bytes(b"\x00" * 512)
    return Config(music_root=root, api_host="192.168.50.158")


def check(
    config: Config, *, acknowledge_backup: bool = True, **kwargs
) -> health_module.HealthReport:
    """A health check with the network-touching and host-touching parts stubbed by default.

    ``acknowledge_backup`` defaults to true so that every test here is about the check it names: an
    unconfigured backup destination is a *failure* (see :class:`TestBackupAge`), and it would
    otherwise mask the result of every other assertion in this file.
    """
    kwargs.setdefault("canary", lambda cfg: passing_canary())
    kwargs.setdefault("player", FakePlayer(selected=True))
    kwargs.setdefault("listener_run", lambda argv: (0, ""))
    if acknowledge_backup and not config.backup_dest:
        config = replace(config, backup_optional=True)
    return health_module.check_health(config, **kwargs)


def finding(report, name):
    for item in report.findings:
        if item.name == name:
            return item
    raise AssertionError(f"no {name!r} in {[item.name for item in report.findings]}")


class TestTheCanaryCheck:
    def test_a_passing_canary_is_ok(self, config: Config):
        report = check(config)
        assert finding(report, "canary").status == OK
        assert report.ok
        assert report.subject.startswith("tunebox ok")

    def test_a_failing_canary_fails_the_whole_check(self, config: Config):
        report = check(config, canary=lambda cfg: failing_canary())
        assert finding(report, "canary").status == FAIL
        assert not report.ok
        assert "PROBLEM" in report.subject and "canary" in report.subject

    def test_the_fix_text_points_at_the_403_cause(self, config: Config):
        """A 403 on the media request with metadata working is the JS-runtime failure from PLAN.md
        0.3, and it is the single most likely reason this check ever fires."""
        report = check(config, canary=lambda cfg: failing_canary())
        assert "403" in (finding(report, "canary").fix or "")

    def test_it_can_be_skipped_without_being_called_fine(self, config: Config):
        report = check(config, run_canary_check=False)
        assert finding(report, "canary").status == UNKNOWN
        assert report.ok, "skipped is not failed"


class TestDisk:
    def usage(self, monkeypatch, free_gb: float, total_gb: float = 500.0):
        import shutil as shutil_module

        gib = 1024**3

        class Usage:
            total = int(total_gb * gib)
            used = int((total_gb - free_gb) * gib)
            free = int(free_gb * gib)

        monkeypatch.setattr(shutil_module, "disk_usage", lambda path: Usage)

    def test_plenty_of_space_is_ok(self, config: Config, monkeypatch):
        self.usage(monkeypatch, free_gb=200.0)
        assert finding(check(config), "disk").status == OK

    def test_below_the_floor_fails(self, config: Config, monkeypatch):
        """PLAN.md 3.3's "disk-space alarm on the music volume". A full volume fails every future
        ingest, and a partially written file is the one thing a music scanner will happily index."""
        self.usage(monkeypatch, free_gb=1.0)
        report = check(config)
        assert finding(report, "disk").status == FAIL
        assert not report.ok

    def test_approaching_the_floor_warns(self, config: Config, monkeypatch):
        self.usage(monkeypatch, free_gb=7.0)
        report = check(config)
        assert finding(report, "disk").status == WARN
        assert report.ok, "a warning is not an alert"

    def test_the_floor_is_configurable(self, config: Config, monkeypatch):
        self.usage(monkeypatch, free_gb=7.0)
        assert finding(check(replace(config, min_free_gb=50.0)), "disk").status == FAIL


class TestLibrary:
    def test_it_counts_the_tracks(self, config: Config):
        assert "1 track" in finding(check(config), "library").detail

    def test_a_missing_music_root_fails_and_names_the_real_cause(self, config: Config):
        """An external disk that is not mounted. The mount point is usually still writable, so this
        is the *only* check that notices."""
        report = check(replace(config, music_root=config.music_root / "not-mounted"))
        item = finding(report, "library")
        assert item.status == FAIL
        assert "not mounted" in (item.fix or "")
        assert not report.ok

    def test_an_empty_library_warns_and_says_what_it_might_mean(self, config: Config, tmp_path: Path):
        empty = tmp_path / "empty"
        empty.mkdir()
        item = finding(check(replace(config, music_root=empty)), "library")
        assert item.status == WARN
        assert "unmounted external disk" in (item.fix or "")


class TestBackupAge:
    def test_no_destination_at_all_is_a_failure(self, config: Config):
        """It was a warning first, which made the nightly job green forever on a host with no backup
        — the library's only copy missing, reported as "fine". A system whose only irreplaceable
        state has no copy anywhere is not in a healthy state."""
        report = check(config, acknowledge_backup=False)
        item = finding(report, "backup")
        assert item.status == FAIL
        assert not report.ok
        assert "irreplaceable" in (item.fix or "")
        assert "TUNEBOX_BACKUP_DEST=none" in (item.fix or ""), "it must say how to say 'I know'"

    def test_but_it_can_be_acknowledged_rather_than_nagged_about_daily(self, config: Config):
        """Nagging every night about a deliberate choice is the alert fatigue this module exists to
        avoid — and it has to be a *different* value from unset, or it would be indistinguishable
        from never having read the message."""
        report = check(replace(config, backup_optional=True))
        item = finding(report, "backup")
        assert item.status == UNKNOWN
        assert report.ok

    def test_the_acknowledgement_is_read_from_the_environment(self):
        for value in ("none", "off", "NO"):
            parsed = Config.from_env({"TUNEBOX_BACKUP_DEST": value})
            assert parsed.backup_dest is None
            assert parsed.backup_optional is True
        configured = Config.from_env({"TUNEBOX_BACKUP_DEST": "/Volumes/Backup"})
        assert configured.backup_dest == "/Volumes/Backup"
        assert configured.backup_optional is False
        assert Config.from_env({}).backup_optional is False, "unset is not an acknowledgement"

    def test_a_configured_destination_that_never_ran_fails(self, config: Config, tmp_path: Path):
        configured = replace(config, backup_dest=str(tmp_path / "backup"))
        assert finding(check(configured), "backup").status == FAIL

    def test_a_fresh_backup_is_ok(self, config: Config, tmp_path: Path):
        configured = replace(config, backup_dest=str(tmp_path / "backup"))
        OpsState(configured.ops_state_path).save_section(
            "backup", {"at": time.time(), "dest": str(tmp_path / "backup"), "files": 12, "ok": True}
        )
        assert finding(check(configured), "backup").status == OK

    def test_a_stale_backup_fails_and_blames_the_right_thing(self, config: Config, tmp_path: Path):
        """An external disk that is not plugged in looks exactly like a scheduler that is not
        running, and the fix text has to mention both."""
        configured = replace(config, backup_dest=str(tmp_path / "backup"))
        OpsState(configured.ops_state_path).save_section(
            "backup", {"at": time.time() - 96 * 3600, "dest": "x", "files": 12, "ok": True}
        )
        item = finding(check(configured), "backup")
        assert item.status == FAIL
        assert "not plugged in" in (item.fix or "")


class TestQueue:
    def test_no_database_is_not_a_problem(self, config: Config):
        assert finding(check(config), "queue").status == OK

    def test_failed_jobs_warn_without_failing_the_run(self, config: Config):
        """A single bad URL is not a system problem, and treating it as one would make the nightly
        alert fire for something a person did on purpose."""
        from tunebox.jobs.store import JobStore

        store = JobStore(config.database_path)
        try:
            job, _ = store.enqueue(
                url="https://www.youtube.com/watch?v=vid00000001",
                video_id="vid00000001",
                audio_format="m4a",
            )
            store.mark_failed(job.id, "extraction failed")
        finally:
            store.close()

        report = check(config)
        item = finding(report, "queue")
        assert item.status == WARN
        assert "1 failed job" in item.detail
        assert report.ok

    def test_an_unreadable_database_is_a_warning_not_a_crash(self, config: Config):
        config.database_path.parent.mkdir(parents=True, exist_ok=True)
        config.database_path.write_bytes(b"not a database")
        assert finding(check(config), "queue").status == WARN


class TestPlayer:
    def test_a_selected_output_is_ok(self, config: Config):
        item = finding(check(config, player=FakePlayer(selected=True)), "player")
        assert item.status == OK
        assert "selected" in item.detail

    def test_a_missing_player_is_advisory_only(self, config: Config):
        """PLAN.md 3.1: ingest works with no player installed. Failing the nightly job for it would
        teach whoever reads the alert to ignore it."""
        report = check(config, player=FakePlayer(unavailable=True))
        item = finding(report, "player")
        assert item.status == WARN
        assert report.ok

    def test_an_absent_speaker_is_advisory_too(self, config: Config):
        report = check(config, player=FakePlayer(present=False))
        assert finding(report, "player").status == WARN
        assert report.ok

    def test_an_unexpected_player_error_does_not_escape(self, config: Config):
        class Exploding(FakePlayer):
            def ping(self):
                raise RuntimeError("something nobody predicted")

        assert finding(check(config, player=Exploding()), "player").status == WARN


class TestExposureIsIncluded:
    def test_the_cheap_half_runs_and_the_router_half_does_not(self, config: Config, monkeypatch):
        """The router query costs seconds of multicast waiting and its answer only changes when
        somebody reconfigures the router, so it belongs in a command a person runs."""
        calls: list[str] = []
        monkeypatch.setattr(
            exposure_module,
            "SsdpRouterProbe",
            lambda **kwargs: calls.append("probed") or object(),
        )
        report = check(config)
        assert calls == []
        assert finding(report, "configured bind").status == OK

    def test_a_wildcard_bind_fails_the_nightly_check(self, config: Config):
        report = check(replace(config, api_host="0.0.0.0", allow_all_interfaces=True))
        assert finding(report, "configured bind").status == FAIL
        assert not report.ok


class TestTheLog:
    def test_one_line_per_run_with_the_whole_report(self, config: Config):
        health_module.record(config, check(config))
        health_module.record(config, check(config, canary=lambda cfg: failing_canary()))

        entries = read_jsonl(config.health_log_path)
        assert len(entries) == 2
        assert entries[0]["ok"] is True and entries[1]["ok"] is False
        # The whole report, not a summary: this is the only record of a night nobody watched.
        assert entries[1]["canary"]["error"].startswith("HTTP Error 403")
        assert any(f["name"] == "disk" for f in entries[1]["findings"])

    def test_it_rotates_rather_than_growing_forever(self, config: Config):
        """This runs on an SD card. An unbounded append-only file is a way to lose the machine."""
        for index in range(50):
            append_jsonl(config.health_log_path, {"n": index, "pad": "x" * 100}, max_bytes=1500)
        rotated = config.health_log_path.with_suffix(config.health_log_path.suffix + ".1")
        assert rotated.exists()
        assert config.health_log_path.stat().st_size <= 1500
        # The newest record always survives, which is the one that matters.
        assert read_jsonl(config.health_log_path)[-1]["n"] == 49

    def test_a_torn_last_line_does_not_hide_the_rest(self, config: Config):
        """The last line of an append-only file on a machine that lost power is routinely half a
        line, and the interesting content is all the other ones."""
        append_jsonl(config.health_log_path, {"n": 1}, max_bytes=10_000)
        with config.health_log_path.open("a", encoding="utf-8") as stream:
            stream.write('{"n": 2, "trunc')
        assert [entry["n"] for entry in read_jsonl(config.health_log_path)] == [1]

    def test_logging_can_be_switched_off(self, config: Config):
        append_jsonl(config.health_log_path, {"n": 1}, max_bytes=0)
        assert not config.health_log_path.exists()

    def test_an_unwritable_log_does_not_fail_the_run(self, config: Config, monkeypatch):
        """Not being able to write the log must never turn a healthy run into a failed one, nor mask
        a real alert."""
        append_jsonl(Path("/this/does/not/exist/health.jsonl"), {"n": 1}, max_bytes=100)


# ------------------------------------------------------------------------- alerting


class TestThrottling:
    def test_a_first_failure_alerts(self):
        decision = alerts_module.decide(
            ok=False, previous_status=None, last_alert_at=None, repeat_hours=24, now=1000.0
        )
        assert decision.send and decision.reason == alerts_module.NEW_FAILURE

    def test_a_first_success_is_silent(self):
        """Silence is what "fine" should sound like."""
        decision = alerts_module.decide(
            ok=True, previous_status=None, last_alert_at=None, repeat_hours=24, now=1000.0
        )
        assert not decision.send

    def test_the_same_failure_is_not_repeated_the_next_hour(self):
        now = 100_000.0
        decision = alerts_module.decide(
            ok=False, previous_status="fail", last_alert_at=now - 3600, repeat_hours=24, now=now
        )
        assert not decision.send and decision.reason == alerts_module.THROTTLED

    def test_but_it_is_repeated_the_next_day(self):
        """A problem nobody has fixed is still mentioned — once."""
        now = 100_000.0
        decision = alerts_module.decide(
            ok=False, previous_status="fail", last_alert_at=now - 25 * 3600,
            repeat_hours=24, now=now,
        )
        assert decision.send and decision.reason == alerts_module.STILL_FAILING

    def test_recovery_is_worth_saying(self):
        """Without this, the last message anybody has says the system is broken."""
        decision = alerts_module.decide(
            ok=True, previous_status="fail", last_alert_at=1.0, repeat_hours=24, now=100_000.0
        )
        assert decision.send and decision.reason == alerts_module.RECOVERED

    def test_zero_repeat_hours_means_every_run(self):
        decision = alerts_module.decide(
            ok=False, previous_status="fail", last_alert_at=99_999.0, repeat_hours=0, now=100_000.0
        )
        assert decision.send


class TestDelivery:
    def dispatch(self, config: Config, **kwargs):
        return alerts_module.dispatch(
            config,
            ok=kwargs.pop("ok", False),
            subject="tunebox PROBLEM on pi: canary",
            payload={"status": "fail"},
            **kwargs,
        )

    def test_with_no_channel_configured_the_log_is_the_channel(self, config: Config, caplog):
        """Under launchd and systemd, stderr *is* the log file. Better than nothing, and it means a
        host with nothing configured still has a record."""
        with caplog.at_level("ERROR"):
            result = self.dispatch(config)
        assert result.decision.send
        assert result.deliveries == {"log": None}
        assert "ALERT" in caplog.text

    def test_the_command_gets_json_on_stdin_and_the_subject_last(self, config: Config, tmp_path: Path):
        """Enough to drive ``mail``, ``terminal-notifier``, ``ntfy`` or a three-line script, with no
        wrapper — which is why the subject is an argument rather than an environment variable."""
        seen: dict = {}

        def runner(argv, document):
            seen["argv"] = list(argv)
            seen["stdin"] = document
            return 0, None

        configured = replace(config, alert_command="notify --title tunebox --message")
        result = alerts_module.dispatch(
            configured,
            ok=False,
            subject="the subject",
            payload={"status": "fail", "findings": []},
            runner=runner,
        )
        assert seen["argv"] == ["notify", "--title", "tunebox", "--message", "the subject"]
        assert json.loads(seen["stdin"])["status"] == "fail"
        assert result.deliveries == {"command": None}

    def test_a_shell_pipeline_has_to_be_deliberate(self, config: Config):
        """The command is split with shlex and executed as an argv list, never through a shell
        (PLAN.md 3.2). ``|`` is therefore an argument, not a pipe."""
        argv = alerts_module._command_argv("mail -s", "subject | rm -rf /")
        assert argv == ["mail", "-s", "subject | rm -rf /"]

    def test_a_delivery_failure_is_reported_not_raised(self, config: Config):
        """The point of this module is a failure elsewhere. Hiding that behind "could not send
        email" would be perverse."""
        configured = replace(config, alert_command="/nonexistent/notifier")
        result = alerts_module.dispatch(
            configured, ok=False, subject="s", payload={}, runner=alerts_module._run_command
        )
        assert result.failed_channels == ("command",)

    def test_an_alert_nobody_received_is_retried_rather_than_throttled(self, config: Config):
        """The timestamp is written *before* delivery so that a process killed inside a webhook
        timeout does not re-send tomorrow as if nothing had happened. Total failure is the other half
        of that trade: leaving it set would suppress the retry for a day, and the message nobody
        received is the one that mattered."""
        configured = replace(config, alert_command="/nonexistent/notifier")
        first = alerts_module.dispatch(
            configured, ok=False, subject="s", payload={}, runner=alerts_module._run_command
        )
        assert first.failed_channels == ("command",)
        state = OpsState(configured.ops_state_path).section("alerts")
        assert state["last_alert_at"] == 0.0
        assert "notifier" in state["last_delivery_error"]

        second = alerts_module.dispatch(
            configured, ok=False, subject="s", payload={}, runner=alerts_module._run_command
        )
        assert second.decision.send, "the same failure must be re-sent, not throttled"
        assert second.decision.reason == alerts_module.STILL_FAILING

    def test_a_partial_delivery_still_counts_as_delivered(self, config: Config):
        """One channel arriving is enough; retrying because the *other* one is down would send the
        message twice to whoever is actually reading it."""
        configured = replace(
            config, alert_command="/nonexistent/notifier", alert_webhook="https://x/y"
        )
        result = alerts_module.dispatch(
            configured,
            ok=False,
            subject="s",
            payload={},
            runner=alerts_module._run_command,
            poster=lambda url, body: None,
        )
        assert result.failed_channels == ("command",)
        assert OpsState(configured.ops_state_path).section("alerts")["last_alert_at"] > 0

    def test_the_webhook_posts_the_report(self, config: Config):
        posted: dict = {}

        def poster(url, body):
            posted["url"] = url
            posted["body"] = json.loads(body)
            return None

        configured = replace(config, alert_webhook="https://ntfy.example/topic")
        alerts_module.dispatch(
            configured, ok=False, subject="s", payload={"status": "fail"}, poster=poster
        )
        assert posted["url"] == "https://ntfy.example/topic"
        assert posted["body"]["status"] == "fail"

    def test_the_payload_never_contains_the_token(self, config: Config):
        """The report travels to a third party when a webhook is configured. It carries hostnames,
        versions, counts and error strings — and must never carry the shared secret."""
        configured = replace(
            config, api_token="super-secret-token-value", alert_webhook="https://x/y"
        )
        report = check(configured, canary=lambda cfg: failing_canary())
        assert "super-secret-token-value" not in json.dumps(report.as_dict())
        assert "super-secret-token-value" not in repr(configured)

    def test_the_decision_is_recorded_before_delivery_is_attempted(self, config: Config):
        """If the process dies inside a webhook timeout, the next run must not re-send the same "new
        failure" as though nothing had happened."""

        def poster(url, body):
            raise AssertionError("delivery exploded")

        configured = replace(config, alert_webhook="https://x/y")
        with pytest.raises(AssertionError):
            alerts_module.dispatch(
                configured, ok=False, subject="s", payload={}, poster=poster
            )
        assert OpsState(configured.ops_state_path).section("alerts")["status"] == "fail"

    def test_state_drives_the_second_run(self, config: Config):
        configured = replace(config, alert_command=None)
        first = self.dispatch(configured)
        second = self.dispatch(configured)
        assert first.decision.reason == alerts_module.NEW_FAILURE
        assert second.decision.reason == alerts_module.THROTTLED
        recovered = self.dispatch(configured, ok=True)
        assert recovered.decision.reason == alerts_module.RECOVERED


# ---------------------------------------------------------------------- maintenance


class TestMaintenance:
    def updated(self) -> UpdateResult:
        return UpdateResult(ok=True, action=UPDATED, before="2026.8.19", after="2026.9.30")

    def rolled_back(self) -> UpdateResult:
        return UpdateResult(
            ok=False,
            action=ROLLED_BACK,
            before="2026.8.19",
            after="2026.9.30",
            error="the rollback did not restore working extraction — this needs a human",
        )

    def backed_up(self, ok: bool = True) -> BackupResult:
        return BackupResult(
            ok=ok, direction="backup", source="/music", dest="/backup", files=12, bytes=1234,
            verified=ok, error=None if ok else "rsync exited 23",
        )

    def run(self, config: Config, **kwargs):
        kwargs.setdefault("updater", lambda cfg: self.updated())
        kwargs.setdefault("backer", lambda cfg: self.backed_up())
        kwargs.setdefault("health", lambda cfg: check(cfg))
        kwargs.setdefault("alert", False)
        return maintain_module.maintain(config, **kwargs)

    def test_the_order_is_update_then_check_then_backup(self, config: Config, tmp_path: Path):
        """A scheduler cannot express this, which is why it is one command: the update has to run
        before the canary or the day's canary tests yesterday's yt-dlp, and the backup has to run
        after both so that what is copied is a library whose ingest path is known to work."""
        order: list[str] = []
        configured = replace(config, backup_dest=str(tmp_path / "backup"))
        self.run(
            configured,
            updater=lambda cfg: order.append("update") or self.updated(),
            health=lambda cfg: order.append("health") or check(cfg),
            backer=lambda cfg: order.append("backup") or self.backed_up(),
        )
        assert order == ["update", "health", "backup"]

    def test_a_crashing_updater_does_not_cancel_the_backup(self, config: Config, tmp_path: Path):
        """The backup protects the only irreplaceable thing here. Coupling it to the updater's luck
        would be the worst dependency in the system."""
        configured = replace(config, backup_dest=str(tmp_path / "backup"))
        backups: list[str] = []

        def explode(cfg):
            raise RuntimeError("pip segfaulted")

        result = self.run(
            configured,
            updater=explode,
            backer=lambda cfg: backups.append("ran") or self.backed_up(),
        )
        assert backups == ["ran"]
        assert not result.ok
        assert any(
            item.name == "yt-dlp update" and item.status == FAIL for item in result.findings
        )

    def test_a_crashing_backup_is_a_finding_not_a_traceback(self, config: Config, tmp_path: Path):
        configured = replace(config, backup_dest=str(tmp_path / "backup"))

        def explode(cfg):
            raise OSError("the USB disk went away mid-transfer")

        result = self.run(configured, backer=explode)
        assert not result.ok
        assert any(item.name == "backup run" and item.status == FAIL for item in result.findings)

    def test_a_missing_destination_is_not_reported_twice(self, config: Config):
        """The health check already says there is no destination. Saying it again in the same alert
        is how an alert stops being read."""
        result = self.run(config)
        names = [item.name for item in result.findings]
        assert names.count("backup") == 1
        assert "backup run" not in names

    def test_one_bad_night_produces_exactly_one_alert(self, config: Config, tmp_path: Path):
        configured = replace(config, backup_dest=str(tmp_path / "backup"))
        result = maintain_module.maintain(
            configured,
            updater=lambda cfg: self.rolled_back(),
            backer=lambda cfg: self.backed_up(ok=False),
            health=lambda cfg: check(cfg, canary=lambda c: failing_canary()),
            alert=True,
        )
        assert result.alert is not None
        assert result.alert.decision.send
        assert result.alert.decision.reason == alerts_module.NEW_FAILURE
        # Three separate failures, one message, and the subject names all of them.
        assert "canary" in result.subject and "backup run" in result.subject
        assert result.alert.deliveries == {"log": None}, "nothing configured, so the log is it"

    def test_everything_reaches_the_log_as_one_record(self, config: Config, tmp_path: Path):
        configured = replace(config, backup_dest=str(tmp_path / "backup"))
        self.run(configured)
        entry = read_jsonl(configured.health_log_path)[-1]
        assert entry["maintenance"]["update"]["action"] == UPDATED
        assert entry["maintenance"]["backup"]["files"] == 12

    def test_update_can_be_switched_off_entirely(self, config: Config):
        """The right setting on a host whose venv is managed by something else — a container image,
        Nix, a distribution package."""
        calls: list[str] = []
        result = self.run(
            replace(config, auto_update=False),
            updater=lambda cfg: calls.append("no") or self.updated(),
        )
        assert calls == []
        assert any(
            item.name == "yt-dlp update" and item.status == UNKNOWN for item in result.findings
        )

    def test_the_result_is_json_serialisable(self, config: Config, tmp_path: Path):
        configured = replace(config, backup_dest=str(tmp_path / "backup"))
        payload = json.loads(json.dumps(self.run(configured).as_dict(), default=str))
        assert payload["update"]["action"] == UPDATED
        assert payload["health"]["findings"]


# ---------------------------------------------------------------------------- the CLI


class TestTheCommands:
    def env(self, monkeypatch, config: Config) -> None:
        monkeypatch.setenv("TUNEBOX_MUSIC_ROOT", str(config.music_root))
        monkeypatch.setenv("TUNEBOX_API_HOST", config.api_host)
        # "none" rather than unset: an unconfigured backup is a failure, and these tests are about
        # the commands rather than about that.
        monkeypatch.setenv("TUNEBOX_BACKUP_DEST", "none")
        monkeypatch.setattr(
            exposure_module, "read_listeners",
            lambda **kwargs: exposure_module.ListenerTable((), command="fake"),
        )
        monkeypatch.setattr(health_module, "run_canary", lambda cfg: passing_canary())
        monkeypatch.setattr(
            health_module, "_player_finding",
            lambda cfg, player: health_module.Finding(
                name="player", status=WARN, detail="not checked in tests"
            ),
        )

    def test_healthcheck_writes_the_log_and_exits_zero(self, config: Config, monkeypatch, capsys):
        from tunebox.cli import main

        self.env(monkeypatch, config)
        assert main(["healthcheck", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert read_jsonl(config.health_log_path)

    def test_healthcheck_exits_one_when_the_canary_fails(self, config: Config, monkeypatch, capsys):
        from tunebox.cli import main

        self.env(monkeypatch, config)
        monkeypatch.setattr(health_module, "run_canary", lambda cfg: failing_canary())
        assert main(["healthcheck"]) == 1
        out = capsys.readouterr().out
        assert "canary" in out and "403" in out

    def test_no_canary_needs_no_network(self, config: Config, monkeypatch, capsys):
        from tunebox.cli import main

        self.env(monkeypatch, config)

        def explode(cfg):  # pragma: no cover - must not be called
            raise AssertionError("--no-canary must not download anything")

        monkeypatch.setattr(health_module, "run_canary", explode)
        assert main(["healthcheck", "--no-canary"]) == 0

    def test_history_prints_past_runs(self, config: Config, monkeypatch, capsys):
        from tunebox.cli import main

        self.env(monkeypatch, config)
        main(["healthcheck", "--no-canary"])
        capsys.readouterr()
        assert main(["healthcheck", "--history", "5"]) == 0
        assert "tunebox ok" in capsys.readouterr().out

    def test_maintain_runs_the_whole_sequence(self, config: Config, monkeypatch, capsys):
        from tunebox.cli import main

        self.env(monkeypatch, config)
        monkeypatch.setattr(
            maintain_module, "update_ytdlp",
            lambda cfg: UpdateResult(ok=True, action=UPDATED, before="a", after="b"),
        )
        assert main(["maintain", "--no-backup", "--no-alert", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["update"]["action"] == UPDATED
        assert payload["alert"] is None

    def test_maintain_exits_one_on_a_bad_night(self, config: Config, monkeypatch, capsys):
        from tunebox.cli import main

        self.env(monkeypatch, config)
        monkeypatch.setattr(health_module, "run_canary", lambda cfg: failing_canary())
        assert main(["maintain", "--no-update", "--no-backup", "--no-alert"]) == 1
