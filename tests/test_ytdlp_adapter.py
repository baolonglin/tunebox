"""The yt-dlp adapter's own logic: argv construction, progress parsing, output picking.

No test here reaches the network: ``subprocess.run`` is stubbed, and the few tests that do spawn
a process spawn *this interpreter* with an inline script, which is how timeout and kill behaviour
can be verified for real.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tunebox.config import Config
from tunebox.core.models import Progress
from tunebox.errors import InvalidURLError, SourceError, SourceTimeout
from tunebox.sources import ytdlp


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(music_root=tmp_path / "music", ytdlp=("yt-dlp-stub",))


@pytest.fixture
def source(config: Config) -> ytdlp.YtDlpSource:
    return ytdlp.YtDlpSource(config)


class TestArgv:
    """Every invocation must be an argv list with no shell, plus polite-client defaults."""

    def test_base_argv_pins_behaviour(self, source: ytdlp.YtDlpSource):
        argv = source._base_argv()
        assert argv[0] == "yt-dlp-stub"
        assert "--ignore-config" in argv  # host config must not change our behaviour
        assert "--sleep-requests" in argv  # PLAN.md 0.3: be a light-touch client

    def test_search_argv(self, source, monkeypatch):
        captured = _capture_run(monkeypatch, stdout=json.dumps({"entries": []}))
        source.search("kraftwerk autobahn", limit=5)
        argv = captured["argv"]

        assert "ytsearch5:kraftwerk autobahn" in argv
        assert "--flat-playlist" in argv and "--dump-single-json" in argv
        assert captured["shell"] is not True
        assert all(isinstance(part, str) for part in argv)

    def test_search_limit_is_clamped(self, source, monkeypatch):
        captured = _capture_run(monkeypatch, stdout=json.dumps({"entries": []}))
        source.search("x", limit=500)
        assert "ytsearch50:x" in captured["argv"]

    @pytest.mark.parametrize("query", ["", "   ", "line\nbreak"])
    def test_bad_queries_never_reach_a_subprocess(self, source, monkeypatch, query):
        _capture_run(monkeypatch, stdout="{}")
        with pytest.raises(SourceError):
            source.search(query)

    def test_inspect_uses_the_canonical_url_and_skips_download(self, source, monkeypatch, plain_upload_info):
        captured = _capture_run(monkeypatch, stdout=json.dumps(plain_upload_info))
        tags = source.inspect("https://youtu.be/iukUMRlaBBE?t=30")

        assert "https://www.youtube.com/watch?v=iukUMRlaBBE" in captured["argv"]
        assert "--skip-download" in captured["argv"]
        assert "--no-playlist" in captured["argv"]
        assert tags.artist == "Kraftwerk"

    def test_inspect_rejects_disallowed_hosts_before_running_anything(self, source, monkeypatch):
        captured = _capture_run(monkeypatch, stdout="{}")
        with pytest.raises(InvalidURLError):
            source.inspect("https://evil.example.com/watch?v=iukUMRlaBBE")
        assert "argv" not in captured


class TestRunJsonFailures:
    def test_non_zero_exit_becomes_source_error(self, source, monkeypatch):
        _capture_run(monkeypatch, returncode=1, stderr="ERROR: Video unavailable")
        with pytest.raises(SourceError, match="Video unavailable"):
            source.search("x")

    def test_unparseable_json_becomes_source_error(self, source, monkeypatch):
        _capture_run(monkeypatch, stdout="not json at all")
        with pytest.raises(SourceError, match="unparseable JSON"):
            source.search("x")

    def test_json_array_is_rejected(self, source, monkeypatch):
        _capture_run(monkeypatch, stdout="[]")
        with pytest.raises(SourceError, match="expected a JSON object"):
            source.search("x")

    def test_timeout_becomes_source_timeout(self, source, monkeypatch):
        def fake_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 1))

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(SourceTimeout):
            source.search("x")

    def test_missing_binary_is_reported_clearly(self, source, monkeypatch):
        def fake_run(argv, **kwargs):
            raise FileNotFoundError(argv[0])

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(SourceError, match="yt-dlp not found"):
            source.search("x")


class TestFetchArgv:
    def test_audio_selection_prefers_passthrough_aac(self, source, tmp_path, monkeypatch):
        argv = _capture_fetch(source, tmp_path, monkeypatch, audio_format="m4a")
        assert "ba[ext=m4a]/ba" in argv
        assert argv[argv.index("--audio-format") + 1] == "m4a"
        # our tag writer owns metadata, so yt-dlp must not embed its own
        assert "--no-embed-metadata" in argv
        assert "--embed-thumbnail" not in argv
        assert "--write-thumbnail" in argv
        assert "--audio-quality" not in argv

    def test_output_directory_is_passed_with_a_type_prefix(self, source, tmp_path, monkeypatch):
        """``--paths`` splits on the first colon; an unprefixed path with ':' would be misread."""
        argv = _capture_fetch(source, tmp_path, monkeypatch)
        assert argv[argv.index("--paths") + 1] == f"home:{tmp_path / 'work'}"

    def test_mp3_requests_best_quality_reencode(self, source, tmp_path, monkeypatch):
        argv = _capture_fetch(source, tmp_path, monkeypatch, audio_format="mp3")
        assert argv[argv.index("--audio-format") + 1] == "mp3"
        assert argv[argv.index("--audio-quality") + 1] == "0"

    def test_sponsorblock_is_not_used_by_default(self, source, tmp_path, monkeypatch):
        """It forces a re-encode, which contradicts the passthrough decision (PLAN.md 3.4)."""
        argv = _capture_fetch(source, tmp_path, monkeypatch)
        flags = [part for part in argv if part.startswith("--")]
        assert not any("sponsorblock" in flag for flag in flags)

    def test_missing_audio_output_is_an_error(self, source, tmp_path, monkeypatch):
        monkeypatch.setattr(source, "_run_streaming", lambda *a, **k: None)
        with pytest.raises(SourceError, match="no audio file"):
            source.fetch("iukUMRlaBBE", tmp_path / "work")


class TestProgressParsing:
    def test_complete_sample(self):
        progress = ytdlp._parse_progress("TBPROGRESS 500000 1000000 NA 250000.5 12")
        assert progress == Progress(
            percent=50.0, downloaded_bytes=500000, total_bytes=1000000, speed=250000.5, eta=12
        )

    def test_falls_back_to_the_estimate_when_total_is_unknown(self):
        progress = ytdlp._parse_progress("TBPROGRESS 250000 NA 1000000 NA NA")
        assert progress.percent == pytest.approx(25.0)
        assert progress.speed is None and progress.eta is None

    def test_unknown_total_yields_no_percentage_rather_than_a_wrong_one(self):
        progress = ytdlp._parse_progress("TBPROGRESS 250000 NA NA NA NA")
        assert progress.percent is None
        assert progress.downloaded_bytes == 250000

    def test_percentage_is_capped(self):
        assert ytdlp._parse_progress("TBPROGRESS 1200 1000 NA NA NA").percent == 100.0

    @pytest.mark.parametrize("line", ["TBPROGRESS", "TBPROGRESS 1 2 3", "TBPROGRESS a b c d e f g"])
    def test_malformed_lines_are_ignored(self, line):
        assert ytdlp._parse_progress(line) is None


class TestPickOutput:
    def test_picks_the_largest_audio_file(self, tmp_path: Path):
        (tmp_path / "track.m4a").write_bytes(b"x" * 100)
        (tmp_path / "fragment.m4a").write_bytes(b"x" * 10)
        assert ytdlp._pick(tmp_path, ytdlp.AUDIO_EXTENSIONS).name == "track.m4a"

    def test_ignores_partial_downloads(self, tmp_path: Path):
        (tmp_path / "track.m4a.part").write_bytes(b"x" * 1000)
        (tmp_path / "track.m4a").write_bytes(b"x" * 10)
        assert ytdlp._pick(tmp_path, ytdlp.AUDIO_EXTENSIONS).name == "track.m4a"

    def test_nothing_matching(self, tmp_path: Path):
        assert ytdlp._pick(tmp_path, ytdlp.AUDIO_EXTENSIONS) is None


class TestPickCover:
    def test_embeddable_format_wins_over_a_larger_webp(self, tmp_path: Path):
        """Only JPEG/PNG can be embedded, so a leftover .webp must not cost us the artwork."""
        (tmp_path / "track.webp").write_bytes(b"x" * 10_000)
        (tmp_path / "track.jpg").write_bytes(b"x" * 10)
        assert ytdlp._pick_cover(tmp_path).name == "track.jpg"

    def test_png_is_accepted(self, tmp_path: Path):
        (tmp_path / "track.png").write_bytes(b"x")
        assert ytdlp._pick_cover(tmp_path).name == "track.png"

    def test_no_embeddable_cover(self, tmp_path: Path):
        (tmp_path / "track.webp").write_bytes(b"x")
        (tmp_path / "track.m4a").write_bytes(b"x")
        assert ytdlp._pick_cover(tmp_path) is None


class TestFetchTimeout:
    def test_a_silent_hang_is_killed_at_the_timeout(self, tmp_path: Path):
        """A stalled extraction produces no output at all, so the timeout cannot be output-driven."""
        config = Config(
            music_root=tmp_path / "music",
            ytdlp=(sys.executable, "-c", "import time; time.sleep(60)"),
            fetch_timeout=2,
        )
        source = ytdlp.YtDlpSource(config)

        started = time.monotonic()
        with pytest.raises(SourceTimeout):
            source.fetch("iukUMRlaBBE", tmp_path / "work")
        assert time.monotonic() - started < 20

    def test_a_child_ignoring_sigterm_is_still_killed(self, tmp_path: Path, monkeypatch):
        config = Config(
            music_root=tmp_path / "music",
            ytdlp=(
                sys.executable,
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
            ),
            fetch_timeout=2,
        )
        started: list = []
        original_popen = subprocess.Popen

        def tracking_popen(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            started.append(process)
            return process

        monkeypatch.setattr(subprocess, "Popen", tracking_popen)
        with pytest.raises(SourceTimeout):
            ytdlp.YtDlpSource(config).fetch("iukUMRlaBBE", tmp_path / "work")

        assert started and started[0].poll() is not None  # reaped, not orphaned

    def test_a_fast_successful_child_is_not_reported_as_a_timeout(self, tmp_path: Path):
        """The watchdog must fire only on a *running* child.

        Regression test: the parent can still be inside a slow progress callback when the
        deadline passes, even though the download already finished successfully.
        """
        script = (
            "import sys;"
            "d=sys.argv[sys.argv.index('--paths')+1].split('home:',1)[-1];"
            "print('TBPROGRESS 100 100 NA 1.0 0');"
            "open(d+'/track.m4a','wb').write(b'audio')"
        )
        config = Config(
            music_root=tmp_path / "music",
            ytdlp=(sys.executable, "-c", script),
            fetch_timeout=1,
        )
        work = tmp_path / "work"

        def slow_callback(progress):
            time.sleep(1.5)  # outlives the timeout on purpose

        media = ytdlp.YtDlpSource(config).fetch("iukUMRlaBBE", work, on_progress=slow_callback)
        assert media.audio_path.name == "track.m4a"

    def test_non_zero_exit_from_a_fetch_is_reported(self, tmp_path: Path):
        config = Config(
            music_root=tmp_path / "music",
            ytdlp=(sys.executable, "-c", "import sys; print('ERROR: nope'); sys.exit(1)"),
        )
        with pytest.raises(SourceError, match="nope"):
            ytdlp.YtDlpSource(config).fetch("iukUMRlaBBE", tmp_path / "work")

    def test_progress_lines_are_forwarded_from_a_real_subprocess(self, tmp_path: Path):
        script = (
            "print('TBPROGRESS 500 1000 NA 100.0 5');"
            "print('[download] something human-readable')"
        )
        config = Config(music_root=tmp_path / "music", ytdlp=(sys.executable, "-c", script))
        samples: list = []
        work = tmp_path / "work"
        work.mkdir(parents=True)
        (work / "track.m4a").write_bytes(b"audio")

        ytdlp.YtDlpSource(config).fetch("iukUMRlaBBE", work, on_progress=samples.append)
        assert [s.percent for s in samples] == [50.0]


# ------------------------------------------------------------------- helpers


def _capture_run(monkeypatch, *, stdout: str = "{}", stderr: str = "", returncode: int = 0) -> dict:
    """Replace ``subprocess.run`` and record how it was called."""
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["shell"] = kwargs.get("shell")
        captured["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return captured


def _capture_fetch(source, tmp_path: Path, monkeypatch, audio_format: str = "m4a") -> list[str]:
    """Run ``fetch`` with the streaming call stubbed out, returning the argv it would have run."""
    captured: dict = {}
    work = tmp_path / "work"

    def fake_streaming(args, *, timeout, on_progress, what):
        captured["argv"] = [*source._base_argv(), *args]
        (work / "track.m4a").write_bytes(b"audio")

    monkeypatch.setattr(source, "_run_streaming", fake_streaming)
    source.fetch("iukUMRlaBBE", work, audio_format=audio_format)
    return captured["argv"]
