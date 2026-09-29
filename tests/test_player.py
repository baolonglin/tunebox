"""Phase 3 tests: the player adapter, the output keeper and the rescan hook.

Still offline. The OwnTone adapter is exercised against a **real HTTP server on loopback**
(:class:`StubOwnTone`) rather than a mocked ``urllib``, because half of what this adapter does is
build URLs and read status codes — a fake that skipped the transport would not have caught the
query-string encoding or the ``204 No Content`` handling, which are the two things most likely to
be wrong. ``conftest.no_network`` permits loopback and nothing else, so this stays inside the
Phase 1 "no network in CI" rule.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from tunebox.config import Config
from tunebox.core.models import REPEAT_MODES, Output
from tunebox.errors import (
    ConfigError,
    OutputNotFound,
    PlayerError,
    PlayerUnavailable,
    TuneboxError,
)
from tunebox.player.hooks import rescan_library
from tunebox.player.owntone import OwnTonePlayer, _path_expression, _quote_free_tail
from tunebox.player.selection import OutputKeeper

from fakes import SOUNDBAR, FakePlayer

# --------------------------------------------------------------------- the stub


class StubOwnTone:
    """A tiny HTTP server that answers like OwnTone, and remembers what it was asked.

    Responses are per-path callables so a test can make one endpoint fail without inventing a
    second server. Anything unset answers ``204`` with no body, which is what OwnTone does for
    commands and therefore the interesting default.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, list[str]], dict | None]] = []
        #: The query string exactly as it arrived on the wire. ``parse_qs`` decodes ``+`` to a space,
        #: so anything asserting *how* a value was encoded has to look here — a test that went
        #: through ``parse_qs`` was true of both encodings and proved nothing.
        self.raw_queries: list[tuple[str, str]] = []
        self.responses: dict[str, tuple[int, object]] = {}
        self.auth_headers: list[str | None] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:  # keep pytest output readable
                pass

            def _handle(self, method: str) -> None:
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                stub.auth_headers.append(self.headers.get("Authorization"))
                stub.raw_queries.append((parsed.path, parsed.query))
                stub.requests.append(
                    (method, parsed.path, parse_qs(parsed.query, keep_blank_values=True), body)
                )
                status, payload = stub.responses.get(f"{method} {parsed.path}", (204, None))
                if callable(payload):
                    payload = payload()
                if payload is None:
                    self.send_response(status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:
                self._handle("GET")

            def do_PUT(self) -> None:
                self._handle("PUT")

            def do_POST(self) -> None:
                self._handle("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # Connection threads must not outlive the test, and the default 0.5 s shutdown poll was
        # costing half a second of teardown per test — 35 s across this file for nothing.
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )

    def __enter__(self) -> "StubOwnTone":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def on(self, method: str, path: str, payload: object, status: int = 200) -> None:
        self.responses[f"{method} {path}"] = (status, payload)

    def params(self, method: str, path: str) -> dict[str, list[str]]:
        for seen_method, seen_path, params, _ in self.requests:
            if (seen_method, seen_path) == (method, path):
                return params
        raise AssertionError(f"{method} {path} was never called; saw {self.calls()}")

    def body(self, method: str, path: str) -> dict | None:
        for seen_method, seen_path, _, body in self.requests:
            if (seen_method, seen_path) == (method, path):
                return body
        raise AssertionError(f"{method} {path} was never called; saw {self.calls()}")

    def calls(self) -> list[str]:
        return [f"{method} {path}" for method, path, _, _ in self.requests]

    def raw_query(self, path: str) -> str:
        """The undecoded query string for the first request to ``path``."""
        for seen_path, query in self.raw_queries:
            if seen_path == path:
                return query
        raise AssertionError(f"{path} was never called; saw {self.calls()}")

    def count(self, method: str, path: str) -> int:
        return sum(1 for m, p, _, _ in self.requests if (m, p) == (method, path))


class RawServer:
    """Answers every request with one fixed body, whatever it is.

    :class:`StubOwnTone` always serialises JSON, so it cannot produce the two failure modes that
    matter here: a body that is not JSON at all (a proxy or captive portal answering instead of the
    daemon), and a body that is valid JSON but not an object.
    """

    def __init__(self, body: bytes, *, content_type: str = "application/json") -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", outer._content_type)
                self.send_header("Content-Length", str(len(outer._body)))
                self.end_headers()
                self.wfile.write(outer._body)

        self._body = body
        self._content_type = content_type
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )

    def __enter__(self) -> "RawServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"


OUTPUT_SOUNDBAR = {
    "id": "8365057286ba1880",
    "name": SOUNDBAR,
    "type": "Chromecast",
    "selected": False,
    "volume": 33,
    "needs_auth_key": False,
}
OUTPUT_LOCAL = {
    "id": "0",
    "name": "Computer",
    "type": "ALSA",
    "selected": True,
    "volume": 19,
    "needs_auth_key": False,
}


@pytest.fixture
def stub():
    with StubOwnTone() as server:
        yield server


@pytest.fixture
def config(tmp_path: Path, stub: StubOwnTone) -> Config:
    return Config(
        music_root=tmp_path / "music",
        player_url=stub.url,
        output_name=SOUNDBAR,
        player_timeout=5.0,
        output_check_interval=0.01,
    )


@pytest.fixture
def player(config: Config) -> OwnTonePlayer:
    return OwnTonePlayer(config)


# ----------------------------------------------------------------------- client


