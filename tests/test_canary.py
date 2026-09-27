"""The canary — one real download, used both as the update gate and as the daily health check.

What is worth testing about a canary is everything it refuses to be fooled by, because a canary that
passes when the system is broken is worse than not having one: it is an *affirmative* wrong answer,
and it is the thing the alerting is built on.

So: a zero-length file must fail, a truncated file must fail, a file whose duration does not match
the metadata must fail, and none of those may be reported as "the download failed" — the stage the
canary reached is what tells an operator whether to look at YouTube or at the JS runtime.

No network here. :class:`CanarySource` writes bytes into the scratch directory exactly as yt-dlp
would, which is enough because what is under test is the *verification*, not the transport.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tunebox.config import CANARY_MIN_BYTES, Config
from tunebox.core.models import FetchedMedia, TrackTags
from tunebox.errors import SourceError, SourceTimeout
from tunebox.ops import canary as canary_module
from tunebox.ops.canary import ytdlp_version as real_ytdlp_version

CANARY_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


class CanarySource:
    """A source that writes ``size`` bytes of "audio", or fails at a chosen stage."""

    name = "canary-fake"

    def __init__(
        self,
        *,
        size: int = CANARY_MIN_BYTES * 2,
        duration: float | None = 19.0,
        fail_inspect: Exception | None = None,
        fail_fetch: Exception | None = None,
    ) -> None:
        self.size = size
        self.duration = duration
        self.fail_inspect = fail_inspect
        self.fail_fetch = fail_fetch
        self.dest_dirs: list[Path] = []

    def search(self, query, limit=None):  # pragma: no cover - not used by the canary
        return []

    def inspect(self, url: str) -> TrackTags:
        if self.fail_inspect is not None:
            raise self.fail_inspect
        return TrackTags(
            title="Me at the zoo",
            artist="jawed",
            duration=self.duration,
            source="youtube",
            source_id="jNQXAC9IVRw",
            source_url=url,
        )

    def fetch(self, url, dest_dir: Path, audio_format="m4a", on_progress=None) -> FetchedMedia:
        if self.fail_fetch is not None:
            raise self.fail_fetch
        self.dest_dirs.append(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        audio = dest_dir / f"track.{audio_format}"
        audio.write_bytes(b"\x00" * self.size)
        return FetchedMedia(audio_path=audio, cover_path=None)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(music_root=tmp_path / "music", canary_url=CANARY_URL)


@pytest.fixture(autouse=True)
def no_ffprobe(monkeypatch):
    """ffprobe cannot read the fake payload, and in the real world it is what catches a truncated
    file. Tests that care about the duration check patch this with a number."""
    monkeypatch.setattr(canary_module, "probe_duration", lambda path: None)


@pytest.fixture(autouse=True)
def fixed_version(monkeypatch):
    monkeypatch.setattr(canary_module, "ytdlp_version", lambda config, argv=None: "2026.8.19")


class TestThePassingCase:
    def test_it_downloads_and_reports_what_it_got(self, config: Config):
        source = CanarySource()
        result = canary_module.run_canary(config, source=source)
        assert result.ok
        assert result.stage == "verify"
        assert result.video_id == "jNQXAC9IVRw"
        assert result.title == "Me at the zoo"
        assert result.size == CANARY_MIN_BYTES * 2
        assert result.ytdlp_version == "2026.8.19"
        assert "canary ok" in result.summary

    def test_it_downloads_into_the_scratch_directory_and_cleans_up(self, config: Config):
        """Inside the music root, not ``/tmp``: under the systemd unit the installer writes, the
        music root is the only path this service may write to. And removed afterwards, or a daily
        job slowly fills the library volume with copies of a test video."""
        source = CanarySource()
        canary_module.run_canary(config, source=source)
        assert source.dest_dirs[0].parent == config.canary_dir
        assert config.canary_dir.is_relative_to(config.music_root)
        assert list(config.canary_dir.iterdir()) == [], "the run's directory is removed"

    def test_keep_leaves_the_file_for_inspection(self, config: Config):
        result = canary_module.run_canary(config, source=CanarySource(), keep=True)
        assert any(config.canary_dir.rglob("track.m4a"))
        assert result.ok

    def test_two_runs_do_not_share_a_directory(self, config: Config):
        """They can overlap for real: the nightly job runs one while somebody types
        ``tunebox healthcheck``. Sharing a directory would let one delete the other's download — and
        inside ``tunebox update`` that spurious failure would trigger a needless rollback and
        blocklist a perfectly good release."""
        first, second = CanarySource(), CanarySource()
        canary_module.run_canary(config, source=first, keep=True)
        canary_module.run_canary(config, source=second, keep=True)
        assert first.dest_dirs[0] != second.dest_dirs[0]

    def test_leftovers_from_a_killed_run_are_swept(self, config: Config):
        """A run that was killed leaves its directory behind. Small, but not forever."""
        old = config.canary_dir / "run-ancient"
        old.mkdir(parents=True)
        (old / "track.m4a").write_bytes(b"\x00" * 1024)
        import os

        os.utime(old, (0, 0))
        canary_module.run_canary(config, source=CanarySource())
        assert not old.exists()

    def test_a_stale_file_from_a_previous_run_cannot_make_it_pass(self, config: Config):
        """Each run gets an empty directory of its own. Otherwise a fetch that silently wrote nothing
        would be "verified" against last night's download, forever."""
        config.canary_dir.mkdir(parents=True)
        (config.canary_dir / "track.m4a").write_bytes(b"\x01" * (CANARY_MIN_BYTES * 4))

        class WritesNothing(CanarySource):
            def fetch(self, url, dest_dir, audio_format="m4a", on_progress=None):
                dest_dir.mkdir(parents=True, exist_ok=True)
                raise SourceError("yt-dlp produced no audio file")

        result = canary_module.run_canary(config, source=WritesNothing())
        assert not result.ok
        assert result.stage == "fetch"


