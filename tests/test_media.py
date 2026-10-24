"""Playing on the phone itself: capability tickets, the media route, and the browser-side player.

Three layers, tested from three sides, for the same reason the rest of the remote is:

* :mod:`tunebox.core.tickets` is pure, so it is tested directly and hard — a signature that can be
  forged, a payload that can be edited or an expiry that is not checked would each turn an
  unauthenticated route into a file-read primitive.
* ``GET /media/{ticket}`` is tested through the **real app**, with the real middleware, because the
  one property that matters most about it is that it answers *without* the token while nothing else
  does. A fake app would prove nothing about that.
* The browser half is read as source and, for the queue state machine, **run under deno** — the
  same approach ``tests/test_ui.py`` takes, and for the same reason: the local queue, shuffle and
  the repeat-at-the-end-of-the-queue rules are real logic, and nothing in a Python suite would
  otherwise execute a line of it.

Byte ranges get their own class. They are not a nicety: iOS Safari opens a track with ``bytes=0-1``
and refuses a ``200`` in reply, so a range bug is "no audio on iPhones" rather than "seeking is
slow".
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tunebox.api.app import create_app
from tunebox.api.media import content_type_for
from tunebox.config import Config
from tunebox.core import tickets
from tunebox.errors import ConfigError

TOKEN = "test-token-that-is-long-enough"
AUTH = {"X-Tunebox-Token": TOKEN}

#: A deterministic blob that is not valid audio. Nothing here decodes it — the route's job is to
#: hand over bytes, and using real audio would make every assertion about offsets unreadable.
BODY = bytes(range(256)) * 40  # 10240 bytes
TRACK = "Kraftwerk/Autobahn/01 - Autobahn.m4a"
CJK = "陳奕迅/Singles/K歌之王.m4a"

WEB = Path(__file__).resolve().parent.parent / "src" / "tunebox" / "web"


def asset(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def code_only(name: str) -> str:
    """``name`` with its comments removed.

    Needed by one small family of assertions below: "the token never appears in a URL" is a claim
    about the *code*, and this file's prose necessarily discusses ``?token=`` in order to explain
    why it is not there. Block comments go first, then whole-line ``//`` comments — a trailing
    ``//`` is not stripped, because ``protocol}//${host}`` is a URL and not a comment.
    """
    text = re.sub(r"/\*.*?\*/", "", asset(name), flags=re.S)
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )


@pytest.fixture
def music(tmp_path: Path) -> Path:
    root = tmp_path / "music"
    for relative in (TRACK, CJK):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(BODY)
    return root


@pytest.fixture
def config(music: Path, tmp_path: Path) -> Config:
    return Config(
        music_root=music,
        db_path=tmp_path / "jobs.db",
        api_token=TOKEN,
        ytdlp=("false",),
    )


@pytest.fixture
def app(config: Config):
    return create_app(config, run_worker=False, keep_output=False)


@pytest.fixture
def client(app) -> TestClient:
    with TestClient(app) as client:
        yield client


def ticket_url(client: TestClient, path: str = TRACK) -> str:
    response = client.post("/library/tickets", json={"paths": [path]}, headers=AUTH)
    assert response.status_code == 201, response.text
    return response.json()["tickets"][0]["url"]


# --------------------------------------------------------------------- the tickets


class TestTheTicketItself:
    """A ticket is the whole of the authorisation for an unauthenticated route, so these are the
    assertions that decide whether the feature is safe to have at all."""

    KEY = tickets.signing_key("a-token-that-is-long-enough")

    def test_a_minted_ticket_names_the_path_it_was_minted_for(self):
        verified = tickets.verify(tickets.mint(TRACK, key=self.KEY, ttl=60), key=self.KEY)
        assert verified.path == TRACK

    def test_a_cjk_path_survives_the_round_trip(self):
        """The library this was written for is full of them, and base64 of UTF-8 is the reason it
        works — a ticket that mangled a filename would be a 404 nobody could explain."""
        verified = tickets.verify(tickets.mint(CJK, key=self.KEY, ttl=60), key=self.KEY)
        assert verified.path == CJK

    def test_an_expired_ticket_is_refused(self):
        minted = tickets.mint(TRACK, key=self.KEY, ttl=60, now=time.time() - 7200)
        with pytest.raises(tickets.TicketError):
            tickets.verify(minted, key=self.KEY)

    def test_a_ticket_that_is_still_in_date_is_accepted(self):
        minted = tickets.mint(TRACK, key=self.KEY, ttl=3600)
        assert tickets.verify(minted, key=self.KEY).expires_in > 0

    def test_editing_the_payload_breaks_the_signature(self):
        """The attack this stops: take a valid ticket, swap the path inside it for ``/etc/passwd``,
        keep the signature. Rejected as a forgery, which is why the signature is checked *before*
        the payload is read."""
        payload, _, signature = tickets.mint(TRACK, key=self.KEY, ttl=60).partition(".")
        other = tickets.mint("something/else.m4a", key=self.KEY, ttl=60).partition(".")[0]
        with pytest.raises(tickets.TicketError):
            tickets.verify(f"{other}.{signature}", key=self.KEY)
        # ...and the original still verifies, so the test above failed for the right reason.
        assert tickets.verify(f"{payload}.{signature}", key=self.KEY).path == TRACK

    def test_editing_the_expiry_breaks_the_signature(self):
        """An expiry that could be extended by editing the URL would make the lifetime decorative."""
        minted = tickets.mint(TRACK, key=self.KEY, ttl=60, now=time.time() - 7200)
        signature = minted.partition(".")[2]
        fresh = tickets.mint(TRACK, key=self.KEY, ttl=86400).partition(".")[0]
        with pytest.raises(tickets.TicketError):
            tickets.verify(f"{fresh}.{signature}", key=self.KEY)

    def test_a_tampered_signature_is_refused(self):
        minted = tickets.mint(TRACK, key=self.KEY, ttl=60)
        flipped = minted[:-1] + ("A" if minted[-1] != "A" else "B")
        with pytest.raises(tickets.TicketError):
            tickets.verify(flipped, key=self.KEY)

    def test_a_ticket_from_another_token_does_not_verify(self):
        """Which is what makes rotating ``TUNEBOX_TOKEN`` invalidate every outstanding ticket, with
        no state anywhere to clear."""
        other = tickets.signing_key("a-completely-different-token")
        with pytest.raises(tickets.TicketError):
            tickets.verify(tickets.mint(TRACK, key=other, ttl=60), key=self.KEY)

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            "nodot",
            ".",
            "payload.",
            ".signature",
            "not-base64!.AAAAAAAAAAAAAAAAAAAAAA",
        ],
    )
    def test_rubbish_is_refused_rather_than_crashing(self, bad):
        """Every one of these arrives as a path segment from the open internet's point of view, so
        the failure has to be a refusal and never a traceback."""
        with pytest.raises(tickets.TicketError):
            tickets.verify(bad, key=self.KEY)

    def test_the_signing_key_is_not_the_token(self):
        """A leaked signature must not be a step towards the credential it was derived from."""
        token = "a-token-that-is-long-enough"
        assert tickets.signing_key(token) != token.encode("utf-8")
        assert tickets.signing_key(token) == tickets.signing_key(token)

    def test_an_empty_token_cannot_sign(self):
        """Signing with an empty key would produce tickets anybody could mint. The same refusal the
        token middleware makes, for the same reason."""
        with pytest.raises(ValueError):
            tickets.signing_key("")

    def test_a_zero_ttl_is_refused(self):
        with pytest.raises(ValueError):
            tickets.mint(TRACK, key=self.KEY, ttl=0)

    def test_two_mints_a_moment_apart_are_the_same_url(self):
        """So the browser's HTTP cache recognises a track it has already fetched. Without the expiry
        bucket, replaying the song that just finished would be a second download of it."""
        first = tickets.mint(TRACK, key=self.KEY, ttl=3600, now=1_700_000_000.0)
        second = tickets.mint(TRACK, key=self.KEY, ttl=3600, now=1_700_000_001.0)
        assert first == second

    def test_but_the_bucket_does_not_shorten_the_configured_life(self):
        """Rounded *up*, so quantising can only ever add seconds to a ticket's life."""
        now = 1_700_000_001.0
        minted = tickets.mint(TRACK, key=self.KEY, ttl=3600, now=now)
        verified = tickets.verify(minted, key=self.KEY, now=now)
        assert verified.expires_at >= int(now + 3600)

    def test_a_long_queue_of_tickets_eventually_differs(self):
        """The bucket is a minute, not forever: an hour later is a different URL."""
        first = tickets.mint(TRACK, key=self.KEY, ttl=3600, now=1_700_000_000.0)
        later = tickets.mint(TRACK, key=self.KEY, ttl=3600, now=1_700_003_600.0)
        assert first != later

    def test_a_ticket_is_one_url_path_segment(self):
        """No slash, no padding, nothing to escape — so the route can take it as ``{ticket}`` rather
        than as a ``:path`` converter, which is one fewer thing to get wrong."""
        minted = tickets.mint(CJK, key=self.KEY, ttl=60)
        assert "/" not in minted and "=" not in minted and "?" not in minted


