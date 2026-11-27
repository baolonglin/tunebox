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

from tunebox import library
from tunebox.config import Config
from tunebox.core.models import Output
from tunebox.errors import OutputUnreachable
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
    ("PUT", "/player/repeat"),
    ("PUT", "/player/shuffle"),
    ("PUT", "/player/output"),
    ("PUT", "/player/output/select"),
    ("GET", "/player/queue"),
    ("POST", "/player/queue"),
    ("DELETE", "/player/queue"),
    # The only parameterised route in this router. Listed by its *template*, which is what
    # ``test_the_router_exposes_only_the_port`` compares against — and it still answers 401
    # unauthenticated, because the token middleware runs before anything is routed.
    ("PUT", "/player/queue/{item_id}/play"),
    ("GET", "/library"),
    ("DELETE", "/library"),
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


class TestPlaybackModes:
    """``PUT /player/repeat`` and ``PUT /player/shuffle`` — TODO.md's "playback modes".

    The acceptance test for repeat-one is a hardware one (a track reaching its end and starting
    again on the soundbar) and cannot live here. What can is the half that would silently swallow a
    mistake: the daemon answers ``204`` to a mode it cannot parse and treats it as "off", so these
    assert that nonsense is refused at *our* edge and that the player is left alone when it is.
    """

    @pytest.mark.parametrize("mode", ["off", "all", "single"])
    def test_each_mode_is_one_command_and_answers_with_the_new_state(
        self, client: TestClient, player: FakePlayer, mode: str
    ):
        body = client.put("/player/repeat", headers=AUTH, json={"mode": mode}).json()
        assert body["repeat"] == mode
        assert player.commands == [f"repeat:{mode}"]

    def test_repeat_one_song(self, client: TestClient, player: FakePlayer):
        """TODO.md's first item. ``single`` is the daemon's word for it, and the remote uses the same
        one rather than inventing ``one`` and translating in the adapter."""
        assert client.put("/player/repeat", headers=AUTH, json={"mode": "single"}).json()["repeat"] == "single"
        assert player.repeat == "single"

    def test_repeat_the_queue(self, client: TestClient, player: FakePlayer):
        """TODO.md's second item, and the one that makes an album play round in the evening."""
        assert client.put("/player/repeat", headers=AUTH, json={"mode": "all"}).json()["repeat"] == "all"
        assert player.repeat == "all"

    def test_repeat_off_again(self, client: TestClient, player: FakePlayer):
        player.repeat = "single"
        assert client.put("/player/repeat", headers=AUTH, json={"mode": "off"}).json()["repeat"] == "off"

    def test_the_mode_is_absolute_so_two_phones_converge(
        self, client: TestClient, player: FakePlayer
    ):
        """There is deliberately no ``/player/repeat/cycle``. A relative command from a phone holding
        a two-second-old poll would land wherever the *other* phone left it, so the UI's tri-state
        button sends the mode its own label implied and the response redraws it."""
        client.put("/player/repeat", headers=AUTH, json={"mode": "all"})
        client.put("/player/repeat", headers=AUTH, json={"mode": "all"})
        assert player.repeat == "all"
        assert player.commands == ["repeat:all", "repeat:all"]

    @pytest.mark.parametrize("enabled", [True, False])
    def test_shuffle_is_set_explicitly_rather_than_toggled(
        self, client: TestClient, player: FakePlayer, enabled: bool
    ):
        body = client.put("/player/shuffle", headers=AUTH, json={"enabled": enabled}).json()
        assert body["shuffle"] is enabled
        assert player.commands == [f"shuffle:{'true' if enabled else 'false'}"]

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"mode": "bogus"},
            {"mode": "one"},
            {"mode": "ALL"},
            {"mode": None},
            {"mode": "all", "extra": 1},
            {"state": "all"},
        ],
    )
    def test_a_bad_repeat_body_is_a_422_and_changes_nothing(
        self, client: TestClient, player: FakePlayer, payload: dict
    ):
        """The daemon would answer ``204`` to every one of these and set repeat to "off", which is
        the trap this route exists to close."""
        assert client.put("/player/repeat", headers=AUTH, json=payload).status_code == 422
        assert player.commands == []
        assert player.repeat == "off"

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"enabled": "maybe"},
            {"enabled": None},
            {"enabled": True, "extra": 1},
            {"shuffle": True},
            # Accepted by pydantic's ordinary ``bool`` and refused here on purpose: an edge looser
            # than the adapter behind it is an edge that is not the rule.
            {"enabled": "true"},
            {"enabled": "false"},
            {"enabled": "yes"},
            {"enabled": "on"},
            {"enabled": 1},
            {"enabled": 0},
        ],
    )
    def test_a_bad_shuffle_body_is_a_422_and_changes_nothing(
        self, client: TestClient, player: FakePlayer, payload: dict
    ):
        assert client.put("/player/shuffle", headers=AUTH, json=payload).status_code == 422
        assert player.commands == []
        assert player.shuffle is False

    def test_only_a_json_boolean_is_a_boolean(self, client: TestClient, player: FakePlayer):
        """The pair to the test above, so "everything is a 422" cannot be how it passes."""
        assert client.put("/player/shuffle", headers=AUTH, json={"enabled": True}).status_code == 200
        assert player.shuffle is True

    def test_consume_is_reported_but_has_no_route(self, client: TestClient):
        """Deliberate (TODO.md): it is part of the daemon's state, but a queue that eats itself is
        not a mode anybody asked for, and a control is easier to add later than to take away."""
        assert "consume" in client.get("/player", headers=AUTH).json()
        for method in ("GET", "PUT", "POST"):
            assert client.request(method, "/player/consume", headers=AUTH).status_code in (404, 405)

    def test_a_dead_daemon_is_503_not_a_bad_request(self, client: TestClient, player: FakePlayer):
        player.unavailable = True
        assert client.put("/player/repeat", headers=AUTH, json={"mode": "all"}).status_code == 503
        assert client.put("/player/shuffle", headers=AUTH, json={"enabled": True}).status_code == 503


