"""The canary: one real download of one known video, proving extraction still works.

This is the single most load-bearing piece of Phase 5, because it is the only thing in the system
that can tell the difference between "nobody has asked for a track today" and "nothing can be
downloaded any more". PLAN.md 3.3 asks for it twice, for two different jobs:

* after every yt-dlp update, as the test that decides whether the update is kept or rolled back;
* once a day on its own, as the way the house learns extraction broke *before* somebody tries to
  play music (PLAN.md 5, "one synthetic ingest per day as a health check").

Both are the same operation, so it is written once and injected into both.

**It downloads media on purpose.** A metadata-only check would be cheaper and would also be nearly
worthless: the failure this project has already measured (PLAN.md 0.3) is that metadata extraction
keeps working perfectly while the *media* request answers HTTP 403 because no JavaScript runtime is
available. A canary that stopped before the media stream would have passed on that host, cheerfully,
while every real ingest failed.

**And it checks the bytes, not the exit code.** yt-dlp plus ffmpeg can produce an empty or truncated
container and still exit 0; a canary that trusted the return code would be a canary that never
sings. So the file has to be big enough to be audio, and ffprobe has to agree that it *is* audio of
roughly the expected length.

Nothing is written to the library: the canary is a throwaway fetch into the scratch directory, which
is removed afterwards even when it fails. It is deliberately not an ``ingest.ingest()`` call — the
library must not slowly fill up with copies of a test video, and dedupe would make the second run
a no-op that proved nothing.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..config import CANARY_MIN_BYTES, Config
from ..core.ports import MusicSource
from ..errors import TuneboxError
from ..sources.ytdlp import YtDlpSource

log = logging.getLogger(__name__)

#: How far the downloaded audio's duration may differ from what the metadata promised, as a
#: fraction. Generous, because a 19-second video remuxed to AAC legitimately lands a fraction of a
#: second out, and because the failure being caught here is "this is an HTML error page" or "this is
#: 4 seconds of a 19-second video", not "this is 0.3s short".
DURATION_TOLERANCE = 0.25

#: Stages, in order. The stage a canary *reached* is the most useful single field in the report: it
#: separates "YouTube will not talk to us at all" from the 403-on-media failure that PLAN.md 0.3
#: describes, and those two have completely different fixes.
STAGES = ("inspect", "fetch", "verify")


@dataclass(frozen=True)
class CanaryResult:
    """What one canary run learned. Serialisable, because it goes into the health log verbatim."""

    ok: bool
    url: str
    #: The last stage entered — where it got to if it failed, ``"verify"`` if it passed.
    stage: str
    ytdlp_version: str | None = None
    video_id: str | None = None
    title: str | None = None
    #: Size of the audio file that came back, in bytes.
    size: int | None = None
    #: Duration ffprobe read out of the file, and the one the metadata promised.
    duration: float | None = None
    expected_duration: float | None = None
    elapsed: float = 0.0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "url": self.url,
            "stage": self.stage,
            "ytdlp_version": self.ytdlp_version,
            "video_id": self.video_id,
            "title": self.title,
            "size": self.size,
            "duration": self.duration,
            "expected_duration": self.expected_duration,
            "elapsed": round(self.elapsed, 2),
            "error": self.error,
        }

    @property
    def summary(self) -> str:
        if self.ok:
            return (
                f"canary ok: {self.title or self.video_id or self.url} "
                f"({self.size or 0} bytes in {self.elapsed:.1f}s, yt-dlp {self.ytdlp_version or '?'})"
            )
        return f"canary FAILED at {self.stage}: {self.error or 'unknown error'}"


def run_canary(
    config: Config,
    *,
    source: MusicSource | None = None,
    keep: bool = False,
) -> CanaryResult:
    """Download the canary video and check that what came back is really audio.

    Never raises for a *download* problem — a failed canary is the normal, expected answer that the
    whole feature is built around, and a caller that had to catch an exception to find that out
    would be one ``except`` clause away from treating a broken extractor as a crash. Programming
    errors are still allowed to propagate.
    """
    source = source or YtDlpSource(config)
    version = ytdlp_version(config)
    url = config.canary_url
    started = time.monotonic()
    stage = STAGES[0]
    work: Path | None = None

    try:
        tags = source.inspect(url)
        stage = STAGES[1]
        work = _fresh_dir(config.canary_dir)
        media = source.fetch(url, work, audio_format=config.audio_format)

        stage = STAGES[2]
        size = media.audio_path.stat().st_size
        if size < CANARY_MIN_BYTES:
            return CanaryResult(
                ok=False,
                url=url,
                stage=stage,
                ytdlp_version=version,
                video_id=tags.source_id,
                title=tags.title,
                size=size,
                expected_duration=tags.duration,
                elapsed=time.monotonic() - started,
                error=(
                    f"downloaded only {size} bytes (expected at least {CANARY_MIN_BYTES}); "
                    "yt-dlp exited successfully, so this is a truncated or empty container, "
                    "not a network failure"
                ),
            )

        duration = probe_duration(media.audio_path)
        problem = _duration_problem(duration, tags.duration)
        return CanaryResult(
            ok=problem is None,
            url=url,
            stage=stage,
            ytdlp_version=version,
            video_id=tags.source_id,
            title=tags.title,
            size=size,
            duration=duration,
            expected_duration=tags.duration,
            elapsed=time.monotonic() - started,
            error=problem,
        )
    except TuneboxError as exc:
        return CanaryResult(
            ok=False,
            url=url,
            stage=stage,
            ytdlp_version=version,
            elapsed=time.monotonic() - started,
            error=f"{type(exc).__name__}: {exc}",
        )
    except OSError as exc:
        # An unwritable scratch directory, or a full disk. Not YouTube's fault, and the message has
        # to say so or somebody spends an evening debugging the wrong half of the system.
        return CanaryResult(
            ok=False,
            url=url,
            stage=stage,
            ytdlp_version=version,
            elapsed=time.monotonic() - started,
            error=f"local failure, not an extraction failure: {exc}",
        )
    finally:
        if work is not None and not keep:
            shutil.rmtree(work, ignore_errors=True)


def _duration_problem(actual: float | None, expected: float | None) -> str | None:
    """Is the audio the right *length*? ``None`` means "no reason to complain"."""
    if actual is None:
        # ffprobe is a hard dependency of ingest, so its absence is a real problem — but it is
        # `tunebox doctor`'s problem, and failing the canary for it would blame YouTube for a
        # missing local binary.
        return None
    if actual <= 0:
        return "ffprobe read a duration of zero: the file is a container with no audio in it"
    if not expected or expected <= 0:
        return None
    drift = abs(actual - expected) / expected
    if drift > DURATION_TOLERANCE:
        return (
            f"downloaded audio is {actual:.1f}s but the metadata says {expected:.1f}s "
            f"({drift * 100:.0f}% out): a partial download, or the wrong stream"
        )
    return None


def _fresh_dir(parent: Path) -> Path:
    """A new, empty directory for this run, under ``parent``.

    A unique subdirectory rather than ``parent`` itself, for two reasons. A leftover audio file from a
    previous run must not be picked up as *this* run's result — otherwise a fetch that silently wrote
    nothing would keep "passing" forever. And two canaries can legitimately overlap: the nightly job
    runs one while somebody types ``tunebox healthcheck``, and sharing a directory would let one
    delete the other's download, producing a spurious failure — which, inside ``tunebox update``,
    would trigger an unnecessary rollback and blocklist a perfectly good release.

    Leftovers from a run that was killed are swept on the way in, by age, so nothing accumulates.
    """
    parent.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 24 * 3600
    for stale in parent.iterdir():
        try:
            if stale.is_dir() and stale.stat().st_mtime < cutoff:
                shutil.rmtree(stale, ignore_errors=True)
        except OSError:  # pragma: no cover - raced with another sweep
            continue
    return Path(tempfile.mkdtemp(prefix="run-", dir=parent))


def probe_duration(path: Path) -> float | None:
    """Audio duration in seconds according to ffprobe, or ``None`` if it cannot say.

    ffprobe rather than mutagen, even though mutagen is already a dependency: mutagen reads the
    container's *header*, which is exactly the part a truncated download still has intact. ffprobe
    is asked about the format, which means it has to make sense of the file.
    """
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    argv = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, never a shell string
            argv,
            capture_output=True,
            timeout=30,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        return float(completed.stdout.strip())
    except ValueError:
        return None


def ytdlp_version(config: Config, argv: Sequence[str] | None = None) -> str | None:
    """The version of the yt-dlp that will actually run, asked of the binary itself.

    Not ``importlib.metadata.version("yt-dlp")``: ``TUNEBOX_YTDLP`` can point at a completely
    different yt-dlp, and the version that matters for a rollback is the one that produced the
    failing download — the same reasoning as ``doctor``'s JS-runtime probe in
    :func:`tunebox.cli._probe_js_runtimes`.
    """
    command = list(argv) if argv is not None else [*config.ytdlp, "--version"]
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, never a shell string
            command,
            capture_output=True,
            timeout=60,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip().splitlines()[0].strip() if completed.stdout.strip() else None
