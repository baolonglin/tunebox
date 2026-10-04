"""The HTTP service: auth on every route, validation at the edge, and Phase 2's exit criterion.

The app under test is the real one — real middleware, real SQLite queue, real worker threads —
with a fake source in place of yt-dlp. Nothing here opens a socket to the internet.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient

from tunebox.config import Config
from tunebox.core.models import SearchResult
from tunebox.errors import (
    ConfigError,
    OutputNotFound,
    PlayerError,
    PlayerUnavailable,
    SourceError,
    SourceTimeout,
    TuneboxError,
)
from tunebox.jobs.store import JobStore

from fakes import SOUNDBAR, FakePlayer, FakeSource, RecordingTagWriter, video_id

from tunebox.api.app import create_app

TOKEN = "test-token-that-is-long-enough"
AUTH = {"X-Tunebox-Token": TOKEN}

#: Every route the app exposes, with a concrete path. ``test_the_auth_matrix_covers_every_route``
#: fails if a later phase adds a route without deciding what its auth story is.
ROUTES = (
    ("GET", "/healthz", "/healthz"),
    ("GET", "/search", "/search?q=x"),
    ("GET", "/library", "/library"),
    ("DELETE", "/library", "/library"),
    ("POST", "/ingest", "/ingest"),
    ("GET", "/jobs", "/jobs"),
    ("GET", "/jobs/{job_id}", "/jobs/abc123"),
    ("DELETE", "/jobs/{job_id}", "/jobs/abc123"),
    # Phase 4's remote. Behaviour lives in ``tests/test_remote.py``; they are listed here so the
    # matrix below still covers every route in the app.
    ("GET", "/player", "/player"),
    ("PUT", "/player/play", "/player/play"),
    ("PUT", "/player/pause", "/player/pause"),
    ("PUT", "/player/toggle", "/player/toggle"),
    ("PUT", "/player/stop", "/player/stop"),
    ("PUT", "/player/next", "/player/next"),
    ("PUT", "/player/previous", "/player/previous"),
    ("PUT", "/player/volume", "/player/volume"),
    ("PUT", "/player/seek", "/player/seek"),
    ("PUT", "/player/repeat", "/player/repeat"),
    ("PUT", "/player/shuffle", "/player/shuffle"),
    ("PUT", "/player/output/select", "/player/output/select"),
    ("GET", "/player/queue", "/player/queue"),
    ("POST", "/player/queue", "/player/queue"),
    ("DELETE", "/player/queue", "/player/queue"),
    ("PUT", "/player/queue/{item_id}/play", "/player/queue/1/play"),
    # Playlists. Behaviour lives in ``tests/test_playlists.py``; listed here so the matrix below
    # still covers every route in the app.
    ("GET", "/playlists", "/playlists"),
    ("POST", "/playlists", "/playlists"),
    ("GET", "/playlists/{name}", "/playlists/x"),
    ("DELETE", "/playlists/{name}", "/playlists/x"),
    ("PATCH", "/playlists/{name}", "/playlists/x"),
    ("POST", "/playlists/{name}/tracks", "/playlists/x/tracks"),
    ("DELETE", "/playlists/{name}/tracks", "/playlists/x/tracks"),
    ("PUT", "/playlists/{name}/tracks/{position}", "/playlists/x/tracks/0"),
    ("PUT", "/playlists/{name}/play", "/playlists/x/play"),
)

#: Added by FastAPI itself, outside our router — and therefore the reason auth is middleware.
DOC_ROUTES = ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")

#: The Phase 4 app shell: the *only* paths served without the token, because a browser cannot put a
#: header on the document request it makes on your behalf. They carry no data — see
#: ``tests/test_ui.py``, which is where that exemption is held to account.
SHELL_ROUTES = ("/", "/sw.js", "/manifest.webmanifest", "/ui/{name}")


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        music_root=tmp_path / "music",
        db_path=tmp_path / "jobs.db",
        api_token=TOKEN,
        ytdlp=("false",),
        job_retry_backoff=0.0,
        job_poll_interval=0.02,
    )


@pytest.fixture
def store(config: Config) -> JobStore:
    store = JobStore(config.database_path)
    yield store
    store.close()


@pytest.fixture
def source() -> FakeSource:
    return FakeSource(
        results=[
            SearchResult(
                id=video_id(1),
                title="Track one",
                url=f"https://www.youtube.com/watch?v={video_id(1)}",
                uploader="Uploader",
                duration=123.0,
                thumbnail="https://example.invalid/t.jpg",
            )
        ]
    )


@pytest.fixture
def player() -> FakePlayer:
    """Injected everywhere, so the suite never talks to a real OwnTone.

    Not a nicety: ``create_app`` builds an :class:`~tunebox.player.owntone.OwnTonePlayer` pointing at
    loopback by default, and ``conftest.no_network`` permits loopback — so on the development host,
    where OwnTone *is* running, these tests would otherwise re-select speakers and trigger library
    rescans on the real daemon.
    """
    return FakePlayer()


@pytest.fixture
def app(config: Config, store: JobStore, source: FakeSource, player: FakePlayer):
    return create_app(
        config,
        store=store,
        source=source,
        tag_writer=RecordingTagWriter(),
        run_worker=False,
        player=player,
        keep_output=False,
    )


@pytest.fixture
def client(app) -> TestClient:
    """No ``with``: the lifespan (and therefore the worker) stays out of these tests."""
    return TestClient(app)


# --------------------------------------------------------------------------- auth


class TestAuth:
    @pytest.mark.parametrize("method,_template,path", ROUTES)
    def test_no_token_is_rejected(self, client: TestClient, method, _template, path):
        response = client.request(method, path)
        assert response.status_code == 401
        assert response.json() == {"error": "unauthorized"}

    @pytest.mark.parametrize("path", DOC_ROUTES)
    def test_the_generated_docs_are_not_a_hole(self, client: TestClient, path):
        """These are mounted by FastAPI outside our router: a per-route dependency would miss
        them, which is why the token check is middleware."""
        assert client.get(path).status_code == 401

    def test_an_unknown_path_is_401_not_404(self, client: TestClient):
        """A 404 would confirm which paths exist to an unauthenticated caller."""
        assert client.get("/nope").status_code == 401

    def test_a_wrong_token_is_rejected(self, client: TestClient):
        assert client.get("/healthz", headers={"X-Tunebox-Token": "nope"}).status_code == 401

    def test_a_token_prefix_is_not_enough(self, client: TestClient):
        assert client.get("/healthz", headers={"X-Tunebox-Token": TOKEN[:-1]}).status_code == 401

    def test_the_header_form_is_accepted(self, client: TestClient):
        assert client.get("/healthz", headers=AUTH).status_code == 200

    def test_the_bearer_form_is_accepted(self, client: TestClient):
        """So curl, fetch() and every HTTP client can use what they already know."""
        headers = {"Authorization": f"Bearer {TOKEN}"}
        assert client.get("/healthz", headers=headers).status_code == 200

    def test_the_401_asks_for_credentials(self, client: TestClient):
        assert client.get("/healthz").headers["www-authenticate"] == "Bearer"

    def test_an_empty_token_header_is_rejected(self, client: TestClient):
        assert client.get("/healthz", headers={"X-Tunebox-Token": ""}).status_code == 401

    def test_a_bearer_header_without_a_token_is_rejected(self, client: TestClient):
        assert client.get("/healthz", headers={"Authorization": "Bearer "}).status_code == 401
        assert client.get("/healthz", headers={"Authorization": "Basic x"}).status_code == 401

    @pytest.mark.parametrize("method", ["HEAD", "OPTIONS", "PUT", "PATCH"])
    def test_other_verbs_are_rejected_too(self, client: TestClient, method):
        """A 405 would still confirm the route exists to an unauthenticated caller."""
        assert client.request(method, "/healthz").status_code == 401

    def test_a_non_ascii_token_is_rejected_not_a_500(self, client: TestClient):
        """Header bytes are compared as bytes. Decoding them and handing the result to
        ``compare_digest`` raises TypeError *outside* the exception handlers, which turned an
        unauthenticated request into a 500."""
        response = client.get(
            "/healthz",
            headers={"X-Tunebox-Token": "tøken-thåt-is-not-ascii".encode("utf-8")},
        )
        assert response.status_code == 401

    def test_raw_invalid_utf8_in_the_header_is_rejected(self, app):
        """Goes around httpx, which will not send invalid UTF-8 itself."""
        sent: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "path": "/healthz",
            "raw_path": b"/healthz",
            "query_string": b"",
            "root_path": "",
            "scheme": "http",
            "headers": [(b"x-tunebox-token", b"\xff\xfe\x00bad")],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8080),
        }
        import anyio

        anyio.run(app, scope, receive, send)
        assert sent[0]["status"] == 401

    def test_a_websocket_upgrade_does_not_slip_past(self, app):
        """No websocket routes exist yet; the middleware still must not wave non-HTTP through."""
        closed: list[dict] = []

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            closed.append(message)

        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "path": "/healthz",
            "raw_path": b"/healthz",
            "query_string": b"",
            "root_path": "",
            "scheme": "ws",
            "headers": [],
            "client": ("127.0.0.1", 1234),
        }
        import anyio

        anyio.run(app, scope, receive, send)
        assert closed == [{"type": "websocket.close", "code": 1008}]

    def test_the_auth_matrix_covers_every_route(self, app):
        registered = {route.path for route in app.routes}
        considered = {template for _, template, _ in ROUTES} | set(DOC_ROUTES) | set(SHELL_ROUTES)
        assert registered <= considered, "a new route needs an entry in ROUTES"

    def test_only_the_shell_is_exempt(self, app):
        """The exemption list is small, deliberate, and worth failing a test over: it is the one
        hole PLAN.md 3.2's "checked on *every* route" has ever had."""
        from tunebox.api import ui

        exempt = set(ui.exempt_paths())
        assert exempt == {"/", "/sw.js", "/manifest.webmanifest"}
        assert ui.exempt_prefixes() == ("/ui/",)
        api_paths = {template for _, template, _ in ROUTES}
        for path in api_paths:
            assert path not in exempt
            assert not any(path.startswith(prefix) for prefix in ui.exempt_prefixes())

    def test_serving_without_a_token_is_refused(self, tmp_path: Path):
        config = Config(music_root=tmp_path, db_path=tmp_path / "jobs.db", api_token=None)
        with pytest.raises(ConfigError, match="TUNEBOX_TOKEN"):
            create_app(config)

    def test_serving_a_short_token_is_refused(self, tmp_path: Path):
        config = Config(music_root=tmp_path, db_path=tmp_path / "jobs.db", api_token="hunter2")
        with pytest.raises(ConfigError, match="too short"):
            create_app(config)

    def test_serving_a_non_ascii_token_is_refused_at_startup(self, tmp_path: Path):
        """Otherwise the service starts and then rejects (or 500s on) every request, including
        correct ones."""
        config = Config(
            music_root=tmp_path, db_path=tmp_path / "jobs.db", api_token="tøken" * 5
        )
        with pytest.raises(ConfigError, match="ASCII"):
            create_app(config)

    @pytest.mark.parametrize(
        "host",
        ["0.0.0.0", "", "*", "::", "[::]", "::0", "0:0:0:0:0:0:0:0", "0", "0.0", "0x0", "00"],
    )
    def test_binding_every_interface_needs_an_explicit_opt_in(self, tmp_path: Path, host):
        """PLAN.md 3.2: no public exposure, ever. ``0.0.0.0`` has many spellings and the socket
        layer accepts most of them, so this is parsed rather than string-matched."""
        base = dict(music_root=tmp_path, db_path=tmp_path / "jobs.db", api_token=TOKEN)
        with pytest.raises(ConfigError, match="every interface"):
            Config(**base, api_host=host).validate_for_serve()
        assert Config(**base, api_host=host, allow_all_interfaces=True).validate_for_serve()

    @pytest.mark.parametrize("host", ["127.0.0.1", "192.168.50.20", "::1", "localhost", "pi.local"])
    def test_a_specific_address_is_allowed(self, tmp_path: Path, host):
        base = dict(music_root=tmp_path, db_path=tmp_path / "jobs.db", api_token=TOKEN)
        assert Config(**base, api_host=host).validate_for_serve() == TOKEN

    def test_the_default_host_is_loopback(self, tmp_path: Path):
        assert Config(music_root=tmp_path).api_host == "127.0.0.1"