class TestOutputSelection:
    def test_it_selects_the_configured_speaker(self, client: TestClient, player: FakePlayer):
        player.selected = False
        body = client.put("/player/output/select", headers=AUTH).json()
        assert body["output_selected"] is True
        assert player.selections == [(player.output_id, None)]

    def test_no_id_is_taken_from_the_caller(self, client: TestClient):
        """This route still takes none, and that is now a division of labour rather than the whole
        policy: it is the "enable the speaker this installation is configured for" button, which is
        what the output keeper and the remote's *wake* button want. Choosing *which* speaker is
        ``PUT /player/output`` below, where the id is checked against the daemon's live list and
        against ``TUNEBOX_OUTPUT_ALLOW``."""
        assert client.put("/player/output/select", headers=AUTH, json={"output_id": "x"}).status_code in (
            200,
            422,
        )

    def test_an_absent_speaker_is_a_404(self, client: TestClient, player: FakePlayer):
        """A genuinely missing resource: an unplugged soundbar is not on the network to be found."""
        player.present = False
        assert client.put("/player/output/select", headers=AUTH).status_code == 404

    def test_a_speaker_that_will_not_come_up_is_a_503_a_human_can_read(
        self, client: TestClient, player: FakePlayer
    ):
        """Listed but refusing to start — a Cast device in standby, measured on the real hardware.

        ``503`` because the client's move is to try again, not ``502`` (which is what a plain
        ``PlayerError`` gives) and not ``404`` (the speaker is right there in the output list). And
        the message must be a sentence: the daemon's own answer to this is an HTML error page, and
        the whole point of the dedicated error type is that no markup reaches the phone.
        """
        player.select_error = OutputUnreachable(
            "the player has output 9999 listed but could not bring it up after 3 attempt(s). "
            "A Cast speaker in standby does exactly this, so try again in a moment."
        )
        response = client.put("/player/output/select", headers=AUTH)
        assert response.status_code == 503
        assert "<html>" not in response.json()["error"]
        assert "try again in a moment" in response.json()["error"]


