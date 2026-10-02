# Tunebox — TODO

Small wants, in no particular order. Anything here is a feature on top of an architecture that is
already decided; if an item turns out to need a *decision*, it belongs in `PLAN.md` instead.

---

## A speaker in standby, and the silence nobody was told about — **done, 2026-09-30**

The remote's **Turn the speaker on** button failed with an error nobody could read, and the screen
above it claimed to be playing music while the room was silent. Both were found by using it, both
were measured on the real daemon, and they are the same incident:

```
PUT /api/outputs/2080974779 {"selected": true}   -> 400  <html>…400 Bad Request…</html>
PUT /api/outputs/2080974779 {"selected": false}  -> 204
PUT /api/outputs/2080974779 {"volume": 40}       -> 204
```

Only *enabling* failed, so it was neither the id, the body nor the credentials. OwnTone's own log had
the reason, and it is the interesting part:

```
player: Speaker enable: 'Living Room speaker' (id=2080974779)
misc:   Connecting to 'Chomecast control' at 192.168.50.13 (port 8009)
misc:   Could not connect … : No route to host
cast:   Could not connect to Living Room speaker
web:    JSON api request failed with error code 400 (/api/outputs/2080974779)
```

The soundbar was in Wi-Fi standby: still advertising `_googlecast._tcp`, so the daemon listed it and
the page correctly drew it as visible-but-off, but not answering ARP, so port 8009 was unreachable.
**An mDNS advert is not proof of reachability**, which is the fact the old code assumed away. It then
recovered on its own — a later attempt established `TLS1.3` and played, with nothing reconfigured,
fifty-four seconds after the first failure. So the failure is transient by nature.

- [x] **Retry a speaker that is waking up** — `select_output`, `TUNEBOX_OUTPUT_SELECT_ATTEMPTS=3`.
- [x] **Say when audio is going nowhere** — `playing_without_output` on `GET /player`, and a line on
      the page.

Four decisions:

- **The retry is in the adapter, not in the route or the keeper.** The keeper already retried every
  fifteen seconds — that is what recovered the session above — so the *session* was never really
  broken; what was broken was the one path a human drives, where "try again in fifteen seconds" is
  indistinguishable from "this button is dead". Putting it in `select_output` means the CLI's
  `tunebox player select` and the keeper get it too, and none of them had to learn about it.
- **A new error type, `OutputUnreachable`, mapped to `503`.** `OutputNotFound` (404) is "it is not
  there" and a plain `PlayerError` (502) is "that command was wrong"; this one is "it is there, it
  will not start, try again", and the status code is the difference between a remote that says so and
  one that shrugs. The message deliberately carries **none** of the daemon's answer: that answer is a
  full HTML error page, and `HTTP 400 <html><head><title>…` in a toast on a phone was the original
  bug. Same class of problem as the queue-tap pre-check below, and the same conclusion.
- **The list is re-read between attempts.** Measured: OwnTone answers a failed enable, `id=999999`
  and `id=bogus` with the identical `400` and the identical HTML, so the status code cannot tell
  "asleep" from "gone". One loopback GET can, and a speaker that has genuinely left is a `404` rather
  than three seconds of pointless retrying.
- **"Playing" with no output attached is its own field, not something the client derives.** It is the
  one state where every other value in the response looks healthy: OwnTone logs `All selected
  speakers failed to start` and then keeps `state: play` with `item_progress_ms` advancing — measured
  going from 7 690 ms to 231 120 ms across the outage — so the progress bar was honest about the queue
  and a lie about the room. Any selected output counts, not just the configured one; audio reaching
  the wrong speaker is a different complaint from audio reaching none.

The button now says **Waking the speaker…** and refuses a second tap while the request is in flight,
because the request is slow *on purpose* (up to about four seconds), and four seconds of a button
that looks untouched reads as a button that does nothing — the very thing being fixed.

