"""The daily health check: does this system still work, and will it still work tomorrow?

PLAN.md 3.3 asks for three things and this is where they meet: *"one synthetic ingest per day as a
health check — this is how we learn extraction broke before a user does"*, *"structured logs,
retained"*, and *"disk-space alarm on the music volume"*.

The checks are ordered by how early they catch a problem, not by how interesting they are:

* **canary** — a real download (:mod:`tunebox.ops.canary`). The only check that can detect the
  failure this project actually expects: YouTube changes something and extraction stops working.
* **disk** — free space on the music volume. This one is *predictive*: a full disk fails every future
  ingest, and it is the failure that gives the most warning and is most often ignored.
* **library** — how many tracks exist. Not a pass/fail on its own; it is the number that makes a
  backup's "1 400 files" meaningful, and a sudden drop is the signature of a volume that did not
  mount (in which case the music root exists, is empty, and every ingest will "work").
* **backup** — how long ago the library was last copied somewhere else.
* **queue** — jobs that failed or are wedged, read out of the same SQLite the API uses.
* **player** — whether OwnTone is answering and the speaker is selected. Advisory: ingest works
  with no player installed (PLAN.md 3.1), so this never fails the run.
* **exposure** — the cheap half of :mod:`tunebox.ops.exposure` (configuration and the socket table).
  The router half is skipped here: it costs seconds of multicast waiting and its answer only changes
  when somebody reconfigures the router, so it belongs in a command a person runs.

An unmounted volume deserves a note of its own, because of how badly it fails silently. If the USB
SSD is not mounted, the mount point is usually still a perfectly writable directory on the *root*
disk — so ingest succeeds, the library appears empty, and the SD card slowly fills with music that
the backup does not know about. That is why "the music root exists" is not the check, and free space
plus track count are reported together.
"""

from __future__ import annotations

import logging
import platform as platform_module
import shutil
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .. import __version__
from ..config import Config
from ..errors import TuneboxError
from . import exposure as exposure_module
from .backup import backup_status
from .canary import CanaryResult, run_canary
from .report import FAIL, OK, UNKNOWN, WARN, Finding, format_findings, worst
from .state import OpsState, append_jsonl

log = logging.getLogger(__name__)

#: A library that had tracks and now has none is not an empty library, it is a missing disk.
#: See the module docstring: this is the failure mode that hides behind a writable mount point.
GIB = 1024**3


@dataclass(frozen=True)
class HealthReport:
    findings: tuple[Finding, ...]
    canary: CanaryResult | None = None
    at: float = 0.0
    host: str = ""
    version: str = ""

    @property
    def status(self) -> str:
        return worst(finding.status for finding in self.findings)

    @property
    def ok(self) -> bool:
        return not any(finding.status == FAIL for finding in self.findings)

    @property
    def failures(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.status == FAIL)

    @property
    def subject(self) -> str:
        """One line, suitable for a notification title or an email subject."""
        if self.ok:
            return f"tunebox ok on {self.host or 'this host'}"
        names = ", ".join(finding.name for finding in self.failures)
        return f"tunebox PROBLEM on {self.host or 'this host'}: {names}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "host": self.host,
            "version": self.version,
            "status": self.status,
            "ok": self.ok,
            "subject": self.subject,
            "findings": [finding.as_dict() for finding in self.findings],
            "canary": self.canary.as_dict() if self.canary else None,
        }

    def render(self) -> str:
        return format_findings(self.findings)


def check_health(
    config: Config,
    *,
    canary: Callable[[Config], CanaryResult] | None = None,
    run_canary_check: bool = True,
    check_exposure: bool = True,
    player: Any | None = None,
    now: Callable[[], float] = time.time,
    listener_run: exposure_module.CommandRunner = exposure_module.DEFAULT_RUNNER,
) -> HealthReport:
    """Run every check and return one report. Never raises for a failing check."""
    findings: list[Finding] = []
    canary_result: CanaryResult | None = None

    if run_canary_check:
        canary_result = (canary or (lambda cfg: run_canary(cfg)))(config)
        findings.append(_canary_finding(canary_result))
    else:
        findings.append(
            Finding(
                name="canary",
                status=UNKNOWN,
                detail="skipped (--no-canary)",
                fix="the canary is the only check that can tell you extraction broke; skip it only "
                "when you are deliberately offline",
            )
        )

    findings.append(_disk_finding(config))
    library = _library_finding(config)
    findings.append(library)
    findings.append(_backup_finding(config, now=now))
    findings.append(_queue_finding(config))
    findings.append(_player_finding(config, player))

    if check_exposure:
        report = exposure_module.audit(
            config, check_router=False, run=listener_run
        )
        findings.extend(
            finding for finding in report.findings if finding.name != "router"
        )

    return HealthReport(
        findings=tuple(findings),
        canary=canary_result,
        at=now(),
        host=_hostname(),
        version=__version__,
    )


