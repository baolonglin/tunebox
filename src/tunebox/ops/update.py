"""Updating yt-dlp on a schedule, safely: canary first, rollback if the canary says so.

PLAN.md 3.3: *"Pin the yt-dlp version; update on a schedule with a canary ingest of a known-good
video after each update. Alert on failure, keep the previous version to roll back."*

The order of operations here is the whole design, and it is not the obvious one. The obvious version
is *update, then canary, then roll back if the canary fails* — which is wrong in the situation that
matters most. If extraction is **already** broken (YouTube changed something yesterday and the pinned
version cannot cope), then after updating, the canary fails, and the obvious logic rolls back to the
version that was *also* broken and marks the new release bad. The house stays silent and the one
thing that could have fixed it has just been reverted.

So the canary runs **twice**: once before the update and once after, and the two answers together
decide what happens:

=================  ================  ==========================================================
canary before      canary after      decision
=================  ================  ==========================================================
pass               pass              keep the new version, record it as known-good
pass               **fail**          **roll back** — the update is a regression. Record the bad
                                     version so tomorrow's run does not install it again, and
                                     re-run the canary to prove the rollback actually worked
**fail**           pass              keep it, and say so loudly: the update *fixed* extraction
**fail**           **fail**          keep the new version and alert. Rolling back would be
                                     pointless (the old one is broken too) and actively harmful:
                                     the newer extractor is the one the next release will fix
=================  ================  ==========================================================

Two smaller decisions worth recording:

* **A version whose canary failed is remembered.** Without that, a bad release is installed,
  canaried, rolled back, and then installed again tomorrow — forever, with an alert every night.
* **This refuses to run outside a virtualenv** unless explicitly told otherwise, because on Debian
  (which is what a Raspberry Pi runs) ``pip install`` into the system interpreter is both blocked by
  PEP 668 and, when forced, a way to break ``apt``.

Every subprocess is injectable, so the tests drive all four rows of that table — including the
rollback and the "already broken" case — without pip, the network, or a real yt-dlp.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ..config import Config
from .canary import CanaryResult, run_canary, ytdlp_version
from .state import OpsState

log = logging.getLogger(__name__)

#: ``(argv) -> (returncode, output)``. ``output`` is stdout and stderr together: pip puts the
#: interesting part of a failure in either one depending on the failure, and the only consumer is a
#: log line and an alert.
PipRunner = Callable[[Sequence[str]], "tuple[int, str]"]

#: ``(config) -> CanaryResult``, so the update logic can be tested against every canary outcome.
CanaryRunner = Callable[[Config], CanaryResult]

#: What happened, as one word. Named constants because these end up in the health log, where a
#: typo'd string is a query that silently returns nothing.
UNCHANGED = "unchanged"
UPDATED = "updated"
RECOVERED = "recovered"
ROLLED_BACK = "rolled-back"
KEPT_BROKEN = "kept-broken"
SKIPPED_KNOWN_BAD = "skipped-known-bad"
REFUSED = "refused"
FAILED = "failed"
REAPPLIED = "reapplied"


@dataclass(frozen=True)
class UpdateResult:
    ok: bool
    action: str
    channel: str = "stable"
    before: str | None = None
    after: str | None = None
    canary_before: CanaryResult | None = None
    canary_after: CanaryResult | None = None
    #: The canary run *after* a rollback. Its whole purpose is to answer "did the rollback work?",
    #: which is the question an operator asks first and which nothing else can answer.
    canary_restored: CanaryResult | None = None
    messages: tuple[str, ...] = ()
    error: str | None = None
    known_good: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "channel": self.channel,
            "before": self.before,
            "after": self.after,
            "known_good": self.known_good,
            "canary_before": self.canary_before.as_dict() if self.canary_before else None,
            "canary_after": self.canary_after.as_dict() if self.canary_after else None,
            "canary_restored": self.canary_restored.as_dict() if self.canary_restored else None,
            "messages": list(self.messages),
            "error": self.error,
        }

    @property
    def summary(self) -> str:
        if self.action == UNCHANGED:
            return f"yt-dlp {self.before or '?'} is current ({self.channel} channel)"
        if self.action == UPDATED:
            return f"yt-dlp {self.before} -> {self.after}, canary passed"
        if self.action == RECOVERED:
            return f"yt-dlp {self.before} -> {self.after}: extraction was broken and this fixed it"
        if self.action == ROLLED_BACK:
            restored = "and the rollback was verified" if (
                self.canary_restored and self.canary_restored.ok
            ) else "AND THE ROLLBACK DID NOT RESTORE IT"
            return f"yt-dlp {self.after} broke the canary; rolled back to {self.before} {restored}"
        if self.action == KEPT_BROKEN:
            return (
                f"extraction is broken on both {self.before} and {self.after}; kept {self.after} "
                "because the newer extractor is the one that will be fixed"
            )
        if self.action == SKIPPED_KNOWN_BAD:
            return f"yt-dlp {self.after} is already known bad; staying on {self.before}"
        if self.action == REAPPLIED:
            return f"reinstalled the canaried known-good yt-dlp {self.after}"
        return self.error or self.action


@dataclass
class _Pip:
    """The real pip runner: this interpreter's pip, never a ``pip`` from ``PATH``.

    ``sys.executable -m pip`` because the venv the service runs from is the thing being updated, and
    a ``pip`` earlier on ``PATH`` would cheerfully upgrade a completely different environment and
    report success.
    """

    timeout: float = 600.0
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str]) -> tuple[int, str]:
        import subprocess

        command = [sys.executable, "-m", "pip", *argv]
        self.calls.append(command)
        log.info("running %s", " ".join(command))
        try:
            completed = subprocess.run(  # noqa: S603 - argv list, never a shell string
                command,
                capture_output=True,
                timeout=self.timeout,
                check=False,
                # pip prints whatever a package's metadata contains; decoded permissively for the
                # same reason as everywhere else in this package.
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, f"{type(exc).__name__}: {exc}"
        return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def in_virtualenv() -> bool:
    return sys.prefix != sys.base_prefix


def update_ytdlp(
    config: Config,
    *,
    pip: PipRunner | None = None,
    canary: CanaryRunner | None = None,
    allow_system: bool = False,
    dry_run: bool = False,
) -> UpdateResult:
    """Update yt-dlp, canary it, and roll back if that made things worse.

    Returns rather than raises for every operational outcome — a failed update is data the caller
    has to alert on, not an exception it has to catch.
    """
    channel = config.ytdlp_channel
    state = OpsState(config.ops_state_path)
    section = state.section("ytdlp")
    blocked: dict[str, Any] = dict(section.get("blocked") or {})
    messages: list[str] = []

    pip = pip or _Pip()
    canary = canary or (lambda cfg: run_canary(cfg))

    if not in_virtualenv() and not allow_system:
        return UpdateResult(
            ok=False,
            action=REFUSED,
            channel=channel,
            before=ytdlp_version(config),
            known_good=section.get("known_good"),
            error=(
                "refusing to pip-install into a non-virtualenv interpreter "
                f"({sys.executable}). On Debian this is blocked by PEP 668 and, when forced, is a "
                "way to break apt. Install tunebox with deploy/install.sh (which makes a venv), or "
                "pass --allow-system if you are certain."
            ),
        )

    if tuple(config.ytdlp[:3]) != (sys.executable, "-m", "yt_dlp"):
        # TUNEBOX_YTDLP can point anywhere, and a yt-dlp found on PATH is not the one in this venv.
        # Updating this venv then reports success while the yt-dlp that actually runs is untouched —
        # a silent no-op is the worst kind of outcome for a job nobody watches.
        messages.append(
            f"note: yt-dlp runs as {' '.join(config.ytdlp)}, which is not this venv's "
            f"'{sys.executable} -m yt_dlp'; pip cannot manage it, so this update may not change "
            "the yt-dlp that actually runs"
        )

    before = ytdlp_version(config)
    canary_before = canary(config)
    if canary_before.ok and before:
        section = state.save_section(
            "ytdlp", {"known_good": before, "last_good_at": time.time()}
        )
    known_good = section.get("known_good") or before

    if dry_run:
        return UpdateResult(
            ok=canary_before.ok,
            action=UNCHANGED,
            channel=channel,
            before=before,
            after=before,
            canary_before=canary_before,
            known_good=known_good,
            messages=(*messages, "dry run: nothing was installed"),
        )

    if blocked:
        # Ask before acting, when there is a reason to. Installing first and reverting afterwards
        # (which is what this did originally) is self-healing but wasteful and briefly wrong: the
        # known-broken version *is* installed for the length of the revert, and a nightly job killed
        # in that window leaves it in place until tomorrow. ``--dry-run`` has been in pip since 22.2;
        # when it is not understood, or its output cannot be read, the fall-through below still
        # catches the case after the fact, so this is an optimisation of a correct path rather than
        # the thing correctness depends on.
        candidate = _candidate_version(pip, channel)
        if candidate is not None and candidate in blocked:
            state.save_section(
                "ytdlp", {"last_checked": time.time(), "last_action": SKIPPED_KNOWN_BAD}
            )
            return UpdateResult(
                ok=canary_before.ok,
                action=SKIPPED_KNOWN_BAD,
                channel=channel,
                before=before,
                after=before,
                canary_before=canary_before,
                known_good=known_good,
                messages=(
                    *messages,
                    f"{candidate} is available but recorded as bad ({blocked[candidate]}); "
                    f"staying on {before} and not downloading it again",
                ),
            )

    returncode, output = pip(_install_argv(channel))
    if returncode != 0:
        state.save_section("ytdlp", {"last_checked": time.time(), "last_action": FAILED})
        # ``ok=False`` even when extraction itself is fine, and that is the deliberate answer to a
        # question this code got wrong first time round. A host where pip cannot reach PyPI is a host
        # whose *protection* has quietly stopped working: the canary still runs, but nothing can ever
        # act on it, so the day YouTube changes something there is no update to fix it. Reporting
        # that as a success made the command exit 0 and the nightly job green — forever, silently,
        # which is exactly the shape of failure this whole phase exists to prevent. The message says
        # in as many words that music still plays, so nobody panics about the wrong thing.
        return UpdateResult(
            ok=False,
            action=FAILED,
            channel=channel,
            before=before,
            after=before,
            canary_before=canary_before,
            known_good=known_good,
            messages=(
                *messages,
                "extraction itself is "
                + ("still working" if canary_before.ok else "ALSO broken")
                + ": this is a failure of the updater, not of playback",
            ),
            error=f"pip install failed (exit {returncode}): {_tail(output)}",
        )

    after = ytdlp_version(config)
    if after == before:
        state.save_section(
            "ytdlp", {"last_checked": time.time(), "last_action": UNCHANGED}
        )
        return UpdateResult(
            ok=canary_before.ok,
            action=UNCHANGED,
            channel=channel,
            before=before,
            after=after,
            canary_before=canary_before,
            known_good=known_good,
            messages=tuple(messages),
        )

    messages.append(f"pip installed yt-dlp {after} (was {before})")

    if after is not None and after in blocked:
        # Already tried, already broke the canary. Do not download the canary video again to prove
        # it a second time: go straight back to the version that works.
        rollback_error = _install_version(pip, known_good or before)
        restored = ytdlp_version(config)
        state.save_section(
            "ytdlp", {"last_checked": time.time(), "last_action": SKIPPED_KNOWN_BAD}
        )
        return UpdateResult(
            # Reverting cleanly is not the same as being healthy: if extraction was already broken
            # and the only newer release is one we know to be broken too, this host has a problem
            # that no update can fix tonight, and it has to say so rather than report a tidy revert.
            ok=rollback_error is None and canary_before.ok,
            action=SKIPPED_KNOWN_BAD,
            channel=channel,
            before=before,
            after=after,
            canary_before=canary_before,
            known_good=known_good,
            messages=(
                *messages,
                f"{after} is recorded as bad ({blocked[after]}); reverted to {restored or '?'}",
            ),
            error=rollback_error,
        )

    canary_after = canary(config)
    if canary_after.ok:
        action = RECOVERED if not canary_before.ok else UPDATED
        state.save_section(
            "ytdlp",
            {
                "known_good": after,
                "last_good_at": time.time(),
                "last_checked": time.time(),
                "last_action": action,
            },
        )
        return UpdateResult(
            ok=True,
            action=action,
            channel=channel,
            before=before,
            after=after,
            canary_before=canary_before,
            canary_after=canary_after,
            known_good=after,
            messages=tuple(messages),
        )

    if not canary_before.ok:
        # Broken before, broken after. Keeping the new version is the deliberate choice: the old one
        # is no better, and when yt-dlp ships the fix it will ship it on top of this release.
        state.save_section(
            "ytdlp", {"last_checked": time.time(), "last_action": KEPT_BROKEN}
        )
        return UpdateResult(
            ok=False,
            action=KEPT_BROKEN,
            channel=channel,
            before=before,
            after=after,
            canary_before=canary_before,
            canary_after=canary_after,
            known_good=known_good,
            messages=(
                *messages,
                "not rolling back: the previous version failed the canary too, so the rollback "
                "would restore a version that is equally broken",
            ),
            error=canary_after.error,
        )

    # A regression: it worked before the update and does not now.
    target = known_good or before
    rollback_error = _install_version(pip, target)
    restored_version = ytdlp_version(config)
    canary_restored = canary(config) if rollback_error is None else None
    blocked[str(after)] = canary_after.error or f"canary failed at {canary_after.stage}"
    state.save_section(
        "ytdlp",
        {
            "blocked": blocked,
            "last_checked": time.time(),
            "last_action": ROLLED_BACK,
            "known_good": target,
        },
    )
    return UpdateResult(
        ok=bool(canary_restored and canary_restored.ok),
        action=ROLLED_BACK,
        channel=channel,
        before=restored_version or target,
        after=after,
        canary_before=canary_before,
        canary_after=canary_after,
        canary_restored=canary_restored,
        known_good=target,
        messages=(*messages, f"rolled back to {restored_version or target}"),
        error=rollback_error or (None if canary_restored and canary_restored.ok else (
            "the rollback did not restore working extraction — this needs a human"
        )),
    )


def reapply_known_good(
    config: Config, *, pip: PipRunner | None = None, allow_system: bool = False
) -> UpdateResult:
    """Reinstall the recorded known-good yt-dlp if the installed one differs.

    Called by ``deploy/install.sh`` after it upgrades the package, and it exists because of an
    interaction that is easy to miss: ``pyproject.toml`` pins yt-dlp deliberately, so re-running the
    installer would quietly **undo** a canaried update and put a version back that may be the one
    that cannot download anything. The installer is the upgrade path, so it must not be the thing
    that reintroduces a fixed bug.

    No canary here on purpose: this version already passed one, and the installer must not need the
    network to finish.
    """
    state = OpsState(config.ops_state_path)
    section = state.section("ytdlp")
    known_good = section.get("known_good")
    current = ytdlp_version(config)
    if not known_good or known_good == current:
        return UpdateResult(
            ok=True,
            action=UNCHANGED,
            channel=config.ytdlp_channel,
            before=current,
            after=current,
            known_good=known_good,
            messages=("nothing recorded to reapply",) if not known_good else (),
        )
    if not in_virtualenv() and not allow_system:
        return UpdateResult(
            ok=False,
            action=REFUSED,
            channel=config.ytdlp_channel,
            before=current,
            known_good=known_good,
            error="refusing to pip-install into a non-virtualenv interpreter",
        )
    error = _install_version(pip or _Pip(), str(known_good))
    now_at = ytdlp_version(config)
    return UpdateResult(
        ok=error is None,
        action=REAPPLIED if error is None else FAILED,
        channel=config.ytdlp_channel,
        before=current,
        after=now_at,
        known_good=str(known_good),
        messages=(f"reinstalled the known-good yt-dlp {known_good} over {current}",),
        error=error,
    )


#: ``Would install yt-dlp-2026.9.30`` — pip's ``--dry-run`` summary line. Matched loosely because
#: pip lists every package it would install on one line, and only ours is interesting.
_WOULD_INSTALL = re.compile(r"\byt[-_]dlp-([0-9][^\s]*)")


def _candidate_version(pip: PipRunner, channel: str) -> str | None:
    """What an upgrade *would* install, or ``None`` if pip will not say.

    ``None`` is a perfectly good answer here and the caller treats it as "go ahead and find out the
    expensive way": ``--dry-run`` arrived in pip 22.2, and a Raspberry Pi OS release older than that
    is entirely plausible. This is an optimisation of a path that is already correct, not a
    correctness requirement.
    """
    returncode, output = pip([*_install_argv(channel), "--dry-run"])
    if returncode != 0:
        return None
    match = _WOULD_INSTALL.search(output)
    return match.group(1) if match else None


def _install_argv(channel: str) -> list[str]:
    """pip arguments for an upgrade on the given channel.

    ``--pre`` is the whole difference between the two: yt-dlp publishes its nightly builds to PyPI as
    pre-releases, so "nightly" needs no second index, no URL and no GitHub token — which is why that
    is the mechanism used rather than a release-asset download.
    """
    argv = ["install", "--upgrade", "--no-input", "--disable-pip-version-check"]
    if channel == "nightly":
        argv.append("--pre")
    argv.append("yt-dlp")
    return argv


def _install_version(pip: PipRunner, version: str | None) -> str | None:
    """Pin yt-dlp to exactly ``version``. Returns an error string, or ``None`` on success."""
    if not version:
        return (
            "no known-good version to roll back to: nothing has passed a canary on this host yet. "
            "Pin one by hand with: pip install 'yt-dlp==<version>'"
        )
    returncode, output = pip(
        [
            "install",
            "--no-input",
            "--disable-pip-version-check",
            # The version is going down, and pip will not downgrade without being asked plainly.
            "--upgrade",
            "--force-reinstall",
            "--no-deps",
            f"yt-dlp=={version}",
        ]
    )
    if returncode != 0:
        return f"rollback to yt-dlp {version} failed (exit {returncode}): {_tail(output)}"
    return None


def _tail(text: str, limit: int = 600) -> str:
    cleaned = (text or "").strip()
    return cleaned[-limit:]