Not verified: the retry has not yet been observed winning against the real soundbar, because
reproducing it means waiting for the speaker to go back into standby. What *was* run against the live
daemon, 2026-09-30: enabling the reachable speaker still takes one attempt and 0.09 s, and an enable
of `id=999999` — the daemon's real `400` with its real HTML body — comes back as `OutputNotFound`
naming the outputs that *are* visible, in 0.00 s, with no markup in the message and no retrying. The
rest is tested against the stub: refused twice then accepted, refused throughout, refused and then
gone from the list, and a daemon that is down not being retried at all. Tests 1262 → 1276.

---

## Playback modes — **done, 2026-09-28** (the hardware check is still outstanding)

OwnTone owns the queue (PLAN.md 2), so all three of these were daemon features that tunebox simply
did not expose. **Verified against the running daemon on this host, 2026-09-27:**

```
PUT /api/player/repeat?state=single   -> 204, repeat=single
PUT /api/player/repeat?state=all      -> 204, repeat=all
PUT /api/player/repeat?state=off      -> 204, repeat=off
PUT /api/player/shuffle?state=true    -> 204, shuffle=true
PUT /api/player/shuffle?state=false   -> 204, shuffle=false
```

The read half already existed end to end: `PlayerStatus` carries `repeat`, `shuffle` and `consume`
(`core/models.py`), `player/owntone.py` populates them from `/api/player`, and `GET /player` returns
them (`api/remote.py`, `api/schemas.py`). What was missing was the write half and a control on the page.

- [x] **Repeat one song** — `repeat=single`.
- [x] **Repeat the queue** — `repeat=all`.
- [x] **Shuffle** — `shuffle=true|false`.

One change, not three, and it landed as scoped: `set_repeat` / `set_shuffle` on the `Player` port in
`core/ports.py`, the matching pair in `player/owntone.py`, `PUT /player/repeat` and
`PUT /player/shuffle` added to the enumerated route list in `api/remote.py` (fifteen `/player/*`
routes now, up from thirteen), a tri-state repeat button and a shuffle toggle on the now-playing
panel, and the route list in README's *What is and is not exposed* kept truthful. No CLI verb, as
planned.

The three traps, and what was done about each:

- **Validation is in our adapter, not in the daemon.** `state=bogus` returns `204` on both routes and
  is silently taken as "off"/"false" — measured above — so a typo in a client would have looked like
  success and quietly changed the mode. There are now two checks and both are tested: pydantic at the
  edge (`RepeatRequest`, `ShuffleRequest`, so a bad mode is a `422` and never reaches the player), and
  `_repeat_state` / `_bool_state` in the adapter for callers that are not HTTP. The `Literal` in the
  schema is built from `core.models.REPEAT_MODES` rather than spelled a second time, so the API and
  the adapter cannot drift into accepting different words. `set_shuffle` insists on a real `bool`:
  `set_shuffle("false")` is truthy in Python and false to the daemon, which is the one way to get a
  mode that is the opposite of the one asked for. The schema uses pydantic's `StrictBool` for the same
  reason — the ordinary `bool` also accepts `"yes"`, `"on"` and `1`, which would have made the edge
  looser than the adapter behind it and the rule two rules.
- **No forwarding route was added.** `tests/test_remote.py` still asserts that none exists, and its
  `test_the_router_exposes_only_the_port` is derived from the enumerated list, so both new routes had
  to be declared there to pass. Everything goes through the `Player` port, which is what keeps
  fallback A a drop-in.
- **`consume` stays read-only.** Reported by `GET /player`, no route, no control — and now a test
  saying so, so removing the omission is a deliberate act rather than an oversight.

One thing that was a decision rather than a detail: **the route takes a mode, not a cycle.** The UI
draws one tri-state button because off → queue → one song is the order anybody wants, but it sends
the absolute mode its own label implied. A `/player/repeat/cycle` would have been relative to a
two-second-old poll on a session two phones and OwnTone's own web UI can touch (PLAN.md 2), so two
taps could land somewhere neither phone asked for; an absolute mode is idempotent instead. Both
controls carry their state in their *text* ("Repeat the queue", "Shuffle on") with the border as a
second signal, because a three-way control is the hardest case for this stylesheet's rule that
nothing conveys state by colour alone.

