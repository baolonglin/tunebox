# Tunebox — TODO

Small wants, in no particular order. Anything here is a feature on top of an architecture that is
already decided; if an item turns out to need a *decision*, it belongs in `PLAN.md` instead.

---

## A speaker in standby, and the silence nobody was told about — **done, 2026-09-30**

The remote's **Turn the speaker on** button failed with an error nobody could read, and the screen
above it claimed to be playing music while the room was silent. Both were found by using it, both
were measured on the real daemon, and they are the same incident:

```
PUT /api/outputs/1234567890 {"selected": true}   -> 400  <html>…400 Bad Request…</html>
PUT /api/outputs/1234567890 {"selected": false}  -> 204
PUT /api/outputs/1234567890 {"volume": 40}       -> 204
```

Only *enabling* failed, so it was neither the id, the body nor the credentials. OwnTone's own log had
the reason, and it is the interesting part:

```
player: Speaker enable: 'Living Room speaker' (id=1234567890)
misc:   Connecting to 'Chomecast control' at 192.168.50.13 (port 8009)
misc:   Could not connect … : No route to host
cast:   Could not connect to Living Room speaker
web:    JSON api request failed with error code 400 (/api/outputs/1234567890)
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

## Playlists — **done, 2026-10-03**

**Feasible, and cheaper than it looks — because a playlist in this architecture is a file, not a
table.** Measured against the running daemon and its binary, 2026-09-27.

- [x] **Playlists** — create, play, append to, delete. `src/tunebox/playlists.py`, six
  `/playlists` routes, `tunebox playlist <verb>`, and a fourth tab on the remote.

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

(That last sentence turned out to be **wrong**, and the audit caught it: the same
`allow_modifying_stored_playlists = false` that stops the daemon writing a playlist stops its web UI
editing one, so there is nothing to link out *to* for a reorder. The staging decision stands — v1 has
no reorder — but the reason is now "a text file and `$EDITOR`, or rebuild the queue and save over the
list", not "OwnTone does it".)

### What was decided

All five open questions above were answered rather than deferred; they are recorded here and in the
README under *Playlists*. Shipped slightly wider than the staging said — `list`, `show` and `delete`
came along because each is a few lines once the reader and the writer exist, and a feature that can
create a playlist and not see or remove one is a feature that needs a terminal to undo a typo.

1. **Where the files live: a visible `Playlists/` directory under the music root**
   (`TUNEBOX_PLAYLISTS_DIR`). The pseudo-artist is harmless — `.m3u` is not in
   `library.AUDIO_EXTENSIONS`, so `GET /library` never lists one and nothing can enqueue one as a
   song, with no code written to exclude it. A dot-directory is refused at startup rather than
   silently accepted, because the files in it would never be indexed. `naming.sanitize_component`
   already strips a leading dot, so the check in `Config` exists to *say so*, not to enforce it.
2. **Relative entries, relative to the playlist file** — so `../Artist/Album/Track.m4a` from a
   subdirectory. Verified as written bytes in a test, because round-tripping through this module alone
   would still pass if they quietly became absolute. **And verified against the real daemon**, which
   was the one thing question 2 called out as untested: a `tunebox playlist create` into
   `Playlists/`, then the rescan tunebox triggers itself, left OwnTone 29.3 reporting
   `{"type": "plain", "item_count": 1, "uri": "library:playlist:7"}` and
   `GET /api/library/playlists/7/tracks` resolving the `../Imagine Dragons/Singles/Believer.m4a`
   entry to the real track with its correct absolute path — so **`../` out of a subdirectory works**
   (measured 2026-10-03, probe deleted afterwards: playlists back to 0, songs unchanged at 34). It
   would not have mattered to playback either way, because tunebox resolves the entries itself and
   hands `enqueue_paths` absolute paths; what it buys is the playlist appearing in OwnTone's own
   browse view, which is where the footer link sends people to reorder one. An absolute entry is read
   and never written.
3. **Concurrent edits: last-write-wins, stated.** Every edit is a read-modify-write. The *write* is
   atomic — staged under a dot-name beside the target, then `os.replace` — so a scanner cannot read
   half a list and a crash cannot truncate one. The merge is not attempted, deliberately.
4. **Library delete prunes, and reports.** `DELETE /library` calls `playlists.forget_tracks` **before**
   the unlink, which is what makes it exact rather than a guess: while the file still exists its entry
   resolves, so it is matched by `(device, inode)` — which covers the case-insensitive spelling, the
   NFD form of a CJK filename and a hard link — and an entry that was already dangling for an
   unrelated reason is left alone. The playlists that changed come back in `playlists_updated`.
   Pruning rather than merely reporting, because OwnTone skips an unresolvable entry silently — the
   alternative is a list that is one track shorter forever with nothing having mentioned it.
5. **`POST /api/queue/save` is not used, and the config flip was not needed.** `from_queue` reads the
   queue through the `Player` port and writes the file here, so the one-tap "save what is playing" is
   the *default* way a playlist is made on the phone — there is no track-picker in the UI at all,
   because the queue already is one. Queue items that are not library tracks are skipped and named in
   `skipped` rather than failing the save; explicit `paths` stay all-or-nothing.

Two distinctions that are rules because they were bugs first, both found by an audit of this feature
before it shipped:

- **Creating a playlist sanitises the name; addressing one does not.** Sanitising a lookup means
  `a:b` and `a_b` name the same file, so `DELETE /playlists/a:b` removed `a_b.m3u` — somebody else's
  list. `normalise_name` is now the write-side function and `exact_name` the read-side one, which
  refuses a name that is not already a usable filename rather than quietly rewriting it.
- **Reading a playlist is never capped; writing one is.** `MAX_ENTRIES` applied to the read meant a
  hand-written 1005-entry playlist was *read* as 1000 — and then the next unrelated `DELETE /library`
  rewrote the file from that truncated reading and destroyed the last five lines. Measured: 1005 → 999.

Reused rather than rebuilt: `library.resolve_track_path` for every entry in both directions (so a
playlist cannot name a file outside the music root, and a hand-written one naming a URL reports it as
unresolvable rather than handing it to the player), `library.describe_track` so a playlist's tracks
read exactly like `GET /library` rows, `naming.sanitize_component` for the name — a playlist called
`Road trip: 2026/07` is one component, `Road trip_ 2026_07`, and the create response says so —
`player/hooks.rescan_library` for the "tell OwnTone but never fail because of it" call, and
`remote.player_view` so `PUT /playlists/{name}/play` answers with the same document as
`PUT /player/play`.

Also reported rather than hidden: **an entry that resolves to nothing**. `GET /playlists/{name}` has a
`missing` list, the phone's row says *"8 tracks · 2 missing"*, and `tunebox playlist play` says so on
stderr. A list that plays eight of its ten tracks in silence is this feature's most likely failure and
the one OwnTone will not mention.

### What the audit found

An independent review before shipping found five real defects, all reproduced and then fixed; they are
worth recording because three of them are the same mistake in different clothes — *a rewrite that
knows less about the file than the file does*.

1. **A write outside the music root.** `forget_tracks` and `list_playlists` joined the directory name
   and called `iterdir` with no containment check, so with `Playlists/` replaced by a symlink pointing
   out of the tree a `DELETE /library` read *and rewrote* a file elsewhere on disk. A direct violation
   of PLAN.md 3.2, measured. `_playlists_dir` now puts `naming.resolve_within` in front of every walk,
   and each `.m3u` is checked too, so a single symlinked playlist file is skipped rather than read.
2. **Silent truncation of long playlists** — the read cap, above.
3. **`#EXTINF` and comments destroyed** by an append or a prune, in files tunebox did not write. Every
   edit now carries the file across line for line; a prune removes an entry and the `#EXTINF`
   immediately above it, and nothing else.