# ------------------------------------------------------------------- minting route


class TestMintingTickets:
    def test_it_hands_back_one_ticket_per_path_in_order(self, client: TestClient):
        response = client.post(
            "/library/tickets", json={"paths": [CJK, TRACK]}, headers=AUTH
        )
        assert response.status_code == 201
        assert [item["path"] for item in response.json()["tickets"]] == [CJK, TRACK]

    def test_it_requires_the_token(self, client: TestClient):
        """The half of this feature that must *not* be exempt: a route that hands out capability
        URLs to anybody who asks is a route that has no capability at all."""
        assert client.post("/library/tickets", json={"paths": [TRACK]}).status_code == 401

    def test_the_url_is_relative_and_under_the_exempt_prefix(self, client: TestClient):
        url = ticket_url(client)
        assert url.startswith("/media/")
        assert "://" not in url

    def test_the_url_does_not_carry_the_token(self, client: TestClient):
        """The whole reason this is a capability URL. The token stays in a header, and out of
        uvicorn's access log (PLAN.md 3.2)."""
        assert TOKEN not in ticket_url(client)

    def test_an_m4a_is_labelled_audio_mp4(self, client: TestClient):
        """``audio/m4a`` is what some hosts' mime tables answer and Safari will not play it, so the
        table in ``tunebox.api.media`` is spelled out rather than inherited from the host."""
        response = client.post("/library/tickets", json={"paths": [TRACK]}, headers=AUTH)
        assert response.json()["tickets"][0]["content_type"] == "audio/mp4"

    def test_it_reports_how_long_the_tickets_last(self, client: TestClient, config: Config):
        response = client.post("/library/tickets", json={"paths": [TRACK]}, headers=AUTH)
        assert response.json()["expires_in"] == int(config.media_ticket_ttl)

    def test_an_absolute_path_is_accepted(self, client: TestClient, music: Path):
        """Because a finished job reports one, and the Ingests list's Play button hands it straight
        back — the same two spellings ``POST /player/queue`` takes."""
        response = client.post(
            "/library/tickets", json={"paths": [str(music / TRACK)]}, headers=AUTH
        )
        assert response.status_code == 201
        assert response.json()["tickets"][0]["path"] == TRACK

    @pytest.mark.parametrize("bad", ["../outside.m4a", "/etc/passwd", "", "   "])
    def test_a_path_outside_the_library_is_refused(self, client: TestClient, bad):
        response = client.post("/library/tickets", json={"paths": [bad]}, headers=AUTH)
        assert response.status_code == 400
        assert "error" in response.json()

    def test_a_non_audio_file_is_refused(self, client: TestClient, music: Path):
        (music / "notes.txt").write_text("hello", encoding="utf-8")
        response = client.post("/library/tickets", json={"paths": ["notes.txt"]}, headers=AUTH)
        assert response.status_code == 400

    def test_one_bad_path_mints_nothing(self, client: TestClient):
        """All-or-nothing, like every other list-of-paths route here: the caller is building a queue
        out of the answer, and a response quietly missing its third entry is a gap nobody can see."""
        response = client.post(
            "/library/tickets", json={"paths": [TRACK, "../nope.m4a"]}, headers=AUTH
        )
        assert response.status_code == 400
        assert "tickets" not in response.json()

    @pytest.mark.parametrize(
        "body",
        [
            {"paths": []},
            {"paths": ["a"] * 101},
            {"paths": TRACK},
            {"path": TRACK},
            {"paths": [TRACK], "ttl": 99999},
        ],
    )
    def test_a_malformed_body_is_422(self, client: TestClient, body):
        """Unknown fields are rejected rather than ignored — including a caller trying to choose its
        own lifetime, which is the server's decision and not a request parameter."""
        assert client.post("/library/tickets", json=body, headers=AUTH).status_code == 422


# ------------------------------------------------------------------ the media route


