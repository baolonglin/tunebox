"""The little bit of state the nightly job has to remember between runs.

Three facts, and each of them turns an operation that would otherwise be guesswork into a decision:

* **the known-good yt-dlp version**, without which "roll back" has no destination;
* **versions whose canary already failed**, without which a broken release is re-installed and
  re-rolled-back every single night, forever;
* **when the last backup and the last alert happened**, which is what makes "the backup is stale"
  and "do not send this same message again" answerable.

It is JSON in one small file rather than a table in the job database, for a reason that is about
failure rather than taste: this is read and written by a cron-like job that may run while the
service is stopped, mid-upgrade, or on a volume that has just been remounted. A file that can be
read with ``cat``, hand-edited when something has gone wrong, and thrown away with no consequence
beyond "forget the known-good version" is the right shape for that. Nothing in here is
irreplaceable, and :meth:`OpsState.read` treats a corrupt file as an empty one on purpose.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Bumped only if the shape changes incompatibly. Present so a future version can recognise — and
#: ignore — a file written by a future *newer* version rather than misreading it.
STATE_VERSION = 1


class OpsState:
    """A tiny JSON document on disk. Every write is atomic; every read is forgiving."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # -------------------------------------------------------------------- read

    def read(self) -> dict[str, Any]:
        """The whole document, or ``{}`` if it is missing, unreadable or corrupt.

        Never raises. A nightly maintenance job that dies because its own bookkeeping file was
        truncated by a power cut would fail in exactly the situation it exists to detect.
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:  # unreadable, or a directory where a file should be
            log.warning("cannot read ops state %s: %s", self.path, exc)
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning("ops state %s is not valid JSON (%s); starting from empty", self.path, exc)
            return {}
        if not isinstance(payload, dict):
            log.warning("ops state %s is not a JSON object; starting from empty", self.path)
            return {}
        return payload

    def section(self, name: str) -> dict[str, Any]:
        """One top-level section, always a dict — ``state.section("ytdlp")["known_good"]``."""
        value = self.read().get(name)
        return dict(value) if isinstance(value, dict) else {}

    # ------------------------------------------------------------------- write

    def save_section(self, name: str, values: dict[str, Any]) -> dict[str, Any]:
        """Merge ``values`` into section ``name`` and write the file. Returns the new section.

        Read-modify-write, not a lock. The only writers are the nightly job and someone running a
        command by hand, so a concurrent write is possible but its worst outcome is a lost
        timestamp — and a lock file that outlives a killed process would be a worse failure than
        the one it prevents.
        """
        document = self.read()
        section = document.get(name)
        merged = dict(section) if isinstance(section, dict) else {}
        merged.update(values)
        document[name] = merged
        document["version"] = STATE_VERSION
        document["updated_at"] = time.time()
        self._write(document)
        return merged

    def _write(self, document: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Same temp-then-replace as everywhere else in this project: a half-written state file is
        # indistinguishable from a corrupt one, and this one is written by a job nobody watches.
        handle, temporary = tempfile.mkstemp(dir=str(self.path.parent), prefix=".ops-", suffix=".json")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(document, stream, indent=2, sort_keys=True, default=str)
                stream.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except OSError:
            Path(temporary).unlink(missing_ok=True)
            raise


def append_jsonl(path: Path, record: dict[str, Any], *, max_bytes: int) -> None:
    """Append one JSON line to ``path``, rotating it to ``.1`` once it exceeds ``max_bytes``.

    PLAN.md 3.3 asks for "structured logs, retained". One line per run, so the whole history of the
    system's health is ``jq`` over one file — and rotated at a size rather than a date because this
    runs on an SD card, where an unbounded append-only file is a way to lose the machine.

    ``max_bytes`` of 0 disables logging entirely. Failures are logged and swallowed: not being able
    to write the log must never turn a healthy run into a failed one, nor mask a real alert.
    """
    if max_bytes <= 0:
        return
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    # Bytes, not characters: the size on disk is what the limit is about, and this project's library
    # is full of CJK titles, where one character is three bytes.
    size = len(line.encode("utf-8")) + 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Rotate *before* appending, so the limit is a limit rather than a limit plus one record.
        try:
            if path.stat().st_size + size > max_bytes:
                os.replace(path, path.with_suffix(path.suffix + ".1"))
        except FileNotFoundError:
            pass
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    except OSError as exc:
        log.warning("cannot append to health log %s: %s", path, exc)


def read_jsonl(path: Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    """The records in a health log, oldest first, skipping anything unparseable.

    Skipping rather than raising because the last line of an append-only file on a machine that
    lost power is routinely half a line, and the interesting content is all the other ones.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    if limit is not None and limit >= 0:
        return records[-limit:]
    return records