class TestClientTransport:
    def test_a_command_answering_204_is_a_success_not_an_empty_body_error(
        self, player: OwnTonePlayer, stub: StubOwnTone
    ):
        """OwnTone answers every transport command with 204, so this is the common case."""
        player.play()
        assert stub.calls() == ["PUT /api/player/play"]

    def test_a_dead_daemon_reads_as_unavailable_with_the_fix_in_the_message(self, config: Config):
        # Port 1 on loopback: nothing listens there, and it is still a local address so the
        # no-network guard stays satisfied.
        dead = OwnTonePlayer(replace(config, player_url="http://127.0.0.1:1"))
        with pytest.raises(PlayerUnavailable) as caught:
            dead.status()
        assert "Is OwnTone running?" in str(caught.value)

    def test_an_http_error_is_a_player_error_carrying_the_status(
        self, player: OwnTonePlayer, stub: StubOwnTone
    ):
        stub.on("PUT", "/api/player/play", {"error": "no queue"}, status=500)
        with pytest.raises(PlayerError) as caught:
            player.play()
        assert "HTTP 500" in str(caught.value)

    @pytest.mark.parametrize("status", [401, 403])
    def test_a_rejected_password_names_the_setting_that_fixes_it(
        self, player: OwnTonePlayer, stub: StubOwnTone, status: int
    ):
        """Distinct from a generic failure: the fix is a password, not a different command."""
        stub.on("GET", "/api/player", None, status=status)
        with pytest.raises(PlayerUnavailable) as caught:
            player.status()
        assert "TUNEBOX_PLAYER_PASSWORD" in str(caught.value)

    def test_a_password_is_sent_pre_emptively_as_basic_auth_with_the_admin_username(
        self, config: Config, stub: StubOwnTone
    ):
        """The username is not ours to choose: OwnTone hard-codes ``admin`` in
        ``httpd_request_is_authorized``. Sending an empty username — which looks reasonable, and is
        what the first version did — is answered with 401, so this asserts the exact bytes."""
        OwnTonePlayer(replace(config, player_password="hunter2")).rescan()

        header = stub.auth_headers[-1]
        assert header is not None and header.startswith("Basic ")
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
        assert decoded == "admin:hunter2"

    def test_no_password_means_no_authorization_header(self, player: OwnTonePlayer, stub):
        player.rescan()
        assert stub.auth_headers[-1] is None

    def test_an_empty_body_on_a_get_means_nothing_rather_than_raising(
        self, player: OwnTonePlayer, stub
    ):
        """A restarting daemon can answer 204 to anything; "no outputs" is the honest reading."""
        stub.on("GET", "/api/outputs", None, status=204)
        assert player.outputs() == []

    def test_a_non_json_body_is_a_player_error_not_a_crash(self, config: Config):
        """Mutation-checked: without the ``JSONDecodeError`` → ``PlayerError`` mapping this raises
        ``JSONDecodeError``, which is outside the exception tree and so outside every caller's error
        handling. The original version of this test passed an *empty* body and therefore proved
        nothing — it stayed green with the mapping deleted."""
        with RawServer(b"<html>proxy says no</html>", content_type="text/html") as raw:
            player = OwnTonePlayer(replace(config, player_url=raw.url))
            with pytest.raises(PlayerError) as caught:
                player.outputs()
        assert "non-JSON" in str(caught.value)
        assert "proxy says no" in str(caught.value)

    @pytest.mark.parametrize("body", [b"[]", b'"ok"', b"42", b"true"])
    def test_a_json_body_that_is_not_an_object_is_a_player_error(self, config: Config, body: bytes):
        """Every read in the adapter subscripts the result. A list or a string would otherwise
        surface as ``AttributeError`` from inside a getter — escaping the exception tree, and in the
        output keeper's case killing the thread that notices the soundbar coming back."""
        with RawServer(body, content_type="application/json") as raw:
            player = OwnTonePlayer(replace(config, player_url=raw.url))
            with pytest.raises(PlayerError):
                player.outputs()
            with pytest.raises(PlayerError):
                player.status()
            with pytest.raises(PlayerError):
                player.queue()
            with pytest.raises(PlayerError):
                player.library_info()

    def test_a_json_null_body_means_nothing_rather_than_an_error(self, config: Config):
        """``null`` is the one non-object body with an unambiguous reading: there is no data. Treated
        the same as the empty body of a ``204``, because every caller already handles "nothing"."""
        with RawServer(b"null", content_type="application/json") as raw:
            player = OwnTonePlayer(replace(config, player_url=raw.url))
            assert player.outputs() == []
            assert player.library_info() == {}


# ------------------------------------------------------------------------ modes


class TestPlaybackModes:
    """``repeat`` and ``shuffle``: the write half of state the adapter has always read.

    What is worth testing here is the *validation*, and the reason is measured rather than assumed:
    ``PUT /api/player/repeat?state=bogus`` and ``…/shuffle?state=bogus`` are both answered ``204``
    by the running daemon and silently taken as "off"/"false" (2026-09-27). So a check that lives in
    OwnTone does not exist, and a typo in a caller would look like success while quietly changing
    the mode. Hence: nothing invalid may reach the wire, and that is an assertion, not a docstring.
    """

    @pytest.mark.parametrize("mode", ["off", "all", "single"])
    def test_each_mode_is_sent_as_the_daemon_spells_it(
        self, player: OwnTonePlayer, stub: StubOwnTone, mode: str
    ):
        player.set_repeat(mode)
        assert stub.calls() == ["PUT /api/player/repeat"]
        assert stub.params("PUT", "/api/player/repeat")["state"] == [mode]

    def test_repeat_one_and_repeat_the_queue_are_different_requests(
        self, player: OwnTonePlayer, stub: StubOwnTone
    ):
        """The two features TODO.md asks for are one method and two values, and ``single`` is the
        daemon's word for "this song again" — not ``one``, which is the obvious guess."""
        player.set_repeat("single")
        player.set_repeat("all")
        states = [params["state"] for _, path, params, _ in stub.requests if path.endswith("/repeat")]
        assert states == [["single"], ["all"]]

    @pytest.mark.parametrize("given,sent", [(True, "true"), (False, "false")])
    def test_shuffle_sends_a_lowercase_json_boolean(
        self, player: OwnTonePlayer, stub: StubOwnTone, given: bool, sent: str
    ):
        """``str(True)`` is ``"True"``, which OwnTone cannot parse and would therefore take as
        false — so the spelling is asserted rather than left to Python's."""
        player.set_shuffle(given)
        assert stub.params("PUT", "/api/player/shuffle")["state"] == [sent]

    @pytest.mark.parametrize("mode", ["bogus", "one", "ALL", "", "single ", None, 1, True])
    def test_an_unknown_repeat_mode_never_reaches_the_daemon(
        self, player: OwnTonePlayer, stub: StubOwnTone, mode
    ):
        with pytest.raises(ValueError) as caught:
            player.set_repeat(mode)
        assert "off, all, single" in str(caught.value)
        assert stub.calls() == []

    @pytest.mark.parametrize("enabled", ["true", "false", "", 1, 0, None, "yes"])
    def test_shuffle_takes_a_bool_and_not_something_truthy(
        self, player: OwnTonePlayer, stub: StubOwnTone, enabled
    ):
        """``set_shuffle("false")`` is the trap: a non-empty string is truthy in Python, so a lax
        adapter would read it as "on" and the daemon would read the word as "off"."""
        with pytest.raises(ValueError):
            player.set_shuffle(enabled)
        assert stub.calls() == []

    def test_the_modes_are_read_back_from_the_player_state(
        self, player: OwnTonePlayer, stub: StubOwnTone
    ):
        """The read half already existed; this pins that the two halves use one vocabulary, so a UI
        can round-trip what it just set."""
        stub.on("GET", "/api/player", {"state": "play", "repeat": "single", "shuffle": True})
        stub.on("GET", "/api/outputs", {"outputs": []})
        status = player.status()
        assert status.repeat == "single"
        assert status.shuffle is True
        assert status.consume is False

    def test_every_mode_the_model_names_is_accepted_by_the_adapter(
        self, player: OwnTonePlayer, stub: StubOwnTone
    ):
        """One vocabulary, from the other end: this is the test that fails if
        :data:`tunebox.core.models.REPEAT_MODES` ever gains a mode the adapter does not accept.

        It proves nothing about the three modes themselves — the parametrised test above does that —
        but the API's ``Literal`` is built from that tuple, so a member the adapter refuses would be a
        value the edge waves through and the player then rejects with a ``ValueError``: a 500 on a
        request that was, by our own declaration, valid.
        """
        for mode in REPEAT_MODES:
            player.set_repeat(mode)
        assert len(stub.requests) == len(REPEAT_MODES)