def _canary_finding(result: CanaryResult) -> Finding:
    if result.ok:
        return Finding(
            name="canary",
            status=OK,
            detail=result.summary,
            data=result.as_dict(),
        )
    return Finding(
        name="canary",
        status=FAIL,
        detail=result.summary,
        fix=(
            "extraction is broken. `tunebox update` pulls a newer yt-dlp and canaries it; if the "
            "canary failed at 'fetch' rather than 'inspect', check the JS runtime first — "
            "`tunebox doctor` reports which one yt-dlp found, and without one every media "
            "download answers 403 (PLAN.md 0.3)."
        ),
        data=result.as_dict(),
    )


def _disk_finding(config: Config) -> Finding:
    root = config.music_root
    try:
        usage = shutil.disk_usage(root if root.exists() else root.parent)
    except OSError as exc:
        return Finding(
            name="disk",
            status=UNKNOWN,
            detail=f"cannot read free space for {root}: {exc}",
        )
    free_gb = usage.free / GIB
    percent = usage.free / usage.total * 100 if usage.total else 0.0
    data = {
        "path": str(root),
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "free_gb": round(free_gb, 2),
        "free_percent": round(percent, 1),
    }
    detail = f"{free_gb:.1f} GiB free ({percent:.0f}%) on the volume holding {root}"
    if free_gb < config.min_free_gb:
        return Finding(
            name="disk",
            status=FAIL,
            detail=detail + f" — below the {config.min_free_gb:.1f} GiB floor",
            fix="free space on the music volume, or move the library to a bigger disk. Every "
            "future ingest fails when this runs out, and a partially written file is the one "
            "thing a music scanner will happily index.",
            data=data,
        )
    if free_gb < config.min_free_gb * 2:
        return Finding(
            name="disk",
            status=WARN,
            detail=detail + " — within a factor of two of the floor",
            data=data,
        )
    return Finding(name="disk", status=OK, detail=detail, data=data)


def _library_finding(config: Config) -> Finding:
    from ..library import list_tracks

    root = config.music_root
    if not root.is_dir():
        return Finding(
            name="library",
            status=FAIL,
            detail=f"the music root does not exist: {root}",
            fix="if this is an external disk, it is not mounted. Nothing will be ingested, and "
            "anything that is will land on the wrong disk once the mount point is writable.",
            data={"path": str(root)},
        )
    page = list_tracks(root, limit=0, max_files=config.library_max_files)
    data = {"path": str(root), "tracks": page.total, "truncated": page.truncated}
    if page.total == 0:
        return Finding(
            name="library",
            status=WARN,
            detail=f"{root} contains no music files",
            fix="normal on a fresh install. On a host that had a library, this is what an "
            "unmounted external disk looks like: the mount point is still a writable directory, "
            "so nothing else complains.",
            data=data,
        )
    return Finding(
        name="library",
        status=OK,
        detail=f"{page.total} track(s) under {root}"
        + (" (walk truncated)" if page.truncated else ""),
        data=data,
    )


def _backup_finding(config: Config, *, now: Callable[[], float]) -> Finding:
    status = backup_status(config, now=now)
    data = status.as_dict()
    if not status.configured:
        if config.backup_optional:
            return Finding(
                name="backup",
                status=UNKNOWN,
                detail="no backup, by configuration (TUNEBOX_BACKUP_DEST=none)",
                fix="the library is the only irreplaceable state here: the audio can be downloaded "
                "again, the tags and the layout cannot. This says you know.",
                data=data,
            )
        return Finding(
            name="backup",
            status=FAIL,
            detail="no backup destination is configured, so the library has no copy anywhere",
            fix="set TUNEBOX_BACKUP_DEST to a path on another disk (or pi@nas:/srv/music). The "
            "library is the only irreplaceable state in this system: tracks can be downloaded "
            "again, the tags and the layout cannot. If that is a deliberate choice, say so with "
            "TUNEBOX_BACKUP_DEST=none and this becomes a note instead of a failure.",
            data=data,
        )
    if status.at is None:
        return Finding(
            name="backup",
            status=FAIL,
            detail=f"a destination is configured ({status.dest}) but no backup has ever run",
            fix="run `tunebox backup` once by hand to check it works; the nightly job does the rest",
            data=data,
        )
    age = status.age_hours or 0.0
    detail = f"last backup {age:.1f}h ago to {status.dest} ({status.files or 0} files)"
    if status.ok is False:
        return Finding(
            name="backup",
            status=FAIL,
            detail=detail + " — and it did not complete",
            fix="run `tunebox backup` by hand to see rsync's error",
            data=data,
        )
    if status.stale:
        return Finding(
            name="backup",
            status=FAIL,
            detail=detail + f" — older than the {config.backup_max_age_hours:.0f}h limit",
            fix="the scheduled maintenance job is not running, or the destination is unreachable "
            "(an external disk that is not plugged in looks exactly like this)",
            data=data,
        )
    return Finding(name="backup", status=OK, detail=detail, data=data)


