"""Command line front end — the whole of Phase 1's user interface.

Three commands: ``search``, ``ingest``, ``doctor``. Output is JSON on stdout by default so the
Phase 2 API can be a thin wrapper over the same use cases, and so shell pipelines work; progress
and logs go to stderr and never corrupt that JSON.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Sequence, TextIO

from . import __version__, ingest as ingest_module
from .config import SUPPORTED_FORMATS, Config
from .core.models import Progress
from .errors import TuneboxError
from .sources.ytdlp import YtDlpSource


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tunebox",
        description="Search YouTube and pull tracks into a local music library.",
    )
    parser.add_argument("--version", action="version", version=f"tunebox {__version__}")
    parser.add_argument(
        "--music-root",
        type=Path,
        help="library root (default: $TUNEBOX_MUSIC_ROOT or ~/Music/tunebox)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging on stderr")
    sub = parser.add_subparsers(dest="command", required=True)

    search = sub.add_parser("search", help="search for candidate tracks (JSON on stdout)")
    search.add_argument("query", nargs="+", help="search terms")
    search.add_argument("-n", "--limit", type=int, default=None, help="max results (default 20)")
    search.add_argument("--table", action="store_true", help="human-readable output instead of JSON")
    search.set_defaults(func=_cmd_search)

    ing = sub.add_parser("ingest", help="download one URL into the library")
    ing.add_argument("url", help="YouTube URL or bare 11-character video id")
    ing.add_argument(
        "-f",
        "--format",
        choices=SUPPORTED_FORMATS,
        default=None,
        help="container to produce (default m4a: AAC passthrough, verified on the soundbar)",
    )
    ing.add_argument("--artist", help="override the detected artist")
    ing.add_argument("--title", help="override the detected title")
    ing.add_argument("--album", help="override the detected album")
    ing.add_argument("--year", type=int, help="override the detected year")
    ing.add_argument("--track-number", type=int, help="set a track number")
    ing.add_argument("--force", action="store_true", help="re-download even if already present")
    ing.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch metadata only and print the destination path",
    )
    ing.set_defaults(func=_cmd_ingest)

    doctor = sub.add_parser("doctor", help="check external dependencies and the library root")
    doctor.set_defaults(func=_cmd_doctor)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        config = Config.from_env()
        overrides: dict[str, object] = {}
        if args.music_root is not None:
            overrides["music_root"] = args.music_root.expanduser()
        if getattr(args, "format", None):
            overrides["audio_format"] = args.format
        if overrides:
            config = replace(config, **overrides)
        return int(args.func(args, config))
    except TuneboxError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        # Unwritable library, full disk, a music root that is actually a file: all user-fixable
        # situations that must read as an error, not as a stack trace.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


# ---------------------------------------------------------------- subcommands


def _cmd_search(args: argparse.Namespace, config: Config) -> int:
    source = YtDlpSource(config)
    results = source.search(" ".join(args.query), limit=args.limit)
    if args.table:
        if not results:
            print("no results", file=sys.stderr)
        for index, result in enumerate(results, start=1):
            print(
                f"{index:2d}. {result.title}\n"
                f"    {result.uploader or '?'} · {_format_duration(result.duration)} · {result.url}"
            )
    else:
        json.dump([r.as_dict() for r in results], sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    return 0 if results else 2


def _cmd_ingest(args: argparse.Namespace, config: Config) -> int:
    source = YtDlpSource(config)
    overrides: dict[str, object] = {
        "artist": args.artist,
        "title": args.title,
        "album": args.album,
        "year": args.year,
        "track_number": args.track_number,
    }

    if args.dry_run:
        tags, destination = ingest_module.plan_destination(
            args.url, source=source, config=config, overrides=overrides
        )
        json.dump(
            {"path": str(destination), "exists": destination.exists(), "tags": tags.as_dict()},
            sys.stdout,
            indent=2,
            ensure_ascii=False,
        )
        sys.stdout.write("\n")
        return 0

    reporter = _ProgressReporter(sys.stderr)
    result = ingest_module.ingest(
        args.url,
        source=source,
        config=config,
        on_progress=reporter,
        overrides=overrides,
        force=args.force,
    )
    reporter.finish()
    json.dump(result.as_dict(), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def _cmd_doctor(args: argparse.Namespace, config: Config) -> int:
    """Check exactly the dependencies PLAN.md 0.3 says YouTube audio needs."""
    checks: list[tuple[str, bool, str]] = []

    version = _probe_version([*config.ytdlp, "--version"])
    checks.append(("yt-dlp", version is not None, version or "not runnable (required)"))

    # ffmpeg extracts the audio; deno is the JS runtime yt-dlp-ejs needs. Both are hard
    # requirements: without the JS runtime every media download fails with HTTP 403 (PLAN.md 0.3).
    for binary in ("ffmpeg", "ffprobe", "deno"):
        path = shutil.which(binary)
        checks.append((binary, path is not None, path or "MISSING (required)"))

    # Without yt-dlp-ejs + a JS runtime, YouTube returns 403 for the media stream (PLAN.md 0.3).
    try:
        import yt_dlp_ejs

        checks.append(("yt-dlp-ejs", True, getattr(yt_dlp_ejs, "__version__", "installed")))
    except ImportError:
        checks.append(("yt-dlp-ejs", False, "not importable (required: downloads fail with 403)"))

    try:
        import mutagen

        checks.append(("mutagen", True, mutagen.version_string))
    except ImportError:  # pragma: no cover - dependency is declared
        checks.append(("mutagen", False, "not importable (required)"))

    root = config.music_root
    writable = _probe_writable(root)
    checks.append(("music root", writable, f"{root} ({'writable' if writable else 'NOT writable'})"))

    width = max(len(name) for name, _, _ in checks)
    for name, ok, note in checks:
        print(f"[{'ok' if ok else '!!'}] {name.ljust(width)}  {note}")
    return 0 if all(ok for _, ok, _ in checks) else 1


# -------------------------------------------------------------------- helpers


class _ProgressReporter:
    """One rewritten line on stderr; silent when stderr is not a terminal (e.g. cron, systemd)."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._enabled = stream.isatty()
        self._last = -1.0

    def __call__(self, progress: Progress) -> None:
        if not self._enabled or progress.percent is None:
            return
        if progress.percent - self._last < 1.0 and progress.percent < 100.0:
            return
        self._last = progress.percent
        speed = f" {progress.speed / 1024 / 1024:.1f} MiB/s" if progress.speed else ""
        eta = f" eta {progress.eta}s" if progress.eta else ""
        self._stream.write(f"\rdownloading {progress.percent:5.1f}%{speed}{eta}   ")
        self._stream.flush()

    def finish(self) -> None:
        if self._enabled and self._last >= 0:
            self._stream.write("\r" + " " * 48 + "\r")
            self._stream.flush()


def _format_duration(seconds: float | None) -> str:
    if not seconds:
        return "?:??"
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


def _probe_version(argv: Sequence[str]) -> str | None:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() or None if completed.returncode == 0 else None


def _probe_writable(root: Path) -> bool:
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".tunebox-write-test"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError:
        return False
    return True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