# ---------------------------------------------------------------------- outputs


class TestOutputs:
    def test_outputs_are_parsed_including_the_cast_type(self, player: OwnTonePlayer, stub):
        stub.on("GET", "/api/outputs", {"outputs": [OUTPUT_SOUNDBAR, OUTPUT_LOCAL]})
        outputs = player.outputs()
        assert [o.name for o in outputs] == [SOUNDBAR, "Computer"]
        assert outputs[0].is_cast is True
        assert outputs[1].is_cast is False
        assert outputs[0].volume == 33

    def test_the_target_is_found_by_name_case_insensitively(self, config: Config, stub):
        stub.on("GET", "/api/outputs", {"outputs": [OUTPUT_LOCAL, OUTPUT_SOUNDBAR]})
        player = OwnTonePlayer(replace(config, output_name="living room SPEAKER"))
        assert player.target_output().id == OUTPUT_SOUNDBAR["id"]

    def test_an_id_wins_over_a_name(self, config: Config, stub):
        stub.on("GET", "/api/outputs", {"outputs": [OUTPUT_LOCAL, OUTPUT_SOUNDBAR]})
        player = OwnTonePlayer(replace(config, output_id="0", output_name=SOUNDBAR))
        assert player.target_output().name == "Computer"

    def test_a_missing_output_says_what_is_visible_and_why_it_might_not_be(
        self, player: OwnTonePlayer, stub
    ):
        stub.on("GET", "/api/outputs", {"outputs": [OUTPUT_LOCAL]})
        with pytest.raises(OutputNotFound) as caught:
            player.target_output()
        message = str(caught.value)
        assert "'Computer' (ALSA)" in message
        assert "unplugged" in message

    def test_selecting_an_output_does_not_disable_the_others(self, player: OwnTonePlayer, stub):
        """``PUT /api/outputs/{id}``, never ``PUT /api/outputs/set``.

        ``set`` takes the complete list of enabled outputs and switches off everything absent from
        it, so using it here would make "select the soundbar" silently mute a second speaker.
        """
        player.select_output("8365057286ba1880", volume=40)
        assert stub.calls() == ["PUT /api/outputs/8365057286ba1880"]
        assert stub.body("PUT", "/api/outputs/8365057286ba1880") == {
            "selected": True,
            "volume": 40,
        }
        assert "PUT /api/outputs/set" not in stub.calls()

    @pytest.mark.parametrize("given,sent", [(-5, 0), (0, 0), (50, 50), (100, 100), (250, 100)])
    def test_volume_is_clamped_before_it_reaches_the_daemon(
        self, player: OwnTonePlayer, stub, given: int, sent: int
    ):
        player.set_volume(given)
        assert stub.params("PUT", "/api/player/volume")["volume"] == [str(sent)]

    def test_a_relative_volume_is_sent_as_a_step_not_a_level(self, player: OwnTonePlayer, stub):
        """So two ``+5`` in a row add ten, without the CLI reading the level first."""
        player.change_volume(-5)
        params = stub.params("PUT", "/api/player/volume")
        assert params["step"] == ["-5"]
        assert "volume" not in params


# ------------------------------------------------------------------------ queue


TRACK = {
    "id": "10749",
    "title": "Autobahn (Single version 1974)",
    "artist": "Kraftwerk",
    "path": "/music/Kraftwerk/Singles/Autobahn (Single version 1974).m4a",
}


