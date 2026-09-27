"""The library rescan hook — PLAN.md Phase 3, first bullet.

    OwnTone on the host against the music dir; **library rescan hook after ingest.**

One rule governs this entire module: **a rescan failure is never an ingest failure.** The audio is
already on disk, correctly tagged and in the right place; the only thing a dead player daemon costs
is that the track is not immediately playable. Turning that into a failed job would throw away a
download to report a problem the user can fix by starting OwnTone — and, worse, the job's retry
would re-download the file.

So the hook returns a warning string instead of raising, and the caller attaches it to the ingest
result. That is also why it lives here rather than inline in the worker: the CLI ingests too, and
both paths must behave identically.
"""

from __future__ import annotations

import logging

from ..errors import PlayerError, TuneboxError

log = logging.getLogger(__name__)


def rescan_library(player: object) -> str | None:
    """Ask ``player`` to re-index the music root. Returns a warning if that did not work.

    ``player`` is typed loosely on purpose: anything with a ``rescan()`` satisfies the
    :class:`~tunebox.core.ports.Player` port's requirement here, which keeps the fallback-A
    player (and the tests' fakes) from having to import anything from this module.
    """
    rescan = getattr(player, "rescan", None)
    if rescan is None:  # pragma: no cover - a Player without rescan() is a programming error
        return "player has no rescan()"
    try:
        rescan()
    except PlayerError as exc:
        # Expected and survivable: OwnTone not started yet, wrong port, wrong password.
        log.warning("library rescan failed: %s", exc)
        return f"library rescan failed: {exc}"
    except TuneboxError as exc:  # pragma: no cover - other tunebox errors are not expected here
        log.warning("library rescan failed: %s", exc)
        return f"library rescan failed: {exc}"
    except Exception as exc:
        # A bug in the adapter must not cost a finished download either. Logged with a traceback
        # because, unlike the cases above, this one is ours to fix.
        log.exception("library rescan raised unexpectedly")
        return f"library rescan failed: {type(exc).__name__}: {exc}"
    log.debug("library rescan triggered")
    return None
