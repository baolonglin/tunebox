"""OwnTone adapter: the :class:`~tunebox.core.ports.Player` port, spoken over OwnTone's JSON API.

This is the *primary* playback path in PLAN.md 3.1 and the one its risk table is most nervous
about, because the SN9YG has no AirPlay and OwnTone's Chromecast output is the less-travelled half
of OwnTone. Everything here is therefore written so that the only thing which has to change if
fallback A (``pychromecast``) is ever needed is this file.

Three decisions that are not style:

* **stdlib ``urllib``, not httpx or requests.** The requests are a handful of tiny JSON calls to a
  daemon on loopback. Adding a runtime HTTP dependency to a Raspberry Pi install to save six lines
  of boilerplate is a bad trade, and ``httpx`` is currently a *test-only* dependency
  (``pyproject.toml``) — quietly promoting it to a runtime one is exactly the kind of drift that
  makes a deployment fail on the host and not in CI.

* **Paths are resolved to OwnTone track ids by us, and matched exactly in Python.** OwnTone's queue
  takes ``library:track:<id>`` uris or a smart-playlist expression, and its expression operators
  (``is``, ``includes``, ...) are documented as *case-insensitive* — so ``path is "…/Autobahn.m4a"``
  can legitimately return a different file on a case-insensitive filesystem. The expression is
  therefore only a filter to keep the response small; the decision is a byte-comparison here. See
  :meth:`OwnTonePlayer.resolve_track`.

* **A quote in a path is handled rather than escaped.** OwnTone's expression lexer is
  ``\\"[^\\"\\n]*[\\"\\n]`` (``src/parsers/smartpl_lexer.l``): there is no escape sequence, so a
  ``"`` inside a value simply ends the string. tunebox's own filenames can never contain one
  (:mod:`tunebox.core.naming` maps it to ``_``), but the music *root* is user-supplied and might.
  So the expression is built from the longest quote-free tail of the path with ``ends with``, and
  the exact match still happens here. No amount of escaping would have worked.
"""

from __future__ import annotations

import base64
import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Sequence

from ..config import Config
from ..core.models import REPEAT_MODES, Output, PlayerStatus, QueueItem
from ..errors import OutputNotFound, OutputUnreachable, PlayerError, PlayerUnavailable

log = logging.getLogger(__name__)

#: Pause after a refused ``enable``, doubling per attempt. Half a second because the refusal itself
#: already takes about one (the connect to port 8009 has to time out first), so the useful part of
#: the wait is mostly the daemon's, not ours.
_ENABLE_BACKOFF = 0.5

#: Characters that terminate a string in OwnTone's expression lexer, so they can never appear
#: inside one. See the module docstring.
_EXPRESSION_BREAKERS = '"\n\r'

#: OwnTone answers most commands with ``204 No Content``; anything 2xx is success.
_OK = range(200, 300)

#: The HTTP Basic username OwnTone requires for its web API. Hard-coded in the daemon
#: (``src/httpd.c``), so it is hard-coded here too rather than pretending to be configurable.
ADMIN_USER = "admin"

#: Page sizes for the path lookup. An exact ``path is`` match can only return case-variants of one
#: file; the degraded forms can match an album or the whole library, and a truncated page there would
#: be indistinguishable from "not indexed".
_SEARCH_LIMIT_EXACT = 50
_SEARCH_LIMIT_WIDE = 2000


def _quote_query(value: Any, safe: str = "", encoding: str | None = None, errors: str | None = None) -> str:
    """Percent-encode a query value, leaving ``:``, ``,`` and ``/`` alone. See :meth:`request`."""
    return urllib.parse.quote(str(value), safe=":,/", encoding=encoding, errors=errors)