# ------------------------------------------------------------------------- routes


class TestHealth:
    def test_health_reports_the_queue_and_the_library(self, client: TestClient, config: Config):
        body = client.get("/healthz", headers=AUTH).json()
        assert body["status"] == "ok"
        assert body["music_root"] == str(config.music_root)
        assert body["jobs"] == {"queued": 0, "running": 0, "done": 0, "failed": 0}
        assert body["worker_running"] is False  # run_worker=False in this fixture

    def test_health_reports_the_playback_session(
        self, client: TestClient, player: FakePlayer, config: Config
    ):
        """PLAN.md Phase 3 wants to be able to check "music plays with no phone involved" from a
        second device. This is the cheapest way to ask the host what it thinks is happening."""
        player.selected = True
        player.state = "play"
        player.enqueue_paths([Path("/music/Kraftwerk/Singles/Autobahn.m4a")], start=True)

        body = client.get("/healthz", headers=AUTH).json()["player"]

        assert body["reachable"] is True
        assert body["state"] == "play"
        assert body["output"] == config.output_name
        assert body["output_selected"] is True
        assert body["queue_length"] == 1
        assert "Autobahn" in body["now_playing"]

    def test_an_unplugged_soundbar_is_reported_not_selected_rather_than_hidden(
        self, client: TestClient, player: FakePlayer, config: Config
    ):
        player.present = False
        body = client.get("/healthz", headers=AUTH).json()["player"]
        assert body["reachable"] is True, "the daemon is up; it is the speaker that is not"
        assert body["output"] == config.output_name
        assert body["output_selected"] is False

    def test_a_configured_output_id_wins_over_a_name_match(
        self, config: Config, store: JobStore, source: FakeSource
    ):
        """The README promises the id wins, and `find_output` implements that. Matching on *either*
        would let a speaker that merely shares the configured name be reported as the selected
        output while the id points somewhere else entirely — a green health check for a speaker
        nothing is driving."""
        player = FakePlayer(output_id="1234", output_name=SOUNDBAR, selected=True)
        app = create_app(
            replace(config, output_id="not-this-one", output_name=SOUNDBAR),
            store=store,
            source=source,
            tag_writer=RecordingTagWriter(),
            run_worker=False,
            player=player,
            keep_output=False,
        )

        body = TestClient(app).get("/healthz", headers=AUTH).json()["player"]

        assert body["output"] == "not-this-one", "the configured id, since nothing matched it"
        assert body["output_selected"] is False

    def test_a_dead_player_does_not_make_the_health_check_fail(
        self, client: TestClient, player: FakePlayer
    ):
        """A monitor polling /healthz must not see 503 because the *soundbar* is off. The player
        being down is a field in the answer, not the answer's status code."""
        player.unavailable = True

        response = client.get("/healthz", headers=AUTH)

        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["player"]["reachable"] is False
        assert "not answering" in response.json()["player"]["error"]

    def test_an_adapter_bug_does_not_make_the_health_check_fail_either(
        self, client: TestClient, player: FakePlayer
    ):
        """"Never fails because of the player" has to include the exception types we did not think
        of, or the guarantee is only as good as our imagination."""

        def explode():
            raise AttributeError("'list' object has no attribute 'get'")

        player.status = explode  # type: ignore[method-assign]

        response = client.get("/healthz", headers=AUTH)

        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["player"]["reachable"] is False
        assert "AttributeError" in response.json()["player"]["error"]


