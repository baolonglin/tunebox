"""Phase 5 — hardening: the parts of tunebox that keep it working when nobody is looking.

Everything here answers a row of PLAN.md's Phase 5 list or its risk table, and everything here is
*operational* rather than part of the request path — nothing in this package is imported by
:mod:`tunebox.api` or the worker, so a bug in it cannot break ingest or playback.

============================  ==============================================================
:mod:`.exposure`              is anything reachable from outside the house? (sockets + router)
:mod:`.canary`                one real download of one known video — the only true liveness test
:mod:`.update`                yt-dlp update, canaried, with a rollback that is verified
:mod:`.backup`                the library mirror, its verification, and the restore path
:mod:`.health`                the daily check, and the structured log it appends to
:mod:`.alerts`               delivery, with the throttling that keeps it worth reading
:mod:`.maintain`              the one command the scheduler runs, composing the above
:mod:`.state`                 the small JSON file that makes "roll back" and "stale" answerable
:mod:`.report`                the four-valued status vocabulary they all answer in
============================  ==============================================================

The shared design rule: **every piece of I/O is injectable.** A canary that could only be tested by
downloading from YouTube, or a rollback that could only be tested by installing packages, would be a
rollback nobody had ever run — which is precisely the failure mode this package exists to prevent.
"""

from .report import FAIL, MARKS, OK, SEVERITY, UNKNOWN, WARN, Finding, format_findings, worst

__all__ = [
    "FAIL",
    "MARKS",
    "OK",
    "SEVERITY",
    "UNKNOWN",
    "WARN",
    "Finding",
    "format_findings",
    "worst",
]
