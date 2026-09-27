# Tunebox — Plan

Home audio library built on top of yt-dlp.
Goal: from a phone on the home Wi-Fi, search for a track and get it into a personal library;
an always-on host plays it on the living-room LG SN9YG soundbar over Chromecast.
The phone is a remote control and nothing more — it is never in the audio path, and music
continues when it sleeps or leaves.

Scope is deliberately small: **one household, one home network, listened to at home.**
Nothing is reachable from the internet. This is not a product and is not intended to
become one.

`tunebox` is a placeholder name. This document is a decision record, not a spec — it exists
so we choose an architecture before writing code.

Status: **Phases 1, 2 and 3 implemented (2026-09-26/27); language and code architecture decided
(3.4).** Scope and architecture decided. The soundbar, the Cast transport and the audio format are
**verified working on hardware** (see Phase 0); ingest produces a correctly tagged AAC file from
the CLI, and from an authenticated HTTP request queued on a job worker. **The Phase 0 blocker is
cleared: OwnTone does drive this soundbar over Chromecast, measured 2026-09-27, and the host now
plays music with no phone involved.**

One acceptance criterion did not survive contact with the hardware: **the soundbar's display stays
blank, because OwnTone's Cast output is screen-mirroring and carries no track metadata.** That is not
a bug in tunebox and cannot be configured away — it is the reason fallback A (`pychromecast`) is
still on the table, now as a fix for a specific measured shortfall rather than as insurance. The same
measurement removed a security requirement (3.2's capability URLs are inapplicable: the soundbar never
fetches from us) and added a cost nobody predicted (every play is re-encoded to Opus on the host).

---

## 0. Constraints that shape everything

### 0.1 Do not modify or contribute to the yt-dlp repo

The yt-dlp clone at `../yt-dlp` contains `.NO_AI/README.md`, which forbids LLM/agent
involvement in any contribution to that project. Consequences for this plan:

- yt-dlp is consumed as an **external dependency** (PyPI `yt-dlp`, or the released binary).
  Never vendored, never patched in place.
- No upstream issues or PRs from this work.
- If we hit a yt-dlp bug, we work around it in our code, or a human reports it upstream
  in their own words without AI assistance.

This costs us nothing: yt-dlp is Unlicense (public domain), and its documented embedding
API (`yt_dlp.YoutubeDL`) plus CLI are stable enough to build on.

### 0.2 Legal and policy reality — read before choosing scope

| Scope | Risk | Notes |
|---|---|---|
| Single user, own hardware, own listening | Low, but still breaches YouTube ToS | Private, no distribution, no public endpoint |
| Family / handful of invited users | Moderate | Becomes "distribution" in some jurisdictions |
| Public signup, hosted, monetised | **High** | This is a stream-ripping service. Rights holders (RIAA/IFPI et al.) have sued and shut these down; Google actively blocks them |

Additional hard facts:

- **App stores reject YouTube downloaders.** Google Play and the App Store both remove them.
  A native app of this kind ships via F-Droid, sideloading, or TestFlight only.
- **Extraction breaks constantly.** YouTube changes things; yt-dlp ships nightly builds for
  this reason. Any product built on it needs a continuous-update story or it dies quietly.
- **Datacenter IPs get bot-checked.** A VPS is far more likely to be challenged than a home
  connection. This pushes toward self-hosting.

**Resolved:** the target is the top row — personal, self-hosted, home network only. One
listener, own hardware, no signup, no remote access, no second household. Alternatives D
and E stay in this document as recorded reasoning, not as roadmap.

The "datacenter IPs get bot-checked" bullet stops being a preference and becomes a property
of the design: the only place this ever runs is a domestic connection.

### 0.3 Technical dependencies yt-dlp needs for YouTube audio

Per yt-dlp's own README:

- `ffmpeg` + `ffprobe` — required for audio extraction/conversion and metadata embedding.
- `yt-dlp-ejs` + a JS runtime (`deno` recommended) — required for *full* YouTube support
  (signature/n-param deciphering). Without it, formats may be missing or throttled.
  **Measured 2026-09-26: this is stronger than "may".** With `deno` installed but `yt-dlp-ejs`
  absent, extraction and thumbnail download succeed and the audio download then dies with
  `HTTP Error 403: Forbidden`. Both pieces are mandatory, and the failure does not name the
  missing dependency — see Phase 1.
- Possibly cookies and/or PO tokens for some videos; expect rate limiting.
- `--sleep-requests` / `--sleep-interval` (or the `-t sleep` preset) to behave politely.

Any container image or host we build must include these, not just the Python package.

### 0.4 Home-only — what that constraint buys and costs

Listening happens at home, on the home network. This removes work:

- **No VPN needed for the core path.** Bind the API and the player daemon to the LAN interface
  only.
  Tailscale drops from "required" (as it was in the first draft) to "optional convenience" —
  worth adding only if you want to queue a track while out and find it waiting when you're
  home. Ingest-from-anywhere is a nice-to-have; play-from-anywhere is explicitly not a goal.
- **No TLS or certificate management.** Plain HTTP over the LAN is acceptable for a service
  whose worst-case leak is "which songs you downloaded". If that changes, Tailscale brings
  its own transport encryption without certificates.
- **Offline caching stops being load-bearing.** It was the mitigation for "home box offline
  while away"; if you're never away while listening, the box being up *is* the requirement.
  Still nice for the client to cache, but it no longer decides which client we pick.
- **The home box can be small.** No concurrent users, no burst load. A Raspberry Pi 4/5 is
  sufficient — which reinforces the m4a/opus passthrough decision in 3.1: a Pi re-encoding
  to MP3 is slow and pointless heat.

What it costs: the box becomes a single point of failure for all listening, and it must be
always-on. That is an acceptable trade for a device that sits next to the soundbar anyway.

One thing home-only does **not** buy: the LAN is not an authentication boundary. This service
runs yt-dlp and ffmpeg on request, and a home network usually contains at least one IoT
device nobody is proud of. See 3.2.

---

## 1. Alternatives considered

### Alternative A — Self-hosted server + PWA (build our own UI)

yt-dlp runs on a home box (NAS, Raspberry Pi, spare Mac). FastAPI + job queue behind it.
A Progressive Web App gives search, an ingest button, a library view, and an HTML5 audio
player. Phone connects to the soundbar over normal Bluetooth A2DP — the OS handles that;
the app is just an audio player.

- **Pros:** full control of UX; one codebase; no app store; home IP avoids bot checks.
- **Cons:** we build and maintain a whole player UI. iOS PWA background audio and lockscreen
  controls are limited — `MediaSession` helps but Safari PWA backgrounding is unreliable.
  This is the part most likely to disappoint in daily use.

### Alternative B — yt-dlp as an ingest worker feeding a music server (recommended)

Same self-hosted backend, but the library is owned by **Navidrome** (or Jellyfin). Our code
is only: search + ingest + tag + drop file into the music folder + trigger rescan.
Playback comes from mature existing mobile clients that speak the Subsonic API —
Symfonium, substreamer, Amperfy, Tempo.

- **Pros:** we do not build a player. Get offline caching, gapless playback, lockscreen and
  Bluetooth metadata (AVRCP track titles on the soundbar display), playlists, scrobbling,
  multi-user — all for free, all already battle-tested on phones. Our surface area shrinks to
  the one thing that actually needs building. Clean separation: if yt-dlp breaks, the library
  still plays.
- **Cons:** search/ingest lives in a separate small web UI from playback, so two apps on the
  phone (a PWA for "get this track", a native client for listening). Slightly less integrated.
- **Mitigation for the two-app seam:** the ingest PWA can be a single screen — search box,
  results, one tap to queue. Ingest is occasional; playback is constant. Optimise for playback.
- **Update:** the seam is gone entirely. Section 2 moves playback to the host, so there is no
  native client and no second app — one web page does search, ingest, and transport. The
  Subsonic-client ecosystem is retained only as a documented fallback.

### Alternative C — Native/React Native app with on-device extraction

Bundle extraction into the app (`youtubedl-android`, or Python via Chaquopy/BeeWare).
No server at all.

