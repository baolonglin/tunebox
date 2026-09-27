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
from typing import TYPE_CHECKING, Sequence, TextIO

from . import __version__, ingest as ingest_module
from .config import SUPPORTED_FORMATS, Config
from .core.models import Progress
from .errors import PlayerError, TuneboxError
from .sources.ytdlp import YtDlpSource

if TYPE_CHECKING:  # the player adapter is imported lazily so `tunebox search` stays fast
    from .player.owntone import OwnTonePlayer


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

    serve = sub.add_parser(
        "serve",
        help="run the HTTP API and the ingest worker (needs TUNEBOX_TOKEN)",
    )
    serve.add_argument(
        "--host",
        help="address to bind (default $TUNEBOX_API_HOST or 127.0.0.1; never 0.0.0.0)",
    )
    serve.add_argument("--port", type=int, help="port to bind (default $TUNEBOX_API_PORT or 8080)")
    serve.add_argument(
        "--workers",
        type=int,
        help="concurrent ingests, 1 or 2 (default $TUNEBOX_WORKERS or 1)",
    )
    serve.set_defaults(func=_cmd_serve)

    _add_player_parser(sub)

    return parser


#: ``tunebox player <verb>`` — the whole transport surface, and the thing the Phase 3 acceptance
#: test is driven with. Phase 4 puts a web page in front of these; a terminal is enough to prove
#: that the *host* owns the session, which is what Phase 3 is about.
def _add_player_parser(sub: argparse._SubParsersAction) -> None:
    player = sub.add_parser(
        "player",
        help="drive the playback session on the host (OwnTone -> Chromecast -> soundbar)",
        description=(
            "Control the player daemon that owns the queue. Nothing here streams audio: the "
            "soundbar pulls it, so every command returns as soon as the daemon has accepted it."
        ),
    )
    player.add_argument(
        "--player-url",
        help="player daemon base URL (default $TUNEBOX_PLAYER_URL or http://127.0.0.1:3689)",
    )
    player.add_argument("--output", help="speaker name to act on (default $TUNEBOX_OUTPUT)")
    verbs = player.add_subparsers(dest="player_command", required=True)

    status = verbs.add_parser("status", help="transport state, volume, now playing, outputs")
    status.set_defaults(func=_cmd_player_status)

    outputs = verbs.add_parser("outputs", help="list the speakers the daemon can see")
    outputs.set_defaults(func=_cmd_player_outputs)

    select = verbs.add_parser("select", help="enable the configured speaker (or --output NAME)")
    select.add_argument("--volume", type=int, help="also set this output's volume (0-100)")
    select.set_defaults(func=_cmd_player_select)

    play = verbs.add_parser(
        "play",
        help="resume playback, or queue FILEs and start them",
        description=(
            "With no arguments this resumes whatever is loaded. With file paths it clears the "
            "queue, adds them and starts — which is the 'play this now' the acceptance test uses."
        ),
    )
    play.add_argument("files", nargs="*", type=Path, help="library files to play now")
    play.add_argument(
        "--add",
        action="store_true",
        help="append to the queue instead of replacing it, and do not start playback",
    )
    play.add_argument(
        "--wait",
        type=float,
        default=20.0,
        help="seconds to wait for a freshly ingested file to appear in the library (default 20)",
    )
    play.set_defaults(func=_cmd_player_play)

    for verb, handler, helptext in (
        ("pause", _cmd_player_pause, "pause playback"),
        ("stop", _cmd_player_stop, "stop playback and unload the track"),
        ("next", _cmd_player_next, "skip to the next queue item"),
        ("prev", _cmd_player_prev, "skip to the previous queue item"),
    ):
        node = verbs.add_parser(verb, help=helptext)
        node.set_defaults(func=handler)

    volume = verbs.add_parser("volume", help="set or change volume, e.g. 40, +5, -5")
    volume.add_argument("level", help="absolute 0-100, or a signed step such as +5")
    volume.set_defaults(func=_cmd_player_volume)

    queue = verbs.add_parser("queue", help="list the queue")
    queue.add_argument("--clear", action="store_true", help="empty the queue instead of listing it")
    queue.set_defaults(func=_cmd_player_queue)

    rescan = verbs.add_parser("rescan", help="ask the daemon to index new files under the music root")
    rescan.set_defaults(func=_cmd_player_rescan)


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
        if getattr(args, "player_url", None):
            overrides["player_url"] = args.player_url.strip()
        if getattr(args, "output", None):
            # Naming an output on the command line also clears any configured id: otherwise the id
            # would silently win and ``--output "Kitchen"`` would act on the living room.
            overrides["output_name"] = args.output.strip()
            overrides["output_id"] = None
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
    result = _rescan_after_cli_ingest(result, config)
    json.dump(result.as_dict(), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def _rescan_after_cli_ingest(result, config: Config):
    """The Phase 3 rescan hook, for the CLI path.

    The worker does this too (:meth:`tunebox.jobs.worker.Worker._rescan`); both call the same hook
    so that a track ingested from a terminal is as playable as one queued from a phone. Skipped for
    an already-present file, and it can only ever add a warning — a player daemon that is not
    running must not turn a successful download into a failure.
    """
    if result.already_present or not config.rescan_after_ingest:
        return result
    from .player.hooks import rescan_library

    warning = rescan_library(_player(config))
    if warning is None:
        return result
    return replace(result, warnings=(*result.warnings, warning))


def _cmd_doctor(args: argparse.Namespace, config: Config) -> int:
    """Check exactly the dependencies PLAN.md 0.3 says YouTube audio needs."""
    checks: list[tuple[str, bool, str]] = []
    version = _probe_version([*config.ytdlp, "--version"])
    checks.append(("yt-dlp", version is not None, version or "not runnable (required)"))

    # ffmpeg extracts the audio and ffprobe inspects it. Both are hard requirements.
    for binary in ("ffmpeg", "ffprobe"):
        path = shutil.which(binary)
        checks.append((binary, path is not None, path or "MISSING (required)"))

    # The JS runtime is also a hard requirement — without one, every media download fails with
    # HTTP 403 (PLAN.md 0.3) — but *which* one is not our business, and asserting "deno" would be
    # wrong on the 32-bit Raspberry Pi that has no deno build. So yt-dlp is asked what it found.
    runtime = _probe_js_runtimes(config)
    checks.append(
        (
            "JS runtime",
            runtime is not None,
            runtime or "none usable (required: downloads fail with 403). Install deno, "
            "or node 22+ / quickjs / bun — see TUNEBOX_JS_RUNTIMES",
        )
    )

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

    # Phase 3, and *advisory*: ingest is fully functional with no player installed, so a missing
    # OwnTone is information rather than a broken dependency. Reporting it as a failure would make
    # `doctor` exit non-zero on every Phase 1/2 host, which is how a useful check gets ignored.
    for name, note in _probe_player(config):
        print(f"[--] {name.ljust(width)}  {note}")

    return 0 if all(ok for _, ok, _ in checks) else 1


def _probe_player(config: Config) -> list[tuple[str, str]]:
    """Describe the player daemon and the configured speaker, never raising.

    "Never raising" has to include the shape of the answer, not just the transport: ``doctor`` is the
    command someone runs *because* something is wrong, and a traceback out of the diagnostic is the
    least useful possible outcome.
    """
    wanted = config.output_id or config.output_name
    try:
        player = _player(config)
        info = player.ping()
        version = info.get("version") if isinstance(info, dict) else None
    except TuneboxError as exc:
        return [("player", f"{config.player_base_url} — {exc}")]
    except Exception as exc:  # pragma: no cover - doctor must always print something
        return [("player", f"{config.player_base_url} — {type(exc).__name__}: {exc}")]

    rows = [("player", f"{config.player_base_url} — OwnTone {version or '?'}")]
    try:
        output = player.target_output()
    except TuneboxError as exc:
        rows.append(("output", f"{wanted!r} — {exc}"))
        return rows
    except Exception as exc:  # pragma: no cover - same reasoning as above
        rows.append(("output", f"{wanted!r} — {type(exc).__name__}: {exc}"))
        return rows
    rows.append(
        (
            "output",
            f"{output.name!r} ({output.type}, id {output.id}) — "
            f"{'selected' if output.selected else 'NOT selected'}"
            + (f", volume {output.volume}" if output.volume is not None else ""),
        )
    )
    return rows


def _cmd_serve(args: argparse.Namespace, config: Config) -> int:
    """Run the API and the worker in one process (PLAN.md Phase 2).

    One process, because the queue is a SQLite file and the worker is two threads waiting on
    subprocesses — a second daemon would buy nothing and add a way for the two halves to disagree
    about which database they are using.
    """
    import uvicorn  # imported here: `tunebox search` should not pay for FastAPI's import time

    from .api.app import create_app

    updates: dict[str, object] = {}
    if args.host:
        updates["api_host"] = args.host.strip()
    if args.port:
        updates["api_port"] = args.port
    if args.workers:
        updates["worker_concurrency"] = args.workers
    if updates:
        config = replace(config, **updates)

    config.validate_for_serve()  # fail before the socket exists, with a fixable message
    app = create_app(config)

    print(
        f"tunebox {__version__} serving on http://{config.api_host}:{config.api_port}\n"
        f"  library:  {config.music_root}\n"
        f"  queue:    {config.database_path}\n"
        f"  workers:  {config.worker_concurrency}\n"
        f"  player:   {config.player_base_url}\n"
        f"  output:   {config.output_id or config.output_name!r}"
        + (
            f" (re-selected every {config.output_check_interval:.0f}s)"
            if config.output_check_interval > 0
            else " (keeper disabled)"
        )
        + "\n"
        + (
            f"  remote:   http://{config.api_host}:{config.api_port}/ "
            "— open on the phone, paste the token once, add to home screen\n"
            if config.ui_enabled
            else "  remote:   disabled (TUNEBOX_UI=0)\n"
        )
        + "  auth:     X-Tunebox-Token header required on every route except the app shell",
        file=sys.stderr,
    )
    uvicorn.run(
        app,
        host=config.api_host,
        port=config.api_port,
        log_level="debug" if args.verbose else "info",
        # Access logs record which IP asked for what, which is the audit trail PLAN.md 3.2 wants
        # on a network that contains devices nobody is proud of.
        access_log=True,
    )
    return 0


# -------------------------------------------------------------- player commands
#
# PLAN.md Phase 3. These are thin by design: every one of them is "build the adapter, send one
# command, print what the daemon says". The interesting behaviour lives in
# :mod:`tunebox.player.owntone` and :mod:`tunebox.player.selection`, which is where it can be
# tested without argparse in the way.


def _player(config: Config) -> "OwnTonePlayer":
    from .player.owntone import OwnTonePlayer

    return OwnTonePlayer(config)


def _emit(payload: object) -> int:
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False, default=str)
    sys.stdout.write("\n")
    return 0