class TestStreamingToTheBrowser:
    def test_it_answers_without_the_token(self, client: TestClient):
        """The one thing this route exists for. ``<audio src>`` is a request the browser makes and a
        page cannot put a header on it, so a token-protected URL could never be played."""
        response = client.get(ticket_url(client))
        assert response.status_code == 200
        assert response.content == BODY

    def test_the_whole_file_comes_back_byte_for_byte(self, client: TestClient):
        response = client.get(ticket_url(client, CJK))
        assert response.content == BODY
        assert response.headers["content-length"] == str(len(BODY))

    def test_it_advertises_range_support(self, client: TestClient):
        """Browsers decide whether they can seek from this header before they try."""
        assert client.get(ticket_url(client)).headers["accept-ranges"] == "bytes"

    def test_it_is_labelled_as_audio(self, client: TestClient):
        assert client.get(ticket_url(client)).headers["content-type"] == "audio/mp4"

    def test_the_response_cannot_be_rendered_as_a_page(self, client: TestClient):
        """It is reachable without the token, so it carries its own refusal to be anything but
        bytes: no sniffing, no framing, and a CSP of its own rather than the shell's."""
        headers = client.get(ticket_url(client)).headers
        assert headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert "default-src 'none'" in headers["content-security-policy"]

    def test_a_cjk_filename_survives_the_disposition_header(self, client: TestClient):
        """RFC 5987's ``filename*``, because a header is bytes and the library is full of CJK. The
        ASCII fallback must still be present and must still be a quoted string."""
        disposition = client.get(ticket_url(client, CJK)).headers["content-disposition"]
        assert disposition.startswith("inline; ")
        assert "filename*=UTF-8''" in disposition
        assert re.search(r'filename="[\x20-\x7e]*"', disposition)

    def test_it_is_not_cached_by_a_shared_proxy(self, client: TestClient):
        assert "private" in client.get(ticket_url(client)).headers["cache-control"]

    def test_a_forged_ticket_is_403(self, client: TestClient):
        assert client.get("/media/not-a-real-ticket").status_code == 403

    def test_an_expired_ticket_is_403(self, client: TestClient, app):
        stale = tickets.mint(TRACK, key=app.state.media_key, ttl=60, now=time.time() - 7200)
        assert client.get(f"/media/{stale}").status_code == 403

    def test_a_ticket_signed_with_another_token_is_403(self, client: TestClient):
        forged = tickets.mint(TRACK, key=tickets.signing_key("x" * 32), ttl=60)
        assert client.get(f"/media/{forged}").status_code == 403

    def test_a_ticket_for_a_deleted_track_is_404(self, client: TestClient, music: Path):
        """The remote can delete tracks, so this is an ordinary thing to happen between building a
        queue and reaching its ninth song — not an error about the ticket."""
        url = ticket_url(client)
        (music / TRACK).unlink()
        assert client.get(url).status_code == 404

    def test_the_path_is_re_resolved_and_not_merely_trusted(self, client: TestClient, app):
        """Defence in depth, and the test that justifies the design.

        A signature proves the path came from us. It does not prove the path is still a track under
        the music root — so a ticket is minted here *directly*, with the server's own key, naming a
        file outside the library. A route that trusted its own signature would open it."""
        escaping = tickets.mint("../../../etc/passwd", key=app.state.media_key, ttl=60)
        response = client.get(f"/media/{escaping}")
        assert response.status_code in (400, 403, 404)
        assert b"root:" not in response.content

    def test_a_ticket_cannot_be_re_pointed_at_another_file(self, client: TestClient):
        """Swap the payload of one valid ticket onto the signature of another."""
        mine = ticket_url(client, TRACK).removeprefix("/media/")
        theirs = ticket_url(client, CJK).removeprefix("/media/")
        hybrid = f"{theirs.partition('.')[0]}.{mine.partition('.')[2]}"
        assert client.get(f"/media/{hybrid}").status_code == 403

    def test_head_answers_with_the_headers_and_no_body(self, client: TestClient):
        """A media element sends one to learn the length and the type before committing to a
        download, and a 405 there makes some browsers abandon the source."""
        response = client.head(ticket_url(client))
        assert response.status_code == 200
        assert response.content == b""
        assert response.headers["content-length"] == str(len(BODY))
        assert response.headers["accept-ranges"] == "bytes"

    def test_an_empty_file_is_served_rather_than_erroring(self, client: TestClient, music: Path):
        """A zero-byte file in the library is a failed copy, not a crash in the player: the browser
        gets a legitimate empty body and reports that it cannot decode it."""
        (music / TRACK).write_bytes(b"")
        response = client.get(ticket_url(client))
        assert response.status_code == 200
        assert response.content == b""


class TestByteRanges:
    """Not a nicety. iOS Safari opens a track with ``bytes=0-1`` and will not play a reply that
    comes back ``200`` with the whole file, so every assertion here is the difference between audio
    and silence on an iPhone — and seeking is a range request on every browser there is."""

    def test_the_two_byte_probe_ios_opens_with(self, client: TestClient):
        response = client.get(ticket_url(client), headers={"Range": "bytes=0-1"})
        assert response.status_code == 206
        assert response.headers["content-range"] == f"bytes 0-1/{len(BODY)}"
        assert response.content == BODY[:2]
        assert response.headers["content-length"] == "2"

    def test_a_middle_range(self, client: TestClient):
        response = client.get(ticket_url(client), headers={"Range": "bytes=100-199"})
        assert response.status_code == 206
        assert response.content == BODY[100:200]

    def test_an_open_ended_range_is_the_rest_of_the_file(self, client: TestClient):
        response = client.get(ticket_url(client), headers={"Range": "bytes=10000-"})
        assert response.status_code == 206
        assert response.content == BODY[10000:]
        assert response.headers["content-range"] == f"bytes 10000-{len(BODY) - 1}/{len(BODY)}"

    def test_a_suffix_range_is_the_last_n_bytes(self, client: TestClient):
        """``bytes=-16`` means the last sixteen bytes, not the first sixteen — which is the one
        range syntax it is easy to implement backwards, and the one a container's trailing index
        is read with."""
        response = client.get(ticket_url(client), headers={"Range": "bytes=-16"})
        assert response.status_code == 206
        assert response.content == BODY[-16:]

    def test_a_suffix_longer_than_the_file_is_the_whole_file(self, client: TestClient):
        response = client.get(ticket_url(client), headers={"Range": "bytes=-99999"})
        assert response.status_code == 206
        assert response.content == BODY

    def test_an_end_past_the_file_is_clamped(self, client: TestClient):
        """Asking for more than there is, is how a media element says "the rest of it"."""
        response = client.get(ticket_url(client), headers={"Range": "bytes=10200-99999"})
        assert response.status_code == 206
        assert response.content == BODY[10200:]

    def test_a_start_past_the_end_is_416_with_the_real_size(self, client: TestClient):
        """The size has to be in the 416, or a client cannot work out what it should have asked."""
        response = client.get(ticket_url(client), headers={"Range": "bytes=99999-"})
        assert response.status_code == 416
        assert response.headers["content-range"] == f"bytes */{len(BODY)}"

    def test_every_range_against_an_empty_file_is_416(self, client: TestClient, music: Path):
        (music / TRACK).write_bytes(b"")
        response = client.get(ticket_url(client), headers={"Range": "bytes=0-"})
        assert response.status_code == 416

    @pytest.mark.parametrize(
        "header",
        [
            "bytes=0-10,20-30",  # multipart/byteranges: legitimate to decline
            "items=0-10",  # a unit we do not speak
            "bytes=abc-def",
            "bytes=",
            "bytes=-",
            "nonsense",
        ],
    )
    def test_a_range_we_will_not_serve_falls_back_to_the_whole_file(
        self, client: TestClient, header
    ):
        """RFC 9110 lets a server answer any range request with the full representation, and doing
        that is much better than the alternative: a correct multipart/byteranges body is a pile of
        code no audio element has ever asked this for."""
        response = client.get(ticket_url(client), headers={"Range": header})
        assert response.status_code == 200
        assert response.content == BODY

    def test_a_head_with_a_range_describes_the_range(self, client: TestClient):
        response = client.head(ticket_url(client), headers={"Range": "bytes=0-99"})
        assert response.status_code == 206
        assert response.headers["content-length"] == "100"
        assert response.content == b""

    def test_the_ranges_reassemble_into_the_file(self, client: TestClient):
        """Chunked reads with a seek are where an off-by-one lives, so the file is fetched in pieces
        larger than one read and glued back together."""
        url = ticket_url(client)
        step = 3000
        pieces = []
        for start in range(0, len(BODY), step):
            response = client.get(
                url, headers={"Range": f"bytes={start}-{start + step - 1}"}
            )
            assert response.status_code == 206
            pieces.append(response.content)
        assert b"".join(pieces) == BODY


