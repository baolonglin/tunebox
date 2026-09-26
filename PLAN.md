# Tunebox — Plan

Home audio library built on top of yt-dlp.
Goal: from a phone on the home Wi-Fi, search for a track, get audio into a personal
library, and play it on the living-room LG SN9YG soundbar — by casting to it, with the phone
acting as a remote rather than as the audio source.

Scope is deliberately small: **one household, one home network, listened to at home.**
Nothing is reachable from the internet. This is not a product and is not intended to
become one.

`tunebox` is a placeholder name. This document is a decision record, not a spec — it exists
so we choose an architecture before writing code.

Status: **draft, no code written yet.** Scope and architecture decided (Phase 0 mostly
closed); two hardware questions remain open — see 7.

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

- **No VPN needed for the core path.** Bind the API and Navidrome to the LAN interface only.
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
the room. So the host runs the player and drives the soundbar directly over Chromecast or
AirPlay 2; the phone talks plain HTTP to the host and nothing else. We build search and
ingest, plus a thin remote UI over an existing player daemon.

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
│   ┌──────────────────┐                         │ AirPlay 2, over LAN │
│   │ LG SN9YG         │◀────────────────────────┘                     │
│   │ Chromecast /     │                                               │
│   │ AirPlay 2 / BT   │   the phone is never in the audio path        │
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

**Playback.** The soundbar is an **LG SN9YG**, which is network-attached: Chromecast built-in,
AirPlay 2, and Spotify Connect, plus Bluetooth, HDMI in, HDMI out (eARC) and optical.
It is on the Wi-Fi already, so it is a first-class endpoint on the same network as the Pi —
not a dumb speaker at the end of a cable. Consequences:

- **No DAC, no HDMI cable, no purchase.** The wired plans in the previous draft are dropped.
- **Bluetooth is demoted to a fallback.** It was only ever the plan because a Bluetooth sink
  was assumed to be the sole input. Casting beats it on quality (no A2DP re-encode of
  already-lossy audio) and on reliability.
- **The phone leaves the audio path entirely.** A Cast sender hands the receiver a URL and the
  *soundbar* fetches the stream itself over the LAN. Making the **host** that sender — rather
  than the phone — means the host also holds the session and the queue, so playback survives
  the phone sleeping, leaving, or dying. No computer needs to sit next to the soundbar to get
  this; the network is the cable.

So playback is host-owned, with two fallbacks kept alive:

1. **Primary — player daemon on the host, output to the soundbar over AirPlay 2 or Chromecast.**
   The intended daemon is **OwnTone** (formerly forked-daapd): it scans a music folder, owns the
   queue and the playback session, outputs to AirPlay 2 and Chromecast devices, ships a web UI,
   exposes a JSON API for our own UI to drive, and additionally speaks the MPD protocol so
   off-the-shelf MPD remotes work. *Not yet verified against this soundbar — see the Phase 0
   spike. Its Cast/AirPlay output is the linchpin of this design, so it gets tested first.*
2. **Fallback A — Subsonic client casting from the phone.** Symfonium on Android can cast; the
   soundbar then pulls from Navidrome. Works with near-zero integration effort, which makes it a
   useful early smoke test that the soundbar casts at all. Only partly phone-independent
   (see section 2).
3. **Fallback B — phone → Bluetooth → soundbar.** Needs no network and no cast stack. This is
   the thing to compare against when the other two misbehave, so keep it working.

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
Good tags are what makes Navidrome's library usable and what the soundbar shows on its display —
via the cast metadata on the primary path, or AVRCP on the Bluetooth fallback. Optional later: MusicBrainz lookup to correct tags.

**Queue.** Start with SQLite-backed jobs (one table, a worker loop) — no Redis until there's
a reason. Concurrency capped at 1–2. Per-job timeout. Retries with backoff, bounded.

**Storage.** Plain files in Navidrome's music dir, `Artist/Album/Track.ext`. The filesystem
is the database for audio; SQLite only tracks jobs and the ingest history needed for dedupe.

### 3.2 Security — decided up front, not bolted on

This service downloads arbitrary URLs and executes ffmpeg. It must never be an open endpoint.

- **No public exposure, and no port forwarding — ever.** Bind to the LAN address only. Do not
  enable UPnP on the router for this. Home-only scope means there is no legitimate reason for
  a packet from the internet to reach it.
- **The LAN is not a trust boundary.** A flat home network typically includes a smart TV, a
  vacuum, and a lightbulb running firmware from 2019. Put a password on the ingest API even
  though it is LAN-only — a single shared token in a header is enough at this scale, checked
  on *every* route. Navidrome has its own auth; keep it on.
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
- [x] Soundbar identified: **LG SN9YG** — Chromecast built-in, AirPlay 2, Bluetooth, HDMI in,
      HDMI out/eARC, optical. Nothing needs buying.
