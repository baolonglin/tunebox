"""The yt-dlp adapter.

yt-dlp runs as a **subprocess, never as an import** (PLAN.md 3.1): hard timeouts, memory
isolation, one bad extraction cannot take down the caller, and the version becomes a swappable
binary rather than a code-coupled API. Every invocation is an ``argv`` list — no shell string is
ever built, so a title full of quotes and semicolons is just data (PLAN.md 3.2).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Any, Sequence

from ..config import Config
from ..core.models import FetchedMedia, Progress, SearchResult, TrackTags
from ..core.ports import ProgressCallback
from ..errors import SourceError, SourceTimeout
from . import urls
from .ytdlp_tags import SOURCE_NAME, search_results_from_playlist, tags_from_info

log = logging.getLogger(__name__)

AUDIO_EXTENSIONS = frozenset({".m4a", ".mp3", ".opus", ".webm", ".ogg", ".aac", ".flac", ".wav"})

#: Cover art is chosen in this order: only JPEG and PNG can actually be embedded.
COVER_PREFERENCE = (".jpg", ".jpeg", ".png")

#: Machine-readable progress, so we parse numbers instead of yt-dlp's human formatting.
_PROGRESS_PREFIX = "TBPROGRESS"
_PROGRESS_TEMPLATE = (
    "download:" + _PROGRESS_PREFIX + " %(progress.downloaded_bytes)s"
    " %(progress.total_bytes)s %(progress.total_bytes_estimate)s"
    " %(progress.speed)s %(progress.eta)s"
)

#: Prefer YouTube's AAC-in-m4a audio stream so the common case is a passthrough remux rather
#: than a re-encode; ``/ba`` keeps rare Opus-only videos working (they are then transcoded).
_FORMAT_SELECTOR = "ba[ext=m4a]/ba"


class YtDlpSource:
    """A :class:`~tunebox.core.ports.MusicSource` backed by the yt-dlp CLI."""

    name = SOURCE_NAME

    def __init__(self, config: Config) -> None:
        self._config = config

    # ------------------------------------------------------------------ search

    def search(self, query: str, limit: int | None = None) -> list[SearchResult]:
        text = (query or "").strip()
        if not text:
            raise SourceError("empty search query")
        if any(c in text for c in "\r\n"):
            raise SourceError("search query contains control characters")
        count = max(1, min(int(limit or self._config.search_limit), 50))

        payload = self._run_json(
            [
                "--flat-playlist",
                "--dump-single-json",
                f"ytsearch{count}:{text}",
            ],
            timeout=self._config.inspect_timeout,
            what=f"search {text!r}",
        )
        return search_results_from_playlist(payload)

    # ----------------------------------------------------------------- inspect

    def inspect(self, url: str) -> TrackTags:
        _, canonical = urls.canonical_video_url(url)
        payload = self._run_json(
            [
                "--no-playlist",
                "--skip-download",
                "--dump-single-json",
                canonical,
            ],
            timeout=self._config.inspect_timeout,
            what=f"inspect {canonical}",
        )
        return tags_from_info(payload)

    # ------------------------------------------------------------------- fetch

    def fetch(
        self,
        url: str,
        dest_dir: Path,
        audio_format: str = "m4a",
        on_progress: ProgressCallback | None = None,
    ) -> FetchedMedia:
        _, canonical = urls.canonical_video_url(url)
        dest_dir.mkdir(parents=True, exist_ok=True)

        args = [
            "--no-playlist",
            # The "home:" type prefix matters: --paths parses [TYPES:]PATH on the first colon, so
            # an unprefixed path containing ':' would be read as a type name and silently write
            # somewhere else.
            "--paths",
            f"home:{dest_dir}",
            "--output",
            "track.%(ext)s",
            "--format",
            _FORMAT_SELECTOR,
            "--extract-audio",
            "--audio-format",
            audio_format,
            # Cover art is written next to the audio and embedded by our own tag writer, so a
            # thumbnail problem can never fail the audio download (PLAN.md 3.4).
            "--write-thumbnail",
            "--convert-thumbnails",
            "jpg",
            "--no-embed-metadata",
            "--newline",
            "--progress-template",
            _PROGRESS_TEMPLATE,
        ]
        if audio_format == "mp3":
            args += ["--audio-quality", "0"]
        args.append(canonical)

        self._run_streaming(args, timeout=self._config.fetch_timeout, on_progress=on_progress,
                            what=f"fetch {canonical}")

        audio = _pick(dest_dir, AUDIO_EXTENSIONS)
        if audio is None:
            raise SourceError(f"yt-dlp produced no audio file in {dest_dir}")
        cover = _pick_cover(dest_dir)
        return FetchedMedia(audio_path=audio, cover_path=cover)

    # --------------------------------------------------------------- internals

    def _base_argv(self) -> list[str]:
        cfg = self._config
        argv = [
            *cfg.ytdlp,
            # Ignore user/system yt-dlp config files: this project's behaviour must not change
            # because someone left a ~/.config/yt-dlp/config on the host.
            "--ignore-config",
            "--no-colors",
            "--socket-timeout",
            "20",
            "--sleep-requests",
            str(cfg.sleep_requests),
            "--sleep-interval",
            str(cfg.sleep_interval),
            "--max-sleep-interval",
            str(cfg.max_sleep_interval),
        ]
        # yt-dlp enables **only deno** by default and then uses the highest-priority runtime that is
        # actually installed. Naming the others *adds* them below deno rather than replacing it, so
        # this is "deno if present, otherwise node, otherwise quickjs, otherwise bun" — which is the
        # whole reason a 32-bit Raspberry Pi, where no deno build exists, can run this at all. See
        # tunebox.config.DEFAULT_JS_RUNTIMES.
        for runtime in cfg.js_runtimes:
            argv += ["--js-runtimes", runtime]
        return argv

    def _run_json(self, args: Sequence[str], *, timeout: int, what: str) -> dict[str, Any]:
        argv = [*self._base_argv(), "--no-progress", *args]
        log.debug("running %s", argv)
        try:
            completed = subprocess.run(  # noqa: S603 - argv list, never a shell string
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SourceError(f"yt-dlp not found: {' '.join(self._config.ytdlp)}") from exc
        except subprocess.TimeoutExpired as exc:
            raise SourceTimeout(f"{what} timed out after {timeout}s") from exc

        if completed.returncode != 0:
            raise SourceError(f"{what} failed (exit {completed.returncode}): "
                              f"{_tail(completed.stderr) or _tail(completed.stdout)}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise SourceError(f"{what}: yt-dlp returned unparseable JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise SourceError(f"{what}: expected a JSON object, got {type(payload).__name__}")
        return payload

    def _run_streaming(
        self,
        args: Sequence[str],
        *,
        timeout: int,
        on_progress: ProgressCallback | None,
        what: str,
    ) -> None:
        argv = [*self._base_argv(), *args]
        log.debug("running %s", argv)
        try:
            process = subprocess.Popen(  # noqa: S603 - argv list, never a shell string
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                # Own process group: yt-dlp spawns ffmpeg, and a timeout must take the whole
                # tree down rather than orphaning a transcode.
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise SourceError(f"yt-dlp not found: {' '.join(self._config.ytdlp)}") from exc

        # A watchdog, not a deadline check in the read loop: a hung extraction produces no
        # output at all, so a loop-based check would never run. This is the difference between
        # "times out" and "hangs until someone notices".
        timed_out = threading.Event()

        def _watchdog() -> None:
            # Only a *running* child is a timeout. The parent can still be draining buffered
            # output (or in a slow progress callback) after a successful, fast exit, and killing
            # nothing while reporting a timeout would fail a download that actually worked.
            if process.poll() is not None:
                return
            timed_out.set()
            _terminate(process)

        timer = threading.Timer(timeout, _watchdog)
        timer.daemon = True
        timer.start()

        tail: deque[str] = deque(maxlen=20)
        assert process.stdout is not None
        try:
            for line in process.stdout:
                line = line.rstrip("\n")
                if line.startswith(_PROGRESS_PREFIX):
                    if on_progress is not None:
                        progress = _parse_progress(line)
                        if progress is not None:
                            on_progress(progress)
                    continue
                if line.strip():
                    tail.append(line)
                    log.debug("yt-dlp: %s", line)
            returncode = process.wait(timeout=30)
        except subprocess.TimeoutExpired as exc:
            _terminate(process)
            raise SourceTimeout(f"{what} timed out after {timeout}s") from exc
        finally:
            timer.cancel()
            if process.poll() is None:
                _terminate(process)
            process.stdout.close()

        if timed_out.is_set():
            raise SourceTimeout(f"{what} timed out after {timeout}s")
        if returncode != 0:
            raise SourceError(f"{what} failed (exit {returncode}): {_tail(chr(10).join(tail))}")


def _terminate(process: subprocess.Popen) -> None:
    """Kill the whole process group: yt-dlp's ffmpeg child must not survive us."""
    if process.poll() is not None:
        return  # already reaped; its pid may have been recycled, so never signal it
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        process.terminate()
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
        log.error("yt-dlp process %s did not die", process.pid)