class TestContentTypes:
    """A media element decides whether it can play a file from this header, so a wrong one is
    silence rather than an error — and the host's own mime table is not a reliable source."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("a.m4a", "audio/mp4"),
            ("a.M4A", "audio/mp4"),
            ("a.mp3", "audio/mpeg"),
            ("a.flac", "audio/flac"),
            ("a.opus", "audio/ogg"),
            ("a.ogg", "audio/ogg"),
            ("a.wav", "audio/wav"),
        ],
    )
    def test_the_formats_the_library_holds(self, name, expected):
        assert content_type_for(Path(name)) == expected

    def test_an_unknown_extension_is_opaque_bytes(self):
        """Never ``text/html`` and never whatever the host guessed: this route is reachable without
        the token, so it must not label a response in a way a browser would try to *render*."""
        assert content_type_for(Path("a.weird")) == "application/octet-stream"
        assert content_type_for(Path("a.html")) == "application/octet-stream"


# ---------------------------------------------------------------- the off switch


class TestTurningItOff:
    def test_the_routes_and_the_exemption_go_together(self, config: Config):
        """``TUNEBOX_LOCAL_PLAYBACK=0`` must leave no token-free way to read a byte of audio — not
        merely a route that 404s. So the exemption is removed with the routes, and ``/media/x``
        answers the middleware's 401 rather than anything from this module."""
        from dataclasses import replace

        app = create_app(
            replace(config, local_playback=False), run_worker=False, keep_output=False
        )
        with TestClient(app) as client:
            assert client.post("/library/tickets", json={"paths": [TRACK]}, headers=AUTH).status_code == 404
            assert client.get("/media/anything").status_code == 401

    def test_the_page_is_told_so(self, config: Config):
        """Rather than leaving the script to probe for a 404: a feature that is off should not be
        something the UI has to go and find out about."""
        from dataclasses import replace

        off = create_app(replace(config, local_playback=False), run_worker=False, keep_output=False)
        with TestClient(off) as client:
            assert 'data-local-playback="0"' in client.get("/").text

    def test_the_page_is_told_when_it_is_on(self, client: TestClient):
        assert 'data-local-playback="1"' in client.get("/").text

    def test_a_zero_ttl_is_a_config_error_not_a_dead_feature(self, music: Path):
        """Zero would mean a ticket that has already expired when it is handed over, so every track
        would fail with a 403 that looked like a bug."""
        with pytest.raises(ConfigError, match="TUNEBOX_LOCAL_PLAYBACK=0"):
            Config(music_root=music, api_token=TOKEN, media_ticket_ttl=0)

    @pytest.mark.parametrize(
        ("env", "expected"),
        [({}, True), ({"TUNEBOX_LOCAL_PLAYBACK": "0"}, False), ({"TUNEBOX_LOCAL_PLAYBACK": "1"}, True)],
    )
    def test_the_env_var_is_read(self, env, expected):
        assert Config.from_env({**env, "TUNEBOX_TOKEN": TOKEN}).local_playback is expected

    def test_the_ttl_is_configurable(self):
        config = Config.from_env({"TUNEBOX_TOKEN": TOKEN, "TUNEBOX_MEDIA_TICKET_TTL": "300"})
        assert config.media_ticket_ttl == 300.0


# ------------------------------------------------------------------ the shell side


class TestTheShellAllowsAudioAtAll:
    def test_the_csp_permits_same_origin_media(self, client: TestClient):
        """Without ``media-src``, ``default-src 'none'`` blocks the audio element — and it does it
        silently, reporting only a generic decode failure, which is a very long afternoon."""
        policy = client.get("/").headers["content-security-policy"]
        assert "media-src 'self'" in policy

    def test_the_csp_is_still_otherwise_shut(self, client: TestClient):
        """Adding one directive must not have relaxed any other."""
        policy = client.get("/").headers["content-security-policy"]
        for directive in (
            "default-src 'none'",
            "script-src 'self'",
            "form-action 'none'",
            "frame-ancestors 'none'",
        ):
            assert directive in policy
        assert "unsafe-inline" not in policy
        assert "media-src *" not in policy

    def test_the_audio_element_ships_with_no_source(self):
        """Nothing is fetched until a track is loaded, and the src is always a capability URL."""
        markup = asset("index.html")
        element = re.search(r"<audio[^>]*>", markup)
        assert element, "the audio element is in the markup, not created by the script"
        assert 'id="local-audio"' in element.group(0)
        assert "src=" not in element.group(0)
        # No native controls: the transport is the control, and two sets that disagree is worse.
        assert "controls" not in element.group(0)

    def test_the_device_selector_is_one_labelled_control(self):
        """One selector for every device, rather than the two-button control this started as. The
        label matters: a bare `<select>` has no accessible name, and "Playing on" is the whole
        question the control answers."""
        markup = asset("index.html")
        card = markup[markup.index('class="card device"') : markup.index('id="local-audio"')]
        assert '<label for="device">' in card
        assert 'id="device"' in card
        assert "<select" in card and card.count("<select") == 1

    def test_it_separates_this_device_from_the_shared_speakers(self):
        """The distinction is not cosmetic. A shared speaker's queue is the daemon's and anybody in
        the house can change it; this device's queue is its own and nobody else can reach it. The
        two optgroups are where that is said on screen."""
        markup = asset("index.html")
        card = markup[markup.index('class="card device"') : markup.index('id="local-audio"')]
        assert 'id="device-self"' in card and 'label="This device"' in card
        assert 'id="device-speakers"' in card
        assert 'value="local"' in card

    def test_the_speaker_list_starts_empty_and_is_filled_by_the_script(self):
        """The outputs come from ``GET /player``; shipping any in the markup would mean a speaker
        name on screen before anything had asked the daemon whether it was there."""
        markup = asset("index.html")
        group = re.search(r'<optgroup id="device-speakers"[^>]*>(.*?)</optgroup>', markup, re.S)
        assert group and "<option" not in group.group(1)

    def test_the_handoff_button_ships_hidden_and_unlabelled(self):
        """Its label says which direction the music would move, so it cannot be written until the
        script knows which device this page is pointed at."""
        markup = asset("index.html")
        button = re.search(r'<button[^>]*id="btn-handoff"[^>]*>(.*?)</button>', markup, re.S)
        assert button and "hidden" in button.group(0)
        assert button.group(1).strip() == ""

    def test_the_old_two_button_picker_is_gone(self):
        """It modelled "house or phone", which is wrong twice over: this household has four outputs,
        and the laptop running this page is a device too."""
        markup = asset("index.html")
        for dead in ('id="out-house"', 'id="out-phone"', 'id="output-note"', 'id="speaker-state"'):
            assert dead not in markup, dead

    def test_the_service_worker_does_not_touch_media(self):
        """A cached track would be the audio equivalent of the cached now-playing screen the worker
        already refuses to serve — and the shell cache is a small allowlist, so this holds by
        construction rather than by a rule about ``/media/``."""
        worker = asset("sw.js")
        body = worker[worker.index("function isShell(") :]
        assert "/media" not in worker
        assert '"/ui/"' in body and 'url.pathname === "/"' in body
        assert "startsWith" in body


