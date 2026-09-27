"""The nightly job: update, check, back up, alert once.

PLAN.md 3.3 and Phase 5 list four scheduled activities, and this composes them into **one** command
with **one** timer, for reasons that are about operations rather than tidiness:

* three timers produce three logs, three ways to be half-installed, and three alerts for what is
  really one bad night;
* the order matters and a scheduler cannot express it. The update has to run *before* the health
  check, or the day's canary tests yesterday's yt-dlp; the backup has to run *after* both, so that
  what gets copied is a library whose ingest path is known to work.

Ordering aside, the steps are deliberately independent: each is wrapped, and a step that throws is
turned into a failed finding rather than being allowed to skip the ones after it. A backup that does
not happen because the updater crashed would be the worst possible coupling — the backup is the step
that protects the only irreplaceable thing in the system.

The alert is sent once, at the end, with every finding in it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from ..config import Config
from ..errors import TuneboxError
from . import alerts as alerts_module
from .backup import BackupResult, run_backup
from .health import HealthReport, check_health, record
from .report import FAIL, OK, UNKNOWN, WARN, Finding, format_findings, worst
from .update import UpdateResult, update_ytdlp

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MaintenanceResult:
    findings: tuple[Finding, ...]
    health: HealthReport | None = None
    update: UpdateResult | None = None
    backup: BackupResult | None = None
    alert: alerts_module.AlertResult | None = None
    elapsed: float = 0.0

    @property
    def status(self) -> str:
        return worst(finding.status for finding in self.findings)

    @property
    def ok(self) -> bool:
        return not any(finding.status == FAIL for finding in self.findings)

    @property
    def subject(self) -> str:
        if self.ok:
            return self.health.subject if self.health else "tunebox ok"
        names = ", ".join(
            finding.name for finding in self.findings if finding.status == FAIL
        )
        host = self.health.host if self.health else ""
        return f"tunebox PROBLEM on {host or 'this host'}: {names}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "subject": self.subject,
            "elapsed": round(self.elapsed, 2),
            "findings": [finding.as_dict() for finding in self.findings],
            "update": self.update.as_dict() if self.update else None,
            "backup": self.backup.as_dict() if self.backup else None,
            "health": self.health.as_dict() if self.health else None,
            "alert": self.alert.as_dict() if self.alert else None,
        }

    def render(self) -> str:
        return format_findings(self.findings)


def maintain(
    config: Config,
    *,
    do_update: bool | None = None,
    do_backup: bool = True,
    do_canary: bool = True,
    alert: bool = True,
    now: Callable[[], float] = time.time,
    updater: Callable[[Config], UpdateResult] | None = None,
    backer: Callable[[Config], BackupResult] | None = None,
    health: Callable[[Config], HealthReport] | None = None,
) -> MaintenanceResult:
    """Run the whole nightly sequence and return everything it learned."""
    started = time.monotonic()
    findings: list[Finding] = []
    update_result: UpdateResult | None = None
    backup_result: BackupResult | None = None

    update_enabled = config.auto_update if do_update is None else do_update
    if update_enabled:
        update_result, finding = _step_update(config, updater)
        findings.append(finding)
    else:
        findings.append(
            Finding(
                name="yt-dlp update",
                status=UNKNOWN,
                detail="skipped (TUNEBOX_AUTO_UPDATE=0 or --no-update)",
            )
        )

    report = (health or (lambda cfg: check_health(cfg, run_canary_check=do_canary)))(config)
    findings.extend(report.findings)

    if do_backup and (config.backup_dest or "").strip():
        backup_result, finding = _step_backup(config, backer)
        # The health check already has a `backup` finding, about *staleness*; this one is about the
        # run that just happened. Named differently so both survive into the log.
        findings.append(finding)
    elif do_backup:
        # Not repeated as a finding: check_health already reports the missing destination, and
        # saying it twice in one alert is how an alert stops being read.
        log.info("no backup destination configured; skipping the backup step")

    result = MaintenanceResult(
        findings=tuple(findings),
        health=report,
        update=update_result,
        backup=backup_result,
        elapsed=time.monotonic() - started,
    )

    record(
        config,
        report,
        extra={
            "maintenance": {
                "status": result.status,
                "update": update_result.as_dict() if update_result else None,
                "backup": backup_result.as_dict() if backup_result else None,
            }
        },
    )

    if not alert:
        return result
    dispatched = alerts_module.dispatch(
        config,
        ok=result.ok,
        subject=result.subject,
        payload=result.as_dict(),
        body=result.render(),
        now=now,
    )
    return MaintenanceResult(
        findings=result.findings,
        health=report,
        update=update_result,
        backup=backup_result,
        alert=dispatched,
        elapsed=result.elapsed,
    )


def _step_update(
    config: Config, updater: Callable[[Config], UpdateResult] | None
) -> tuple[UpdateResult | None, Finding]:
    try:
        result = (updater or (lambda cfg: update_ytdlp(cfg)))(config)
    except Exception as exc:  # noqa: BLE001 - one step must not take the others down
        log.exception("the update step raised")
        return None, Finding(
            name="yt-dlp update",
            status=FAIL,
            detail=f"the update step itself failed: {type(exc).__name__}: {exc}",
            fix="run `tunebox update` by hand to see the whole error",
        )
    if not result.ok:
        status = FAIL
    elif result.action.startswith("skipped"):
        # Nothing is broken, but the newest release is one this host has already rejected — worth
        # seeing in the report, not worth waking anybody for.
        status = WARN
    else:
        status = OK
    return result, Finding(
        name="yt-dlp update",
        status=status,
        detail=result.summary,
        fix=None if result.ok else (result.error or "see the update report"),
        data=result.as_dict(),
    )


def _step_backup(
    config: Config, backer: Callable[[Config], BackupResult] | None
) -> tuple[BackupResult | None, Finding]:
    try:
        result = (backer or (lambda cfg: run_backup(cfg)))(config)
    except TuneboxError as exc:
        return None, Finding(
            name="backup run",
            status=FAIL,
            detail=str(exc),
            fix="check TUNEBOX_BACKUP_DEST",
        )
    except Exception as exc:  # noqa: BLE001 - same reasoning as the update step
        log.exception("the backup step raised")
        return None, Finding(
            name="backup run",
            status=FAIL,
            detail=f"the backup step itself failed: {type(exc).__name__}: {exc}",
        )
    return result, Finding(
        name="backup run",
        status=OK if result.ok else FAIL,
        detail=result.summary,
        fix=None if result.ok else "run `tunebox backup` by hand to see rsync's output",
        data=result.as_dict(),
    )