class TestSearch:
    def test_search_returns_the_fields_a_phone_needs(self, client: TestClient):
        body = client.get("/search", params={"q": "kraftwerk autobahn"}, headers=AUTH).json()
        assert [item["id"] for item in body] == [video_id(1)]
        assert body[0]["uploader"] == "Uploader"
        assert body[0]["thumbnail"].startswith("https://")

    def test_the_limit_is_passed_through(self, client: TestClient, source: FakeSource):
        client.get("/search", params={"q": "x", "limit": 5}, headers=AUTH)
        assert source.search_calls[-1] == ("x", 5)

    def test_an_empty_query_is_a_validation_error(self, client: TestClient):
        assert client.get("/search", params={"q": ""}, headers=AUTH).status_code == 422
        assert client.get("/search", headers=AUTH).status_code == 422

    def test_an_absurd_limit_is_rejected_rather_than_clamped(self, client: TestClient):
        assert client.get("/search", params={"q": "x", "limit": 500}, headers=AUTH).status_code == 422

    def test_no_results_is_an_empty_list_not_an_error(self, client: TestClient, source):
        source.results = []
        response = client.get("/search", params={"q": "x"}, headers=AUTH)
        assert response.status_code == 200
        assert response.json() == []

    def test_an_upstream_failure_reads_as_upstream(self, client: TestClient, source):
        def boom(*args, **kwargs):
            raise SourceError("HTTP Error 403: Forbidden")

        source.search = boom
        response = client.get("/search", params={"q": "x"}, headers=AUTH)
        assert response.status_code == 502
        assert "403" in response.json()["error"]

    def test_a_hung_extraction_reads_as_a_timeout(self, client: TestClient, source):
        def slow(*args, **kwargs):
            raise SourceTimeout("search timed out after 60s")

        source.search = slow
        assert client.get("/search", params={"q": "x"}, headers=AUTH).status_code == 504


