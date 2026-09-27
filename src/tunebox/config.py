"""Configuration: one frozen dataclass, populated from the environment.

Defaults are chosen so that ``tunebox search`` and ``tunebox ingest`` work on a fresh checkout
with nothing exported. Anything a Raspberry Pi deployment needs to change is an env var.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .errors import ConfigError

#: Containers we are willing to produce. ``m4a`` (AAC) is the default because it is the format
#: verified playing on the SN9YG over Cast; ``mp3`` exists for the Bluetooth fallback.
SUPPORTED_FORMATS = ("m4a", "mp3")

DEFAULT_MUSIC_ROOT = Path.home() / "Music" / "tunebox"

#: Scratch directory *inside* the music root, so the final move is same-filesystem and atomic.
#: The leading dot keeps music daemons from indexing partial downloads.
SCRATCH_DIRNAME = ".tunebox-tmp"

#: State directory for the job queue, also inside the music root: the library volume is the one
#: thing a deployment definitely has, and the dot keeps scanners out of it.
STATE_DIRNAME = ".tunebox"
DB_FILENAME = "jobs.db"

#: Loopback, not ``0.0.0.0``: PLAN.md 3.2 forbids an open endpoint, so binding every interface
#: has to be a deliberate act (``TUNEBOX_API_HOST=192.168.x.y``), never a default.
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8080

#: PLAN.md 3.1: "Concurrency capped at 1–2." It is a politeness limit towards YouTube, not a
#: throughput knob, so the ceiling is enforced rather than documented.
MAX_WORKER_CONCURRENCY = 2


def binds_every_interface(host: str) -> bool:
    """Would binding ``host`` expose the service on every interface, including the router-facing
    one?

    Parsed with :mod:`ipaddress` rather than compared against a list of strings, because
    ``0.0.0.0`` has many spellings — ``0``, ``0x0``, ``0.0``, ``::``, ``::0`` and
    ``0:0:0:0:0:0:0:0`` all resolve to the wildcard, and a blocklist of literals catches none of
    them. An empty host means the same thing to the socket layer.
    """
    value = (host or "").strip().strip("[]")
    if value == "" or value == "*":
        return True
    try:
        return ipaddress.ip_address(value).is_unspecified
    except ValueError:
        pass
    # Not a literal address: it is a hostname, and a hostname cannot be the wildcard. Anything
    # numeric-but-unparseable (``0x0``, ``0.0``, ``00``) is a legacy inet_aton spelling that the
    # socket layer *does* accept as the wildcard, so refuse those too.
    if all(ch in "0.:xX" for ch in value):
        return True
    return False


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc


def _env_bool(env: Mapping[str, str], key: str, default: bool = False) -> bool:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key} must be a boolean (1/0, true/false), got {raw!r}")


def resolve_ytdlp_command(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Decide how to invoke yt-dlp.

    Prefers ``python -m yt_dlp`` from the *current interpreter* so the version actually used is
    the one pinned in this project's dependencies, not whatever happens to be first on PATH
    (PLAN.md 3.3 pins the version on purpose). Falls back to a PATH binary, and finally to the
    bare name so the resulting error message is about a missing binary rather than a crash.
    """
    env = os.environ if env is None else env
    override = env.get("TUNEBOX_YTDLP")
    if override:
        return tuple(override.split())

    if importlib.util.find_spec("yt_dlp") is not None:
        return (sys.executable, "-m", "yt_dlp")

    found = shutil.which("yt-dlp")
    return (found,) if found else ("yt-dlp",)