class TestChoosingADevice:
    """``PUT /player/output`` — the route behind the remote's device selector.

    It is a deliberate softening of the posture ``/player/output/select`` was written with, so these
    tests are mostly about the two things kept in place of the original flat refusal: the id must be
    one the daemon is currently reporting, and it must pass the allowlist. The third property is
    exclusivity, which is what makes a single-choice selector mean what it says.

    The extra outputs here are the ones a real OwnTone reported on this host (two Chromecast, two
    AirPlay 2, measured 2026-10-24) — this household has four, which is why a two-button control was
    the wrong shape in the first place.
    """

    KITCHEN = Output(id="55267078216198", name="MacBook Pro", type="AirPlay 2")
    TV = Output(id="9876543210", name="Living Room TV", type="Chromecast")

    @pytest.fixture
    def player(self) -> FakePlayer:
        return FakePlayer(others=[self.KITCHEN, self.TV])

    def test_it_switches_playback_to_the_named_output(
        self, client: TestClient, player: FakePlayer
    ):
        body = client.put("/player/output", headers=AUTH, json={"id": self.TV.id}).json()
        assert self.TV.id in player.selected_others
        assert any(output["id"] == self.TV.id and output["selected"] for output in body["outputs"])

    def test_it_switches_the_others_off(self, client: TestClient, player: FakePlayer):
        """What a single-choice selector means. The adapter's ``select_output`` deliberately does
        *not* do this — it is the keeper's primitive, which asserts one speaker and has no opinion
        about the rest — so ``select_only`` exists for the question a person asked."""
        player.selected = True
        client.put("/player/output", headers=AUTH, json={"id": self.TV.id})
        assert player.selected is False, "the previously selected speaker was switched off"
        assert player.selected_others == {self.TV.id}

    def test_the_target_is_enabled_before_the_others_are_switched_off(
        self, client: TestClient, player: FakePlayer
    ):
        """Ordering, and it is the difference between a failed handoff and a silent house: a speaker
        that refuses to wake must leave the one that was working still playing."""
        player.selected = True
        player.select_error = OutputUnreachable("the TV will not come up")
        assert client.put("/player/output", headers=AUTH, json={"id": self.TV.id}).status_code == 503
        assert player.selected is True, "the working speaker must not be switched off on a failure"

    def test_an_id_the_daemon_does_not_report_is_404(self, client: TestClient):
        """Raised here, never forwarded. Beyond tidiness: OwnTone answers an unknown id and a
        speaker that will not wake with the same ``400`` and the same HTML body, so without this
        check "you typed that wrong" and "the soundbar is asleep" would be one error."""
        response = client.put("/player/output", headers=AUTH, json={"id": "999999"})
        assert response.status_code == 404
        assert "999999" in response.json()["error"]

    def test_the_404_says_what_is_visible_instead(self, client: TestClient):
        response = client.put("/player/output", headers=AUTH, json={"id": "nope"})
        assert SOUNDBAR in response.json()["error"]

    def test_nothing_is_selected_when_the_id_is_refused(
        self, client: TestClient, player: FakePlayer
    ):
        client.put("/player/output", headers=AUTH, json={"id": "999999"})
        assert player.selections == []

    @pytest.mark.parametrize("body", [{}, {"id": ""}, {"id": 7}, {"output_id": "1"}, {"id": "1", "volume": 5}])
    def test_a_malformed_body_is_422(self, client: TestClient, body):
        assert client.put("/player/output", headers=AUTH, json=body).status_code == 422

    def test_a_dead_daemon_is_503(self, client: TestClient, player: FakePlayer):
        player.unavailable = True
        assert client.put("/player/output", headers=AUTH, json={"id": self.TV.id}).status_code == 503