class TestIngest:
    def test_a_post_queues_a_job_and_returns_immediately(self, client: TestClient, source):
        response = client.post("/ingest", json={"url": f"https://youtu.be/{video_id(1)}"}, headers=AUTH)
        assert response.status_code == 201
        body = response.json()
        assert body["state"] == "queued"
        assert body["video_id"] == video_id(1)
        # The request did no work: the worker owns everything slow.
        assert source.inspect_calls == []
        assert source.fetch_calls == []

    def test_the_url_is_canonicalised_before_it_is_stored(self, client: TestClient):
        body = client.post(
            "/ingest",
            json={"url": f"https://m.youtube.com/watch?v={video_id(2)}&list=PLabc&t=42"},
            headers=AUTH,
        ).json()
        assert body["url"] == f"https://www.youtube.com/watch?v={video_id(2)}"

    def test_a_bare_video_id_is_accepted(self, client: TestClient):
        assert client.post("/ingest", json={"url": video_id(3)}, headers=AUTH).status_code == 201

    def test_a_host_outside_the_allowlist_is_a_client_error(self, client: TestClient):
        response = client.post("/ingest", json={"url": "https://vimeo.com/12345"}, headers=AUTH)
        assert response.status_code == 400
        assert "host not allowed" in response.json()["error"]

    def test_a_file_url_never_reaches_a_subprocess(self, client: TestClient, source):
        response = client.post("/ingest", json={"url": "file:///etc/passwd"}, headers=AUTH)
        assert response.status_code == 400
        assert source.inspect_calls == []

    def test_an_unknown_field_is_rejected_rather_than_ignored(self, client: TestClient):
        response = client.post(
            "/ingest", json={"url": video_id(1), "artistt": "typo"}, headers=AUTH
        )
        assert response.status_code == 422

    def test_an_unsupported_format_is_rejected(self, client: TestClient):
        response = client.post("/ingest", json={"url": video_id(1), "format": "flac"}, headers=AUTH)
        assert response.status_code == 422

    def test_overrides_are_recorded_on_the_job(self, client: TestClient, store: JobStore):
        body = client.post(
            "/ingest",
            json={"url": video_id(1), "artist": "Kraftwerk", "track_number": 1},
            headers=AUTH,
        ).json()
        assert store.get(body["id"]).overrides == {"artist": "Kraftwerk", "track_number": 1}

    def test_queueing_the_same_video_twice_returns_the_first_job(self, client: TestClient):
        first = client.post("/ingest", json={"url": video_id(1)}, headers=AUTH)
        second = client.post("/ingest", json={"url": f"https://youtu.be/{video_id(1)}"}, headers=AUTH)

        assert first.status_code == 201
        assert second.status_code == 200  # nothing new was created
        assert second.json()["id"] == first.json()["id"]
        assert len(client.get("/jobs", headers=AUTH).json()) == 1

    def test_force_queues_a_second_job_anyway(self, client: TestClient):
        first = client.post("/ingest", json={"url": video_id(1)}, headers=AUTH)
        second = client.post("/ingest", json={"url": video_id(1), "force": True}, headers=AUTH)
        assert second.status_code == 201
        assert second.json()["id"] != first.json()["id"]

    def test_a_different_format_is_not_treated_as_a_duplicate(self, client: TestClient):
        """Returning the queued m4a job would mean the requested mp3 never gets made."""
        first = client.post("/ingest", json={"url": video_id(1)}, headers=AUTH)
        second = client.post("/ingest", json={"url": video_id(1), "format": "mp3"}, headers=AUTH)
        assert second.status_code == 201
        assert second.json()["id"] != first.json()["id"]
        assert second.json()["audio_format"] == "mp3"

    def test_a_second_request_carrying_overrides_is_not_silently_dropped(self, client: TestClient):
        first = client.post("/ingest", json={"url": video_id(1)}, headers=AUTH)
        second = client.post(
            "/ingest", json={"url": video_id(1), "artist": "Kraftwerk"}, headers=AUTH
        )
        assert second.status_code == 201
        assert second.json()["id"] != first.json()["id"]

    def test_an_identical_repeat_still_returns_the_first_job(self, client: TestClient):
        body = {"url": video_id(1), "artist": "Kraftwerk", "format": "mp3"}
        first = client.post("/ingest", json=body, headers=AUTH)
        second = client.post("/ingest", json=body, headers=AUTH)
        assert second.status_code == 200
        assert second.json()["id"] == first.json()["id"]