Tests 1120 → 1198. What they cannot cover is the acceptance check: set repeat-one on the phone, let a
track reach its end, and hear it start again on the soundbar. That last part is **hardware** and is
**still unverified** — nothing in this change has been run against the real daemon or the real
speaker. Plain queue advancement was measured working across a track boundary (PLAN.md Phase 3), and
repeat is the same mechanism feeding the same continuous mirroring stream, so it is expected to work.
If it misbehaves, that is a finding for PLAN.md 3.1, alongside the gapless note the mirroring
transport already rewrote.

---

## Tapping the queue plays the song — **done, 2026-09-28**

The Now-playing panel drew the queue and the queue did nothing. Rows rendered, titles were right, the
loaded track was highlighted — and a tap was swallowed, because `renderPlayer` built each row with an
empty action list. So the only way to reach track seven was ⏭ six times, and the one gesture everybody
arrives already knowing was the one thing on the page that was inert.

- [x] **Play a song from the queue** — tap the row.

`PUT /player/queue/{item_id}/play`, `Player.play_item` on the port, and the queue row's main area is
now a real `<button>`.

**Verified against the running daemon, 2026-09-28.** The call was not in the JSON API's route table
as such — `^/api/player/play$` is, and the parameters are in the binary's strings next to their own
error messages (`No queue item with item id '%d'`):

```
PUT /api/player/play?item_id=<in the queue>  -> 204, GET /api/player then reports that item_id
PUT /api/player/play?item_id=99999           -> 400 (HTML body)
PUT /api/player/play?item_id=bogus           -> 400 (HTML body)
PUT /api/player/play?item_id=                -> 400 (HTML body)
```

And end to end through the real app against the real daemon: `200`, `item_id` and `now_playing` move
to the tapped item, the queue comes back identical, a stale id is `404`, no token is `401`.

Four decisions, each of which had a wrong version that looks fine:

- **An id, not a position.** OwnTone's route accepts both. A position is only true of the poll it
  came from, and this session is shared — two phones and OwnTone's own web UI (PLAN.md 2) — so the
  tap that landed on row four would play whatever row four had become. Same argument as the repeat
  *mode* rather than a cycle, and `test_there_is_no_play_by_position_route` pins it.
- **Not a path either.** Paths are this port's vocabulary for the *library*, but the same file can be
  in the queue twice, so a path names no single item. `QueueItem.id` was already in the protocol and
  already reported as `PlayerStatus.item_id`, so nothing about which daemon is playing leaks out that
  was not already out — fallback A still only has to implement `play_item`.
- **The queue is checked before the command is sent.** A remote polls every two seconds, so the
  ordinary failure is a row somebody else removed while a thumb was in the air. Without the check the
  answer would be the daemon's `400` *with an HTML body*, surfacing as a `502` quoting a markup
  fragment; with it, that case is a `404` a client can recognise. It also makes the adapter's own
  integer check unreachable from HTTP, which is why `_item_id` raises `ValueError` and not a
  `TuneboxError` — same reasoning as `_repeat_state`. (`isascii()` as well as `isdigit()`: the latter
  is true of `"٣"` and `int()` accepts it, and no C `strtol` will.)
- **The row is the button, and it is a real `<button>`.** A click handler on the existing `<div>`
  would have looked identical in a screenshot and been unreachable by keyboard, silent to a screen
  reader, and deaf to Enter and Space — the standard way this pattern is got wrong. `row()` grew an
  optional `activate`, and when it is given the main area is a button with the `button` rule's border
  and fill undone (a filled control the width of every row reads as a wall of buttons) but its focus
  ring kept. No per-row "Play" label: a queue is a list of things to play, so the verb on every row
  would be noise, and the row is a 44px target where a corner button is not.

The row already playing is **not** exempt, because a dead row is the bug being fixed; it restarts the
track, and its accessible name says `Restart “…”` rather than `Play “…”` so that is not a surprise.
No CLI verb, as with the last two items: `tunebox player play FILE…` already plays a file directly,
and "the fourth thing in the queue" is a thing you point at, not a thing you type.