- **Pros:** nothing to host; works on mobile data anywhere; no home-network dependency.
- **Cons:** no app store distribution (see 0.2). Shipping yt-dlp updates to a sideloaded app
  is painful, and this codebase needs updating often. Phone does the transcoding — battery
  and thermals. Mobile IPs get challenged. Highest effort, most fragile.

### Alternative D — Multi-tenant hosted SaaS

Public signup, shared infrastructure, rate limiting, abuse controls, payments.

- **Pros:** this is what "product" usually means.
- **Cons:** the high-risk row of the table in 0.2. Also needs residential-proxy infrastructure
  to survive Google's blocking, which is itself a ToS-evasion posture. Not recommended.

### Alternative E — Legitimate catalogue, same product shape

Identical architecture to B, but sources are licence-clean: podcast RSS, Free Music Archive,
Jamendo, ccMixter, Bandcamp purchases, YouTube content you own, the user's own files. For
mainstream music, integrate a licensed streaming SDK (Spotify/Apple Music) instead of ripping.

- **Pros:** shippable as a real product. App stores accept it. No takedown risk.
- **Cons:** catalogue is the entire value proposition and it's the weakest part. Doesn't
  satisfy "search any song on YouTube."
- **Worth noting:** the ingest pipeline in B is source-agnostic. Building B for personal use
  keeps the door to E open — swapping the extractor is a contained change if we keep the
  source adapter behind an interface.

---

## 2. Recommendation

**Alternative B**, personal/self-hosted, with the source layer abstracted so E stays reachable.
**The always-on host owns playback; the phone is only a remote control.**

Rationale: the hardest part of this problem is not downloading — yt-dlp already solved that.
It is owning a playback session that does not depend on a phone being present, awake, and in
the room. So the host runs the player and drives the soundbar directly over Chromecast — the
only network audio transport this soundbar supports; the phone talks plain HTTP to the host and
nothing else. We build search and ingest, plus a thin remote UI over an existing player daemon.

**Superseded:** earlier drafts had the phone run a Subsonic client that casts to the soundbar.
That was a holdover from assuming the phone was the audio source, and it carried two problems.
First, it needs two apps on the phone — the "two-app seam" listed as B's main drawback above.
Second, it is only *partly* phone-independent: with Cast the receiver fetches the audio, so the
current track survives the phone sleeping, but the cast *session* belongs to the phone app and
queue advancement is generally sender-driven, so killing the app tends to produce silence at
the end of the track. Host-owned playback removes both problems and makes the iOS-vs-Android
question mostly irrelevant, since a browser remote behaves the same on either.

What we give up by not using a native Subsonic client: lockscreen and hardware-volume-button
control of playback, and offline caching. The second does not matter at home. The first is a
real, small, daily cost — accepted.

---

## 3. Architecture (Alternative B)

```
              Home network — nothing exposed to the internet
┌──────────────────────────────────────────────────────────────────────┐
│                                                                      │
│   Phone — thin remote            Always-on host (Pi / Mac / NAS)     │
│   ┌──────────────────┐           ┌───────────────────────────────┐   │
│   │ tunebox web UI   │           │  tunebox API (FastAPI)        │   │
│   │                  │           │   /search  /ingest  /jobs     │   │
│   │  search          │──HTTP────▶│   /play /pause /next /volume  │   │
│   │  ingest          │           │         │          │          │   │
│   │  now playing     │◀──HTTP────│         ▼          │          │   │
│   │  + transport     │  state    │   SQLite job queue │          │   │
│   └──────────────────┘           │         │          │          │   │
│                                  │         ▼          │          │   │
│    one page, one icon,           │   worker: yt-dlp   │          │   │
│    no native app                 │        ─▶ tag      │          │   │
│                                  │         │          │          │   │
│                                  │         ▼          ▼          │   │
│                                  │  /music/…/Track.m4a           │   │
│                                  │         │                     │   │
│                                  │         ▼                     │   │
│                                  │   player daemon (OwnTone)     │   │
│                                  │   owns the queue + session    │   │
│                                  └─────────────┬─────────────────┘   │
│                                                │                     │
│                                                │ Chromecast /        │
│   ┌──────────────────┐                         │ Chromecast, over LAN│
│   │ LG SN9YG         │◀───── control 8009 ─────┘                     │
│   │ Chromecast only  │────── pulls /media ────▶ (host HTTP)          │
│   │ (no AirPlay)     │   the phone is never in the audio path        │
│   └──────────────────┘                                               │
└──────────────────────────────────────────────────────────────────────┘
```

The host holds the playback session. The phone can sleep, leave, or run out of battery and the
music keeps playing; any other phone or a laptop browser can pick up the same remote.
Fallbacks if the player daemon disappoints: phone → Bluetooth → soundbar, or a Subsonic client
casting (see 3.1 **Playback**).

### 3.1 Component decisions

**Host.** One always-on box running everything: API, worker, and the player daemon. A Raspberry
Pi (4 or 5, 4 GB+) is sufficient — one user, no concurrent load, no re-encoding in the default
path. Storage on an external SSD over USB3, not the SD card, which will wear out under a
database plus continuous writes. An existing always-on NAS or Mac mini works equally well and
saves the purchase. It does **not** need to be near the soundbar — it reaches it over Wi-Fi.

A laptop is a poor fit despite being convenient: it sleeps, closes, and leaves the house. Since
the host holds the playback session, a sleeping host means music stops mid-track. Fine for
developing Phases 1–2, not for running the thing.

**Playback.** The soundbar is an **LG SN9YG**, network-attached over Wi-Fi. **Chromecast is the
only network audio path it offers — it does not support AirPlay.** An earlier draft of this
document claimed AirPlay 2 support; that was wrong.

Verified on the live device, 2026-09-26:

```
mDNS  _googlecast._tcp   LG-SN-Series-ThinQ-S-8365057286ba1880f3bfb0c51901925c
      _googlezone._tcp   present  → supports Cast speaker groups (multi-room later)
      _airplay._tcp      ABSENT
      _raop._tcp         ABSENT
TXT   md=LG SN Series ThinQ Soundbar   fn=Living Room speaker
      st=0               → idle, no receiver app running
host  192.168.50.13      ports 8008, 8009, 8443 all open
info  cast_build_revision 1.52.272222, release_track stable-channel
      connected=true, ethernet_connected=false  (Wi-Fi)
      uptime 63863 s ≈ 17.7 h with the bar not in use
```

Consequences:

- **No DAC, no HDMI cable, no purchase.** The wired plans in an earlier draft are dropped.
- **Cast is the only option, so its weaknesses are unmitigated.** There is no AirPlay path to
  fall back to; below Cast there is only Bluetooth from a phone, which reintroduces the phone as
  transport. This makes the Cast-related risks in section 5 sharper than they would otherwise be.
- **Cast media delivery is pull-based, and that is now a hard constraint, not a maybe.** The
  soundbar fetches the audio from the host over plain HTTP. It cannot send custom auth headers,
  so any token must ride in the URL. See 3.2.
  **Corrected 2026-09-27 — true of the `catt` test, false of the path we actually use.** Whether Cast
  pulls depends entirely on *which Cast protocol the sender speaks*, and OwnTone does not speak the
  one measured on 2026-09-26. It uses screen-mirroring (`castMode: "mirroring"`, receiver app
  `85CDB22F`) and **pushes Opus over RTP/UDP** to a port the device nominates; it never sends a
  `LOAD` and the soundbar never makes an HTTP request to the host. So for the OwnTone path there is
  no media URL to protect and no capability-token problem — and also no metadata channel, which is
  what costs us the soundbar display (Phase 0, Phase 3). The pull-based description still holds for
  fallback A, which is the whole reason 3.2's media-route design is kept rather than deleted.
- **Bluetooth stays as the last-resort fallback.** Cast beats it on quality (no A2DP re-encode of
  already-lossy audio) and keeps the phone out of the audio path, but it needs the network and,
  possibly, the internet.