class TestJobs:
    def test_a_job_can_be_polled_by_id(self, client: TestClient):
        job_id = client.post("/ingest", json={"url": video_id(1)}, headers=AUTH).json()["id"]
        body = client.get(f"/jobs/{job_id}", headers=AUTH).json()
        assert body["id"] == job_id
        assert body["state"] == "queued"

    def test_an_unknown_job_is_a_404(self, client: TestClient):
        response = client.get("/jobs/0123456789abcdef", headers=AUTH)
        assert response.status_code == 404
        assert response.json() == {"error": "no such job"}

    def test_jobs_are_listed_newest_first_and_filterable(self, client: TestClient, store: JobStore):
        first = client.post("/ingest", json={"url": video_id(1)}, headers=AUTH).json()["id"]
        second = client.post("/ingest", json={"url": video_id(2)}, headers=AUTH).json()["id"]
        store.claim_next()  # first becomes running

        listed = [job["id"] for job in client.get("/jobs", headers=AUTH).json()]
        assert listed == [second, first]

        running = client.get("/jobs", params={"state": "running"}, headers=AUTH).json()
        assert [job["id"] for job in running] == [first]

    def test_an_unknown_state_filter_is_a_validation_error(self, client: TestClient):
        assert client.get("/jobs", params={"state": "sideways"}, headers=AUTH).status_code == 422

    def test_a_queued_job_can_be_cancelled(self, client: TestClient):
        job_id = client.post("/ingest", json={"url": video_id(1)}, headers=AUTH).json()["id"]
        response = client.delete(f"/jobs/{job_id}", headers=AUTH)
        assert response.status_code == 200
        assert response.json()["state"] == "failed"
        assert response.json()["error"] == "cancelled"

    def test_a_running_job_is_left_alone(self, client: TestClient, store: JobStore):
        job_id = client.post("/ingest", json={"url": video_id(1)}, headers=AUTH).json()["id"]
        store.claim_next()
        response = client.delete(f"/jobs/{job_id}", headers=AUTH)
        assert response.status_code == 409
        assert store.get(job_id).state.value == "running"


