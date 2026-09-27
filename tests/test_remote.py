"""``/player/*`` and ``GET /library`` — the API the web remote drives (PLAN.md Phase 4).

The thing most worth testing here is a *negative*: PLAN.md 3.2 says "do not expose [the player
daemon's] API unauthenticated just because our UI proxies to it", and the way that requirement gets
broken is by somebody adding a convenient pass-through route. So alongside the behaviour of each
command there are tests that no such route exists, and that the one route which accepts a filename
cannot be talked into naming a file outside the music root.

The app under test is the real one — real middleware, real router, real SQLite queue — with
:class:`fakes.FakePlayer` in place of OwnTone. That is not only for speed: ``create_app`` builds a
real :class:`~tunebox.player.owntone.OwnTonePlayer` pointing at loopback by default, and on a
development host where OwnTone *is* running these tests would otherwise clear its queue.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tunebox.config import Config
from tunebox.jobs.store import JobStore

from fakes import SOUNDBAR, FakePlayer, FakeSource, RecordingTagWriter

from tunebox.api.app import create_app

TOKEN = "test-token-that-is-long-enough"
AUTH = {"X-Tunebox-Token": TOKEN}


@pytest.fixture
def config(tmp_path: Path) -> Config:
    root = tmp_path / "music"
    root.mkdir()
    return Config(
        music_root=root,
        db_path=tmp_path / "jobs.db",
        api_token=TOKEN,
        ytdlp=("false",),
    )


@pytest.fixture
def store(config: Config) -> JobStore:
    store = JobStore(config.database_path)
    yield store
    store.close()


@pytest.fixture
def player() -> FakePlayer:
    return FakePlayer()


@pytest.fixture
def client(config: Config, store: JobStore, player: FakePlayer) -> TestClient:
    app = create_app(
        config,
        store=store,
        source=FakeSource(),
        tag_writer=RecordingTagWriter(),
        run_worker=False,
        player=player,
        keep_output=False,
    )
    return TestClient(app)


def add_track(config: Config, relative: str) -> Path:
    path = config.music_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"audio")
    return path


AUTOBAHN = "Kraftwerk/Singles/Autobahn.m4a"
MODEL = "Kraftwerk/3-D Der Katalog/Das Model.m4a"


# ----------------------------------------------------------------------------- auth

#: Every route this module adds, with a concrete path. ``tests/test_api.py`` owns the matrix that
#: fails when a route appears with no auth story; this is the same list from the other side.
ROUTES = (
    ("GET", "/player"),
    ("PUT", "/player/play"),
    ("PUT", "/player/pause"),
    ("PUT", "/player/toggle"),
    ("PUT", "/player/stop"),
    ("PUT", "/player/next"),
    ("PUT", "/player/previous"),
    ("PUT", "/player/volume"),
    ("PUT", "/player/seek"),
    ("PUT", "/player/output/select"),
    ("GET", "/player/queue"),
    ("POST", "/player/queue"),
    ("DELETE", "/player/queue"),
    ("GET", "/library"),
)


@pytest.mark.parametrize("method,path", ROUTES)
def test_every_remote_route_needs_the_token(client: TestClient, method: str, path: str):
    assert client.request(method, path).status_code == 401


# ------------------------------------------------------------------------ the state


class TestPlayerState:
    def test_one_request_describes_the_whole_session(
        self, client: TestClient, player: FakePlayer, config: Config
    ):
        """One response rather than three, so the transport state and the queue on screen cannot
        disagree with each other."""
        add_track(config, AUTOBAHN)
        player.state = "play"
        player.selected = True
        player.enqueue_paths([config.music_root / AUTOBAHN])

        body = client.get("/player", headers=AUTH).json()

        assert body["state"] == "play"
        assert body["volume"] == 30
        assert body["url"] == player.base_url
        assert body["now_playing"]["title"] == "Autobahn"
        assert [item["title"] for item in body["queue"]] == ["Autobahn"]
        assert body["output"]["name"] == SOUNDBAR
        assert body["output_selected"] is True

    def test_an_unplugged_speaker_is_reported_rather_than_hidden(
        self, client: TestClient, player: FakePlayer
    ):
        """A Cast device that is powered off vanishes from the daemon's output list entirely. The
        remote needs to say so, or someone stands in a kitchen wondering why there is no sound."""
        player.present = False
        body = client.get("/player", headers=AUTH).json()
        assert body["output"] is None
        assert body["output_selected"] is False

    def test_a_deselected_speaker_is_visible_but_off(self, client: TestClient, player: FakePlayer):
        player.selected = False
        body = client.get("/player", headers=AUTH).json()
        assert body["output"]["name"] == SOUNDBAR
        assert body["output_selected"] is False

    def test_a_dead_daemon_is_503_here_and_200_on_healthz(
        self, client: TestClient, player: FakePlayer
    ):
        """Deliberately different: ``/healthz`` is what a monitor polls and must not report an
        outage because a soundbar is unplugged, while on ``/player`` the player *is* the subject."""
        player.unavailable = True
        assert client.get("/player", headers=AUTH).status_code == 503
        health = client.get("/healthz", headers=AUTH)
        assert health.status_code == 200
        assert health.json()["player"]["reachable"] is False

    def test_the_configured_output_is_matched_by_id_when_one_is_set(
        self, config: Config, store: JobStore, player: FakePlayer
    ):
        """Id wins over name, and a *wrong* id must not fall back to a name match — otherwise a
        speaker somebody renamed could be reported as the configured one."""
        mismatched = replace(config, output_id="not-the-right-id")
        app = create_app(
            mismatched,
            store=store,
            source=FakeSource(),
            tag_writer=RecordingTagWriter(),
            run_worker=False,
            player=player,
            keep_output=False,
        )
        body = TestClient(app).get("/player", headers=AUTH).json()
        assert body["output"] is None


# --------------------------------------------------------------------- transport


class TestTransport:
    @pytest.mark.parametrize(
        "path,command",
        [
            ("/player/play", "play"),
            ("/player/pause", "pause"),
            ("/player/stop", "stop"),
            ("/player/next", "next"),
            ("/player/previous", "previous"),
        ],
    )
    def test_each_button_sends_one_command(
        self, client: TestClient, player: FakePlayer, path: str, command: str
    ):
        response = client.put(path, headers=AUTH)
        assert response.status_code == 200
        assert player.commands == [command]

    def test_the_response_is_the_new_state_not_an_acknowledgement(
        self, client: TestClient, player: FakePlayer
    ):
        """The remote redraws from this rather than guessing, because the session is shared: a
        second phone, OwnTone's own web UI and the soundbar's remote all touch it."""
        assert client.put("/player/play", headers=AUTH).json()["state"] == "play"

    def test_toggle_is_one_round_trip(self, client: TestClient, player: FakePlayer):
        """Read-then-decide on the client would race anything else driving the session, and the
        button would then do the opposite of what its label said."""
        player.state = "play"
        assert client.put("/player/toggle", headers=AUTH).json()["state"] == "pause"
        assert player.commands == ["toggle"]

    def test_seek(self, client: TestClient, player: FakePlayer):
        assert client.put("/player/seek", headers=AUTH, json={"position_ms": 5000}).status_code == 200
        assert player.commands == ["seek:5000"]

    def test_a_negative_seek_is_rejected_at_the_edge(self, client: TestClient, player: FakePlayer):
        assert client.put("/player/seek", headers=AUTH, json={"position_ms": -1}).status_code == 422
        assert player.commands == []


class TestVolume:
    def test_absolute(self, client: TestClient, player: FakePlayer):
        assert client.put("/player/volume", headers=AUTH, json={"volume": 55}).json()["volume"] == 55

    def test_a_step_does_not_need_the_level_read_first(
        self, client: TestClient, player: FakePlayer
    ):
        """Which is what makes two taps of ``+`` do the right thing."""
        body = client.put("/player/volume", headers=AUTH, json={"step": 5}).json()
        assert body["volume"] == 35

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"volume": 10, "step": 5},
            {"volume": 101},
            {"volume": -1},
            {"step": 500},
            {"volume": 10, "extra": 1},
        ],
    )
    def test_nonsense_is_a_422_not_a_guess(
        self, client: TestClient, player: FakePlayer, payload: dict
    ):
        assert client.put("/player/volume", headers=AUTH, json=payload).status_code == 422
        assert player.volume == 30


class TestOutputSelection:
    def test_it_selects_the_configured_speaker(self, client: TestClient, player: FakePlayer):
        player.selected = False
        body = client.put("/player/output/select", headers=AUTH).json()
        assert body["output_selected"] is True
        assert player.selections == [(player.output_id, None)]

    def test_no_id_is_taken_from_the_caller(self, client: TestClient):
        """Accepting one would let the remote enable a *neighbour's* Cast device, which is reachable
        from a home network and is nobody's idea of a feature."""
        assert client.put("/player/output/select", headers=AUTH, json={"output_id": "x"}).status_code in (
            200,
            422,
        )

    def test_an_absent_speaker_is_a_404(self, client: TestClient, player: FakePlayer):
        """A genuinely missing resource: an unplugged soundbar is not on the network to be found."""
        player.present = False
        assert client.put("/player/output/select", headers=AUTH).status_code == 404


