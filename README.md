# tunebox

Personal, home-network-only music ingest on top of [yt-dlp](https://github.com/yt-dlp/yt-dlp).
Search for a track, pull it into a local library as a correctly tagged file, and let a music
daemon play it on the living-room soundbar. See [PLAN.md](PLAN.md) for scope, architecture and
the reasoning behind both — this README only covers running the code.

**Phases 1 and 2 (this code): CLI and HTTP service.** Search, ingest, a job queue and an API.
No playback yet.

## Requirements

| Dependency | Why |
|---|---|
| Python 3.11+ | 3.11 is what current Raspberry Pi OS ships |
| `ffmpeg` + `ffprobe` | audio extraction and thumbnail conversion |
| `deno` (or another JS runtime) | with `yt-dlp-ejs`, required for YouTube signature deciphering — **without it downloads fail with HTTP 403** |

`yt-dlp`, `yt-dlp-ejs`, `mutagen`, `fastapi` and `uvicorn` are pinned Python dependencies and
install with the project.

## Install

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
which answer `401` rather than `404`. `serve` refuses to start without an ASCII token of at least
16 characters, and refuses to bind a wildcard address (`0.0.0.0`, `::`, and the other spellings
the socket layer accepts) unless `TUNEBOX_ALLOW_ALL_INTERFACES=1`: this service runs yt-dlp and
ffmpeg on request and must never be reachable from outside the house (PLAN.md 3.2).

| Route | Purpose |
|---|---|
| `GET /healthz` | version, library path, queue depth per state |
| `GET /search?q=…&limit=…` | candidates; the only route that waits on the network |
| `POST /ingest` | queue a URL, returns a job — `201` new, `200` if that exact request is already queued |
| `GET /jobs/{id}` | state, progress, attempts, final path, tags, warnings |
| `GET /jobs?state=…&limit=…` | recent jobs, newest first |
| `DELETE /jobs/{id}` | cancel a job that has not started (`409` once it is running) |

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

### Configuration

| Variable | Default | Notes |
|---|---|---|
| `TUNEBOX_MUSIC_ROOT` | `~/Music/tunebox` | library root; all writes are confined to it |
| `TUNEBOX_AUDIO_FORMAT` | `m4a` | `m4a` is AAC passthrough (verified on the soundbar); `mp3` re-encodes |
| `TUNEBOX_SEARCH_LIMIT` | `20` | default result count |
| `TUNEBOX_INSPECT_TIMEOUT` | `60` | seconds for metadata-only calls |
| `TUNEBOX_FETCH_TIMEOUT` | `600` | seconds for a download; expiry kills the whole yt-dlp process group |
| `TUNEBOX_SLEEP_REQUESTS` / `TUNEBOX_SLEEP_INTERVAL` / `TUNEBOX_MAX_SLEEP_INTERVAL` | `1` / `1` / `5` | politeness towards YouTube |
| `TUNEBOX_YTDLP` | `python -m yt_dlp` | override the yt-dlp invocation |
| `TUNEBOX_TOKEN` | — | **required by `serve`**; shared secret for every route |
| `TUNEBOX_API_HOST` / `TUNEBOX_API_PORT` | `127.0.0.1` / `8080` | bind address; set the host to the LAN address |
| `TUNEBOX_ALLOW_ALL_INTERFACES` | `0` | opt-in for `0.0.0.0`; don't |
| `TUNEBOX_DB` | `$TUNEBOX_MUSIC_ROOT/.tunebox/jobs.db` | job queue and ingest history |
| `TUNEBOX_WORKERS` | `1` | concurrent ingests, capped at 2 on purpose |
| `TUNEBOX_JOB_ATTEMPTS` | `3` | attempts per job, including the first |
| `TUNEBOX_JOB_BACKOFF` / `TUNEBOX_JOB_BACKOFF_MAX` | `10` / `300` | exponential retry delay, in seconds |
| `TUNEBOX_JOB_POLL_INTERVAL` | `1` | how long an idle worker waits before re-checking for due retries |

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

## Layout

```
src/tunebox/core/      domain: models, ports, naming and path safety — no I/O, no yt-dlp
src/tunebox/sources/   yt-dlp adapter: URL allowlist, subprocess calls, tag mapping
src/tunebox/ingest.py  the use case, wired from injected ports
src/tunebox/tagging.py mutagen tag writer — the authority on file metadata
src/tunebox/library.py atomic placement under the music root
src/tunebox/jobs/      SQLite job queue + the worker that drains it
src/tunebox/api/       FastAPI app, token middleware, routes
src/tunebox/cli.py     search / ingest / doctor / serve
```

Section 3.4 of PLAN.md explains why it is shaped this way.