class TestTheRemoteSendsNoTokenToTheAudioElement:
    """The one mistake that would undo the whole ticket design: putting the shared token in a URL
    the browser fetches. It would land in uvicorn's access log once per track."""

    def test_the_src_is_always_a_minted_url(self):
        code = asset("app.js")
        assert "element.src = url" in code
        assert "localTicket(" in code
        assert "/library/tickets" in code

    def test_no_token_is_ever_interpolated_into_a_url(self):
        """Precise, and against the code rather than the prose: the page accepts a token in the URL
        *fragment* and documents why (a fragment is not sent to the server), so what is forbidden is
        the token reaching a query string or a string that becomes a ``src``."""
        code = code_only("app.js")
        assert "?token" not in code
        assert "&token=" not in code
        assert "token=${" not in code
        assert "state.token}" not in code, "a template literal holding the token builds a URL"

    def test_the_token_travels_only_as_a_header(self):
        code = asset("app.js")
        assert '"X-Tunebox-Token": state.token' in code
        assert code.count("state.token") >= 1


# ----------------------------------------------------- the queue, run under deno


class TestThePhoneQueueStateMachine:
    """The local queue, shuffle and repeat rules, executed.

    Read-the-source assertions cannot reach this: "what does ⏭ do on the last track with repeat
    off" is a question with a behaviour for an answer. So the functions are lifted out of ``app.js``
    and run under deno against a stub audio element — the same approach, and the same justification,
    as ``TestTheAddToPlaylistControlIsBuiltCorrectly`` in ``tests/test_ui.py``.
    """

    FUNCTIONS = (
        "localMode",
        "localTrack",
        "localReorder",
        "localEnqueue",
        "localPlayItem",
        "localNext",
        "localPrev",
        "localStop",
        "localClear",
        "localSetRepeat",
        "localSetShuffle",
        "localForget",
        "localView",
    )

    HARNESS = r"""
const REPEAT_NEXT = { off: "all", all: "single", single: "off" };
const LOCAL_PLAYBACK = true;
const LOCAL_DEVICE = "local";

const element = {
  paused: true,
  currentTime: 0,
  duration: 180,
  volume: 1,
  loop: false,
  _src: null,
  pause() { this.paused = true; },
  play() { this.paused = false; return Promise.resolve(); },
  load() {},
  getAttribute() { return this._src; },
  removeAttribute() { this._src = null; },
  set src(value) { this._src = value; this.paused = false; },
  get src() { return this._src; },
};
const audio = () => element;
const canSetVolume = () => true;

const state = {
  device: "local",
  progressMs: 0,
  outputs: [{ id: "2080974779", name: "Living Room speaker", type: "Chromecast", selected: true, selectable: true }],
  local: { queue: [], order: [], cursor: -1, repeat: "off", shuffle: false, error: null },
};

// The two things the engine delegates: loading a track (a fetch and a capability URL) and drawing
// the screen. Stubbed, so what is under test is the queue arithmetic and nothing else. `loads`
// records what would have been played, in order, which is the observable behaviour.
const loads = [];
async function localLoad({ autoplay }) {
  const track = localTrack();
  loads.push(track ? track.path : null);
  element._src = track ? `/media/ticket-for-${track.path}` : null;
  element.paused = !(autoplay && track);
}
function localPersist() {}
function localRender() {}

__FUNCTIONS__

// Math.random is replaced with a deterministic sequence, so "shuffled" is checkable rather than
// merely different: the same seed must give the same permutation on every run of this test.
let seed = 42;
Math.random = () => {
  seed = (seed * 1103515245 + 12345) % 2147483648;
  return seed / 2147483648;
};

const track = (name) => ({ path: `${name}.m4a`, title: name, artist: "A", album: null });
const ABCD = ["a", "b", "c", "d"].map(track);
const result = {};
const snapshot = () => ({
  queue: state.local.queue.map((t) => t.path),
  order: state.local.order.slice(),
  cursor: state.local.cursor,
  playing: localTrack() ? localTrack().path : null,
  paused: element.paused,
  loads: loads.slice(),
});
const reset = () => {
  state.local = { queue: [], order: [], cursor: -1, repeat: "off", shuffle: false, error: null };
  element._src = null;
  element.paused = true;
  element.currentTime = 0;
  loads.length = 0;
};

await (async () => {
  reset();
  await localEnqueue(ABCD, "replace", true);
  result.replaceStarts = snapshot();

  reset();
  await localEnqueue([track("a")], "append", false);
  await localEnqueue([track("b")], "append", false);
  result.appendWithoutStarting = snapshot();

  reset();
  await localEnqueue(ABCD, "replace", true);
  await localNext(true);
  await localNext(true);
  result.twoSkips = snapshot();

  // The last track, repeat off, reached on its own: stop rather than wrap.
  reset();
  await localEnqueue(ABCD, "replace", true);
  state.local.cursor = 3;
  await localNext(false);
  result.endOfQueue = snapshot();

  // The same place, but somebody pressed skip: still advances (and wraps), because a button that
  // does nothing is the bug, not the feature.
  reset();
  await localEnqueue(ABCD, "replace", true);
  state.local.cursor = 3;
  await localNext(true);
  result.skipPastTheEnd = snapshot();

  reset();
  await localEnqueue(ABCD, "replace", true);
  state.local.cursor = 3;
  await localSetRepeat("all");
  await localNext(false);
  result.repeatAllWraps = snapshot();
  result.repeatAllLoopFlag = element.loop;

  reset();
  await localEnqueue(ABCD, "replace", true);
  await localSetRepeat("single");
  result.repeatSingleUsesTheElementsLoop = element.loop;
  await localNext(false);
  result.repeatSingleReplays = snapshot();
  await localNext(true);
  result.repeatSingleStillSkips = snapshot();

  // Three seconds in, ⏮ restarts the track; before that it goes back one.
  reset();
  await localEnqueue(ABCD, "replace", true);
  await localNext(true);
  element.currentTime = 10;
  await localPrev();
  result.prevRestarts = { ...snapshot(), currentTime: element.currentTime };
  element.currentTime = 1;
  await localPrev();
  result.prevGoesBack = snapshot();

  reset();
  await localEnqueue(ABCD, "replace", true);
  await localPlayItem("local-2");
  result.tappingARow = snapshot();
  await localPlayItem("local-99");
  result.tappingAMissingRow = snapshot();

  reset();
  await localEnqueue(ABCD, "replace", true);
  await localStop();
  result.stopKeepsTheQueue = snapshot();

  reset();
  await localEnqueue(ABCD, "replace", true);
  await localClear();
  result.clearEmptiesIt = snapshot();

  // Shuffle must not skip the song that is playing, and must still contain every track exactly once.
  reset();
  await localEnqueue(ABCD, "replace", true);
  await localNext(true);
  const before = localTrack().path;
  await localSetShuffle(true);
  result.shuffleKeepsTheCurrentTrack = { ...snapshot(), before };
  await localSetShuffle(false);
  result.unshuffleRestoresTheOrder = snapshot();

  reset();
  await localEnqueue(ABCD, "replace", true);
  await localSetShuffle(true);
  const seen = [];
  for (let i = 0; i < 4; i += 1) { seen.push(localTrack().path); await localNext(true); }
  result.shufflePlaysEachOnce = seen.slice().sort();

  reset();
  await localEnqueue(ABCD, "replace", true);
  await localNext(true);
  localForget("c.m4a");
  result.forgetDropsARow = snapshot();
  localForget("b.m4a");
  result.forgetTheCurrentTrackStops = snapshot();

  reset();
  await localEnqueue(ABCD, "replace", true);
  result.view = localView();

  state.device = "2080974779";
  result.speakerModeIsNotLocal = localMode();
  state.device = "local";
})();

console.log(JSON.stringify(result));
"""

    @classmethod
    def run(cls) -> dict:
        deno = shutil.which("deno")
        if deno is None:  # pragma: no cover - deno is a declared requirement, but not here
            pytest.skip("deno is not installed")
        code = asset("app.js")
        sources = []
        for name in cls.FUNCTIONS:
            # ``async`` as well as plain, because half of these await a load.
            match = re.search(rf"^  (?:async )?function {name}\(", code, re.M)
            assert match, f"{name} is not a top-level function in app.js"
            start = match.start()
            end = code.index("\n  }\n", start) + 4
            sources.append(code[start:end])
        script = cls.HARNESS.replace("__FUNCTIONS__", "\n".join(sources))
        completed = subprocess.run(
            [deno, "eval", script], capture_output=True, text=True, timeout=60
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    @pytest.fixture(scope="class")
    def ran(self) -> dict:
        return self.run()

    def test_play_replaces_the_queue_and_starts_the_first_track(self, ran):
        state = ran["replaceStarts"]
        assert state["queue"] == ["a.m4a", "b.m4a", "c.m4a", "d.m4a"]
        assert state["playing"] == "a.m4a"
        assert state["paused"] is False
        assert state["loads"] == ["a.m4a"]

    def test_appending_loads_nothing_but_still_selects_something(self, ran):
        """"Nothing loaded" beside a full queue is a play button that does nothing when pressed —
        which the house speaker is allowed to be and this is not, because here the button *is* the
        player."""
        state = ran["appendWithoutStarting"]
        assert state["queue"] == ["a.m4a", "b.m4a"]
        assert state["playing"] == "a.m4a"
        assert state["paused"] is True
        assert state["loads"] == []

    def test_skipping_walks_the_queue(self, ran):
        assert ran["twoSkips"]["playing"] == "c.m4a"
        assert ran["twoSkips"]["loads"] == ["a.m4a", "b.m4a", "c.m4a"]

    def test_the_end_of_the_queue_stops_rather_than_wrapping(self, ran):
        """With repeat off. The track stays selected, so ⏯ plays it again — a stop is "that is
        enough", not "start over"."""
        state = ran["endOfQueue"]
        assert state["paused"] is True
        assert state["cursor"] == 3

    def test_pressing_skip_on_the_last_track_still_does_something(self, ran):
        """The distinction ``manual`` exists for: a queue that ended on its own stops, and a thumb
        on ⏭ wraps. A button that silently does nothing is the bug this separation avoids."""
        assert ran["skipPastTheEnd"]["playing"] == "a.m4a"
        assert ran["skipPastTheEnd"]["paused"] is False

    def test_repeat_the_queue_wraps_at_the_end(self, ran):
        assert ran["repeatAllWraps"]["playing"] == "a.m4a"
        assert ran["repeatAllWraps"]["paused"] is False
        assert ran["repeatAllLoopFlag"] is False

    def test_repeat_one_song_uses_the_elements_own_loop(self, ran):
        """Gapless, where waiting for ``ended`` and re-assigning ``src`` is a re-fetch and an
        audible seam."""
        assert ran["repeatSingleUsesTheElementsLoop"] is True

    def test_repeat_one_song_does_not_trap_the_skip_button(self, ran):
        """It replays on its own and still advances when asked — the same ``manual`` distinction."""
        assert ran["repeatSingleReplays"]["playing"] == "a.m4a"
        assert ran["repeatSingleStillSkips"]["playing"] == "b.m4a"

    def test_previous_restarts_the_track_once_it_is_underway(self, ran):
        """Ten seconds in, ⏮ means "I missed the beginning" — which is what every music player on
        the phone this runs on does."""
        assert ran["prevRestarts"]["playing"] == "b.m4a"
        assert ran["prevRestarts"]["currentTime"] == 0

    def test_previous_goes_back_a_track_near_the_start(self, ran):
        assert ran["prevGoesBack"]["playing"] == "a.m4a"

    def test_tapping_a_queue_row_plays_it(self, ran):
        assert ran["tappingARow"]["playing"] == "c.m4a"
        assert ran["tappingARow"]["paused"] is False

    def test_tapping_a_row_that_is_gone_changes_nothing(self, ran):
        assert ran["tappingAMissingRow"]["playing"] == "c.m4a"

    def test_stop_keeps_the_queue(self, ran):
        state = ran["stopKeepsTheQueue"]
        assert len(state["queue"]) == 4
        assert state["paused"] is True

    def test_clear_empties_it(self, ran):
        state = ran["clearEmptiesIt"]
        assert state["queue"] == [] and state["order"] == [] and state["cursor"] == -1
        assert state["playing"] is None

    def test_turning_shuffle_on_does_not_skip_the_current_song(self, ran):
        """The most annoying possible bug in a shuffle button, and the reason ``localReorder`` takes
        the index it must keep under the cursor."""
        state = ran["shuffleKeepsTheCurrentTrack"]
        assert state["playing"] == state["before"]
        assert state["loads"] == ["a.m4a", "b.m4a"], "no reload: the song keeps playing"

    def test_turning_shuffle_off_restores_the_order_somebody_built(self, ran):
        """Which is why the shuffle is a separate ``order`` array rather than a permutation of the
        queue itself."""
        assert ran["unshuffleRestoresTheOrder"]["order"] == [0, 1, 2, 3]

    def test_a_shuffled_queue_plays_every_track_exactly_once(self, ran):
        """Fisher–Yates over the remaining tracks, not "pick a random next": the latter replays
        songs while others never come up at all."""
        assert ran["shufflePlaysEachOnce"] == ["a.m4a", "b.m4a", "c.m4a", "d.m4a"]

    def test_a_deleted_track_leaves_the_queue(self, ran):
        """``DELETE /library`` can take a file out from under this queue, and no amount of asking
        the server would correct a list the server does not have."""
        state = ran["forgetDropsARow"]
        assert state["queue"] == ["a.m4a", "b.m4a", "d.m4a"]
        assert state["playing"] == "b.m4a", "the song playing is untouched"

    def test_deleting_the_track_that_is_playing_stops_it(self, ran):
        state = ran["forgetTheCurrentTrackStops"]
        assert "b.m4a" not in state["queue"]
        assert state["paused"] is True

    def test_the_view_is_shaped_like_the_player_api(self, ran):
        """One renderer for both outputs, which is only possible if this document has the same field
        names ``GET /player`` does — a second renderer would be a second place for the screen to be
        subtly different."""
        view = ran["view"]
        for field in (
            "state",
            "volume",
            "repeat",
            "shuffle",
            "consume",
            "item_id",
            "item_length_ms",
            "item_progress_ms",
            "now_playing",
            "outputs",
            "queue",
            "output",
            "output_selected",
            "playing_without_output",
        ):
            assert field in view, field
        assert view["local"] is True
        assert view["state"] == "play"
        # The cached speaker list comes along, and that is the mechanism rather than a detail: it is
        # what lets the one renderer draw the one device selector in both modes. An empty list here
        # would mean the selector lost every speaker the moment you chose this device.
        assert [o["name"] for o in view["outputs"]] == ["Living Room speaker"]
        assert view["now_playing"]["path"] == "a.m4a"
        assert [item["id"] for item in view["queue"]] == [
            "local-0",
            "local-1",
            "local-2",
            "local-3",
        ]

    def test_it_never_claims_to_be_playing_without_an_output(self, ran):
        """Impossible in this mode by construction: the output is the device drawing the page."""
        assert ran["view"]["playing_without_output"] is False

    def test_pointing_the_page_at_a_speaker_leaves_local_mode(self, ran):
        """`state.device` is "local" or an output id, so this is the whole of the mode test — and
        the id used here is a real one off this household's daemon."""
        assert ran["speakerModeIsNotLocal"] is False


class TestTheDeviceSelectorIsWiredUp:
    """The selector, executed, against this household's real output list.

    It is the entry point to everything else on the panel — the transport, the queue and the Library
    buttons all follow whatever it says — and nothing else reaches it: the id cross-check in
    ``tests/test_ui.py`` proves the elements exist and the class above proves the queue arithmetic,
    but neither would notice a stored device that is never read back or a speaker that has left the
    network still being drawn as the thing that is playing.

    The four outputs are the ones a real OwnTone reported on this host (two Chromecast, two
    AirPlay 2, measured 2026-10-24), because the control's whole reason for existing is that the
    household has more than one.
    """

    FUNCTIONS = (
        "localMode",
        "currentOutput",
        "handoffTarget",
        "readStoredDevice",
        "storeDevice",
        "reconcileDevice",
        "renderDevices",
        "renderDeviceNote",
        "renderHandoff",
    )

    HARNESS = r"""
const LOCAL_PLAYBACK = true;
const LOCAL_DEVICE = "local";
const OUTPUT_KEY = "tunebox.output";

const store = {};
const window = {
  localStorage: {
    getItem: (key) => (key in store ? store[key] : null),
    setItem: (key, value) => { store[key] = value; },
  },
};

class FakeNode {
  constructor(id) {
    this.id = id;
    this.hidden = false;
    this.disabled = false;
    this.textContent = "";
    this.value = "";
    this.children = [];
    this.attributes = {};
  }
  appendChild(child) { this.children.push(child); return child; }
  removeChild(child) { this.children = this.children.filter((c) => c !== child); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener() {}
  get firstChild() { return this.children[0] || null; }
  // What a <select> does: selectedIndex is -1 when no option matches the value.
  get selectedIndex() {
    const options = nodes["device-speakers"].children.concat(
      nodes["device-self"].hidden ? [] : [{ value: LOCAL_DEVICE }]
    );
    return options.findIndex((o) => o.value === this.value);
  }
}

const nodes = {};
const el = (id) => (nodes[id] ||= new FakeNode(id));
const document = { createElement: () => new FakeNode("option"), getElementById: el };

function clear(node) { node.children = []; }
function make(tag, className, text) {
  const node = new FakeNode(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

const element = { paused: false, currentTime: 0, pause() { this.paused = true; } };
const audio = () => element;
const canSetVolume = () => true;

// The four outputs a real OwnTone reported on this host.
const OUTPUTS = [
  { id: "265020192762911", name: "[LG] webOS TV OLED55C15LA", type: "AirPlay 2", selected: false, selectable: true },
  { id: "55267078216198", name: "MacBook Pro", type: "AirPlay 2", selected: false, selectable: true },
  { id: "2080974779", name: "Living Room speaker", type: "Chromecast", selected: true, selectable: true },
  { id: "2345877078", name: "Living Room TV", type: "Chromecast", selected: false, selectable: false },
];

const state = {
  device: LOCAL_DEVICE,
  outputs: OUTPUTS.map((o) => ({ ...o })),
  deviceKey: null,
  player: null,
  local: { queue: [], order: [], cursor: -1, repeat: "off", shuffle: false, error: null },
};

function localTrack() {
  const { queue, order, cursor } = state.local;
  if (cursor < 0 || cursor >= order.length) return null;
  return queue[order[cursor]] || null;
}

__FUNCTIONS__

const view = (extra) => ({ outputs: state.outputs, queue: [], ...extra });
const options = () =>
  nodes["device-speakers"].children.map((o) => ({
    value: o.value, label: o.textContent, disabled: o.disabled,
  }));
const snapshot = () => ({
  device: state.device,
  stored: store[OUTPUT_KEY] || null,
  selectValue: el("device").value,
  options: options(),
  speakersHidden: nodes["device-speakers"].hidden,
  selfHidden: nodes["device-self"].hidden,
  note: el("device-note").textContent,
  handoffHidden: el("btn-handoff").hidden,
  handoffLabel: el("btn-handoff").textContent,
});

const result = {};

// Local, with a queue of its own and a speaker available: handoff offers to move it.
state.local.queue = [{ path: "a.m4a", title: "a" }];
state.local.order = [0];
state.local.cursor = 0;
renderDevices(view({ local: true }));
renderDeviceNote(view({ local: true }));
renderHandoff(view({ local: true }));
result.onThisDevice = snapshot();

// Pointed at the configured speaker, with the room's queue on screen.
state.device = "2080974779";
state.deviceKey = null;
renderDevices(view({}));
renderDeviceNote(view({}));
renderHandoff(view({ queue: [{ id: "1" }, { id: "2" }] }));
result.onTheSpeaker = snapshot();

// An unchanged list must not rebuild the <option>s: a rebuild while the OS picker is open closes it.
const before = options().length;
renderDevices(view({}));
result.rebuiltWhenUnchanged = options().length !== before;

// The speaker this page was pointed at has left the network.
state.outputs = OUTPUTS.filter((o) => o.id !== "2080974779").map((o) => ({ ...o }));
reconcileDevice();
renderDevices(view({}));
renderDeviceNote(view({}));
result.speakerWentAway = snapshot();

// Nothing visible at all — a host with no OwnTone, which is a supported configuration.
state.outputs = [];
state.device = LOCAL_DEVICE;
state.deviceKey = null;
renderDevices(view({ local: true }));
renderDeviceNote(view({ local: true }));
renderHandoff(view({ local: true }));
result.noSpeakersAtAll = snapshot();

// Storage round trip.
state.outputs = OUTPUTS.map((o) => ({ ...o }));
storeDevice("55267078216198");
result.stored = readStoredDevice();
delete store[OUTPUT_KEY];
result.nothingStored = readStoredDevice();

// handoffTarget never offers a speaker the allowlist excludes.
state.device = LOCAL_DEVICE;
result.handoffTargetName = handoffTarget().name;
state.outputs = [{ id: "2345877078", name: "Living Room TV", type: "Chromecast", selected: true, selectable: false }];
result.handoffTargetWhenNoneAllowed = handoffTarget();

console.log(JSON.stringify(result));
"""

    @classmethod
    def run(cls) -> dict:
        deno = shutil.which("deno")
        if deno is None:  # pragma: no cover - deno is a declared requirement, but not here
            pytest.skip("deno is not installed")
        code = asset("app.js")
        sources = []
        for name in cls.FUNCTIONS:
            match = re.search(rf"^  (?:async )?function {name}\(", code, re.M)
            assert match, f"{name} is not a top-level function in app.js"
            sources.append(code[match.start() : code.index("\n  }\n", match.start()) + 4])
        script = cls.HARNESS.replace("__FUNCTIONS__", "\n".join(sources))
        completed = subprocess.run(
            [deno, "eval", script], capture_output=True, text=True, timeout=60
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    @pytest.fixture(scope="class")
    def ran(self) -> dict:
        return self.run()

    def test_every_visible_speaker_is_offered(self, ran):
        assert [option["label"] for option in ran["onThisDevice"]["options"]] == [
            "[LG] webOS TV OLED55C15LA · AirPlay 2",
            "MacBook Pro · AirPlay 2",
            "Living Room speaker · Chromecast",
            "Living Room TV · Chromecast (not available to this remote)",
        ]

    def test_a_speaker_the_allowlist_excludes_is_disabled_not_hidden(self, ran):
        """"That speaker exists and is not yours to use" is a different statement from "there is no
        such speaker", and ``TUNEBOX_OUTPUT_ALLOW`` is the only thing that can make the first true."""
        options = {option["label"]: option["disabled"] for option in ran["onThisDevice"]["options"]}
        assert options["Living Room TV · Chromecast (not available to this remote)"] is True
        assert options["Living Room speaker · Chromecast"] is False

    def test_the_type_is_shown_because_two_protocols_are_in_the_list(self, ran):
        """A household with both AirPlay and Chromecast devices can have two entries whose names say
        nothing about which is which — this one has two called "Living Room …"."""
        labels = [option["label"] for option in ran["onThisDevice"]["options"]]
        assert any("AirPlay 2" in label for label in labels)
        assert any("Chromecast" in label for label in labels)

    def test_this_device_says_its_queue_is_private_and_fragile(self, ran):
        """Both halves have to be said: the queue nobody else can touch, and the music that stops
        when this device does. The second reverses the property the rest of the system is built on."""
        note = ran["onThisDevice"]["note"]
        assert "private" in note
        assert "stops" in note

    def test_a_shared_speaker_says_its_queue_is_public(self, ran):
        """The point your question turned on: anybody in the house can change this one."""
        note = ran["onTheSpeaker"]["note"]
        assert "Living Room speaker" in note and "on" in note
        assert "shared" in note and "anyone in the house" in note

    def test_the_selector_follows_the_chosen_device(self, ran):
        assert ran["onThisDevice"]["selectValue"] == "local"
        assert ran["onTheSpeaker"]["selectValue"] == "2080974779"

    def test_an_unchanged_list_is_not_rebuilt(self, ran):
        """A rebuild while the OS picker is open closes it under the thumb that opened it."""
        assert ran["rebuiltWhenUnchanged"] is False

    def test_a_speaker_that_left_the_network_does_not_stay_on_screen(self, ran):
        """The outcome ruled out is a selector naming a speaker while the transport underneath it
        belongs to something else. It falls back to whatever is actually playing, or to this device."""
        went = ran["speakerWentAway"]
        assert went["device"] != "2080974779"
        assert went["selectValue"] == went["device"]

    def test_a_host_with_no_player_still_has_a_usable_selector(self, ran):
        """Local playback does not need OwnTone — its queue was never the daemon's — so "no speakers
        visible" is a sentence under the selector rather than a broken screen."""
        none = ran["noSpeakersAtAll"]
        assert none["device"] == "local"
        assert none["options"] == []
        assert none["speakersHidden"] is True
        assert none["selfHidden"] is False

    def test_handoff_offers_the_direction_the_music_would_move(self, ran):
        assert ran["onThisDevice"]["handoffHidden"] is False
        assert ran["onThisDevice"]["handoffLabel"] == "Continue on Living Room speaker"
        assert ran["onTheSpeaker"]["handoffHidden"] is False
        assert ran["onTheSpeaker"]["handoffLabel"] == "Continue on this device"

    def test_handoff_is_hidden_when_there_is_nothing_to_move(self, ran):
        """No speaker to send to, and an empty queue on this side."""
        assert ran["noSpeakersAtAll"]["handoffHidden"] is True

    def test_handoff_never_targets_a_speaker_the_allowlist_excludes(self, ran):
        """Otherwise the button would be an end-run around the thing that enforces "not ours"."""
        assert ran["handoffTargetName"] == "Living Room speaker"
        assert ran["handoffTargetWhenNoneAllowed"] is None

    def test_the_chosen_device_is_remembered(self, ran):
        assert ran["stored"] == "55267078216198"

    def test_a_device_was_never_chosen_means_this_one(self, ran):
        """The safe default is the device in your hand, not somebody else's room."""
        assert ran["nothingStored"] == "local"