- **The Cast endpoint does not sleep.** ~18 hours of uptime with the bar unused, still answering
  mDNS and accepting TCP on 8009. So no wake-on-LAN, IR blaster or HDMI-CEC trickery is needed:
  a `LOAD` on the always-listening control channel is what powers up the audio path and switches
  input. This is load-bearing for the whole design and it is now measured rather than assumed.
- **The phone leaves the audio path entirely.** A Cast sender hands the receiver a URL and the
  *soundbar* fetches the stream itself over the LAN. Making the **host** that sender — rather
  than the phone — means the host also holds the session and the queue, so playback survives
  the phone sleeping, leaving, or dying. No computer needs to sit next to the soundbar to get
  this; the network is the cable.

So playback is host-owned, with two fallbacks kept alive:

1. **Primary — player daemon on the host, Chromecast output to the soundbar.** The intended
   daemon is **OwnTone** (formerly forked-daapd): it scans a music folder, owns the queue and the
   playback session, ships a web UI, exposes a JSON API for our own UI to drive, and additionally
   speaks the MPD protocol so off-the-shelf MPD remotes work. **Caveat that matters more now:**
   OwnTone began as an AirPlay server and AirPlay is its most mature output. With AirPlay off the
   table we are relying on its *Chromecast* output, the less-travelled path. *~~Unverified — this is
   the Phase 0 spike, and it is the single riskiest assumption in the document.~~*
   **Verified 2026-09-27: it works, and it is worse than AirPlay in one specific, permanent way.**
   OwnTone 29.3 discovers the SN9YG, plays to it, honours volume and advances its own queue. But its
   Cast output is *screen-mirroring* — it pushes Opus over RTP and sends no track metadata — so the
   soundbar's display shows nothing useful and the audio is re-encoded rather than passed through.
   Neither is a bug we can fix from outside OwnTone. Full measurements in Phase 0; the consequence is
   that **fallback A is no longer only insurance — it is the fix for the display**, and possibly
   for the re-encode too.