# ------------------------------------------------------------------------- queue


class TestQueue:
    def test_append_adds_without_starting(
        self, client: TestClient, player: FakePlayer, config: Config
    ):
        add_track(config, AUTOBAHN)
        response = client.post(
            "/player/queue", headers=AUTH, json={"paths": [AUTOBAHN], "mode": "append"}
        )
        assert response.status_code == 201
        assert [item["title"] for item in response.json()["queue"]] == ["Autobahn"]
        assert player.state == "stop"

    def test_replace_and_start_is_play_this_now(
        self, client: TestClient, player: FakePlayer, config: Config
    ):
        add_track(config, AUTOBAHN)
        add_track(config, MODEL)
        client.post("/player/queue", headers=AUTH, json={"paths": [AUTOBAHN]})

        response = client.post(
            "/player/queue",
            headers=AUTH,
            json={"paths": [MODEL], "mode": "replace", "start": True},
        )

        assert [item["title"] for item in response.json()["queue"]] == ["Das Model"]
        assert player.state == "play"

    def test_order_is_preserved(self, client: TestClient, config: Config):
        add_track(config, AUTOBAHN)
        add_track(config, MODEL)
        body = client.post(
            "/player/queue", headers=AUTH, json={"paths": [MODEL, AUTOBAHN]}
        ).json()
        assert [item["title"] for item in body["added"]] == ["Das Model", "Autobahn"]

    def test_an_absolute_path_from_a_job_is_accepted(self, client: TestClient, config: Config):
        """``GET /jobs`` reports the absolute path the ingest wrote, and the remote's "Play" button
        sends back exactly what it was given."""
        absolute = add_track(config, AUTOBAHN)
        assert (
            client.post("/player/queue", headers=AUTH, json={"paths": [str(absolute)]}).status_code
            == 201
        )

    def test_get_and_clear(self, client: TestClient, config: Config):
        add_track(config, AUTOBAHN)
        client.post("/player/queue", headers=AUTH, json={"paths": [AUTOBAHN]})
        assert len(client.get("/player/queue", headers=AUTH).json()) == 1

        assert client.delete("/player/queue", headers=AUTH).json()["queue"] == []
        assert client.get("/player/queue", headers=AUTH).json() == []

    def test_a_dead_daemon_does_not_look_like_a_bad_request(
        self, client: TestClient, player: FakePlayer, config: Config
    ):
        add_track(config, AUTOBAHN)
        player.unavailable = True
        assert (
            client.post("/player/queue", headers=AUTH, json={"paths": [AUTOBAHN]}).status_code == 503
        )