class TestTheOutputAllowlist:
    """``TUNEBOX_OUTPUT_ALLOW`` — what turns "the remote must not control other people's devices"
    from an intention into a rule.

    It exists because mDNS does not stop at the front door: OwnTone lists a neighbour's Chromecast
    exactly like your own, and the device selector is a list of everything the daemon can see. Empty
    by default, because every installation that predates the selector has one speaker and a default
    that broke them would be the wrong answer.
    """

    NEIGHBOUR = Output(id="900900900", name="Nextdoor Nest Mini", type="Chromecast")

    @pytest.fixture
    def player(self) -> FakePlayer:
        return FakePlayer(others=[self.NEIGHBOUR])

    @pytest.fixture
    def config(self, config: Config) -> Config:
        return replace(config, output_allow=(SOUNDBAR,))

    def test_an_output_outside_the_allowlist_cannot_be_chosen(
        self, client: TestClient, player: FakePlayer
    ):
        response = client.put("/player/output", headers=AUTH, json={"id": self.NEIGHBOUR.id})
        assert response.status_code == 403
        assert "Nextdoor Nest Mini" in response.json()["error"]
        assert player.selections == []

    def test_it_is_403_rather_than_404(self, client: TestClient):
        """Pretending it does not exist would be a lie the UI can already disprove: ``GET /player``
        lists it with ``selectable: false``, which is how the selector greys it out."""
        assert (
            client.put("/player/output", headers=AUTH, json={"id": self.NEIGHBOUR.id}).status_code
            == 403
        )

    def test_the_allowed_speaker_still_works(self, client: TestClient, player: FakePlayer):
        assert (
            client.put("/player/output", headers=AUTH, json={"id": player.output_id}).status_code
            == 200
        )

    def test_get_player_says_which_outputs_are_selectable(self, client: TestClient):
        """Computed on this side of the wire, so the selector greys out exactly what the route would
        refuse rather than offering it and reporting a 403 after the tap."""
        outputs = {o["name"]: o["selectable"] for o in client.get("/player", headers=AUTH).json()["outputs"]}
        assert outputs[SOUNDBAR] is True
        assert outputs["Nextdoor Nest Mini"] is False

    def test_everything_is_selectable_when_the_allowlist_is_empty(
        self, client: TestClient, config: Config, store: JobStore, player: FakePlayer
    ):
        app = create_app(
            replace(config, output_allow=()),
            store=store,
            source=FakeSource(),
            tag_writer=RecordingTagWriter(),
            run_worker=False,
            player=player,
            keep_output=False,
        )
        with TestClient(app) as unrestricted:
            outputs = unrestricted.get("/player", headers=AUTH).json()["outputs"]
            assert all(output["selectable"] for output in outputs)

    def test_the_configured_speaker_is_allowed_even_if_the_list_forgets_it(
        self, client: TestClient, config: Config, store: JobStore, player: FakePlayer
    ):
        """Otherwise ``TUNEBOX_OUTPUT`` and ``TUNEBOX_OUTPUT_ALLOW`` could disagree, and the way that
        would surface is the output keeper selecting a speaker the remote then refuses to show as
        selectable — a contradiction in the one part of this system meant to need no attention."""
        app = create_app(
            replace(config, output_allow=("Some other room",)),
            store=store,
            source=FakeSource(),
            tag_writer=RecordingTagWriter(),
            run_worker=False,
            player=player,
            keep_output=False,
        )
        with TestClient(app) as client2:
            assert (
                client2.put("/player/output", headers=AUTH, json={"id": player.output_id}).status_code
                == 200
            )