One measured thing the UI does not hide: for **~2.5 s after the jump** the daemon answers
`state=pause, item_progress_ms=0` while the Cast output starts, then flips to `play` on its own with
no further command (sampled every 0.5 s on the real soundbar). The response redraws the panel, so the
transport button shows ▶ until the next poll. That is the daemon being honest and the same lag
`playback=start` on a queue add already had, so nothing optimistic was added to paper over it —
rule 2 of `app.js` is that the server is the only source of truth about playback.

Tests 1198 → 1244.

---

## Delete from the library — **done, 2026-09-27**


The Library panel offered **Play** (replace the queue and start) and **Queue** (append) and nothing
else, so the remote was a one-way door: a mistagged ingest, a wrong search result or a duplicate could
be added from the phone and then only removed by finding a terminal.

- [x] **Delete a track from the library** — from the Library panel, and over the API.

`DELETE /library` with `{"paths": [...]}`, `library.delete_track`, and a confirmation strip behind
each row's `⋯`. This is the first destructive operation tunebox exposes and the thing it destroys is
the only irreplaceable state the system has, so all five open questions were decisions; they are
recorded in PLAN.md Phase 4 as well as here, and in the README under *Deleting from the library*.

**What was decided:**

1. **Hard delete, no trash.** The backup is the undo. Which makes this and `TUNEBOX_BACKUP_PRUNE`
   related in a way nothing previously said out loud — with prune on there is no undo left — so both
   the README's backup section and its env-var table now say so, as does PLAN.md Phase 5.
2. **Empty parents are pruned**, upward, stopping at the music root and only while genuinely empty —
   `rmdir` failing *is* the emptiness check, so a parallel ingest writing into the directory between
   a check and a remove cannot lose files. A root holding nothing else survives, and a directory with
   a `cover.jpg` in it does too. Both are tests.
3. **Playing or queued:** the track the daemon has *loaded* is a `409`; anything further down the
   queue is deleted, because OwnTone discovers a missing file on its next `LOAD` and refusing for the
   whole queue would make a queued album undeletable. A daemon that cannot be asked does not block the
   delete — if it is not answering, it is not playing — which is the same indifference the rescan hook
   already had.
4. **Route shape as proposed:** a body, not a wildcard path parameter, and all-or-nothing. Every path
   is resolved through `library.resolve_track_path` before anything is unlinked, so a typo in the
   third entry costs nothing; two spellings of one file are one delete rather than a `400` about a
   file the same request just removed.
5. **Confirmation in the UI, not in the API**, and not as a third button beside Play: `⋯` on the row,
   then a strip naming the track. No `window.confirm` — a browser may suppress it, and in an installed
   web app the modal reads as a crash. No CLI verb, as planned.

Reused rather than rebuilt, as intended: `resolve_track_path` for every path, `player/hooks.rescan_library`
for the "tell OwnTone but never fail because of it" call, and nothing new in the jobs database —
`store.forget_path` drops the history row eagerly, but the worker already treated an entry whose file
had vanished as no entry at all, so re-ingesting a deleted track worked either way.

Not verified by a test, because it cannot be: what OwnTone actually does if the file under the
*current* track disappears anyway — the `409` is there so nobody finds out by accident, but the
daemon's behaviour on a mid-song `ENOENT` is unmeasured. Tests 1056 → 1120.

---

## Playlists

**Feasible, and cheaper than it looks — because a playlist in this architecture is a file, not a
table.** Measured against the running daemon and its binary, 2026-09-27.

- [ ] **Playlists** — create, play, append to, delete. See the staging below; v1 is three verbs.

### What OwnTone actually offers

Its complete JSON API route table, read out of the binary
(`strings ~/owntone_data/usr/sbin/owntone | grep '\^/api/'`), contains:

```
^/api/library/playlists$                    ^/api/queue/items/add$
^/api/library/playlists/[[:digit:]]+$       ^/api/queue/save$
^/api/library/playlists/[[:digit:]]+/tracks$
^/api/library/tracks/[[:digit:]]+/playlists$
```

