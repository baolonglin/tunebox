"""Config and CLI wiring. No subprocess, no network."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tunebox.cli import _probe_js_runtimes, build_parser, main
from tunebox.config import (
    DEFAULT_JS_RUNTIMES,
    Config,
    resolve_js_runtimes,
    resolve_ytdlp_command,
)
from tunebox.core.models import QueueItem
from tunebox.errors import ConfigError

from fakes import SOUNDBAR, FakePlayer


class TestConfig:
    def test_defaults(self):
        config = Config()
        assert config.audio_format == "m4a"
        assert config.music_root.name == "tunebox"
        assert config.scratch_root.name == ".tunebox-tmp"


class TestJsRuntimeConfig:
    """``TUNEBOX_JS_RUNTIMES`` — the setting that makes a Raspberry Pi installable.

    yt-dlp enables **only deno** by default and picks the highest-priority runtime that is actually
    installed. There is no official deno build for 32-bit ARM, so without naming the alternatives a
    32-bit Raspberry Pi has no JS runtime, and without a JS runtime every media download fails with
    HTTP 403 (PLAN.md 0.3).
    """

    def test_the_default_adds_fallbacks_below_deno(self):
        """Not *instead of* deno: yt-dlp orders by its own priority, so naming these means "deno if
        you have it, otherwise node, otherwise quickjs, otherwise bun"."""
        assert resolve_js_runtimes({}) == DEFAULT_JS_RUNTIMES
        assert "deno" not in DEFAULT_JS_RUNTIMES
        assert DEFAULT_JS_RUNTIMES[0] == "node"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("node", ("node",)),
            ("node,quickjs", ("node", "quickjs")),
            ("node quickjs bun", ("node", "quickjs", "bun")),
            ("  node ,  bun ", ("node", "bun")),
        ],
    )
    def test_it_is_parsed_from_a_list(self, raw, expected):
        assert resolve_js_runtimes({"TUNEBOX_JS_RUNTIMES": raw}) == expected

    def test_empty_means_deno_only(self):
        """An escape hatch for a host with an unusable runtime on PATH that must not be tried."""
        assert resolve_js_runtimes({"TUNEBOX_JS_RUNTIMES": ""}) == ()
        assert resolve_js_runtimes({"TUNEBOX_JS_RUNTIMES": "   "}) == ()

    def test_a_typo_is_a_startup_error(self):
        """Otherwise it is a download that fails at 403 hours later, for a reason nothing reports."""
        with pytest.raises(ConfigError, match="unknown JS runtime"):
            resolve_js_runtimes({"TUNEBOX_JS_RUNTIMES": "deno,dino"})
        with pytest.raises(ConfigError, match="unknown JS runtime"):
            Config(js_runtimes=("dino",))

    def test_they_reach_every_yt_dlp_invocation(self, tmp_path: Path):
        from tunebox.sources.ytdlp import YtDlpSource

        argv = YtDlpSource(
            Config(music_root=tmp_path, ytdlp=("yt-dlp",), js_runtimes=("node", "bun"))
        )._base_argv()

        assert argv.count("--js-runtimes") == 2
        assert argv[argv.index("--js-runtimes") + 1] == "node"
        # Order is preserved, because yt-dlp reads it as a priority list.
        assert argv[len(argv) - 1] == "bun"

    def test_deno_only_adds_no_flag(self, tmp_path: Path):
        """So a ``TUNEBOX_YTDLP`` override pointing at a yt-dlp older than ``--js-runtimes`` has a
        way out that is not "patch tunebox"."""
        from tunebox.sources.ytdlp import YtDlpSource

        argv = YtDlpSource(
            Config(music_root=tmp_path, ytdlp=("yt-dlp",), js_runtimes=())
        )._base_argv()
        assert "--js-runtimes" not in argv


