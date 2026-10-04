"""Exception hierarchy.

Every error tunebox raises deliberately derives from :class:`TuneboxError` so the CLI (and
later the API) can distinguish "this ingest failed" from "our code has a bug".
"""


class TuneboxError(Exception):
    """Base class for all expected tunebox failures."""


class ConfigError(TuneboxError):
    """Configuration is unusable (bad format name, unwritable music root, ...)."""


class InvalidURLError(TuneboxError):
    """URL failed the allowlist check in :mod:`tunebox.sources.urls`."""


class SourceError(TuneboxError):
    """The source adapter (yt-dlp) failed: extraction error, bad JSON, missing binary."""


class SourceTimeout(SourceError):
    """The source adapter exceeded its timeout and was killed."""


class LibraryError(TuneboxError):
    """Placing a file in the library failed, including attempted escapes from the root."""


class PlaylistError(TuneboxError):
    """A playlist could not be named, read or written.

    Deliberately *not* a subclass of :class:`LibraryError`, even though a playlist is a file in the
    library: the API maps the two differently, and the distinction is the useful one. A bad library
    path in a playlist's entries is still a :class:`LibraryError` (``400``, "that is not a track"),
    while this is about the playlist itself — its name, or its absence.
    """


class PlaylistNotFound(PlaylistError):
    """No playlist by that name. Mapped to ``404``: it names a resource that is not there."""


class PlaylistExists(PlaylistError):
    """A playlist by that name is already there and the caller did not ask to replace it.

    ``409`` rather than ``400``: the request is well formed, and the one thing standing in its way is
    state the caller can see with ``GET /playlists``. Overwriting by default would make "create" and
    "replace everything in it" the same request, which is a bad default for the one thing in the
    library a person arranged by hand.
    """


class TaggingError(TuneboxError):
    """Writing tags to the downloaded file failed."""


class PlayerError(TuneboxError):
    """The player daemon refused a command or answered with something we cannot parse."""


class PlayerUnavailable(PlayerError):
    """The player daemon is not answering at all — not started, wrong port, or wrong password.

    Separate from :class:`PlayerError` because the fix is different: this one is "start OwnTone",
    not "that command was wrong". The ingest path treats it as a warning, never a failure.
    """


class OutputNotFound(PlayerError):
    """The configured speaker is not in the daemon's output list.

    Expected and survivable: a soundbar that is unplugged disappears from mDNS. PLAN.md Phase 3
    requires that it be re-selected automatically when it comes back, which is
    :class:`tunebox.player.selection.OutputKeeper`'s job.
    """


class OutputUnreachable(PlayerError):
    """The speaker *is* listed, and the daemon could not bring it up.

    The distinct case between :class:`OutputNotFound` ("it is not there") and a plain
    :class:`PlayerError` ("that command was wrong"), and the one a Cast device produces most often:
    an LG soundbar in standby keeps advertising ``_googlecast._tcp`` while its Wi-Fi stops answering
    ARP, so OwnTone lists it, fails to open the control channel on port 8009 with ``No route to
    host``, and answers the enable with ``400`` and an HTML body. Measured on this household's
    hardware against OwnTone 29.3, 2026-09-30.

    Retryable, and that is why it is separate: the API maps it to ``503`` rather than ``502``, and
    the message says "try again in a moment" because on the measured failure a later attempt
    connected with nothing else changed.
    """