class TestASpeakerThatWantsAPin:
    """An AirPlay output that will not play until somebody pairs it — found in use, not by design.

    Selecting a Mac advertising itself as an AirPlay receiver came back as ``OutputUnreachable``,
    whose message is Cast prose ending in "try again in a moment". The daemon's own log said
    otherwise, in as many words (OwnTone 29.3, 2026-10-24): *"The AirPlay 2 device 'MacBook Pro'
    requires a valid PIN or password"*, after the receiver answered ``403 Forbidden`` to PIN start.
    Retrying that forever is the one thing that cannot work, which is why it has its own status code
    and its own sentence.
    """

    MAC = Output(
        id="55267078216198", name="MacBook Pro", type="AirPlay 2", requires_auth=True
    )

    @pytest.fixture
    def player(self) -> FakePlayer:
        return FakePlayer(others=[self.MAC])

    def test_choosing_it_is_409_not_503(self, client: TestClient):
        """``503`` means "try again", which is exactly the advice that cannot help. ``409`` says the
        request was fine and some state has to change first — elsewhere, in OwnTone's own UI."""
        response = client.put("/player/output", headers=AUTH, json={"id": self.MAC.id})
        assert response.status_code == 409

    def test_the_message_names_the_speaker_and_the_actual_fix(self, client: TestClient):
        error = client.put("/player/output", headers=AUTH, json={"id": self.MAC.id}).json()["error"]
        assert "MacBook Pro" in error
        assert "PIN" in error
        assert "web interface" in error, "say where the pairing happens"
        assert "try again" not in error.lower()
        assert "Cast" not in error, "the cause this used to assert, wrongly"

    def test_it_is_not_even_attempted(self, client: TestClient, player: FakePlayer):
        """Once the daemon has told us, a tap must not cost four seconds of retrying to rediscover
        it. The adapter raises the same error when it *finds out* mid-attempt; this is the cheap
        path from the second tap onwards."""
        client.put("/player/output", headers=AUTH, json={"id": self.MAC.id})
        assert player.selections == []

    def test_get_player_flags_it_so_the_selector_can_grey_it_out(self, client: TestClient):
        """Rather than offering it and reporting a 409 after the tap — the same rule the allowlist
        follows. ``needs_pairing`` folds OwnTone's two spellings into the one the UI needs."""
        outputs = {o["name"]: o for o in client.get("/player", headers=AUTH).json()["outputs"]}
        assert outputs["MacBook Pro"]["needs_pairing"] is True
        assert outputs["MacBook Pro"]["requires_auth"] is True
        assert outputs[SOUNDBAR]["needs_pairing"] is False

    def test_a_working_speaker_is_unaffected(self, client: TestClient, player: FakePlayer):
        assert (
            client.put("/player/output", headers=AUTH, json={"id": player.output_id}).status_code
            == 200
        )


class TestPlayingIntoNothing:
    """The one state where every other field looks healthy and the room is silent.

    Measured on the real daemon: after ``All selected speakers failed to start`` OwnTone keeps
    reporting ``state: play`` and keeps advancing ``item_progress_ms``, so a remote that trusted the
    transport drew a moving progress bar for three minutes of silence.
    """

    def test_playing_with_no_output_attached_is_reported(
        self, client: TestClient, player: FakePlayer
    ):
        player.state = "play"
        player.selected = False
        assert client.get("/player", headers=AUTH).json()["playing_without_output"] is True

    def test_playing_on_a_selected_speaker_is_not_flagged(
        self, client: TestClient, player: FakePlayer
    ):
        player.state = "play"
        player.selected = True
        assert client.get("/player", headers=AUTH).json()["playing_without_output"] is False

    def test_not_playing_is_not_flagged_however_the_speakers_are(
        self, client: TestClient, player: FakePlayer
    ):
        """Stopped with nothing attached is the normal resting state, not a fault."""
        player.state = "stop"
        player.selected = False
        assert client.get("/player", headers=AUTH).json()["playing_without_output"] is False

    def test_an_invisible_speaker_while_playing_is_still_flagged(
        self, client: TestClient, player: FakePlayer
    ):
        """The soundbar vanishing mid-song is exactly when this matters most."""
        player.state = "play"
        player.present = False
        body = client.get("/player", headers=AUTH).json()
        assert body["output"] is None
        assert body["playing_without_output"] is True


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


