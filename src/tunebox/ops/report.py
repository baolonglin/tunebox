"""The vocabulary every Phase 5 check answers in.

One four-valued status rather than a boolean, because the honest answer to several of these
questions is neither yes nor no:

* ``ok`` — checked, and fine.
* ``warn`` — a real finding that is not this system's failure, or not dangerous on its own. The
  player daemon listening on every interface is the canonical example: it is OwnTone's default and
  the fix is its password, not its socket.
* ``fail`` — something is wrong and somebody has to act.
* ``unknown`` — **could not be established**, which is different from "fine". "No UPnP device
  answered" and "the service is not running so nothing is listening" both land here, and collapsing
  them into ``ok`` would turn this tool into one that reassures rather than one that checks.

Only ``fail`` affects an exit code. That is a deliberate choice about human behaviour: a check that
exits non-zero for an ``unknown`` it can never resolve is a check that gets commented out of the
nightly job within a fortnight.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

OK = "ok"
WARN = "warn"
FAIL = "fail"
UNKNOWN = "unknown"

#: Ordered least- to most-serious, so "the status of a report" is a max over its findings.
SEVERITY = {OK: 0, UNKNOWN: 1, WARN: 2, FAIL: 3}

#: Two characters, aligned, so a column of them reads as a shape rather than as prose. Matches the
#: ``[ok]``/``[!!]`` convention ``tunebox doctor`` already prints.
MARKS = {OK: "ok", WARN: "~~", FAIL: "!!", UNKNOWN: "??"}


@dataclass(frozen=True)
class Finding:
    """One answered question. ``fix`` is printed only when there is something to do about it."""

    name: str
    status: str
    detail: str
    fix: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name, "status": self.status, "detail": self.detail}
        if self.fix:
            payload["fix"] = self.fix
        if self.data:
            payload["data"] = self.data
        return payload


def worst(statuses: Iterable[str]) -> str:
    return max(statuses, key=lambda status: SEVERITY.get(status, 0), default=OK)


def format_findings(findings: Iterable[Finding], *, show_fixes: bool = True) -> str:
    """The human rendering, shared by ``exposure``, ``healthcheck`` and ``maintain``."""
    rows = list(findings)
    if not rows:
        return "(nothing checked)"
    width = max(len(row.name) for row in rows)
    lines: list[str] = []
    for row in rows:
        lines.append(f"[{MARKS.get(row.status, '??')}] {row.name.ljust(width)}  {row.detail}")
        if show_fixes and row.fix and row.status != OK:
            lines.append(f"     {' ' * width}  -> {row.fix}")
    return "\n".join(lines)
