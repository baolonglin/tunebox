# tunebox

Personal, home-network-only music ingest on top of [yt-dlp](https://github.com/yt-dlp/yt-dlp).
Search for a track, pull it into a local library as a correctly tagged file, and let a music
daemon play it on the living-room soundbar. See [PLAN.md](PLAN.md) for scope, architecture and
the reasoning behind both — this README only covers running the code.

**Phase 1 (this code): CLI only.** No HTTP server, no job queue, no playback yet.

## Requirements

| Dependency | Why |
|---|---|
| Python 3.11+ | 3.11 is what current Raspberry Pi OS ships |
| `ffmpeg` + `ffprobe` | audio extraction and thumbnail conversion |
| `deno` (or another JS runtime) | with `yt-dlp-ejs`, required for YouTube signature deciphering — **without it downloads fail with HTTP 403** |

`yt-dlp`, `yt-dlp-ejs` and `mutagen` are pinned Python dependencies and install with the project.

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

### Configuration

| Variable | Default | Notes |
|---|---|---|
| `TUNEBOX_MUSIC_ROOT` | `~/Music/tunebox` | library root; all writes are confined to it |
| `TUNEBOX_AUDIO_FORMAT` | `m4a` | `m4a` is AAC passthrough (verified on the soundbar); `mp3` re-encodes |
| `TUNEBOX_SEARCH_LIMIT` | `20` | default result count |
| `TUNEBOX_INSPECT_TIMEOUT` | `60` | seconds for metadata-only calls |
| `TUNEBOX_FETCH_TIMEOUT` | `600` | seconds for a download |
| `TUNEBOX_SLEEP_REQUESTS` / `TUNEBOX_SLEEP_INTERVAL` / `TUNEBOX_MAX_SLEEP_INTERVAL` | `1` / `1` / `5` | politeness towards YouTube |
| `TUNEBOX_YTDLP` | `python -m yt_dlp` | override the yt-dlp invocation |

## Tests

```bash
.venv/bin/python -m pytest
```

The suite never touches the network. Source metadata comes from recorded yt-dlp JSON in
`tests/fixtures/`; re-record it deliberately with `python tools/record_fixtures.py` when yt-dlp's
output shape changes. `tests/test_tagging.py` needs `ffmpeg` to synthesise a one-second audio
file, and skips without it.

## Layout

```
src/tunebox/core/      domain: models, ports, naming and path safety — no I/O, no yt-dlp
src/tunebox/sources/   yt-dlp adapter: URL allowlist, subprocess calls, tag mapping
src/tunebox/ingest.py  the use case, wired from injected ports
src/tunebox/tagging.py mutagen tag writer — the authority on file metadata
src/tunebox/library.py atomic placement under the music root
src/tunebox/cli.py     search / ingest / doctor
```

Section 3.4 of PLAN.md explains why it is shaped this way.