class TestPlayingAQueueItem:
    """``PUT /player/queue/{item_id}/play`` — tapping a row in the remote's queue.

    The queue panel used to be a display: the rows were drawn with no action on them, so the only
    way to reach track seven was ⏭ six times. This is the route behind making the row itself the
    button, and what is worth testing is the two things that are *not* the happy path: an id the
    caller read two seconds ago may already be gone, and the queue must come out of this reordered by
    nothing and shortened by nothing — a jump is not a skip.
    """

    def queued(self, client: TestClient, config: Config) -> list[dict]:
        add_track(config, AUTOBAHN)
        add_track(config, MODEL)
        response = client.post(
            "/player/queue", headers=AUTH, json={"paths": [AUTOBAHN, MODEL], "mode": "replace"}
        )
        assert response.status_code == 201
        return response.json()["queue"]

    def test_it_plays_the_item_that_was_asked_for(
        self, client: TestClient, player: FakePlayer, config: Config
    ):
        items = self.queued(client, config)
        second = items[1]["id"]

        body = client.put(f"/player/queue/{second}/play", headers=AUTH).json()

        assert player.commands == [f"play_item:{second}"]
        assert body["item_id"] == second
        assert body["now_playing"]["title"] == "Das Model"
        assert body["state"] == "play"

    def test_the_queue_is_not_reordered_or_consumed(
        self, client: TestClient, player: FakePlayer, config: Config
    ):
        """A jump is not a skip. ``⏭`` on the way to track seven would have thrown six tracks away
        with ``consume`` on, and even without it the queue is what the panel is showing — a route
        that rebuilt it would make the row somebody tapped move under their thumb."""
        items = self.queued(client, config)

        body = client.put(f"/player/queue/{items[1]['id']}/play", headers=AUTH).json()

        assert [item["id"] for item in body["queue"]] == [item["id"] for item in items]
        assert [item["title"] for item in body["queue"]] == ["Autobahn", "Das Model"]

    def test_it_is_absolute_so_two_taps_land_in_the_same_place(
        self, client: TestClient, player: FakePlayer, config: Config
    ):
        """Same reasoning as the repeat *mode* rather than a cycle: the session is shared, so an id
        is idempotent where "the next one" would depend on a poll that is already stale."""
        items = self.queued(client, config)
        first = items[0]["id"]

        client.put(f"/player/queue/{first}/play", headers=AUTH)
        body = client.put(f"/player/queue/{first}/play", headers=AUTH).json()

        assert body["item_id"] == first
        assert player.commands == [f"play_item:{first}", f"play_item:{first}"]

    def test_the_track_already_loaded_is_not_a_dead_row(
        self, client: TestClient, player: FakePlayer, config: Config
    ):
        """The row that does nothing is the bug being fixed, and the currently-playing row is the one
        most likely to be tapped by accident *and* on purpose. It restarts the track — the remote's
        label for that row says "Restart", so the behaviour is not a surprise."""
        items = self.queued(client, config)
        current = client.get("/player", headers=AUTH).json()["item_id"]
        assert current == items[0]["id"]

        assert client.put(f"/player/queue/{current}/play", headers=AUTH).status_code == 200
        assert player.commands == [f"play_item:{current}"]

    def test_an_id_that_is_no_longer_in_the_queue_is_a_404(
        self, client: TestClient, player: FakePlayer, config: Config
    ):
        """The ordinary failure, not an exotic one: the remote polls every two seconds, so another
        phone clearing the queue while a thumb is in the air is the expected way to get here."""
        items = self.queued(client, config)
        gone = items[1]["id"]
        client.delete("/player/queue", headers=AUTH)

        response = client.put(f"/player/queue/{gone}/play", headers=AUTH)

        assert response.status_code == 404
        assert "queue" in response.json()["error"]
        assert player.commands == []

    @pytest.mark.parametrize("given", ["bogus", "0", "-1", "9999", "1.5", "%20", "null"])
    def test_a_nonsense_id_is_a_404_rather_than_a_500(
        self, client: TestClient, player: FakePlayer, config: Config, given: str
    ):
        """It is refused by *looking in the queue*, which is a check no spelling of an id can get
        past — and so the adapter's own integer check (``_item_id``) is unreachable from HTTP, which
        is what makes it a ``ValueError`` there and not a handled failure."""
        self.queued(client, config)

        assert client.put(f"/player/queue/{given}/play", headers=AUTH).status_code == 404
        assert player.commands == []

    def test_nothing_can_be_played_out_of_an_empty_queue(
        self, client: TestClient, player: FakePlayer
    ):
        assert client.put("/player/queue/1/play", headers=AUTH).status_code == 404
        assert player.commands == []

    def test_a_dead_daemon_is_503_and_not_a_missing_item(
        self, client: TestClient, player: FakePlayer, config: Config
    ):
        """The 404 must mean "that item is gone", so a daemon that cannot be asked at all has to
        answer differently — otherwise a remote would redraw a queue it has no information about."""
        items = self.queued(client, config)
        player.unavailable = True
        assert client.put(f"/player/queue/{items[0]['id']}/play", headers=AUTH).status_code == 503

    def test_there_is_no_play_by_position_route(self, client: TestClient, config: Config):
        """A position is only true of the poll it came from. The id is the whole point, so the
        obvious convenience route is asserted absent rather than merely not written."""
        for path in ("/player/queue/0", "/player/queue/position/0", "/player/queue/0/position"):
            assert client.put(path, headers=AUTH).status_code in (404, 405), path


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


