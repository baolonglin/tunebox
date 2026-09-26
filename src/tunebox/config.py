"""Configuration: one frozen dataclass, populated from the environment.

Defaults are chosen so that ``tunebox search`` and ``tunebox ingest`` work on a fresh checkout
with nothing exported. Anything a Raspberry Pi deployment needs to change is an env var.
"""

from __future__ import annotations

import importlib.util
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

    def __post_init__(self) -> None:
        if self.audio_format not in SUPPORTED_FORMATS:
            raise ConfigError(
                f"unsupported audio format {self.audio_format!r}; "
                f"expected one of {', '.join(SUPPORTED_FORMATS)}"
            )

    @property
    def scratch_root(self) -> Path:
        return self.music_root / SCRATCH_DIRNAME

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        root = env.get("TUNEBOX_MUSIC_ROOT")
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
        )