class TestQueuePathSafety:
    """The one route that takes a filename from a client. PLAN.md 3.2: confine to the music root,
    reject traversal."""

    @pytest.mark.parametrize(
        "given",
        [
            "../../../etc/passwd",
            "Kraftwerk/../../escape.m4a",
            "/etc/passwd",
            "/etc/shadow",
            "..\\..\\windows\\system32\\config\\sam",
            "",
            "   ",
        ],
    )
    def test_a_path_outside_the_library_is_a_400(
        self, client: TestClient, player: FakePlayer, given: str
    ):
        response = client.post("/player/queue", headers=AUTH, json={"paths": [given]})
        assert response.status_code == 400, given
        assert player.items == []

    def test_a_non_music_file_in_the_library_is_a_400(self, client: TestClient, config: Config):
        """The queue database lives under the music root; nothing may ask the daemon to play it."""
        add_track(config, "notes.txt")
        assert (
            client.post("/player/queue", headers=AUTH, json={"paths": ["notes.txt"]}).status_code
            == 400
        )

    def test_a_missing_file_is_a_400_not_a_500(self, client: TestClient):
        assert (
            client.post("/player/queue", headers=AUTH, json={"paths": ["A/Singles/ghost.m4a"]}).status_code
            == 400
        )

    def test_one_bad_path_adds_nothing(
        self, client: TestClient, player: FakePlayer, config: Config
    ):
        """All-or-nothing: a typo in the second path must not leave the queue half-replaced with
        playback already running."""
        add_track(config, AUTOBAHN)
        response = client.post(
            "/player/queue",
            headers=AUTH,
            json={"paths": [AUTOBAHN, "../escape.m4a"], "mode": "replace", "start": True},
        )
        assert response.status_code == 400
        assert player.items == []
        assert player.state == "stop"

    @pytest.mark.parametrize(
        "payload",
        [
            {"paths": []},
            {"paths": ["a.m4a"] * 101},
            {"paths": "not-a-list"},
            {},
            {"paths": ["a.m4a"], "mode": "obliterate"},
            {"paths": ["a.m4a"], "unknown": True},
        ],
    )
    def test_a_malformed_body_is_a_422(self, client: TestClient, payload: dict):
        assert client.post("/player/queue", headers=AUTH, json=payload).status_code == 422