class TestQueue:
    def _library(self, stub: StubOwnTone, tracks: list[dict]) -> None:
        stub.on("GET", "/api/search", {"tracks": {"items": tracks}})

    def test_enqueue_resolves_paths_to_track_ids_and_adds_them_in_order(
        self, player: OwnTonePlayer, tmp_path: Path, stub
    ):
        first = tmp_path / "a.m4a"
        second = tmp_path / "b.m4a"
        first.write_bytes(b"")
        second.write_bytes(b"")
        self._library(
            stub,
            [{"id": "1", "path": str(first.resolve())}, {"id": "2", "path": str(second.resolve())}],
        )
        stub.on("POST", "/api/queue/items/add", {"count": 2, "items": []})

        player.enqueue_paths([first, second], clear=True, start=True)

        params = stub.params("POST", "/api/queue/items/add")
        assert params["uris"] == ["library:track:1,library:track:2"]
        assert params["clear"] == ["true"]
        assert params["playback"] == ["start"]

    def test_a_path_that_is_not_indexed_fails_before_the_queue_is_touched(
        self, player: OwnTonePlayer, tmp_path: Path, stub
    ):
        """A typo in the third path must not leave the queue half-replaced and playing."""
        good = tmp_path / "good.m4a"
        good.write_bytes(b"")
        self._library(stub, [{"id": "1", "path": str(good.resolve())}])

        with pytest.raises(PlayerError) as caught:
            player.enqueue_paths([good, tmp_path / "missing.m4a"], clear=True, start=True)

        assert "not in the player's library" in str(caught.value)
        assert "POST /api/queue/items/add" not in stub.calls()

    def test_the_path_match_is_byte_exact_because_owntone_matches_case_insensitively(
        self, player: OwnTonePlayer, tmp_path: Path, stub
    ):
        """``is``/``ends with`` are documented case-insensitive, so the server can return a sibling.

        Without the exact comparison in Python this would enqueue ``AUTOBAHN.m4a`` — a different
        file — and the soundbar would play the wrong track with no error anywhere.
        """
        wanted = tmp_path / "Autobahn.m4a"
        wanted.write_bytes(b"")
        self._library(
            stub,
            [
                {"id": "111", "path": str(tmp_path / "AUTOBAHN.m4a")},
                {"id": "222", "path": str(wanted.resolve())},
            ],
        )
        assert player.resolve_track(wanted)["id"] == "222"

    def test_resolve_polls_while_a_rescan_is_still_running(
        self, player: OwnTonePlayer, tmp_path: Path, stub
    ):
        """A track ingested a second ago is on disk but not yet in OwnTone's database."""
        track = tmp_path / "new.m4a"
        track.write_bytes(b"")
        attempts = {"n": 0}

        def answer():
            attempts["n"] += 1
            if attempts["n"] < 3:
                return {"tracks": {"items": []}}
            return {"tracks": {"items": [{"id": "7", "path": str(track.resolve())}]}}

        stub.on("GET", "/api/search", answer)
        assert player.resolve_track(track, timeout=10)["id"] == "7"
        assert attempts["n"] >= 3

    def test_clearing_with_no_paths_still_clears(self, player: OwnTonePlayer, stub):
        assert player.enqueue_paths([], clear=True) == []
        assert stub.calls() == ["PUT /api/queue/clear"]

    def test_a_music_root_reached_through_a_symlink_still_resolves(
        self, player: OwnTonePlayer, tmp_path: Path, stub
    ):
        """PLAN.md 3.1 puts the library on an external SSD, which is normally reached through a
        symlink. OwnTone indexes whatever path its own ``directories`` setting gave it, so matching
        only the fully-resolved form would report a perfectly indexed file as missing.
        """
        real = tmp_path / "ssd" / "Artist" / "Singles"
        real.mkdir(parents=True)
        track = real / "Track.m4a"
        track.write_bytes(b"")
        link = tmp_path / "music"
        link.symlink_to(tmp_path / "ssd")
        via_link = link / "Artist" / "Singles" / "Track.m4a"
        assert str(via_link) != str(via_link.resolve())

        # OwnTone recorded the *unresolved* path, because that is what it was configured with.
        self._library(stub, [{"id": "5", "path": str(via_link)}])
        assert player.resolve_track(via_link)["id"] == "5"

    def test_a_track_record_with_no_id_is_a_player_error_not_a_key_error(
        self, player: OwnTonePlayer, tmp_path: Path, stub
    ):
        track = tmp_path / "a.m4a"
        track.write_bytes(b"")
        self._library(stub, [{"path": str(track.resolve())}])  # no "id"

        with pytest.raises(PlayerError, match="no id"):
            player.enqueue_paths([track])

    def test_a_truncated_search_page_says_so_instead_of_blaming_the_library(
        self, player: OwnTonePlayer, tmp_path: Path, stub
    ):
        """With a degraded expression the server can match the whole library, and a truncated page
        looks exactly like "not indexed" — which sends the reader off to check OwnTone's
        ``directories`` for a problem that is not there."""
        track = tmp_path / "Track.m4a"
        track.write_bytes(b"")
        stub.on(
            "GET",
            "/api/search",
            {"tracks": {"items": [{"id": "1", "path": "/elsewhere/Other.m4a"}], "total": 9000}},
        )

        with pytest.raises(PlayerError) as caught:
            player.resolve_track(track)
        assert "too large for this lookup to be conclusive" in str(caught.value)
        assert "9000" in str(caught.value)

    def test_an_exact_expression_uses_a_small_page_and_a_degraded_one_a_large_page(
        self, player: OwnTonePlayer, tmp_path: Path, stub
    ):
        """The page size follows the precision of the query: ``path is`` can only return
        case-variants of one file, ``ends with`` can return an album."""
        exact = tmp_path / "Track.m4a"
        exact.write_bytes(b"")
        self._library(stub, [{"id": "1", "path": str(exact.resolve())}])
        player.resolve_track(exact)
        assert stub.params("GET", "/api/search")["limit"] == ["50"]

        quoted = tmp_path / 'we"ird' / "Track.m4a"
        quoted.parent.mkdir()
        quoted.write_bytes(b"")
        fresh = StubOwnTone()
        with fresh:
            other = OwnTonePlayer(replace(self._config_for(fresh), output_name=SOUNDBAR))
            fresh.on("GET", "/api/search", {"tracks": {"items": [{"id": "2", "path": str(quoted)}]}})
            other.resolve_track(quoted)
            assert fresh.params("GET", "/api/search")["limit"] == ["2000"]

    @staticmethod
    def _config_for(server: "StubOwnTone") -> Config:
        return Config(music_root=Path("/tmp/tunebox-test"), player_url=server.url)

    def test_now_playing_comes_from_the_queue_so_the_two_cannot_disagree(
        self, player: OwnTonePlayer, stub
    ):
        stub.on("GET", "/api/player", {"state": "play", "volume": 33, "item_id": 269})
        stub.on("GET", "/api/outputs", {"outputs": [OUTPUT_SOUNDBAR]})
        stub.on(
            "GET",
            "/api/queue",
            {"items": [{**TRACK, "id": 269, "position": 0, "length_ms": 207000}]},
        )
        status = player.status()
        assert status.playing is True
        assert status.now_playing is not None
        assert status.now_playing.title == TRACK["title"]
        assert status.now_playing.artist == "Kraftwerk"

    def test_item_id_zero_means_nothing_is_loaded(self, player: OwnTonePlayer, stub):
        stub.on("GET", "/api/player", {"state": "stop", "item_id": 0})
        stub.on("GET", "/api/outputs", {"outputs": []})
        status = player.status()
        assert status.now_playing is None
        # And it does not go looking in the queue for a track that cannot be there.
        assert "GET /api/queue" not in stub.calls()