4. **The prune missed on this project's own filesystem.** Matching by path string meant an entry
   spelled `../A/SINGLES/t.m4a` on case-insensitive macOS resolved to the file being deleted and
   compared unequal to it, so it survived into a playlist that could never play it. Inode identity now.
5. **Three reachable 500s** — `LibraryError` had no handler and fell through to the `TuneboxError`
   catch-all, so a path parameter that refused a traversal reported itself as a server fault. It is
   mapped to `400` in `api/app.py` now, which is what the routes catching it inline already did.

Also from the audit: `list_playlists` derived each name by sanitising the stem, which hid any file
whose name would not survive that (a hand-made `Road:trip.m3u` appeared in no listing) and showed two
different files as one row; it now reports the real filename. `Config.playlists_dir` was removed as a
second, unchecked answer to "where are the playlists". An `OSError` from the writer is no longer
dressed up as a `400`, because a read-only disk is not a malformed request. And the claim that
OwnTone's web UI could reorder these files was wrong, as noted above.

### What the first version got wrong, and the second one fixed

Shipped, used, and the verdict was three sentences: *only the queue can create a playlist; after
creating one it is hard to know which songs are in it; and it cannot be modified.* All three are the
same mistake — the staging above ("v1 is three verbs and no reordering UI") was drawn up on the
assumption that OwnTone's web UI could do the rest, and that assumption was already known to be false
by the time it shipped. With no reorder, no remove and no rename anywhere, a playlist was a write-once
file: one wrong song in forty meant rebuilding the list.