class TestThereIsNoPassThrough:
    """PLAN.md 3.2: *"do not expose [the player daemon's] API unauthenticated just because our UI
    proxies to it"*. The way that requirement gets broken is a convenience route that forwards
    whatever arrives to port 3689 — so these assert that no such route exists.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "/player/api/player/stop",
            "/player/api/outputs/set",
            "/player/api/library/playlists",
            "/player/api/config",
            "/player/outputs/9999/select",
        ],
    )
    def test_owntone_paths_are_not_forwarded(self, client: TestClient, path: str):
        for method in ("GET", "PUT", "POST"):
            assert client.request(method, path, headers=AUTH).status_code in (404, 405), path

    def test_the_router_exposes_only_the_port(self, client: TestClient):
        """Everything reachable goes through :class:`~tunebox.core.ports.Player`, which is what
        makes the fallback-A player PLAN.md 3.1 holds in reserve a drop-in replacement."""
        paths = {route.path for route in client.app.routes if route.path.startswith("/player")}
        assert paths == {path for _, path in ROUTES if path.startswith("/player")}


# ----------------------------------------------------------------------- library


class TestLibraryRoute:
    def test_it_lists_relative_paths_newest_first(self, client: TestClient, config: Config):
        import os

        old = add_track(config, AUTOBAHN)
        new = add_track(config, MODEL)
        os.utime(old, (1_000_000, 1_000_000))
        os.utime(new, (2_000_000, 2_000_000))

        body = client.get("/library", headers=AUTH).json()

        assert body["total"] == 2
        assert [t["path"] for t in body["tracks"]] == [MODEL, AUTOBAHN]
        assert body["tracks"][0]["artist"] == "Kraftwerk"
        assert body["tracks"][0]["album"] == "3-D Der Katalog"

    def test_an_absolute_path_never_reaches_a_client(self, client: TestClient, config: Config):
        add_track(config, AUTOBAHN)
        body = client.get("/library", headers=AUTH).text
        assert str(config.music_root) not in body

    def test_it_works_with_no_player_at_all(self, client: TestClient, player: FakePlayer, config: Config):
        """The library is a fact about our own disk. PLAN.md 3.1 wants ingest — and therefore
        browsing what was ingested — to keep working on a host with no player installed."""
        add_track(config, AUTOBAHN)
        player.unavailable = True
        assert client.get("/library", headers=AUTH).json()["total"] == 1

    def test_filtering(self, client: TestClient, config: Config):
        add_track(config, AUTOBAHN)
        add_track(config, MODEL)
        assert client.get("/library?q=model", headers=AUTH).json()["total"] == 1
        assert client.get("/library?q=zzz", headers=AUTH).json()["total"] == 0

    def test_the_limit_is_bounded(self, client: TestClient):
        assert client.get("/library?limit=0", headers=AUTH).status_code == 422
        assert client.get("/library?limit=501", headers=AUTH).status_code == 422

    def test_truncation_is_reported(self, config: Config, store: JobStore, player: FakePlayer):
        """A music root wider than intended gives an incomplete answer that says so, rather than a
        slow one that does not."""
        for index in range(6):
            add_track(config, f"A/Singles/{index}.m4a")
        capped = replace(config, library_max_files=3)
        app = create_app(
            capped,
            store=store,
            source=FakeSource(),
            tag_writer=RecordingTagWriter(),
            run_worker=False,
            player=player,
            keep_output=False,
        )
        assert TestClient(app).get("/library", headers=AUTH).json()["truncated"] is True


# --------------------------------------------------- the round trip Phase 4 promises


def test_ingest_then_play_it(client: TestClient, player: FakePlayer, config: Config):
    """The whole Phase 4 loop, minus yt-dlp: the library lists a track, the remote sends its
    relative path back, and the host is playing it. Exit criterion: one icon does everything."""
    add_track(config, AUTOBAHN)

    listing = client.get("/library", headers=AUTH).json()
    path = listing["tracks"][0]["path"]

    client.post(
        "/player/queue", headers=AUTH, json={"paths": [path], "mode": "replace", "start": True}
    )
    state = client.get("/player", headers=AUTH).json()

    assert state["state"] == "play"
    assert state["now_playing"]["title"] == "Autobahn"
    assert player.items[0].path == str(config.music_root / AUTOBAHN)