And a playlist is an **M3U file in the library directory**. Verified end to end: a
`tunebox-probe-DELETEME.m3u` written into the music root and followed by `PUT /api/update` was
indexed as `{"type": "plain", "item_count": 2, "uri": "library:playlist:7"}`; both entry forms
resolved to real tracks — a **relative** path (`Kraftwerk/Singles/Autobahn….m4a`) and an **absolute**
one, the latter with CJK in it. `POST /api/queue/items/add?uris=library:playlist:7` then expanded it
into two queue items in order. Deleting the file and rescanning took it back out (playlists `0`,
songs unchanged at `13`).

**But OwnTone will not write one for us without a config change.** `owntone.conf` ships
`allow_modifying_stored_playlists = false` and an empty `default_playlist_directory`, both commented
out, and the binary carries the matching refusals — *"Invalid playlist save directory, disabling
modifying stored playlists"*, *"Non-writable playlist save directory"*. Exactly the shape of the
`trusted_networks` default in PLAN.md 3.2: a default that has to be changed on purpose.

### The consequence: tunebox should write the M3U itself

Not as a workaround — it is the better design, for four reasons that are all already-made decisions:

- **No new database.** "The filesystem is the library database" (PLAN.md 3.1). A playlist is a text
  file in the music root, so it is backed up by `tunebox backup` with no new code, survives a restore,
  and is readable by Navidrome or MPD if Phase 6 ever adds one.
- **No OwnTone config change**, so the daemon keeps its write-side locked down.
- **Playback needs no new daemon integration at all.** Read the M3U, resolve the entries, hand them
  to the existing `Player.enqueue_paths`. Which means **fallback A gets playlists for free** —
  whereas building on `uris=library:playlist:N` would leak OwnTone's ids through the `Player` port
  and break the drop-in property the port exists for (PLAN.md 3.4).
- **The write path already exists.** `library.place` for the atomic replace, `naming.sanitize_component`
  for the filename, `library.resolve_track_path` for every entry — so a playlist cannot name a file
  outside the music root, which is the whole risk of accepting a list of paths from a phone.

Two properties of the existing code that happen to be exactly right: `.m3u` is not in
`library.AUDIO_EXTENSIONS`, so playlist files will neither appear in `GET /library` nor be
enqueueable as if they were tracks — no code needed to exclude them. And the rescan hook
(`player/hooks.py`) is already the "tell OwnTone, but never fail because of it" path.

### Open questions

1. **Where do the files live?** The root is `Artist/Album/Track` by convention, so a top-level
   `Playlists/` directory is a pseudo-artist. Harmless to tunebox (it lists audio files only) and it
   is what OwnTone's own browse view expects, but **a hidden directory will not work** — the scanner
   skips dot-entries, which is the same rule that keeps `.tunebox-tmp` out of the library.
2. **Relative or absolute entries?** Relative is verified working and survives the library moving —
   the symlinked external SSD of PLAN.md 3.1, and a restore to a different path. The catch: relative
   resolves against *the playlist file's own* directory, so a `Playlists/` subdirectory means entries
   like `../Artist/Singles/Track.m4a`. Only the root-level case was measured; the subdirectory case
   is **untested** and is the first thing to check.
3. **Concurrent edits.** Two phones editing one playlist is last-write-wins. Read-modify-write
   through `library.place`'s atomic replace is enough at one household's scale; say so rather than
   leaving it unstated.
4. **Interaction with library delete** (previous item, now implemented): deleting a track will leave a
   dangling entry in any playlist that names it. OwnTone simply does not resolve it, so nothing breaks,
   but a playlist that silently shrinks is worth either pruning on delete or reporting — and the place
   to do it is `DELETE /library`, which already returns the directories it removed and could return the
   playlists it left holes in.
5. **`POST /api/queue/save` is a tempting one-tap "save this queue as a playlist"** and it is the one
   feature that genuinely needs the config flip. Worth it only if the flip is accepted; tunebox can
   produce the same result by writing the file from `Player.queue()` instead, with no flip at all.

### Staging

v1 is three verbs and no reordering UI: **create from the current queue**, **play a playlist**
(replace queue and start), and **append one track**. Rename, reorder, nested folders and smart
playlists are all things OwnTone's own web UI can do on the same files — Phase 4 already decided to
link out for the expensive half of a music UI rather than rebuild it, and that decision applies here
unchanged.