# --------------------------------------------------------------- jumping in the queue


class TestPlayItem:
    """``play_item`` — ``PUT /api/player/play?item_id=N``, the call behind tapping a queue row.

    Measured against the running daemon, 2026-09-28: a jump to an item further down the queue is
    ``204`` and ``GET /api/player`` then reports that ``item_id``; ``99999``, ``bogus`` and an empty
    value are each ``400`` with an **HTML** body. That last detail is why the id is checked on this
    side — an unchecked bad id would reach a caller as a ``502`` quoting a markup fragment.
    """

    def test_it_asks_the_daemon_to_start_at_that_item(self, player: OwnTonePlayer, stub):
        player.play_item("48")
        assert stub.calls() == ["PUT /api/player/play"]
        assert stub.params("PUT", "/api/player/play")["item_id"] == ["48"]

    def test_an_integer_id_is_accepted_because_the_daemon_sends_integers(
        self, player: OwnTonePlayer, stub
    ):
        """``QueueItem.id`` is normalised to ``str`` by ``_as_str`` precisely because OwnTone sends
        some ids as JSON numbers, so a caller holding the raw value must not be punished for it."""
        player.play_item(48)
        assert stub.params("PUT", "/api/player/play")["item_id"] == ["48"]

    def test_the_position_parameter_is_not_used(self, player: OwnTonePlayer, stub):
        """``^/api/player/play$`` takes ``position`` as well, and it is the wrong one: the queue is
        shared, so a position is only true of the poll it came from."""
        player.play_item("7")
        assert "position" not in stub.params("PUT", "/api/player/play")

    @pytest.mark.parametrize(
        "given", ["", "   ", "bogus", "1.5", "-1", "1,2", "3 ; drop", None, True, 1.5, b"1", "٣", "²"]
    )
    def test_an_id_that_is_not_an_integer_never_reaches_the_daemon(
        self, player: OwnTonePlayer, stub, given
    ):
        """OwnTone's ids are unsigned integers. ``"٣"`` and ``"²"`` are the interesting ones:
        ``str.isdigit()`` is true of both and ``int()`` accepts the first, so a digit-shaped string
        that no C ``strtol`` will read is exactly what has to be refused rather than forwarded."""
        with pytest.raises(ValueError) as caught:
            player.play_item(given)
        assert "positive integer" in str(caught.value)
        assert stub.calls() == []

    def test_the_queue_is_left_alone(self, player: OwnTonePlayer, stub):
        """A jump is not a skip and not a re-add: one request, and none of it touches ``/api/queue``.
        Anything that rebuilt the queue would move the row under the thumb that tapped it."""
        player.play_item("2")
        assert stub.calls() == ["PUT /api/player/play"]

    def test_an_id_the_daemon_refuses_is_a_player_error_not_a_crash(
        self, player: OwnTonePlayer, stub
    ):
        """The measured race: the item was in the queue when the remote read it and gone by the time
        the command arrived. ``tunebox.api.remote`` checks the queue first so this is rare, but the
        window cannot be closed from here — and it has to stay inside the exception tree."""
        stub.on("PUT", "/api/player/play", None, status=400)
        with pytest.raises(PlayerError) as caught:
            player.play_item("99999")
        assert "HTTP 400" in str(caught.value)

    def test_it_is_a_command_and_not_a_query(self, player: OwnTonePlayer, stub):
        """No body, and no read of the state first: the response redraws the caller's screen, and a
        read-then-write here would race whatever else is driving the session."""
        player.play_item("5")
        assert stub.body("PUT", "/api/player/play") is None


# ------------------------------------------------------- the expression escaping


class TestPathExpressions:
    """OwnTone's expression lexer has no escape sequence — see the adapter's module docstring."""

    def test_an_ordinary_path_uses_an_exact_match(self):
        assert _path_expression(Path("/music/Kraftwerk/Singles/Autobahn.m4a")) == (
            'path is "/music/Kraftwerk/Singles/Autobahn.m4a"'
        )

    def test_cjk_and_spaces_need_no_special_handling(self):
        expression = _path_expression(Path("/music/單依純/Singles/好久不見 (Live).m4a"))
        assert expression == 'path is "/music/單依純/Singles/好久不見 (Live).m4a"'

    def test_a_quote_in_the_music_root_falls_back_to_a_suffix_match(self):
        """tunebox's own filenames can never contain ``"`` (naming.py maps it to ``_``), but the
        music root is user-supplied and might."""
        expression = _path_expression(Path('/home/rl/My "Music"/Kraftwerk/Singles/Autobahn.m4a'))
        assert expression == 'path ends with "/Kraftwerk/Singles/Autobahn.m4a"'
        assert '"Music"' not in expression

    def test_a_newline_also_terminates_a_string_and_is_handled_the_same_way(self):
        expression = _path_expression(Path("/root\nx/Artist/Singles/Track.m4a"))
        assert expression == 'path ends with "x/Artist/Singles/Track.m4a"'

    def test_a_path_ending_in_a_quote_degrades_to_the_widest_filter(self):
        """Pathological, but it must produce a *valid* expression rather than a broken one."""
        assert _path_expression(Path('/music/weird"')) == "media_kind is music"

    @pytest.mark.parametrize(
        "text,tail",
        [
            ("/a/b/c", "/a/b/c"),
            ('/a/"b"/c', "/c"),
            ('"', ""),
            ("", ""),
        ],
    )
    def test_quote_free_tail(self, text: str, tail: str):
        assert _quote_free_tail(text) == tail

    def test_the_expression_is_percent_encoded_not_plus_encoded(
        self, player: OwnTonePlayer, tmp_path: Path, stub
    ):
        """A ``+`` would be read literally by OwnTone's parser, so a space must become ``%20``.

        Asserted against the **raw** query string: the original version of this test went through
        ``parse_qs``, which decodes ``+`` to a space, so it was true of both encodings and stayed
        green with ``quote_via=quote`` removed.
        """
        track = tmp_path / "with space.m4a"
        track.write_bytes(b"")
        stub.on(
            "GET", "/api/search", {"tracks": {"items": [{"id": "1", "path": str(track.resolve())}]}}
        )
        player.resolve_track(track)

        query = stub.raw_query("/api/search")
        assert "%20" in query
        assert "+" not in query

    def test_a_uri_list_keeps_its_colons_and_commas_literal(
        self, player: OwnTonePlayer, tmp_path: Path, stub
    ):
        """``library:track:3`` is the daemon's own vocabulary; ``:`` and ``,`` are legal in a query
        value, and leaving them alone keeps the request readable in OwnTone's log — which is where
        the first hardware failure of this was diagnosed."""
        track = tmp_path / "a.m4a"
        track.write_bytes(b"")
        stub.on(
            "GET", "/api/search", {"tracks": {"items": [{"id": "3", "path": str(track.resolve())}]}}
        )
        stub.on("POST", "/api/queue/items/add", {"count": 1, "items": []})

        player.enqueue_paths([track])

        query = stub.raw_query("/api/queue/items/add")
        assert "library:track:3" in query
        assert "%3A" not in query