# ------------------------------------------------------------- the exit criterion


class TestPlayerErrorMapping:
    """The player half of the "one exception tree" property (PLAN.md 3.4).

    No Phase 3 route can reach these — `/healthz` deliberately swallows player failures — so they
    would otherwise be wiring nobody had ever executed by the time Phase 4 starts proxying transport
    commands. Asserted by invoking the registered handlers, which is what Starlette does.
    """

    @pytest.mark.parametrize(
        "exc,expected",
        [
            (PlayerUnavailable("owntone is not running"), 503),
            (OutputNotFound("no output named 'Living Room speaker'"), 404),
            (PlayerError("HTTP 500 from the daemon"), 502),
        ],
    )
    def test_each_player_failure_maps_to_its_own_status(self, app, exc, expected):
        handler = _handler_for(app, type(exc))
        assert handler is not None, f"no handler registered for {type(exc).__name__}"

        response = anyio.run(handler, None, exc)

        assert response.status_code == expected
        assert json.loads(bytes(response.body))["error"] == str(exc)

    def test_the_specific_handlers_win_over_the_generic_tunebox_one(self, app):
        """Registration order must not decide this: a 503 reported as a 500 tells a client to stop
        retrying when it should keep going."""
        assert _handler_for(app, PlayerUnavailable) is not _handler_for(app, TuneboxError)
        assert _handler_for(app, OutputNotFound) is not _handler_for(app, PlayerError)


