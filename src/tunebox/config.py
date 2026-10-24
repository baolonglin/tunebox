"""Configuration: one frozen dataclass, populated from the environment.

Defaults are chosen so that ``tunebox search`` and ``tunebox ingest`` work on a fresh checkout
with nothing exported. Anything a Raspberry Pi deployment needs to change is an env var.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import os
import shutil
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .errors import ConfigError

#: Containers we are willing to produce. ``m4a`` (AAC) is the default because it is the format
#: verified playing on the SN9YG over Cast; ``mp3`` exists for the Bluetooth fallback.
SUPPORTED_FORMATS = ("m4a", "mp3")

#: JavaScript runtimes yt-dlp knows how to drive, in its own order of priority. Named here only so a
#: typo in ``TUNEBOX_JS_RUNTIMES`` is a startup error rather than a download that fails at 403.
KNOWN_JS_RUNTIMES = ("deno", "node", "quickjs", "bun")

#: Runtimes to *add* to yt-dlp's default. yt-dlp enables **only deno** out of the box and picks the
#: highest-priority one that is actually installed, so naming the rest here means "deno if you have
#: it, otherwise node, otherwise quickjs, otherwise bun" — and costs nothing on a host that has deno.
#:
#: This is not a nicety, it is what makes a 32-bit Raspberry Pi installable at all: there is no
#: official deno build for 32-bit ARM, and without a JS runtime every media download fails with
#: HTTP 403 (PLAN.md 0.3). Left as a default rather than something the installer has to detect,
#: because "try the ones that exist" needs no detection.
DEFAULT_JS_RUNTIMES = ("node", "quickjs", "bun")

DEFAULT_MUSIC_ROOT = Path.home() / "Music" / "tunebox"

#: Scratch directory *inside* the music root, so the final move is same-filesystem and atomic.
#: The leading dot keeps music daemons from indexing partial downloads.
SCRATCH_DIRNAME = ".tunebox-tmp"

#: State directory for the job queue, also inside the music root: the library volume is the one
#: thing a deployment definitely has, and the dot keeps scanners out of it.
STATE_DIRNAME = ".tunebox"
DB_FILENAME = "jobs.db"

#: Loopback, not ``0.0.0.0``: PLAN.md 3.2 forbids an open endpoint, so binding every interface
#: has to be a deliberate act (``TUNEBOX_API_HOST=192.168.x.y``), never a default.
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8080

#: PLAN.md 3.1: "Concurrency capped at 1–2." It is a politeness limit towards YouTube, not a
#: throughput knob, so the ceiling is enforced rather than documented.
MAX_WORKER_CONCURRENCY = 2

#: Phase 3: the player daemon. Loopback again — OwnTone runs on the same always-on host as the
#: API, and its own web UI is the Phase 3 remote, so nothing needs to reach it from off-box
#: except a browser the user points at OwnTone's port directly.
DEFAULT_PLAYER_URL = "http://127.0.0.1:3689"

#: The speaker this household actually owns (PLAN.md Phase 0). Matched case-insensitively against
#: the daemon's output names, so it is a default rather than a hard-coded device.
DEFAULT_OUTPUT_NAME = "Living Room speaker"

#: How often to check that the chosen output is still selected. Fifteen seconds is short enough
#: that a soundbar power-cycle is invisible by the time someone reaches for their phone, and long
#: enough to be free.
DEFAULT_OUTPUT_CHECK_INTERVAL = 15.0

#: How many times to ask the daemon to enable the output before giving up. More than one because a
#: Cast device in standby refuses the *first* connection and accepts a later one — measured on this
#: household's soundbar, where the failing attempt is a one-second ``No route to host`` and the
#: attempt a minute later established TLS with nothing else changed. Three, because each failed
#: attempt costs about a second and a phone waiting on a button has a budget.
DEFAULT_OUTPUT_SELECT_ATTEMPTS = 3

#: Phase 4: how many files ``GET /library`` is willing to look at before it stops walking. The
#: filesystem is the library database (PLAN.md 3.1), so a browse is a directory walk, and a walk of
#: an accidentally-enormous root — a music root set to ``/`` by a typo — must not hold a worker
#: thread for minutes. The answer is truncated and says so rather than being slow.
DEFAULT_LIBRARY_MAX_FILES = 50_000

#: Phase 6: the directory under the music root that holds playlists, one ``.m3u`` file each.
#:
#: A *visible* top-level directory, which is the one non-obvious part. The music root is
#: ``Artist/Album/Track`` by convention, so this is a pseudo-artist — harmless, because
#: :func:`tunebox.library.list_tracks` lists audio files and ``.m3u`` is not one, and it is where
#: OwnTone's own browse view expects to find playlists. A *hidden* directory would not work at all:
#: every music scanner skips dot-entries (it is the same rule that keeps :data:`SCRATCH_DIRNAME` and
#: :data:`STATE_DIRNAME` out of the library), so a ``.playlists`` directory would hold files no
#: daemon ever indexed. Hence the validation in :meth:`Config.__post_init__`.
DEFAULT_PLAYLISTS_DIRNAME = "Playlists"

#: How long a media capability URL stays valid, in seconds. Six hours.
#:
#: The number is a trade between two concrete things. A ticket is spent by ``<audio src>``, so it has
#: to outlive the gap between "the queue was built" and "the ninth track starts" — an evening, not a
#: minute — and a page that had to re-mint mid-queue would stall between tracks on a phone whose
#: screen is off. Against that, the URL appears in uvicorn's access log, so its lifetime is how long
#: that log line is worth anything to somebody who can read it. Six hours is longer than any queue
#: this household builds and shorter than the gap to the next time anyone is in the room.
#:
#: It is not a session: nothing is remembered, so this is also the longest a *deleted* track's URL
#: could keep working — except that it cannot, because the path is re-resolved on every request.
DEFAULT_MEDIA_TICKET_TTL = 6 * 60 * 60.0

# ------------------------------------------------------------------ Phase 5: hardening

#: The canary video. "Me at the zoo" — the first video ever uploaded to YouTube, 19 seconds long.
#: Chosen for the two properties a canary needs and nothing else: it is *tiny*, so a daily download
#: costs nothing, and it is the least likely video on the platform to be deleted, region-locked or
#: made members-only. A canary that fails because the video went away teaches nothing and trains
#: whoever reads the alert to ignore it.
DEFAULT_CANARY_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"

#: Below this, whatever came back is not audio. The real failure mode this catches is a "successful"
#: download of an error page: yt-dlp exits 0 far less often than one would hope, but ffmpeg happily
#: produces a 0-byte container, and a canary that only checked the exit code would pass.
CANARY_MIN_BYTES = 8 * 1024

#: yt-dlp's two release channels. ``nightly`` is what PLAN.md 3.3 suggests ("nightly channel"), but
#: ``stable`` is the default here: the canary makes an update safe, it does not make it free, and a
#: household that is not currently broken by a YouTube change gains nothing from being a day ahead.
#: ``TUNEBOX_YTDLP_CHANNEL=nightly`` is the right setting *while* extraction is broken.
YTDLP_CHANNELS = ("stable", "nightly")

#: Free space on the music volume below which the health check fails (PLAN.md 3.3, "disk-space
#: alarm on the music volume"). Absolute rather than a percentage because the number that matters is
#: "can the next album be written", and 5 GiB is roughly a hundred of them at the AAC default.
DEFAULT_MIN_FREE_GB = 5.0

#: How stale a backup may get before the health check complains. Two days, not one: a nightly
#: backup that fails once must not page anybody, and one that has failed twice must.
DEFAULT_BACKUP_MAX_AGE_HOURS = 48.0

#: How often the *same* ongoing failure is re-alerted. Alerting on every run turns a broken
#: extractor into a stream of identical messages that get filtered, which is worse than not
#: alerting; alerting only on the transition risks the one alert being missed.
DEFAULT_ALERT_REPEAT_HOURS = 24.0

#: Where the ops state (known-good yt-dlp version, last backup, last alert) and the health log live.
#: Both under :data:`STATE_DIRNAME` next to the job database, for the reason that directory exists
#: at all: the music volume is the one filesystem a deployment definitely has, and under systemd the
#: music root is the only path this service may write to.
OPS_STATE_FILENAME = "ops.json"
HEALTH_LOG_FILENAME = "health.jsonl"

#: The health log is rotated at this size, keeping one previous generation. PLAN.md 3.3 asks for
#: "structured logs, retained" — retained, not unbounded, because this runs on an SD card.
DEFAULT_HEALTH_LOG_MAX_BYTES = 1_000_000


def binds_every_interface(host: str) -> bool:
    """Would binding ``host`` expose the service on every interface, including the router-facing
    one?

    Parsed with :mod:`ipaddress` rather than compared against a list of strings, because
    ``0.0.0.0`` has many spellings — ``0``, ``0x0``, ``0.0``, ``::``, ``::0`` and
    ``0:0:0:0:0:0:0:0`` all resolve to the wildcard, and a blocklist of literals catches none of
    them. An empty host means the same thing to the socket layer.
    """
    value = (host or "").strip().strip("[]")
    if value == "" or value == "*":
        return True
    try:
        return ipaddress.ip_address(value).is_unspecified
    except ValueError:
        pass
    # Not a literal address: it is a hostname, and a hostname cannot be the wildcard. Anything
    # numeric-but-unparseable (``0x0``, ``0.0``, ``00``) is a legacy inet_aton spelling that the
    # socket layer *does* accept as the wildcard, so refuse those too.
    if all(ch in "0.:xX" for ch in value):
        return True
    return False


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc


def _env_opt_int(env: Mapping[str, str], key: str, default: int | None = None) -> int | None:
    """Like :func:`_env_int` but "unset" is a meaningful value, not a number.

    Used for the output volume, where "leave whatever the speaker remembers" is different from
    any particular level.
    """
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


def _env_bool(env: Mapping[str, str], key: str, default: bool = False) -> bool:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key} must be a boolean (1/0, true/false), got {raw!r}")


def _output_allow(env: Mapping[str, str]) -> tuple[str, ...]:
    """Parse ``TUNEBOX_OUTPUT_ALLOW`` — the speakers the remote may switch to.

    Comma-separated, each entry an output **id or name**, matched case-insensitively. Empty (the
    default) means no restriction.

    Why this exists at all: OwnTone lists every Cast and AirPlay device it can see over mDNS, and
    mDNS does not stop at the front door — a neighbour's Chromecast is in that list. For as long as
    tunebox only ever selected the one configured speaker that did not matter, but the device
    selector is a list of everything the daemon can see, so "the remote must not control devices
    that are not ours" needs a way to be *said* rather than merely hoped for.

    Comma-separated, which cannot express a name containing a comma — and the fix for that is the
    same one :attr:`Config.output_id` already exists for: use the id, which is stable anyway, where
    a name is whatever somebody last typed into the Google Home app.
    """
    raw = env.get("TUNEBOX_OUTPUT_ALLOW")
    if raw is None:
        return ()
    return tuple(entry.strip() for entry in raw.split(",") if entry.strip())


#: Spellings of "I have thought about backups and do not want one". Accepted as values of
#: ``TUNEBOX_BACKUP_DEST`` so that "no backup" can be *said* rather than merely left unsaid — see
#: :attr:`Config.backup_optional` for why the two have to be different states.
_NO_BACKUP_VALUES = frozenset({"none", "no", "off", "disabled", "false", "0"})


def _backup_dest(env: Mapping[str, str]) -> str | None:
    """The backup destination, with the "no thank you" spellings resolved to ``None``.

    A path is returned as given — not resolved or expanded here, because it may be an rsync remote
    spec (``pi@nas:/srv/music``) that no filesystem operation should be applied to.
    """
    raw = (env.get("TUNEBOX_BACKUP_DEST") or "").strip()
    if not raw or raw.lower() in _NO_BACKUP_VALUES:
        return None
    return raw


def resolve_ytdlp_command(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Decide how to invoke yt-dlp.

    Prefers ``python -m yt_dlp`` from the *current interpreter* so the version actually used is
    the one pinned in this project's dependencies, not whatever happens to be first on PATH
    (PLAN.md 3.3 pins the version on purpose). Falls back to a PATH binary, and finally to the
    bare name so the resulting error message is about a missing binary rather than a crash.
    """
    env = os.environ if env is None else env
    override = env.get("TUNEBOX_YTDLP")
    if override:
        return tuple(override.split())

    if importlib.util.find_spec("yt_dlp") is not None:
        return (sys.executable, "-m", "yt_dlp")

    found = shutil.which("yt-dlp")
    return (found,) if found else ("yt-dlp",)


