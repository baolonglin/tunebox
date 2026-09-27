# tunebox

Personal, home-network-only music ingest on top of [yt-dlp](https://github.com/yt-dlp/yt-dlp).
Search for a track, pull it into a local library as a correctly tagged file, and let a music
daemon play it on the living-room soundbar. See [PLAN.md](PLAN.md) for scope, architecture and
the reasoning behind both — this README only covers running the code.

**Phases 1–4 (this code): CLI, HTTP service, host-owned playback, and one web remote.** Search,
ingest, a job queue, an API, playback on a Chromecast speaker driven by OwnTone, and a single
installable page that does all of it — the phone is a remote, never the audio path. One known
shortfall: the soundbar's own display stays blank, because OwnTone's Cast output is screen-mirroring
and carries no track metadata. See [PLAN.md](PLAN.md) Phase 0.

## Requirements

| Dependency | Why |
|---|---|
| Python 3.11+ | 3.11 is what current Raspberry Pi OS ships |
| `ffmpeg` + `ffprobe` | audio extraction and thumbnail conversion |
| a JavaScript runtime | with `yt-dlp-ejs`, required for YouTube signature deciphering — **without one, downloads fail with HTTP 403**. `deno` is best; `node` 22+, `quickjs` and `bun` also work |
| [OwnTone](https://owntone.github.io/owntone-server/) 29+, built `--enable-chromecast` | playback only. Ingest works fine without it |

`yt-dlp`, `yt-dlp-ejs`, `mutagen`, `fastapi` and `uvicorn` are pinned Python dependencies and
install with the project. `deploy/install.sh` installs the rest.

**On the JavaScript runtime.** yt-dlp enables only `deno` by default, and there is no official deno
build for 32-bit ARM — so on a 32-bit Raspberry Pi that default means *no* runtime and every download
403s. tunebox therefore offers yt-dlp the alternatives as well (`TUNEBOX_JS_RUNTIMES`, default
`node,quickjs,bun`), which means "deno if you have it, otherwise the best of the rest". `tunebox
doctor` reports the one yt-dlp will actually use, with its version, by asking yt-dlp rather than
guessing:

```
[ok] JS runtime  deno-2.9.6, node-23.7.0
```

## Install

```bash
git clone <this repo> && cd tunebox
./deploy/install.sh
```

That is the whole of it on macOS and on Raspberry Pi OS / Debian: it installs what is missing,
creates a venv, generates a token, works out this host's LAN address, registers a service, starts it,
and prints the URL to open on the phone. See [Deploying](#deploying) for what it decides and how to
undo it.

For development, or to run it by hand:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/tunebox doctor          # verifies every dependency above
```

## Use

```bash
export TUNEBOX_MUSIC_ROOT=~/Music/tunebox      # default if unset

tunebox search kraftwerk autobahn --table      # drop --table for JSON
tunebox ingest iukUMRlaBBE --dry-run           # what would be written, and where
tunebox ingest https://youtu.be/iukUMRlaBBE    # download, tag, place
tunebox ingest <url> --artist Kraftwerk --album Autobahn --track-number 1
```

`ingest` accepts a full YouTube URL or a bare 11-character video id, and is idempotent: a track
already in the library is skipped after one metadata request (`--force` re-downloads).
Output is JSON on stdout; progress and logs go to stderr.

Exit codes: `0` success, `1` error (with a one-line `error: ...` on stderr, never a traceback),
`2` from `search` when there were no results, `130` on interrupt.

Files land as `Artist/Album/Track.m4a` (album-less tracks under `Artist/Singles/`), which is the
layout OwnTone, Navidrome, Jellyfin and MPD all read.

Non-Latin titles work, including the CJK `Artist《Title》[Official MV]` convention, where the
uploader is usually a record label rather than the artist. Where a title has no separator at all
(`K歌之王 陈奕迅 (歌词版)`), the channel name is used and `--artist` is the way to correct it —
tunebox will not guess.

## Service

```bash
export TUNEBOX_MUSIC_ROOT=~/Music/tunebox
export TUNEBOX_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')

tunebox serve                                  # 127.0.0.1:8080
tunebox serve --host 192.168.50.10 --port 8080 # reachable from the phone
```

One process runs the API and the ingest worker. **Every route requires the token**, in an
`X-Tunebox-Token` header or as `Authorization: Bearer` — including `/docs` and unknown paths,
which answer `401` rather than `404`. The one exception is the web app shell (see
[The remote](#the-remote)), which carries no data and asks for the token itself. `serve` refuses to
start without an ASCII token of at least 16 characters, and refuses to bind a wildcard address
(`0.0.0.0`, `::`, and the other spellings the socket layer accepts) unless
`TUNEBOX_ALLOW_ALL_INTERFACES=1`: this service runs yt-dlp and ffmpeg on request and must never be
reachable from outside the house (PLAN.md 3.2).

| Route | Purpose |
|---|---|
| `GET /healthz` | version, library path, queue depth per state, and the playback session |
| `GET /search?q=…&limit=…` | candidates; the only route that waits on the network |
| `POST /ingest` | queue a URL, returns a job — `201` new, `200` if that exact request is already queued |
| `GET /jobs/{id}` | state, progress, attempts, final path, tags, warnings |
| `GET /jobs?state=…&limit=…` | recent jobs, newest first |
| `DELETE /jobs/{id}` | cancel a job that has not started (`409` once it is running) |
| `GET /library?q=…&limit=…` | music files under the music root, newest first, paths relative to it |
| `GET /player` | transport state, volume, now playing, queue and outputs, in one response |
| `PUT /player/play`&nbsp;`pause`&nbsp;`toggle`&nbsp;`stop`&nbsp;`next`&nbsp;`previous` | transport; each answers with the new state |
| `PUT /player/volume` | `{"volume": 0–100}` or `{"step": ±n}`, exactly one |
| `PUT /player/seek` | `{"position_ms": n}` |
| `PUT /player/output/select` | enable the configured speaker (no id is taken from the caller) |
| `GET`&nbsp;`POST`&nbsp;`DELETE /player/queue` | list, add library files, or empty it |

```bash
T=$TUNEBOX_TOKEN
curl -s -H "X-Tunebox-Token: $T" 'http://127.0.0.1:8080/search?q=kraftwerk+autobahn&limit=5'

curl -s -H "X-Tunebox-Token: $T" -H 'Content-Type: application/json' \
     -d '{"url":"https://youtu.be/iukUMRlaBBE"}' http://127.0.0.1:8080/ingest
# -> {"id":"c0a53fe8fe970e0f","state":"queued",...}

curl -s -H "X-Tunebox-Token: $T" http://127.0.0.1:8080/jobs/c0a53fe8fe970e0f
# -> {"state":"done","path":"…/Kraftwerk/Singles/Autobahn (Single version 1974).m4a",…}
```

`POST /ingest` accepts the same overrides as the CLI (`artist`, `title`, `album`, `album_artist`,
`year`, `track_number`), plus `format` and `force`. Unknown fields are rejected with `422` rather
than ignored. Bad hosts are `400`, an upstream yt-dlp failure is `502`, a timeout `504`.

The queue is a SQLite file (`$TUNEBOX_MUSIC_ROOT/.tunebox/jobs.db` by default), and one worker
owns it: a second `serve` against the same database refuses to start rather than double the
download concurrency. The queue survives a restart — jobs left running by a crash are requeued
with their attempt already counted, and jobs out of attempts are failed rather than retried
forever.

Ingest is deduped by video id **and container**, so re-queueing a track you already have as m4a
costs no network request at all, while asking for the same track as mp3 still downloads. A repeat
`POST` returns the first job only when the whole request matches (same video, same format, same
overrides); otherwise it is a new job, so a correction is never silently dropped. `force` bypasses
all of it.

Timeouts come from the yt-dlp subprocesses (`TUNEBOX_INSPECT_TIMEOUT`, `TUNEBOX_FETCH_TIMEOUT`),
which are killed as a process group on expiry and surface as a retryable job failure. Behind
those, any job stuck `running` with no live thread is requeued automatically, so nothing can sit
in the queue forever.

## Playback

The **host** owns the playback session, not the phone. OwnTone holds the queue and casts to the
speaker; tunebox tells it what to do and keeps the right speaker selected. Music therefore keeps
playing when the phone sleeps, leaves, or dies — including the transition to the next track.

### Setting up OwnTone

OwnTone must be built with Chromecast support (`./configure --enable-chromecast`) and pointed at the
same directory as `TUNEBOX_MUSIC_ROOT`:

```
# owntone.conf
general {
    uid = "youruser"
    admin_password = "…"                 # see the security note below
    trusted_networks = { "localhost" }   # NOT the default
}
library {
    directories = { "/home/youruser/Music/tunebox" }
}
```

**Change `trusted_networks`.** OwnTone ships `{ "lan" }`, which skips its own password for every
device on the home network — a `curl -X PUT http://host:3689/api/player/stop` from anywhere in the
house works. Restricting it to `localhost` and setting `admin_password` means tunebox (on loopback)
still needs no credentials while everything else needs the password. The Basic-auth username is
`admin`; it is hard-coded in OwnTone and not configurable.

On macOS, `libinotify-kqueue` is a hard build requirement and needs
`./configure CFLAGS="-Wno-error=unguarded-availability-new"` on macOS 26. On Linux, neither applies.

### Driving it

```bash
tunebox player outputs            # every speaker the daemon can see
tunebox player select             # enable the configured one (TUNEBOX_OUTPUT)
tunebox player play FILE...       # replace the queue with these files and start
tunebox player play --add FILE    # append instead, without starting
tunebox player play               # resume whatever is loaded
tunebox player pause | stop | next | prev
tunebox player volume 40          # absolute, or a step: +5 / -10
tunebox player queue [--clear]
tunebox player status             # transport state, now playing, outputs
tunebox player rescan             # index new files (also automatic after every ingest)
```

`tunebox doctor` reports the player and the configured speaker as advisory `[--]` rows — they never
affect its exit code, because ingest works perfectly well on a host with no player installed.

Every successful ingest that produced a new file triggers a library rescan, so a freshly ingested
track becomes playable without waiting for a periodic scan. The rescan is *asynchronous* — OwnTone
returns as soon as it is queued — so "immediately" means seconds, not instantly. `tunebox player
play` on a file the daemon has never seen handles that for you: it rescans, waits for the scan to
finish, and only then reads a track id. It does those in that order for a reason — OwnTone's ids are
not stable *during* a scan, so resolving one first is what avoids a lost race.

**A rescan failure never fails an ingest.** With OwnTone stopped, a job still completes as `done`
with the file on disk and a warning attached, because the alternative is a retry that re-downloads
audio you already have. On a host with no player at all, that warning appears on every job — set
`TUNEBOX_RESCAN_AFTER_INGEST=0` to silence it.

### Keeping the speaker selected

`tunebox serve` runs a keeper that re-selects the configured output when it comes back — on startup,
and after the speaker or the daemon has been out of sight (unplugged, off Wi-Fi, OwnTone restarted).

It deliberately does **not** re-select a speaker you switched off yourself in OwnTone's web UI, and
that holds afterwards too: once it has seen you turn the speaker off, neither a power-cycle nor an
OwnTone restart switches it back on. The rule is *"assert selection when the speaker is visible and
off, and either we have never seen it, or it has been away since we last saw it and the last thing we
knew was that it should be on"*.

`GET /healthz` gains a `player` block reporting the session, the selected output and the queue
length. It reports `"reachable": false` with the reason when the daemon is down — and still answers
`200`, because a monitor should not see an outage because a soundbar is unplugged.

### What this does not do

The soundbar's **front-panel display shows no track title.** OwnTone's Chromecast output is
screen-mirroring: it streams Opus over RTP and sends no Cast media metadata at all, so there is
nothing to put a title into. It also means the host re-encodes AAC → Opus on every play, so playback
is not the zero-CPU passthrough that ingest is. Neither is configurable. PLAN.md Phase 0 has the
measurements and PLAN.md 7.11 has the open question about whether to fix it by driving Cast directly.

## The remote

One page, served by the API at `/`. Open `http://<host>:8080/` on the phone, paste the token once,
and use the browser's **Add to Home Screen** — from then on it is an icon that opens straight into
the app, with no address bar.

```bash
# Paste-once shortcut: the token in the URL *fragment*.
echo "http://192.168.50.10:8080/#token=$TUNEBOX_TOKEN"
```

The fragment specifically, never `?token=`: a fragment is not sent to the server, so the shared
secret stays out of uvicorn's access log — which is the audit trail worth having on a home network
(PLAN.md 3.2). The page removes it from the address bar as soon as it has read it and keeps it in
`localStorage`; **Forget token** in the footer removes it again.

Three panels:

- **Now playing** — title and artist of the current track, a progress bar, previous/play-pause/next/stop,
  a volume slider with ± buttons, the queue, and the state of the speaker. When the speaker is visible
  but switched off, a button appears to turn it back on; that is the one case the output keeper
  deliberately will not handle for you (see above).
- **Add music** — one field that does the right thing: a YouTube link or a bare 11-character video id
  is queued straight away, anything else is searched. Below it, every ingest with its progress,
  cancel while queued, and **Play** the moment it finishes.
- **Library** — everything under the music root, newest first, with a filter. Play replaces the queue
  and starts; Queue appends.

The footer links to **OwnTone's own web UI** for browsing by artist and album, playlists and artwork
— the expensive half of a music UI, which OwnTone already has and tunebox does not rebuild. The link
is built in the browser from the host you reached tunebox on plus the player's port, because OwnTone
listens on loopback and a link to `127.0.0.1:3689` would point the phone at itself. Set
`TUNEBOX_OWNTONE_UI` if the daemon is somewhere that guess cannot reach.

Everything the page does is the API above, so the same things work from a terminal:

```bash
T=$TUNEBOX_TOKEN
curl -s -H "X-Tunebox-Token: $T" http://127.0.0.1:8080/library
# -> {"tracks":[{"path":"Kraftwerk/Singles/Autobahn (Single version 1974).m4a",…}],"total":4,…}

curl -s -H "X-Tunebox-Token: $T" -H 'Content-Type: application/json' \
     -d '{"paths":["Kraftwerk/Singles/Autobahn (Single version 1974).m4a"],
          "mode":"replace","start":true}' \
     http://127.0.0.1:8080/player/queue
```

`paths` are relative to the music root (what `GET /library` hands out) or the absolute path a
finished job reports; either way the path is rebuilt and re-checked against the music root, so no
request body can name a file outside the library. A path outside it is `400`, not `500`.

### What is and is not exposed

The **app shell** — `/`, `/sw.js`, `/manifest.webmanifest` and `/ui/*` — is the only thing served
without the token, because a browser cannot put a header on the document request it makes on your
behalf. It carries no data: it is the same markup, CSS, script and icons that are in this repository,
and its first action is to ask for a token and check it against `/healthz`. Every route that can read
the library, queue an ingest or touch the player still requires it. `TUNEBOX_UI=0` removes the routes
and the exemption together.

Some deliberate absences, each with a test:

- **There is no pass-through to OwnTone.** tunebox holds the daemon's credentials and talks to it
  from a network it trusts, so a route that forwarded arbitrary paths would hand every caller the
  daemon's whole API — including one that switches *off* every speaker absent from its argument.
  What the remote may do is the fourteen routes listed above, all of them through the `Player` port.
- **Nothing on the page is fetched from the internet** — no CDN, no font, and deliberately not
  YouTube's search thumbnails. A remote that loaded images from Google on every search would make
  the house's own music browser phone home once per keystroke.
- **No inline script and no cookies**, which is what lets the page be served under
  `default-src 'none'; script-src 'self'` with no `unsafe-inline`. `form-action 'none'` is there so
  that a broken script cannot fall back to a native form submission and put the token in a URL.
- **The service worker caches the shell and does not intercept API requests at all.** A cached
  now-playing screen showing a track that stopped ten minutes ago, with a pause button that does
  nothing, is worse than no screen.

What the remote does **not** give you is lockscreen or hardware-volume-button control — the accepted
cost of the phone not being the audio path (PLAN.md 2).

## Deploying

```bash
./deploy/install.sh              # install or upgrade, then start
./deploy/install.sh --dry-run    # print every step, change nothing
./deploy/install.sh --uninstall   # stop and remove the service
```

One script for macOS and for Raspberry Pi OS / Debian. There is nothing to configure first, because
the three things a deployment normally has to decide are decided by looking:

- **the token** is generated (`secrets.token_urlsafe(32)`) and written to a mode-600 file;
- **the listen address** is read off the default route — `route get default` + `ipconfig` on macOS,
  `ip route get` on Linux — so the remote is reachable from the phone without anyone typing an IP.
  It is never `0.0.0.0`: this service runs yt-dlp and ffmpeg on request (PLAN.md 3.2). If no LAN
  address can be found it binds loopback and says so, rather than falling back to the wildcard;
- **the JavaScript runtime** is whichever of deno/node/quickjs/bun the host has, which is why no
  step of this asks what architecture the Pi is.

What it creates:

| Path | What |
|---|---|
| `~/.local/share/tunebox/venv` | the venv; `pip install`ed from the checkout, not editable, so the clone can be deleted afterwards |
| `~/.local/share/tunebox/bin/tunebox-serve` | wrapper that loads the env file and execs `tunebox serve` |
| `~/.config/tunebox/tunebox.env` | the configuration, mode 600 in a mode 700 directory |
| `~/Music/tunebox` | the library, unless `--music-root` says otherwise |
| `~/Library/LaunchAgents/io.tunebox.serve.plist` *(macOS)* | starts at login, restarts on failure |
| `/etc/systemd/system/tunebox.service` *(Linux)* | starts at boot, restarts on failure |

Both service managers exec the same wrapper rather than reading the env file themselves: launchd
cannot read one at all, and using systemd's `EnvironmentFile=` would leave two different answers to
"where does the configuration come from". One wrapper also means the service is reproducible by hand
— run `~/.local/share/tunebox/bin/tunebox-serve` and you get exactly what the supervisor gets.

The systemd unit is a **system** unit running as the invoking user, not `systemd --user`: a Pi is
headless and reboots unattended, and a user service would need `loginctl enable-linger` to come back
without somebody logging in. It carries the sandboxing that PLAN.md 3.2 asks for in prose —
`ProtectSystem=strict`, `ProtectHome=read-only`, `NoNewPrivileges`, `PrivateTmp` — with
`ReadWritePaths` set to the music root and nothing else.

**Re-running is the upgrade path**, and it is idempotent: the code is reinstalled, the config is left
alone. In particular the token is never regenerated, because that would silently log out every phone
in the house and the only symptom would be a token prompt.

`--uninstall` stops and removes the service and the venv. It keeps the configuration (`--purge` also
removes that) and it **never** touches the music: the library is the only irreplaceable state in the
system — tracks can be re-ingested, tags and playlists cannot.

Useful flags: `--music-root DIR` (on a Pi, point it at the USB SSD, not the SD card), `--host`,
`--port`, `--no-deps`, `--no-service`.

### Playback: pointing OwnTone at the library

The installer does not install OwnTone. That is the one step that genuinely differs per platform — a
source build on macOS, a package on Debian — and a script that guessed wrong would leave a half-built
daemon behind. Install it (see the [OwnTone
docs](https://owntone.github.io/owntone-server/installation/)), then:

```bash
./deploy/owntone-setup.sh            # configure, restart, find the speaker
./deploy/owntone-setup.sh --dry-run  # show the diff it would apply to owntone.conf
```

It makes three changes to `owntone.conf`, backing the file up first with a timestamped copy:

- `library.directories` → tunebox's music root, or the daemon never sees an ingested track;
- `general.trusted_networks` → `{ "localhost" }`. **This is the one that matters.** OwnTone ships
  `{ "lan" }`, which skips its own password for every device on the home network — measured:
  `curl -X PUT http://host:3689/api/player/stop` from anywhere in the house returns `204`. tunebox
  keeps working because it talks to loopback;
- `general.admin_password` → generated, and written into `tunebox.env` at the same time so the two
  cannot drift.

Then it finds the speaker and writes `TUNEBOX_OUTPUT_ID` into `tunebox.env` — the id rather than the
name, because an id is stable and a name is what somebody renames in the Google Home app. It waits
for the device to appear, since a Cast speaker takes a few seconds to turn up in mDNS after a restart.
With more than one candidate it lists them and stops rather than choosing.

### What was verified, and where

The installer was run end to end on the macOS host: service running under launchd, `/healthz` `200`
with the token and `/library` `401` without, listening on the LAN address specifically (`lsof` shows
`192.168.50.158:8080`, not `0.0.0.0`), config mode `600` in a `700` directory, a re-run leaving the
token byte-identical, and `--uninstall` leaving the music and config in place.

The **Linux path has not been run on a Pi.** What is tested is the code that generates it: the suite
puts `uname`/`systemctl`/`sudo` shims on `PATH` and runs the installer's Linux branch on macOS,
asserting the unit's hardening directives, its `ReadWritePaths`, `WantedBy=multi-user.target`, and
that the dependency on OwnTone is `Wants` and not `Requires`. Whether `apt-get` has the packages, and
whether OwnTone's Debian build enables Chromecast, cannot be established from here — the scripts
check and report rather than assume.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `TUNEBOX_MUSIC_ROOT` | `~/Music/tunebox` | library root; all writes are confined to it |
| `TUNEBOX_AUDIO_FORMAT` | `m4a` | `m4a` is AAC passthrough (verified on the soundbar); `mp3` re-encodes |
| `TUNEBOX_SEARCH_LIMIT` | `20` | default result count |
| `TUNEBOX_INSPECT_TIMEOUT` | `60` | seconds for metadata-only calls |
| `TUNEBOX_FETCH_TIMEOUT` | `600` | seconds for a download; expiry kills the whole yt-dlp process group |
| `TUNEBOX_SLEEP_REQUESTS` / `TUNEBOX_SLEEP_INTERVAL` / `TUNEBOX_MAX_SLEEP_INTERVAL` | `1` / `1` / `5` | politeness towards YouTube |
| `TUNEBOX_YTDLP` | `python -m yt_dlp` | override the yt-dlp invocation |
| `TUNEBOX_JS_RUNTIMES` | `node,quickjs,bun` | runtimes offered to yt-dlp *below* its deno default, so the first one installed wins. Empty means deno only |
| `TUNEBOX_TOKEN` | — | **required by `serve`**; shared secret for every route |
| `TUNEBOX_API_HOST` / `TUNEBOX_API_PORT` | `127.0.0.1` / `8080` | bind address; set the host to the LAN address |
| `TUNEBOX_ALLOW_ALL_INTERFACES` | `0` | opt-in for `0.0.0.0`; don't |
| `TUNEBOX_DB` | `$TUNEBOX_MUSIC_ROOT/.tunebox/jobs.db` | job queue and ingest history |
| `TUNEBOX_WORKERS` | `1` | concurrent ingests, capped at 2 on purpose |
| `TUNEBOX_JOB_ATTEMPTS` | `3` | attempts per job, including the first |
| `TUNEBOX_JOB_BACKOFF` / `TUNEBOX_JOB_BACKOFF_MAX` | `10` / `300` | exponential retry delay, in seconds |
| `TUNEBOX_JOB_POLL_INTERVAL` | `1` | how long an idle worker waits before re-checking for due retries |
| `TUNEBOX_PLAYER_URL` | `http://127.0.0.1:3689` | OwnTone's JSON API |
| `TUNEBOX_PLAYER_PASSWORD` | — | OwnTone's `admin_password`; unnecessary when talking to loopback, since `localhost` is trusted |
| `TUNEBOX_PLAYER_TIMEOUT` | `5` | seconds; a playback command that has not been acknowledged has failed |
| `TUNEBOX_OUTPUT` | `Living Room speaker` | speaker to keep selected, matched case-insensitively by name |
| `TUNEBOX_OUTPUT_ID` | — | select by the daemon's output id instead; wins over the name |
| `TUNEBOX_OUTPUT_VOLUME` | — | volume to set when first selecting; unset leaves whatever the speaker remembers |
| `TUNEBOX_OUTPUT_CHECK_INTERVAL` | `15` | seconds between selection checks; `0` disables the keeper |
| `TUNEBOX_RESCAN_AFTER_INGEST` | `1` | trigger a library rescan after each ingest that produced a new file |
| `TUNEBOX_UI` | `1` | serve the web remote; `0` removes the shell routes *and* their auth exemption |
| `TUNEBOX_OWNTONE_UI` | derived | absolute URL for the "full library in OwnTone" link; by default the browser joins its own hostname to the player's port |
| `TUNEBOX_LIBRARY_MAX_FILES` | `50000` | ceiling on the `GET /library` directory walk; the answer says when it was truncated |

## Tests

```bash
.venv/bin/python -m pytest
```

The suite never touches the network, and that is enforced rather than promised: an autouse fixture
in `tests/conftest.py` fails any test that opens a non-local connection or resolves a name. Source
metadata comes from recorded yt-dlp JSON in `tests/fixtures/`; re-record it deliberately with
`python tools/record_fixtures.py` when yt-dlp's output shape changes. The queue, the worker threads
and the FastAPI app are all exercised for real against a fake source (`tests/fakes.py`).
`tests/test_tagging.py` needs `ffmpeg` to synthesise a one-second audio file, and skips without it.

The OwnTone adapter is tested against a **real stub HTTP server on loopback**, not a mocked
`urllib`: half of what that adapter does is build query strings and read status codes, so a fake that
skipped the transport would not have caught the parts most likely to be wrong. The player is faked
everywhere else (`FakePlayer`), which also keeps the suite from re-selecting speakers on a
development machine that happens to be running OwnTone.

The web remote is tested from three sides, because the first two are not enough. The routes and the
auth exemption go through the real app (`tests/test_ui.py`); the assets are read as *files*, so a
CDN reference, a renamed icon, a missing manifest field or an inline script fails a test; and `deno`
— already a hard dependency, for yt-dlp-ejs — compiles `app.js` and `sw.js`, which is the only thing
in a Python suite that catches a typo in them. The markup's element ids and ARIA references are
cross-checked against what `app.js` looks up, because a renamed id is otherwise a `null` that
silently stops a button working. None of that substitutes for opening the page: the one bug in
Phase 4 that mattered was invisible to every DOM-level assertion and obvious in a browser
(PLAN.md Phase 4 has the story).

The deployment scripts are tested in `tests/test_deploy.py`, and the most useful test there runs the
**Linux** branch of the installer on macOS by putting `uname`/`systemctl`/`sudo` shims on `PATH` —
otherwise the systemd unit, the part a Pi depends on and a Mac never executes, would go unread until
the hardware existed. `shellcheck` runs over both scripts if it is installed, and skips if not. The
`owntone.conf` editor is exercised against realistic input, including the two cases that broke it
while it was being written: a setting that is commented out, and a value spanning several lines.

## Layout

```
src/tunebox/core/      domain: models, ports, naming and path safety — no I/O, no yt-dlp
src/tunebox/sources/   yt-dlp adapter: URL allowlist, subprocess calls, tag mapping
src/tunebox/ingest.py  the use case, wired from injected ports
src/tunebox/tagging.py mutagen tag writer — the authority on file metadata
src/tunebox/library.py atomic placement under the music root, and listing what is in it
src/tunebox/jobs/      SQLite job queue + the worker that drains it
src/tunebox/player/    OwnTone adapter, output keeper, rescan hook — behind the Player port
src/tunebox/api/       FastAPI app, token middleware, routes, the remote's routes, the app shell
src/tunebox/web/       the one page: hand-written HTML/CSS/JS, manifest, service worker, icons
src/tunebox/cli.py     search / ingest / doctor / serve / player
deploy/                install.sh (macOS + Raspberry Pi) and owntone-setup.sh
tools/make_icons.py    regenerates the PNG icons from the same drawing as icon.svg
```

Section 3.4 of PLAN.md explains why it is shaped this way. The `Player` port is the load-bearing
part of the Phase 3 layout: swapping OwnTone for a `pychromecast` player we drive ourselves is a new
sibling of `player/owntone.py` and no change anywhere above it.