def _handler_for(app, exc_type: type) -> object | None:
    """The handler Starlette would pick for ``exc_type``: the nearest match up the MRO."""
    for klass in exc_type.__mro__:
        if klass in app.exception_handlers:
            return app.exception_handlers[klass]
    return None


class TestExitCriterion:
    def test_three_urls_queued_over_http_all_land(
        self, config: Config, store: JobStore, source, player: FakePlayer
    ):
        """PLAN.md Phase 2: *"queue three URLs from curl, all three land correctly."*

        Same path curl takes — POST three times, poll the job ids — with the worker running for
        real inside the app's lifespan.
        """
        app = create_app(
            config,
            store=store,
            source=source,
            tag_writer=RecordingTagWriter(),
            run_worker=True,
            player=player,
            keep_output=False,
        )
        ids = [video_id(n) for n in (1, 2, 3)]

        with TestClient(app) as client:
            job_ids = [
                client.post("/ingest", json={"url": f"https://youtu.be/{vid}"}, headers=AUTH).json()[
                    "id"
                ]
                for vid in ids
            ]
            assert client.get("/healthz", headers=AUTH).json()["worker_running"] is True

            deadline = time.monotonic() + 15
            jobs: list[dict] = []
            while time.monotonic() < deadline:
                jobs = [client.get(f"/jobs/{j}", headers=AUTH).json() for j in job_ids]
                if all(job["state"] in ("done", "failed") for job in jobs):
                    break
                time.sleep(0.05)

        assert [job["state"] for job in jobs] == ["done"] * 3
        assert [job["already_present"] for job in jobs] == [False] * 3
        for job, vid in zip(jobs, ids):
            path = Path(job["path"])
            assert path.is_file()
            assert path == config.music_root / f"Artist {vid}" / "Singles" / f"Track {vid}.m4a"
            assert job["tags"]["source_id"] == vid

    def test_a_failing_ingest_is_visible_as_a_failed_job_not_a_lost_one(
        self, config: Config, store: JobStore, player: FakePlayer
    ):
        source = FakeSource(fail_fetch_times={video_id(9): 99})
        app = create_app(
            config,
            store=store,
            source=source,
            tag_writer=RecordingTagWriter(),
            run_worker=True,
            player=player,
            keep_output=False,
        )
        with TestClient(app) as client:
            job_id = client.post("/ingest", json={"url": video_id(9)}, headers=AUTH).json()["id"]
            deadline = time.monotonic() + 15
            job = {}
            while time.monotonic() < deadline:
                job = client.get(f"/jobs/{job_id}", headers=AUTH).json()
                if job["state"] == "failed":
                    break
                time.sleep(0.05)

        assert job["state"] == "failed"
        assert job["attempts"] == config.job_max_attempts
        assert "failed on purpose" in job["error"]