# --------------------------------------------------------------- output keeper


class TestOutputKeeper:
    """PLAN.md Phase 3: *"survives an OwnTone restart and a soundbar power-cycle without manual
    re-selection"* — and, just as importantly, does not fight the person in the room.

    The keeper is one rule over two pieces of state (see the module docstring), so these tests are
    organised as the transition sequences that rule has to get right. An audit found that the
    original set all started from ``selected=True``, which is exactly the initial state that hides
    the worst failure — so several of them start from a fresh, unselected daemon on purpose.
    """

    def keeper(self, player: FakePlayer, **overrides) -> OutputKeeper:
        config = Config(music_root=Path("/tmp/tunebox-test"), **overrides)
        return OutputKeeper(player=player, config=config)

    # -------------------------------------------------------------- first sight

    def test_first_sight_of_a_deselected_output_selects_it(self):
        """A fresh OwnTone with nothing selected."""
        player = FakePlayer(selected=False)
        keeper = self.keeper(player)

        assert keeper.check_once() is not None
        assert player.selections == [(player.output_id, None)]
        assert player.selected is True

    def test_an_already_selected_output_is_left_alone(self):
        player = FakePlayer(selected=True)
        keeper = self.keeper(player)
        assert keeper.check_once() is None
        assert player.selections == []

    def test_the_configured_volume_is_applied_on_selection(self):
        player = FakePlayer(selected=False)
        keeper = self.keeper(player, output_volume=25)
        keeper.check_once()
        assert player.selections == [(player.output_id, 25)]
        assert player.volume == 25

    # ------------------------------------------------------- the speaker leaves

    def test_a_power_cycle_reselects_without_anybody_asking(self):
        """present → gone → back must end up selected again."""
        player = FakePlayer(selected=True)
        keeper = self.keeper(player)
        keeper.check_once()

        player.present = False  # unplugged: drops off mDNS
        player.selected = False  # ...and OwnTone forgets the session
        assert keeper.check_once() is None
        assert player.selections == []

        player.present = True  # plugged back in
        assert keeper.check_once() is not None
        assert player.selected is True
        assert keeper.selections == 1
        assert keeper.recoveries == 1

    def test_a_power_cycle_after_the_keeper_did_the_selecting_also_reselects(self):
        """The defect this test exists for: the keeper used to record the *observation* that led to
        a selection ("off") rather than the state it had just established ("on").

        The consequence was permanent and silent — a fresh OwnTone, one keeper selection, then any
        power-cycle, and the soundbar stayed off forever because the keeper believed somebody had
        switched it off on purpose.
        """
        player = FakePlayer(selected=False)  # fresh daemon: the keeper does the selecting
        keeper = self.keeper(player)
        assert keeper.check_once() is not None
        assert keeper.selections == 1

        player.present = False
        player.selected = False
        keeper.check_once()

        player.present = True
        assert keeper.check_once() is not None, "the keeper must still want this speaker on"
        assert player.selected is True
        assert keeper.selections == 2

    def test_an_absent_speaker_does_not_raise_and_records_why(self):
        player = FakePlayer(present=False)
        keeper = self.keeper(player)
        assert keeper.check_once() is None
        assert keeper.last_error is not None
        assert "no output named" in keeper.last_error

    # -------------------------------------------------------- the daemon leaves

    def test_a_daemon_restart_re_asserts_the_selection(self):
        """PLAN.md Phase 3: *"survives an OwnTone restart ... without manual re-selection"*."""
        player = FakePlayer(selected=True)
        keeper = self.keeper(player)
        keeper.check_once()
        assert player.selections == []

        player.unavailable = True  # OwnTone stops
        assert keeper.check_once() is None

        player.unavailable = False  # ...and comes back with nothing selected
        player.selected = False
        assert keeper.check_once() is not None
        assert player.selected is True
        assert keeper.recoveries == 1

    def test_a_daemon_restart_after_the_keeper_did_the_selecting_also_reselects(self):
        """Same defect as the power-cycle case, by the other route into it."""
        player = FakePlayer(selected=False)
        keeper = self.keeper(player)
        keeper.check_once()
        assert keeper.selections == 1

        player.unavailable = True
        keeper.check_once()
        player.unavailable = False
        player.selected = False

        assert keeper.check_once() is not None
        assert keeper.selections == 2

    def test_a_daemon_restart_that_remembered_the_selection_changes_nothing(self):
        player = FakePlayer(selected=True)
        keeper = self.keeper(player)
        keeper.check_once()

        player.unavailable = True
        keeper.check_once()
        player.unavailable = False  # back, and it did remember

        assert keeper.check_once() is None
        assert player.selections == []
        assert keeper.recoveries == 1

    def test_a_dead_daemon_is_not_mistaken_for_the_speaker_being_switched_off(self):
        """An outage says nothing about whether the speaker should be on. Recording "off" there is
        what would lose the soundbar permanently."""
        player = FakePlayer(selected=True)
        keeper = self.keeper(player)
        keeper.check_once()

        player.unavailable = True
        assert keeper.check_once() is None
        assert keeper._wanted is True, "the desired state must survive an outage untouched"
        assert keeper.last_error is not None

    # ------------------------------------------------- respecting a human being

    def test_a_deliberate_deselection_is_respected(self):
        """Switching the speaker off in OwnTone's web UI must not be undone fifteen seconds later.

        This is the one case a naive reconciler gets wrong, and it would be worse than the problem
        it solves.
        """
        player = FakePlayer(selected=True)
        keeper = self.keeper(player)
        keeper.check_once()

        player.selected = False  # somebody pressed the speaker button in the web UI
        for _ in range(5):
            assert keeper.check_once() is None
        assert player.selections == []
        assert player.selected is False

    def test_a_power_cycle_does_not_resurrect_a_speaker_switched_off_on_purpose(self):
        """The recovery rule must not become a back door around the deselection rule — and the
        speaker leaving is the route an audit found was missing the check entirely."""
        player = FakePlayer(selected=True)
        keeper = self.keeper(player)
        keeper.check_once()

        player.selected = False  # switched off in the web UI
        keeper.check_once()

        player.present = False  # then unplugged
        keeper.check_once()
        player.present = True  # and plugged back in

        assert keeper.check_once() is None
        assert player.selections == []
        assert player.selected is False
        assert player.volume == 30, "and its volume must not be rewritten either"

    def test_a_daemon_restart_does_not_resurrect_a_speaker_switched_off_on_purpose(self):
        player = FakePlayer(selected=True)
        keeper = self.keeper(player)
        keeper.check_once()

        player.selected = False
        keeper.check_once()

        player.unavailable = True
        keeper.check_once()
        player.unavailable = False

        assert keeper.check_once() is None
        assert player.selections == []
        assert player.selected is False

    # ----------------------------------------------------------------- failures

    def test_a_failed_selection_is_retried_rather_than_remembered_as_done(self):
        """A speaker can drop off Wi-Fi between being listed and being selected.

        Concluding "present and off" at that point would file it as somebody's decision and leave it
        off forever.
        """
        player = FakePlayer(selected=False, fail_select=True)
        keeper = self.keeper(player)

        assert keeper.check_once() is None  # first sight, selection fails
        assert keeper.selections == 0
        assert keeper.last_error is not None

        player.fail_select = False
        assert keeper.check_once() is not None, "the next pass must try again"
        assert player.selected is True
        assert keeper.selections == 1

    def test_an_unexpected_error_from_the_daemon_moves_no_state(self):
        """A 500 means it is up and the speaker is presumably there; guessing either way would act
        on a fact we do not have."""
        player = FakePlayer(selected=True)
        keeper = self.keeper(player)
        keeper.check_once()

        def explode():
            raise PlayerError("HTTP 500")

        player.target_output = explode  # type: ignore[method-assign]
        assert keeper.check_once() is None
        assert keeper.recoveries == 0
        assert keeper._wanted is True
        assert keeper._away is False

    def test_a_non_tunebox_exception_does_not_escape_a_pass(self):
        """``check_once`` promises never to raise, and "never" has to include the exception types we
        did not think of — an adapter bug must not end playback recovery for the process."""
        player = FakePlayer(selected=True)
        keeper = self.keeper(player)

        def explode():
            raise AttributeError("'list' object has no attribute 'get'")

        player.target_output = explode  # type: ignore[method-assign]

        assert keeper.check_once() is None
        assert keeper.last_error is not None
        assert "AttributeError" in keeper.last_error

    def test_the_thread_survives_an_adapter_bug_and_keeps_checking(self):
        """The consequence of the above, and the reason it matters: nothing restarts this thread."""
        player = FakePlayer(selected=False)
        boom = {"on": True}
        real = player.target_output

        def flaky():
            if boom["on"]:
                raise AttributeError("nope")
            return real()

        player.target_output = flaky  # type: ignore[method-assign]
        keeper = self.keeper(player, output_check_interval=0.01)
        keeper.start()
        try:
            deadline = time.monotonic() + 5
            while keeper.checks < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert keeper.running is True, "an adapter bug must not kill the keeper thread"
            boom["on"] = False  # the bug stops happening
            deadline = time.monotonic() + 5
            while not player.selected and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            keeper.stop()
        assert player.selected is True, "and it must still recover afterwards"

    # ---------------------------------------------------------------- the thread

    def test_zero_interval_disables_the_keeper_entirely(self):
        keeper = self.keeper(FakePlayer(), output_check_interval=0)
        assert keeper.enabled is False
        keeper.start()
        assert keeper.running is False

    def test_the_thread_checks_immediately_and_then_repeatedly(self):
        player = FakePlayer(selected=False)
        keeper = self.keeper(player, output_check_interval=0.01)
        keeper.start()
        try:
            deadline = time.monotonic() + 5
            while keeper.checks < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            keeper.stop()
        assert keeper.checks >= 3
        assert player.selected is True
        # Selected once, not once per pass.
        assert keeper.selections == 1

    def test_the_thread_survives_a_daemon_that_is_down_the_whole_time(self):
        player = FakePlayer(unavailable=True)
        keeper = self.keeper(player, output_check_interval=0.01)
        keeper.start()
        try:
            deadline = time.monotonic() + 5
            while keeper.checks < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert keeper.running is True
        finally:
            keeper.stop()
        assert keeper.checks >= 3

    def test_starting_twice_is_refused(self):
        keeper = self.keeper(FakePlayer(), output_check_interval=0.05)
        keeper.start()
        try:
            with pytest.raises(RuntimeError, match="already started"):
                keeper.start()
        finally:
            keeper.stop()

    def test_stop_is_idempotent_and_safe_before_start(self):
        keeper = self.keeper(FakePlayer(), output_check_interval=0.05)
        keeper.stop()  # never started
        keeper.start()
        keeper.stop()
        keeper.stop()
        assert keeper.running is False

    def test_the_keeper_drives_the_real_adapter_over_http(self, config: Config, stub: StubOwnTone):
        """End to end through urllib, so the keeper is not only tested against a fake."""
        stub.on("GET", "/api/outputs", {"outputs": [OUTPUT_LOCAL, OUTPUT_SOUNDBAR]})
        keeper = OutputKeeper(player=OwnTonePlayer(config), config=config)

        selected = keeper.check_once()

        assert selected is not None and selected.name == SOUNDBAR
        assert stub.body("PUT", f"/api/outputs/{OUTPUT_SOUNDBAR['id']}") == {"selected": True}