class TestTheFailingCases:
    def test_metadata_failure_reports_the_inspect_stage(self, config: Config):
        """"inspect" versus "fetch" is the most useful single field in the report: metadata working
        while media fails is the exact signature of a missing JS runtime (PLAN.md 0.3), and it has a
        completely different fix from "YouTube will not talk to us"."""
        result = canary_module.run_canary(
            config, source=CanarySource(fail_inspect=SourceError("HTTP 429 Too Many Requests"))
        )
        assert not result.ok
        assert result.stage == "inspect"
        assert "429" in (result.error or "")
        assert "canary FAILED at inspect" in result.summary

    def test_media_failure_reports_the_fetch_stage(self, config: Config):
        result = canary_module.run_canary(
            config, source=CanarySource(fail_fetch=SourceError("HTTP Error 403: Forbidden"))
        )
        assert not result.ok
        assert result.stage == "fetch"
        assert "403" in (result.error or "")

    def test_a_timeout_is_a_failure_not_an_exception(self, config: Config):
        result = canary_module.run_canary(
            config, source=CanarySource(fail_fetch=SourceTimeout("fetch timed out after 600s"))
        )
        assert not result.ok
        assert "SourceTimeout" in (result.error or "")

    def test_an_empty_container_fails_even_though_the_download_succeeded(self, config: Config):
        """yt-dlp and ffmpeg can exit 0 having produced nothing usable. A canary that trusted the
        return code would never sing."""
        result = canary_module.run_canary(config, source=CanarySource(size=0))
        assert not result.ok
        assert result.stage == "verify"
        assert "0 bytes" in (result.error or "")
        assert "not a network failure" in (result.error or "")

    def test_a_truncated_file_fails(self, config: Config):
        result = canary_module.run_canary(config, source=CanarySource(size=CANARY_MIN_BYTES - 1))
        assert not result.ok
        assert "truncated" in (result.error or "")

    def test_the_wrong_duration_fails(self, config: Config, monkeypatch):
        """The failure this catches is a partial download that is still a valid container — four
        seconds of a nineteen-second video, which nothing else in the pipeline notices."""
        monkeypatch.setattr(canary_module, "probe_duration", lambda path: 4.0)
        result = canary_module.run_canary(config, source=CanarySource(duration=19.0))
        assert not result.ok
        assert "19.0s" in (result.error or "") and "4.0s" in (result.error or "")

    def test_a_small_drift_is_accepted(self, config: Config, monkeypatch):
        """A 19-second video remuxed to AAC is legitimately a fraction of a second out. Failing for
        that would produce a nightly false alarm, and a false alarm a day is how alerting dies."""
        monkeypatch.setattr(canary_module, "probe_duration", lambda path: 19.4)
        assert canary_module.run_canary(config, source=CanarySource(duration=19.0)).ok

    def test_zero_duration_fails(self, config: Config, monkeypatch):
        monkeypatch.setattr(canary_module, "probe_duration", lambda path: 0.0)
        result = canary_module.run_canary(config, source=CanarySource())
        assert not result.ok
        assert "no audio in it" in (result.error or "")

    def test_a_local_failure_says_so(self, config: Config, monkeypatch):
        """An unwritable scratch directory is not YouTube's fault, and an error that implied it was
        would send somebody to debug the wrong half of the system."""

        def explode(path):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(canary_module, "_fresh_dir", explode)
        result = canary_module.run_canary(config, source=CanarySource())
        assert not result.ok
        assert "local failure, not an extraction failure" in (result.error or "")

    def test_a_missing_ffprobe_does_not_fail_the_canary(self, config: Config, monkeypatch):
        """ffprobe is a hard dependency of ingest, so its absence is a real problem — but it is
        ``tunebox doctor``'s problem. Failing here would blame YouTube for a missing local binary."""
        monkeypatch.setattr(canary_module, "probe_duration", lambda path: None)
        assert canary_module.run_canary(config, source=CanarySource()).ok


class TestSerialisation:
    def test_the_result_is_json_serialisable(self, config: Config):
        """It goes into the health log and into the alert payload verbatim."""
        result = canary_module.run_canary(config, source=CanarySource())
        text = json.dumps(result.as_dict())
        assert json.loads(text)["stage"] == "verify"


class TestTheDefaultVideo:
    def test_it_is_the_first_youtube_video(self):
        """"Me at the zoo": 19 seconds, and the least likely video on the platform to be deleted,
        region-locked or made members-only. A canary that fails because its video went away teaches
        nothing and trains its reader to ignore it."""
        from tunebox.config import DEFAULT_CANARY_URL

        assert "jNQXAC9IVRw" in DEFAULT_CANARY_URL

    def test_it_is_configurable(self):
        assert Config.from_env({"TUNEBOX_CANARY_URL": "https://youtu.be/abc"}).canary_url == (
            "https://youtu.be/abc"
        )


class TestVersionProbe:
    """Bound at import time so the autouse stub above cannot hide the real function."""

    def test_it_asks_the_binary_that_will_actually_run(self, config: Config):
        """Not ``importlib.metadata``: ``TUNEBOX_YTDLP`` can point at a different yt-dlp entirely,
        and the version that matters for a rollback is the one that produced the failing download."""
        version = real_ytdlp_version(config, argv=["python3", "-c", "print('2026.9.1')"])
        assert version == "2026.9.1"

    def test_a_broken_binary_is_none_not_a_crash(self, config: Config):
        assert real_ytdlp_version(config, argv=["/nonexistent/yt-dlp"]) is None
        assert real_ytdlp_version(config, argv=["python3", "-c", "raise SystemExit(3)"]) is None
