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


class TaggingError(TuneboxError):
    """Writing tags to the downloaded file failed."""
