# tunebox

Personal, home-network-only music ingest on top of [yt-dlp](https://github.com/yt-dlp/yt-dlp).
Search for a track, pull it into a local library as a correctly tagged file, and let a music
daemon play it on the living-room soundbar. See [PLAN.md](PLAN.md) for scope, architecture and
the reasoning behind both — this README only covers running the code.

**Phases 1–4 (this code): CLI, HTTP service, host-owned playback, and one web remote.** Search,
ingest, a job queue, an API, playback on a Chromecast speaker driven by OwnTone, and a single
installable page that does all of it. The remote has **one device selector**: this device, plus
every speaker OwnTone can see. Everything on the screen then means whichever one you picked. A
shared speaker is the default, because the phone leaving should not stop the music — and **each
device keeps its own queue**, which is the whole design: see
[Where the music plays](#where-the-music-plays). One known
shortfall: the soundbar's own display stays blank, because OwnTone's Cast output is screen-mirroring
and carries no track metadata. See [PLAN.md](PLAN.md) Phase 0.

## Requirements

| Dependency | Why |
|---|---|
| Python 3.11+ | 3.11 is what current Raspberry Pi OS ships |
| `ffmpeg` + `ffprobe` | audio extraction and thumbnail conversion |
| a JavaScript runtime | with `yt-dlp-ejs`, required for YouTube signature deciphering — **without one, downloads fail with HTTP 403**. `deno` is best; `node` 22+, `quickjs` and `bun` also work |
| [OwnTone](https://owntone.github.io/owntone-server/) 29+, built `--enable-chromecast` | playback only. Ingest works fine without it |

`yt-dlp`, `yt-dlp-ejs`, `mutagen`, `fastapi`, `uvicorn` and `segno` are pinned Python dependencies
and install with the project. `deploy/install.sh` installs the rest.

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
| `DELETE /library` | delete tracks — `{"paths": [...]}`, all-or-nothing, and there is no trash |
| `POST /library/tickets` | capability URLs for streaming tracks to *this* device — `{"paths": [...]}` |
| `GET /media/{ticket}` | the audio itself, with byte ranges. **The one route the token does not guard**: the ticket is the credential |
| `GET /player` | transport state, volume, now playing, queue and outputs, in one response |
| `PUT /player/play`&nbsp;`pause`&nbsp;`toggle`&nbsp;`stop`&nbsp;`next`&nbsp;`previous` | transport; each answers with the new state |
| `PUT /player/volume` | `{"volume": 0–100}` or `{"step": ±n}`, exactly one |
| `PUT /player/seek` | `{"position_ms": n}` |
| `PUT /player/repeat` | `{"mode": "off" \| "all" \| "single"}` — nothing, the whole queue, or one song |
| `PUT /player/shuffle` | `{"enabled": true \| false}` |
| `PUT /player/output` | `{"id": "..."}` — switch to one of the daemon's outputs and switch the others off; `404` if the id is not currently reported, `403` if `TUNEBOX_OUTPUT_ALLOW` excludes it, `409` if it needs pairing |
| `PUT /player/output/select` | enable the *configured* speaker (no id taken from the caller); `503` if it is listed but will not start |
| `GET`&nbsp;`POST`&nbsp;`DELETE /player/queue` | list, add library files, or empty it |
| `PUT /player/queue/{id}/play` | jump to an item already in the queue — `404` if it has since gone |
| `GET`&nbsp;`POST /playlists` | list playlists, or create one — from library paths, from the queue, or empty |
| `GET`&nbsp;`DELETE /playlists/{name}` | one playlist and its entries; delete the list (no audio is touched) |
| `PATCH /playlists/{name}` | rename it, keeping its tracks |
| `POST`&nbsp;`DELETE /playlists/{name}/tracks` | append tracks, or remove them by `positions` or by `paths` |
| `PUT /playlists/{name}/tracks/{position}` | `{"to": n}` — move one entry, which is the whole reorder surface |
| `PUT /playlists/{name}/play` | replace the queue with a playlist and start, or append it |

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
    clear_queue_on_stop_disable = true   # keep the queue when the music ends
}
```

**Set `clear_queue_on_stop_disable`.** OwnTone's default is iTunes': stopping playback empties the
play queue, and reaching the end of the last track *is* stopping playback — so the list of songs on
the remote deletes itself at the exact moment somebody might want to play it again. With this set the
queue is only ever emptied on purpose, by the remote's clear button or OwnTone's own UI. It belongs in
`library`; `mpd` is the old spelling and 29.x warns about it.

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

### Waking a speaker that is asleep

A Cast device in standby is not the same thing as an absent one, and telling them apart is the
difference between a button that works and a button that lies. A soundbar in standby keeps
advertising itself over mDNS, so OwnTone lists it and the remote correctly shows it as visible but
off — and then the connection to its control port fails with `No route to host` and the enable comes
back `400`. Measured on this household's hardware: three consecutive enables failed that way, and an
attempt fifty-four seconds later established TLS and played, with nothing reconfigured in between.
**An mDNS advert is not proof of reachability.**

So `select_output` retries: `TUNEBOX_OUTPUT_SELECT_ATTEMPTS` attempts (3) with a doubling pause,
about four seconds in the worst case, which is why the remote's button says *Waking the speaker…*
while it waits. Between attempts the output list is re-read, because OwnTone answers a failed enable
and an unknown id with the same `400` and the same HTML body — so the only way to tell "asleep" from
"gone" is to ask, and a speaker that has genuinely left is then a `404` rather than three seconds of
pointless retrying. A speaker that never comes up is a `503` whose message is a sentence, not the
daemon's HTML error page.

**`GET /player` also reports `playing_without_output`**, and it is the one field worth knowing about:
when every speaker fails to start, OwnTone keeps reporting `state: play` and keeps advancing the
track position, so a remote that trusted the transport would draw a confident progress bar for a
silent room — measured, for three minutes. The page says *"Playing, but no speaker is attached"*
instead.

### Where the music plays

One selector, at the top of Now playing, listing every device this remote can drive:

```
Playing on:  ┌──────────────────────────────────┐
             │ This device                      │
             │   This device                    │
             │ Shared speakers                  │
             │   Living Room speaker · Chromecast
             │   MacBook Pro · AirPlay 2        │
             │   [LG] webOS TV · AirPlay 2      │
             └──────────────────────────────────┘
```

Everything else on the screen then means whichever one you picked: the transport, the volume, the
queue rows, repeat, shuffle, and Play and Queue over in the Library and Playlists panels. The choice
is remembered on that device and sent nowhere.

**Each device keeps its own queue, and that is the design rather than a limitation.** A queue belongs
to an output, not to a page:

| Device | Queue lives in | Who can change it | Why that is right |
|---|---|---|---|
| A shared speaker | OwnTone, on the host | anyone with the token | one speaker, one room. Somebody skipping a track they dislike is the feature — and the music keeps playing when your phone sleeps, leaves or dies |
| This device | that device's own storage | only that device | one pair of headphones. Nobody else gets to skip your song |

So two phones both set to *This device* are two independent players, and two phones both pointed at
the soundbar are two remotes for one player. An earlier draft of this had the two sharing a single
queue, which reads well until you follow it through: somebody pressing **Clear** in the kitchen would
empty what another person was listening to on headphones, and there is no sense in which that is
"changing the music in the room".

**Moving the music between them is a button, not a side effect.** *Continue on Living Room speaker*
(and, from a speaker, *Continue on this device*) copies the queue and the playback position across,
starts it there, and pauses the side you left — paused rather than stopped, so whoever was listening
in the room can press play and carry on from where it was. A copy somebody asked for cannot reach
into what a stranger is hearing; an implicitly shared list can.

Switching the selector to a speaker **enables that speaker and switches the others off**, because
that is what a single-choice control means. Multi-room is therefore not reachable from the remote;
OwnTone's own web UI still does it.

#### Playing on the device you are holding

A soundbar does not reach every room, somebody else is sometimes using it, and headphones exist. What
*This device* buys, and what it costs:

- **The lockscreen works.** Track title, artist, and the play/pause/skip controls on the lockscreen
  and on headphone buttons — via the Media Session API, which needs the audio to be coming out of
  the browser and therefore could never work on the OwnTone path. This is the one thing a local
  device can do that a shared speaker cannot.
- **The music stops when that device does.** Locked for long enough, out of Wi-Fi range, battery
  dead, tab closed: the audio was coming out of it. The panel says so rather than leaving it to be
  discovered. This is the exact property PLAN.md 2 chose the host-owned session to avoid, which is
  why a shared speaker is still the default.
- **It needs no OwnTone at all.** Its queue was never the daemon's, so a host with no player
  installed still plays music on the phone in your hand. The selector simply says *"No speakers
  visible"*.
- **The volume slider does not work on iOS.** `HTMLMediaElement.volume` is read-only there, and
  Apple does not say so — assignments are silently ignored. So the page detects it, disables the
  slider and the ± buttons, and says *"use the phone's own volume buttons"*. Nothing is more
  annoying than a control that looks like it worked.
- **The queue survives closing the app**, because it is in that device's storage. The playback
  *position* does not: resuming a track to the second is a promise this cannot keep.

#### Which speakers the remote may use

`PUT /player/output` takes an id, which is a deliberate change of posture: for as long as there was
one configured speaker, the selection route took no id at all, because accepting one could only add
risk. A selector listing the speakers in the house cannot work that way, so the two things that made
the original refusal right are kept as checks instead:

- **the id must be one the daemon is currently reporting.** Anything else is a `404` from tunebox and
  is never forwarded. That earns its place beyond tidiness: OwnTone answers an unknown id and a
  speaker that will not wake with the same `400` and the same HTML body, so without the check "you
  typed that wrong" and "the soundbar is asleep" would be one error;
- **it must pass `TUNEBOX_OUTPUT_ALLOW`.** mDNS does not stop at the front door — the daemon lists a
  neighbour's Chromecast exactly like your own — and an allowlist is the only thing that can turn
  "this remote must not control other people's devices" from an intention into a rule. Unrestricted
  by default, because every installation that predates the selector has one speaker. Set it to the
  ids or names you own:

```bash
TUNEBOX_OUTPUT_ALLOW='Living Room speaker,2345877078'
```

An excluded speaker is listed and **greyed out** rather than hidden, because "that speaker exists and
is not yours to use" is a different statement from "there is no such speaker". `GET /player` reports
`selectable` per output, so the page greys out exactly what the route would refuse instead of
offering it and reporting a `403` after the tap. The configured speaker is always allowed, even if an
allowlist forgets it — otherwise `TUNEBOX_OUTPUT` and `TUNEBOX_OUTPUT_ALLOW` could disagree, and the
symptom would be the output keeper selecting a speaker the remote refuses to show as selectable.

#### A speaker that will not play, and why it says which kind it is

Not every output OwnTone lists can be used, and the three ways one fails need three different
answers. Getting this wrong is worse than saying nothing: the first version of the selector answered
an AirPlay failure with Cast prose ending in *"try again in a moment"*, for a failure that would
never clear.

| What happened | What you get | What to do |
|---|---|---|
| it left the network | `404` naming what is visible instead | plug it in, or pick something else |
| it is listed and will not wake | `503`, retried three times first | a Cast speaker in standby does this; try again in a moment |
| it wants a PIN or a password | `409`, **not retried at all** | pair it once in OwnTone's own web UI |

The third one is the one worth knowing about, because an AirPlay 2 receiver is the common case and
retrying it is four seconds spent on something that cannot change. Measured on this host against
OwnTone 29.3 — a Mac advertising itself as an AirPlay receiver:

```
airplay: Status flags from 'MacBook Pro' was 516: ... one time pairing 1 ...
airplay: Starting device pairing for 'MacBook Pro', go to the web interface and enter PIN
airplay: Response to PIN start from 'MacBook Pro' was negative, aborting (403 Forbidden)
player: The AirPlay 2 device 'MacBook Pro' requires a valid PIN or password
```

**tunebox cannot do that pairing**, and that is the same decision as everywhere else: it is `POST
/api/outputs/{id}/verification` on the daemon, and there is no pass-through to OwnTone (PLAN.md 3.2).
So the honest answer is to name the speaker and point at the daemon's own web UI, which the footer
already links to. Once paired, OwnTone stops reporting `requires_auth` and the speaker becomes
selectable on the next poll with nothing to clear here.

`requires_auth` is **discovered, not known**: the same Mac reported `false` until the first enable
attempt failed and `true` from then on. So an output that is not flagged is not a promise that it
will work — it is only a promise that nothing has found out otherwise yet. The selector greys out
and labels *(needs pairing in OwnTone)* the ones the daemon has learned about, which is why the
first tap on a new speaker can still produce the `409`.

Choosing a speaker from the remote also **tells the output keeper**, so that picking a different one
is not quietly undone fifteen seconds later. The keeper would have got there on its own — a
configured speaker that is visible, off and did not go anywhere is already recorded as somebody's
decision — but not before the next check, and inside that window a speaker that power-cycles is one
it would switch back on top of the choice you just made.

#### How the audio gets there, and why there is a token-free route

`<audio src="…">` is a request the *browser* makes, and a page cannot put a header on it. So the
one route that carries audio cannot be guarded by `X-Tunebox-Token` like everything else. The three
ways out of that are a cookie, the token in a query string, and a capability URL, and only the third
fits what this project already decided:

- **a cookie** would be attached by the browser to requests a third-party page could cause, which is
  the one property the header token was chosen for;
- **`?token=`** would write the master credential into uvicorn's access log once per track, and
  keeping it out of that log is why the remote carries it in a URL *fragment* and strips it on
  arrival (PLAN.md 3.2);
- **a capability URL** is what `tunebox.api.auth`'s `exempt_prefixes` was reserved for back in
  Phase 3 — "the capability URL a Cast-pulled `/media/{id}` would need". The Cast path never needed
  it; this one does.

```bash
T=$TUNEBOX_TOKEN
curl -s -H "X-Tunebox-Token: $T" -H 'Content-Type: application/json' \
     -d '{"paths":["Kraftwerk/Singles/Autobahn (Single version 1974).m4a"]}' \
     http://127.0.0.1:8080/library/tickets
# -> {"tickets":[{"path":"Kraftwerk/Singles/Autobahn (Single version 1974).m4a",
#                 "url":"/media/eyJlIjoxNzkyNzE4OTQwLCJwIjoiS3JhZnR3ZXJrL1Npbmds…",
#                 "content_type":"audio/mp4"}],"expires_in":21600}

curl -s -r 0-1 -D - -o /dev/null http://127.0.0.1:8080/media/eyJlIjoxNzky…   # no token
# HTTP/1.1 206 Partial Content
# content-range: bytes 0-1/4194304
```

A ticket is an HMAC over the library path and an expiry, keyed by a value derived from
`TUNEBOX_TOKEN`. What makes it acceptable in a log line is everything it is *not*:

- **it names one file**, signed, so it cannot be re-pointed at another — and the path is still
  re-resolved through `library.resolve_track_path` on every request, so a ticket is never trusted to
  have been minted for something sane. A bug in the minting route cannot become a file-read
  primitive;
- **it expires** (`TUNEBOX_MEDIA_TICKET_TTL`, six hours — longer than any queue this household
  builds, shorter than the gap to the next time anyone is in the room);
- **it is read-only**, and the response carries `nosniff` and a `default-src 'none'` CSP of its own
  so it cannot be anything but bytes;
- **it dies with the token.** Rotating `TUNEBOX_TOKEN` invalidates every outstanding ticket, with no
  state anywhere to clear — there is no table of tickets, deliberately: it would be a second
  database (PLAN.md 3.1), it would need pruning, and it would not survive the restart an upgrade
  performs mid-song.
- **the minting route is not inside the exempt prefix.** `POST /library/tickets` requires the token
  like everything else, and lives under `/library/` rather than `/media/` precisely because
  `/media/` is the prefix that is exempt. A route that handed out capability URLs to anyone who
  asked would be a capability worth nothing.

`TUNEBOX_LOCAL_PLAYBACK=0` removes the `/media/` routes **and** their auth exemption together, the
same way `TUNEBOX_UI=0` does for the shell — so a household that only ever plays on the soundbar can
run a service with no token-free way to read a byte of audio.

**Byte ranges are implemented, not inherited**, and that is not gold-plating: iOS Safari opens a
track by asking for `bytes=0-1` and will not play a reply that comes back `200` with the whole file
instead of `206`. Seeking is a range request on every browser there is. A multi-range request is
answered with the whole file, which RFC 9110 permits and which is much better than the pile of code
a correct `multipart/byteranges` body would be.

### What this does not do

The soundbar's **front-panel display shows no track title.** OwnTone's Chromecast output is
screen-mirroring: it streams Opus over RTP and sends no Cast media metadata at all, so there is
nothing to put a title into. It also means the host re-encodes AAC → Opus on every play, so playback
is not the zero-CPU passthrough that ingest is. Neither is configurable. PLAN.md Phase 0 has the
measurements and PLAN.md 7.11 has the open question about whether to fix it by driving Cast directly.

## Playlists

**A playlist here is a file, not a table.** `$TUNEBOX_MUSIC_ROOT/Playlists/Saturday morning.m3u`,
written by tunebox, read by OwnTone — and by Navidrome or MPD if either ever gets pointed at the same
tree. The easiest way to make one is to build the queue you want and then save it:

```bash
tunebox playlist create "Road trip" FILE...       # from library files, in order
tunebox playlist create "Right now" --from-queue  # whatever is playing, saved
tunebox playlist create "Later" --empty           # name it now, fill it later
tunebox playlist list                             # names and track counts
tunebox playlist show "Road trip"                 # its entries, with positions
tunebox playlist add "Road trip" FILE...          # append
tunebox playlist remove "Road trip" FILE...       # every entry naming those tracks
tunebox playlist remove "Road trip" --position 2  # exactly that entry
tunebox playlist move "Road trip" 3 0             # make the fourth track first
tunebox playlist rename "Road trip" "Long drive"  # keeps the tracks
tunebox playlist play "Road trip"                 # replace the queue and start
tunebox playlist play "Road trip" --add           # append it instead
tunebox playlist delete "Road trip"               # the list only; no audio is touched
```

Positions are zero-based and are what `show` prints, because a playlist may hold the same track twice
and may hold an entry that resolves to nothing — so a path does not name a line and an index into the
*playable* tracks does not either.

On the phone it is the **Playlists** tab:

- **Making one.** Type a name, then **Save the queue** (what is playing right now) or **Start empty**.
  Either way the new row opens itself. Filing a single track is done where the track is — the Library
  row's **⋯** holds *Add to playlist*.
- **Seeing inside one.** Tap the name. The row expands into its entries, numbered, with the artist and
  album under each — and any entry whose file is missing marked in red saying so.
- **Changing one.** Each entry has **↑ ↓** to move it and **✕** to take it out. No confirmation on the
  ✕: it removes a line of text and the track stays on disk, and a strip there would teach people to tap
  through the one in the Library panel that does delete a file. **⋯** on the playlist holds **Rename**
  and the delete confirmation.

Why a file, rather than OwnTone's own playlist API: **the daemon will not write one without a config
change.** `owntone.conf` ships `allow_modifying_stored_playlists = false` with an empty
`default_playlist_directory`, the same shape of locked-down default as `trusted_networks`. Writing the
M3U ourselves turns out to be the better half of that trade anyway:

- **No new database.** The filesystem is the library database (PLAN.md 3.1), so a playlist is backed
  up by `tunebox backup`, survives `tunebox restore`, and is portable — with no new code in any of
  those three places.
- **OwnTone's write side stays shut.**
- **Playback needs no new daemon integration.** tunebox reads the M3U, resolves the entries and hands
  the paths to the same `enqueue_paths` the Library panel uses. Building on
  `uris=library:playlist:N` would have put the daemon's own playlist ids through the `Player` port and
  broken the drop-in property that port exists for (PLAN.md 3.4) — so the fallback player gets
  playlists for free.
- **The write path already existed.** Every entry goes through `library.resolve_track_path`, the one
  function that refuses `..`, a symlink out of the tree and a non-audio file, so a playlist cannot
  name a file outside the music root — which is the whole risk of accepting a list of paths from a
  phone.

What is in the file, and why:

```
#EXTM3U
# tunebox playlist: Road trip
../Kraftwerk/Autobahn/01 - Autobahn.m4a
../陳奕迅/Singles/K歌之王.m4a
```

- **Entries are relative to the playlist file**, which is what M3U means everywhere and what survives
  the library moving — the symlinked external SSD, and a restore to a different path. Because the
  playlists live in a subdirectory, that means `../`, and OwnTone resolves it: measured on this host
  against OwnTone 29.3, a playlist tunebox wrote into `Playlists/` was indexed as
  `{"type": "plain", "item_count": 1}` with its `../Artist/Singles/Track.m4a` entry resolving to the
  real track. An absolute entry is *read* and never written, so a hand-written file plays.
- **No `#EXTINF`** written — and none removed. Writing one would be a second copy of each track's
  metadata, going stale the moment somebody retags a file, to tell players something they read from the
  tags anyway. But a playlist another program wrote is carried across line for line by an append or a
  prune, so its `#EXTINF`s survive; the one exception is deliberate, in that pruning an entry takes its
  own `#EXTINF` with it rather than leaving it attached to the next track.
- **UTF-8**, and `.m3u` is not in `library.AUDIO_EXTENSIONS` — so playlists never appear in
  `GET /library` and cannot be enqueued as if they were songs, with no code to exclude them.

Details worth knowing:

- **An entry is identified by its position, not its path.** The same track may legitimately be in a
  playlist twice, and an entry may resolve to nothing at all, so neither a path nor an index into the
  playable tracks names a line. `GET /playlists/{name}` reports `entries` with a `position` each, and
  that is what a removal or a move takes. Removal by `paths` also exists, and means "every copy".
- **The name is the filename**, so *creating* one sanitises it exactly as a track name is sanitised:
  `Road trip: 2026/07` becomes one component, `Road trip_ 2026_07`. The create response says the name
  it used, which is the one to put in the next URL. Every other verb takes the name **verbatim** and
  refuses one that is not already a usable filename, rather than sanitising it too — otherwise
  `a:b` and `a_b` would address the same file and a delete could remove the wrong playlist. The price
  is that a `.m3u` somebody hand-named with a `:` in it is listed and cannot be played or deleted
  through the API; renaming it on the host is the fix.
- **A visible `Playlists/` directory, not a hidden one.** It is a pseudo-artist in OwnTone's browse
  view, which is where it is expected; a dot-directory would not work at all, because every music
  scanner skips dot-entries — the same rule that keeps `.tunebox-tmp` out of the library. `tunebox
  serve` refuses a `TUNEBOX_PLAYLISTS_DIR` starting with a dot rather than writing files nothing will
  ever index.
- **Deleting a track removes its entry from every playlist that named it**, and `DELETE /library`
  reports which in `playlists_updated`. The pruning happens *before* the unlink, which is what makes
  it exact: while the file still exists, every entry can be resolved and matched by *inode* rather
  than by comparing text — so `../A/SINGLES/one.m4a` on a case-insensitive filesystem, the NFD
  spelling of a CJK filename and a hard link are all recognised, while an entry that was already
  dangling for some unrelated reason is left exactly where it is. Without this, OwnTone would skip the
  dead entry silently and the playlist would be one track shorter forever with nothing having
  mentioned it.
- **An entry that resolves to nothing is reported, not dropped.** It keeps its position, the row on
  the phone says *"8 tracks · 2 missing"* and shows the entry in red when expanded — so it can be taken
  out from the phone, which a path-based removal could never name. `tunebox playlist play` says so on
  stderr. A list that plays eight of its ten tracks in silence is this feature's most likely
  failure, and it is the one OwnTone will not mention.
- **Two phones editing one playlist is last-write-wins.** Every edit is a read-modify-write. The
  *write* is atomic — staged beside the target under a dot-name, then `os.replace` — so a scanner
  never reads half a list and a crash cannot truncate one; it is only the merge that is not attempted,
  deliberately, at one household's scale.
- **Reorder and rename are tunebox's own, because nothing else can do them.** The same
  `allow_modifying_stored_playlists = false` that stops the daemon writing a playlist stops its web UI
  editing one — so "reorder it over there" was never true, and the first version of this feature shipped
  with no editing at all on that false assumption. Moving is one entry at a time
  (`PUT /playlists/{name}/tracks/{position}`) rather than sending a whole new order, for the reason
  `PUT /player/repeat` takes a mode rather than a cycle: two phones can be looking at one list, and an
  order computed from a two-second-old reading would silently undo whatever the other one just did.
- **No nesting, and no multi-select.** A playlist is one flat file. Building one by picking twenty
  tracks at once is the queue's job — fill it, then **Save the queue**.

## The remote

One page, served by the API at `/`. Open `http://<host>:8080/` on the phone, paste the token once,
and use the browser's **Add to Home Screen** — from then on it is an icon that opens straight into
the app, with no address bar.

Nobody should be thumb-typing a 43-character token, so `tunebox pair` prints the link as a QR code.
Point the phone's camera at it and the paste never happens. `deploy/install.sh` shows the same code
at the end of a successful install.

```bash
tunebox pair            # QR code, plus the link underneath it
tunebox pair --no-qr    # just the link, for AirDrop or a password manager
```

Both refuse to write to anything but a terminal — the code and the link are each the credential
itself, and redirecting a command into a log file is a normal thing to do. `--force` overrides it.

**Adding it to the home screen needs the token once more, and that is not a bug.** The page strips
the token from the address bar the moment it reads it, so the shortcut captures a clean `/`; and iOS
gives an installed web app its own storage container, so it cannot see what Safari stored. The
installed icon therefore starts at the token gate. **Show token** in the footer reveals it as
selectable text while you are unlocked in Safari — long-press, copy, open the icon, paste, once per
device. The gate input is `type="password"` with `autocomplete="current-password"`, so a password
manager fills it instead if you have one.

On Android the shortcut is only ever a shortcut: `http://` on a LAN address is not a secure context,
so Chrome will not install it as a standalone app and `navigator.serviceWorker` does not exist, which
costs the offline cache and nothing else. A real HTTPS origin — Tailscale is the clean route — is the
only fix.

The token travels in the URL fragment, never `?token=`: a fragment is not sent to the server, so the
shared secret stays out of uvicorn's access log — which is the audit trail worth having on a home
network (PLAN.md 3.2). The page removes it from the address bar as soon as it has read it and keeps
it in `localStorage`; **Forget token** in the footer removes it again.

Four panels:

- **Now playing** — the **device selector** (this device, plus every speaker OwnTone can see — see
  [Where the music plays](#where-the-music-plays)), the title and artist of the current
  track, a progress bar, previous/play-pause/next/stop,
  a volume slider with ± buttons, a **repeat** button that cycles off → the queue → one song, a
  **shuffle** toggle, the queue, and the state of the chosen device. Every one of those means
  whichever device is selected, and each device has its own queue. **Tapping a track in the queue plays
  it** — the row is the button, so track seven is one tap and not six presses of ⏭; the row already
  playing restarts it, and its label says so. When the speaker is visible
  but switched off, a button appears to turn it back on; that is the one case the output keeper
  deliberately will not handle for you (see above), and it says *Waking the speaker…* while it waits,
  because a Cast device in standby takes a few seconds to answer. If audio is going nowhere at all —
  the daemon playing with no speaker attached — the panel says so outright rather than leaving a
  moving progress bar to imply otherwise. `consume` — the mode where the queue eats itself
  — is reported by `GET /player` but has no control, deliberately.
- **Add music** — one field that does the right thing: a YouTube link or a bare 11-character video id
  is queued straight away, anything else is searched. Below it, every ingest with its progress,
  cancel while queued, and **Play** the moment it finishes.
- **Library** — everything under the music root, newest first, with a filter. Play replaces the queue
  and starts; Queue appends. **⋯** on a row opens *Add to playlist* and, below it, a confirmation
  strip that names the track and then deletes it — see below, because that one is not undoable from
  the phone. The listing is re-read whenever you open the tab and whenever an ingest finishes, so a
  track that says "done" above is already here.
- **Playlists** — make one from the queue or empty, then **Play** (replace and start) or **Queue**
  (append). **Tap the name to see what is in it**: the row expands into numbered entries with **↑ ↓**
  to reorder and **✕** to remove, and a missing file says so in red rather than being a number in a
  subtitle. **⋯** holds *Rename* and the delete confirmation, and deletes no audio. See
  [Playlists](#playlists).

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

### Deleting from the library

```bash
curl -s -X DELETE -H "X-Tunebox-Token: $T" -H 'Content-Type: application/json' \
     -d '{"paths":["Kraftwerk/Singles/Autobahn (Single version 1974).m4a"]}' \
     http://127.0.0.1:8080/library
# -> {"deleted":["Kraftwerk/Singles/Autobahn (Single version 1974).m4a"],
#     "removed_directories":["Kraftwerk/Singles","Kraftwerk"],
#     "playlists_updated":["Playlists/Road trip.m3u"],"warnings":[]}
```

**This unlinks the file. There is no trash directory, and the remote cannot put it back** — the
backup is the undo, which is why `TUNEBOX_BACKUP_PRUNE` is off by default and why the two settings
belong together in your head: with pruning on, tonight's backup forgets the track too.

Details worth knowing before you point it at an album:

- **Directories that empty are removed with it**, upward, stopping at the music root and only while
  they are genuinely empty — a stray `cover.jpg` is enough to keep one. They are reported in
  `removed_directories`. Without this, deleting the last track of an artist leaves a directory that
  `GET /library` never shows (it walks files) and OwnTone's browse view does, as a phantom artist.
- **The track the player currently has loaded is refused with `409`.** Skip past it or stop, then
  delete. Tracks further down the queue are deleted without complaint: OwnTone finds out on its next
  load, and a queue is not a claim on the library.
- **All-or-nothing.** Every path is resolved before anything is unlinked, so a typo in the third entry
  is a `400` that deleted nothing. The same path is accepted in either spelling, and naming one file
  twice is one delete.
- **OwnTone is told to re-index afterwards**, and a daemon that is down cannot fail the delete — the
  file is gone either way, so it comes back as a string in `warnings`.
- **Playlists that named the track lose the entry**, and they are listed in `playlists_updated`. See
  [Playlists](#playlists) for why that is pruned rather than left dangling, and why it happens before
  the unlink rather than after.
- There is no CLI verb for this: on the host, `rm` is the tool. The API exists because a phone has no
  terminal, which is the whole reason the remote was a one-way door before it.

In the Library panel this lives behind the row's **⋯** and then behind a button naming the track,
deliberately not as a third button beside Play — the two live where a thumb lands.

### After an upgrade, one reload

`./deploy/install.sh` restarts the service, and the page picks up the new version on its next load —
including a home-screen icon, which does a fresh navigation when you reopen it. Nothing has to be
cleared by hand: the document, the manifest and the service worker are served `no-cache`, and
`/ui/app.js` and `/ui/app.css` are requested with `?v=<build id>`, where the build id is a digest of
those files rather than the release version. Change an asset and the URL changes with it, so a browser
cannot be holding a stale copy of it — which is a real trap rather than a hypothetical: keying the
query on `__version__` meant an upgrade between releases left phones running the previous script for
an hour while the host served the new one.

### What is and is not exposed

**Two things are served without the token, and no more.**

The **app shell** — `/`, `/sw.js`, `/manifest.webmanifest` and `/ui/*` — because a browser cannot put
a header on the document request it makes on your behalf. It carries no data: it is the same markup,
CSS, script and icons that are in this repository, and its first action is to ask for a token and
check it against `/healthz`. Every route that can read the library, queue an ingest or touch the
player still requires it. `TUNEBOX_UI=0` removes the routes and the exemption together.

**`GET /media/{ticket}`** — because `<audio src>` is a request the browser makes, and a page cannot
put a header on that either. The ticket in the path is the credential: signed, expiring, scoped to
one file and read-only, and the route that *mints* one requires the token like everything else. See
[How the audio gets there](#how-the-audio-gets-there-and-why-there-is-a-token-free-route) for the
full reasoning. `TUNEBOX_LOCAL_PLAYBACK=0` removes the routes and the exemption together.

The two are different shapes on purpose: the shell is a list of **exact paths**, because `/` as a
prefix would exempt the whole API, while `/media/` is a **prefix**, because the credential is in the
path. `tunebox.api.auth` refuses `/` or an empty string as a prefix outright rather than trusting a
future caller to notice the difference.

Some deliberate absences, each with a test:

- **There is no pass-through to OwnTone.** tunebox holds the daemon's credentials and talks to it
  from a network it trusts, so a route that forwarded arbitrary paths would hand every caller the
  daemon's whole API — including one that switches *off* every speaker absent from its argument.
  What the remote may do is the `/player/*` routes listed above, all of them through the
  `Player` port — including the one that takes an output id, which is checked against the daemon's
  live list and against `TUNEBOX_OUTPUT_ALLOW` here rather than forwarded. Validation is on this side too, and not only as good manners: `state=bogus` on
  OwnTone's own repeat and shuffle routes is answered `204` and silently taken as "off", so a mode
  outside `off|all|single` is a `422` here rather than a success that changed the mode. The one
  route that takes an id in its path checks the queue for it first, so an id that is no longer
  there is a `404` rather than the daemon's own `400`-with-an-HTML-body.
- **There is no `DELETE /library/{path}`**, and the one destructive route takes no shortcuts with the
  path it is given: it goes through the same resolver `POST /player/queue` uses, so traversal, a
  symlink out of the tree and a non-audio file are all refused there rather than filtered here. The
  confirmation for it lives in the UI, where somebody is holding the phone; an API that asked twice
  would be a protocol every client eventually skips.
- **Nothing on the page is fetched from the internet** — no CDN, no font, and deliberately not
  YouTube's search thumbnails. A remote that loaded images from Google on every search would make
  the house's own music browser phone home once per keystroke.- **No inline script and no cookies**, which is what lets the page be served under
  `default-src 'none'; script-src 'self'` with no `unsafe-inline`. `form-action 'none'` is there so
  that a broken script cannot fall back to a native form submission and put the token in a URL.
- **The service worker caches the shell and does not intercept API requests at all.** A cached
  now-playing screen showing a track that stopped ten minutes ago, with a pause button that does
  nothing, is worse than no screen.

What the remote does **not** give you on the house-speaker path is lockscreen or
hardware-volume-button control — the accepted cost of the phone not being the audio path (PLAN.md
2). Select **This device** and both work, because then it is: see
[Where the music plays](#where-the-music-plays).

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
| `~/.local/share/tunebox/bin/tunebox-maintain` | the same, for the nightly `tunebox maintain` |
| `~/.config/tunebox/tunebox.env` | the configuration, mode 600 in a mode 700 directory |
| `~/Music/tunebox` | the library, unless `--music-root` says otherwise |
| `~/Music/tunebox/.tunebox/` | queue database, ops state (`ops.json`), health log (`health.jsonl`) |
| `~/Library/LaunchAgents/io.tunebox.serve.plist` *(macOS)* | starts at login, restarts on failure |
| `~/Library/LaunchAgents/io.tunebox.maintain.plist` *(macOS)* | nightly at 04:17; `RunAtLoad` is false |
| `/etc/systemd/system/tunebox.service` *(Linux)* | starts at boot, restarts on failure |
| `/etc/systemd/system/tunebox-maintain.{service,timer}` *(Linux)* | nightly, `Persistent=true` so a machine that was off still runs it |

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
`--port`, `--no-deps`, `--no-service`, `--no-maintenance`.

### Playback: pointing OwnTone at the library

The installer does not install OwnTone. That is the one step that genuinely differs per platform — a
source build on macOS, a package on Debian — and a script that guessed wrong would leave a half-built
daemon behind. Install it (see the [OwnTone
docs](https://owntone.github.io/owntone-server/installation/)), then:

```bash
./deploy/owntone-setup.sh            # configure, supervise, find the speaker
./deploy/owntone-setup.sh --dry-run  # show the diff it would apply to owntone.conf
./deploy/owntone-setup.sh --supervise  # just start it and keep it started; change no config
```

It makes four changes to `owntone.conf`, backing the file up first with a timestamped copy:

- `library.directories` → tunebox's music root, or the daemon never sees an ingested track;
- `library.clear_queue_on_stop_disable` → `true`, so the queue outlives the music. OwnTone's default
  clears it when playback stops, and the end of the last track counts — see
  [Setting up OwnTone](#setting-up-owntone);
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

**Starting it, on macOS.** One script, and it is the one to reach for:

```bash
./deploy/start.sh            # start both daemons, then check the speaker really answers
./deploy/start.sh status     # what is running; changes nothing
./deploy/start.sh stop       # stop both
./deploy/start.sh restart
```

It hard-codes this host's answers — OwnTone as a source build under `~/owntone_data`, tunebox as its
launchd agent — so nothing has to be remembered or discovered. `deploy/owntone-setup.sh` is the
portable version; `start.sh` is the one somebody runs.

It also **verifies the Cast session rather than the exit code**, which is the whole reason it exists.
`owntone-setup.sh --supervise` registers a launchd agent (`io.tunebox.owntone`) so the daemon starts
at login, and **on macOS 26 that agent cannot reach the speaker.** macOS gates local-network access
per process, a launchd agent gets no consent dialog and is simply denied, and the denial does not look
like one: mDNS discovery still works, because that goes through mDNSResponder out of process, so
OwnTone *finds* the speaker and lists it — and then the direct connection fails with

```
cast: Could not connect to Living Room speaker
misc: Could not connect to 'Chomecast control' at 192.168.50.13 (port 8009): No route to host
```

while `nc -z 192.168.50.13 8009` from a shell succeeds. `GET /player` reports `state: play`, the
progress bar advances, and no sound comes out of anything. Measured on this host, both ways round:
started from a terminal the daemon inherits that terminal's grant and the log says `cast: Session
ready`; started by launchd it does not.

So `start.sh` starts OwnTone **from your session** and confirms afterwards that the speaker answered.
The cost is that nothing restarts it after a reboot — running the script is the restart. To get
supervision back, grant the permission by hand in System Settings → Privacy & Security → Local
Network, then use `owntone-setup.sh --supervise`; `start.sh status` flags the case where the daemon is
supervised and therefore probably mute, because nothing else will.

`--supervise` is otherwise the flag for a host that is already configured: it starts the daemon under
launchd and re-checks the speaker, leaving `owntone.conf` and the generated password alone. Re-running
the whole script instead would rotate the password and write another backup for nothing.

On Debian none of the above applies — there is no local-network gate, the daemon has a packaged
systemd unit, and `sudo systemctl enable --now owntone` is the whole of it.

```bash
tunebox player status   # is the speaker there?
tunebox doctor          # is everything there?
```

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

## Keeping it alive

Six commands, and one timer that runs them. YouTube changes things, disks die, and both failures are
silent by default — the point of everything in this section is that the house finds out before
somebody reaches for their phone and nothing happens.

```bash
tunebox exposure      # is anything reachable from outside the house?
tunebox update        # update yt-dlp, canary it, roll back if that made things worse
tunebox healthcheck   # the daily check: one synthetic ingest, plus disk, library, queue, player
tunebox backup        # mirror the library, and verify that it arrived
tunebox restore       # bring it back (a dry run unless --yes)
tunebox maintain      # what the nightly timer runs: update, check, back up, alert once
```

Each takes `--json`, and each exits non-zero **only for a failure** — never for an "unknown". That is
a deliberate choice about human behaviour: several of these questions genuinely cannot be answered
(no UPnP device replied; the service is not running, so nothing is listening), and a check that
exited 1 for those is a check that gets commented out of the nightly job within a fortnight.

### The canary

Everything else here hangs off one operation: a real download of one known video ("Me at the zoo",
19 seconds, the least likely video on the platform to be deleted or region-locked). It **downloads
media on purpose**. A metadata-only check would be cheaper and nearly worthless — the failure this
project has already measured is that metadata extraction keeps working perfectly while the media
request answers `403` because no JavaScript runtime is available, so a canary that stopped short of
the media stream would have passed cheerfully on the host where every real ingest failed.

It also checks the bytes rather than the exit code: the file must be big enough to be audio, and
ffprobe must agree that it *is* audio of roughly the promised length. yt-dlp and ffmpeg can produce an
empty or truncated container and still exit 0. Nothing is written to the library — the canary fetches
into the scratch directory and deletes it afterwards.

### Updating yt-dlp

`tunebox update` runs the canary **twice**, before and after, and that is the whole design. The
obvious version — update, canary, roll back on failure — does the wrong thing in the case that
matters most:

| canary before | canary after | what happens |
|---|---|---|
| pass | pass | keep it, and record it as the known-good version |
| pass | **fail** | **roll back**, remember the bad version, and re-canary to prove the rollback worked |
| **fail** | pass | keep it — the update *fixed* extraction, and the report says so |
| **fail** | **fail** | keep the **new** version and alert. The old one is no better, and yt-dlp's next fix will be built on the newer extractor |

Row two is why the bad version is remembered: without that, a broken release is installed, canaried,
rolled back — and installed again tomorrow, forever, with an alert every night. Row four is what the
obvious design gets wrong; it would revert the release that was the cure.

`TUNEBOX_YTDLP_CHANNEL=nightly` switches to yt-dlp's nightly builds (published to PyPI as
pre-releases, so this is just `pip install --pre`). Stable is the default: the canary makes an update
*safe*, not *free*, and a household that is not currently broken gains nothing from being a day
ahead. Nightly is the right setting *while* extraction is broken.

The command refuses to `pip install` outside a virtualenv unless given `--allow-system`. On Debian —
which is what a Pi runs — that is blocked by PEP 668, and forcing it is a way to break `apt`.

One interaction worth knowing about: `pyproject.toml` pins yt-dlp deliberately, so re-running the
installer would *downgrade* a version this command installed and verified. The installer therefore
finishes with `tunebox update --reapply`, which reinstalls the recorded known-good version and needs
no network.

### Backups, and the restore nobody usually tests

The library is the only irreplaceable state in the system. The audio can be downloaded again; the
`--parse-metadata` corrections, the artist names worked out from a CJK title, and the layout somebody
rearranged by hand cannot. So set this and nothing else will need thinking about:

```bash
TUNEBOX_BACKUP_DEST=/Volumes/Backup/tunebox   # or pi@nas:/srv/music
```

`tunebox backup` mirrors the tree with rsync and then **verifies by asking rsync again**: a second
`--dry-run --itemize-changes` pass with nothing left to send is a fact, whereas "rsync exited 0" is
not — rsync exits 0 after skipping files it could not read. The destination is a plain readable tree
rather than an archive, because the day this is needed the recovery must not depend on this program
still working.

`--delete` is **opt-in** (`--prune`, or `TUNEBOX_BACKUP_PRUNE=1`). The accident a home backup most
often has to survive is somebody deleting an album, and `--delete` propagates exactly that within a
day — which is now more than a hypothetical, because the remote can delete tracks
([Deleting from the library](#deleting-from-the-library)) and this mirror is the only undo it has.
The live SQLite queue is excluded and a consistent snapshot sent in its place — copying an open
WAL-mode database byte-wise is how a backup that restores into a corrupt database gets made. A
destination on the same filesystem as the library is allowed and warned about: it protects against an
accidental delete and does nothing whatsoever about the disk dying.

```bash
tunebox backup --status     # age, file count, and the exact restore command
tunebox restore             # dry run: shows every file it would write
tunebox restore --yes       # do it
```

`restore` never passes `--delete` under any flag: a restore that removed files the backup does not
know about would turn "get my album back" into "lose everything ingested since the last backup".

### The daily check, and being told

`tunebox healthcheck` runs the canary and then the things that fail slowly: free space on the music
volume, the track count, the age of the last backup, failed jobs in the queue, whether OwnTone is
answering and the speaker is selected, and the cheap half of `tunebox exposure`. Two of those deserve
a note:

- **The player is advisory.** Ingest works with no player installed, so a missing OwnTone is a
  warning. Failing the nightly job for it would teach whoever reads the alert to ignore it.
- **An empty library is a warning, not a shrug.** If an external disk is not mounted, the mount point
  is usually still a perfectly writable directory — so ingest "works", the library appears empty, and
  music quietly accumulates on the wrong disk. Free space and track count are reported together for
  that reason.

Every run appends one JSON object to `.tunebox/health.jsonl` (rotated at 1 MB, because this runs on an
SD card), so the whole history is one `jq` away. `tunebox healthcheck --history 20` prints it.

Alerts go to whichever of these is configured, and to the log regardless — under launchd and systemd
stderr *is* the log file:

```bash
TUNEBOX_ALERT_COMMAND='/usr/local/bin/terminal-notifier -title tunebox -message'
TUNEBOX_ALERT_WEBHOOK=https://ntfy.sh/your-private-topic
```

The command gets the JSON report on **stdin** and a one-line subject as its **last argument**, which
is enough for `mail`, `terminal-notifier`, `ntfy` or a three-line script with no wrapper. It is split
with `shlex` and executed as an argv list, never through a shell — so a pipeline has to be written as
`sh -c '…'` on purpose. The webhook is the only outbound request tunebox makes to a third party, it
happens only because somebody configured it, and the payload carries hostnames, versions, counts and
error strings — never the token.

Alerting is throttled: it fires on the **transition** into failure, repeats an unchanged failure at
most once a day (`TUNEBOX_ALERT_REPEAT_HOURS`), and sends a **recovery** notice. An identical message
every night is a message that gets filtered, and the filter is still in place the night something new
breaks.

### Checking nothing is exposed

```bash
tunebox exposure              # reads the socket table and asks the router over UPnP
tunebox exposure --no-router  # skip the multicast, finish instantly
```

Three questions, three kinds of answer. What tunebox is *configured* to bind (already enforced at
startup). What is *actually* listening, from the kernel's socket table via `lsof`/`ss` — which is the
part that catches a listener nobody configured, because intent and reality can disagree. And whether
the router forwards anything here, over UPnP IGD.

That last answer is genuinely three-valued, and conflating the cases would miss the point: mappings
found is a fact, no mappings is a fact, and *the router did not answer* is neither — it is consistent
with UPnP being switched off, which is what we want, and equally consistent with a router that simply
ignores us. So the command reports `unknown` there and always prints the two checks a human still has
to do: look at the port-forwarding page once (a rule typed in by hand is invisible to UPnP), and
confirm UPnP itself is off.

**It only ever reads.** `GetGenericPortMappingEntry` and `GetExternalIPAddress`, never
`AddPortMapping` and specifically never `DeletePortMapping` — a forwarding rule is shared household
infrastructure and might be somebody's console or a colleague's VPN, so an audit tool that silently
rearranged the router would be more dangerous than the thing it audits. A test asserts it.

Measured on this network: LAN bind, no wildcard listener for tunebox, and the router reporting one UDP
mapping to a set-top box and none to this host. OwnTone shows up as a warning: it binds every
interface and cannot be told not to, which is why `deploy/owntone-setup.sh` closes the *authorisation*
half instead.

### What the nightly job does, and why it is one job

`tunebox maintain` is what the launchd agent and the systemd timer run, at 04:17. One timer rather
than three, for two reasons: the order matters and a scheduler cannot express it (the update must
precede the day's canary, or the canary tests yesterday's yt-dlp; the backup must follow both, so what
is copied is a library whose ingest path is known to work), and one bad night should produce one
alert rather than three. The steps are otherwise independent — a step that throws becomes a failed
finding instead of cancelling the ones after it, because the backup protects the only irreplaceable
thing here and must still run on a night when the updater crashed.

`TUNEBOX_AUTO_UPDATE=0` makes it a pure health check, which is right on a host whose venv is managed
by something else. Run it by hand any time:
`~/.local/share/tunebox/bin/tunebox-maintain`.

The systemd timer carries `Persistent=true` — a Pi is unplugged without ceremony, and without that a
missed nightly job is simply skipped. Its unit's `ReadWritePaths` is wider than the service's by the
venv — pip has to be able to install into it, and the long-running service must not be able to
rewrite its own code — plus the backup destination when that is a local path. Each is a separate,
quoted directive, because systemd splits an unquoted value on whitespace and "Backup Disk" is an
ordinary name for an external disk.

### What is verified, and what is not

Verified on this host: a real canary download (309 156 bytes, ffprobe 19.06 s against the 19 s the
metadata promised); the health check reporting disk, library, queue and `OwnTone 29.3` with the
speaker selected; a real backup of the live library including CJK paths, its verification pass, its
status and a restore dry run; `tunebox update --dry-run` recording the known-good version after a
passing canary; and the UPnP audit against the real router.

Not verified: **no rollback has ever been triggered by a real bad yt-dlp release** — that path runs on
every test run against a fake pip, which proves the logic and not the packaging. Nothing has run on a
Pi. The alert channels are tested through injected transports, so whether that ntfy topic actually
arrives on the phone is unanswered. And the timers have not been observed *firing*; that takes a
night, not a test.

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
| `TUNEBOX_OUTPUT_SELECT_ATTEMPTS` | `3` | tries per "turn the speaker on"; a Cast device in standby refuses the first connection and accepts a later one |
| `TUNEBOX_OUTPUT_ALLOW` | — | ids or names, comma-separated, the device selector may switch to. Empty means every speaker the daemon can see — **including a neighbour's**, since mDNS does not stop at the front door. The configured output is always allowed |
| `TUNEBOX_RESCAN_AFTER_INGEST` | `1` | trigger a library rescan after each ingest that produced a new file |
| `TUNEBOX_UI` | `1` | serve the web remote; `0` removes the shell routes *and* their auth exemption |
| `TUNEBOX_LOCAL_PLAYBACK` | `1` | offer **This device** in the selector; `0` removes the `/media/` routes *and* their auth exemption, leaving no token-free way to read audio |
| `TUNEBOX_MEDIA_TICKET_TTL` | `21600` | seconds a streaming capability URL stays valid. Six hours: longer than an evening's queue, short enough that the URL in an access log stops being useful |
| `TUNEBOX_OWNTONE_UI` | derived | absolute URL for the "full library in OwnTone" link; by default the browser joins its own hostname to the player's port |
| `TUNEBOX_LIBRARY_MAX_FILES` | `50000` | ceiling on the `GET /library` directory walk; the answer says when it was truncated |
| `TUNEBOX_PLAYLISTS_DIR` | `Playlists` | directory under the music root holding the `.m3u` files. One name, not a path, and never starting with a dot — a music scanner skips dot-entries, so the playlists would never be indexed |
| `TUNEBOX_CANARY_URL` | "Me at the zoo" | the video the canary downloads; pick something tiny and permanent |
| `TUNEBOX_YTDLP_CHANNEL` | `stable` | or `nightly` (pip `--pre`); nightly is for while extraction is broken |
| `TUNEBOX_AUTO_UPDATE` | `1` | `0` makes `tunebox maintain` a pure health check and never touches the venv |
| `TUNEBOX_BACKUP_DEST` | — | **set this.** A path on another disk, or anything rsync understands (`pi@nas:/srv/music`) |
| `TUNEBOX_BACKUP_PRUNE` | `0` | let the backup delete what the library no longer has; off, so a deleted album survives — and so the backup stays the undo for `DELETE /library` |
| `TUNEBOX_BACKUP_MAX_AGE_HOURS` | `48` | older than this and the health check fails. Two days, so one failed night does not page anybody |
| `TUNEBOX_MIN_FREE_GB` | `5` | free space on the music volume below which the health check fails; half of it warns |
| `TUNEBOX_ALERT_COMMAND` | — | argv (split with `shlex`, no shell); gets the JSON report on stdin and the subject last |
| `TUNEBOX_ALERT_WEBHOOK` | — | URL to POST the JSON report to; never contains the token |
| `TUNEBOX_ALERT_REPEAT_HOURS` | `24` | how often an *unchanged* failure is repeated; `0` means every run |
| `TUNEBOX_HEALTH_LOG_MAX_BYTES` | `1000000` | size at which `health.jsonl` rotates to `.1`; `0` disables the log |

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

Playback on the device you are holding is tested in `tests/test_media.py`, and the two tests there
worth knowing about run the **queue state machine and the device selector under deno**. The capability tickets are pure, so they are tested
directly and hard — forging a signature, editing the path or the expiry inside a valid ticket, and
reusing one signed with a different token all have a test, as does the defence-in-depth property that
matters most: a ticket minted *with the server's own key* for `../../../etc/passwd` is still refused,
because the path is re-resolved on every request rather than trusted because a signature matched.
Byte ranges get their own class, including the `bytes=0-1` probe iOS Safari opens a track with and
the `bytes=-16` suffix form that is easy to implement backwards. And the browser half — "what does ⏭
do on the last track with repeat off", "does turning shuffle on skip the song that is playing" — is
a question with a *behaviour* for an answer, so those functions are lifted out of `app.js` and run
under deno against a stub audio element, with `Math.random` replaced by a fixed sequence so that
"shuffled" is checkable rather than merely different.

The Phase 5 machinery is tested by injecting every piece of I/O, which is not a style preference: a
canary that could only be tested by downloading from YouTube, or a rollback that could only be tested
by installing packages, would be a rollback nobody had ever run — precisely the failure those features
exist to prevent. So `tests/test_update.py` drives all four rows of the update decision table
against a fake pip, `tests/test_exposure.py` drives a fake UPnP router that answers with forwarded
ports, and `tests/test_backup.py` runs **real rsync** on temporary directories, in both directions,
because what matters there is that the files actually arrive and the restore actually restores.

The deployment scripts are tested in `tests/test_deploy.py`, and the most useful test there runs the
**Linux** branch of the installer on macOS by putting `uname`/`systemctl`/`sudo` shims on `PATH` —
otherwise the systemd unit, the part a Pi depends on and a Mac never executes, would go unread until
the hardware existed. `shellcheck` runs over both scripts if it is installed, and skips if not. The
`owntone.conf` editor is exercised against realistic input, including the two cases that broke it
while it was being written: a setting that is commented out, and a value spanning several lines. The
launchd plists are written for real by running the installer's own functions in isolation against a
`launchctl` shim, since a `--dry-run` cannot show the contents of a file it does not write and the
contents are the whole point.

1276 tests at the end of Phase 5; 1562 with playlists; 1742 with playback on the phone.

## Layout

```
src/tunebox/core/      domain: models, ports, naming, path safety and media capability tickets —
                       no I/O, no yt-dlp
src/tunebox/sources/   yt-dlp adapter: URL allowlist, subprocess calls, tag mapping
src/tunebox/ingest.py  the use case, wired from injected ports
src/tunebox/tagging.py mutagen tag writer — the authority on file metadata
src/tunebox/library.py atomic placement under the music root, and listing what is in it
src/tunebox/playlists.py the M3U reader/writer — a playlist is a file in the library, not a table
src/tunebox/jobs/      SQLite job queue + the worker that drains it
src/tunebox/player/    OwnTone adapter, output keeper, rescan hook — behind the Player port
src/tunebox/api/       FastAPI app, token middleware, routes, the remote's routes, the app shell,
                       and media.py — the capability-URL audio route the phone output streams from
src/tunebox/web/       the one page: hand-written HTML/CSS/JS, manifest, service worker, icons
src/tunebox/ops/       Phase 5: canary, yt-dlp update + rollback, backup/restore, health, alerts,
                       exposure audit, and the one nightly job that composes them. Nothing in the
                       request path imports it, so a bug here cannot break ingest or playback
src/tunebox/cli.py     search / ingest / doctor / serve / player / exposure / update / healthcheck /
                       backup / restore / maintain
deploy/                install.sh (macOS + Raspberry Pi), owntone-setup.sh, and start.sh for this Mac
tools/make_icons.py    regenerates the PNG icons from the same drawing as icon.svg
```

Section 3.4 of PLAN.md explains why it is shaped this way. The `Player` port is the load-bearing
part of the Phase 3 layout: swapping OwnTone for a `pychromecast` player we drive ourselves is a new
sibling of `player/owntone.py` and no change anywhere above it.