- **Seeing inside one.** `GET /playlists/{name}` now answers with `entries` — every line that names
  something, in order, each with a `position` and either its track or a flag saying it resolves to
  nothing. On the phone the playlist's name became the control: tap it and the row expands into the
  numbered list. The old `tracks`/`missing` pair is gone from the JSON; they are properties of the
  model, derived from `entries`, so the two cannot drift.
- **An entry is named by its position, not its path.** The distinction the old shape could not express:
  a playlist may hold the same track twice, and an entry may resolve to nothing — so "remove the second
  copy" and "remove that dangling line" are both unsayable with a path, and an index into the *playable*
  tracks is not an index into the file. `DELETE /playlists/{name}/tracks` takes `positions` (lines) or
  `paths` (every copy of a track), exactly one.
- **Reordering is one move at a time.** `PUT /playlists/{name}/tracks/{position}` with `{"to": n}`,
  which is what ↑ and ↓ send. Not a whole new order, for the reason `PUT /player/repeat` takes a mode
  rather than a cycle: two phones can be looking at one list, and an order computed from a
  two-second-old reading would silently undo the other one's edit.
- **Renaming.** `PATCH /playlists/{name}`. It also brings tunebox's own `# tunebox playlist:` comment
  line up to date, so a file somebody opens does not disagree with its own name; a header another
  program wrote is left alone.
- **Creating without the queue.** A name on its own now makes an empty playlist, which is the thing
  that was actually missing: `paths` needs a client that already knows what it wants and `from_queue`
  needs the list to be the thing playing, so there was nowhere to put a *first* track. The CLI keeps
  requiring `--empty` explicitly, because at a prompt a bare `create NAME` is far more likely to be a
  forgotten argument than a request for an empty list.

One internal change carried all of it: edits operate on **blocks** (`playlists._Block`) rather than
lines. A block is an entry plus the `#EXTINF` immediately above it, ending where the next entry begins,
with everything before the first entry as an untouchable preamble. That is what makes "move track two"
carry its own metadata with it, "remove track one" leave the header alone, and a playlist another
program wrote survive an edit line for line.

Found while testing it, and fixed: the `409` added for a stale position swallowed `PlaylistNotFound`
(it *is* a `PlaylistError`), so editing a playlist that did not exist answered "that playlist is in the
way" instead of `404`.

Not verified: **two phones editing one playlist at the same instant** — last-write-wins is a decision
rather than a measurement, and the atomic write is tested only in the sense that no `.partial` file
survives one. **Nothing has run on a Pi**, as everywhere else. Tests 1276 → 1479 → 1562.


