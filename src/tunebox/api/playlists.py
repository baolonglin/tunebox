"""``/playlists`` — the three verbs of v1, plus reading and deleting.

A playlist is an M3U file in the music root (:mod:`tunebox.playlists`), which decides the shape of
this router more than any taste did:

* **The name is the identity**, because it is the filename. So it goes in the path, and there is no
  id to hand out. That is the opposite of the choice ``DELETE /library`` made — a body, because a
  library path contains slashes and URL-encoding one twice is how a delete names the wrong file — and
  the reason it is safe here is that a playlist name is *one sanitised path component*
  (:func:`tunebox.playlists.normalise_name`), so it cannot contain a slash in the first place. CJK in
  a name is ordinary percent-encoding, which Starlette decodes for us.

* **Nothing here knows which daemon is playing.** Playback is
  :meth:`~tunebox.core.ports.Player.enqueue_paths` with absolute paths this module resolved itself —
  not OwnTone's ``uris=library:playlist:N``, which would put the daemon's own playlist ids through the
  ``Player`` port and break the swap PLAN.md 3.4 keeps it swappable for.

* **The one route that writes files to the library goes through the library's own resolver**, every
  entry, every time. ``POST /playlists`` with a path outside the music root is a ``400`` from
  :func:`tunebox.library.resolve_track_path`, the same answer ``POST /player/queue`` gives, rather
  than a new path handler in a new module — which is where a traversal bug would be worth having.

Plain ``def`` handlers, like the rest of the API: these read and write small files and call a loopback
HTTP API, so Starlette's threadpool is where they belong.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status

from .. import playlists as playlist_files
from ..config import Config
from ..core.ports import Player
from ..errors import LibraryError, PlayerUnavailable, PlaylistError, PlaylistNotFound
from ..library import resolve_track_path
from ..player.hooks import rescan_library
from .remote import player_view
from .schemas import (
    LibraryTrackView,
    PlayerView,
    PlaylistCreateRequest,
    PlaylistDeletedView,
    PlaylistEntryView,
    PlaylistMoveRequest,
    PlaylistPlayRequest,
    PlaylistRemoveRequest,
    PlaylistRenameRequest,
    PlaylistTracksRequest,
    PlaylistView,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/playlists", tags=["playlists"])


# --------------------------------------------------------------------- plumbing


def _config(request: Request) -> Config:
    return request.app.state.config


def _player(request: Request) -> Player:
    player = getattr(request.app.state, "player", None)
    if player is None:  # pragma: no cover - create_app always installs one
        raise PlayerUnavailable("no player is configured")
    return player


def _view(playlist, *, skipped: list[str] | None = None) -> PlaylistView:
    data = playlist.as_dict()
    return PlaylistView(
        name=data["name"],
        path=data["path"],
        count=data["count"],
        missing_count=data["missing_count"],
        modified=data["modified"],
        entries=[
            PlaylistEntryView(
                position=entry["position"],
                entry=entry["entry"],
                missing=entry["missing"],
                track=LibraryTrackView(**entry["track"]) if entry["track"] else None,
            )
            for entry in data["entries"]
        ],
        skipped=skipped or [],
    )


def _bad_request(exc: LibraryError) -> HTTPException:
    """A client naming a file it should not, or one that is not there: its mistake, not ours.

    Explicit because :class:`~tunebox.errors.LibraryError` has no handler of its own — it would fall
    through to the 500 every other :class:`~tunebox.errors.TuneboxError` gets, and a path that does
    not exist is not a bug in the server.
    """
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _rescan(request: Request) -> list[str]:
    """Tell the daemon the playlist file changed. Cannot fail the operation that called it.

    The same rule as every other rescan in the project (:mod:`tunebox.player.hooks`): the file is
    written, and a daemon that is not answering costs only that OwnTone's own playlist view is stale
    until it next scans. tunebox's own playback reads the file directly, so nothing the remote does
    depends on this having worked.
    """
    player = getattr(request.app.state, "player", None)
    if player is None:  # pragma: no cover - create_app always installs one
        return []
    warning = rescan_library(player)
    return [warning] if warning else []


# ---------------------------------------------------------------------- routes


@router.get("", response_model=list[PlaylistView], summary="Every playlist, by name")
def list_playlists(request: Request) -> list[PlaylistView]:
    """Names and counts, without the tracks — see :func:`tunebox.playlists.list_playlists`."""
    config = _config(request)
    found = playlist_files.list_playlists(
        config.music_root, directory=config.playlists_dirname
    )
    return [_view(playlist) for playlist in found]


@router.post(
    "",
    response_model=PlaylistView,
    status_code=status.HTTP_201_CREATED,
    summary="Create a playlist, from library paths or from the queue",
)
def create_playlist(payload: PlaylistCreateRequest, request: Request) -> PlaylistView:
    """``{"name": ..., "paths": [...]}`` or ``{"name": ..., "from_queue": true}``.

    ``from_queue`` is the one-tap "save what is playing", and it is why this feature needs no OwnTone
    config change: ``POST /api/queue/save`` would do the same thing and is exactly the route the
    daemon refuses without ``allow_modifying_stored_playlists``, so tunebox reads the queue through
    the ``Player`` port and writes the file itself.

    Items in the queue that are not library tracks are skipped and named in ``skipped`` rather than
    failing the save; explicit ``paths`` are all-or-nothing. :class:`PlaylistCreateRequest` has the
    reasoning for the asymmetry.
    """
    config = _config(request)
    skipped: list[str] = []
    if payload.from_queue:
        entries, skipped = _queue_entries(request)
        if not entries:
            # 409 rather than an empty file: "save the queue" with nothing in the queue is a mistake,
            # and an empty playlist is something you ask for by name (below), not something you get by
            # accident. The message says how many items were there but unusable.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "the queue holds no library tracks to save"
                    + (f" ({len(skipped)} item(s) are not in the library)" if skipped else "")
                ),
            )
    else:
        # ``None`` is "create it empty" — the entry point for assembling a playlist one track at a
        # time from the Library panel, which is the half of creation that was missing at first.
        entries = list(payload.paths or ())
        if payload.skip_missing and entries:
            # The caller said these are a queue, not a list it has just read: *This device*'s queue
            # arrives this way because it lives in the phone's own storage and the server has never
            # seen it. A queue outlives the library, so one track deleted since it was built must
            # cost that one row and not the whole save.
            kept = [(entry, _resolved_or_none(config.music_root, entry)) for entry in entries]
            entries = [resolved for _, resolved in kept if resolved is not None]
            skipped = [entry for entry, resolved in kept if resolved is None]
            if not entries:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "none of those paths are library tracks any more "
                        f"({len(skipped)} item(s) have gone)"
                    ),
                )

    try:
        playlist = playlist_files.write_playlist(
            config.music_root,
            payload.name,
            entries,
            directory=config.playlists_dirname,
            overwrite=payload.replace,
        )
    except LibraryError as exc:
        raise _bad_request(exc) from exc
    _rescan(request)
    log.info("created playlist %s with %d track(s)", playlist.name, playlist.count)
    return _view(playlist, skipped=skipped)


def _resolved_or_none(root, path: str) -> str | None:
    """The resolved path, or ``None`` if it is not a library track any more.

    Resolution is the only honest existence check here: it is the same function the write path uses,
    so "this will be skipped" and "this would have been refused" cannot disagree.
    """
    try:
        return str(resolve_track_path(root, path))
    except LibraryError:
        return None


def _queue_entries(request: Request) -> tuple[list[str], list[str]]:
    """The queue's library tracks, in order, and the titles of whatever else was in it.

    Resolved here rather than inside :mod:`tunebox.playlists` because *skipping* is a decision about
    this request: the module's job is to refuse a path it cannot resolve, and the one caller that
    wants a filter is this one.
    """
    config = _config(request)
    entries: list[str] = []
    skipped: list[str] = []
    for item in _player(request).queue():
        if not item.path:
            skipped.append(item.title or f"queue item {item.position + 1}")
            continue
        resolved = _resolved_or_none(config.music_root, item.path)
        if resolved is None:
            skipped.append(item.title or item.path)
            continue
        entries.append(resolved)
    return entries, skipped


@router.get("/{name}", response_model=PlaylistView, summary="One playlist and its tracks")
def get_playlist(name: str, request: Request) -> PlaylistView:
    config = _config(request)
    playlist = playlist_files.read_playlist(
        config.music_root, name, directory=config.playlists_dirname
    )
    return _view(playlist)


@router.post(
    "/{name}/tracks",
    response_model=PlaylistView,
    summary="Append tracks to a playlist",
)
def add_tracks(name: str, payload: PlaylistTracksRequest, request: Request) -> PlaylistView:
    """Append. The counterpart to ``DELETE`` on the same path and the move route below it.

    Together those three are what makes a playlist editable from a phone — which it was not when this
    router first shipped, and the omission mattered more than it looked: without them the only way to
    fix a playlist was to rebuild the queue and save over it, so a list of forty tracks with one wrong
    song in it was a list to recreate from scratch.
    """
    config = _config(request)
    try:
        playlist = playlist_files.append_tracks(
            config.music_root, name, list(payload.paths), directory=config.playlists_dirname
        )
    except LibraryError as exc:
        raise _bad_request(exc) from exc
    _rescan(request)
    return _view(playlist)


@router.delete(
    "/{name}/tracks",
    response_model=PlaylistView,
    summary="Remove entries from a playlist (no audio is touched)",
)
def remove_tracks(name: str, payload: PlaylistRemoveRequest, request: Request) -> PlaylistView:
    """``{"positions": [1]}`` from the remote, or ``{"paths": [...]}`` from a terminal.

    Nothing like the ceremony of ``DELETE /library``, because this destroys nothing: the tracks are
    all still on disk and the only thing removed is a line of text. So there is no confirmation strip
    behind a ``⋯`` for it on the phone — the ``✕`` on the row is the whole gesture.

    A ``position`` that is not in the playlist is a ``409``, not a silent no-op. The remote read the
    list a couple of seconds before the tap, so a stale index means somebody else edited the playlist
    in between, and deleting whatever happens to sit at that index now is the one outcome nobody asked
    for. :class:`~tunebox.api.schemas.PlaylistRemoveRequest` has the rest of the reasoning.
    """
    config = _config(request)
    try:
        playlist = playlist_files.remove_entries(
            config.music_root,
            name,
            positions=payload.positions,
            paths=payload.paths,
            directory=config.playlists_dirname,
        )
    except LibraryError as exc:
        raise _bad_request(exc) from exc
    except PlaylistNotFound:
        # Before PlaylistError below, because it *is* one: a missing playlist is a 404 from the app's
        # own handler, and catching it here answered 409 — "that playlist is in the way" about a
        # playlist that does not exist. Found by a test, which is why the ordering is now deliberate.
        raise
    except PlaylistError as exc:
        # A stale index or a track the playlist does not hold: the request was well formed and the
        # playlist's own state is what stands in its way, which is 409 rather than 400.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    _rescan(request)
    return _view(playlist)


@router.put(
    "/{name}/tracks/{position}",
    response_model=PlaylistView,
    summary="Move one entry to another position",
)
def move_track(
    name: str, position: int, payload: PlaylistMoveRequest, request: Request
) -> PlaylistView:
    """The reorder primitive — what the remote's ↑ and ↓ send, one tap at a time.

    One entry rather than a whole order, and zero-based like the player's queue positions. See
    :class:`~tunebox.api.schemas.PlaylistMoveRequest` for why a move and not a permutation, and
    :class:`tunebox.playlists._Block` for what actually moves: the entry *and* its ``#EXTINF``, since
    an entry's metadata left behind would describe the track that took its place.
    """
    config = _config(request)
    try:
        playlist = playlist_files.move_entry(
            config.music_root,
            name,
            position,
            payload.to,
            directory=config.playlists_dirname,
        )
    except LibraryError as exc:
        raise _bad_request(exc) from exc
    except PlaylistNotFound:
        raise  # 404, not the 409 below — see remove_tracks
    except PlaylistError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    _rescan(request)
    return _view(playlist)


@router.patch("/{name}", response_model=PlaylistView, summary="Rename a playlist")
def rename_playlist(name: str, payload: PlaylistRenameRequest, request: Request) -> PlaylistView:
    """``PATCH`` because it changes one property of the playlist and keeps its contents.

    The new name is sanitised — it is naming a file — so the answer's ``name`` is the one to use in the
    next URL, exactly as with ``POST /playlists``. An existing playlist of that name is a ``409``
    rather than an overwrite: a rename that silently ate another list would be the worst kind of
    convenience.
    """
    config = _config(request)
    playlist = playlist_files.rename_playlist(
        config.music_root, name, payload.name, directory=config.playlists_dirname
    )
    _rescan(request)
    return _view(playlist)


@router.delete("/{name}", response_model=PlaylistDeletedView, summary="Delete a playlist")
def delete_playlist(name: str, request: Request) -> PlaylistDeletedView:
    """Removes the ``.m3u`` file and **no audio whatsoever**.

    Which is why it needs none of the ceremony ``DELETE /library`` has — no all-or-nothing resolve, no
    ``409`` for the track that is playing, and the remote asks once rather than naming what it is
    about to destroy. A playlist is a list; the tracks it named are all still there, and rebuilding it
    from the queue is a few taps.
    """
    config = _config(request)
    deleted = playlist_files.delete_playlist(
        config.music_root, name, directory=config.playlists_dirname
    )
    return PlaylistDeletedView(deleted=deleted, warnings=_rescan(request))


@router.put(
    "/{name}/play",
    response_model=PlayerView,
    summary="Play a playlist — replace the queue and start, or append it",
)
def play_playlist(name: str, payload: PlaylistPlayRequest, request: Request) -> PlayerView:
    """The verb this feature exists for: one request between "a playlist" and "music".

    Everything slow about it is already solved elsewhere — the entries resolve through the library's
    resolver, and ``enqueue_paths`` resolves every track id before it sends anything, so a playlist
    that has lost a file cannot leave the queue half-replaced with playback already running.

    A playlist whose entries all resolve to nothing is a ``409``: the request is fine and the
    playlist is real, but there is nothing in it to play. Reporting that as success would give the
    remote a tap that empties the queue and calls it playing.
    """
    config = _config(request)
    player = _player(request)
    playlist = playlist_files.read_playlist(
        config.music_root, name, directory=config.playlists_dirname
    )
    try:
        paths = playlist_files.resolved_paths(config.music_root, playlist)
    except LibraryError as exc:
        # A file that vanished between the read and this line. Rare, and its own answer rather than
        # a 500: the client's move is to re-read the playlist, which will now report it as missing.
        raise _bad_request(exc) from exc
    if not paths:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"playlist {playlist.name!r} has no playable tracks"
                + (f" ({len(playlist.missing)} entry(ies) name files that are not there)"
                   if playlist.missing else "")
            ),
        )
    player.enqueue_paths(paths, clear=payload.mode == "replace", start=payload.start)
    log.info(
        "playing playlist %s (%d track(s), %s%s)",
        playlist.name,
        len(paths),
        payload.mode,
        ", starting" if payload.start else "",
    )
    return player_view(player, config)
