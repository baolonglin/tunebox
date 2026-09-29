"""The playback half of Phase 4: ``/player/*``, for the web remote.

PLAN.md Phase 4 asks for "playback controls [that] proxy to OwnTone's JSON API". The word *proxy*
is doing a lot of work there, and the shape this module does **not** have is the one it suggests:
there is no pass-through route, no ``/player/{path:path}`` forwarding whatever arrives to port
3689. Two reasons, and the first is a security requirement rather than taste.

* **PLAN.md 3.2:** *"The player daemon has its own auth and web UI — keep it enabled too, and do
  not expose its API unauthenticated just because our UI proxies to it."* A generic forwarder is
  exactly that: tunebox holds OwnTone's credentials and talks to it from a trusted network, so a
  forwarder would hand every caller of tunebox the daemon's full API — including
  ``/api/library/…`` writes and ``/api/outputs/set``, which can switch *off* speakers this
  household does not own (see :meth:`~tunebox.player.owntone.OwnTonePlayer.select_output`). What a
  remote may do is enumerated here instead.
* **The port must stay swappable.** Everything below goes through
  :class:`~tunebox.core.ports.Player`, so the fallback-A player PLAN.md 3.1 keeps in reserve
  serves this router unchanged. A route that spoke OwnTone's URL scheme would be a second place
  that knows which daemon is playing, and the first one to break on the swap.

Every handler is a plain ``def``: each one blocks on a loopback HTTP call, so Starlette's
threadpool is the right place for it — the same reasoning as :mod:`tunebox.api.routes`.

One deliberate difference from ``GET /healthz``: these routes **do** fail when the daemon is down.
``/healthz`` answers ``200`` with ``"reachable": false``, because a monitor must not report an
outage when a soundbar is unplugged. Here the player *is* the subject, so a dead daemon is a
``503`` (from :class:`~tunebox.errors.PlayerUnavailable`) and the remote can say so.

(The module is ``remote`` rather than ``player`` so that there is exactly one
``tunebox.player`` — the adapter package — and no import in this file is ambiguous about which
half of the system it means.)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status

from ..config import Config
from ..core.models import Output, PlayerStatus
from ..core.ports import Player
from ..errors import LibraryError, PlayerUnavailable
from ..library import resolve_track_path
from .schemas import (
    OutputView,
    PlayerView,
    QueueAddedView,
    QueueItemView,
    QueueRequest,
    RepeatRequest,
    SeekRequest,
    ShuffleRequest,
    VolumeRequest,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/player", tags=["player"])


# --------------------------------------------------------------------- plumbing


def _config(request: Request) -> Config:
    return request.app.state.config


def _player(request: Request) -> Player:
    player = getattr(request.app.state, "player", None)
    if player is None:  # pragma: no cover - create_app always installs one
        raise PlayerUnavailable("no player is configured")
    return player


def configured_output(outputs: tuple[Output, ...], config: Config) -> Output | None:
    """The speaker the config names, as the daemon currently sees it.

    Id *or* name, never both — the same precedence as
    :meth:`~tunebox.player.owntone.OwnTonePlayer.find_output`. Matching on either would let a
    speaker somebody renamed in the Google Home app collide with a configured id and report the
    wrong device as the selected one.
    """
    if config.output_id:
        return next((output for output in outputs if output.id == config.output_id), None)
    wanted = config.output_name.strip().casefold()
    if not wanted:
        return None
    return next(
        (output for output in outputs if output.name.strip().casefold() == wanted), None
    )


def _view(player: Player, config: Config, status_: PlayerStatus | None = None) -> PlayerView:
    state = status_ if status_ is not None else player.status()
    target = configured_output(state.outputs, config)
    return PlayerView(
        url=getattr(player, "base_url", config.player_base_url),
        state=state.state,
        volume=state.volume,
        repeat=state.repeat,
        shuffle=state.shuffle,
        consume=state.consume,
        item_id=state.item_id,
        item_length_ms=state.item_length_ms,
        item_progress_ms=state.item_progress_ms,
        now_playing=QueueItemView(**state.now_playing.as_dict()) if state.now_playing else None,
        outputs=[OutputView(**output.as_dict()) for output in state.outputs],
        queue=[QueueItemView(**item.as_dict()) for item in player.queue()],
        output=OutputView(**target.as_dict()) if target else None,
        output_selected=bool(target and target.selected),
    )


# ---------------------------------------------------------------------- routes


@router.get("", response_model=PlayerView, summary="Transport state, queue and outputs")
def get_player(request: Request) -> PlayerView:
    return _view(_player(request), _config(request))


@router.put("/play", response_model=PlayerView, summary="Start or resume")
def play(request: Request) -> PlayerView:
    player = _player(request)
    player.play()
    return _view(player, _config(request))


@router.put("/pause", response_model=PlayerView, summary="Pause, keeping the track loaded")
def pause(request: Request) -> PlayerView:
    player = _player(request)
    player.pause()
    return _view(player, _config(request))


@router.put("/toggle", response_model=PlayerView, summary="Pause if playing, play if not")
def toggle(request: Request) -> PlayerView:
    """One button on the remote, one round trip.

    Doing this client-side — read the state, decide, send play or pause — would be wrong on a
    remote that is stateless by design: between the read and the write, somebody else's phone (or
    the soundbar's own remote) can have changed it, and the button would then do the opposite of
    what its label said. ``toggle`` is not in the :class:`~tunebox.core.ports.Player` protocol, so
    it degrades to read-then-write for a player that lacks it.
    """
    player = _player(request)
    action = getattr(player, "toggle", None)
    if action is not None:
        action()
    else:  # pragma: no cover - OwnTone and the fake both have toggle()
        (player.pause if player.status().playing else player.play)()
    return _view(player, _config(request))


@router.put("/stop", response_model=PlayerView, summary="Stop and unload the track")
def stop(request: Request) -> PlayerView:
    player = _player(request)
    player.stop()
    return _view(player, _config(request))


@router.put("/next", response_model=PlayerView, summary="Skip to the next queue item")
def next_track(request: Request) -> PlayerView:
    player = _player(request)
    player.next_track()
    return _view(player, _config(request))


@router.put("/previous", response_model=PlayerView, summary="Skip to the previous queue item")
def previous_track(request: Request) -> PlayerView:
    player = _player(request)
    player.previous_track()
    return _view(player, _config(request))


@router.put("/volume", response_model=PlayerView, summary="Set volume, or change it by a step")
def set_volume(payload: VolumeRequest, request: Request) -> PlayerView:
    player = _player(request)
    if payload.step is not None:
        change = getattr(player, "change_volume", None)
        if change is not None:
            change(payload.step)
        else:  # pragma: no cover - OwnTone and the fake both have change_volume()
            current = player.status().volume or 0
            player.set_volume(max(0, min(100, current + payload.step)))
    else:
        assert payload.volume is not None  # guaranteed by VolumeRequest
        player.set_volume(payload.volume)
    return _view(player, _config(request))


@router.put("/seek", response_model=PlayerView, summary="Jump to a position in the current track")
def seek(payload: SeekRequest, request: Request) -> PlayerView:
    player = _player(request)
    action = getattr(player, "seek", None)
    if action is None:  # pragma: no cover - not in the Player protocol, so not guaranteed
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="this player cannot seek",
        )
    action(payload.position_ms)
    return _view(player, _config(request))


@router.put(
    "/repeat",
    response_model=PlayerView,
    summary="Repeat nothing, the whole queue, or one song",
)
def set_repeat(payload: RepeatRequest, request: Request) -> PlayerView:
    """``{"mode": "off" | "all" | "single"}`` — the mode wanted, not a cycle.

    A tri-state *button* is what the UI draws, but a cycle is not what it sends: this session is
    shared (PLAN.md 2), and "advance the mode" from a phone holding a two-second-old poll would
    depend on what anything else did in the meantime. An absolute mode is idempotent, so two phones
    pressing it converge rather than chase each other.

    ``consume`` has no route on purpose. It is reported by ``GET /player`` because it is part of the
    daemon's state, but a queue that eats itself is not a mode anybody asked for, and a control is
    easier to add later than to take away.
    """
    player = _player(request)
    player.set_repeat(payload.mode)
    return _view(player, _config(request))


@router.put("/shuffle", response_model=PlayerView, summary="Shuffle the queue, or stop shuffling")
def set_shuffle(payload: ShuffleRequest, request: Request) -> PlayerView:
    """``{"enabled": true | false}``, explicitly — there is no toggle here.

    Unlike ``/player/toggle``, which is one round trip precisely *because* play/pause is the button
    whose label would otherwise lie, shuffle is a setting: sending the state wanted costs nothing and
    cannot land inverted.
    """
    player = _player(request)
    player.set_shuffle(payload.enabled)
    return _view(player, _config(request))


@router.put(
    "/output/select",
    response_model=PlayerView,
    summary="Enable the configured speaker",
)
def select_output(request: Request) -> PlayerView:
    """Select the speaker from the config — no id is taken from the caller.

    Not an oversight. ``TUNEBOX_OUTPUT`` names the one speaker this installation plays to, the
    keeper (:class:`~tunebox.player.selection.OutputKeeper`) already re-asserts it, and this route
    exists for the one case the keeper deliberately will not handle: somebody switched the speaker
    off on purpose and now wants it back without waiting for the next power-cycle. Accepting an
    arbitrary id would also make the remote able to enable a *neighbour's* Cast device, which is
    reachable from a home network and is nobody's idea of a feature.
    """
    player = _player(request)
    config = _config(request)
    target = getattr(player, "target_output", None)
    if target is None:  # pragma: no cover - OwnTone and the fake both have target_output()
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="this player cannot select outputs by configuration",
        )
    output = target()
    player.select_output(output.id, volume=config.output_volume)
    return _view(player, config)


@router.get("/queue", response_model=list[QueueItemView], summary="The daemon's queue")
def get_queue(request: Request) -> list[QueueItemView]:
    return [QueueItemView(**item.as_dict()) for item in _player(request).queue()]


@router.post(
    "/queue",
    response_model=QueueAddedView,
    status_code=status.HTTP_201_CREATED,
    summary="Add library files to the queue",
)
def add_to_queue(payload: QueueRequest, request: Request) -> QueueAddedView:
    """Resolve every path under the music root, then hand the lot to the player at once.

    All-or-nothing on purpose: :meth:`~tunebox.core.ports.Player.enqueue_paths` resolves every
    track id before it sends anything, so a typo in the third path cannot leave the queue
    half-replaced with playback already running.
    """
    player = _player(request)
    config = _config(request)
    try:
        paths = [resolve_track_path(config.music_root, entry) for entry in payload.paths]
    except LibraryError as exc:
        # A client naming a file it should not, or one that is not there. Its mistake, not ours —
        # and the default handler for LibraryError is a 500, which this is not.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    replace_queue = payload.mode == "replace"
    added = player.enqueue_paths(paths, clear=replace_queue, start=payload.start)
    log.info(
        "queued %d track(s) (%s%s)",
        len(paths),
        payload.mode,
        ", starting" if payload.start else "",
    )
    return QueueAddedView(
        added=[QueueItemView(**item.as_dict()) for item in added],
        queue=[QueueItemView(**item.as_dict()) for item in player.queue()],
    )


@router.delete("/queue", response_model=QueueAddedView, summary="Empty the queue")
def clear_queue(request: Request) -> QueueAddedView:
    player = _player(request)
    player.clear_queue()
    return QueueAddedView(queue=[QueueItemView(**item.as_dict()) for item in player.queue()])


@router.put(
    "/queue/{item_id}/play",
    response_model=PlayerView,
    summary="Play an item that is already in the queue",
)
def play_queue_item(item_id: str, request: Request) -> PlayerView:
    """Jump to one queue item — what tapping a row in the remote's queue does.

    The id in the path is the ``id`` from ``GET /player`` (or ``GET /player/queue``), not a position
    and not a path: the queue is shared (PLAN.md 2), so a position is relative to a snapshot another
    phone can invalidate between a poll and a tap, and the same file may legitimately be in the queue
    twice, so a path names no single item.

    **The queue is checked before the command is sent**, and the ``404`` that comes out of it is the
    point. A remote polls every two seconds, so the ordinary failure here is a row somebody else
    removed while a thumb was on its way to it — a *gone* resource, which the client can recognise and
    redraw from. Without the check the answer would be OwnTone's own refusal, which is HTTP ``400``
    with an HTML body (measured 2026-09-28) and would surface as a ``502`` about a markup fragment.

    Playing the item that is *already* loaded is allowed and restarts it, because that is what the
    absolute command means and a row that quietly does nothing is the bug this route was added to
    fix. The remote's label for that row says so.
    """
    player = _player(request)
    if not any(item.id == item_id for item in player.queue()):
        # 404 and not 409: the id names a resource, and "it is not there" is the whole answer. A
        # client is expected to hit this occasionally and simply refresh.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no queue item with id {item_id!r} — the queue has changed since it was read",
        )
    player.play_item(item_id)
    return _view(player, _config(request))