class TestLibraryDelete:
    """``DELETE /library`` — the only destructive route, on the only irreplaceable state.

    The remote is otherwise a one-way door: a mistagged ingest or a wrong search result can be added
    from a phone and then only removed by finding a terminal. What that buys has to be paid for with
    the properties below — chiefly that a path from a client cannot name anything outside the music
    root, which is the same requirement ``POST /player/queue`` has and is met the same way.
    """

    def delete(self, client: TestClient, paths, **kwargs):
        return client.request("DELETE", "/library", headers=AUTH, json={"paths": paths}, **kwargs)

    def test_it_deletes_the_file(self, client: TestClient, config: Config):
        path = add_track(config, AUTOBAHN)

        response = self.delete(client, [AUTOBAHN])

        assert response.status_code == 200
        assert response.json()["deleted"] == [AUTOBAHN]
        assert not path.exists()

    def test_the_listing_agrees_immediately(self, client: TestClient, config: Config):
        add_track(config, AUTOBAHN)
        add_track(config, MODEL)

        self.delete(client, [AUTOBAHN])

        body = client.get("/library", headers=AUTH).json()
        assert [t["path"] for t in body["tracks"]] == [MODEL]
        assert body["total"] == 1

    def test_the_absolute_path_a_job_reports_is_accepted(
        self, client: TestClient, config: Config
    ):
        """``GET /jobs`` hands out the absolute path the ingest wrote, and the remote sends back
        whatever it was given."""
        absolute = add_track(config, AUTOBAHN)
        assert self.delete(client, [str(absolute)]).status_code == 200
        assert not absolute.exists()

    def test_emptied_directories_are_removed_and_reported(
        self, client: TestClient, config: Config
    ):
        """They are invisible in ``GET /library`` and visible in OwnTone's browse view, so leaving
        them behind would accumulate phantom artists that only the daemon shows."""
        add_track(config, AUTOBAHN)

        body = self.delete(client, [AUTOBAHN]).json()

        assert body["removed_directories"] == ["Kraftwerk/Singles", "Kraftwerk"]
        assert not (config.music_root / "Kraftwerk").exists()
        assert config.music_root.is_dir()

    def test_several_at_once(self, client: TestClient, config: Config):
        add_track(config, AUTOBAHN)
        add_track(config, MODEL)

        body = self.delete(client, [AUTOBAHN, MODEL]).json()

        assert sorted(body["deleted"]) == sorted([AUTOBAHN, MODEL])
        assert library.list_tracks(config.music_root).total == 0

    def test_two_spellings_of_one_file_are_one_delete(self, client: TestClient, config: Config):
        """Otherwise the second entry would be a 400 about a file the first entry had just removed,
        and the request would report failure having done exactly what was asked."""
        absolute = add_track(config, AUTOBAHN)

        response = self.delete(client, [AUTOBAHN, str(absolute)])

        assert response.status_code == 200
        assert response.json()["deleted"] == [AUTOBAHN]

    def test_the_player_is_told_to_re_index(
        self, client: TestClient, player: FakePlayer, config: Config
    ):
        """Same hook as after an ingest, for the same reason: the daemon's index is the only other
        place that thinks this file exists."""
        add_track(config, AUTOBAHN)
        before = player.rescans

        assert self.delete(client, [AUTOBAHN]).json()["warnings"] == []
        assert player.rescans == before + 1

    def test_a_dead_daemon_does_not_block_a_delete(
        self, client: TestClient, player: FakePlayer, config: Config
    ):
        """The file is on our disk, not OwnTone's. A rescan that cannot happen is a warning, never
        a failure — the same rule the ingest hook follows."""
        path = add_track(config, AUTOBAHN)
        player.unavailable = True

        response = self.delete(client, [AUTOBAHN])

        assert response.status_code == 200
        assert not path.exists()
        assert response.json()["warnings"] != []

    def test_the_loaded_track_is_refused(
        self, client: TestClient, player: FakePlayer, config: Config
    ):
        """Pulling the file out from under the daemon mid-song is the one case with an audible
        failure mode, so it is a 409 and not a surprise."""
        path = add_track(config, AUTOBAHN)
        player.enqueue_paths([path], start=True)

        response = self.delete(client, [AUTOBAHN])

        assert response.status_code == 409
        assert path.exists()

    def test_a_later_queue_entry_is_not_refused(
        self, client: TestClient, player: FakePlayer, config: Config
    ):
        """A queue is not a claim on the library: OwnTone discovers a missing file on its next
        ``LOAD``, and refusing here would make a queued album undeletable."""
        loaded = add_track(config, AUTOBAHN)
        queued = add_track(config, MODEL)
        player.enqueue_paths([loaded, queued], start=True)

        assert self.delete(client, [MODEL]).status_code == 200
        assert not queued.exists()

    def test_one_bad_path_deletes_nothing(
        self, client: TestClient, player: FakePlayer, config: Config
    ):
        """All-or-nothing, like ``POST /player/queue``: every path is resolved before any file is
        unlinked, so a typo in the second entry cannot cost the first track."""
        first = add_track(config, AUTOBAHN)

        response = self.delete(client, [AUTOBAHN, "../escape.m4a"])

        assert response.status_code == 400
        assert first.exists()

    def test_a_refusal_deletes_nothing_either(
        self, client: TestClient, player: FakePlayer, config: Config
    ):
        loaded = add_track(config, AUTOBAHN)
        other = add_track(config, MODEL)
        player.enqueue_paths([loaded], start=True)

        assert self.delete(client, [MODEL, AUTOBAHN]).status_code == 409
        assert other.exists()

    def test_the_ingest_history_stops_pointing_at_it(
        self, client: TestClient, store: JobStore, config: Config
    ):
        """So that re-ingesting the video downloads it again rather than being deduped against a
        file that is not there. The worker survives a stale entry anyway; this is the tidy half."""
        absolute = add_track(config, AUTOBAHN)
        store.record_track(video_id="vid00000001", path=str(absolute), title="Autobahn")
        assert store.find_track("vid00000001") is not None

        self.delete(client, [AUTOBAHN])

        assert store.find_track("vid00000001") is None

    @pytest.mark.parametrize(
        "given",
        [
            "../../../etc/passwd",
            "Kraftwerk/../../escape.m4a",
            "/etc/passwd",
            "..\\..\\windows\\system32\\config\\sam",
            "",
            "   ",
        ],
    )
    def test_a_path_outside_the_library_is_a_400(self, client: TestClient, given: str):
        assert self.delete(client, [given]).status_code == 400, given

    def test_a_non_music_file_in_the_library_is_a_400(self, client: TestClient, config: Config):
        """The queue database lives under the music root, so this must not be a way to remove it."""
        add_track(config, "notes.txt")
        assert self.delete(client, ["notes.txt"]).status_code == 400
        assert (config.music_root / "notes.txt").exists()

    def test_a_missing_file_is_a_400_not_a_500(self, client: TestClient):
        assert self.delete(client, ["A/Singles/ghost.m4a"]).status_code == 400

    def test_a_directory_cannot_be_deleted(self, client: TestClient, config: Config):
        add_track(config, AUTOBAHN)
        assert self.delete(client, ["Kraftwerk"]).status_code == 400
        assert (config.music_root / "Kraftwerk").is_dir()

    @pytest.mark.parametrize(
        "payload",
        [
            {"paths": []},
            {"paths": ["a.m4a"] * 101},
            {"paths": "not-a-list"},
            {},
            {"paths": ["a.m4a"], "prune": False},
        ],
    )
    def test_a_malformed_body_is_a_422(self, client: TestClient, payload: dict):
        assert client.request("DELETE", "/library", headers=AUTH, json=payload).status_code == 422

    def test_there_is_no_wildcard_path_route(self, client: TestClient, config: Config):
        """``DELETE /library/{path:path}`` is the shape this is deliberately not: a library path
        holds slashes and CJK, and URL-encoding one twice is how a delete names the wrong file."""
        add_track(config, AUTOBAHN)
        assert client.request(
            "DELETE", f"/library/{AUTOBAHN}", headers=AUTH
        ).status_code in (404, 405)
        assert (config.music_root / AUTOBAHN).exists()


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