@dataclass(frozen=True)
class Config:
    music_root: Path = DEFAULT_MUSIC_ROOT
    audio_format: str = "m4a"
    ytdlp: tuple[str, ...] = field(default_factory=resolve_ytdlp_command)
    search_limit: int = 20
    #: Metadata-only calls are fast; a long hang here is a bot challenge, not slow progress.
    inspect_timeout: int = 60
    fetch_timeout: int = 600
    #: Be a light-touch client (PLAN.md 0.3). These are seconds.
    sleep_requests: float = 1.0
    sleep_interval: float = 1.0
    max_sleep_interval: float = 5.0

    # ---------------------------------------------------------------- Phase 2: the service
    #: Where the API listens. Loopback by default; a LAN address is an explicit choice.
    api_host: str = DEFAULT_API_HOST
    api_port: int = DEFAULT_API_PORT
    #: Shared secret checked on *every* request (PLAN.md 3.2). No default: a service that
    #: downloads URLs and runs ffmpeg does not get to have a blank password.
    api_token: str | None = None
    #: Opt-in escape hatch for binding every interface, e.g. inside a container that only has
    #: one. Never enable it on a host that faces the router.
    allow_all_interfaces: bool = False
    #: ``None`` means "under the music root" — see :attr:`database_path`.
    db_path: Path | None = None
    worker_concurrency: int = 1
    #: Bounded retries (PLAN.md 3.1): three attempts total, exponential backoff in between.
    job_max_attempts: int = 3
    job_retry_backoff: float = 10.0
    job_retry_backoff_max: float = 300.0
    #: How long an idle worker waits before looking again; enqueues wake it immediately, so this
    #: only bounds the delay for retries that became due.
    job_poll_interval: float = 1.0

    def __post_init__(self) -> None:
        if self.audio_format not in SUPPORTED_FORMATS:
            raise ConfigError(
                f"unsupported audio format {self.audio_format!r}; "
                f"expected one of {', '.join(SUPPORTED_FORMATS)}"
            )
        if not 1 <= self.api_port <= 65535:
            raise ConfigError(f"api port out of range: {self.api_port}")
        if not 1 <= self.worker_concurrency <= MAX_WORKER_CONCURRENCY:
            raise ConfigError(
                f"worker concurrency must be between 1 and {MAX_WORKER_CONCURRENCY}, "
                f"got {self.worker_concurrency} (PLAN.md 3.1 caps it deliberately)"
            )
        if self.job_max_attempts < 1:
            raise ConfigError(f"job attempts must be at least 1, got {self.job_max_attempts}")
        if self.job_retry_backoff < 0 or self.job_retry_backoff_max < 0:
            raise ConfigError("retry backoff must not be negative")
        if self.job_poll_interval <= 0:
            raise ConfigError(f"poll interval must be positive, got {self.job_poll_interval}")

    @property
    def scratch_root(self) -> Path:
        return self.music_root / SCRATCH_DIRNAME

    @property
    def database_path(self) -> Path:
        """SQLite file for jobs and ingest history. Tracks live on the filesystem (PLAN.md 3.1)."""
        return self.db_path or self.music_root / STATE_DIRNAME / DB_FILENAME

    def retry_delay(self, attempts: int) -> float:
        """Backoff before attempt ``attempts + 1``: exponential, capped, never negative."""
        if self.job_retry_backoff == 0:
            return 0.0
        delay = self.job_retry_backoff * (2 ** max(0, attempts - 1))
        return min(delay, self.job_retry_backoff_max) if self.job_retry_backoff_max else delay

    @property
    def job_timeout(self) -> float:
        """How long a job may be ``running`` before it is treated as abandoned.

        Derived rather than configured: the real timeouts are the ones the yt-dlp adapter enforces
        on its subprocesses, and this only has to be comfortably longer than both of them put
        together so that a slow-but-live download is never reclaimed underneath itself.
        """
        return self.inspect_timeout + self.fetch_timeout + 60.0

    def validate_for_serve(self) -> str:
        """Check the things that only matter when listening, and return the token.

        Deliberately *not* in ``__post_init__``: ``tunebox search`` and ``tunebox ingest`` must
        keep working on a fresh checkout with nothing exported, and they open no socket.
        """
        token = (self.api_token or "").strip()
        if not token:
            raise ConfigError(
                "TUNEBOX_TOKEN is not set. The API is password-protected even on the LAN "
                "(PLAN.md 3.2). Generate one with: python3 -c "
                "'import secrets; print(secrets.token_urlsafe(32))'"
            )
        if len(token) < 16:
            raise ConfigError("TUNEBOX_TOKEN is too short; use at least 16 characters")
        if not token.isascii():
            # Header values are bytes, and a token that only round-trips through one encoding is
            # a way to lock yourself out of your own service. Refuse it at startup instead.
            raise ConfigError("TUNEBOX_TOKEN must be ASCII; use secrets.token_urlsafe")
        if any(ch.isspace() for ch in token):
            raise ConfigError("TUNEBOX_TOKEN must not contain whitespace")
        if binds_every_interface(self.api_host) and not self.allow_all_interfaces:
            raise ConfigError(
                f"refusing to bind {self.api_host or '(empty)'}: that is every interface, and "
                "PLAN.md 3.2 says this service is never exposed. Set TUNEBOX_API_HOST to the LAN "
                "address, or TUNEBOX_ALLOW_ALL_INTERFACES=1 if you really mean it"
            )
        return token

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        root = env.get("TUNEBOX_MUSIC_ROOT")
        db = env.get("TUNEBOX_DB")
        return cls(
            music_root=Path(root).expanduser() if root else DEFAULT_MUSIC_ROOT,
            audio_format=(env.get("TUNEBOX_AUDIO_FORMAT") or "m4a").lower(),
            ytdlp=resolve_ytdlp_command(env),
            search_limit=_env_int(env, "TUNEBOX_SEARCH_LIMIT", 20),
            inspect_timeout=_env_int(env, "TUNEBOX_INSPECT_TIMEOUT", 60),
            fetch_timeout=_env_int(env, "TUNEBOX_FETCH_TIMEOUT", 600),
            sleep_requests=_env_float(env, "TUNEBOX_SLEEP_REQUESTS", 1.0),
            sleep_interval=_env_float(env, "TUNEBOX_SLEEP_INTERVAL", 1.0),
            max_sleep_interval=_env_float(env, "TUNEBOX_MAX_SLEEP_INTERVAL", 5.0),
            api_host=(env.get("TUNEBOX_API_HOST") or DEFAULT_API_HOST).strip(),
            api_port=_env_int(env, "TUNEBOX_API_PORT", DEFAULT_API_PORT),
            api_token=env.get("TUNEBOX_TOKEN"),
            allow_all_interfaces=_env_bool(env, "TUNEBOX_ALLOW_ALL_INTERFACES", False),
            db_path=Path(db).expanduser() if db else None,
            worker_concurrency=_env_int(env, "TUNEBOX_WORKERS", 1),
            job_max_attempts=_env_int(env, "TUNEBOX_JOB_ATTEMPTS", 3),
            job_retry_backoff=_env_float(env, "TUNEBOX_JOB_BACKOFF", 10.0),
            job_retry_backoff_max=_env_float(env, "TUNEBOX_JOB_BACKOFF_MAX", 300.0),
            job_poll_interval=_env_float(env, "TUNEBOX_JOB_POLL_INTERVAL", 1.0),
        )