2. **Fallback A — drive Cast ourselves with `pychromecast`.** If OwnTone's Cast output
   disappoints, we own the player: FastAPI serves `/media/{id}` and uses `pychromecast` (the
   mature library behind Home Assistant's Cast integration, also wrapped by `catt`) to `LOAD`
   that URL with metadata, then watches for `IDLE` to advance the queue. Costs us a queue
   controller — on the order of a couple hundred lines — and we lose OwnTone's library
   management, playlists and scrobbling. Buys us one less daemon and no dependence on OwnTone's
   Cast quality. Given that Cast is now the *only* transport, being able to drive it directly is
   a meaningful insurance policy.
3. **Fallback B — Subsonic client casting from the phone.** Symfonium on Android can cast; the
   soundbar then pulls from Navidrome. Near-zero integration effort, which makes it a useful
   early smoke test that the soundbar casts at all. Only partly phone-independent (see section 2).
4. **Fallback C — phone → Bluetooth → soundbar.** Needs no network, no cast stack and no
   internet. Reintroduces the phone as transport, so it is a last resort — but it is the only
   path that works when Wi-Fi or the WAN is down. Keep it working.

**Known limitation of a Cast-only design: gapless playback is not realistically achievable.**
Each track is a separate `LOAD`, so album transitions will have a short gap. The Cast media-queue
API can preload the next item and narrow it, but do not promise true gapless. This is a real
regression versus what a local-output player would give, and it is the price of having no cable.

**Revised 2026-09-27, in OwnTone's favour.** "Each track is a separate `LOAD`" is not true of the
path we use: OwnTone holds **one continuous mirroring stream** and changes what it feeds into it, so
a track transition involves no receiver-side session teardown at all — observed live across an
`Autobahn → K歌之王` boundary with `state=play` unbroken. Whether it is *bit*-gapless was not
measured and would need ears on a gapless album, not an API. The one-`LOAD`-per-track gap remains a
real cost of **fallback A**, which is now a point in OwnTone's favour worth weighing against the
display problem.

**Does Navidrome survive?** If OwnTone owns the library and playback, Navidrome is largely
redundant — two daemons scanning one folder. Start with OwnTone alone. Add Navidrome later only
if a native client with offline caching becomes wanted, or if fallback A is needed; it is a
drop-in addition over the same directory, so deferring costs nothing.

**Remote UI.** Because the phone is a pure HTTP client, the ingest screen and the playback
remote are the same web page — search, queue, now-playing, transport. This removes the
"two-app seam" that section 1 listed as Alternative B's main drawback. If OwnTone's own web UI
turns out to be good enough, the playback half may need no code at all; link to it and only
build the search/ingest half.

**Format, revisited.** The soundbar's inputs constrain the container choice — see
**Audio format** below.

**Search.** `ytsearch20:<query>` with flat extraction to list candidates. Return id, title,
uploader, duration, thumbnail. Cache aggressively; every search is a network request to
YouTube and we want to be a light-touch client.

**Ingest.** Run yt-dlp as a **subprocess**, not in-process. Reasons: hard timeouts, memory
isolation, one bad extraction can't take down the API, and the yt-dlp version becomes a
swappable binary rather than a code-coupled import. Parse progress from
`--progress-template` / `--newline`, and final metadata from `--print-to-file`.

**Audio format.** The request said MP3; default instead to **m4a (AAC) passthrough**, selected
not re-encoded: `-f 'ba[ext=m4a]/ba'`. The deciding factor is the cast receiver, not quality —
Chromecast handles AAC and MP3 reliably but Opus only inside WebM, and bare `.opus`/Ogg-Opus
support is inconsistent. So the earlier "m4a/opus" ambiguity resolves to AAC. Passthrough also
avoids a lossy re-encode of already-lossy audio, and keeps CPU cost near zero on a Pi.

Keep MP3 available as an explicit per-request option for the Bluetooth fallback or older
hardware, accepting the re-encode. Ignore the SN9YG's 24-bit/96 kHz Hi-Res capability
entirely: YouTube audio arrives lossy at 128–256 kbps and no container choice recovers that.

**Metadata.** `--embed-metadata --embed-thumbnail`, `--parse-metadata` to split
"Artist - Title" uploads into proper fields, `--sponsorblock-remove` to strip intros/outros.
Good tags are what makes the library browsable. Optional later: MusicBrainz lookup to correct tags.

**Metadata, CJK — measured 2026-09-26/27, and not an afterthought.** The `"Artist - Title"` habit
is Western. Chinese-language uploads write `陳奕迅 Eason Chan《K歌之王》[Official MV]`, put the title
inside `《》`, and are usually uploaded by the *record label* ("英皇娛樂 eeg music"), so naive
mapping produces an artist tag naming a company. Two live searches drove this out: `k歌之王` (five
results, artist wrong on four) and `好久不见` (eight results, five distinct new failure modes).
`sources/ytdlp_tags.py` therefore handles:

- `《》`/`〈〉`/`「」`/`『』` title quotation, in both orders, and `[…]`/`【…】` as a secondary form
  (`JAMES 楊永聰 [好久不見 Long Time No See]` — house style of several Taiwanese labels);
- separators without spaces (`陈奕迅-K歌之王`), attempted only on titles containing CJK so
  `Blink-182` is safe;
- Chinese boilerplate: `歌词版`, `官方MV`, `中文字幕`, `動態歌詞MV`, full-width brackets, and a bare
  `MV` glued to CJK text (`好久不見MV`);
- channel decoration: `单依纯Official`, `…官方频道`, `Official Channel`, alongside `- Topic`/`VEVO`;
- a **leading** hashtag as the performer (`#单依纯 《好久不见》…`, uploaded by a TV show's channel),
  while trailing hashtags are stripped as topic spam;
- lyric teasers: a quotation group over ~25 characters is song lyrics, not a title, and a spaced
  separator occurring before the first bracket wins
  (`單依純 - 好久不見 (Live)『你會不會忽然地出現…』【動態歌詞MV】` → `單依純` / `好久不見 (Live)`);
- a plausibility guard: a candidate artist containing sentence punctuation is prose, not a name,
  so clickbait (`开口就碎了！陈奕迅深情演绎《好久不见》…`) falls back to the channel instead of creating
  a directory named after a sentence.

`(現場版)`, `(超CD水準)` and `(Live)` survive, because they are information rather than marketing.
Where a title genuinely contains no separator (`好久不见 陈奕迅 (歌词版)` on a lyric-video channel)
the channel name stands and `--artist` is the fix; guessing would be worse. This is the concrete
reason the MusicBrainz lookup above stays on the list.

**Tags are not enough for the display — verified 2026-09-26.** A Cast receiver never reads the
tags inside the file; title, artist and artwork come from the `LOAD` payload the *sender*
supplies. In the test cast, an embedded title of "Tunebox Test Tone" showed up as
`tunebox_test` — the filename — because `catt` populates Cast metadata from the path. So
whatever drives playback must explicitly map file tags into the LOAD message, or the soundbar
and every cast UI will show filenames. Two consequences:

- If OwnTone is the player, confirm it passes tags through to Cast metadata. Add it to the spike.
- If we drive Cast ourselves (fallback A), populate `MusicTrackMediaMetadata` — title, artist,
  albumName, images — from the tags we wrote at ingest. It is a small mapping, but nothing
  supplies it for free.

On the Bluetooth fallback the same information travels over AVRCP instead, which *is* driven by
the playing app's own tag reading — so the two paths need metadata wired up separately.

**Queue.** Start with SQLite-backed jobs (one table, a worker loop) — no Redis until there's
a reason. Concurrency capped at 1–2. Per-job timeout. Retries with backoff, bounded.

**Storage.** Plain files under a single music root, `Artist/Album/Track.ext`, scanned by the
player daemon. The filesystem is the database for audio; SQLite only tracks jobs and the ingest
history needed for dedupe. Keeping the layout conventional is what lets the player be swapped
— OwnTone, Navidrome, Jellyfin and MPD all read the same tree.

### 3.2 Security — decided up front, not bolted on

This service downloads arbitrary URLs and executes ffmpeg. It must never be an open endpoint.

- **No public exposure, and no port forwarding — ever.** Bind to the LAN address only. Do not
  enable UPnP on the router for this. Home-only scope means there is no legitimate reason for
  a packet from the internet to reach it.
- **The LAN is not a trust boundary.** A flat home network typically includes a smart TV, a
  vacuum, and a lightbulb running firmware from 2019. Put a password on the ingest API even
  though it is LAN-only — a single shared token in a header is enough at this scale, checked
  on *every* route. The player daemon has its own auth and web UI — keep it enabled too, and
  do not expose its API unauthenticated just because our UI proxies to it.
- **The media route is the exception, and must be designed for it.** Cast is pull-based: the
  soundbar itself fetches the audio over plain HTTP and cannot send an `Authorization` header.
  So `/media/{id}` needs a capability-style URL — an unguessable per-track or per-session token
  in the path or query string, ideally expiring — rather than header auth. Do not solve this by
  making the media route wide open. Log which IP fetched what; the soundbar's address should be
  the only one that ever appears.
  **Status 2026-09-27: not needed yet, and not deleted.** The OwnTone path never makes the soundbar
  fetch anything from us (see 3.1), so tunebox currently serves no media route at all — the safest
  possible version of this requirement. It comes back in full the moment fallback A does.
- **The player daemon's own auth is not on by default, and the default is the wrong one.** Measured
  2026-09-27: OwnTone ships `trusted_networks = { "lan" }`, which means `admin_password` is skipped
  for every device on the home network — so `curl -X PUT http://host:3689/api/player/stop` from the
  IoT device nobody is proud of returns `204`. That directly contradicts the bullet above about the
  LAN not being a trust boundary. The fix is two lines of `owntone.conf`:
  `trusted_networks = { "localhost" }` and a real `admin_password`. tunebox itself keeps working with
  no credentials because it talks to loopback, and `TUNEBOX_PLAYER_PASSWORD` covers the case where it
  does not. The Basic-auth username is `admin`, hard-coded in the daemon.
- Tailscale remains the right answer *if* remote ingest is ever wanted, and is strictly better
  than opening a port. It is no longer required for the home path.
- **Input handling:** treat search terms and URLs as untrusted. Pass arguments as a list to
  `subprocess` — never build a shell string. Validate URLs against an allowlist before
  handing them to yt-dlp.
- **Path safety:** sanitise output templates; confine writes under the music root; reject
  traversal. Rely on yt-dlp's own extension allowlist (do not enable `allow-unsafe-ext`).
- **Never `--exec` with user-controlled values.**
- Run the worker as an unprivileged user with write access to the music directory and nothing
  else.

### 3.3 Keeping it alive

- Pin the yt-dlp version; update on a schedule (nightly channel) with a canary ingest of a
  known-good video after each update. Alert on failure, keep the previous version to roll back.
- One synthetic ingest per day as a health check — this is how we learn extraction broke
  before a user does.
- Structured logs, retained; disk-space alarm on the music volume.

### 3.4 Implementation language and code architecture

Sections 3.1–3.3 decided the *system* shape and left the *code* shape open. Decided now,
before Phase 1, because it is cheap to decide and expensive to change.

**Language: Python 3.11+.** Not because Python is the best tool for daemons, but because every
dependency this project cannot replace is Python: `yt-dlp` is the whole point, `pychromecast`
is the fallback-A player, and FastAPI is the Phase 2 API. A Go or Rust service would still
shell out to yt-dlp, and would then need a second language for fallback A — two toolchains for
no gain. 3.11 is the floor because that is what a current Raspberry Pi OS ships; 3.12 is used
for development here.

What this costs, accepted: no single static binary, so deployment is a venv (or a container)
plus system `ffmpeg`/`deno`. Python's weak process/threading story is irrelevant at a
concurrency cap of 1–2.

Rejected: TypeScript/Node (no yt-dlp binding worth having, and the Cast libraries are weaker
than `pychromecast`), Go/Rust (see above), shell scripts (the tag mapping and path safety in
3.2 want real tests).

**Architecture: ports and adapters, with a CLI-first core.** The rule is that everything
yt-dlp-shaped lives behind one interface, because 1.E and 3.1 both depend on being able to
swap the source:

```
src/tunebox/
  core/          domain: no network, no yt-dlp and no mutagen import. `naming.py` does touch the
                 filesystem — resolving a path is how root confinement is enforced — but nothing
                 in core reads or writes media.
    models.py    SearchResult, TrackTags, FetchedMedia, IngestResult
    ports.py     MusicSource protocol (search / inspect / fetch), TagWriter protocol
    naming.py    TrackTags -> Artist/Album/Track.ext, sanitising and root confinement
  sources/       adapters that know about YouTube
    urls.py      URL allowlist and canonical video id  (3.2 "validate before yt-dlp")
    ytdlp.py     subprocess adapter: argv lists, timeouts, progress parsing
    ytdlp_tags.py  yt-dlp info dict -> TrackTags — the one place YouTube's shape is known
  library.py     atomic placement under the music root
  tagging.py     mutagen tag + cover writer (the TagWriter adapter)
  ingest.py      the use case: inspect -> name -> dedupe -> fetch -> tag -> place.
                 Application layer, not core: it is the thing that wires adapters together, and
                 it takes the source and the tag writer as arguments so tests can pass fakes.
  config.py      env-driven config, one frozen dataclass
  errors.py      one exception tree, so the CLI can tell "this ingest failed" from "we have a bug"
  cli.py         argparse front end: search / ingest / doctor  (+ `__main__.py` for `python -m`)
  jobs/          Phase 2: the queue. models.py (Job, TrackRecord), store.py (SQLite: claim,
                 retry, ingest history), worker.py (threads calling `ingest.py`)
  api/           Phase 2: FastAPI. app.py (factory + error handlers), auth.py (token middleware),
                 routes.py, schemas.py. Thin: it validates, enqueues, and reads the queue back
  player/        Phase 3: the playback session, behind the `Player` port in core/ports.py.
                 owntone.py (the JSON-API adapter), selection.py (keeps the soundbar selected),
                 hooks.py (the rescan-after-ingest hook). The port is load-bearing, not
                 decoration: fallback A is a sibling of owntone.py and nothing above it changes
```

**One more consequence of the port, made concrete in Phase 3.** The `Player` protocol speaks in
**paths**, not in the player daemon's own track ids — `ingest.py` returns a path, the filesystem is
the library database, and mapping "this file" onto "whatever OwnTone calls it" is the adapter's
problem. That turned out to matter: OwnTone's ids are *not stable across a library rescan*, so an
adapter that leaked them upward would have leaked a race as well (see Phase 3's defect list).

Three consequences worth stating, since they are decisions and not style:

- **yt-dlp is a subprocess, never an import** (already decided in 3.1). The adapter builds
  `argv` lists, never a shell string, and enforces its own timeouts. The pinned PyPI package
  is still a declared dependency — it is what supplies the pinned binary, invoked as
  `python -m yt_dlp` so the venv's version is the one that runs.
- **Our tag mapping is the single source of truth for metadata, not `--embed-metadata`.**
  yt-dlp downloads audio and a cover image; `tagging.py` writes every tag with mutagen. This
  is a direct consequence of the 2026-09-26 Cast finding: something must map file tags into the
  Cast `LOAD` payload, and that something should read the same `TrackTags` we wrote at ingest,
  not re-derive them. It also removes a failure mode — `--embed-thumbnail` on m4a wants
  AtomicParsley or a working ffmpeg mux path, and a cover-art failure should never fail an
  ingest.
- **The filesystem stays the library database** (3.1). `ingest.py` returns a path; nothing
  in Phase 1 writes a database. SQLite arrives in Phase 2 for *jobs*, not for tracks.

**Audio format, one implementation note.** `-f 'ba[ext=m4a]/ba'` plus
`-x --audio-format m4a`: yt-dlp skips conversion when the stream is already AAC, so the normal
YouTube path (format 140) is a passthrough remux at near-zero CPU, and the rare Opus-only video
is transcoded to AAC rather than landing in a container the soundbar plays inconsistently.
MP3 stays available as `--format mp3`, which does re-encode, for the Bluetooth fallback.

`--sponsorblock-remove` is **not** on by default, contrary to the wording in 3.1: cutting
segments forces a re-encode of the audio, which contradicts the passthrough decision. It
becomes an opt-in flag later, per ingest.

---

## 4. Phased plan

**Phase 0 — Decisions (no code)**
- [x] Confirm scope: personal/self-hosted, private, **home network only**.
- [x] Confirm Alternative B vs A. → B.
- [x] Host hardware: Raspberry Pi + USB SSD (or an existing always-on box).
- [x] Access path: LAN-only bind. Tailscale optional, for remote *ingest* only.
- [x] Soundbar identified: **LG SN9YG**. **Chromecast built-in only — no AirPlay** (confirmed by
      mDNS sweep and by the vendor's own spec). Also Bluetooth, HDMI in, HDMI out/eARC, optical.
      Nothing needs buying.
- [x] Confirmed live on the network: `192.168.50.13`, name "Living Room speaker", Cast ports
      8008/8009/8443 open, `st=0` idle, ~18 h uptime while unused, same /24 as the host — so mDNS
      discovery works and there is no VLAN problem. **The Cast endpoint does not sleep.**
- [x] Playback owner: **the host**, not the phone. Phone is an HTTP remote only.
- [x] **Cast verified end-to-end on the real device, 2026-09-26.** A 9 s AAC/m4a clip cast from
      a laptop with `catt`:
      - `State: PLAYING`, correct duration detected, clean finish and session teardown.
      - Host HTTP log: `192.168.50.13 - - "GET /… HTTP/1.1" 206 - audio/mp4 - 132.67 KB`.
        **The request came from the soundbar's own IP** — pull-based delivery confirmed on
        hardware, not inferred. `206` means range requests work, so seeking is viable.
      - Woke from `st=0` idle and selected the Cast input with nobody touching the remote.
      - Volume reported as 33 and honoured; AAC/m4a needed no transcode.
      So the format decision, the wake behaviour and the media-pull direction are all settled.
- [x] **Spike: can OwnTone drive the SN9YG over Chromecast? — YES, measured 2026-09-27.** Built
      OwnTone 29.3 from source on the development Mac following upstream's own
      `.github/workflows/macos.yml`, `--enable-chromecast`, into `$HOME/owntone_data` (no sudo: this
      Homebrew install owns `/usr/local`). `GET /api/config` reports `Chromecast` in `buildoptions`;
      the binary links `libprotobuf-c` and `libgnutls`. It then found the soundbar by itself:
      ```
      mdns: Bonjour Browser: NEW service 'LG-SN-Series-ThinQ-S-8365057286ba1880…' _googlecast._tcp
      mdns: … resolved to 192.168.50.13
      cast: Adding Chromecast device 'Living Room speaker'
      ```
      and `GET /api/outputs` listed it as `{"type": "Chromecast", "id": "2080974779"}`. Selecting it
      and adding a queue item played audio: `state: play`, progress advancing, receiver reporting
      `playerState: PLAYING`, volume honoured. **The hard blocker is cleared and fallback A is not
      needed for sound** — but see the two findings below, one of which is not survivable for the
      Phase 3 acceptance test as originally written.
      - **Two build notes for the Pi, learned here.** `libinotify-kqueue` is a *hard* requirement —
        OwnTone's configure aborts with "INOTIFY_CFLAGS and INOTIFY_LIBS" without it — and on
        macOS 26 it fails to build because `fdclosedir` trips `-Werror=unguarded-availability-new`;
        `./configure CFLAGS="-Wno-error=unguarded-availability-new"` fixes it. Neither applies to
        Linux, where inotify is in libc.
- [x] **OwnTone's Chromecast output is *mirroring*, not media-load — and that changes two decisions.**
      Measured from OwnTone's own Cast protocol log: it launches receiver app `85CDB22F`
      ("Chrome Audio Mirroring"), negotiates `{"castMode": "mirroring"}`, and streams **Opus over
      RTP/UDP** to a port the device nominates (`UDP port in ANSWER is 10008`). Across an entire
      session it sent **zero `LOAD` messages**, and the only media metadata the device ever received
      was `{"metadataType": 0, "title": ""}`. Consequences:
      - **The soundbar's display cannot show the track title on this path.** Not a configuration
        problem: there is no metadata channel in use to put a title into. The Phase 3 acceptance
        criterion that asks for it is therefore **not met by OwnTone** — see Phase 3.
      - **Cast delivery is not pull-based here.** The host *pushes* RTP; the soundbar never makes an
        HTTP request to us. The whole `/media/{id}` capability-URL requirement in 3.2 is inapplicable
        to the OwnTone path, which removes a security design problem rather than creating one.
      - **It re-encodes.** AAC 128 kbps → Opus 128 kbps at 48 kHz, every stream. The "no transcoding
        on a Pi" property from 3.1 holds for *ingest* and does not hold for *playback*.
- [x] **OwnTone's own API is unauthenticated on the LAN by default, contrary to 3.2.** Its default
      `trusted_networks = { "lan" }` bypasses `admin_password` for exactly the clients that matter:
      `curl http://<host>:3689/api/player/stop` from any device on the home network returned `204`.
      Fixed by `trusted_networks = { "localhost" }` plus an `admin_password`, after which the LAN gets
      `401` and tunebox (on loopback) still needs no credentials. Also measured: the Basic-auth
      username is **`admin`**, hard-coded in `httpd_request_is_authorized` (`src/httpd.c`) and not
      configurable — an empty username gets `401`.
- [ ] **Test: does casting survive a WAN outage?** Unplug the router's uplink, leave Wi-Fi up,
      try to start playback. The Cast receiver app is fetched from Google, so this may fail —
      and with no AirPlay path, Bluetooth is the only remaining fallback. Worth knowing now.
      *(Now more pointed than when it was written: OwnTone uses the `85CDB22F` mirroring receiver,
      which is a Google-hosted app like any other.)*
- [x] If the OwnTone spike fails: fall back to driving Cast directly with `pychromecast`
      (fallback A in 3.1), which is well-trodden and under our control. **Not needed for audio** —
      but it is now the only known way to get track titles onto the soundbar's display, so it is
      promoted from insurance policy to the fix for a specific, measured shortfall.

**Phase 1 — Walking skeleton (CLI only)** — **done, 2026-09-26**
- [x] Language and code architecture decided and recorded (3.4): Python 3.11+, ports and adapters.
- [x] Python project, yt-dlp pinned as a dependency; ffmpeg + deno present (`tunebox doctor`).
- [x] `tunebox ingest <url>` → tagged file in the right place.
- [x] `tunebox search <query>` → JSON results.
- [x] Tests: search parsing and tag mapping against recorded fixtures (no network in CI).
      261 tests, all offline (verified with sockets blocked); green on Python 3.11 and 3.12.
      Fixtures re-recorded by `tools/record_fixtures.py`.
- [x] **Non-Latin titles verified on real data, twice.** `tunebox search "k歌之王"` and
      `"好久不见"` → correct results; `tunebox ingest Sm_DVsixxsk` →
      `陳奕迅 Eason Chan/Singles/K歌之王.m4a`; `tunebox ingest I2hva376wbk` →
      `單依純/Singles/好久不見 (Live).m4a`. Both AAC with cover art, and `ffprobe` (an independent
      reader) confirms the Chinese tags. Every messy real-world title shape from those two
      searches is now a recorded fixture with a test — see 3.1 **Metadata, CJK**.
- [x] **`yt-dlp-ejs` is a hard requirement, not an optional extra — learned the hard way.**
      With `deno` on PATH but no `yt-dlp-ejs` installed, metadata extraction and thumbnail
      download both succeed and then the media request fails with `HTTP Error 403: Forbidden`.
      Installing `yt-dlp-ejs` fixed it with no other change. It is now pinned in `pyproject.toml`
      and checked by `tunebox doctor`, because the failure mode looks like a YouTube block
      rather than a missing dependency and would waste an afternoon in Phase 5.
- Exit criteria: one command, one correct tagged file on disk. **Met**, verified on real output:
  `tunebox ingest iukUMRlaBBE` → `Kraftwerk/Singles/Autobahn (Single version 1974).m4a`,
  3.4 MB, `aac`/`mp4a` 128 kbps 44.1 kHz stereo, 207 s, cover art embedded, tags read back as
  title `Autobahn (Single version 1974)` / artist `Kraftwerk` / source id `iukUMRlaBBE`.
  yt-dlp confirmed the passthrough itself: *"Not converting audio; file is already in target
  format m4a"* — so a Pi does no transcoding in the default path. A second ingest of the same
  track (via a different URL form) was deduped without downloading.

**Phase 2 — Service** — **done, 2026-09-27**
- [x] FastAPI: `POST /ingest`, `GET /jobs/{id}`, `GET /search` (plus `GET /jobs`,
      `DELETE /jobs/{id}` for a queued job, and `GET /healthz` reporting queue depth).
      Handlers are synchronous and run in Starlette's threadpool: `/search` blocks on a yt-dlp
      subprocess and must not sit on the event loop.
- [x] SQLite job queue + worker, concurrency cap, timeouts, bounded retries. One process holds
      both, because the queue is a file and the worker is threads waiting on subprocesses — and an
      exclusive lock file beside the database makes that a rule: a second `serve` on the same queue
      refuses to start rather than quietly doubling the download concurrency and re-claiming the
      first one's in-flight jobs. Claims are a conditional `UPDATE` inside `BEGIN IMMEDIATE`, so no
      two threads take the same row; backoff is stored as the job's `available_at`, so a waiting
      retry occupies no thread; jobs left `running` by a crash are requeued at startup, and
      requeued jobs keep their attempt count so a job that kills the process cannot become a
      restart loop. Timeouts are the adapter's (it kills the whole yt-dlp process group), with a
      sweep that requeues anything `running` past `inspect+fetch+60 s` with no live thread behind
      it — measured, not assumed, that nothing can sit in the queue forever.
- [x] Shared-token auth on every route; bound to the LAN interface only. The check is ASGI
      middleware, not a dependency: FastAPI mounts `/docs` and `/openapi.json` outside the
      router, so "every route" had to be a property rather than a habit. Unknown paths answer
      `401`, not `404`, and so do `HEAD`, `OPTIONS` and a websocket upgrade. Tokens are compared
      as **bytes** — decoding a header and handing it to `compare_digest` turns a request carrying
      `0xFF` into a 500 raised outside the exception handlers. `serve` refuses to start without an
      ASCII token of 16+ characters, and refuses a wildcard bind address parsed with `ipaddress`
      rather than string-matched: `0`, `0.0`, `0x0`, `::`, `::0` and `0:0:0:0:0:0:0:0` all mean
      `0.0.0.0` to the socket layer and a literal blocklist catches none of them.
- [x] Dedupe by video id, in three layers: an already queued/running job for the *same request*
      (same video, same container, same overrides) returns that job (`200`, not a second
      download); a video already in the library **in the requested format** is caught by the
      worker *before* it spends a metadata request; and the Phase 1 destination-path check
      remains as the backstop for files that arrived another way. Matching on the video id alone
      would have made `format` and `--artist` inoperative on a second request, and would have
      reported an mp3 request as `done` while pointing at the m4a. A history entry whose file has
      been deleted is dropped rather than blocking a re-ingest.
- [x] Tests: 413 total (from 261), still entirely offline — and now *enforced* offline: an autouse
      fixture fails any test that opens a non-local socket or resolves a name. The real store, the
      real worker threads and the real app run against a fake source. Verified green on Python
      3.11 and 3.12. The concurrency cap is tested by counting *overlapping downloads*, not
      threads, since the cap exists to be polite to YouTube rather than to size a pool.
- Exit criteria: queue three URLs from `curl`, all three land correctly. **Met**, verified live
  on 2026-09-27 against `127.0.0.1:8099` with a throwaway music root:
  `POST /ingest` ×3 (a `watch?v=` URL, a `youtu.be/` URL and a bare id, all canonicalised on the
  way in) → `Kraftwerk/Singles/Autobahn (Single version 1974).m4a`,
  `陳奕迅 Eason Chan/Singles/K歌之王.m4a`, `單依純/Singles/好久不見 (Live).m4a`, each `aac` 128 kbps
  with the correct Chinese tags read back by `ffprobe`. Requests without the token got `401`.
  **The retry path was exercised for real, not simulated:** one job hit `HTTP Error 403:
  Forbidden` on attempt 1, was rescheduled 10 s later, and succeeded on attempt 2 — while the
  worker moved on to the next job in the meantime. A repeat `POST` of an already-ingested video
  came back `done`/`already_present` with no network request at all. `SIGINT` drained the worker
  and shut down cleanly.
- **Reviewed by an independent auditor after implementation, and it was worth it.** Nine real
  defects came out of that pass, all fixed above and each with a regression test: the 500 on a
  non-ASCII token, the bypassable wildcard-bind check, a store error killing a worker thread and
  wedging the queue behind it, dedupe ignoring the requested format, an enqueue dedupe that
  discarded the new request's overrides, no reaper for stuck `running` jobs, two `serve` processes
  sharing one queue, a write lock orphaned when `BEGIN` failed, and a `close()` that did not
  actually close. Two of the concurrency tests also turned out to pass with the transaction
  removed — they were exercising an in-process lock, not the database. They now run through
  separate connections, and the dedupe one does fail with `BEGIN IMMEDIATE` removed (checked by
  mutating the code). The claim test passes either way, and that is correct rather than weak:
  what prevents a double claim is the conditional `UPDATE ... WHERE state = 'queued'` and its
  rowcount check; the transaction is what makes the read and that write one step.

**Deliberately not done in Phase 2** (no user, no need yet): `/search` is not rate-limited, so
enough concurrent searches could exhaust the ASGI threadpool; the API has no pagination beyond
`limit`; and there is no metrics endpoint. All three are one-listener non-problems.

**Phase 3 — Sound out of the soundbar, driven by the host** — **implemented 2026-09-27; one
acceptance criterion not met, and it is not fixable with OwnTone**
- [x] OwnTone on the host against the music dir; library rescan hook after ingest. The hook is
      `PUT /api/update` behind a new `Player` port, called by the worker after a job that produced a
      *new* file and by `tunebox ingest` after a CLI ingest. **It can never fail an ingest**: a
      stopped OwnTone becomes a warning on a `done` job, because the audio is already on disk and a
      retry would re-download it for nothing. Verified live — a `POST /ingest` of `qCFIRjycV5Q`
      landed `Kraftwerk/3-D Der Katalog/Das Model.m4a` and OwnTone logged
      `lib: Library rescan triggered` / `completed in 0 sec (2 changes)` with no warnings on the job,
      taking its library from 3 songs to 4.
- [x] Soundbar selected as a persistent output; survives an OwnTone restart and a soundbar
      power-cycle without manual re-selection. Implemented as `player/selection.py`'s `OutputKeeper`,
      a thread inside `tunebox serve`. **Verified on hardware for the restart case, twice and in both
      directions:** with OwnTone's own speaker memory intact it came back already selected; with its
      database deleted while it was down — a daemon that has genuinely forgotten — the keeper logged
      `player is answering again; re-checking the output selection from scratch` /
      `selected output 'Living Room speaker' (Chromecast, id 2080974779) at volume 20` and the output
      was selected again with nobody touching anything. *The physical power-cycle was **not** tested:
      it needs somebody to unplug the soundbar. The absent → present transition it produces is
      covered by an offline test, not by hardware.*
      - **The interesting part is when the keeper stays out of the way**, and getting that wrong is
        how a "feature" becomes a bug. Deselecting the speaker in OwnTone's web UI is a legitimate
        thing to do, so the keeper asserts selection only on *first sight* and on *absent → present*,
        never on "present the whole time and switched off". A daemon outage is tracked separately from
        speaker absence — otherwise restarting OwnTone would look like the soundbar reappearing and
        would undo a deliberate deselection — and the recovery from an outage only re-asserts if the
        speaker *was* selected before it. Verified live: deselecting via OwnTone's API left it
        deselected across many keeper passes.
- [x] **Verify end-to-end: search on the phone → ingest → host plays it on the SN9YG** — the audio
      half is **met**, over HTTP, in the order a phone would do it: `GET /search?q=kraftwerk+das+model`
      → `POST /ingest` → job `done` → rescan → `tunebox player play` → `state: play`, progress
      advancing, `Living Room speaker (Chromecast)` the selected output at volume 20.
- [ ] **…with the correct track title on the soundbar's display. NOT MET, and not a bug to fix.**
      OwnTone's Cast output is screen-mirroring (Phase 0), so there is no Cast media metadata to
      populate: the device received `{"metadataType": 0, "title": ""}` and zero `LOAD` messages all
      session. Nothing in tunebox's configuration can change that. **This is precisely the failure
      mode 3.1 predicted when it said tags are not enough for the display, and the mitigation it
      named — fallback A, populating `MusicTrackMediaMetadata` ourselves — is now the only known fix.**
      The `Player` port exists so that swapping it in is one new file, not a rewrite.
- [x] **Walk away with the phone, then kill the browser. Audio must continue, including the
      transition to the next track.** **Met.** Two tracks queued, seeked to five seconds before the
      end of the first, then only observed: `Autobahn (207 s) → K歌之王` with `state=play` unbroken
      across the boundary and the second track's progress counting up from zero. Nothing was driving
      it; the host advanced its own queue. As a bonus, the mirroring transport does **not** tear down
      and re-establish a session per track, which is why the plan's flat "no gapless" prediction is
      softened below.
- [x] Control it from a second device (laptop browser) to confirm the remote is stateless. **Met.**
      OwnTone's web UI and JSON API answer on the host's LAN address; two independent fresh clients
      paused and resumed the same session and the progress continued rather than restarting, with
      **zero cookies set**. The session is on the host, and the remote carries no state. (Reaching it
      now needs the `admin` password added in Phase 0 — still stateless, since Basic auth is
      per-request.)
- [ ] Verify the Bluetooth fallback still works. **Not tested** — it needs a phone physically paired
      to the soundbar, which no amount of API calls can stand in for.
- Exit criteria: music plays with no phone involved at all. **Met** — verified above, with the
  soundbar as the only selected output and every command issued from the host.
- **What this cost in code:** a `Player` port in `core/ports.py`, `player/owntone.py` (the adapter,
  stdlib `urllib` only — no new runtime dependency for a handful of JSON calls to loopback),
  `player/selection.py` (the keeper), `player/hooks.py` (the rescan hook), a `tunebox player`
  command group, and a `player` block on `GET /healthz` that reports the session and **cannot fail
  the route** — a monitor must not see `503` because a soundbar is unplugged. Tests went from 413 to
  560, still entirely offline: the adapter is exercised against a real stub HTTP server on loopback
  rather than a mocked `urllib`, because half of what it does is build URLs and read status codes.
- **Three defects that only hardware could have found**, each now with a regression test:
  - **Track ids are unstable during a rescan.** `tunebox player play FILE` originally rescanned and
    then resolved the path, which is the obvious order and the wrong one: a scan deletes and
    re-inserts rows, so the id was rejected as `Invalid uri` and OwnTone answered `500` — which reads
    like a malformed request. Corrected to resolve *first* and rescan only when the file is genuinely
    unknown, then wait for `updating` to go false before reading an id.
  - **The keeper could not fix an OwnTone restart.** Because `tunebox serve` outlives OwnTone, the
    keeper's own memory still said "present and handled", so a daemon that came back having forgotten
    the speaker was left switched off — the exact scenario this bullet promises. Fixed by tracking
    daemon reachability as a separate transition.
  - **The Basic-auth username was wrong.** `:password` looks like the natural encoding and gets
    `401`; OwnTone hard-codes `admin`.
- **Reviewed by an independent auditor afterwards, and — as in Phase 2 — it was worth it.** The
  reviewer read the diff against this document and mutated copies of the code to check whether the
  tests had teeth. Three findings were serious:
  - **The keeper could lose the soundbar permanently.** It recorded "was the output selected when I
    last looked", so the observation that *triggered* a selection — "off" — was the one written down.
    One power-cycle or OwnTone restart after the keeper had done the selecting, and it concluded the
    speaker had been switched off on purpose and never brought it back. Silent, permanent, and
    reachable from the most ordinary starting state there is: a fresh OwnTone. Fixed by rewriting the
    state machine around *what we believe should be on* plus *has it been out of sight*, which
    collapses three special-cased transitions into one rule and makes the bug structurally impossible
    rather than patched.
  - **`check_once` promised never to raise and only caught `PlayerError`.** Any other exception
    killed the keeper thread for the lifetime of the process, with nothing to restart it and nothing
    anywhere reporting its absence — and there was a reachable route to one, because a JSON body that
    was not an object produced an `AttributeError` from inside a getter (see below). Fixed at both
    ends: the adapter validates response shape, and both `check_once` and the loop are total.
  - **The tests for the keeper all started from "already selected"** — the one initial state that
    hides the first finding. They now start from a fresh unselected daemon where it matters, and the
    load-bearing lines were each mutation-checked: removing the recovery branch fails four tests,
    removing the "it went away" flag fails five, and removing the deselection memory fails two.
  Medium findings, all fixed: unexpected JSON shapes escaping the exception tree; a bare `KeyError`
  from a track record with no `id`; `/healthz` matching the output by name *or* id so a name collision
  could report the wrong speaker as selected; a hard `limit=50` on the path lookup that turned a
  truncated page into a misleading "the daemon has not indexed it"; a path match that only tried the
  fully-resolved form and so would have failed on the symlinked external SSD this document's own 3.1
  recommends; `doctor` able to traceback on a malformed `/api/config`; and secrets appearing in
  `repr(Config)`. Two tests were vacuous and were proved so by mutation — one "non-JSON body" test
  that passed an *empty* body, and one percent-encoding test that asserted through a decoder and so
  was true of both encodings. 413 → 560 tests.

**Phase 4 — One web UI**
- [ ] Single page served by the API: search, ingest progress, now-playing, transport controls.
      Installable to the home screen. Playback controls proxy to OwnTone's JSON API.
- [ ] Decide whether OwnTone's own web UI is good enough for the playback half; if so, link to
      it and build only search/ingest.
- Exit criteria: one icon on the phone does everything.

**Phase 5 — Hardening**
- [ ] Confirm nothing is bound to a public interface and no port is forwarded.
- [ ] yt-dlp auto-update + canary + rollback.
- [ ] Daily health-check ingest, alerting, backups of the music volume.
- [ ] SSD-failure story: the music library is the only irreplaceable state.

**Phase 6 — Optional**
- [ ] Navidrome alongside OwnTone over the same folder, if a native Subsonic client with
      offline caching or lockscreen controls turns out to be wanted after all.
- [ ] Snapcast for synchronised multi-room, if a second speaker ever appears.
- [ ] Tailscale, if queueing tracks from outside the house turns out to be wanted.
- [ ] Source adapter interface + a licence-clean source, keeping Alternative E open.
- [ ] Playlist import; "album mode" ingest.

---

## 5. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Extraction breaks after a YouTube change | High | Nightly updates, canary, daily health check, rollback |
| Rate limiting / bot challenge | Low–Medium | Home IP by design, sleep intervals, concurrency 1–2, caching |
| iOS PWA background audio is inadequate | High (for Alt A) | Chose Alt B specifically to avoid this |
| Poor tags make the library unusable | Medium | `--parse-metadata` rules, optional MusicBrainz, manual fix path |
| **OwnTone can't drive the SN9YG reliably** | ~~Medium~~ **Resolved** | **The Phase 0 spike answered it on hardware 2026-09-27: it can.** Plays, honours volume, advances its own queue. What it cannot do is put a title on the display — tracked as its own row below |
| Host is a single point of failure for all listening | Medium–High | Higher than before: the host now holds the playback session, so it must be genuinely always-on. Not a laptop |
| SD-card or SSD failure loses the library | Medium | Library on USB SSD not SD; periodic backup. Tracks are re-ingestable but tags and playlists are not |
| Soundbar can't play the file | ~~Low~~ **Resolved** | AAC/m4a verified playing on the device 2026-09-26. Opus still untested; AAC is the default so it does not matter unless we change it — **and OwnTone transcodes to Opus for Cast anyway, so the soundbar's Opus decoder is in fact exercised on every play** |
| **Soundbar display shows filenames instead of track names** | ~~Medium~~ **Certain, on the OwnTone path** | Worse than predicted: it shows *nothing*, not filenames. OwnTone's Cast output is screen-mirroring and carries no media metadata at all (measured: zero `LOAD`s, `title: ""`). Unfixable from outside OwnTone. **Only fallback A can fix it**, by sending `MusicTrackMediaMetadata` ourselves — which is why the `Player` port exists |
| **The host transcodes on every play** | **New, Certain** | Not foreseen: OwnTone re-encodes AAC → Opus 128 kbps/48 kHz for Cast, so 3.1's "a Pi does no transcoding" is true of ingest only. One Opus encode of one stereo stream is within a Pi 4's budget, but it is no longer free, and it is a second lossy generation |
| **The player daemon is unauthenticated on the LAN out of the box** | **New, Certain if unaddressed** | OwnTone's default `trusted_networks = { "lan" }` bypasses its own password for every device on the home network — measured. Set `trusted_networks = { "localhost" }` plus `admin_password`; see 3.2 |
| Soundbar's Chromecast stack needs internet | Medium–High | **No AirPlay fallback exists on this model**, so a WAN outage may leave only Bluetooth. Tested explicitly in Phase 0 — **still outstanding, and sharper now that the receiver in use (`85CDB22F`) is known to be a Google-hosted app** |
| Soundbar drops off Wi-Fi | Medium | DHCP reservation for 192.168.50.13; keep it on the same subnet as the host; Bluetooth covers outages |
| OwnTone's *Chromecast* output is weaker than its AirPlay output | ~~Medium–High~~ **Confirmed, and quantified** | It is weaker in exactly two ways, both measured: no metadata to the device, and a mandatory Opus re-encode. It is *stronger* in one way nobody expected — one continuous stream instead of one `LOAD` per track, so transitions do not tear down the session |
| No gapless playback | ~~Certain~~ **Better than assumed** | Inherent to one-`LOAD`-per-track, which OwnTone does **not** do; transitions were seamless at the protocol level. Bit-gaplessness unmeasured. Becomes Certain again if fallback A is adopted |
| Media route must be reachable unauthenticated-ish | ~~Certain~~ **Not applicable on the OwnTone path** | The soundbar never fetches from us: OwnTone pushes RTP. tunebox serves no media route at all. Returns in full with fallback A |
| Output selection doesn't survive reboots or power-cycles | ~~Medium~~ **Handled for restarts; power-cycle untested** | `OutputKeeper` re-selects on first sight and on absent → present, and re-asserts after a daemon outage — verified on hardware against an OwnTone whose database had been deleted. The *physical* power-cycle was not performed; only the offline test covers it |
| No lockscreen or volume-button control | Certain | Accepted trade for phone-independence. Soundbar remote does volume; "Hey Google, pause" works once playing |
| Web remote feels worse than a native app | Medium | Keep it to one screen. Phase 6 can add Navidrome + a native client if it grates |
| Legal exposure grows with user count | — | Home-only, single user, not shared. Alternative E if it must ever be a product |

## 6. Non-goals

- Publishing to app stores.
- Public signup or monetisation.
- **Listening outside the home**, or any internet-reachable endpoint.
- Multi-user or multi-household.
- **Playback that depends on the phone.** The phone is a remote; it is never a transport.
- Building a player engine, queue, or cast stack — that is OwnTone's job.
- Video. Audio only. In particular, nothing about the soundbar's Atmos or TV-audio role is
  in scope — this project only ever sends it stereo music.
- Any change to the yt-dlp repo (see 0.1).

## 7. Open questions

1. ~~Personal use only?~~ **Confirmed: home-only, single household.**
2. ~~Does the soundbar support AirPlay/Chromecast?~~ **Chromecast only. No AirPlay — confirmed by
   mDNS sweep and vendor spec. An earlier draft of this document wrongly claimed AirPlay 2;
   the fallback chain in 3.1 was rewritten because of it.**
3. ~~Should the phone run a Subsonic client?~~ **No. The host owns playback; the phone is an
   HTTP remote. See section 2.**
4. ~~iOS, Android, or both?~~ **Largely moot now — a browser remote behaves the same on both.
   Only resurfaces if the OwnTone spike fails.**
5. ~~Does OwnTone drive this soundbar?~~ **Yes — measured 2026-09-27. Audio, volume and queue
   advancement all work. But it drives it by screen-mirroring, so the display stays blank and the
   audio is re-encoded to Opus. See Phase 0.**
6. Is there already an always-on machine in the house (NAS, Mac mini, existing Pi)? If yes,
   nothing needs buying; if no, that is the only spend. It need not be near the soundbar.
7. Is MP3 specifically needed anywhere, or was it shorthand for "audio file"? The AAC default
   assumes the latter.
8. ~~Is the soundbar on the same subnet as the host?~~ **Yes — 192.168.50.13, same /24. mDNS
   discovery verified working from this machine.**
9. **Does Cast playback work with the WAN down?** Unknown and now important, because there is no
   AirPlay path to fall back to. Phase 0 test.
10. Is the phone's browser enough for a remote, or will the missing lockscreen controls push us
    back to Navidrome plus a native client? Answer after living with Phase 4 for a week.
11. **Is a blank soundbar display actually worth switching player to fix?** The new open question,
    and the only one that could still change the architecture. Fallback A would put the title on the
    display and remove the Opus re-encode, at the cost of writing our own queue controller, losing
    OwnTone's library management and web UI, and reintroducing one `LOAD` per track (so real gaps
    between album tracks) plus 3.2's capability-URL work. Note that the *remote* will show the
    correct title either way from Phase 4 onwards — this is specifically about the hardware display
    on the soundbar's front panel. Worth deciding after living with Phase 4, not before.