def _cmd_player_status(args: argparse.Namespace, config: Config) -> int:
    return _emit(_player(config).status().as_dict())


def _cmd_player_outputs(args: argparse.Namespace, config: Config) -> int:
    outputs = _player(config).outputs()
    if not outputs:
        print(
            "no outputs visible. A Chromecast speaker only appears while it is powered and on "
            "the network — check the soundbar, then try again",
            file=sys.stderr,
        )
    return _emit([output.as_dict() for output in outputs])


def _cmd_player_select(args: argparse.Namespace, config: Config) -> int:
    player = _player(config)
    output = player.target_output()
    volume = args.volume if args.volume is not None else config.output_volume
    player.select_output(output.id, volume=volume)
    # Read it back rather than reporting what we asked for: "selected" is the daemon's opinion, and
    # a Cast device that refuses the session is exactly the failure this command exists to catch.
    return _emit(player.find_output(output_id=output.id).as_dict())


def _cmd_player_play(args: argparse.Namespace, config: Config) -> int:
    player = _player(config)
    if not args.files:
        player.play()
        return _emit(player.status().as_dict())

    paths = [path.expanduser() for path in args.files]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise TuneboxError(f"not a file: {', '.join(missing)}")

    _ensure_indexed(player, paths, wait=args.wait)

    items = player.enqueue_paths(paths, clear=not args.add, start=not args.add)
    return _emit(
        {
            "queued": [item.as_dict() for item in items],
            "player": player.status().as_dict(),
        }
    )