# ------------------------------------------------------------------ rescan hook


class TestRescanHook:
    """PLAN.md Phase 3: *"library rescan hook after ingest"*, and the rule that it can never cost
    a finished download."""

    def test_a_successful_rescan_returns_no_warning(self):
        player = FakePlayer()
        assert rescan_library(player) is None
        assert player.rescans == 1

    def test_a_dead_daemon_becomes_a_warning_not_an_exception(self):
        player = FakePlayer(unavailable=True)
        warning = rescan_library(player)
        assert warning is not None
        assert "library rescan failed" in warning

    def test_an_unexpected_exception_is_also_contained(self):
        player = FakePlayer(rescan_error=RuntimeError("boom"))
        warning = rescan_library(player)
        assert warning is not None
        assert "RuntimeError: boom" in warning

    def test_it_goes_over_http_to_put_api_update(self, player: OwnTonePlayer, stub: StubOwnTone):
        assert rescan_library(player) is None
        assert stub.calls() == ["PUT /api/update"]

    def test_wait_for_scan_blocks_until_the_library_settles(
        self, player: OwnTonePlayer, stub: StubOwnTone
    ):
        """A rescan returns as soon as it is *queued*, and while it runs OwnTone deletes and
        re-inserts rows — so a track id read too early is rejected as ``Invalid uri``. Observed on
        hardware as a 500 that looked like a malformed request."""
        polls = {"n": 0}

        def answer():
            polls["n"] += 1
            return {"songs": 3, "updating": polls["n"] < 3}

        stub.on("GET", "/api/library", answer)

        assert player.wait_for_scan(timeout=10, poll=0.01) is True
        assert polls["n"] >= 3

    def test_wait_for_scan_gives_up_rather_than_hanging(
        self, player: OwnTonePlayer, stub: StubOwnTone
    ):
        stub.on("GET", "/api/library", {"songs": 3, "updating": True})
        assert player.wait_for_scan(timeout=0.05, poll=0.01) is False

    def test_wait_for_scan_reports_failure_when_the_daemon_dies_mid_scan(self, config: Config):
        dead = OwnTonePlayer(replace(config, player_url="http://127.0.0.1:1"))
        assert dead.wait_for_scan(timeout=0.1, poll=0.01) is False


