"""Telling somebody. PLAN.md 3.3: *"Alert on failure."*

The hard part of alerting at this scale is not delivery, it is **not becoming noise**. A nightly job
that sends an identical "extraction is broken" message every night for a week trains its reader to
filter it, and the filter is still in place the night something new breaks. So:

* an alert is sent when the status **changes** — working to broken, or broken to working;
* an unchanged failure is repeated at most once every
  :attr:`~tunebox.config.Config.alert_repeat_hours` (a day, by default), so a problem that nobody has
  fixed is still mentioned, but once;
* **recovery is alerted too**, because "it started working again on its own" is information, and
  without it the last message anybody has says the system is broken.

Two delivery channels, chosen for what a household actually has rather than for generality:

* ``TUNEBOX_ALERT_COMMAND`` — any command. The report arrives as JSON on **stdin** and a one-line
  subject as the **last argument**, which is enough to drive ``mail``, ``terminal-notifier``,
  ``osascript``, ``ntfy``, or a three-line shell script. Split with :mod:`shlex` and executed as an
  argv list, never through a shell (PLAN.md 3.2) — so a pipeline has to be written as ``sh -c '…'``
  on purpose rather than by accident.
* ``TUNEBOX_ALERT_WEBHOOK`` — a URL to POST the JSON to. This is the only *outbound* request tunebox
  makes to a third party, it happens only because someone configured it, and the report it sends
  contains no secrets: hostnames, versions, counts and error strings. The token is never in it.

And the channel that always exists: the log. Every alert is written to stderr and to the health log
before any channel is tried, so a host with nothing configured still has a record — under launchd
and systemd, stderr *is* the log file. A delivery failure is reported but never raised: the point of
this module is a failure elsewhere, and hiding that behind "could not send email" would be perverse.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ..config import Config
from .state import OpsState

log = logging.getLogger(__name__)

#: Why an alert was (or was not) sent. These strings end up in the health log.
NEW_FAILURE = "new-failure"
STILL_FAILING = "still-failing"
RECOVERED = "recovered"
THROTTLED = "throttled"
NOTHING_TO_SAY = "nothing-to-say"

#: How long to give a delivery channel. Short: this runs at the end of a nightly job, and an alert
#: that hangs holds the job open until whatever supervisor is watching kills it.
DELIVERY_TIMEOUT = 20.0


@dataclass(frozen=True)
class AlertDecision:
    send: bool
    reason: str
    previous_status: str | None = None


@dataclass(frozen=True)
class AlertResult:
    decision: AlertDecision
    subject: str = ""
    #: ``channel -> None on success, or the error string``.
    deliveries: dict[str, str | None] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sent": self.decision.send,
            "reason": self.decision.reason,
            "previous_status": self.decision.previous_status,
            "subject": self.subject,
            "deliveries": dict(self.deliveries),
        }

    @property
    def failed_channels(self) -> tuple[str, ...]:
        return tuple(name for name, error in self.deliveries.items() if error is not None)


def decide(
    *,
    ok: bool,
    previous_status: str | None,
    last_alert_at: float | None,
    repeat_hours: float,
    now: float,
) -> AlertDecision:
    """Should this run produce an alert? Pure, so the throttling is testable without a clock.

    ``previous_status`` is the *last recorded* status, which is ``None`` on the very first run. A
    first run that fails alerts; a first run that succeeds does not — silence is what "fine" should
    sound like.
    """
    was_ok = previous_status is None or previous_status == "ok"
    if not ok:
        if was_ok:
            return AlertDecision(True, NEW_FAILURE, previous_status)
        if repeat_hours <= 0:
            return AlertDecision(True, STILL_FAILING, previous_status)
        if last_alert_at is None or (now - last_alert_at) >= repeat_hours * 3600:
            return AlertDecision(True, STILL_FAILING, previous_status)
        return AlertDecision(False, THROTTLED, previous_status)
    if not was_ok:
        return AlertDecision(True, RECOVERED, previous_status)
    return AlertDecision(False, NOTHING_TO_SAY, previous_status)


def dispatch(
    config: Config,
    *,
    ok: bool,
    subject: str,
    payload: dict[str, Any],
    body: str = "",
    now: Callable[[], float] = time.time,
    runner: Callable[[Sequence[str], str], tuple[int, str]] | None = None,
    poster: Callable[[str, bytes], str | None] | None = None,
) -> AlertResult:
    """Decide, deliver, and remember. Never raises.

    ``runner`` and ``poster`` are injected by the tests; the real ones are :func:`_run_command` and
    :func:`_post`.
    """
    state = OpsState(config.ops_state_path)
    section = state.section("alerts")
    timestamp = now()
    decision = decide(
        ok=ok,
        previous_status=section.get("status"),
        last_alert_at=_as_float(section.get("last_alert_at")),
        repeat_hours=config.alert_repeat_hours,
        now=timestamp,
    )

    # Recorded before delivery: if the process dies inside a webhook timeout, the next run must not
    # re-send the same "new failure" as if nothing had happened.
    state.save_section(
        "alerts",
        {
            "status": "ok" if ok else "fail",
            "last_run_at": timestamp,
            **({"last_alert_at": timestamp, "last_subject": subject} if decision.send else {}),
        },
    )

    if not decision.send:
        log.info("no alert: %s", decision.reason)
        return AlertResult(decision=decision, subject=subject, deliveries={})

    # The log first, always. On a host with no channels configured this *is* the alert, and under
    # launchd or systemd it lands in the service log next to everything else.
    (log.error if not ok else log.warning)("ALERT: %s", subject)
    if body:
        log.info("%s", body)

    document = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    deliveries: dict[str, str | None] = {}
    if config.alert_command:
        deliveries["command"] = (runner or _run_command)(
            _command_argv(config.alert_command, subject), document
        )[1] or None
    if config.alert_webhook:
        deliveries["webhook"] = (poster or _post)(
            config.alert_webhook, document.encode("utf-8")
        )
    if not deliveries:
        deliveries["log"] = None
    for name, error in deliveries.items():
        if error:
            log.error("alert channel %s failed: %s", name, error)

    if deliveries and all(error is not None for error in deliveries.values()):
        # Nothing got through. The timestamp was written before delivery on purpose (a process killed
        # inside a webhook timeout must not re-send the same "new failure" tomorrow as if nothing had
        # happened), but *total* failure is the other half of that trade: leaving it set would
        # suppress the retry for a whole day, and the message nobody received is the one that
        # mattered. So it is rolled back to the previous value, and the next run tries again.
        state.save_section(
            "alerts",
            {
                "last_alert_at": _as_float(section.get("last_alert_at")) or 0.0,
                "last_delivery_error": "; ".join(
                    f"{name}: {error}" for name, error in deliveries.items()
                )[:500],
            },
        )
    return AlertResult(decision=decision, subject=subject, deliveries=deliveries)


def _command_argv(command: str, subject: str) -> list[str]:
    """``shlex`` split plus the subject as the final argument.

    The subject goes last rather than into the environment so that the common configurations work
    with no wrapper at all: ``mail -s`` wants its subject as an argument, and
    ``terminal-notifier -message`` does too.
    """
    return [*shlex.split(command), subject]


def _run_command(argv: Sequence[str], document: str) -> tuple[int, str | None]:
    try:
        completed = subprocess.run(  # noqa: S603 - argv list from shlex, never a shell string
            list(argv),
            input=document,
            capture_output=True,
            timeout=DELIVERY_TIMEOUT,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return 127, f"{argv[0] if argv else '(empty)'} is not installed"
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    if completed.returncode != 0:
        tail = ((completed.stderr or "") + (completed.stdout or "")).strip()[-400:]
        return completed.returncode, f"exited {completed.returncode}: {tail or 'no output'}"
    return 0, None


def _post(url: str, body: bytes) -> str | None:
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "tunebox/alerts"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=DELIVERY_TIMEOUT) as response:  # noqa: S310
            if response.status >= 400:  # pragma: no cover - urllib raises for these
                return f"HTTP {response.status}"
        return None
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code} from the webhook"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"


def _as_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None
