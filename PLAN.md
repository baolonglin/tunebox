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

Status: **draft, no project code written yet.** Scope and architecture decided. The soundbar,
the Cast transport and the audio format are **verified working on hardware** (see Phase 0). One
blocker remains: whether OwnTone can drive Cast well, or whether we drive it ourselves.

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
   table we are relying on its *Chromecast* output, the less-travelled path. *Unverified — this is
   the Phase 0 spike, and it is the single riskiest assumption in the document.*
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
- [ ] **Spike: can OwnTone drive the SN9YG over Chromecast?** Install it anywhere, point it at a
      folder with one file, select the soundbar as output, press play. *(still the hard blocker:
      the above proves the soundbar and the transport are fine, but not that OwnTone's Cast
      output is. Its AirPlay output is more mature and this soundbar cannot use it.)*
- [ ] **Test: does casting survive a WAN outage?** Unplug the router's uplink, leave Wi-Fi up,
      try to start playback. The Cast receiver app is fetched from Google, so this may fail —
      and with no AirPlay path, Bluetooth is the only remaining fallback. Worth knowing now.
- [ ] If the OwnTone spike fails: fall back to driving Cast directly with `pychromecast`
      (fallback A in 3.1), which is well-trodden and under our control.

**Phase 1 — Walking skeleton (CLI only)**
- [ ] Python project, yt-dlp pinned as a dependency; ffmpeg + deno present.
- [ ] `tunebox ingest <url>` → tagged file in the right place.
- [ ] `tunebox search <query>` → JSON results.
- [ ] Tests: search parsing and tag mapping against recorded fixtures (no network in CI).
- Exit criteria: one command, one correct tagged file on disk.

**Phase 2 — Service**
- [ ] FastAPI: `POST /ingest`, `GET /jobs/{id}`, `GET /search`.
- [ ] SQLite job queue + worker, concurrency cap, timeouts, bounded retries.
- [ ] Shared-token auth on every route; bound to the LAN interface only.
- [ ] Dedupe by video id.
- Exit criteria: queue three URLs from `curl`, all three land correctly.

**Phase 3 — Sound out of the soundbar, driven by the host**
- [ ] OwnTone on the host against the music dir; library rescan hook after ingest.
- [ ] Soundbar selected as a persistent output; survives an OwnTone restart and a soundbar
      power-cycle without manual re-selection.
- [ ] **Verify end-to-end: search on the phone → ingest → host plays it on the SN9YG, with the
      correct track title on the soundbar's display.** This is the acceptance test for the
      whole project.
- [ ] **Walk away with the phone, then kill the browser. Audio must continue, including the
      transition to the next track.** The queue lives on the host; prove it.
- [ ] Control it from a second device (laptop browser) to confirm the remote is stateless.
- [ ] Verify the Bluetooth fallback still works.
- Exit criteria: music plays with no phone involved at all.

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
| **OwnTone can't drive the SN9YG reliably** | Medium | **The Phase 0 spike exists solely to find this out early.** Falls back to a casting Subsonic client, then Bluetooth |
| Host is a single point of failure for all listening | Medium–High | Higher than before: the host now holds the playback session, so it must be genuinely always-on. Not a laptop |
| SD-card or SSD failure loses the library | Medium | Library on USB SSD not SD; periodic backup. Tracks are re-ingestable but tags and playlists are not |
| Soundbar can't play the file | ~~Low~~ **Resolved** | AAC/m4a verified playing on the device 2026-09-26. Opus still untested; AAC is the default so it does not matter unless we change it |
| Soundbar display shows filenames instead of track names | Medium | Cast metadata comes from the sender's LOAD payload, not the file's tags (verified). Map tags → LOAD explicitly; check OwnTone does this |
| Soundbar's Chromecast stack needs internet | Medium–High | **No AirPlay fallback exists on this model**, so a WAN outage may leave only Bluetooth. Tested explicitly in Phase 0 |
| Soundbar drops off Wi-Fi | Medium | DHCP reservation for 192.168.50.13; keep it on the same subnet as the host; Bluetooth covers outages |
| OwnTone's *Chromecast* output is weaker than its AirPlay output | Medium–High | Raised from Medium now that AirPlay is unavailable. Phase 0 spike, then `pychromecast` fallback under our own control |
| No gapless playback | Certain | Inherent to Cast: one `LOAD` per track. Narrow it with the Cast queue API; do not promise gapless |
| Media route must be reachable unauthenticated-ish | Certain | Cast pulls over HTTP and cannot send headers → capability URLs with expiring tokens (see 3.2) |
| Output selection doesn't survive reboots or power-cycles | Medium | Explicitly tested in Phase 3; automate re-selection on startup if manual |
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
5. **Does OwnTone drive this soundbar?** The one hard unknown, and the only Phase 0 blocker
   left. Cheap to answer and answerable today, with no tunebox code written.
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