def _ensure_indexed(player: "OwnTonePlayer", paths: Sequence[Path], *, wait: float) -> None:
    """Make sure every path is in the player's library, rescanning only if one is not.

    The order matters, and it is the opposite of the obvious one. Rescanning first looks safer but
    is actively worse: a scan deletes and re-inserts rows, so a track id read during one is rejected
    as ``Invalid uri`` when the queue-add arrives — measured on hardware, as a ``500`` that looked
    like a malformed request. Resolving first means the common case (playing something already in
    the library) touches no scan at all, and the rescan is reserved for the case it actually exists
    for: a file ingested seconds ago.
    """
    unknown: list[Path] = []
    for path in paths:
        try:
            player.resolve_track(path)
        except PlayerError:
            unknown.append(path)
    if not unknown:
        return

    player.rescan()
    # Wait for the scan to *finish*, not just to be accepted: ids are unstable while it runs.
    player.wait_for_scan(timeout=max(wait, 1.0))
    for path in unknown:
        player.resolve_track(path, timeout=wait)


def _cmd_player_pause(args: argparse.Namespace, config: Config) -> int:
    player = _player(config)
    player.pause()
    return _emit(player.status().as_dict())


def _cmd_player_stop(args: argparse.Namespace, config: Config) -> int:
    player = _player(config)
    player.stop()
    return _emit(player.status().as_dict())