# ----------------------------------------------------------------------- config


class TestPlayerConfig:
    def test_defaults_point_at_a_local_owntone_and_the_household_soundbar(self):
        config = Config()
        assert config.player_base_url == "http://127.0.0.1:3689"
        assert config.output_name == SOUNDBAR
        assert config.rescan_after_ingest is True

    def test_a_trailing_slash_does_not_produce_a_double_slash_path(self):
        assert Config(player_url="http://pi:3689/").player_base_url == "http://pi:3689"

    @pytest.mark.parametrize("url", ["pi:3689", "ftp://pi", "", "127.0.0.1:3689"])
    def test_a_url_without_an_http_scheme_is_refused(self, url: str):
        with pytest.raises(ConfigError):
            Config(player_url=url)

    @pytest.mark.parametrize("volume", [-1, 101])
    def test_an_impossible_output_volume_is_refused(self, volume: int):
        with pytest.raises(ConfigError):
            Config(output_volume=volume)

    def test_a_negative_check_interval_is_refused_but_zero_is_allowed(self):
        assert Config(output_check_interval=0).output_check_interval == 0
        with pytest.raises(ConfigError):
            Config(output_check_interval=-1)

    def test_the_environment_drives_every_player_setting(self):
        config = Config.from_env(
            {
                "TUNEBOX_PLAYER_URL": "http://pi.local:3689",
                "TUNEBOX_PLAYER_PASSWORD": "secret",
                "TUNEBOX_OUTPUT": "Kitchen",
                "TUNEBOX_OUTPUT_ID": "42",
                "TUNEBOX_OUTPUT_VOLUME": "35",
                "TUNEBOX_OUTPUT_CHECK_INTERVAL": "30",
                "TUNEBOX_RESCAN_AFTER_INGEST": "0",
            }
        )
        assert config.player_url == "http://pi.local:3689"
        assert config.player_password == "secret"
        assert config.output_name == "Kitchen"
        assert config.output_id == "42"
        assert config.output_volume == 35
        assert config.output_check_interval == 30
        assert config.rescan_after_ingest is False

    def test_an_unset_output_volume_means_leave_it_alone(self):
        assert Config.from_env({}).output_volume is None
        assert Config.from_env({"TUNEBOX_OUTPUT_VOLUME": ""}).output_volume is None

    def test_a_non_numeric_output_volume_is_a_config_error_not_a_value_error(self):
        with pytest.raises(ConfigError):
            Config.from_env({"TUNEBOX_OUTPUT_VOLUME": "loud"})

    def test_player_errors_are_part_of_the_one_exception_tree(self):
        """So the CLI and the API keep telling "this failed" apart from "we have a bug"."""
        for exc in (PlayerError, PlayerUnavailable, OutputNotFound):
            assert issubclass(exc, TuneboxError)
        assert issubclass(PlayerUnavailable, PlayerError)
        assert issubclass(OutputNotFound, PlayerError)

    def test_secrets_do_not_appear_in_the_config_repr(self):
        """Logging a Config is exactly what someone does while debugging a startup problem, and a
        traceback renders one for free. Neither may put a password in a log file."""
        config = Config(api_token="t" * 32, player_password="hunter2")
        text = repr(config)

        assert "hunter2" not in text
        assert "t" * 32 not in text
        # ...and the values are still readable when asked for deliberately.
        assert config.player_password == "hunter2"


# ------------------------------------------------------------------------ model


class TestOutputModel:
    @pytest.mark.parametrize("kind,expected", [("Chromecast", True), ("cast", True), ("AirPlay", False), ("", False)])
    def test_is_cast(self, kind: str, expected: bool):
        assert Output(id="1", name="x", type=kind).is_cast is expected

    def test_selected_outputs_filters(self):
        from tunebox.core.models import PlayerStatus

        status = PlayerStatus(
            state="play",
            outputs=(
                Output(id="1", name="a", selected=True),
                Output(id="2", name="b", selected=False),
            ),
        )
        assert [o.name for o in status.selected_outputs] == ["a"]
        assert status.playing is True