class TestConfigFromEnv:
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
        monkeypatch.setattr("tunebox.cli._probe_js_runtimes", lambda config: "deno-2.9.6")

        code = main(["--music-root", str(tmp_path / "music"), "doctor"])
        out = capsys.readouterr().out

        for expected in (
            "yt-dlp",
            "ffmpeg",
            "ffprobe",
            "JS runtime",
            "yt-dlp-ejs",
            "mutagen",
            "music root",
        ):
            assert expected in out
        assert code == 0

    def test_doctor_names_the_runtime_yt_dlp_found(self, monkeypatch, capsys, tmp_path):
        """Not "deno", because on a 32-bit Raspberry Pi there is no deno build and the answer is
        node. Whatever yt-dlp says it will use is what gets printed."""
        monkeypatch.setattr("tunebox.cli._probe_version", lambda argv: "2026.08.19")
        monkeypatch.setattr("tunebox.cli.shutil.which", lambda name: f"/bin/{name}")
        monkeypatch.setattr("tunebox.cli._probe_js_runtimes", lambda config: "node-22.4.0")

        code = main(["--music-root", str(tmp_path / "music"), "doctor"])
        out = capsys.readouterr().out

        assert code == 0
        assert "[ok] JS runtime" in out
        assert "node-22.4.0" in out

    def test_doctor_fails_when_no_js_runtime_is_usable(self, monkeypatch, capsys, tmp_path):
        """No JS runtime means every download 403s (PLAN.md 0.3), so it is not a warning."""
        monkeypatch.setattr("tunebox.cli._probe_version", lambda argv: "2026.08.19")
        monkeypatch.setattr("tunebox.cli.shutil.which", lambda name: f"/bin/{name}")
        monkeypatch.setattr("tunebox.cli._probe_js_runtimes", lambda config: None)

        code = main(["--music-root", str(tmp_path / "music"), "doctor"])
        out = capsys.readouterr().out

        assert code == 1
        assert "[!!] JS runtime" in out
        assert "403" in out

    def test_doctor_fails_when_a_required_binary_is_missing(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr("tunebox.cli._probe_version", lambda argv: "2026.08.19")
        monkeypatch.setattr("tunebox.cli._probe_js_runtimes", lambda config: "deno-2.9.6")
        monkeypatch.setattr(
            "tunebox.cli.shutil.which", lambda name: None if name == "ffmpeg" else f"/bin/{name}"
        )

        code = main(["--music-root", str(tmp_path / "music"), "doctor"])
        out = capsys.readouterr().out

        assert code == 1
        assert "[!!] ffmpeg" in out

    def test_doctor_reports_an_unwritable_music_root(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr("tunebox.cli._probe_version", lambda argv: "2026.08.19")
        monkeypatch.setattr("tunebox.cli.shutil.which", lambda name: f"/bin/{name}")
        monkeypatch.setattr("tunebox.cli._probe_js_runtimes", lambda config: "deno-2.9.6")
        blocked = tmp_path / "library"
        blocked.write_bytes(b"")  # a file where a directory is expected

        code = main(["--music-root", str(blocked), "doctor"])
        assert code == 1
        assert "NOT writable" in capsys.readouterr().out


class TestJsRuntimeProbe:
    """``_probe_js_runtimes`` reads yt-dlp's own answer rather than looking for binaries itself.

    Duplicating yt-dlp's version thresholds here would mean two answers to one question, and
    ``doctor`` would print the wrong one exactly when it mattered — on a host with node 18 installed,
    which is present, on PATH, and useless.
    """

    @staticmethod
    def config(tmp_path, **overrides) -> Config:
        return Config(music_root=tmp_path, ytdlp=("yt-dlp",), **overrides)

    def fake_run(self, monkeypatch, *, stderr: str, returncode: int = 1):
        seen: dict = {}

        def run(argv, **kwargs):
            seen["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, returncode, stdout="", stderr=stderr)

        monkeypatch.setattr("tunebox.cli.subprocess.run", run)
        return seen

    def test_it_parses_the_debug_banner(self, monkeypatch, tmp_path):
        self.fake_run(monkeypatch, stderr="[debug] JS runtimes: deno-2.9.6, node-23.7.0\n")
        assert _probe_js_runtimes(self.config(tmp_path)) == "deno-2.9.6, node-23.7.0"

    def test_a_failing_exit_code_is_not_a_failed_probe(self, monkeypatch, tmp_path):
        """The probe URL has nothing to download, so yt-dlp always exits non-zero. The banner is
        printed before it ever looks at an extractor, and the banner is all we read."""
        self.fake_run(monkeypatch, stderr="[debug] JS runtimes: deno-2.9.6\n", returncode=1)
        assert _probe_js_runtimes(self.config(tmp_path)) == "deno-2.9.6"

    @pytest.mark.parametrize("banner", ["[debug] JS runtimes: none\n", "[debug] JS runtimes: \n"])
    def test_no_runtime_is_none(self, monkeypatch, tmp_path, banner):
        self.fake_run(monkeypatch, stderr=banner)
        assert _probe_js_runtimes(self.config(tmp_path)) is None

    def test_a_yt_dlp_that_says_nothing_is_none(self, monkeypatch, tmp_path):
        """An older yt-dlp with no such banner must read as "cannot tell", not as a crash."""
        self.fake_run(monkeypatch, stderr="[debug] Command-line config: ['-v']\n")
        assert _probe_js_runtimes(self.config(tmp_path)) is None

    def test_it_offers_the_configured_runtimes(self, monkeypatch, tmp_path):
        """Otherwise the probe would report deno-only while ingest used the fallbacks, which is the
        one disagreement a diagnostic must not have."""
        seen = self.fake_run(monkeypatch, stderr="[debug] JS runtimes: node-22.4.0\n")
        _probe_js_runtimes(self.config(tmp_path, js_runtimes=("node", "bun")))

        argv = seen["argv"]
        assert argv[:2] == ["yt-dlp", "--ignore-config"]
        assert "--js-runtimes" in argv
        assert argv[argv.index("--js-runtimes") + 1] == "node"
        assert argv.count("--js-runtimes") == 2

    def test_the_probe_url_is_resolved_locally(self, monkeypatch, tmp_path):
        """``doctor`` must not make a network request; a ``file://`` URL is handled by the generic
        extractor without one."""
        seen = self.fake_run(monkeypatch, stderr="[debug] JS runtimes: deno-2.9.6\n")
        _probe_js_runtimes(self.config(tmp_path))
        assert seen["argv"][-1].startswith("file://")

    def test_an_unrunnable_yt_dlp_is_none_not_a_traceback(self, monkeypatch, tmp_path):
        def boom(argv, **kwargs):
            raise OSError("no such binary")

        monkeypatch.setattr("tunebox.cli.subprocess.run", boom)
        assert _probe_js_runtimes(self.config(tmp_path)) is None


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

    def test_serve_says_where_the_remote_is(self, monkeypatch, capsys, tmp_path, uvicorn_calls):
        """The Phase 4 exit criterion is one icon on a phone, and the banner is where somebody
        standing at the host finds out what to type into it."""
        monkeypatch.setenv("TUNEBOX_TOKEN", "a-token-long-enough-to-pass")
        main(["--music-root", str(tmp_path / "music"), "serve", "--port", "9125"])
        err = capsys.readouterr().err

        assert "remote:" in err
        assert "http://127.0.0.1:9125/" in err
        # The token is never printed, here or anywhere: this banner goes to a journal.
        assert "a-token-long-enough-to-pass" not in err

    def test_serve_says_so_when_the_remote_is_off(self, monkeypatch, capsys, tmp_path, uvicorn_calls):
        monkeypatch.setenv("TUNEBOX_TOKEN", "a-token-long-enough-to-pass")
        monkeypatch.setenv("TUNEBOX_UI", "0")
        main(["--music-root", str(tmp_path / "music"), "serve"])
        err = capsys.readouterr().err

        assert "remote:   disabled" in err
        assert uvicorn_calls[0]["app"].state.config.ui_enabled is False

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


class TestPlayerCLI:
    """``tunebox player …`` — PLAN.md Phase 3.

    The adapter is patched out at :func:`tunebox.cli._player`, because what is being tested here is
    argparse wiring and output shape; the adapter's own behaviour is covered against a real HTTP
    server in ``test_player.py``.
    """

    @pytest.fixture
    def player(self, monkeypatch) -> FakePlayer:
        fake = FakePlayer()
        monkeypatch.setattr("tunebox.cli._player", lambda config: fake)
        return fake

    def _json(self, capsys) -> object:
        return json.loads(capsys.readouterr().out)

    def test_status_prints_the_session_as_json(self, player: FakePlayer, capsys):
        player.state = "play"
        assert main(["player", "status"]) == 0
        body = self._json(capsys)
        assert body["state"] == "play"
        assert body["outputs"][0]["name"] == SOUNDBAR

    def test_outputs_lists_what_the_daemon_can_see(self, player: FakePlayer, capsys):
        assert main(["player", "outputs"]) == 0
        assert [o["name"] for o in self._json(capsys)] == [SOUNDBAR]

    def test_outputs_says_something_useful_when_the_list_is_empty(self, player: FakePlayer, capsys):
        player.present = False
        assert main(["player", "outputs"]) == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out) == []
        assert "powered and on" in captured.err

    def test_select_enables_the_configured_speaker_and_reads_it_back(
        self, player: FakePlayer, capsys
    ):
        assert main(["player", "select"]) == 0
        assert player.selected is True
        assert self._json(capsys)["selected"] is True

    def test_select_honours_an_explicit_volume(self, player: FakePlayer, capsys):
        assert main(["player", "select", "--volume", "45"]) == 0
        capsys.readouterr()
        assert player.selections == [(player.output_id, 45)]

    def test_select_reports_an_absent_speaker_as_an_error_exit(self, player: FakePlayer, capsys):
        player.present = False
        assert main(["player", "select"]) == 1
        assert "no output named" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "verb,expected",
        [("pause", "pause"), ("stop", "stop"), ("next", "next"), ("prev", "previous")],
    )
    def test_the_transport_verbs_map_to_one_command_each(
        self, player: FakePlayer, capsys, verb: str, expected: str
    ):
        assert main(["player", verb]) == 0
        capsys.readouterr()
        assert player.commands == [expected]

    def test_play_with_no_arguments_resumes(self, player: FakePlayer, capsys):
        assert main(["player", "play"]) == 0
        capsys.readouterr()
        assert player.commands == ["play"]
        assert player.rescans == 0, "resuming must not walk the library"

    def test_play_with_files_replaces_the_queue_and_starts(
        self, player: FakePlayer, capsys, tmp_path: Path
    ):
        track = tmp_path / "Autobahn.m4a"
        track.write_bytes(b"")
        player.items = [
            QueueItem(id="99", position=0, title="Something else", path="/old.m4a")
        ]

        assert main(["player", "play", str(track)]) == 0

        body = self._json(capsys)
        assert [item["title"] for item in body["queued"]] == ["Autobahn"]
        assert [item.title for item in player.items] == ["Autobahn"], "queue replaced, not appended"
        assert player.state == "play"

    def test_a_file_already_in_the_library_is_played_without_a_rescan(
        self, player: FakePlayer, capsys, tmp_path: Path
    ):
        """Rescanning first is not just wasteful, it is wrong: a scan deletes and re-inserts rows,
        so a track id read during one is rejected by the time the queue-add lands. Measured on
        hardware as a 500 from OwnTone."""
        track = tmp_path / "Known.m4a"
        track.write_bytes(b"")

        assert main(["player", "play", str(track)]) == 0
        capsys.readouterr()

        assert player.rescans == 0
        assert player.scan_waits == 0

    def test_a_file_the_player_has_never_seen_triggers_a_rescan_and_waits_for_it(
        self, player: FakePlayer, capsys, tmp_path: Path
    ):
        """The "play what I just ingested" case, which is the only reason the rescan is here."""
        track = tmp_path / "Fresh.m4a"
        track.write_bytes(b"")
        player.indexed = set()  # the player knows nothing yet

        def index_on_rescan() -> None:
            player.indexed = {str(track)}

        original = player.rescan

        def rescan() -> None:
            original()
            index_on_rescan()

        player.rescan = rescan  # type: ignore[method-assign]

        assert main(["player", "play", str(track)]) == 0
        capsys.readouterr()

        assert player.rescans == 1
        assert player.scan_waits == 1, "ids are unstable until the scan finishes"
        assert [item.title for item in player.items] == ["Fresh"]

    def test_a_file_that_never_appears_is_a_clean_error(
        self, player: FakePlayer, capsys, tmp_path: Path
    ):
        track = tmp_path / "Ghost.m4a"
        track.write_bytes(b"")
        player.indexed = set()  # a rescan will not help: it is outside OwnTone's directories

        assert main(["player", "play", "--wait", "0", str(track)]) == 1
        assert "not in the player's library" in capsys.readouterr().err

    def test_play_with_add_appends_and_does_not_start(
        self, player: FakePlayer, capsys, tmp_path: Path
    ):
        existing = QueueItem(id="1", position=0, title="First", path="/first.m4a")
        player.items = [existing]
        track = tmp_path / "Second.m4a"
        track.write_bytes(b"")

        assert main(["player", "play", "--add", str(track)]) == 0
        capsys.readouterr()

        assert [item.title for item in player.items] == ["First", "Second"]
        assert player.state == "stop"

    def test_play_refuses_a_path_that_is_not_a_file(self, player: FakePlayer, capsys, tmp_path):
        assert main(["player", "play", str(tmp_path / "nope.m4a")]) == 1
        assert "not a file" in capsys.readouterr().err
        assert player.rescans == 0

    @pytest.mark.parametrize("given,expected", [("40", 40), ("0", 0), ("100", 100)])
    def test_an_absolute_volume_is_set(self, player: FakePlayer, capsys, given: str, expected: int):
        assert main(["player", "volume", given]) == 0
        capsys.readouterr()
        assert player.volume == expected

    @pytest.mark.parametrize("given,expected", [("+5", 35), ("-10", 20)])
    def test_a_signed_volume_is_a_step(self, player: FakePlayer, capsys, given: str, expected: int):
        player.volume = 30
        assert main(["player", "volume", given]) == 0
        capsys.readouterr()
        assert player.volume == expected

    def test_a_nonsense_volume_is_a_clean_error(self, player: FakePlayer, capsys):
        assert main(["player", "volume", "loud"]) == 1
        assert "signed step" in capsys.readouterr().err

    def test_queue_lists_and_can_clear(self, player: FakePlayer, capsys):
        player.items = [QueueItem(id="1", position=0, title="One", path="/one.m4a")]
        assert main(["player", "queue"]) == 0
        assert [item["title"] for item in self._json(capsys)] == ["One"]

        assert main(["player", "queue", "--clear"]) == 0
        assert self._json(capsys) == []
        assert player.items == []

    def test_rescan_triggers_the_hook(self, player: FakePlayer, capsys):
        assert main(["player", "rescan"]) == 0
        capsys.readouterr()
        assert player.rescans == 1

    def test_a_dead_daemon_is_an_error_exit_with_a_readable_message(
        self, player: FakePlayer, capsys
    ):
        """Not a traceback: "start OwnTone" is a fixable situation, so it must read as one."""
        player.unavailable = True
        assert main(["player", "status"]) == 1
        assert "not answering" in capsys.readouterr().err

    def test_output_on_the_command_line_overrides_a_configured_id(self, monkeypatch):
        """Otherwise a configured TUNEBOX_OUTPUT_ID would silently win and ``--output Kitchen``
        would act on the living room."""
        seen: dict[str, object] = {}

        def capture(config):
            seen["name"] = config.output_name
            seen["id"] = config.output_id
            return FakePlayer()

        monkeypatch.setenv("TUNEBOX_OUTPUT_ID", "42")
        monkeypatch.setattr("tunebox.cli._player", capture)
        main(["player", "--output", "Kitchen", "rescan"])

        assert seen == {"name": "Kitchen", "id": None}

    def test_the_player_url_can_be_overridden_on_the_command_line(self, monkeypatch):
        seen: dict[str, object] = {}

        def capture(config):
            seen["url"] = config.player_base_url
            return FakePlayer()

        monkeypatch.setattr("tunebox.cli._player", capture)
        main(["player", "--player-url", "http://pi.local:3689/", "rescan"])

        assert seen == {"url": "http://pi.local:3689"}

    def test_every_verb_is_reachable_from_the_parser(self):
        """A verb that exists in the help but crashes on dispatch is worse than a missing one."""
        parser = build_parser()
        for verb in (
            "status",
            "outputs",
            "select",
            "play",
            "pause",
            "stop",
            "next",
            "prev",
            "queue",
            "rescan",
        ):
            args = parser.parse_args(["player", verb])
            assert callable(args.func), verb
        # `volume` takes a required positional, so it cannot go in the loop above — the original
        # version of this test had an `if verb == "volume"` branch inside a tuple that never
        # contained it, which is a dead branch pretending to be coverage.
        assert callable(parser.parse_args(["player", "volume", "40"]).func)

    def test_volume_requires_a_level(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["player", "volume"])


class TestDoctorPlayerCheck:
    """The player check is *advisory*: PLAN.md 3.1 keeps ingest working with no player installed,
    so a missing OwnTone must not make ``doctor`` exit non-zero on every Phase 1/2 host."""

    @pytest.fixture(autouse=True)
    def stub_probes(self, monkeypatch):
        monkeypatch.setattr("tunebox.cli._probe_version", lambda argv: "2026.08.19")
        monkeypatch.setattr("tunebox.cli.shutil.which", lambda name: f"/usr/local/bin/{name}")

    def test_rsync_is_reported_advisory_because_only_backups_need_it(
        self, monkeypatch, capsys, tmp_path
    ):
        """Phase 5's backup is rsync and nothing else in the system is, so its absence must not fail
        a host that only ingests — but it has to be *said*, because the first time anybody would
        otherwise find out is the night the library needed copying."""
        monkeypatch.setattr("tunebox.cli._player", lambda config: FakePlayer(unavailable=True))
        # ``have_rsync``, not ``shutil.which``: patching the latter would patch the *shared*
        # shutil module and take ffmpeg and ffprobe down with it, which is a different test.
        monkeypatch.setattr("tunebox.ops.backup.have_rsync", lambda: False)

        code = main(["--music-root", str(tmp_path / "music"), "doctor"])
        out = capsys.readouterr().out

        assert code == 0, "no rsync is not a broken ingest"
        assert "[--] rsync" in out
        assert "apt-get install rsync" in out

    def test_rsync_present_says_what_it_is_for(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr("tunebox.cli._player", lambda config: FakePlayer(unavailable=True))
        monkeypatch.setattr("tunebox.ops.backup.have_rsync", lambda: True)

        main(["--music-root", str(tmp_path / "music"), "doctor"])
        out = capsys.readouterr().out
        assert "[--] rsync" in out and "tunebox backup" in out

    def test_a_reachable_player_and_speaker_are_reported(self, monkeypatch, capsys, tmp_path):
        player = FakePlayer(selected=True)
        monkeypatch.setattr("tunebox.cli._player", lambda config: player)

        code = main(["--music-root", str(tmp_path / "music"), "doctor"])
        out = capsys.readouterr().out

        assert code == 0
        assert "[--] player" in out
        assert SOUNDBAR in out
        assert "selected" in out

    def test_a_missing_player_is_reported_without_failing_the_check(
        self, monkeypatch, capsys, tmp_path
    ):
        monkeypatch.setattr("tunebox.cli._player", lambda config: FakePlayer(unavailable=True))

        code = main(["--music-root", str(tmp_path / "music"), "doctor"])
        out = capsys.readouterr().out

        assert code == 0, "ingest works without a player, so this cannot be a failure"
        assert "not answering" in out

    def test_an_absent_speaker_is_reported_without_failing_the_check(
        self, monkeypatch, capsys, tmp_path
    ):
        monkeypatch.setattr("tunebox.cli._player", lambda config: FakePlayer(present=False))

        code = main(["--music-root", str(tmp_path / "music"), "doctor"])
        out = capsys.readouterr().out

        assert code == 0
        assert "[--] output" in out
        assert "no output named" in out

    def test_a_player_answering_nonsense_does_not_traceback(self, monkeypatch, capsys, tmp_path):
        """``doctor`` is what someone runs *because* something is wrong. A traceback out of the
        diagnostic is the least useful possible outcome, so "never raises" has to include the shape
        of the answer and not just the transport."""
        player = FakePlayer()
        player.ping = lambda: ["not", "a", "dict"]  # type: ignore[method-assign]
        monkeypatch.setattr("tunebox.cli._player", lambda config: player)

        code = main(["--music-root", str(tmp_path / "music"), "doctor"])
        out = capsys.readouterr().out

        assert code == 0
        assert "[--] player" in out

    def test_a_player_whose_output_lookup_explodes_does_not_traceback(
        self, monkeypatch, capsys, tmp_path
    ):
        player = FakePlayer()

        def explode():
            raise AttributeError("'list' object has no attribute 'get'")

        player.target_output = explode  # type: ignore[method-assign]
        monkeypatch.setattr("tunebox.cli._player", lambda config: player)

        code = main(["--music-root", str(tmp_path / "music"), "doctor"])
        out = capsys.readouterr().out

        assert code == 0
        assert "AttributeError" in out