class OwnToneClient:
    """Minimal JSON client for one OwnTone instance.

    Deliberately not a general-purpose HTTP client: it knows the base URL, the timeout, the
    optional admin password, and how to turn a transport failure into
    :class:`~tunebox.errors.PlayerUnavailable` so callers never have to catch ``URLError``.
    """

    def __init__(
        self,
        base_url: str,
        *,
        password: str | None = None,
        timeout: float = 5.0,
        opener: Any | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._opener = opener or urllib.request.build_opener()
        self._auth: str | None = None
        if password:
            # OwnTone's web API uses HTTP Basic with the username **"admin"** — hard-coded in
            # ``httpd_request_is_authorized`` (src/httpd.c: ``httpd_basic_auth(hreq, "admin", …)``),
            # not configurable. An empty username is rejected with 401, which is how this was found:
            # the first hardware run sent ``:password`` and could not authenticate at all.
            # Sent pre-emptively because a 401 handshake per command is pure latency on a LAN.
            raw = base64.b64encode(f"{ADMIN_USER}:{password}".encode()).decode("ascii")
            self._auth = f"Basic {raw}"

    # ------------------------------------------------------------------ requests

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        """Call the API and return decoded JSON, or ``None`` for an empty body.

        ``204 No Content`` is OwnTone's normal answer to a command, so "no body" is a success and
        not a surprise.
        """
        url = f"{self.base_url}{path}"
        if params:
            # ``quote_via=quote`` because the default (``quote_plus``) turns a space into ``+``, and
            # OwnTone's expression parser reads ``+`` literally — an expression containing a space
            # would then silently match nothing.
            #
            # ``safe`` keeps ``:`` and ``,`` literal: they are legal in a query value (RFC 3986
            # sub-delims) and they are the daemon's own vocabulary — ``uris=library:track:3,…``.
            # Encoding them works, but it makes OwnTone's request log unreadable, which is exactly
            # where the first hardware failure of the queue-add had to be diagnosed.
            clean = {k: v for k, v in params.items() if v is not None}
            url = f"{url}?{urllib.parse.urlencode(clean, quote_via=_quote_query)}"

        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self._auth:
            headers["Authorization"] = self._auth

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                payload = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            with exc:  # HTTPError is itself a response object; not closing it leaks the socket
                detail = _short(exc.read())
            if exc.code in (401, 403):
                raise PlayerUnavailable(
                    f"player rejected our credentials ({exc.code}) at {url}. If OwnTone has an "
                    "admin_password and this host is not in its trusted_networks, set "
                    "TUNEBOX_PLAYER_PASSWORD"
                ) from exc
            raise PlayerError(f"{method} {path} failed: HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise PlayerUnavailable(
                f"player daemon is not answering at {self.base_url}: {exc.reason}. "
                "Is OwnTone running? (TUNEBOX_PLAYER_URL)"
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise PlayerUnavailable(
                f"player daemon at {self.base_url} did not answer within {self._timeout}s"
            ) from exc
        except OSError as exc:  # pragma: no cover - a dead socket mid-read
            raise PlayerUnavailable(f"player daemon at {self.base_url} failed: {exc}") from exc

        if status not in _OK:  # pragma: no cover - urllib raises for these
            raise PlayerError(f"{method} {path} returned HTTP {status}")
        if not payload:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PlayerError(f"{method} {path} returned non-JSON: {_short(payload)}") from exc

    def get_object(self, path: str, **params: Any) -> dict[str, Any]:
        """Like :meth:`get`, but guarantees a mapping.

        Every read in this adapter expects a JSON object and then subscripts it. Without this, a
        daemon answering ``[]`` or ``"ok"`` — or a proxy interposing something helpful — produces an
        ``AttributeError`` from deep inside a getter, which escapes the
        :class:`~tunebox.errors.TuneboxError` tree and therefore escapes every caller's error
        handling. An empty body still means "nothing", because that is what OwnTone's ``204`` is, and
        so does a literal ``null`` — the one non-object body with an unambiguous reading.
        """
        payload = self.get(path, **params)
        if payload is None:
            return {}
        if not isinstance(payload, dict):
            raise PlayerError(
                f"GET {path} returned {type(payload).__name__}, expected a JSON object: "
                f"{_short(json.dumps(payload))}"
            )
        return payload

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params)

    def put(self, path: str, *, body: dict[str, Any] | None = None, **params: Any) -> Any:
        return self.request("PUT", path, params=params, body=body)

    def post(self, path: str, *, body: dict[str, Any] | None = None, **params: Any) -> Any:
        return self.request("POST", path, params=params, body=body)


class OwnTonePlayer:
    """:class:`~tunebox.core.ports.Player` backed by OwnTone."""

    name = "owntone"

    def __init__(self, config: Config, *, client: OwnToneClient | None = None) -> None:
        self._config = config
        self._client = client or OwnToneClient(
            config.player_base_url,
            password=config.player_password,
            timeout=config.player_timeout,
        )

    @property
    def base_url(self) -> str:
        return self._client.base_url

    # -------------------------------------------------------------------- status

    def status(self) -> PlayerStatus:
        player = self._client.get_object("/api/player")
        outputs = tuple(self.outputs())
        return PlayerStatus(
            state=str(player.get("state") or "stop"),
            volume=_as_int(player.get("volume")),
            repeat=player.get("repeat"),
            shuffle=bool(player.get("shuffle")),
            consume=bool(player.get("consume")),
            item_id=_as_str(player.get("item_id")),
            item_length_ms=_as_int(player.get("item_length_ms")),
            item_progress_ms=_as_int(player.get("item_progress_ms")),
            now_playing=self._now_playing(_as_str(player.get("item_id"))),
            outputs=outputs,
        )

    def _now_playing(self, item_id: str | None) -> QueueItem | None:
        """The queue item OwnTone says is loaded.

        Looked up from the queue rather than from a separate endpoint so that "what is playing" and
        "what is in the queue" can never disagree — which is the fact the Phase 3 walk-away test
        checks, and a stale answer there would make a passing test meaningless.
        """
        if item_id in (None, "", "0"):
            return None
        for item in self.queue():
            if item.id == item_id:
                return item
        return None

    # ------------------------------------------------------------------- outputs

    def outputs(self) -> list[Output]:
        payload = self._client.get_object("/api/outputs")
        return [_output(raw) for raw in payload.get("outputs") or []]

    def find_output(self, *, output_id: str | None = None, name: str | None = None) -> Output:
        """Locate one output by id, or failing that by case-insensitive name.

        An id is preferred because it is stable; a name is what the config carries by default,
        since that is the thing a human can read off the Google Home app.
        """
        available = self.outputs()
        if output_id:
            for output in available:
                if output.id == output_id:
                    return output
            raise OutputNotFound(
                f"no output with id {output_id!r}; visible now: {_names(available)}"
            )
        wanted = (name or "").strip().casefold()
        if not wanted:
            raise OutputNotFound("no output configured: set TUNEBOX_OUTPUT or TUNEBOX_OUTPUT_ID")
        for output in available:
            if output.name.strip().casefold() == wanted:
                return output
        raise OutputNotFound(
            f"no output named {name!r}; visible now: {_names(available)}. "
            "A soundbar that is unplugged or off the network does not appear here"
        )

    def target_output(self) -> Output:
        """The configured speaker, as OwnTone currently sees it."""
        return self.find_output(output_id=self._config.output_id, name=self._config.output_name)

    def select_output(self, output_id: str, *, volume: int | None = None) -> None:
        """Enable one output without touching the others, retrying a speaker that is waking up.

        ``PUT /api/outputs/{id}`` rather than ``PUT /api/outputs/set``: the latter takes the full
        list of enabled outputs and *disables everything absent from it*, which would make
        selecting the soundbar silently switch off any other speaker — wrong now, and actively
        destructive if Phase 6's second speaker ever appears.

        **Why this retries.** A Cast device in standby is the ordinary state of a soundbar somebody
        is about to use, and it is not the same as an absent one: it keeps advertising over mDNS, so
        the daemon lists it, and then the connection to port 8009 fails with ``No route to host``
        and the enable comes back ``400``. Measured on this household's hardware (OwnTone 29.3,
        2026-09-30): three consecutive enables failed that way over twelve seconds, and an attempt
        fifty-four seconds later established ``TLS1.3`` and played, with nothing reconfigured in
        between. So the failure is transient by nature and a single attempt turns it into a dead
        button. The whole call is bounded — :attr:`Config.output_select_attempts` attempts with a
        doubling pause, about four seconds in the worst case — because the caller is a phone waiting
        on a tap, not a background job.

        **Why it re-reads the output list between attempts.** OwnTone answers *both* "I could not
        start that speaker" and "there is no such id" with the same ``400`` and the same generic
        HTML body (measured: id ``999999`` and id ``bogus`` are indistinguishable from a real device
        that will not come up), so the status code cannot tell them apart. Asking which one it is
        costs one loopback GET and turns the second case into an accurate
        :class:`~tunebox.errors.OutputNotFound` — a speaker really can leave between being listed
        and being selected, and retrying then would be pointless.

        The error this ends with carries **none of the daemon's answer**, deliberately: that answer
        is a full HTML error page, and a remote whose "speaker is asleep" message is a markup
        fragment in a toast on a phone is the failure this method exists to stop. The daemon's text
        is in the log and on ``__cause__``, where whoever is debugging can reach it.
        """
        body: dict[str, Any] = {"selected": True}
        if volume is not None:
            body["volume"] = _clamp_volume(volume)
        path = f"/api/outputs/{urllib.parse.quote(output_id, safe='')}"
        attempts = max(1, int(self._config.output_select_attempts))
        delay = _ENABLE_BACKOFF
        last: PlayerError | None = None

        for attempt in range(1, attempts + 1):
            try:
                self._client.put(path, body=body)
            except PlayerUnavailable:
                # The *daemon* is not answering. Retrying here would only delay the true answer,
                # and it is a different fix ("start OwnTone") from the one below.
                raise
            except PlayerError as exc:
                last = exc
                log.info(
                    "could not enable output %s (attempt %d/%d): %s",
                    output_id,
                    attempt,
                    attempts,
                    exc,
                )
                if attempt == attempts:
                    break
                # Raises OutputNotFound if it has gone, which ends this loop with the truth.
                self.find_output(output_id=output_id)
                time.sleep(delay)
                delay *= 2
                continue
            if attempt > 1:
                log.info("output %s came up on attempt %d of %d", output_id, attempt, attempts)
            return

        raise OutputUnreachable(
            f"the player has output {output_id} listed but could not bring it up after "
            f"{attempts} attempt(s). A Cast speaker in standby does exactly this: it is still "
            "advertised on the network while it stops answering, so try again in a moment. "
            'The reason is in the player\'s own log (look for "Could not connect").'
        ) from last

    def deselect_output(self, output_id: str) -> None:
        self._client.put(
            f"/api/outputs/{urllib.parse.quote(output_id, safe='')}", body={"selected": False}
        )

    def set_volume(self, volume: int, *, output_id: str | None = None) -> None:
        self._client.put("/api/player/volume", volume=_clamp_volume(volume), output_id=output_id)

    def change_volume(self, step: int, *, output_id: str | None = None) -> None:
        """Relative volume, so the CLI can offer ``+5``/``-5`` without reading the level first."""
        self._client.put("/api/player/volume", step=max(-100, min(100, int(step))), output_id=output_id)

    # ----------------------------------------------------------------- transport

    def play(self) -> None:
        self._client.put("/api/player/play")

    def pause(self) -> None:
        self._client.put("/api/player/pause")

    def stop(self) -> None:
        self._client.put("/api/player/stop")

    def toggle(self) -> None:
        self._client.put("/api/player/toggle")

    def next_track(self) -> None:
        self._client.put("/api/player/next")

    def previous_track(self) -> None:
        self._client.put("/api/player/previous")

    def seek(self, position_ms: int) -> None:
        self._client.put("/api/player/seek", position_ms=max(0, int(position_ms)))

    # --------------------------------------------------------------------- modes

    def set_repeat(self, mode: str) -> None:
        """``PUT /api/player/repeat?state=off|all|single``.

        Validated here, and that is the whole point of the method rather than a detail of it:
        ``state=bogus`` is answered ``204`` and silently read as ``off`` (measured against the
        running daemon, 2026-09-27). A typo in a caller would therefore look like success *and*
        change the mode — so the check has to be on this side of the wire.
        """
        self._client.put("/api/player/repeat", state=_repeat_state(mode))

    def set_shuffle(self, enabled: bool) -> None:
        """``PUT /api/player/shuffle?state=true|false``.

        ``bool`` strictly, for the same reason: the daemon takes anything it cannot parse as
        ``false``, so ``set_shuffle("false")`` — a string, truthy in Python — would have turned
        shuffle *off* through a code path that looks like it turns it on.
        """
        self._client.put("/api/player/shuffle", state="true" if _bool_state(enabled) else "false")

    # --------------------------------------------------------------------- queue

    def queue(self) -> list[QueueItem]:
        payload = self._client.get_object("/api/queue")
        items = payload.get("items") or []
        return [_queue_item(raw) for raw in items]

    def clear_queue(self) -> None:
        self._client.put("/api/queue/clear")

    def play_item(self, item_id: str) -> None:
        """``PUT /api/player/play?item_id=N`` — start at one item already in the queue.

        **Measured against the running daemon, 2026-09-28**, because none of it was obvious from the
        route table alone (``^/api/player/play$`` takes ``item_id`` *and* ``position``; this uses the
        id, see :meth:`~tunebox.core.ports.Player.play_item` for why):

        ```
        PUT /api/player/play?item_id=<in the queue>  -> 204, and GET /api/player reports that item_id
        PUT /api/player/play?item_id=99999           -> 400 (HTML body)
        PUT /api/player/play?item_id=bogus           -> 400 (HTML body)
        PUT /api/player/play?item_id=                -> 400 (HTML body)
        ```

        Two consequences worth writing down.

        **The id is validated here**, unlike ``repeat`` and ``shuffle`` where the daemon's ``204`` to
        nonsense forced it — this route does refuse. But it refuses in HTML, so an unchecked bad id
        would reach a caller as a ``502`` carrying a paragraph of markup; and OwnTone's ids are
        unsigned integers, which is a fact about *this* daemon and therefore this adapter's business.

        **The jump settles asynchronously.** For ~2.5 s afterwards ``GET /api/player`` answers
        ``state=pause, item_progress_ms=0`` while the Cast output starts, then flips to ``play`` on
        its own with no further command (sampled every 0.5 s on the real soundbar). So a caller that
        reads the state immediately gets "paused", and that is the daemon telling the truth rather
        than a failed command — the same lag ``playback=start`` on a queue add already has.
        """
        self._client.put("/api/player/play", item_id=_item_id(item_id))

    def enqueue_paths(
        self, paths: Sequence[Path], *, clear: bool = False, start: bool = False
    ) -> list[QueueItem]:
        """Add library files to the queue in the order given.

        Resolves every path to a track id *first*, so a typo in the third path does not leave the
        queue half-replaced with playback already running.
        """
        if not paths:
            if clear:
                self.clear_queue()
            return []

        uris = []
        for path in paths:
            track = self.resolve_track(path)
            track_id = _as_str(track.get("id"))
            if not track_id:
                # Would otherwise be a bare KeyError escaping the exception tree.
                raise PlayerError(f"player returned a track record with no id for {path}")
            uris.append(f"library:track:{track_id}")
        payload = self._client.post(
            "/api/queue/items/add",
            uris=",".join(uris),
            clear="true" if clear else None,
            playback="start" if start else None,
        )
        items = (payload or {}).get("items") if isinstance(payload, dict) else None
        return [_queue_item(raw) for raw in items or [] if isinstance(raw, dict)]

    # ------------------------------------------------------- library / the index

    def resolve_track(self, path: Path, *, timeout: float = 0.0) -> dict[str, Any]:
        """Find OwnTone's track record for the file at ``path``.

        ``timeout`` > 0 polls, because a rescan is asynchronous: a track ingested a second ago is
        on disk but not yet in OwnTone's database, and "play what I just added" is the single most
        likely thing anyone asks for.
        """
        given = Path(path).expanduser()
        # Both spellings are compared, because it is not knowable from here which one OwnTone
        # recorded: it indexes the path its own ``directories`` setting gave it. A music root on an
        # external SSD reached through a symlink — which is PLAN.md 3.1's actual deployment — makes
        # the resolved and unresolved forms differ, and matching only the resolved one would report
        # a perfectly indexed file as missing.
        candidates = [str(given)]
        try:
            resolved = str(given.resolve())
        except OSError:  # pragma: no cover - resolve() on a live path
            resolved = str(given)
        if resolved not in candidates:
            candidates.append(resolved)

        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            for candidate in candidates:
                found = self._search_track(candidate)
                if found is not None:
                    return found
            if time.monotonic() >= deadline:
                raise PlayerError(
                    f"{given} is not in the player's library. It was placed under the music "
                    "root but the daemon has not indexed it — check that OwnTone's `directories` "
                    "covers TUNEBOX_MUSIC_ROOT, then `tunebox player rescan`"
                )
            time.sleep(0.5)

    def _search_track(self, absolute: str) -> dict[str, Any] | None:
        expression = _path_expression(absolute)
        payload = self._client.get_object(
            "/api/search",
            type="tracks",
            expression=expression,
            # An exact ``path is`` match can only return the handful of case-variants of one file, so
            # a small limit is fine. The degraded expressions are a different matter: ``ends with``
            # can match a whole album and the ``media_kind is music`` fallback matches the entire
            # library, and a truncated page would make this report a file it simply had not looked
            # at yet as "not indexed". So the limit follows the precision of the query.
            limit=_SEARCH_LIMIT_EXACT if expression.startswith("path is ") else _SEARCH_LIMIT_WIDE,
        )
        paging = payload.get("tracks")
        if not isinstance(paging, dict):
            return None
        tracks = paging.get("items")
        if not isinstance(tracks, list):
            return None
        for track in tracks:
            if not isinstance(track, dict):
                continue
            # Byte-exact, because OwnTone's `is`/`ends with` are case-insensitive and this library
            # can contain "好久不見 (Live).m4a" next to nothing else but could, in principle, contain
            # two names differing only in case on a case-sensitive filesystem.
            if track.get("path") == absolute:
                return track
        total = paging.get("total")
        if isinstance(total, int) and total > len(tracks):
            # Say so rather than reporting "not indexed", which would send the reader off to check
            # OwnTone's `directories` for a problem that is not there.
            raise PlayerError(
                f"{absolute} was not found in the first {len(tracks)} of {total} tracks matching "
                f"{expression!r}. The library is too large for this lookup to be conclusive; "
                "narrow TUNEBOX_MUSIC_ROOT or avoid a quote in its path"
            )
        return None

    def rescan(self) -> None:
        """``PUT /api/update`` — pick up files that appeared under the music root.

        Cheap: OwnTone compares mtimes and does nothing when nothing changed, so calling it after
        every ingest (PLAN.md Phase 3) costs one loopback request.

        Asynchronous: it returns as soon as the scan is *queued*. Anything that needs a track id
        afterwards must call :meth:`wait_for_scan` first — see that method for why.
        """
        self._client.put("/api/update")

    def wait_for_scan(self, timeout: float = 30.0, poll: float = 0.25) -> bool:
        """Block until no library scan is in progress. Returns whether it settled in time.

        This exists because of a race that bit on real hardware: a scan **deletes and re-inserts**
        rows, so a track id read while ``updating`` is true can be rejected as ``Invalid uri`` by the
        time it reaches the queue. OwnTone answers ``500`` to that, which reads like a bug in the
        request rather than a lost race.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                updating = bool(self.library_info().get("updating"))
            except PlayerError:
                return False
            if not updating:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll)

    def library_info(self) -> dict[str, Any]:
        return self._client.get_object("/api/library")

    def ping(self) -> dict[str, Any]:
        """Cheapest "is it there" call, used by ``tunebox doctor``."""
        return self._client.get_object("/api/config")


# ------------------------------------------------------------------- conversions


def _output(raw: dict[str, Any]) -> Output:
    return Output(
        id=_as_str(raw.get("id")) or "",
        name=str(raw.get("name") or ""),
        type=str(raw.get("type") or ""),
        selected=bool(raw.get("selected")),
        volume=_as_int(raw.get("volume")),
        needs_auth_key=bool(raw.get("needs_auth_key")),
    )


def _queue_item(raw: dict[str, Any]) -> QueueItem:
    return QueueItem(
        id=_as_str(raw.get("id")) or "",
        position=_as_int(raw.get("position")) or 0,
        title=raw.get("title"),
        artist=raw.get("artist"),
        album=raw.get("album"),
        length_ms=_as_int(raw.get("length_ms")),
        path=raw.get("path"),
        track_id=_as_str(raw.get("track_id")),
    )


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    """OwnTone documents ids as strings but sends some of them as integers. Normalise once."""
    if value is None:
        return None
    return str(value)


def _clamp_volume(volume: int) -> int:
    return max(0, min(100, int(volume)))


def _repeat_state(mode: Any) -> str:
    """``mode`` as OwnTone spells it, or a :class:`ValueError`.

    ``ValueError`` rather than a :class:`~tunebox.errors.TuneboxError`: by the time anything reaches
    here the HTTP edge has already rejected an unknown mode with a ``422``
    (:class:`tunebox.api.schemas.RepeatRequest`), so the only way to trip this is a call site with a
    bad literal in it — a bug in our code, which is exactly the thing PLAN.md 3.4's exception tree is
    there to *not* look like a handled failure.
    """
    if not isinstance(mode, str) or mode not in REPEAT_MODES:
        raise ValueError(f"repeat mode must be one of {', '.join(REPEAT_MODES)}, not {mode!r}")
    return mode


def _bool_state(enabled: Any) -> bool:
    """``enabled`` if it is genuinely a ``bool``. See :meth:`OwnTonePlayer.set_shuffle`."""
    if not isinstance(enabled, bool):
        raise ValueError(f"shuffle takes a bool, not {type(enabled).__name__} ({enabled!r})")
    return enabled


def _item_id(item_id: Any) -> str:
    """A queue item id in the only spelling OwnTone has, or a :class:`ValueError`.

    Its ids are unsigned integers — ``_as_str`` normalises them on the way in because the daemon
    sends some of them as JSON numbers, so on the way out the digits are all that can be legal.

    ``ValueError`` for the same reason as :func:`_repeat_state`: the HTTP edge has already answered
    ``404`` for an id that is not in the queue (:func:`tunebox.api.remote.play_queue_item`), so
    nothing reaching here with a bad id came from a client — it came from a bug in our own code.

    ``isascii()`` as well as ``isdigit()``: the latter is true of "٣" and of "²", and Python's own
    ``int()`` accepts the first of those, so a digit-shaped string that no C ``strtol`` will read is
    exactly the input this has to refuse rather than forward.
    """
    if isinstance(item_id, bool) or not isinstance(item_id, (str, int)):
        raise ValueError(f"queue item id must be a positive integer, not {item_id!r}")
    text = str(item_id).strip()
    if not text or not (text.isascii() and text.isdigit()):
        raise ValueError(f"queue item id must be a positive integer, not {item_id!r}")
    return text


def _names(outputs: Sequence[Output]) -> str:
    return ", ".join(f"{o.name!r} ({o.type})" for o in outputs) or "(none)"


def _path_expression(absolute: str | Path) -> str:
    """A smart-playlist expression that includes the file at ``absolute``.

    Not a precise match, and not trying to be: it narrows the search server-side and the caller
    compares paths byte-for-byte. See the module docstring for why a ``"`` in the path cannot be
    escaped and is handled by trimming instead.
    """
    text = str(absolute)
    tail = _quote_free_tail(text)
    if tail == text:
        return f'path is "{text}"'
    if tail:
        return f'path ends with "{tail}"'
    # Pathological: every trailing character is a quote or newline. Fall back to the widest
    # possible filter and let the exact match in Python do the work.
    return "media_kind is music"


def _quote_free_tail(text: str) -> str:
    """The longest suffix of ``text`` containing no character that ends an expression string."""
    for index in range(len(text) - 1, -1, -1):
        if text[index] in _EXPRESSION_BREAKERS:
            return text[index + 1 :]
    return text


def _short(payload: bytes | str, limit: int = 200) -> str:
    text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")
