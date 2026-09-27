"""``tunebox backup`` and ``tunebox restore`` — the library's only insurance.

These tests run the **real rsync**, on temporary directories, because the thing worth checking is not
that the right argv was assembled but that the files arrive, that the excludes exclude, and that the
restore restores. Everything stays local, so no network is involved.

The two most valuable tests here are the ones about what the backup *refuses* to do:

* ``--delete`` is off unless asked for, because the accident a home backup most often has to survive
  is somebody deleting an album, and ``--delete`` propagates exactly that within a day;
* ``restore`` writes nothing without ``--yes``, because a restore path nobody has ever executed is a
  hope rather than a plan, and the first time it runs must not be the first time anybody sees what it
  would do.

The SQLite test is the subtle one: the live database is excluded from the transfer and a *snapshot*
is sent instead. Copying an open WAL-mode database file byte-wise is the classic way to produce a
backup that restores into a corrupt database, and the test asserts the restored copy opens and has
the rows.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from tunebox.config import Config
from tunebox.errors import TuneboxError
from tunebox.ops import backup as backup_module
from tunebox.ops.state import OpsState

pytestmark = pytest.mark.skipif(
    shutil.which("rsync") is None, reason="rsync is not installed"
)


def library(root: Path) -> Config:
    """A small, realistic library: two artists, an album, a single, and cover art."""
    (root / "Kraftwerk" / "Trans-Europe Express").mkdir(parents=True)
    (root / "Kraftwerk" / "Trans-Europe Express" / "01 - Europe Endless.m4a").write_bytes(
        b"\x00" * 2048
    )
    (root / "Kraftwerk" / "Trans-Europe Express" / "cover.jpg").write_bytes(b"\xff\xd8\xff")
    (root / "陳奕迅 Eason Chan" / "Singles").mkdir(parents=True)
    (root / "陳奕迅 Eason Chan" / "Singles" / "K歌之王.m4a").write_bytes(b"\x00" * 1024)
    return Config(music_root=root)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return library(tmp_path / "music")


@pytest.fixture
def dest(tmp_path: Path) -> Path:
    return tmp_path / "backup"


def files_under(root: Path) -> set[str]:
    """Every file, ignoring the dot-directories.

    The ops state and the health log are deliberately *not* backed up (they describe the backup, and
    would make every incremental run non-empty), so comparing whole trees would compare the wrong
    thing. What this asserts is the thing that matters: every track arrived.
    """
    return {
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and not any(part.startswith(".") for part in path.relative_to(root).parts)
    }


class TestTheTransfer:
    def test_every_track_arrives(self, config: Config, dest: Path):
        result = backup_module.run_backup(config, dest=str(dest))
        assert result.ok, result.error
        assert files_under(dest) == files_under(config.music_root)
        assert "K歌之王.m4a" in " ".join(files_under(dest)), "CJK filenames must survive rsync"
        assert result.files == 3
        assert result.bytes == 2048 + 3 + 1024

    def test_it_is_verified_by_asking_rsync_again(self, config: Config, dest: Path):
        """"rsync exited 0" is a weaker statement than "a second pass has nothing left to send" —
        rsync exits 0 in cases that are not success, including files it could not read."""
        result = backup_module.run_backup(config, dest=str(dest), verify=True)
        assert result.verified is True

    def test_verification_catches_an_incomplete_transfer(self, config: Config, dest: Path):
        """Simulated by deleting a file from the destination between the two passes, which is what a
        destination disk filling up halfway looks like from here."""
        calls = {"n": 0}
        real = backup_module._run

        def flaky(argv):
            calls["n"] += 1
            outcome = real(argv)
            if calls["n"] == 1:
                for path in dest.rglob("*.m4a"):
                    path.unlink()
            return outcome

        result = backup_module.run_backup(config, dest=str(dest), runner=flaky)
        assert result.verified is False
        assert not result.ok
        assert "incomplete" in (result.error or "")

    def test_the_scratch_directory_is_never_transferred(self, config: Config, dest: Path):
        """It holds partial downloads. Backing those up wastes space and, worse, a restore would put
        half-downloaded files where a music scanner will happily index them."""
        (config.scratch_root / "ingest-abc").mkdir(parents=True)
        (config.scratch_root / "ingest-abc" / "track.m4a.part").write_bytes(b"partial")
        backup_module.run_backup(config, dest=str(dest))
        assert not (dest / ".tunebox-tmp").exists()

    def test_it_is_incremental(self, config: Config, dest: Path):
        first = backup_module.run_backup(config, dest=str(dest))
        assert first.transferred == 3
        second = backup_module.run_backup(config, dest=str(dest))
        assert second.transferred == 0, "nothing changed, so nothing should move"
        assert second.ok

    def test_a_dry_run_transfers_nothing_but_says_what_it_would(self, config: Config, dest: Path):
        result = backup_module.run_backup(config, dest=str(dest), dry_run=True)
        assert result.ok
        assert not dest.exists()
        assert any(".m4a" in change for change in result.changes)

    def test_a_missing_rsync_is_an_actionable_error(self, config: Config, dest: Path):
        result = backup_module.run_backup(
            config, dest=str(dest), runner=lambda argv: (127, "not found")
        )
        assert not result.ok
        assert "apt-get install rsync" in (result.error or "")


class TestDeletingIsOptIn:
    def test_by_default_a_deleted_album_survives_at_the_destination(
        self, config: Config, dest: Path
    ):
        """The whole reason ``--delete`` is off: the failure a backup most often has to cover is a
        deletion, and propagating it turns the backup into a second copy of the mistake."""
        backup_module.run_backup(config, dest=str(dest))
        shutil.rmtree(config.music_root / "Kraftwerk")

        result = backup_module.run_backup(config, dest=str(dest))
        assert result.ok
        assert (dest / "Kraftwerk" / "Trans-Europe Express" / "01 - Europe Endless.m4a").exists()
        assert result.pruned is False

    def test_prune_propagates_deletions_when_explicitly_asked(self, config: Config, dest: Path):
        backup_module.run_backup(config, dest=str(dest))
        shutil.rmtree(config.music_root / "Kraftwerk")
        result = backup_module.run_backup(config, dest=str(dest), prune=True)
        assert result.pruned is True
        assert not (dest / "Kraftwerk").exists()

    def test_the_default_comes_from_config(self, config: Config, dest: Path):
        from dataclasses import replace

        backup_module.run_backup(replace(config, backup_prune=True), dest=str(dest))
        shutil.rmtree(config.music_root / "Kraftwerk")
        result = backup_module.run_backup(replace(config, backup_prune=True), dest=str(dest))
        assert result.pruned is True
        assert not (dest / "Kraftwerk").exists()


class TestDestinationSafety:
    def test_the_music_root_itself_is_refused(self, config: Config):
        with pytest.raises(TuneboxError, match="music root itself"):
            backup_module.run_backup(config, dest=str(config.music_root))

    def test_a_destination_inside_the_library_is_refused(self, config: Config):
        """It would copy the library into itself, and then back that copy up too."""
        with pytest.raises(TuneboxError, match="inside the music root"):
            backup_module.run_backup(config, dest=str(config.music_root / "backup"))

    def test_a_destination_containing_the_library_is_refused(self, config: Config):
        """The catastrophic one, and it looks like a typo rather than a mistake: ``--dest ~/Music``
        for a library at ``~/Music/tunebox``. rsync would mirror the library over the top of
        everything else in that directory and, with ``--prune``, **delete all of it** — including the
        library itself, which is inside the tree being emptied. Found by an audit, and reproduced
        before this guard existed: an emptied music root and a deleted sibling file."""
        with pytest.raises(TuneboxError, match=r"\*contains\* the music root"):
            backup_module.run_backup(config, dest=str(config.music_root.parent))

    def test_the_same_refusal_applies_with_prune_and_before_rsync_runs(self, config: Config):
        sibling = config.music_root.parent / "family-photos.txt"
        sibling.write_text("irreplaceable", encoding="utf-8")
        with pytest.raises(TuneboxError, match=r"\*contains\* the music root"):
            backup_module.run_backup(config, dest=str(config.music_root.parent), prune=True)
        assert sibling.read_text(encoding="utf-8") == "irreplaceable"
        assert (config.music_root / "Kraftwerk").is_dir()

    def test_a_grandparent_destination_is_refused_too(self, config: Config):
        with pytest.raises(TuneboxError, match=r"\*contains\* the music root"):
            backup_module.run_backup(config, dest=str(config.music_root.parent.parent))


    def test_no_destination_at_all_explains_why_it_matters(self, config: Config):
        with pytest.raises(TuneboxError, match="only irreplaceable state"):
            backup_module.run_backup(config)

    def test_a_same_disk_destination_warns_about_the_failure_it_does_not_cover(
        self, config: Config, dest: Path
    ):
        """PLAN.md's risk table is about the SSD dying. A copy on that same SSD does protect against
        an accidental delete and does nothing whatsoever about the disk — so it is allowed, and said
        out loud."""
        result = backup_module.run_backup(config, dest=str(dest))
        assert any("same filesystem" in warning for warning in result.warnings)
        assert result.ok

    @pytest.mark.parametrize(
        "destination,local",
        [
            ("/Volumes/Backup/tunebox", True),
            ("./relative/path", True),
            ("pi@nas:/srv/music", False),
            ("nas:/srv/music", False),
            ("rsync://nas/music", False),
            ("/mnt/weird:name/music", True),
        ],
    )
    def test_remote_destinations_are_recognised(self, destination, local):
        """rsync's own rule — a colon before the first slash — rather than a guess, so a local path
        that happens to contain a colon is not mistaken for a hostname."""
        assert backup_module.is_local_path(destination) is local


class TestTheDatabaseSnapshot:
    def make_database(self, config: Config) -> None:
        config.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(config.database_path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE tracks (video_id TEXT, path TEXT)")
        connection.execute("INSERT INTO tracks VALUES ('jNQXAC9IVRw', 'Kraftwerk/x.m4a')")
        connection.commit()
        # Left open on purpose: the live case is a worker holding the database while the nightly
        # backup runs, which is exactly when a byte-wise copy produces an unrestorable file.
        self.connection = connection

    def test_the_live_database_is_excluded_and_a_snapshot_sent_instead(
        self, config: Config, dest: Path
    ):
        self.make_database(config)
        try:
            result = backup_module.run_backup(config, dest=str(dest))
        finally:
            self.connection.close()

        assert result.ok
        assert not (dest / ".tunebox" / "jobs.db").exists()
        snapshot = dest / ".tunebox" / backup_module.SNAPSHOT_NAME
        assert snapshot.exists()

        restored = sqlite3.connect(snapshot)
        try:
            rows = restored.execute("SELECT video_id FROM tracks").fetchall()
        finally:
            restored.close()
        assert rows == [("jNQXAC9IVRw",)]

    def test_no_database_yet_is_not_a_problem(self, config: Config):
        assert backup_module.snapshot_database(config) is None

    def test_a_corrupt_database_is_a_warning_not_a_failed_backup(
        self, config: Config, dest: Path
    ):
        """The music is what matters. A database that cannot be snapshotted must not stop the tracks
        being copied."""
        config.database_path.parent.mkdir(parents=True, exist_ok=True)
        config.database_path.write_bytes(b"this is not a database")
        result = backup_module.run_backup(config, dest=str(dest))
        assert result.ok
        assert any("queue database" in warning for warning in result.warnings)


class TestStatus:
    def test_it_reports_the_age_and_goes_stale(self, config: Config, dest: Path):
        from dataclasses import replace

        configured = replace(config, backup_dest=str(dest))
        backup_module.run_backup(configured, dest=str(dest))

        fresh = backup_module.backup_status(configured)
        assert fresh.configured and not fresh.stale
        assert fresh.age_hours is not None and fresh.age_hours < 1

        later = backup_module.backup_status(configured, now=lambda: time.time() + 72 * 3600)
        assert later.stale
        assert later.age_hours is not None and later.age_hours > 48

    def test_a_configured_destination_that_has_never_run_is_stale(self, config: Config, dest: Path):
        from dataclasses import replace

        status = backup_module.backup_status(replace(config, backup_dest=str(dest)))
        assert status.at is None
        assert status.stale

    def test_an_unconfigured_destination_is_not_stale_it_is_absent(self, config: Config):
        status = backup_module.backup_status(config)
        assert not status.configured
        assert not status.stale

    def test_the_restore_command_is_printed_where_it_will_be_needed(self, config: Config, dest: Path):
        """The day this is needed, the library is gone and the README may have been on that disk."""
        from dataclasses import replace

        command = backup_module.restore_command(replace(config, backup_dest=str(dest)))
        assert command.startswith("tunebox restore --from ")
        assert str(dest) in command
        assert "--yes" in command


class TestRestore:
    def test_it_is_a_dry_run_unless_confirmed(self, config: Config, dest: Path):
        backup_module.run_backup(config, dest=str(dest))
        shutil.rmtree(config.music_root / "Kraftwerk")

        result = backup_module.restore(config, source=str(dest))
        assert result.ok
        assert result.dry_run
        assert not (config.music_root / "Kraftwerk").exists(), "nothing may be written"
        assert any(".m4a" in change for change in result.changes)
        assert any("dry run" in warning for warning in result.warnings)

    def test_confirmed_it_brings_the_library_back(self, config: Config, dest: Path):
        backup_module.run_backup(config, dest=str(dest))
        expected = files_under(config.music_root)
        shutil.rmtree(config.music_root)

        result = backup_module.restore(config, source=str(dest), confirm=True)
        assert result.ok and not result.dry_run
        assert files_under(config.music_root) == expected

    def test_it_never_deletes_anything_in_the_library(self, config: Config, dest: Path):
        """A restore that removed files the backup does not know about would turn "get my album
        back" into "lose everything ingested since the last backup"."""
        backup_module.run_backup(config, dest=str(dest))
        (config.music_root / "New Artist").mkdir()
        (config.music_root / "New Artist" / "later.m4a").write_bytes(b"\x00" * 64)

        result = backup_module.restore(config, source=str(dest), confirm=True)
        assert result.ok
        assert (config.music_root / "New Artist" / "later.m4a").exists()

    def test_a_nonexistent_backup_is_refused_before_anything_runs(self, config: Config, tmp_path: Path):
        with pytest.raises(TuneboxError, match="not a directory"):
            backup_module.restore(config, source=str(tmp_path / "nope"))

    def test_with_nothing_configured_it_says_what_to_pass(self, config: Config):
        with pytest.raises(TuneboxError, match="--from"):
            backup_module.restore(config)