def resolve_js_runtimes(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Extra runtimes to offer yt-dlp, from ``TUNEBOX_JS_RUNTIMES``.

    An explicitly empty value means "deno only", which is yt-dlp's own default and the right setting
    for a host where some ancient runtime is on PATH and must not be tried.
    """
    env = os.environ if env is None else env
    raw = env.get("TUNEBOX_JS_RUNTIMES")
    if raw is None:
        return DEFAULT_JS_RUNTIMES
    names = tuple(name for name in raw.replace(",", " ").split() if name)
    for name in names:
        if name not in KNOWN_JS_RUNTIMES:
            raise ConfigError(
                f"unknown JS runtime {name!r} in TUNEBOX_JS_RUNTIMES; "
                f"yt-dlp supports {', '.join(KNOWN_JS_RUNTIMES)}"
            )
    return names


@dataclass(frozen=True)
class Config:
    music_root: Path = DEFAULT_MUSIC_ROOT
    audio_format: str = "m4a"
    ytdlp: tuple[str, ...] = field(default_factory=resolve_ytdlp_command)
    #: Extra JS runtimes to enable, appended to yt-dlp's deno-only default. See
    #: :data:`DEFAULT_JS_RUNTIMES` — this is what makes a Pi with no deno build work.
    #:
    #: Passed as ``--js-runtimes`` on every invocation, so a ``TUNEBOX_YTDLP`` override pointing at a
    #: yt-dlp older than the flag needs ``TUNEBOX_JS_RUNTIMES=`` as well.
    js_runtimes: tuple[str, ...] = field(default_factory=resolve_js_runtimes)
    search_limit: int = 20
    #: Metadata-only calls are fast; a long hang here is a bot challenge, not slow progress.
    inspect_timeout: int = 60
    fetch_timeout: int = 600
    #: Be a light-touch client (PLAN.md 0.3). These are seconds.
    sleep_requests: float = 1.0
    sleep_interval: float = 1.0
    max_sleep_interval: float = 5.0

    # ---------------------------------------------------------------- Phase 2: the service
    #: Where the API listens. Loopback by default; a LAN address is an explicit choice.
    api_host: str = DEFAULT_API_HOST
    api_port: int = DEFAULT_API_PORT
    #: Shared secret checked on *every* request (PLAN.md 3.2). No default: a service that
    #: downloads URLs and runs ffmpeg does not get to have a blank password.
    #: ``repr=False`` so that logging a Config — which is exactly what someone does while debugging
    #: a startup problem — cannot put the token in a log file or a traceback.
    api_token: str | None = field(default=None, repr=False)
    #: Opt-in escape hatch for binding every interface, e.g. inside a container that only has
    #: one. Never enable it on a host that faces the router.
    allow_all_interfaces: bool = False
    #: ``None`` means "under the music root" — see :attr:`database_path`.
    db_path: Path | None = None
    worker_concurrency: int = 1
    #: Bounded retries (PLAN.md 3.1): three attempts total, exponential backoff in between.
    job_max_attempts: int = 3
    job_retry_backoff: float = 10.0
    job_retry_backoff_max: float = 300.0
    #: How long an idle worker waits before looking again; enqueues wake it immediately, so this
    #: only bounds the delay for retries that became due.
    job_poll_interval: float = 1.0

    # ------------------------------------------------- Phase 3: the playback session
    #: Base URL of the player daemon's JSON API (OwnTone: ``http://host:3689``).
    player_url: str = DEFAULT_PLAYER_URL
    #: OwnTone's ``admin_password``. Only needed when the request does not come from a network
    #: OwnTone considers trusted; loopback normally is, so this is usually unset. ``repr=False`` for
    #: the same reason as :attr:`api_token`.
    player_password: str | None = field(default=None, repr=False)
    #: Short by design: a playback command that has not been acknowledged in a couple of seconds
    #: has failed, and a phone waiting on a spinner is worse than an error.
    player_timeout: float = 5.0
    #: The speaker to keep selected, by name. An id wins over a name if both are given, because an
    #: id is unambiguous and a name is what a user renamed in the Google Home app last week.
    output_name: str = DEFAULT_OUTPUT_NAME
    output_id: str | None = None
    #: Volume to set when *first* selecting the output, so a fresh session is never silent or
    #: deafening. ``None`` leaves whatever the speaker remembers.
    output_volume: int | None = None
    #: Seconds between output-selection checks. ``0`` disables the keeper entirely.
    output_check_interval: float = DEFAULT_OUTPUT_CHECK_INTERVAL
    #: Attempts per "enable this speaker" command. See
    #: :meth:`~tunebox.player.owntone.OwnTonePlayer.select_output`: a sleeping Cast device refuses
    #: the first connection, so one attempt is a button that fails for a reason that has already
    #: gone away by the time somebody reads the error.
    output_select_attempts: int = DEFAULT_OUTPUT_SELECT_ATTEMPTS
    #: Which speakers the remote's device selector may switch to, as ids or names. Empty means
    #: every output the daemon can see — see :func:`_output_allow` for why that default is a
    #: deliberate choice rather than an oversight, and why a household with a neighbour in the list
    #: should set this.
    output_allow: tuple[str, ...] = ()
    #: Trigger a library rescan after each successful ingest (PLAN.md Phase 3). On by default; it
    #: is the whole reason a freshly ingested track is playable without waiting for a periodic
    #: scan.
    rescan_after_ingest: bool = True

    # -------------------------------------------------- Phase 4: the one web UI
    #: Serve the app shell (HTML/CSS/JS/manifest/icons) from the API. On by default — Phase 4's exit
    #: criterion is "one icon on the phone does everything", and an API with no UI does not meet it.
    #: Off makes tunebox a pure JSON service again, which is the right setting for a host where the
    #: only client is ``curl``: the shell is the one thing served without the token (see
    #: :mod:`tunebox.api.ui`), so being able to remove it entirely is worth an env var.
    ui_enabled: bool = True
    #: Where OwnTone's own web UI lives, for the "browse the whole library / manage playlists" link.
    #: Normally derived rather than configured — see :meth:`owntone_ui` — because the daemon runs on
    #: loopback and a *phone* cannot follow a ``127.0.0.1`` link.
    owntone_ui_url: str | None = None
    #: Ceiling on the directory walk behind ``GET /library``.
    library_max_files: int = DEFAULT_LIBRARY_MAX_FILES
    #: Directory under the music root holding the playlist files. See
    #: :data:`DEFAULT_PLAYLISTS_DIRNAME` for why it must be visible rather than a dot-directory. The
    #: *path* is deliberately not a property here: :mod:`tunebox.playlists` sanitises this name and
    #: checks containment before it joins anything, and a convenience accessor that skipped those
    #: steps would be a second answer to "where are the playlists" that could disagree with the first.
    playlists_dirname: str = DEFAULT_PLAYLISTS_DIRNAME

    # ----------------------------------------- Phase 7: the phone as an output
    #: Let the remote play library audio through the browser it is running in — "This phone" in the
    #: output picker, instead of the house speaker.
    #:
    #: On by default, and it is the one setting here that *adds* an unauthenticated route, so the
    #: reasoning is worth having in one place. ``<audio src>`` is a request the browser makes and a
    #: page cannot put a header on it, so ``/media/{ticket}`` is reached with a signed capability URL
    #: rather than the shared token (:mod:`tunebox.core.tickets`). Turning this off removes the
    #: ``/media/`` routes **and** their auth exemption, exactly as ``TUNEBOX_UI=0`` does for the
    #: shell — so a household that only ever plays on the soundbar can have a service with no
    #: token-free way to read a byte of audio.
    local_playback: bool = True
    #: Lifetime of a media capability URL, in seconds. See :data:`DEFAULT_MEDIA_TICKET_TTL`.
    media_ticket_ttl: float = DEFAULT_MEDIA_TICKET_TTL

    # ------------------------------------------------ Phase 5: keeping it alive
    #: The video the canary ingest downloads. See :data:`DEFAULT_CANARY_URL` for why this one.
    canary_url: str = DEFAULT_CANARY_URL
    #: Which yt-dlp release channel ``tunebox update`` upgrades to: ``stable`` or ``nightly``.
    ytdlp_channel: str = "stable"
    #: Whether ``tunebox maintain`` is allowed to change the installed yt-dlp at all. Off makes the
    #: nightly job a pure health check, which is the right setting on a host where the venv is
    #: managed by something else (a container image, Nix, a distribution package).
    auto_update: bool = True
    #: Where ``tunebox backup`` mirrors the library: a local path, or anything rsync understands
    #: (``user@host:/path``). Unset means no backup, which the health check reports rather than
    #: silently accepting — the library is the only irreplaceable state in the system.
    backup_dest: str | None = None
    #: Deliberately *not* backing up — ``TUNEBOX_BACKUP_DEST=none``. This exists because of what the
    #: alternative costs: with no destination set, the health check **fails**, because a system whose
    #: only irreplaceable state has no copy is not in a healthy state, and reporting it as a mere
    #: warning made the nightly job green forever on a host with no backup at all (found by an audit
    #: of Phase 5). But nagging daily about a deliberate choice is the alert fatigue this module is
    #: built to avoid, so there has to be a way to say "I know" — and it has to be a *different*
    #: value from "unset", or it would be indistinguishable from never having read the message.
    backup_optional: bool = False
    #: Let the backup delete files the source no longer has. **Off by default**, and that is a
    #: safety decision rather than a default-for-the-sake-of-one: the accident a backup most often
    #: has to survive is somebody deleting an album, and ``--delete`` propagates exactly that.
    backup_prune: bool = False
    backup_max_age_hours: float = DEFAULT_BACKUP_MAX_AGE_HOURS
    #: Free space on the music volume below which the health check fails, in GiB.
    min_free_gb: float = DEFAULT_MIN_FREE_GB
    #: Command line run when the health check fails, given the JSON report on stdin and a one-line
    #: subject as its last argument. Split with :mod:`shlex`, executed as an argv list, never a
    #: shell string (PLAN.md 3.2) — so a pipeline has to be written as ``sh -c '...'`` deliberately.
    alert_command: str | None = None
    #: URL to POST the JSON report to. ``repr=False``: a Slack or ntfy webhook *is* a credential.
    alert_webhook: str | None = field(default=None, repr=False)
    #: How often an unchanged failure is re-alerted. See :data:`DEFAULT_ALERT_REPEAT_HOURS`.
    alert_repeat_hours: float = DEFAULT_ALERT_REPEAT_HOURS
    health_log_max_bytes: int = DEFAULT_HEALTH_LOG_MAX_BYTES

    def __post_init__(self) -> None:
        if self.audio_format not in SUPPORTED_FORMATS:
            raise ConfigError(
                f"unsupported audio format {self.audio_format!r}; "
                f"expected one of {', '.join(SUPPORTED_FORMATS)}"
            )
        for runtime in self.js_runtimes:
            if runtime not in KNOWN_JS_RUNTIMES:
                raise ConfigError(
                    f"unknown JS runtime {runtime!r}; "
                    f"yt-dlp supports {', '.join(KNOWN_JS_RUNTIMES)}"
                )
        if not 1 <= self.api_port <= 65535:
            raise ConfigError(f"api port out of range: {self.api_port}")
        if not 1 <= self.worker_concurrency <= MAX_WORKER_CONCURRENCY:
            raise ConfigError(
                f"worker concurrency must be between 1 and {MAX_WORKER_CONCURRENCY}, "
                f"got {self.worker_concurrency} (PLAN.md 3.1 caps it deliberately)"
            )
        if self.job_max_attempts < 1:
            raise ConfigError(f"job attempts must be at least 1, got {self.job_max_attempts}")
        if self.job_retry_backoff < 0 or self.job_retry_backoff_max < 0:
            raise ConfigError("retry backoff must not be negative")
        if self.job_poll_interval <= 0:
            raise ConfigError(f"poll interval must be positive, got {self.job_poll_interval}")
        if self.player_timeout <= 0:
            raise ConfigError(f"player timeout must be positive, got {self.player_timeout}")
        if self.output_check_interval < 0:
            raise ConfigError("output check interval must not be negative (0 disables it)")
        if self.output_volume is not None and not 0 <= self.output_volume <= 100:
            raise ConfigError(f"output volume must be 0-100, got {self.output_volume}")
        if self.library_max_files < 1:
            raise ConfigError(f"library max files must be at least 1, got {self.library_max_files}")
        if self.media_ticket_ttl <= 0:
            # Zero is not "no streaming" — it is a ticket that has already expired when it is
            # handed over, so every track would fail with a 403 that looked like a bug. Saying no is
            # the kinder answer; TUNEBOX_LOCAL_PLAYBACK=0 is how the feature is switched off.
            raise ConfigError(
                f"media ticket ttl must be positive, got {self.media_ticket_ttl} "
                "(set TUNEBOX_LOCAL_PLAYBACK=0 to turn phone playback off instead)"
            )
        playlists = self.playlists_dirname.strip()
        if not playlists or playlists in (".", ".."):
            raise ConfigError("playlists directory must be a name, not empty")
        if "/" in playlists or "\\" in playlists:
            raise ConfigError(
                f"playlists directory must be a single directory name under the music root, "
                f"got {self.playlists_dirname!r}"
            )
        if playlists.startswith("."):
            # Not fussiness: every music scanner skips dot-entries, so a hidden playlists directory
            # would hold files OwnTone never indexes — the playlists would exist and be invisible.
            raise ConfigError(
                f"playlists directory must not start with a dot ({self.playlists_dirname!r}): "
                "music scanners skip dot-entries, so the playlists would never be indexed"
            )
        if self.ytdlp_channel not in YTDLP_CHANNELS:
            raise ConfigError(
                f"unknown yt-dlp channel {self.ytdlp_channel!r}; "
                f"expected one of {', '.join(YTDLP_CHANNELS)}"
            )
        if self.min_free_gb < 0:
            raise ConfigError(f"minimum free space must not be negative, got {self.min_free_gb}")
        if self.backup_max_age_hours <= 0:
            raise ConfigError("backup max age must be positive")
        if self.alert_repeat_hours < 0:
            raise ConfigError("alert repeat interval must not be negative (0 means every run)")
        if self.health_log_max_bytes < 0:
            raise ConfigError("health log size limit must not be negative (0 disables the log)")
        if self.alert_webhook is not None:
            scheme = self.alert_webhook.split("://", 1)[0].lower()
            if scheme not in ("http", "https"):
                raise ConfigError(
                    "alert webhook must start with http:// or https://, got "
                    f"{self.alert_webhook!r}"
                )
        if self.owntone_ui_url is not None:
            scheme = self.owntone_ui_url.split("://", 1)[0].lower()
            if scheme not in ("http", "https"):
                raise ConfigError(
                    "owntone ui url must start with http:// or https://, got "
                    f"{self.owntone_ui_url!r}"
                )
        scheme = self.player_url.split("://", 1)[0].lower() if "://" in self.player_url else ""
        if scheme not in ("http", "https"):
            raise ConfigError(
                f"player url must start with http:// or https://, got {self.player_url!r}"
            )

    @property
    def player_base_url(self) -> str:
        """The daemon's base URL without a trailing slash, so joining a path is unambiguous."""
        return self.player_url.rstrip("/")

    def allows_output(self, output_id: str, output_name: str) -> bool:
        """May the remote switch playback to this output?

        ``True`` for everything when :attr:`output_allow` is empty, which is the default and keeps
        every existing installation working. Otherwise the id or the name must be listed — id *or*
        name rather than id-and-name, because the two are different kinds of answer: an id is what a
        careful configuration carries, a name is what somebody can read off the speaker.

        The configured output is **always** allowed, even if somebody writes an allowlist that
        forgets it. Otherwise ``TUNEBOX_OUTPUT`` and ``TUNEBOX_OUTPUT_ALLOW`` could disagree, and
        the way that would show up is the output keeper selecting a speaker the remote then refuses
        to show as selectable — a contradiction in the one part of this system that is supposed to
        need no attention.
        """
        if not self.output_allow:
            return True
        if self.output_id and output_id == self.output_id:
            return True
        folded = {entry.casefold() for entry in self.output_allow}
        if output_id.strip().casefold() in folded:
            return True
        if output_name.strip().casefold() in folded:
            return True
        return output_name.strip().casefold() == self.output_name.strip().casefold()

    @property
    def player_port(self) -> int:
        """The port OwnTone's own web UI answers on, taken from :attr:`player_url`.

        Not a separate setting: OwnTone serves its web UI and its JSON API from the same listener,
        so a second knob could only ever be wrong.
        """
        parsed = urllib.parse.urlsplit(self.player_base_url)
        if parsed.port:
            return parsed.port
        return 443 if parsed.scheme == "https" else 80

    @property
    def owntone_ui_link(self) -> str:
        """Absolute URL of OwnTone's web UI, or ``""`` for "the browser must work it out".

        The empty answer is the *normal* one and it is not a failure to configure something. The
        player lives on loopback (:data:`DEFAULT_PLAYER_URL`), and handing a phone a link to
        ``http://127.0.0.1:3689`` would point it at itself. The browser already knows the host it
        just loaded this page from, so the UI joins that hostname to :attr:`player_port` instead —
        and the only case where that is wrong is a player on a *different* machine, which is exactly
        the case where the player URL is already the right link.
        """
        if self.owntone_ui_url:
            return self.owntone_ui_url.rstrip("/")
        host = urllib.parse.urlsplit(self.player_base_url).hostname or ""
        try:
            if ipaddress.ip_address(host).is_loopback:
                return ""
        except ValueError:
            if host.lower() in ("localhost", "localhost.localdomain", ""):
                return ""
        return self.player_base_url

    @property
    def scratch_root(self) -> Path:
        return self.music_root / SCRATCH_DIRNAME

    @property
    def state_dir(self) -> Path:
        """Where tunebox keeps the state that is not audio: the queue, the ops state, the log.

        Inside the music root on purpose, and it is the same reason :data:`SCRATCH_DIRNAME` is:
        under the systemd unit the installer writes, the music root is the *only* path this service
        is allowed to write to (``ProtectSystem=strict`` plus one ``ReadWritePaths``). State
        somewhere tidier would be state the hardened service cannot save.
        """
        return self.music_root / STATE_DIRNAME

    @property
    def database_path(self) -> Path:
        """SQLite file for jobs and ingest history. Tracks live on the filesystem (PLAN.md 3.1)."""
        return self.db_path or self.state_dir / DB_FILENAME

    @property
    def ops_state_path(self) -> Path:
        """Known-good yt-dlp version, last backup, last alert. See :mod:`tunebox.ops.state`."""
        return self.state_dir / OPS_STATE_FILENAME

    @property
    def health_log_path(self) -> Path:
        """One JSON object per health check, appended. PLAN.md 3.3's "structured logs, retained"."""
        return self.state_dir / HEALTH_LOG_FILENAME

    @property
    def canary_dir(self) -> Path:
        """Scratch space for the canary download, thrown away after every run.

        Under the scratch root rather than ``/tmp`` because ``/tmp`` is not guaranteed writable by
        the hardened unit, and because a canary that fails for a reason unrelated to YouTube is a
        canary nobody trusts.
        """
        return self.scratch_root / "canary"

    def retry_delay(self, attempts: int) -> float:
        """Backoff before attempt ``attempts + 1``: exponential, capped, never negative."""
        if self.job_retry_backoff == 0:
            return 0.0
        delay = self.job_retry_backoff * (2 ** max(0, attempts - 1))
        return min(delay, self.job_retry_backoff_max) if self.job_retry_backoff_max else delay

    @property
    def job_timeout(self) -> float:
        """How long a job may be ``running`` before it is treated as abandoned.

        Derived rather than configured: the real timeouts are the ones the yt-dlp adapter enforces
        on its subprocesses, and this only has to be comfortably longer than both of them put
        together so that a slow-but-live download is never reclaimed underneath itself.
        """
        return self.inspect_timeout + self.fetch_timeout + 60.0

    def validate_for_serve(self) -> str:
        """Check the things that only matter when listening, and return the token.

        Deliberately *not* in ``__post_init__``: ``tunebox search`` and ``tunebox ingest`` must
        keep working on a fresh checkout with nothing exported, and they open no socket.
        """
        token = (self.api_token or "").strip()
        if not token:
            raise ConfigError(
                "TUNEBOX_TOKEN is not set. The API is password-protected even on the LAN "
                "(PLAN.md 3.2). Generate one with: python3 -c "
                "'import secrets; print(secrets.token_urlsafe(32))'"
            )
        if len(token) < 16:
            raise ConfigError("TUNEBOX_TOKEN is too short; use at least 16 characters")
        if not token.isascii():
            # Header values are bytes, and a token that only round-trips through one encoding is
            # a way to lock yourself out of your own service. Refuse it at startup instead.
            raise ConfigError("TUNEBOX_TOKEN must be ASCII; use secrets.token_urlsafe")
        if any(ch.isspace() for ch in token):
            raise ConfigError("TUNEBOX_TOKEN must not contain whitespace")
        if binds_every_interface(self.api_host) and not self.allow_all_interfaces:
            raise ConfigError(
                f"refusing to bind {self.api_host or '(empty)'}: that is every interface, and "
                "PLAN.md 3.2 says this service is never exposed. Set TUNEBOX_API_HOST to the LAN "
                "address, or TUNEBOX_ALLOW_ALL_INTERFACES=1 if you really mean it"
            )
        return token

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        root = env.get("TUNEBOX_MUSIC_ROOT")
        db = env.get("TUNEBOX_DB")
        return cls(
            music_root=Path(root).expanduser() if root else DEFAULT_MUSIC_ROOT,
            audio_format=(env.get("TUNEBOX_AUDIO_FORMAT") or "m4a").lower(),
            ytdlp=resolve_ytdlp_command(env),
            js_runtimes=resolve_js_runtimes(env),
            search_limit=_env_int(env, "TUNEBOX_SEARCH_LIMIT", 20),
            inspect_timeout=_env_int(env, "TUNEBOX_INSPECT_TIMEOUT", 60),
            fetch_timeout=_env_int(env, "TUNEBOX_FETCH_TIMEOUT", 600),
            sleep_requests=_env_float(env, "TUNEBOX_SLEEP_REQUESTS", 1.0),
            sleep_interval=_env_float(env, "TUNEBOX_SLEEP_INTERVAL", 1.0),
            max_sleep_interval=_env_float(env, "TUNEBOX_MAX_SLEEP_INTERVAL", 5.0),
            api_host=(env.get("TUNEBOX_API_HOST") or DEFAULT_API_HOST).strip(),
            api_port=_env_int(env, "TUNEBOX_API_PORT", DEFAULT_API_PORT),
            api_token=env.get("TUNEBOX_TOKEN"),
            allow_all_interfaces=_env_bool(env, "TUNEBOX_ALLOW_ALL_INTERFACES", False),
            db_path=Path(db).expanduser() if db else None,
            worker_concurrency=_env_int(env, "TUNEBOX_WORKERS", 1),
            job_max_attempts=_env_int(env, "TUNEBOX_JOB_ATTEMPTS", 3),
            job_retry_backoff=_env_float(env, "TUNEBOX_JOB_BACKOFF", 10.0),
            job_retry_backoff_max=_env_float(env, "TUNEBOX_JOB_BACKOFF_MAX", 300.0),
            job_poll_interval=_env_float(env, "TUNEBOX_JOB_POLL_INTERVAL", 1.0),
            player_url=(env.get("TUNEBOX_PLAYER_URL") or DEFAULT_PLAYER_URL).strip(),
            player_password=env.get("TUNEBOX_PLAYER_PASSWORD") or None,
            player_timeout=_env_float(env, "TUNEBOX_PLAYER_TIMEOUT", 5.0),
            output_name=(env.get("TUNEBOX_OUTPUT") or DEFAULT_OUTPUT_NAME).strip(),
            output_id=(env.get("TUNEBOX_OUTPUT_ID") or "").strip() or None,
            output_volume=_env_opt_int(env, "TUNEBOX_OUTPUT_VOLUME"),
            output_check_interval=_env_float(
                env, "TUNEBOX_OUTPUT_CHECK_INTERVAL", DEFAULT_OUTPUT_CHECK_INTERVAL
            ),
            output_select_attempts=_env_int(
                env, "TUNEBOX_OUTPUT_SELECT_ATTEMPTS", DEFAULT_OUTPUT_SELECT_ATTEMPTS
            ),
            output_allow=_output_allow(env),
            rescan_after_ingest=_env_bool(env, "TUNEBOX_RESCAN_AFTER_INGEST", True),
            ui_enabled=_env_bool(env, "TUNEBOX_UI", True),
            owntone_ui_url=(env.get("TUNEBOX_OWNTONE_UI") or "").strip() or None,
            library_max_files=_env_int(
                env, "TUNEBOX_LIBRARY_MAX_FILES", DEFAULT_LIBRARY_MAX_FILES
            ),
            playlists_dirname=(env.get("TUNEBOX_PLAYLISTS_DIR") or "").strip()
            or DEFAULT_PLAYLISTS_DIRNAME,
            local_playback=_env_bool(env, "TUNEBOX_LOCAL_PLAYBACK", True),
            media_ticket_ttl=_env_float(
                env, "TUNEBOX_MEDIA_TICKET_TTL", DEFAULT_MEDIA_TICKET_TTL
            ),
            canary_url=(env.get("TUNEBOX_CANARY_URL") or DEFAULT_CANARY_URL).strip(),
            ytdlp_channel=(env.get("TUNEBOX_YTDLP_CHANNEL") or "stable").strip().lower(),
            auto_update=_env_bool(env, "TUNEBOX_AUTO_UPDATE", True),
            backup_dest=_backup_dest(env),
            backup_optional=(env.get("TUNEBOX_BACKUP_DEST") or "").strip().lower()
            in _NO_BACKUP_VALUES,

            backup_prune=_env_bool(env, "TUNEBOX_BACKUP_PRUNE", False),
            backup_max_age_hours=_env_float(
                env, "TUNEBOX_BACKUP_MAX_AGE_HOURS", DEFAULT_BACKUP_MAX_AGE_HOURS
            ),
            min_free_gb=_env_float(env, "TUNEBOX_MIN_FREE_GB", DEFAULT_MIN_FREE_GB),
            alert_command=(env.get("TUNEBOX_ALERT_COMMAND") or "").strip() or None,
            alert_webhook=(env.get("TUNEBOX_ALERT_WEBHOOK") or "").strip() or None,
            alert_repeat_hours=_env_float(
                env, "TUNEBOX_ALERT_REPEAT_HOURS", DEFAULT_ALERT_REPEAT_HOURS
            ),
            health_log_max_bytes=_env_int(
                env, "TUNEBOX_HEALTH_LOG_MAX_BYTES", DEFAULT_HEALTH_LOG_MAX_BYTES
            ),
        )
