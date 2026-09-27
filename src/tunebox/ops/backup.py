"""Backing up the one irreplaceable thing, and getting it back.

PLAN.md's risk table says it plainly: *"SD-card or SSD failure loses the library. Tracks are
re-ingestable but tags and playlists are not."* That sentence is the whole specification. Audio can
be downloaded again; the hours of ``--parse-metadata`` corrections, the artist names that had to be
worked out from a CJK title, and the directory layout somebody rearranged by hand cannot. So the
backup covers the music tree and the small amount of state next to it, and nothing else — the venv,
the config and the code are all reproducible from ``git clone && ./deploy/install.sh``.

Four decisions, each of which is about a way backups actually fail:

* **rsync, not a copy.** It is on macOS and on every Debian, it is incremental, and it is the one
  tool whose remote form (``user@host:/path``) needs no extra code. The destination is deliberately a
  plain readable tree rather than an archive format: the day this is needed, the recovery must not
  depend on this program still working.

* **Deleting is opt-in** (:attr:`tunebox.config.Config.backup_prune`). The accident a home backup
  most often has to survive is somebody deleting an album, and ``rsync --delete`` propagates exactly
  that, quietly, within a day.

* **The SQLite queue is snapshotted, never rsynced live.** Copying an open database file byte-wise
  is the classic way to produce a backup that restores into a corrupt database, because the file and
  its write-ahead log disagree. So :func:`snapshot_database` uses SQLite's own backup API to write a
  consistent copy, and the live files are excluded from the transfer.

* **The transfer is verified by asking rsync again.** A second pass with ``--dry-run
  --itemize-changes`` that finds nothing left to send is a real end-to-end check that every file
  arrived; "rsync exited 0" is not, because rsync exits 0 after skipping files it could not read.

And the part most backup stories leave out: :func:`restore` exists, is tested, and is a **dry run by
default**. A restore path nobody has ever executed is a hope, not a plan.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from ..config import SCRATCH_DIRNAME, Config
from ..errors import TuneboxError
from .state import OpsState

log = logging.getLogger(__name__)

#: ``(argv) -> (returncode, output)``, injected in tests. The real one is :func:`_run`.
Runner = Callable[[Sequence[str]], "tuple[int, str]"]

#: Never transferred. Three groups, for three different reasons:
#:
#: * the scratch directory holds partial downloads;
#: * the live database files are replaced by a consistent snapshot (see the module docstring);
#: * the ops state and the health log describe *the backup itself* and change on every run, so
#:   including them would mean an incremental backup was never a no-op and "nothing left to send"
#:   could never be true — which is the property :func:`_verify` depends on. They are also the only
#:   things here that a dead disk does not take anything irreplaceable with.
EXCLUDES = (
    f"{SCRATCH_DIRNAME}/",
    "jobs.db",
    "jobs.db-wal",
    "jobs.db-shm",
    "jobs.db.lock",
    "ops.json",
    "health.jsonl",
    "health.jsonl.*",
    ".DS_Store",
    "@eaDir/",  # a Synology NAS destination sprouts these; never send them back
)

#: The consistent copy of the queue database that *is* transferred, written next to the live one.
SNAPSHOT_NAME = "jobs-snapshot.db"

#: How many files moved, as rsync reports it. Two spellings, because the two platforms this project
#: targets ship different implementations: rsync 3.x (Debian, so the Raspberry Pi) prints "Number of
#: regular files transferred", while macOS now ships openrsync, which announces itself as "rsync
#: version 2.6.9 compatible" and prints "Number of files transferred". Matching only the modern
#: wording silently reported ``None`` for every backup on the development host.
_TRANSFERRED = re.compile(r"Number of (?:regular )?files transferred:\s*([\d,]+)")

_ITEMIZED_FILE = re.compile(r"^[<>ch.][fdLDS]")


@dataclass(frozen=True)
class BackupResult:
    ok: bool
    #: ``"backup"`` or ``"restore"``. Same machinery, opposite direction, and the field exists so a
    #: health log can be read without guessing which way the bytes went.
    direction: str
    source: str
    dest: str
    files: int = 0
    bytes: int = 0
    transferred: int | None = None
    pruned: bool = False
    dry_run: bool = False
    #: ``None`` when verification was not asked for; ``True``/``False`` when it was.
    verified: bool | None = None
    elapsed: float = 0.0
    warnings: tuple[str, ...] = ()
    error: str | None = None
    #: Itemised lines from a dry run, so ``tunebox restore`` can show exactly what it would write.
    changes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "direction": self.direction,
            "source": self.source,
            "dest": self.dest,
            "files": self.files,
            "bytes": self.bytes,
            "transferred": self.transferred,
            "pruned": self.pruned,
            "dry_run": self.dry_run,
            "verified": self.verified,
            "elapsed": round(self.elapsed, 2),
            "warnings": list(self.warnings),
            "changes": list(self.changes),
            "error": self.error,
        }

    @property
    def summary(self) -> str:
        if not self.ok:
            return f"{self.direction} failed: {self.error or 'unknown error'}"
        what = f"{self.files} files, {_human(self.bytes)}"
        if self.dry_run:
            return f"{self.direction} dry run: {len(self.changes)} change(s) pending for {what}"
        verified = {True: ", verified", False: ", VERIFICATION FAILED", None: ""}[self.verified]
        moved = f", {self.transferred} transferred" if self.transferred is not None else ""
        return f"{self.direction} ok: {what} at {self.dest}{moved}{verified}"


def _run(argv: Sequence[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, never a shell string
            list(argv),
            capture_output=True,
            timeout=6 * 3600,
            check=False,
            # Not ``text=True``: that decodes strictly, and rsync does not promise UTF-8 output. It
            # escapes or passes through raw bytes from filenames, and this library has CJK artist
            # directories in it — measured, as a UnicodeDecodeError that crashed the backup on a
            # tree that rsync itself had copied perfectly.
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return 127, f"{argv[0]} is not installed"
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


# ------------------------------------------------------------------------ the backup


def run_backup(
    config: Config,
    *,
    dest: str | None = None,
    prune: bool | None = None,
    verify: bool = True,
    dry_run: bool = False,
    runner: Runner = _run,
    now: Callable[[], float] = time.time,
) -> BackupResult:
    """Mirror the library to ``dest``. Returns a result; raises only for a misconfiguration."""
    destination = (dest or config.backup_dest or "").strip()
    source = config.music_root
    started = time.monotonic()
    if not destination:
        raise TuneboxError(
            "no backup destination. Set TUNEBOX_BACKUP_DEST to a path on another disk (or "
            "user@host:/path for a remote one). The library is the only irreplaceable state in "
            "this system — see PLAN.md's risk table."
        )
    if not source.is_dir():
        raise TuneboxError(f"the music root does not exist: {source}")

    warnings = list(_destination_warnings(source, destination))
    prune_enabled = config.backup_prune if prune is None else prune

    snapshot_note = snapshot_database(config)
    if snapshot_note:
        warnings.append(snapshot_note)

    files, total_bytes = _measure(source)
    argv = _rsync_argv(source_spec=f"{source}/", dest_spec=_with_slash(destination),
                       prune=prune_enabled, dry_run=dry_run)
    returncode, output = runner(argv)
    if returncode != 0:
        return BackupResult(
            ok=False,
            direction="backup",
            source=str(source),
            dest=destination,
            files=files,
            bytes=total_bytes,
            pruned=prune_enabled,
            dry_run=dry_run,
            elapsed=time.monotonic() - started,
            warnings=tuple(warnings),
            error=_rsync_error(returncode, output),
        )

    transferred = _transferred_count(output)
    if dry_run:
        return BackupResult(
            ok=True,
            direction="backup",
            source=str(source),
            dest=destination,
            files=files,
            bytes=total_bytes,
            transferred=transferred,
            pruned=prune_enabled,
            dry_run=True,
            elapsed=time.monotonic() - started,
            warnings=tuple(warnings),
            changes=_itemized(output),
        )

    verified: bool | None = None
    if verify:
        verified, pending = _verify(source, destination, prune_enabled, runner)
        if not verified:
            warnings.append(
                "verification found files still pending after the transfer: "
                + ", ".join(pending[:10])
            )

    result = BackupResult(
        ok=verified is not False,
        direction="backup",
        source=str(source),
        dest=destination,
        files=files,
        bytes=total_bytes,
        transferred=transferred,
        pruned=prune_enabled,
        verified=verified,
        elapsed=time.monotonic() - started,
        warnings=tuple(warnings),
        error=None if verified is not False else "the backup is incomplete",
    )
    OpsState(config.ops_state_path).save_section(
        "backup",
        {
            "at": now(),
            "dest": destination,
            "files": files,
            "bytes": total_bytes,
            "ok": result.ok,
            "verified": verified,
        },
    )
    return result


def _rsync_argv(
    *, source_spec: str, dest_spec: str, prune: bool, dry_run: bool, itemize: bool = False
) -> list[str]:
    argv = [
        "rsync",
        # -a is archive: recurse and preserve times, permissions and symlinks. Times matter more
        # than they look — they are what makes the *next* run incremental.
        "-a",
        "--partial",
        "--human-readable",
        "--stats",
    ]
    if dry_run or itemize:
        argv += ["--dry-run", "--itemize-changes"]
    if prune:
        argv.append("--delete")
    argv += [f"--exclude={pattern}" for pattern in EXCLUDES]
    argv += [source_spec, dest_spec]
    return argv


def _verify(
    source: Path, destination: str, prune: bool, runner: Runner
) -> tuple[bool, list[str]]:
    """Ask rsync whether anything is still missing at the destination.

    This is the check that makes the backup a fact rather than an assumption. rsync exits 0 in cases
    that are not success — notably when it could not read a file (exit 23 is *sometimes* used, but a
    vanished file during transfer is not always fatal) — so "did it exit 0" is a weaker statement
    than "a second pass has nothing left to send".
    """
    argv = _rsync_argv(
        source_spec=f"{source}/",
        dest_spec=_with_slash(destination),
        prune=prune,
        dry_run=True,
        itemize=True,
    )
    returncode, output = runner(argv)
    if returncode != 0:
        return False, [f"the verification pass itself failed: {_rsync_error(returncode, output)}"]
    pending = [line for line in _itemized(output) if _ITEMIZED_FILE.match(line)]
    return not pending, pending


def _itemized(output: str) -> tuple[str, ...]:
    lines: list[str] = []
    for line in output.splitlines():
        text = line.rstrip()
        if not text or text.startswith(("sending ", "sent ", "total ", "Number of", "Total ",
                                        "Literal data", "Matched data", "File list",
                                        "created directory", "building file list", "rsync:",
                                        "cannot delete")):
            continue
        if _ITEMIZED_FILE.match(text) or text.startswith("*deleting"):
            lines.append(text)
    return tuple(lines)


def _transferred_count(output: str) -> int | None:
    match = _TRANSFERRED.search(output)
    if match is None:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:  # pragma: no cover - the regex only matches digits and commas
        return None


def _rsync_error(returncode: int, output: str) -> str:
    if returncode == 127:
        return (
            "rsync is not installed. macOS ships it; on Debian: sudo apt-get install rsync"
        )
    tail = (output or "").strip()[-600:]
    return f"rsync exited {returncode}: {tail or 'no output'}"


def _destination_warnings(source: Path, destination: str) -> list[str]:
    """Refuse the impossible, warn about the merely useless."""
    if not is_local_path(destination):
        return []
    target = Path(destination).expanduser()
    try:
        resolved_source = source.resolve()
        resolved_target = target.resolve()
    except OSError:  # pragma: no cover - an unresolvable path is caught by rsync itself
        return []

    if resolved_target == resolved_source:
        raise TuneboxError(f"the backup destination is the music root itself: {destination}")
    if resolved_source in resolved_target.parents:
        raise TuneboxError(
            f"the backup destination {destination} is inside the music root. That would copy the "
            "library into itself, and the copy would then be backed up too."
        )
    if resolved_target in resolved_source.parents:
        # The catastrophic one, and the one that looks harmless. ``--dest ~/Music`` for a library at
        # ``~/Music/tunebox`` is a *plausible typo*, and with --prune rsync then deletes everything
        # in the destination that the source does not have — which is every other file in ~/Music,
        # and then the library itself, because it is inside the tree being emptied. Measured: it
        # removes the lot. A backup command must not have a spelling that destroys data.
        raise TuneboxError(
            f"the backup destination {destination} *contains* the music root {source}. Backing a "
            "directory up into one of its own parents would mirror the library over the top of "
            "everything else there — and with --prune it would delete all of it. Pick a "
            "destination on another disk."
        )

    warnings: list[str] = []
    if not target.exists():
        warnings.append(f"{target} does not exist yet; rsync will create it")
    if _same_device(resolved_source, resolved_target):
        # The risk row this whole module answers is "SSD failure loses the library". A copy on the
        # same SSD does protect against an accidental delete, and does nothing at all about the SSD.
        warnings.append(
            f"{target} is on the same filesystem as the library, so this backup will not survive "
            "that disk failing — which is the failure PLAN.md's risk table is about. It still "
            "protects against deleting something by accident."
        )
    return warnings


def _same_device(left: Path, right: Path) -> bool:
    probe = right
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return left.stat().st_dev == probe.stat().st_dev
    except OSError:  # pragma: no cover
        return False


def is_local_path(destination: str) -> bool:
    """Is this a plain filesystem path rather than an rsync remote spec?

    rsync treats anything with a colon before the first slash as ``host:path``, so that is the rule
    used here rather than a guess: a destination of ``/Volumes/Backup/tunebox`` is local and
    ``pi@nas:/srv/music`` is not.
    """
    text = destination.strip()
    if text.startswith("rsync://"):
        return False
    head = text.split("/", 1)[0]
    return ":" not in head


def _with_slash(destination: str) -> str:
    """rsync's trailing-slash semantics apply to the *source*; the destination is given one for
    readability and because a destination without one is created as a file when the source is a
    single file. Harmless here, and it keeps the two specs symmetrical."""
    return destination if destination.endswith("/") else destination + "/"


#: The excluded *file* names, for :func:`_measure`. Derived from :data:`EXCLUDES` so the count this
#: reports is a count of what actually gets transferred — a "files protected" number that included
#: files deliberately left behind would be quietly wrong in the direction that matters.
_EXCLUDED_NAMES = frozenset(
    name for name in EXCLUDES if not name.endswith("/") and "*" not in name
)


def _measure(root: Path) -> tuple[int, int]:
    """How many files and bytes the library holds, skipping what the backup excludes.

    Reported rather than parsed out of rsync's statistics because it is the number that answers the
    question somebody actually has: "how much of my library is protected?"
    """
    files = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, onerror=None):
        dirnames[:] = [name for name in dirnames if name != SCRATCH_DIRNAME]
        for name in filenames:
            if name in _EXCLUDED_NAMES or name.startswith("health.jsonl."):
                continue
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                continue
            files += 1
    return files, total


def snapshot_database(config: Config) -> str | None:
    """Write a consistent copy of the queue database next to the live one.

    Returns a warning string if it could not be done, or ``None`` on success (including the case
    where there is no database yet, which is normal on a host that has never ingested anything).

    SQLite's own backup API rather than ``shutil.copy``: in WAL mode the committed state is split
    across ``jobs.db`` and ``jobs.db-wal``, and copying the two separately while a worker is writing
    produces a file that restores into a corrupt database. This is the same reason the live files are
    in :data:`EXCLUDES`.
    """
    live = config.database_path
    if not live.exists():
        return None
    snapshot = live.parent / SNAPSHOT_NAME
    try:
        # A read-only URI connection so that taking a backup can never create or migrate anything.
        source = sqlite3.connect(f"file:{live}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error as exc:
        return f"could not open the queue database for snapshotting: {exc}"
    temporary = snapshot.with_suffix(".partial")
    try:
        target = sqlite3.connect(str(temporary))
        try:
            source.backup(target)
        finally:
            target.close()
        os.replace(temporary, snapshot)
    except (sqlite3.Error, OSError) as exc:
        Path(temporary).unlink(missing_ok=True)
        return f"could not snapshot the queue database: {exc}"
    finally:
        source.close()
    return None


# ----------------------------------------------------------------------- the restore


def restore(
    config: Config,
    *,
    source: str | None = None,
    confirm: bool = False,
    runner: Runner = _run,
) -> BackupResult:
    """Copy a backup back over the music root. **A dry run unless ``confirm`` is true.**

    Dry-run-by-default is the point rather than caution for its own sake. A restore writes over the
    library, and the two situations it gets used in are "the disk died" (where the music root is
    empty and anything is an improvement) and "I deleted something I should not have" (where a
    careless restore can overwrite good files with older ones). Showing the itemised list first costs
    one extra command and makes the second case safe.

    ``--delete`` is never passed in this direction, under any flag: a restore that removed files the
    backup does not know about would turn "get my album back" into "lose everything ingested since
    the last backup".
    """
    origin = (source or config.backup_dest or "").strip()
    if not origin:
        raise TuneboxError(
            "no backup to restore from. Pass --from PATH, or set TUNEBOX_BACKUP_DEST."
        )
    target = config.music_root
    started = time.monotonic()
    if is_local_path(origin) and not Path(origin).expanduser().is_dir():
        raise TuneboxError(f"not a directory: {origin}")

    target.mkdir(parents=True, exist_ok=True)
    argv = _rsync_argv(
        source_spec=_with_slash(origin),
        dest_spec=f"{target}/",
        prune=False,
        dry_run=not confirm,
        itemize=not confirm,
    )
    returncode, output = runner(argv)
    files, total_bytes = _measure(target)
    if returncode != 0:
        return BackupResult(
            ok=False,
            direction="restore",
            source=origin,
            dest=str(target),
            files=files,
            bytes=total_bytes,
            dry_run=not confirm,
            elapsed=time.monotonic() - started,
            error=_rsync_error(returncode, output),
        )
    return BackupResult(
        ok=True,
        direction="restore",
        source=origin,
        dest=str(target),
        files=files,
        bytes=total_bytes,
        transferred=_transferred_count(output),
        dry_run=not confirm,
        elapsed=time.monotonic() - started,
        changes=_itemized(output),
        warnings=(
            ()
            if confirm
            else (
                "this was a dry run: nothing was written. Re-run with --yes to restore for real.",
            )
        ),
    )


# ------------------------------------------------------------------------- the status


@dataclass(frozen=True)
class BackupStatus:
    configured: bool
    dest: str | None
    at: float | None
    age_hours: float | None
    stale: bool
    files: int | None = None
    bytes: int | None = None
    ok: bool | None = None
    verified: bool | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "dest": self.dest,
            "at": self.at,
            "age_hours": None if self.age_hours is None else round(self.age_hours, 2),
            "stale": self.stale,
            "files": self.files,
            "bytes": self.bytes,
            "ok": self.ok,
            "verified": self.verified,
            "warnings": list(self.warnings),
        }


def backup_status(config: Config, *, now: Callable[[], float] = time.time) -> BackupStatus:
    """What the last backup did and whether it was long enough ago to worry about."""
    section = OpsState(config.ops_state_path).section("backup")
    configured = bool((config.backup_dest or "").strip())
    at = section.get("at")
    at_value = float(at) if isinstance(at, (int, float)) else None
    age = (now() - at_value) / 3600.0 if at_value else None
    stale = configured and (age is None or age > config.backup_max_age_hours)
    return BackupStatus(
        configured=configured,
        dest=section.get("dest") or config.backup_dest,
        at=at_value,
        age_hours=age,
        stale=stale,
        files=section.get("files"),
        bytes=section.get("bytes"),
        ok=section.get("ok"),
        verified=section.get("verified"),
    )


def restore_command(config: Config) -> str:
    """The command to type when the disk has died. Printed by ``tunebox backup --status``.

    Written down in the tool rather than only in the README because of when it is needed: the
    library is gone, somebody is anxious, and the README is in a git checkout that may be on the
    disk that just failed.
    """
    destination = (config.backup_dest or "/path/to/backup").strip()
    return f"tunebox restore --from {destination}   # add --yes once the dry run looks right"


def _human(size: int | None) -> str:
    value = float(size or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"  # pragma: no cover - the loop always returns


def have_rsync() -> bool:
    return shutil.which("rsync") is not None