def _parse_progress(line: str) -> Progress | None:
    parts = line.split()
    if len(parts) != 6:
        return None
    downloaded, total, total_estimate, speed, eta = parts[1:]

    def num(value: str) -> float | None:
        if value in ("NA", "None", ""):
            return None
        try:
            return float(value)
        except ValueError:
            return None

    total_value = num(total) or num(total_estimate)
    downloaded_value = num(downloaded)
    percent = None
    if total_value and downloaded_value is not None:
        percent = min(100.0, downloaded_value / total_value * 100.0)
    eta_value = num(eta)
    return Progress(
        percent=percent,
        downloaded_bytes=int(downloaded_value) if downloaded_value is not None else None,
        total_bytes=int(total_value) if total_value else None,
        speed=num(speed),
        eta=int(eta_value) if eta_value is not None else None,
    )


def _pick(directory: Path, extensions: frozenset[str]) -> Path | None:
    """Largest matching file in ``directory`` — the audio, not a leftover fragment."""
    candidates = [
        p for p in sorted(directory.iterdir())
        if p.is_file() and p.suffix.lower() in extensions and not p.name.endswith(".part")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def _pick_cover(directory: Path) -> Path | None:
    """Cover art by format preference, not by size.

    The tag writer only embeds JPEG and PNG, so a leftover (often larger) ``.webp`` must not win
    and silently cost us the artwork.
    """
    for extension in COVER_PREFERENCE:
        for candidate in sorted(directory.iterdir()):
            if candidate.is_file() and candidate.suffix.lower() == extension:
                return candidate
    return None


def _tail(text: str | None, limit: int = 600) -> str:
    if not text:
        return ""
    cleaned = text.strip()
    return cleaned[-limit:]
