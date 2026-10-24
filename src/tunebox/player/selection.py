"""Keeping the soundbar selected.

PLAN.md Phase 3 asks for one specific property:

    Soundbar selected as a persistent output; survives an OwnTone restart and a soundbar
    power-cycle without manual re-selection.

and the risk table already predicted the shape of the fix — *"automate re-selection on startup if
manual"*. This module is that automation, and the interesting part is not the loop, it is knowing
**when not to act**.

A naive reconciler that re-selects the output whenever it finds it deselected would be a bug, not a
feature: deselecting the speaker from OwnTone's own web UI is a legitimate thing for the person in
the room to do, and having it silently switch back on within fifteen seconds would be indisputably
worse than having to re-select it after a power cut.

So the keeper holds two pieces of state and one rule:

* ``_wanted`` — what we believe the output's state *should* be. Unknown until we first see it; true
  once we have seen it on or turned it on ourselves; false once somebody has turned it off while we
  were watching.
* ``_away`` — has the output been out of sight since we last saw it? Both a speaker vanishing from
  mDNS (power-cycle, Wi-Fi drop, unplugged) and the daemon vanishing entirely (an OwnTone restart)
  count, and deliberately are not distinguished, because the decision does not depend on which it was.

    **Assert selection when the speaker is visible and off, and either we have never seen it, or it
    has been away since we last saw it and the last thing we knew was that it should be on.**

That one rule produces all the behaviour Phase 3 asks for. A fresh OwnTone with nothing selected is
"never seen it" → selected. A soundbar that was playing, got power-cycled, and came back is "away,
and it should be on" → selected. A speaker somebody switched off is "visible and off and not away"
→ ``_wanted`` becomes false, and from then on neither a power-cycle nor a daemon restart brings it
back, because what we remember is the decision rather than a snapshot.

Two details in that are load-bearing, and the previous design got both wrong:

* **What is remembered is the *decision*, not a snapshot of the last observation.** An earlier version
  tracked "was the output selected when I last looked", which meant the observation that *triggered*
  a selection ("off") was the one written down — so the next time the speaker went away, the keeper
  concluded it had been switched off on purpose and never brought it back. A permanent, silent
  failure, and one an audit found rather than use.
* **An outage must not touch ``_wanted``.** A daemon that is down tells us nothing about whether the
  speaker should be on. Writing "off" there loses the soundbar; writing "on" there overrides the
  person who switched it off. That is the whole reason ``_away`` exists as a separate flag rather
  than being inferred.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from ..config import Config
from ..core.models import Output
from ..errors import OutputNotFound, PlayerError, PlayerUnavailable
from .owntone import OwnTonePlayer

log = logging.getLogger(__name__)


class OutputKeeper:
    """Re-asserts the configured output when it appears, and only then.

    Runs as one daemon thread inside ``tunebox serve``, next to the ingest worker. It is not an
    async task because everything it does is a blocking loopback HTTP call, and it is not a cron
    entry because the state it needs — "was the speaker here last time I looked" — has to survive
    between checks.
    """

    def __init__(
        self,
        *,
        player: OwnTonePlayer,
        config: Config,
        on_select: Callable[[Output], None] | None = None,
    ) -> None:
        self._player = player
        self._config = config
        self._on_select = on_select
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        #: What we believe the output's state *should* be. ``None`` = never seen it, so nothing is
        #: believed yet; ``True`` = it should be on (it was on when we looked, or we turned it on);
        #: ``False`` = somebody turned it off and we are respecting that.
        self._wanted: bool | None = None
        #: Has the output been out of sight since the last successful observation? Covers both the
        #: speaker disappearing from mDNS and the daemon disappearing, deliberately without
        #: distinguishing them — the decision below does not depend on which it was.
        self._away = False
        self._lock = threading.Lock()
        #: Guards the thread handle itself, which ``start``/``stop`` touch from a different thread
        #: than ``_loop`` does.
        self._lifecycle = threading.Lock()
        #: Counters, so a test (and ``GET /healthz`` later) can assert behaviour rather than
        #: scrape logs.
        self.selections = 0
        self.checks = 0
        #: How many times the output came back after being out of sight. Useful evidence when asking
        #: "did the soundbar survive that, and what made it survive".
        self.recoveries = 0
        self.last_error: str | None = None

    # ----------------------------------------------------------------- lifecycle

    @property
    def enabled(self) -> bool:
        return self._config.output_check_interval > 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if not self.enabled:
            log.info("output keeper disabled (TUNEBOX_OUTPUT_CHECK_INTERVAL=0)")
            return
        with self._lifecycle:
            if self._thread is not None:
                raise RuntimeError("output keeper already started")
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="tunebox-output", daemon=True
            )
            self._thread.start()
        log.info(
            "output keeper watching for %r every %.0fs",
            self._config.output_id or self._config.output_name,
            self._config.output_check_interval,
        )

    def stop(self, timeout: float = 5.0) -> None:
        with self._lifecycle:
            self._stop.set()
            thread = self._thread
            if thread is None:
                return
            thread.join(timeout=timeout)
            if thread.is_alive():
                # Do *not* forget the thread: clearing it would let a later start() create a second
                # keeper racing the first over the same output.
                log.warning("output keeper thread did not stop within %.0fs", timeout)
                return
            self._thread = None

    # ---------------------------------------------------------------------- loop

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                # The very first check happens immediately: the point of this thread is that a
                # restart does not need a human, and waiting fifteen seconds to prove it would be
                # theatre.
                self.check_once()
            except BaseException:  # pragma: no cover - check_once is already total
                # Belt and braces. This thread is the only thing that notices the soundbar coming
                # back, so it must outlive any bug in the layer below it. Without this, one
                # unexpected exception ends playback recovery for the lifetime of the process, and
                # nothing anywhere reports that the keeper is gone.
                log.exception("output keeper pass failed unexpectedly; continuing")
            self._stop.wait(self._config.output_check_interval)

    def check_once(self) -> Output | None:
        """One reconciliation pass. Returns the output if it was (re-)selected by this call.

        **Never raises**, for any exception type — not just the ones in
        :mod:`tunebox.errors`. A daemon that is down, a speaker that is unplugged and a shape of JSON
        we did not anticipate are all normal on a home network, and none of them may kill the thread
        whose whole job is to notice the speaker coming back.

        The decision is one rule, and the three interesting cases are all consequences of it:

            assert selection when the speaker is visible and off, **and** either we have never seen
            it before, or it has been out of sight since we last saw it *and* the last thing we knew
            was that it should be on.

        "Out of sight" covers both halves of the problem — the speaker disappearing from mDNS (a
        power-cycle, a Wi-Fi drop) and the daemon disappearing entirely (an OwnTone restart) — which
        is why they do not need separate handling. And keying off *what we last knew should be on*
        rather than off "was it selected a moment ago" is what keeps a deliberate deselection
        deliberate: if somebody switched the speaker off, that is what we remember, and neither a
        power-cycle nor a daemon restart resurrects it.
        """
        with self._lock:
            self.checks += 1
            try:
                output = self._player.target_output()
            except OutputNotFound as exc:
                # The daemon answered; it is the speaker that is gone.
                self._note_away("configured output is not visible", exc)
                return None
            except PlayerUnavailable as exc:
                # The daemon itself is gone, so the speaker's state is unknown, not off.
                self._note_away("output keeper cannot reach the player", exc)
                return None
            except PlayerError as exc:
                # It answered, with something unexpected. Neither an outage nor an absence, so no
                # state moves: guessing either way would act on a fact we do not have.
                if self.last_error != str(exc):
                    log.info("output keeper got an error from the player: %s", exc)
                self.last_error = str(exc)
                return None
            except Exception as exc:
                # A bug in the adapter, or a response shape nobody predicted. Logged with a
                # traceback because unlike the cases above this one is ours to fix — but still not
                # allowed to end the thread.
                log.exception("output keeper could not read the output list")
                self.last_error = f"{type(exc).__name__}: {exc}"
                return None

            first_sight = self._wanted is None
            came_back = self._away
            self._away = False
            self.last_error = None
            if came_back:
                self.recoveries += 1

            if output.selected:
                # Whatever else happened, the desired state is now unambiguous.
                self._wanted = True
                if first_sight or came_back:
                    log.info("output %r (%s) is already selected", output.name, output.type)
                return None

            if first_sight or (came_back and self._wanted is True):
                return self._select(output)

            # Visible, off, and it did not go anywhere: somebody switched it off. Remember that, so
            # that a later power-cycle or daemon restart does not switch it back on.
            if self._wanted is not False:
                log.info(
                    "output %r is deselected and was not away; leaving it off", output.name
                )
            self._wanted = False
            return None

    def note_manual_selection(self, output_id: str, *, is_configured: bool) -> None:
        """Somebody chose an output from the remote. Stop having an opinion that contradicts them.

        Called by ``PUT /player/output``. Without it the keeper would still *eventually* reach the
        right answer — a configured speaker that has been switched off is "visible, off and not
        away" on the next pass, which already records the decision — but "eventually" is up to
        ``TUNEBOX_OUTPUT_CHECK_INTERVAL`` seconds, and inside that window a speaker that
        power-cycles is one the keeper resurrects on top of the choice somebody just made. Being
        told directly closes that window and, more importantly, makes the intent explicit rather
        than emergent: this is the one method whose existence says "a person outranks the
        reconciler".

        ``is_configured`` is decided by the caller rather than re-derived here, because the question
        "is this the configured speaker" has two spellings — an id or a case-folded name — and
        :func:`tunebox.api.remote.configured_output` is already the one place that knows the
        precedence between them. Choosing the configured speaker is the same statement in the other
        direction, so it *sets* ``_wanted``: otherwise re-selecting the soundbar by hand would leave
        the keeper believing it should be off, and the next power-cycle would lose it.
        """
        with self._lock:
            self._wanted = bool(is_configured)
            # Not away: we have just had a successful conversation with the daemon about it.
            self._away = False
            log.info(
                "output %s chosen from the remote; keeper will %s the configured speaker",
                output_id,
                "keep asserting" if is_configured else "leave alone",
            )

    def _note_away(self, message: str, exc: Exception) -> None:
        """Record that the output is out of sight, without deciding *why*.

        ``_wanted`` is deliberately untouched: an outage tells us nothing about whether the speaker
        should be on, and overwriting it here is what would turn a restart into an unwanted
        re-selection (or, the other way round, lose the selection permanently).
        """
        if not self._away:
            # Log the transition once, not once per interval: an unplugged soundbar should not fill
            # the journal.
            log.info("%s: %s", message, exc)
        self._away = True
        self.last_error = str(exc)

    def _select(self, output: Output) -> Output | None:
        try:
            self._player.select_output(output.id, volume=self._config.output_volume)
        except (PlayerUnavailable, PlayerError) as exc:
            # Treat it as out of sight again, so the next pass retries rather than concluding that
            # the speaker is present-and-off and therefore somebody's decision. A speaker can drop
            # off Wi-Fi between being listed and being selected.
            self._away = True
            self.last_error = str(exc)
            log.warning("could not select output %r: %s", output.name, exc)
            return None
        except Exception as exc:
            log.exception("could not select output %r", output.name)
            self._away = True
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None
        # Record the desired state rather than leaving it unknown. Strictly speaking this is
        # redundant today — ``_wanted`` is still ``None`` here, so the *first sight* branch would
        # cover the same cases — but the previous design tracked "was it selected when I looked",
        # wrote the pre-selection observation ("off") into it, and thereby concluded on the next
        # power-cycle that somebody had switched the speaker off on purpose. That was a permanent,
        # silent failure found by an audit rather than by use. Stating the intent here is what keeps
        # the two ways of reaching "this speaker should be on" from drifting apart again.
        self._wanted = True
        self.selections += 1
        log.info(
            "selected output %r (%s, id %s)%s",
            output.name,
            output.type,
            output.id,
            f" at volume {self._config.output_volume}"
            if self._config.output_volume is not None
            else "",
        )
        if self._on_select is not None:
            try:
                self._on_select(output)
            except Exception:  # pragma: no cover - a callback must not break the keeper
                log.debug("output-selected callback failed", exc_info=True)
        return output
