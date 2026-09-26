"""Config and CLI wiring. No subprocess, no network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tunebox.cli import build_parser, main
from tunebox.config import Config, resolve_ytdlp_command
from tunebox.errors import ConfigError


class TestConfig:
    def test_defaults(self):
        config = Config()
        assert config.audio_format == "m4a"
        assert config.music_root.name == "tunebox"
        assert config.scratch_root.name == ".tunebox-tmp"

    def test_from_env(self, tmp_path: Path):
        config = Config.from_env(
            {
                "TUNEBOX_MUSIC_ROOT": str(tmp_path / "lib"),
                "TUNEBOX_AUDIO_FORMAT": "MP3",
                "TUNEBOX_SEARCH_LIMIT": "7",
                "TUNEBOX_FETCH_TIMEOUT": "42",
                "TUNEBOX_SLEEP_REQUESTS": "2.5",
            }
        )
        assert config.music_root == tmp_path / "lib"
        assert config.audio_format == "mp3"
        assert config.search_limit == 7
        assert config.fetch_timeout == 42
        assert config.sleep_requests == 2.5

    def test_unsupported_format_is_rejected_at_construction(self):
        with pytest.raises(ConfigError):
            Config(audio_format="flac")

    def test_non_numeric_env_is_rejected_with_a_useful_message(self):
        with pytest.raises(ConfigError, match="TUNEBOX_FETCH_TIMEOUT"):
            Config.from_env({"TUNEBOX_FETCH_TIMEOUT": "soon"})

    def test_scratch_root_is_inside_the_music_root(self, tmp_path: Path):
        """Keeps the final move same-filesystem, so placement is an atomic rename."""
        config = Config(music_root=tmp_path)
        assert config.scratch_root.parent == tmp_path

    def test_ytdlp_command_prefers_the_pinned_module(self):
        assert resolve_ytdlp_command({})[-2:] == ("-m", "yt_dlp")

    def test_ytdlp_command_override(self):
        assert resolve_ytdlp_command({"TUNEBOX_YTDLP": "/opt/bin/yt-dlp --verbose"}) == (
            "/opt/bin/yt-dlp",
            "--verbose",
        )


class TestCLI:
    def test_parser_exposes_the_phase_1_commands(self):
        parser = build_parser()
        for argv in (["search", "x"], ["ingest", "x"], ["doctor"]):
            assert parser.parse_args(argv).command == argv[0]

    def test_a_command_is_required(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_search_joins_multi_word_queries(self):
        assert build_parser().parse_args(["search", "kraftwerk", "autobahn"]).query == [
            "kraftwerk",
            "autobahn",
        ]

    def test_ingest_rejects_unsupported_formats(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["ingest", "x", "--format", "flac"])

    def test_search_prints_json_and_exits_2_on_no_results(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr("tunebox.cli.YtDlpSource", lambda config: _StubSource([]))
        code = main(["--music-root", str(tmp_path), "search", "nothing"])
        assert code == 2
        assert json.loads(capsys.readouterr().out) == []

    def test_search_prints_results_as_json(self, monkeypatch, capsys, tmp_path, search_payload):
        from tunebox.sources.ytdlp_tags import search_results_from_playlist

        results = search_results_from_playlist(search_payload)
        monkeypatch.setattr("tunebox.cli.YtDlpSource", lambda config: _StubSource(results))
        code = main(["--music-root", str(tmp_path), "search", "kraftwerk"])
        payload = json.loads(capsys.readouterr().out)

        assert code == 0
        assert [item["id"] for item in payload] == ["iukUMRlaBBE", "x-G28iyPtz0"]

    def test_invalid_url_is_a_clean_error_not_a_traceback(self, capsys, tmp_path):
        code = main(["--music-root", str(tmp_path), "ingest", "https://evil.example.com/x"])
        assert code == 1
        assert "host not allowed" in capsys.readouterr().err

    def test_filesystem_errors_are_clean_errors_not_tracebacks(self, monkeypatch, capsys, tmp_path):
        """A music root that is actually a file is user error, and must read like it."""
        not_a_directory = tmp_path / "library"
        not_a_directory.write_bytes(b"")
        monkeypatch.setattr("tunebox.cli.YtDlpSource", lambda config: _StubSource([], ingestable=True))

        code = main(["--music-root", str(not_a_directory), "ingest", "iukUMRlaBBE"])
        err = capsys.readouterr().err

        assert code == 1
        assert err.startswith("error:") or "error:" in err
        assert "Traceback" not in err

    def test_music_root_flag_overrides_the_environment(self, monkeypatch, capsys, tmp_path):
        seen: dict = {}

        def stub(config):
            seen["root"] = config.music_root
            seen["format"] = config.audio_format
            return _StubSource([])

        monkeypatch.setenv("TUNEBOX_MUSIC_ROOT", str(tmp_path / "from-env"))
        monkeypatch.setattr("tunebox.cli.YtDlpSource", stub)
        main(["--music-root", str(tmp_path / "from-flag"), "search", "x"])
        capsys.readouterr()

        assert seen["root"] == tmp_path / "from-flag"

    def test_doctor_reports_each_dependency(self, monkeypatch, capsys, tmp_path):
        # Stub the probes so the suite spawns no subprocess and the result does not depend on
        # what happens to be installed on the machine running the tests.
        monkeypatch.setattr("tunebox.cli._probe_version", lambda argv: "2026.08.19")
        monkeypatch.setattr("tunebox.cli.shutil.which", lambda name: f"/usr/local/bin/{name}")

        code = main(["--music-root", str(tmp_path / "music"), "doctor"])
        out = capsys.readouterr().out

        for expected in ("yt-dlp", "ffmpeg", "ffprobe", "deno", "yt-dlp-ejs", "mutagen", "music root"):
            assert expected in out
        assert code == 0

    def test_doctor_fails_when_a_required_binary_is_missing(self, monkeypatch, capsys, tmp_path):
        """Missing deno means every download 403s (PLAN.md 0.3), so it is not a warning."""
        monkeypatch.setattr("tunebox.cli._probe_version", lambda argv: "2026.08.19")
        monkeypatch.setattr(
            "tunebox.cli.shutil.which", lambda name: None if name == "deno" else f"/bin/{name}"
        )

        code = main(["--music-root", str(tmp_path / "music"), "doctor"])
        out = capsys.readouterr().out

        assert code == 1
        assert "[!!] deno" in out

    def test_doctor_reports_an_unwritable_music_root(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr("tunebox.cli._probe_version", lambda argv: "2026.08.19")
        monkeypatch.setattr("tunebox.cli.shutil.which", lambda name: f"/bin/{name}")
        blocked = tmp_path / "library"
        blocked.write_bytes(b"")  # a file where a directory is expected

        code = main(["--music-root", str(blocked), "doctor"])
        assert code == 1
        assert "NOT writable" in capsys.readouterr().out


class _StubSource:
    name = "stub"

    def __init__(self, results, ingestable: bool = False):
        self._results = results
        self._ingestable = ingestable

    def search(self, query, limit=None):
        return self._results

    def inspect(self, url):
        if not self._ingestable:  # pragma: no cover - guards the search-only tests
            raise AssertionError("should not be called")
        from tunebox.core.models import TrackTags

        return TrackTags(title="Autobahn", artist="Kraftwerk", source_id="iukUMRlaBBE")

    def fetch(self, *a, **k):  # pragma: no cover - never reached: scratch creation fails first
        raise AssertionError("should not be called")