- [ ] Confirm on the unit itself that Chromecast/AirPlay appear as inputs and that it is on
      the Wi-Fi (Google Home app, or the LG remote's input list). *(cheap, do it first)*
- [ ] Pick the phone-side client. If Android: Symfonium, for its cast support. If iOS: check
      whether any Subsonic client can cast rather than only AirPlay-from-phone; if none can,
      the OwnTone variant becomes the phone-independent answer. *(blocker for Phase 3)*

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

**Phase 3 — Listening (cast to the soundbar)**
- [ ] Navidrome deployed against the music dir; rescan hook after ingest.
- [ ] Subsonic client on the phone; verify lockscreen controls.
- [ ] **Verify end-to-end: search on the phone → ingest → cast to the SN9YG → sound, with the
      correct track title on the soundbar's display.** This is the acceptance test for the
      whole project.
- [ ] Confirm the soundbar can reach Navidrome directly: watch the server access log for a
      request originating from the soundbar's IP, not the phone's. If the stream is being
      proxied through the phone, casting is not actually working.
- [ ] **Walk away with the phone. Audio must continue.** This is the property that made the
      wired-Pi plan unnecessary; if it does not hold, that plan comes back.
- [ ] Verify the Bluetooth fallback still works.
- [ ] Minimal ingest PWA: one search screen, installable to home screen.

**Phase 4 — Only if Phase 3's walk-away test fails**
- [ ] OwnTone on the box, output to the SN9YG over AirPlay 2 or Chromecast, browser remote.
- [ ] Decide whether it replaces Navidrome or runs alongside it against the same folder.
- Exit criteria: playback starts and continues with no phone involved.

**Phase 5 — Hardening**
- [ ] Confirm nothing is bound to a public interface and no port is forwarded.
- [ ] yt-dlp auto-update + canary + rollback.
- [ ] Daily health-check ingest, alerting, backups of the music volume.
- [ ] SSD-failure story: the music library is the only irreplaceable state.

**Phase 6 — Optional**
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
| Pi is a single point of failure for all listening | Medium | Accepted. Client-side caching covers short outages; keep the SSD backed up |
| SD-card or SSD failure loses the library | Medium | Library on USB SSD not SD; periodic backup. Tracks are re-ingestable but tags and playlists are not |
| Casting works but the soundbar can't play the file | Low | AAC default (see Audio format); test one Opus file early to confirm the limit rather than assume it |
| Cast handoff still leaves the phone in the audio path | Medium | Explicit access-log check plus walk-away test in Phase 3; OwnTone in Phase 4 if it fails |
| Soundbar's Chromecast stack needs internet, or drops off Wi-Fi | Medium | House has internet anyway for ingest; Bluetooth fallback covers outages. Give the soundbar a DHCP reservation |
| No iOS Subsonic client can cast | Medium | OwnTone's browser remote is the answer, and works on any phone |
| Legal exposure grows with user count | — | Home-only, single user, not shared. Alternative E if it must ever be a product |

## 6. Non-goals

- Publishing to app stores.
- Public signup or monetisation.
- **Listening outside the home**, or any internet-reachable endpoint.
- Multi-user or multi-household.
- Video. Audio only. In particular, nothing about the soundbar's Atmos or TV-audio role is
  in scope — this project only ever sends it stereo music.
- Any change to the yt-dlp repo (see 0.1).

## 7. Open questions

1. ~~Personal use only?~~ **Confirmed: home-only, single household.**
2. ~~Does the soundbar support AirPlay/Chromecast?~~ **LG SN9YG: both, plus Bluetooth,
   HDMI/eARC and optical. Confirm on the unit, but no hardware purchase is expected.**
3. Is there already an always-on machine in the house (NAS, Mac mini, existing Pi)? If yes,
   nothing needs buying at all; if no, that is the only spend.
4. **iOS, Android, or both?** Now the highest-value unknown — it decides whether the phone can
   cast from a Subsonic client (Android/Symfonium: yes) or whether Phase 4's OwnTone is needed
   on day one.
5. Is MP3 specifically needed anywhere, or was it shorthand for "audio file"? The AAC default
   assumes the latter.
6. Is the soundbar on 2.4 GHz or 5 GHz Wi-Fi, and is it on the same subnet as the host? Cast
   discovery is mDNS and does not cross VLANs — relevant if the house has a guest or IoT
   network, which is exactly where a soundbar often ends up.