def _queue_finding(config: Config) -> Finding:
    database = config.database_path
    if not database.exists():
        return Finding(
            name="queue",
            status=OK,
            detail="no queue database yet (nothing has been ingested on this host)",
            data={"path": str(database)},
        )
    try:
        from ..jobs.store import JobStore

        store = JobStore(database)
        try:
            counts = store.counts()
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001 - a health check never dies of its own checks
        return Finding(
            name="queue",
            status=WARN,
            detail=f"cannot read the queue at {database}: {type(exc).__name__}: {exc}",
        )
    failed = int(counts.get("failed", 0))
    queued = int(counts.get("queued", 0))
    running = int(counts.get("running", 0))
    detail = ", ".join(f"{state}={count}" for state, count in sorted(counts.items()) if count)
    data = {"counts": counts}
    if failed:
        return Finding(
            name="queue",
            status=WARN,
            detail=f"{failed} failed job(s) in the queue ({detail or 'no other state'})",
            fix="`tunebox` has no retry-all command by design — look at why they failed first: "
            "GET /jobs?state=failed, or the log. A single bad URL is not a system problem.",
            data=data,
        )
    return Finding(
        name="queue",
        status=OK,
        detail=detail or f"idle (queued={queued}, running={running})",
        data=data,
    )


def _player_finding(config: Config, player: Any | None) -> Finding:
    """Advisory only: PLAN.md 3.1 says ingest works with no player installed, so a missing OwnTone
    must never fail the nightly job — that would train somebody to ignore its output."""
    try:
        if player is None:
            from ..player.owntone import OwnTonePlayer

            player = OwnTonePlayer(config)
        info = player.ping()
        version = info.get("version") if isinstance(info, dict) else None
    except TuneboxError as exc:
        return Finding(
            name="player",
            status=WARN,
            detail=f"{config.player_base_url} is not answering: {exc}",
            fix="playback needs OwnTone; ingest does not. Start it, or ignore this on an "
            "ingest-only host.",
        )
    except Exception as exc:  # noqa: BLE001 - same reasoning as above
        return Finding(
            name="player",
            status=WARN,
            detail=f"{config.player_base_url}: {type(exc).__name__}: {exc}",
        )

    try:
        output = player.target_output()
    except TuneboxError as exc:
        return Finding(
            name="player",
            status=WARN,
            detail=f"OwnTone {version or '?'} is up, but {config.output_id or config.output_name!r} "
            f"is not there: {exc}",
            fix="a Chromecast speaker disappears from mDNS when it is powered off; OutputKeeper "
            "re-selects it when it comes back",
        )
    return Finding(
        name="player",
        status=OK,
        detail=f"OwnTone {version or '?'}, output {output.name!r} "
        f"{'selected' if output.selected else 'present but not selected'}",
        data={"output": output.name, "selected": output.selected},
    )


def _hostname() -> str:
    try:
        return platform_module.node() or socket.gethostname()
    except OSError:  # pragma: no cover
        return ""


# ------------------------------------------------------------------------- the log


def record(config: Config, report: HealthReport, *, extra: dict[str, Any] | None = None) -> None:
    """Append one line to the health log and remember the status for the alerter.

    The log line is the whole report, not a summary: it is the only record of what the system looked
    like on a night nobody was watching, and a summary always turns out to have dropped the field
    that mattered.
    """
    payload = report.as_dict()
    if extra:
        payload.update(extra)
    append_jsonl(config.health_log_path, payload, max_bytes=config.health_log_max_bytes)
    OpsState(config.ops_state_path).save_section(
        "health",
        {
            "at": report.at,
            "status": report.status,
            "ok": report.ok,
            "failures": [finding.name for finding in report.failures],
        },
    )


def history(config: Config, *, limit: int = 20) -> list[dict[str, Any]]:
    from .state import read_jsonl

    return read_jsonl(config.health_log_path, limit=limit)


def combined(findings: Sequence[Finding], *extra: Sequence[Finding]) -> tuple[Finding, ...]:
    """Concatenate finding lists, keeping order. Used by ``tunebox maintain``."""
    out: list[Finding] = list(findings)
    for group in extra:
        out.extend(group)
    return tuple(out)


def log_path_note(config: Config) -> str:
    return f"health log: {config.health_log_path}"


def state_paths(config: Config) -> dict[str, Path]:
    """Every file Phase 5 writes, so ``--json`` output can say where its state lives."""
    return {
        "ops_state": config.ops_state_path,
        "health_log": config.health_log_path,
        "database": config.database_path,
    }
