# Tunebox — TODO

Small wants, in no particular order. Anything here is a feature on top of an architecture that is
already decided; if an item turns out to need a *decision*, it belongs in `PLAN.md` instead.

---

## Playback modes

OwnTone owns the queue (PLAN.md 2), so all three of these are daemon features that tunebox simply
does not expose yet. **Verified against the running daemon on this host, 2026-09-27:**

```
PUT /api/player/repeat?state=single   -> 204, repeat=single
PUT /api/player/repeat?state=all      -> 204, repeat=all
PUT /api/player/repeat?state=off      -> 204, repeat=off
PUT /api/player/shuffle?state=true    -> 204, shuffle=true
PUT /api/player/shuffle?state=false   -> 204, shuffle=false
```

The read half already exists end to end: `PlayerStatus` carries `repeat`, `shuffle` and `consume`
(`core/models.py`), `player/owntone.py` populates them from `/api/player`, and `GET /player` returns
them (`api/remote.py`, `api/schemas.py`). What is missing is the write half and a control on the page.

- [ ] **Repeat one song** — `repeat=single`.
- [ ] **Repeat the queue** — `repeat=all`.
- [ ] **Shuffle** — `shuffle=true|false`.

One change, not three: `set_repeat` / `set_shuffle` on the `Player` port in `core/ports.py`, the
matching pair in `player/owntone.py`, `PUT /player/repeat` and `PUT /player/shuffle` added to the
enumerated route list in `api/remote.py`, a tri-state repeat button and a shuffle toggle on the
now-playing panel, and the route list in README's *What is and is not exposed* kept truthful.

Three things worth getting right, each of which is a real trap rather than a style note:

- **Validate in our adapter, not in the daemon.** `state=bogus` returns `204` on both routes and is
  silently taken as "off"/"false" — measured above. So a typo in a client would look like success and
  quietly change the mode. Reject anything outside `off|all|single` and a real boolean, and test it.
- **Do not add a forwarding route.** `tests/test_remote.py` asserts that none exists, because tunebox
  holds OwnTone's credentials and talks to it from a network OwnTone trusts (PLAN.md Phase 4). These
  go through the `Player` port like the thirteen `/player/*` routes already in that file, which is
  also what keeps fallback A a drop-in.
- **`consume` stays read-only for now.** It is reported because it is part of the daemon's state, but
  a queue that eats itself is not a mode anybody asked for, and exposing a control is easier to add
  later than to take away.

Acceptance: set repeat-one on the phone, let a track reach its end, and hear it start again on the
soundbar. That last part is a **hardware** check and cannot be a test. Plain queue advancement was
measured working across a track boundary (PLAN.md Phase 3), and repeat is the same mechanism feeding
the same continuous mirroring stream, so it is expected to work — but it is unverified, and the Cast
path in use has already produced one surprise per phase. If it does misbehave, that is a finding for
PLAN.md 3.1, alongside the gapless note the mirroring transport already rewrote.

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