def _cmd_player_next(args: argparse.Namespace, config: Config) -> int:
    player = _player(config)
    player.next_track()
    return _emit(player.status().as_dict())


def _cmd_player_prev(args: argparse.Namespace, config: Config) -> int:
    player = _player(config)
    player.previous_track()
    return _emit(player.status().as_dict())


def _cmd_player_volume(args: argparse.Namespace, config: Config) -> int:
    player = _player(config)
    level = args.level.strip()
    try:
        if level.startswith(("+", "-")):
            # A signed value is a step, so the CLI never has to read the level before changing it —
            # which also means two "+5" in a row do the right thing.
            player.change_volume(int(level))
        else:
            player.set_volume(int(level))
    except ValueError as exc:
        raise TuneboxError(f"volume must be 0-100 or a signed step like +5, got {level!r}") from exc
    return _emit(player.status().as_dict())


def _cmd_player_queue(args: argparse.Namespace, config: Config) -> int:
    player = _player(config)
    if args.clear:
        player.clear_queue()
    return _emit([item.as_dict() for item in player.queue()])


def _cmd_player_rescan(args: argparse.Namespace, config: Config) -> int:
    player = _player(config)
    player.rescan()
    return _emit(player.library_info())


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


#: A URL the generic extractor resolves **locally**, so the probe below costs no network request.
#: yt-dlp prints its debug banner before it touches an extractor, which is the only part we read.
_JS_PROBE_URL = "file:///dev/null"
_JS_RUNTIME_LINE = "JS runtimes:"


def _probe_js_runtimes(config: Config) -> str | None:
    """Ask yt-dlp which JavaScript runtime it would use, or ``None`` if it has none.

    yt-dlp already knows how to find deno, node, quickjs and bun, and which versions of each it
    considers usable. Re-implementing that here would mean two answers to one question, and the one
    printed by ``doctor`` would be the wrong one exactly when it mattered — on a host with, say,
    node 18 installed, which is present, on PATH, and useless.

    So this parses yt-dlp's own ``-v`` banner. It reports what will actually be used, including the
    version, and it keeps working when yt-dlp adds a runtime we have never heard of.
    """
    argv = [
        *config.ytdlp,
        "--ignore-config",
        "-v",
        "--simulate",
        *[arg for runtime in config.js_runtimes for arg in ("--js-runtimes", runtime)],
        _JS_PROBE_URL,
    ]
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    # The banner goes to stderr, but the return code is a failure (there is nothing at that URL to
    # download), so it is deliberately not checked.
    for line in (completed.stderr + completed.stdout).splitlines():
        _, separator, found = line.partition(_JS_RUNTIME_LINE)
        if not separator:
            continue
        names = found.strip()
        # "none" is what yt-dlp prints when nothing usable was found; an empty tail means the same.
        if not names or names.lower() == "none":
            return None
        return names
    return None


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
