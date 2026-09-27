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


class TestServiceConfig:
    """Phase 2 settings. The defaults are the security posture, so they are tested as such."""

    def test_defaults_are_loopback_and_tokenless(self):
        config = Config()
        assert config.api_host == "127.0.0.1"
        assert config.api_port == 8080
        assert config.api_token is None  # so `serve` refuses rather than running open
        assert config.worker_concurrency == 1
        assert config.job_max_attempts == 3

    def test_the_queue_database_lives_under_the_music_root(self, tmp_path: Path):
        config = Config(music_root=tmp_path)
        assert config.database_path == tmp_path / ".tunebox" / "jobs.db"

    def test_the_queue_database_can_be_moved_off_the_library_volume(self, tmp_path: Path):
        config = Config.from_env({"TUNEBOX_DB": str(tmp_path / "state" / "q.db")})
        assert config.database_path == tmp_path / "state" / "q.db"

    def test_service_settings_come_from_the_environment(self, tmp_path: Path):
        config = Config.from_env(
            {
                "TUNEBOX_API_HOST": "192.168.50.20",
                "TUNEBOX_API_PORT": "9000",
                "TUNEBOX_TOKEN": "a-token-long-enough-to-pass",
                "TUNEBOX_WORKERS": "2",
                "TUNEBOX_JOB_ATTEMPTS": "5",
                "TUNEBOX_JOB_BACKOFF": "2.5",
            }
        )
        assert (config.api_host, config.api_port) == ("192.168.50.20", 9000)
        assert config.worker_concurrency == 2
        assert config.job_max_attempts == 5
        assert config.job_retry_backoff == 2.5
        assert config.validate_for_serve() == "a-token-long-enough-to-pass"

    def test_concurrency_above_the_cap_is_refused(self):
        """PLAN.md 3.1 caps this at 1–2 to stay a light-touch client, so it is enforced."""
        with pytest.raises(ConfigError, match="between 1 and 2"):
            Config(worker_concurrency=8)
        with pytest.raises(ConfigError, match="between 1 and 2"):
            Config(worker_concurrency=0)

    def test_a_bad_boolean_env_is_rejected_with_a_useful_message(self):
        with pytest.raises(ConfigError, match="TUNEBOX_ALLOW_ALL_INTERFACES"):
            Config.from_env({"TUNEBOX_ALLOW_ALL_INTERFACES": "maybe"})

    def test_an_out_of_range_port_is_refused(self):
        with pytest.raises(ConfigError, match="port out of range"):
            Config(api_port=70000)

    def test_backoff_grows_and_then_stops_growing(self):
        config = Config(job_retry_backoff=10.0, job_retry_backoff_max=25.0)
        assert [config.retry_delay(n) for n in (1, 2, 3, 9)] == [10.0, 20.0, 25.0, 25.0]

    def test_a_zero_backoff_means_retry_immediately(self):
        assert Config(job_retry_backoff=0.0).retry_delay(3) == 0.0


class TestCLI:
    def test_parser_exposes_the_phase_1_commands(self):
        parser = build_parser()
        for argv in (["search", "x"], ["ingest", "x"], ["doctor"]):
            assert parser.parse_args(argv).command == argv[0]

    def test_parser_exposes_serve(self):
        assert build_parser().parse_args(["serve"]).command == "serve"

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


class TestServe:
    """`tunebox serve` wiring. No socket is ever opened: uvicorn.run is stubbed."""

    @pytest.fixture
    def uvicorn_calls(self, monkeypatch) -> list[dict]:
        calls: list[dict] = []

        def fake_run(app, **kwargs):
            calls.append({"app": app, **kwargs})

        monkeypatch.setattr("uvicorn.run", fake_run)
        return calls

    def test_serve_refuses_to_start_without_a_token(self, monkeypatch, capsys, tmp_path):
        """An open endpoint that runs yt-dlp and ffmpeg is the one failure mode PLAN.md 3.2
        refuses outright — so this is an error, not a warning."""
        monkeypatch.delenv("TUNEBOX_TOKEN", raising=False)
        code = main(["--music-root", str(tmp_path / "music"), "serve"])
        err = capsys.readouterr().err

        assert code == 1
        assert "TUNEBOX_TOKEN" in err
        assert "Traceback" not in err

    def test_serve_refuses_the_wildcard_address(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("TUNEBOX_TOKEN", "a-token-long-enough-to-pass")
        code = main(["--music-root", str(tmp_path / "music"), "serve", "--host", "0.0.0.0"])
        assert code == 1
        assert "every interface" in capsys.readouterr().err

    def test_serve_binds_the_configured_address(
        self, monkeypatch, capsys, tmp_path, uvicorn_calls
    ):
        monkeypatch.setenv("TUNEBOX_TOKEN", "a-token-long-enough-to-pass")
        code = main(
            [
                "--music-root",
                str(tmp_path / "music"),
                "serve",
                "--host",
                "192.168.50.20",
                "--port",
                "9123",
                "--workers",
                "2",
            ]
        )
        capsys.readouterr()

        assert code == 0
        assert (uvicorn_calls[0]["host"], uvicorn_calls[0]["port"]) == ("192.168.50.20", 9123)
        assert uvicorn_calls[0]["app"].state.config.worker_concurrency == 2
        # Access logs are the audit trail PLAN.md 3.2 asks for on an untrusted LAN.
        assert uvicorn_calls[0]["access_log"] is True

    def test_serve_defaults_to_loopback(self, monkeypatch, capsys, tmp_path, uvicorn_calls):
        monkeypatch.setenv("TUNEBOX_TOKEN", "a-token-long-enough-to-pass")
        monkeypatch.delenv("TUNEBOX_API_HOST", raising=False)
        main(["--music-root", str(tmp_path / "music"), "serve"])
        capsys.readouterr()
        assert uvicorn_calls[0]["host"] == "127.0.0.1"

    def test_serve_reports_where_it_listens_on_stderr(
        self, monkeypatch, capsys, tmp_path, uvicorn_calls
    ):
        """stdout stays clean: it is where the other commands' JSON goes."""
        monkeypatch.setenv("TUNEBOX_TOKEN", "a-token-long-enough-to-pass")
        main(["--music-root", str(tmp_path / "music"), "serve", "--port", "9124"])
        captured = capsys.readouterr()

        assert "9124" in captured.err
        assert "X-Tunebox-Token" in captured.err
        assert captured.out == ""

    def test_serve_rejects_a_concurrency_above_the_cap(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("TUNEBOX_TOKEN", "a-token-long-enough-to-pass")
        code = main(["--music-root", str(tmp_path / "music"), "serve", "--workers", "9"])
        assert code == 1
        assert "between 1 and 2" in capsys.readouterr().err


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