class TestTheCommands:
    def env(self, monkeypatch, config: Config, dest: Path | None = None) -> None:
        monkeypatch.setenv("TUNEBOX_MUSIC_ROOT", str(config.music_root))
        if dest is not None:
            monkeypatch.setenv("TUNEBOX_BACKUP_DEST", str(dest))
        else:
            monkeypatch.delenv("TUNEBOX_BACKUP_DEST", raising=False)

    def test_backup_then_status_then_restore(
        self, config: Config, dest: Path, monkeypatch, capsys
    ):
        from tunebox.cli import main

        self.env(monkeypatch, config, dest)

        assert main(["backup", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] and payload["verified"] is True

        assert main(["backup", "--status"]) == 0
        assert "to restore:" in capsys.readouterr().out

        shutil.rmtree(config.music_root / "Kraftwerk")
        assert main(["restore"]) == 0
        out = capsys.readouterr().out
        assert "dry run" in out
        assert not (config.music_root / "Kraftwerk").exists()

        assert main(["restore", "--yes"]) == 0
        assert (config.music_root / "Kraftwerk").exists()

    def test_status_exits_nonzero_with_no_destination(self, config: Config, monkeypatch, capsys):
        from tunebox.cli import main

        self.env(monkeypatch, config)
        assert main(["backup", "--status"]) == 1
        assert "only irreplaceable state" in capsys.readouterr().out

    def test_a_refused_destination_is_an_error_not_a_traceback(
        self, config: Config, monkeypatch, capsys
    ):
        from tunebox.cli import main

        self.env(monkeypatch, config)
        assert main(["backup", "--dest", str(config.music_root)]) == 1
        assert "error:" in capsys.readouterr().err


class TestStateFile:
    def test_a_corrupt_state_file_is_treated_as_empty(self, config: Config):
        """It is bookkeeping, not the library. Losing it must degrade to "no known-good version
        recorded", never to a crash in the job that nobody is watching."""
        config.ops_state_path.parent.mkdir(parents=True, exist_ok=True)
        config.ops_state_path.write_text("{not json", encoding="utf-8")
        state = OpsState(config.ops_state_path)
        assert state.read() == {}
        assert state.section("ytdlp") == {}
        state.save_section("ytdlp", {"known_good": "2026.8.19"})
        assert state.section("ytdlp")["known_good"] == "2026.8.19"

    def test_it_is_written_atomically_and_privately(self, config: Config):
        state = OpsState(config.ops_state_path)
        state.save_section("backup", {"at": 1.0})
        assert config.ops_state_path.stat().st_mode & 0o777 == 0o600
        assert json.loads(config.ops_state_path.read_text())["backup"]["at"] == 1.0
        # No temporary files left behind.
        assert [p.name for p in config.ops_state_path.parent.glob(".ops-*")] == []
