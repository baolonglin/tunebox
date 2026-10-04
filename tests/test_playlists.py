"""Playlists — the module, and the ``/playlists`` routes (TODO.md "Playlists").

Two halves, and the first one is where the interesting assertions are. A playlist in tunebox is a
file, so the tests that matter are about *the file*: what is written into it, what is read back out of
it, and what happens when the tree it points into changes underneath it. The API half is then mostly
status codes, because every decision it makes has already been made in the module.

The file format is asserted literally in places, which is deliberate rather than brittle: the entries
being relative to the playlist file (``../Artist/Album/Track.m4a``) is a property OwnTone and every
other scanner depends on, and a change that quietly made them absolute would still pass every test
that only round-tripped through this module.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tunebox import playlists
from tunebox.config import Config
from tunebox.core.models import Playlist
from tunebox.errors import ConfigError, LibraryError, PlaylistError, PlaylistExists, PlaylistNotFound
from tunebox.jobs.store import JobStore
from tunebox.library import list_tracks

from fakes import FakePlayer, FakeSource, RecordingTagWriter

from tunebox.api.app import create_app

TOKEN = "test-token-that-is-long-enough"
AUTH = {"X-Tunebox-Token": TOKEN}

#: One Latin track and one CJK one, because the CJK path is the one this household actually has and
#: the one where an encoding mistake in a text file would show up (PLAN.md Phase 0).
AUTOBAHN = "Kraftwerk/Autobahn/01 - Autobahn.m4a"
KSONG = "陳奕迅/Singles/K歌之王.m4a"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    music = tmp_path / "music"
    for relative in (AUTOBAHN, KSONG, "Kraftwerk/Autobahn/02 - Kometenmelodie.m4a"):
        path = music / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")
    return music


# ------------------------------------------------------------------ the file itself


class TestWhatIsWritten:
    """The bytes on disk, asserted as bytes. Everything else here depends on these."""

    def test_entries_are_relative_to_the_playlist_file(self, root: Path):
        """``../Artist/Album/Track.m4a``, which is what M3U means and what survives the library
        moving — the symlinked external SSD of PLAN.md 3.1, and a restore to a different path.

        The ``..`` is the part only measurable here: the playlist lives in a *subdirectory*, so the
        relative form has to climb out of it, and that case was explicitly untested against the real
        daemon (TODO.md open question 2). tunebox resolving the entries itself for playback is what
        keeps that an OwnTone-browse-view question rather than a playback one.
        """
        playlists.write_playlist(root, "Road trip", [AUTOBAHN, KSONG])
        body = (root / "Playlists" / "Road trip.m3u").read_text(encoding="utf-8")
        assert body.splitlines() == [
            "#EXTM3U",
            "# tunebox playlist: Road trip",
            f"../{AUTOBAHN}",
            f"../{KSONG}",
        ]

    def test_no_absolute_path_is_ever_written(self, root: Path):
        """Even when the caller supplied one. What tunebox produces is portable; what it *reads* is
        allowed to be either, so a hand-written file and one from another program both work."""
        playlists.write_playlist(root, "Mixed", [str(root / AUTOBAHN), KSONG])
        body = (root / "Playlists" / "Mixed.m3u").read_text(encoding="utf-8")
        assert str(root) not in body
        assert body.count("../") == 2

    def test_it_is_utf8_and_the_cjk_title_survives(self, root: Path):
        playlists.write_playlist(root, "廣東歌", [KSONG])
        path = root / "Playlists" / "廣東歌.m3u"
        assert "K歌之王" in path.read_text(encoding="utf-8")
        assert path.read_bytes().decode("utf-8")  # not latin-1, not mojibake

    def test_the_order_given_is_the_order_written(self, root: Path):
        second = "Kraftwerk/Autobahn/02 - Kometenmelodie.m4a"
        playlists.write_playlist(root, "Backwards", [second, AUTOBAHN])
        body = (root / "Playlists" / "Backwards.m3u").read_text(encoding="utf-8")
        assert body.index("Kometenmelodie") < body.index("01 - Autobahn")

    def test_the_same_track_twice_is_kept_twice(self, root: Path):
        """People do this on purpose, and a list somebody arranged by hand is the wrong place to be
        clever about duplicates."""
        playlist = playlists.write_playlist(root, "Twice", [AUTOBAHN, AUTOBAHN])
        assert playlist.count == 2

    def test_nothing_is_left_behind_by_a_write(self, root: Path):
        """The staging file is dot-prefixed *and* removed: a music scanner skips dot-entries, and the
        replace is what makes the visible file appear whole or not at all."""
        playlists.write_playlist(root, "Clean", [AUTOBAHN])
        assert [p.name for p in (root / "Playlists").iterdir()] == ["Clean.m3u"]

    def test_the_playlist_is_not_offered_as_a_track(self, root: Path):
        """``.m3u`` is not in :data:`tunebox.library.AUDIO_EXTENSIONS`, so the file neither appears in
        ``GET /library`` nor can be enqueued as if it were a song — with no code to exclude it. The
        visible ``Playlists/`` directory is a pseudo-artist that nothing lists, which is the whole
        reason it may be visible (TODO.md open question 1)."""
        playlists.write_playlist(root, "Invisible", [AUTOBAHN])
        page = list_tracks(root)
        assert all("Playlists/" not in track.path for track in page.tracks)
        assert page.total == 3

    def test_a_hidden_playlists_directory_cannot_happen(self, root: Path):
        """A scanner skips dot-entries, so playlists in one would exist and never be indexed
        (TODO.md open question 1). ``Config`` refuses such a name outright, and the sanitiser makes it
        unreachable even when a caller passes the directory directly: the dot is stripped, so the
        files land somewhere visible rather than somewhere silent."""
        playlists.write_playlist(root, "Nope", [AUTOBAHN], directory=".lists")
        assert (root / "lists" / "Nope.m3u").is_file()
        assert not (root / ".lists").exists()


class TestNames:
    """The name is the filename, so it goes through the same sanitiser a track name does."""

    def test_a_name_with_a_separator_becomes_one_component(self, root: Path):
        playlist = playlists.write_playlist(root, "Road trip: 2026/07", [AUTOBAHN])
        assert playlist.name == "Road trip_ 2026_07"
        assert playlist.path == "Playlists/Road trip_ 2026_07.m3u"

    @pytest.mark.parametrize("name", ["", "   ", ".", "..", "...", " . "])
    def test_a_name_that_sanitises_to_nothing_is_refused(self, name: str):
        """There is no sensible "Unknown Playlist" to fall back to, so this is an error rather than a
        default — the caller asked for something that cannot be a filename."""
        with pytest.raises(PlaylistError):
            playlists.normalise_name(name)

    @pytest.mark.parametrize(
        "name,expected",
        [("/", "_"), ("../../evil", "_.._evil"), ("a/b/c", "a_b_c"), ("CON", "_CON")],
    )
    def test_a_name_that_looks_like_a_path_is_flattened(self, name: str, expected: str):
        """Not refused but *flattened*, by the same sanitiser that names every track: a separator
        becomes an underscore, so there is no spelling of a playlist name that is two directories or
        a climb out of the music root. ``CON`` is the Windows device name, for a library shared over
        SMB."""
        assert playlists.normalise_name(name) == expected

    def test_the_extension_is_accepted_and_not_doubled(self, root: Path):
        """``tunebox playlist show "Road trip.m3u"`` is what somebody who just looked in the
        directory will type."""
        playlists.write_playlist(root, "Road trip", [AUTOBAHN])
        assert playlists.read_playlist(root, "Road trip.m3u").name == "Road trip"
        assert not (root / "Playlists" / "Road trip.m3u.m3u").exists()

    def test_a_traversing_name_writes_inside_the_root_anyway(self, root: Path):
        """The flattening above, checked where it matters: a name of ``../../evil`` produces one file
        inside the playlists directory and nothing anywhere else."""
        playlist = playlists.write_playlist(root, "../../evil", [AUTOBAHN])
        assert playlist.path == "Playlists/_.._evil.m3u"
        assert (root / "Playlists" / "_.._evil.m3u").is_file()
        assert not (root.parent / "evil.m3u").exists()
        assert not (root.parent.parent / "evil.m3u").exists()


class TestEveryEntryGoesThroughTheLibraryResolver:
    """The whole risk of accepting a list of paths from a phone, handled in one place."""

    @pytest.mark.parametrize(
        "entry",
        [
            "../../../etc/passwd",
            "/etc/passwd",
            "Kraftwerk/../../outside.m4a",
            "Kraftwerk/Autobahn/nothing-here.m4a",
        ],
    )
    def test_a_path_outside_the_library_is_refused(self, root: Path, entry: str):
        with pytest.raises(LibraryError):
            playlists.write_playlist(root, "Evil", [entry])

    def test_a_non_audio_file_is_refused(self, root: Path):
        (root / "Kraftwerk" / "notes.txt").write_text("hello")
        with pytest.raises(LibraryError):
            playlists.write_playlist(root, "Notes", ["Kraftwerk/notes.txt"])

    def test_a_refused_entry_creates_no_file(self, root: Path):
        """All-or-nothing, the same property ``POST /player/queue`` and ``DELETE /library`` have: a
        typo in the second entry must not leave half a playlist."""
        with pytest.raises(LibraryError):
            playlists.write_playlist(root, "Half", [AUTOBAHN, "nope.m4a"])
        assert not (root / "Playlists").exists()

    def test_an_empty_list_is_allowed_and_makes_an_empty_playlist(self, root: Path):
        """Reversed deliberately: refusing one made "save the queue" the only way a playlist could come
        into existence, which left somebody assembling one track by track with nowhere to put the
        first track. An empty M3U is valid and is a usable destination for ``append_tracks``."""
        playlist = playlists.write_playlist(root, "Empty", [])
        assert playlist.count == 0 and playlist.entries == ()
        assert (root / "Playlists" / "Empty.m3u").read_text(encoding="utf-8").splitlines() == [
            "#EXTM3U",
            "# tunebox playlist: Empty",
        ]
        assert playlists.append_tracks(root, "Empty", [AUTOBAHN]).count == 1

    def test_appending_nothing_is_still_refused(self, root: Path):
        """``append_tracks`` is the one that says "nothing to add", because for it that is accurate."""
        playlists.write_playlist(root, "Empty", [])
        with pytest.raises(PlaylistError, match="nothing to add"):
            playlists.append_tracks(root, "Empty", [])

    def test_a_list_longer_than_the_cap_is_refused(self, root: Path):
        with pytest.raises(PlaylistError, match="at most"):
            playlists.write_playlist(root, "Huge", [AUTOBAHN] * (playlists.MAX_ENTRIES + 1))


class TestReadingItBack:
    def test_a_round_trip_describes_the_tracks_like_the_library_does(self, root: Path):
        """Same vocabulary as a ``GET /library`` row — same title, same artist, same relative path —
        because both go through :func:`tunebox.library.describe_track`."""
        playlists.write_playlist(root, "Road trip", [AUTOBAHN, KSONG])
        playlist = playlists.read_playlist(root, "Road trip")
        assert [track.path for track in playlist.tracks] == [AUTOBAHN, KSONG]
        assert [track.title for track in playlist.tracks] == ["Autobahn", "K歌之王"]
        assert playlist.tracks[0].artist == "Kraftwerk"
        assert playlist.tracks[0].album == "Autobahn"
        assert playlist.tracks[0].track_number == 1
        assert playlist.tracks[1].album is None  # Singles is a directory, not a record
        assert playlist.count == 2 and playlist.missing == ()

    def test_an_absolute_entry_resolves(self, root: Path):
        """Verified working against the real daemon, so a playlist written by something else — or by
        hand — must work here too."""
        self.write_raw(root, "Hand written", [str(root / KSONG)])
        playlist = playlists.read_playlist(root, "Hand written")
        assert [track.path for track in playlist.tracks] == [KSONG]

    def test_comments_and_blank_lines_are_skipped(self, root: Path):
        self.write_raw(root, "Commented", ["#EXTINF:213,Kraftwerk - Autobahn", "", f"../{AUTOBAHN}"])
        assert playlists.read_playlist(root, "Commented").count == 1

    def test_an_entry_that_resolves_to_nothing_is_reported_not_dropped(self, root: Path):
        """A playlist that silently shrinks is the failure worth naming: OwnTone skips an entry it
        cannot resolve and says nothing about it."""
        self.write_raw(root, "Holes", [f"../{AUTOBAHN}", "../Kraftwerk/Autobahn/gone.m4a"])
        playlist = playlists.read_playlist(root, "Holes")
        assert playlist.count == 1
        assert playlist.missing == ("../Kraftwerk/Autobahn/gone.m4a",)

    @pytest.mark.parametrize(
        "entry",
        [
            "http://example.com/stream.mp3",
            "https://example.com/stream.mp3",
            "../../../etc/passwd",
            "/etc/passwd",
        ],
    )
    def test_a_url_or_an_escape_is_unresolvable_rather_than_followed(self, root: Path, entry: str):
        """A playlist is a list of files in *this* library. A remote URL is not fetched, not handed
        to the player, and not an error either — it is simply an entry that names no track."""
        self.write_raw(root, "Suspicious", [f"../{AUTOBAHN}", entry])
        playlist = playlists.read_playlist(root, "Suspicious")
        assert [track.path for track in playlist.tracks] == [AUTOBAHN]
        assert playlist.missing == (entry,)

    def test_a_windows_separator_still_resolves(self, root: Path):
        """A hand-written file, or one copied off a NAS share. Cheap to accept, confusing to refuse."""
        self.write_raw(root, "Windows", ["..\\Kraftwerk\\Autobahn\\01 - Autobahn.m4a"])
        assert playlists.read_playlist(root, "Windows").count == 1

    def test_reading_one_that_is_not_there_is_its_own_error(self, root: Path):
        with pytest.raises(PlaylistNotFound):
            playlists.read_playlist(root, "Never existed")

    def test_resolved_paths_are_absolute_and_in_order(self, root: Path):
        playlists.write_playlist(root, "Road trip", [AUTOBAHN, KSONG])
        paths = playlists.resolved_paths(root, playlists.read_playlist(root, "Road trip"))
        assert paths == [root / AUTOBAHN, root / KSONG]
        assert all(path.is_absolute() for path in paths)

    @staticmethod
    def write_raw(root: Path, name: str, entries: list[str]) -> Path:
        """A playlist written by something other than tunebox — by hand, or by another program."""
        path = root / "Playlists" / f"{name}.m3u"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(["#EXTM3U", *entries]) + "\n", encoding="utf-8")
        return path


class TestListing:
    def test_name_order_rather_than_newest_first(self, root: Path):
        """The opposite of ``GET /library``, deliberately: a playlist is a thing with a name that
        somebody is looking *for*, and a list that reordered itself on every edit could not be
        pointed at."""
        for name in ("zed", "Alpha", "middle"):
            playlists.write_playlist(root, name, [AUTOBAHN])
        assert [p.name for p in playlists.list_playlists(root)] == ["Alpha", "middle", "zed"]

    def test_a_listing_carries_counts_but_not_tracks(self, root: Path):
        """So that drawing a sidebar is not sending the whole library back — and so an empty playlist
        is still distinguishable from a summary."""
        playlists.write_playlist(root, "Road trip", [AUTOBAHN, KSONG])
        (found,) = playlists.list_playlists(root)
        assert found.count == 2 and found.tracks == ()

    def test_no_directory_yet_is_no_playlists(self, root: Path):
        assert playlists.list_playlists(root) == ()

    def test_only_playlist_files_are_listed(self, root: Path):
        playlists.write_playlist(root, "Real", [AUTOBAHN])
        (root / "Playlists" / "cover.jpg").write_bytes(b"\xff\xd8\xff")
        (root / "Playlists" / ".Hidden.m3u").write_text("#EXTM3U\n")
        (root / "Playlists" / "sub").mkdir()
        assert [p.name for p in playlists.list_playlists(root)] == ["Real"]


class TestAppending:
    def test_it_goes_on_the_end(self, root: Path):
        playlists.write_playlist(root, "Road trip", [AUTOBAHN])
        playlist = playlists.append_tracks(root, "Road trip", [KSONG])
        assert [track.path for track in playlist.tracks] == [AUTOBAHN, KSONG]

    def test_an_unresolvable_line_is_carried_across_untouched(self, root: Path):
        """This verb is "add something". Somebody who has never been told about a dangling entry must
        not discover that adding a track removed one."""
        TestReadingItBack.write_raw(root, "Holes", [f"../{AUTOBAHN}", "../gone.m4a"])
        playlist = playlists.append_tracks(root, "Holes", [KSONG])
        assert playlist.missing == ("../gone.m4a",)
        assert [track.path for track in playlist.tracks] == [AUTOBAHN, KSONG]

    def test_appending_to_one_that_is_not_there_is_a_not_found(self, root: Path):
        with pytest.raises(PlaylistNotFound):
            playlists.append_tracks(root, "Never existed", [AUTOBAHN])

    def test_a_bad_path_changes_nothing(self, root: Path):
        playlists.write_playlist(root, "Road trip", [AUTOBAHN])
        before = (root / "Playlists" / "Road trip.m3u").read_bytes()
        with pytest.raises(LibraryError):
            playlists.append_tracks(root, "Road trip", ["/etc/passwd"])
        assert (root / "Playlists" / "Road trip.m3u").read_bytes() == before

    def test_the_cap_applies_to_the_total(self, root: Path):
        playlists.write_playlist(root, "Nearly full", [AUTOBAHN] * (playlists.MAX_ENTRIES - 1))
        with pytest.raises(PlaylistError, match="exceed"):
            playlists.append_tracks(root, "Nearly full", [KSONG, AUTOBAHN])


class TestCreateTwice:
    def test_the_second_one_is_refused(self, root: Path):
        playlists.write_playlist(root, "Road trip", [AUTOBAHN])
        with pytest.raises(PlaylistExists):
            playlists.write_playlist(root, "Road trip", [KSONG])

    def test_the_first_one_is_untouched_by_the_refusal(self, root: Path):
        playlists.write_playlist(root, "Road trip", [AUTOBAHN])
        with pytest.raises(PlaylistExists):
            playlists.write_playlist(root, "Road trip", [KSONG])
        assert [t.path for t in playlists.read_playlist(root, "Road trip").tracks] == [AUTOBAHN]

    def test_replacing_is_possible_on_purpose(self, root: Path):
        playlists.write_playlist(root, "Road trip", [AUTOBAHN])
        playlist = playlists.write_playlist(root, "Road trip", [KSONG], overwrite=True)
        assert [track.path for track in playlist.tracks] == [KSONG]


class TestDeleting:
    def test_it_removes_the_file_and_no_audio(self, root: Path):
        playlists.write_playlist(root, "Road trip", [AUTOBAHN, KSONG])
        assert playlists.delete_playlist(root, "Road trip") == "Playlists/Road trip.m3u"
        assert not (root / "Playlists" / "Road trip.m3u").exists()
        assert (root / AUTOBAHN).is_file() and (root / KSONG).is_file()

    def test_the_directory_survives_the_last_playlist(self, root: Path):
        """Unlike the empty artist directories a *track* delete prunes: this one is a fixture of the
        library rather than a by-product, it will be wanted by the next create, and an empty directory
        appears in no browse view."""
        playlists.write_playlist(root, "Only one", [AUTOBAHN])
        playlists.delete_playlist(root, "Only one")
        assert (root / "Playlists").is_dir()

    def test_deleting_one_that_is_not_there(self, root: Path):
        with pytest.raises(PlaylistNotFound):
            playlists.delete_playlist(root, "Never existed")


class TestWhatALibraryDeleteDoesToThem:
    """TODO.md open question 4, which was the one genuinely open question in the design.

    A deleted track leaves a dangling entry in every playlist that named it. OwnTone does not resolve
    it and nothing breaks — which is exactly the problem: the playlist is quietly one track shorter
    forever and nothing ever said so. So the entry is pruned, and the playlists that changed are
    reported.
    """

    def test_the_entry_is_removed(self, root: Path):
        playlists.write_playlist(root, "Road trip", [AUTOBAHN, KSONG])
        changed = playlists.forget_tracks(root, [root / AUTOBAHN])
        assert changed == ("Playlists/Road trip.m3u",)
        assert [t.path for t in playlists.read_playlist(root, "Road trip").tracks] == [KSONG]

    def test_an_already_dangling_entry_is_left_exactly_where_it_is(self, root: Path):
        """The reason this runs *before* the unlink: while the track still exists its entry resolves,
        so it is matched by path rather than guessed at — and somebody else's delete does not sweep up
        a line that was broken for an unrelated reason."""
        TestReadingItBack.write_raw(
            root, "Holes", [f"../{AUTOBAHN}", "../was-broken-already.m4a", f"../{KSONG}"]
        )
        playlists.forget_tracks(root, [root / AUTOBAHN])
        body = (root / "Playlists" / "Holes.m3u").read_text(encoding="utf-8")
        assert "was-broken-already.m4a" in body
        assert "01 - Autobahn" not in body

    def test_a_playlist_that_does_not_name_it_is_not_rewritten(self, root: Path):
        playlists.write_playlist(root, "Unrelated", [KSONG])
        before = (root / "Playlists" / "Unrelated.m3u").read_bytes()
        assert playlists.forget_tracks(root, [root / AUTOBAHN]) == ()
        assert (root / "Playlists" / "Unrelated.m3u").read_bytes() == before

    def test_an_absolute_entry_is_matched_too(self, root: Path):
        TestReadingItBack.write_raw(root, "Absolute", [str(root / AUTOBAHN)])
        assert playlists.forget_tracks(root, [root / AUTOBAHN]) == ("Playlists/Absolute.m3u",)
        assert playlists.read_playlist(root, "Absolute").count == 0

    def test_nothing_to_forget_is_not_an_error(self, root: Path):
        assert playlists.forget_tracks(root, []) == ()
        assert playlists.forget_tracks(root, [root / AUTOBAHN]) == ()

    def test_an_unwritable_playlist_does_not_raise(self, root: Path):
        """It is called from the middle of a delete that has already been authorised, so a playlist
        that cannot be rewritten must not fail an unlink. Same rule as the rescan hook.

        Driven by a genuinely read-only directory rather than a monkeypatched ``_write``: the first
        version of this test raised an exception the real writer did not, which is a test of the
        ``except`` clause rather than of the behaviour.
        """
        playlists.write_playlist(root, "Road trip", [AUTOBAHN])
        (root / "Playlists").chmod(0o500)
        try:
            assert playlists.forget_tracks(root, [root / AUTOBAHN]) == ()
        finally:
            (root / "Playlists").chmod(0o700)
        assert playlists.read_playlist(root, "Road trip").count == 1


class TestWhatTheAuditFound:
    """One test per defect an independent review found before this shipped.

    Kept together rather than filed under the function each belongs to, because what they have in
    common is the lesson: three of the five are the same mistake in different clothes — a rewrite that
    knew less about the file than the file did — and a reader who has just introduced a sixth is
    better served by that than by five scattered assertions.
    """

    def test_a_symlinked_playlists_directory_cannot_be_written_through(self, root: Path, tmp_path):
        """**The one that was a PLAN.md 3.2 violation.** ``forget_tracks`` joined the directory name
        and iterated it with no containment check, so with ``Playlists/`` replaced by a symlink
        pointing out of the tree, a ``DELETE /library`` read *and rewrote* a file elsewhere on disk.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        theirs = outside / "theirs.m3u"
        theirs.write_text(
            f"#EXTM3U\n#EXTINF:100,A - one\n{root / AUTOBAHN}\nIMPORTANT-OTHER-DATA\n",
            encoding="utf-8",
        )
        before = theirs.read_bytes()
        (root / "Playlists").symlink_to(outside, target_is_directory=True)

        assert playlists.forget_tracks(root, [root / AUTOBAHN]) == ()
        assert theirs.read_bytes() == before, "a file outside the music root was rewritten"
        assert playlists.list_playlists(root) == (), "and its contents were listed"

    def test_a_symlinked_playlist_file_is_skipped(self, root: Path, tmp_path):
        """The same check one level down: a single ``.m3u`` symlinked out of the tree would otherwise
        have its contents read into a ``missing`` list and be rewritten by a prune."""
        outside = tmp_path / "outside"
        outside.mkdir()
        theirs = outside / "theirs.m3u"
        theirs.write_text(f"#EXTM3U\n{root / AUTOBAHN}\nSECRET\n", encoding="utf-8")
        before = theirs.read_bytes()
        (root / "Playlists").mkdir()
        (root / "Playlists" / "theirs.m3u").symlink_to(theirs)

        assert playlists.list_playlists(root) == ()
        assert playlists.forget_tracks(root, [root / AUTOBAHN]) == ()
        assert theirs.read_bytes() == before

    def test_a_long_playlist_is_not_truncated_by_an_unrelated_prune(self, root: Path):
        """**Silent data loss, measured as 1005 → 999.** The entry cap was applied when *reading*, so a
        hand-written playlist longer than it was read short — and the next ``DELETE /library`` that
        touched it rewrote the file from that truncated reading. The cap belongs on what tunebox
        writes; a file somebody else wrote is not ours to shorten.
        """
        extra = root / "A" / "Singles"
        extra.mkdir(parents=True)
        entries = []
        for index in range(playlists.MAX_ENTRIES + 5):
            (extra / f"t{index:04d}.m4a").write_bytes(b"audio")
            entries.append(f"../A/Singles/t{index:04d}.m4a")
        directory = root / "Playlists"
        directory.mkdir()
        (directory / "long.m3u").write_text(
            "\n".join(["#EXTM3U", *entries]) + "\n", encoding="utf-8"
        )

        playlists.forget_tracks(root, [extra / "t0000.m4a"])
        left = [
            line
            for line in (directory / "long.m3u").read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ]
        assert len(left) == playlists.MAX_ENTRIES + 4
        assert playlists.read_playlist(root, "long").count == playlists.MAX_ENTRIES + 4

    def test_a_doomed_entry_past_the_cap_is_still_pruned(self, root: Path):
        """The other half of the same bug: an entry beyond the read cap was never even looked at."""
        extra = root / "A" / "Singles"
        extra.mkdir(parents=True)
        entries = []
        for index in range(playlists.MAX_ENTRIES + 5):
            (extra / f"t{index:04d}.m4a").write_bytes(b"audio")
            entries.append(f"../A/Singles/t{index:04d}.m4a")
        directory = root / "Playlists"
        directory.mkdir()
        (directory / "long.m3u").write_text(
            "\n".join(["#EXTM3U", *entries]) + "\n", encoding="utf-8"
        )
        last = f"../A/Singles/t{playlists.MAX_ENTRIES + 4:04d}.m4a"

        playlists.forget_tracks(root, [extra / f"t{playlists.MAX_ENTRIES + 4:04d}.m4a"])
        assert last not in (directory / "long.m3u").read_text(encoding="utf-8")

    def test_an_append_preserves_a_foreign_playlist_line_for_line(self, root: Path):
        """``#EXTINF`` is another program's copy of the metadata. tunebox does not write them and has
        no business removing them — but a rewrite built only from the *entries* threw them away."""
        directory = root / "Playlists"
        directory.mkdir()
        (directory / "Theirs.m3u").write_text(
            "#EXTM3U\n#PLAYLIST:Theirs\n#EXTINF:213,Kraftwerk - Autobahn\n"
            f"../{AUTOBAHN}\n# a note somebody left\n",
            encoding="utf-8",
        )

        playlists.append_tracks(root, "Theirs", [KSONG])
        assert (directory / "Theirs.m3u").read_text(encoding="utf-8").splitlines() == [
            "#EXTM3U",
            "#PLAYLIST:Theirs",
            "#EXTINF:213,Kraftwerk - Autobahn",
            f"../{AUTOBAHN}",
            "# a note somebody left",
            f"../{KSONG}",
        ]

    def test_a_prune_takes_the_extinf_with_the_entry_and_nothing_else(self, root: Path):
        """The one deliberate exception to "line for line": that ``#EXTINF`` describes the track that
        just went, and leaving it behind would attach it to the next one. A comment that is anything
        else is somebody's note and stays."""
        directory = root / "Playlists"
        directory.mkdir()
        (directory / "Theirs.m3u").write_text(
            "#EXTM3U\n# keep me\n#EXTINF:213,Kraftwerk - Autobahn\n"
            f"../{AUTOBAHN}\n#EXTINF:280,Eason - K歌之王\n../{KSONG}\n",
            encoding="utf-8",
        )

        assert playlists.forget_tracks(root, [root / AUTOBAHN]) == ("Playlists/Theirs.m3u",)
        assert (directory / "Theirs.m3u").read_text(encoding="utf-8").splitlines() == [
            "#EXTM3U",
            "# keep me",
            "#EXTINF:280,Eason - K歌之王",
            f"../{KSONG}",
        ]

    @pytest.mark.parametrize("spelling", ["../KRAFTWERK/Autobahn/01 - Autobahn.m4a", "../Kraftwerk/./Autobahn/01 - Autobahn.m4a"])
    def test_a_differently_spelled_entry_is_still_pruned(self, root: Path, spelling: str):
        """Matching by path *string* missed on this project's own filesystem: macOS is
        case-insensitive, so the entry resolved to the file being deleted while comparing unequal to
        it, and survived into a playlist that could never play it. Identity is ``(device, inode)`` now,
        which also covers the NFD spelling of a CJK filename and a hard link.
        """
        if spelling.startswith("../KRAFTWERK") and not (root / "KRAFTWERK").exists():
            # Only meaningful on a case-insensitive filesystem; on ext4 the entry is simply missing.
            pytest.skip("this filesystem is case-sensitive")
        directory = root / "Playlists"
        directory.mkdir()
        (directory / "Spelled.m3u").write_text(f"#EXTM3U\n{spelling}\n", encoding="utf-8")

        assert playlists.forget_tracks(root, [root / AUTOBAHN]) == ("Playlists/Spelled.m3u",)
        assert "Autobahn" not in (directory / "Spelled.m3u").read_text(encoding="utf-8")

    def test_a_hard_link_to_the_deleted_file_is_recognised(self, root: Path):
        link = root / "Kraftwerk" / "Autobahn" / "hardlink.m4a"
        os.link(root / AUTOBAHN, link)
        directory = root / "Playlists"
        directory.mkdir()
        (directory / "Linked.m3u").write_text(
            "#EXTM3U\n../Kraftwerk/Autobahn/hardlink.m4a\n", encoding="utf-8"
        )
        assert playlists.forget_tracks(root, [root / AUTOBAHN]) == ("Playlists/Linked.m3u",)

    def test_looking_a_playlist_up_never_sanitises_the_name(self, root: Path):
        """**The one that could delete the wrong file.** ``a:b`` and ``a_b`` both sanitise to ``a_b``,
        so a lookup that sanitised meant ``DELETE /playlists/a:b`` removed ``a_b.m3u``. Creating still
        sanitises — that is how a typed name becomes a filename — but everything that names an existing
        playlist takes it verbatim and refuses what cannot be one.
        """
        playlists.write_playlist(root, "a_b", [AUTOBAHN])
        with pytest.raises(PlaylistNotFound):
            playlists.read_playlist(root, "a:b")
        with pytest.raises(PlaylistNotFound):
            playlists.delete_playlist(root, "a:b")
        assert (root / "Playlists" / "a_b.m3u").is_file(), "the wrong playlist was deleted"

    @pytest.mark.parametrize(
        "name", ["a/b", "a\\b", "../evil", "", "   ", ".", "..", "trailing.", "a\x00b", "a\nb"]
    )
    def test_an_unaddressable_name_is_refused_rather_than_rewritten(self, name: str):
        with pytest.raises(PlaylistError):
            playlists.exact_name(name)

    @pytest.mark.parametrize("name", ["  Road trip  ", "Road trip.m3u", "廣東歌", "a:b", "a_b"])
    def test_an_addressable_name_comes_back_unchanged(self, name: str):
        """Surrounding whitespace and the extension are trimmed — a name pasted with a trailing space,
        or copied off a directory listing, should still find its playlist — and nothing else is touched.
        ``a:b`` is addressable even though ``write_playlist`` would never produce it, which is the point:
        a file somebody named by hand is addressed as itself or not at all.
        """
        assert playlists.exact_name(name) == name.strip().removesuffix(".m3u")

    def test_a_name_longer_than_a_filename_is_refused(self):
        with pytest.raises(PlaylistError, match="too long"):
            playlists.exact_name("é" * 100)  # 200 bytes, over naming.MAX_COMPONENT

    def test_the_listing_reports_the_real_filename(self, root: Path):
        """Deriving the name by sanitising the stem hid any file whose name would not survive it — a
        hand-made ``Road:trip.m3u`` appeared in no listing at all — and showed two different files as
        one row, with Delete then hitting whichever the sanitiser happened to name."""
        directory = root / "Playlists"
        directory.mkdir()
        for stem in ("Road:trip", "a_b", "a:b"):
            (directory / f"{stem}.m3u").write_text(f"#EXTM3U\n../{AUTOBAHN}\n", encoding="utf-8")
        assert [p.name for p in playlists.list_playlists(root)] == ["a:b", "a_b", "Road:trip"]

    def test_a_read_only_directory_is_not_reported_as_a_bad_request(self, root: Path):
        """An ``OSError`` from the writer is a read-only volume or a full disk, not a malformed
        request — so it is left to surface as the unexpected failure it is rather than dressed up as a
        ``400`` that sends somebody looking for a typo."""
        playlists.write_playlist(root, "Road trip", [AUTOBAHN])
        before = (root / "Playlists" / "Road trip.m3u").read_bytes()
        (root / "Playlists").chmod(0o500)
        try:
            with pytest.raises(OSError):
                playlists.append_tracks(root, "Road trip", [KSONG])
            # And the real read-only case, through the function that must never raise:
            assert playlists.forget_tracks(root, [root / AUTOBAHN]) == ()
        finally:
            (root / "Playlists").chmod(0o700)
        assert (root / "Playlists" / "Road trip.m3u").read_bytes() == before
        assert not list((root / "Playlists").glob(".*.partial"))


class TestRemovingEntries:
    """Taking a track out of a playlist — the half the first version of this feature was missing.

    Without it, fixing one wrong song in a list of forty meant rebuilding the whole list, which is why
    the remote's complaint was "can't modify the playlist" rather than anything about the file format.
    """

    def test_by_position(self, root: Path):
        playlists.write_playlist(root, "Mix", [AUTOBAHN, KSONG])
        playlist = playlists.remove_entries(root, "Mix", positions=[0])
        assert [track.path for track in playlist.tracks] == [KSONG]

    def test_by_position_takes_exactly_that_copy(self, root: Path):
        """The reason positions exist at all: a playlist may hold the same track twice, and "remove the
        second one" is not something a path can say."""
        playlists.write_playlist(root, "Twice", [AUTOBAHN, KSONG, AUTOBAHN])
        playlist = playlists.remove_entries(root, "Twice", positions=[2])
        assert [track.path for track in playlist.tracks] == [AUTOBAHN, KSONG]

    def test_several_positions_at_once(self, root: Path):
        playlists.write_playlist(root, "Mix", [AUTOBAHN, KSONG, AUTOBAHN])
        playlist = playlists.remove_entries(root, "Mix", positions=[0, 2])
        assert [track.path for track in playlist.tracks] == [KSONG]

    def test_a_dangling_entry_can_be_removed(self, root: Path):
        """A path cannot name it — it resolves to nothing — so before positions there was no way to
        clear one from a phone at all."""
        TestReadingItBack.write_raw(root, "Holes", ["../gone.m4a", f"../{AUTOBAHN}"])
        playlist = playlists.remove_entries(root, "Holes", positions=[0])
        assert playlist.missing == () and playlist.count == 1

    def test_by_path_removes_every_copy(self, root: Path):
        """What a terminal wants to type, and what ``DELETE /library`` needs: "this track is gone, take
        it out of everything"."""
        playlists.write_playlist(root, "Twice", [AUTOBAHN, KSONG, AUTOBAHN])
        playlist = playlists.remove_entries(root, "Twice", paths=[AUTOBAHN])
        assert [track.path for track in playlist.tracks] == [KSONG]

    def test_a_position_that_is_not_there_is_an_error(self, root: Path):
        """Not a silent no-op: the remote read the list two seconds before the tap, so a stale index
        means somebody else edited the playlist — and removing whatever sits there now is the one
        outcome nobody asked for."""
        playlists.write_playlist(root, "Mix", [AUTOBAHN])
        with pytest.raises(PlaylistError, match="nothing at position 3"):
            playlists.remove_entries(root, "Mix", positions=[3])
        assert playlists.read_playlist(root, "Mix").count == 1

    def test_a_path_the_playlist_does_not_hold_is_an_error(self, root: Path):
        playlists.write_playlist(root, "Mix", [AUTOBAHN])
        with pytest.raises(PlaylistError, match="does not hold"):
            playlists.remove_entries(root, "Mix", paths=[KSONG])

    def test_a_path_outside_the_library_is_refused(self, root: Path):
        playlists.write_playlist(root, "Mix", [AUTOBAHN])
        with pytest.raises(LibraryError):
            playlists.remove_entries(root, "Mix", paths=["/etc/passwd"])

    @pytest.mark.parametrize(
        "kwargs", [{}, {"positions": [0], "paths": [AUTOBAHN]}, {"positions": []}]
    )
    def test_exactly_one_way_of_saying_which(self, root: Path, kwargs: dict):
        playlists.write_playlist(root, "Mix", [AUTOBAHN])
        with pytest.raises(PlaylistError):
            playlists.remove_entries(root, "Mix", **kwargs)

    def test_removing_everything_leaves_an_empty_playlist(self, root: Path):
        playlists.write_playlist(root, "Mix", [AUTOBAHN])
        playlist = playlists.remove_entries(root, "Mix", positions=[0])
        assert playlist.count == 0
        assert (root / "Playlists" / "Mix.m3u").is_file(), "the file stays; it is still a playlist"

    def test_no_audio_is_touched(self, root: Path):
        playlists.write_playlist(root, "Mix", [AUTOBAHN, KSONG])
        playlists.remove_entries(root, "Mix", positions=[0, 1])
        assert (root / AUTOBAHN).is_file() and (root / KSONG).is_file()

    def test_the_header_and_a_foreign_line_survive(self, root: Path):
        TestReadingItBack.write_raw(
            root, "Theirs", ["#PLAYLIST:Theirs", f"../{AUTOBAHN}", f"../{KSONG}"]
        )
        playlists.remove_entries(root, "Theirs", positions=[1])
        assert (root / "Playlists" / "Theirs.m3u").read_text(encoding="utf-8").splitlines() == [
            "#EXTM3U",
            "#PLAYLIST:Theirs",
            f"../{AUTOBAHN}",
        ]


class TestReordering:
    """``move_entry`` — one entry at a time, which is the whole reordering surface.

    A move rather than a whole new order, for the reason ``PUT /player/repeat`` takes a mode rather
    than a cycle: two phones can be looking at one list, and an order computed from a two-second-old
    reading would silently undo whatever the other one just did.
    """

    def test_up(self, root: Path):
        playlists.write_playlist(root, "Mix", [AUTOBAHN, KSONG])
        playlist = playlists.move_entry(root, "Mix", 1, 0)
        assert [track.path for track in playlist.tracks] == [KSONG, AUTOBAHN]

    def test_down(self, root: Path):
        playlists.write_playlist(root, "Mix", [AUTOBAHN, KSONG])
        playlist = playlists.move_entry(root, "Mix", 0, 1)
        assert [track.path for track in playlist.tracks] == [KSONG, AUTOBAHN]

    def test_to_the_front_from_the_back(self, root: Path):
        third = "Kraftwerk/Autobahn/02 - Kometenmelodie.m4a"
        playlists.write_playlist(root, "Mix", [AUTOBAHN, KSONG, third])
        playlist = playlists.move_entry(root, "Mix", 2, 0)
        assert [track.path for track in playlist.tracks] == [third, AUTOBAHN, KSONG]

    def test_to_the_same_place_is_not_an_error(self, root: Path):
        """A ↑ on the first row is a tap somebody made. The honest answer is the playlist unchanged
        rather than a message about a move that would have changed nothing."""
        playlists.write_playlist(root, "Mix", [AUTOBAHN, KSONG])
        playlist = playlists.move_entry(root, "Mix", 0, 0)
        assert [track.path for track in playlist.tracks] == [AUTOBAHN, KSONG]

    @pytest.mark.parametrize("position,to", [(5, 0), (0, 5), (-1, 0), (0, -1)])
    def test_outside_the_playlist_is_an_error(self, root: Path, position: int, to: int):
        playlists.write_playlist(root, "Mix", [AUTOBAHN, KSONG])
        with pytest.raises(PlaylistError, match="outside playlist"):
            playlists.move_entry(root, "Mix", position, to)
        assert [t.path for t in playlists.read_playlist(root, "Mix").tracks] == [AUTOBAHN, KSONG]

    def test_an_entry_takes_its_extinf_with_it(self, root: Path):
        """The reason a move operates on blocks rather than lines: an ``#EXTINF`` left behind would
        describe whichever track moved into its place."""
        TestReadingItBack.write_raw(
            root,
            "Theirs",
            ["#EXTINF:213,Kraftwerk - Autobahn", f"../{AUTOBAHN}", "#EXTINF:280,Eason", f"../{KSONG}"],
        )
        playlists.move_entry(root, "Theirs", 1, 0)
        assert (root / "Playlists" / "Theirs.m3u").read_text(encoding="utf-8").splitlines() == [
            "#EXTM3U",
            "#EXTINF:280,Eason",
            f"../{KSONG}",
            "#EXTINF:213,Kraftwerk - Autobahn",
            f"../{AUTOBAHN}",
        ]

    def test_the_header_stays_at_the_top(self, root: Path):
        TestReadingItBack.write_raw(
            root, "Theirs", ["# a note at the top", f"../{AUTOBAHN}", f"../{KSONG}"]
        )
        playlists.move_entry(root, "Theirs", 1, 0)
        body = (root / "Playlists" / "Theirs.m3u").read_text(encoding="utf-8").splitlines()
        assert body[:2] == ["#EXTM3U", "# a note at the top"]

    def test_a_missing_entry_can_be_moved_like_any_other(self, root: Path):
        TestReadingItBack.write_raw(root, "Holes", [f"../{AUTOBAHN}", "../gone.m4a"])
        playlist = playlists.move_entry(root, "Holes", 1, 0)
        assert [entry.missing for entry in playlist.entries] == [True, False]


class TestRenaming:
    def test_it_keeps_the_tracks_and_moves_the_file(self, root: Path):
        playlists.write_playlist(root, "Mix", [AUTOBAHN, KSONG])
        playlist = playlists.rename_playlist(root, "Mix", "Road trip")
        assert playlist.name == "Road trip"
        assert playlist.path == "Playlists/Road trip.m3u"
        assert [track.path for track in playlist.tracks] == [AUTOBAHN, KSONG]
        assert not (root / "Playlists" / "Mix.m3u").exists()

    def test_the_new_name_is_sanitised_and_reported(self, root: Path):
        """It is naming a file, which is the one situation that sanitises — so the answer's name is the
        one the next request has to use, exactly as with ``write_playlist``."""
        playlists.write_playlist(root, "Mix", [AUTOBAHN])
        assert playlists.rename_playlist(root, "Mix", "Road trip: 2026/07").name == "Road trip_ 2026_07"

    def test_our_own_comment_line_is_brought_up_to_date(self, root: Path):
        """A file somebody opens after a rename should not disagree with its own filename. Only the
        line tunebox wrote is touched."""
        playlists.write_playlist(root, "Mix", [AUTOBAHN])
        playlists.rename_playlist(root, "Mix", "Road trip")
        body = (root / "Playlists" / "Road trip.m3u").read_text(encoding="utf-8")
        assert "# tunebox playlist: Road trip" in body
        assert "Mix" not in body

    def test_a_foreign_header_is_left_alone(self, root: Path):
        TestReadingItBack.write_raw(root, "Theirs", ["#PLAYLIST:Whatever", f"../{AUTOBAHN}"])
        playlists.rename_playlist(root, "Theirs", "Ours")
        assert "#PLAYLIST:Whatever" in (root / "Playlists" / "Ours.m3u").read_text(encoding="utf-8")

    def test_renaming_onto_an_existing_playlist_is_refused(self, root: Path):
        playlists.write_playlist(root, "One", [AUTOBAHN])
        playlists.write_playlist(root, "Two", [KSONG])
        with pytest.raises(PlaylistExists):
            playlists.rename_playlist(root, "One", "Two")
        assert [t.path for t in playlists.read_playlist(root, "Two").tracks] == [KSONG]
        assert [t.path for t in playlists.read_playlist(root, "One").tracks] == [AUTOBAHN]

    def test_renaming_to_its_own_name_changes_nothing(self, root: Path):
        playlists.write_playlist(root, "Mix", [AUTOBAHN])
        assert playlists.rename_playlist(root, "Mix", "Mix").name == "Mix"

    def test_renaming_one_that_is_not_there(self, root: Path):
        with pytest.raises(PlaylistNotFound):
            playlists.rename_playlist(root, "nope", "Something")

    def test_a_new_name_that_sanitises_to_nothing_is_refused(self, root: Path):
        playlists.write_playlist(root, "Mix", [AUTOBAHN])
        with pytest.raises(PlaylistError):
            playlists.rename_playlist(root, "Mix", "..")
        assert (root / "Playlists" / "Mix.m3u").is_file()

    def test_the_entries_do_not_need_rewriting(self, root: Path):
        """They are relative to the playlist file's *directory*, and the file has not left it — which
        is the same property that lets the whole library move."""
        playlists.write_playlist(root, "Mix", [AUTOBAHN])
        before = [
            line
            for line in (root / "Playlists" / "Mix.m3u").read_text(encoding="utf-8").splitlines()
            if not line.startswith("#")
        ]
        playlists.rename_playlist(root, "Mix", "Road trip")
        after = [
            line
            for line in (root / "Playlists" / "Road trip.m3u").read_text(encoding="utf-8").splitlines()
            if not line.startswith("#")
        ]
        assert after == before == [f"../{AUTOBAHN}"]


class TestTheDirectoryIsConfigurable:
    def test_the_env_var_moves_it(self):
        config = Config.from_env({"TUNEBOX_PLAYLISTS_DIR": "Lists"})
        assert config.playlists_dirname == "Lists"

    def test_the_config_holds_the_name_and_not_the_path(self):
        """``Config`` deliberately has no ``playlists_dir``: :mod:`tunebox.playlists` sanitises the
        name and checks containment before it joins anything, and a convenience accessor that skipped
        those steps would be a second answer to "where are the playlists" that could disagree."""
        assert not hasattr(Config(), "playlists_dir")

    def test_the_default_is_a_visible_directory(self):
        assert Config.from_env({}).playlists_dirname == "Playlists"

    @pytest.mark.parametrize("name", [".lists", ".", "..", "", "   "])
    def test_a_name_a_scanner_would_skip_is_refused_at_startup(self, name: str):
        with pytest.raises(ConfigError):
            Config(playlists_dirname=name)

    @pytest.mark.parametrize("name", ["a/b", "a\\b", "../lists"])
    def test_a_name_that_is_a_path_is_refused_at_startup(self, name: str):
        with pytest.raises(ConfigError):
            Config(playlists_dirname=name)

    def test_a_moved_directory_is_used_end_to_end(self, root: Path):
        playlists.write_playlist(root, "Elsewhere", [AUTOBAHN], directory="Lists")
        assert (root / "Lists" / "Elsewhere.m3u").is_file()
        assert playlists.read_playlist(root, "Elsewhere", directory="Lists").count == 1
        assert playlists.list_playlists(root) == ()  # not in the default directory


class TestTheModel:
    def test_as_dict_is_json_shaped(self, root: Path):
        playlist = playlists.write_playlist(root, "Road trip", [AUTOBAHN])
        body = playlist.as_dict()
        assert set(body) == {"name", "path", "count", "missing_count", "modified", "entries"}
        assert set(body["entries"][0]) == {"position", "entry", "missing", "track"}

    def test_an_empty_summary_and_an_empty_playlist_are_distinguishable(self):
        """Both counts are fields rather than lengths of ``entries``, because a *listing* drops the
        entries — so without them a summary of a forty-track playlist and an empty one would be the
        same document."""
        summary = Playlist(name="a", path="p", count=3, missing_count=1)
        assert (summary.count, summary.missing_count, summary.entries) == (3, 1, ())

    def test_tracks_and_missing_are_views_onto_the_entries(self, root: Path):
        """One ordered truth, two conveniences: playback wants the resolvable half, a UI wants to say
        how many are not. Deriving them means they cannot drift from the file."""
        TestReadingItBack.write_raw(root, "Holes", [f"../{AUTOBAHN}", "../gone.m4a", f"../{KSONG}"])
        playlist = playlists.read_playlist(root, "Holes")
        assert [entry.position for entry in playlist.entries] == [0, 1, 2]
        assert [entry.missing for entry in playlist.entries] == [False, True, False]
        assert [track.path for track in playlist.tracks] == [AUTOBAHN, KSONG]
        assert playlist.missing == ("../gone.m4a",)
        assert (playlist.count, playlist.missing_count) == (2, 1)


# ------------------------------------------------------------------------ the API


@pytest.fixture
def config(root: Path, tmp_path: Path) -> Config:
    return Config(
        music_root=root, db_path=tmp_path / "jobs.db", api_token=TOKEN, ytdlp=("false",)
    )


@pytest.fixture
def store(config: Config) -> JobStore:
    store = JobStore(config.database_path)
    yield store
    store.close()


@pytest.fixture
def player() -> FakePlayer:
    return FakePlayer()


@pytest.fixture
def client(config: Config, store: JobStore, player: FakePlayer) -> TestClient:
    app = create_app(
        config,
        store=store,
        source=FakeSource(),
        tag_writer=RecordingTagWriter(),
        run_worker=False,
        player=player,
        keep_output=False,
    )
    return TestClient(app)


def create(client: TestClient, name: str, paths: list[str], **extra):
    return client.post("/playlists", headers=AUTH, json={"name": name, "paths": paths, **extra})


class TestTheRoutesNeedTheToken:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/playlists"),
            ("POST", "/playlists"),
            ("GET", "/playlists/x"),
            ("DELETE", "/playlists/x"),
            ("POST", "/playlists/x/tracks"),
            ("PUT", "/playlists/x/play"),
        ],
    )
    def test_without_it_everything_is_401(self, client: TestClient, method: str, path: str):
        assert client.request(method, path, json={}).status_code == 401


class TestCreating:
    def test_it_answers_201_with_the_playlist(self, client: TestClient):
        response = create(client, "Road trip", [AUTOBAHN, KSONG])
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "Road trip"
        assert body["path"] == "Playlists/Road trip.m3u"
        assert body["count"] == 2
        assert [entry["track"]["path"] for entry in body["entries"]] == [AUTOBAHN, KSONG]
        assert [entry["position"] for entry in body["entries"]] == [0, 1]

    def test_the_name_it_used_is_in_the_answer(self, client: TestClient):
        """It may not be the name that was sent — it is a filename, so it is sanitised — and the
        client needs the real one to build the next URL."""
        assert create(client, "Road trip: 2026", [AUTOBAHN]).json()["name"] == "Road trip_ 2026"

    def test_a_second_one_of_the_same_name_is_409(self, client: TestClient):
        create(client, "Road trip", [AUTOBAHN])
        assert create(client, "Road trip", [KSONG]).status_code == 409

    def test_replace_is_how_you_mean_it(self, client: TestClient):
        create(client, "Road trip", [AUTOBAHN])
        response = create(client, "Road trip", [KSONG], replace=True)
        assert response.status_code == 201
        assert [e["track"]["path"] for e in response.json()["entries"]] == [KSONG]

    @pytest.mark.parametrize("entry", ["../../../etc/passwd", "/etc/passwd", "nope.m4a"])
    def test_a_path_outside_the_library_is_400(self, client: TestClient, entry: str):
        assert create(client, "Evil", [entry]).status_code == 400

    def test_a_name_that_sanitises_to_nothing_is_400(self, client: TestClient):
        assert create(client, "..", [AUTOBAHN]).status_code == 400

    @pytest.mark.parametrize(
        "body",
        [
            {"name": "x", "paths": [AUTOBAHN], "from_queue": True},  # both
            {"name": "x", "paths": []},
            {"name": "", "paths": [AUTOBAHN]},
            {"name": "x", "paths": [AUTOBAHN], "unexpected": 1},
        ],
    )
    def test_a_malformed_body_is_422(self, client: TestClient, body: dict):
        assert client.post("/playlists", headers=AUTH, json=body).status_code == 422


class TestCreatingFromTheQueue:
    """The one-tap "save what is playing", and the reason this feature needs no OwnTone config change.

    ``POST /api/queue/save`` would do the same thing and is exactly the route the daemon refuses
    without ``allow_modifying_stored_playlists`` (TODO.md open question 5). tunebox reads the queue
    through the ``Player`` port and writes the file itself instead.
    """

    def test_the_queue_becomes_the_playlist_in_order(
        self, client: TestClient, config: Config, player: FakePlayer
    ):
        player.enqueue_paths([config.music_root / KSONG, config.music_root / AUTOBAHN])
        response = client.post(
            "/playlists", headers=AUTH, json={"name": "Right now", "from_queue": True}
        )
        assert response.status_code == 201
        assert [e["track"]["path"] for e in response.json()["entries"]] == [KSONG, AUTOBAHN]

    def test_an_item_that_is_not_a_library_track_is_skipped_and_named(
        self, client: TestClient, config: Config, player: FakePlayer
    ):
        """Rather than failing the save: the queue is shared state OwnTone's own web UI can add to, so
        "save the queue" must not be impossible because something in it came from elsewhere."""
        player.enqueue_paths([config.music_root / AUTOBAHN, Path("/elsewhere/stream.mp3")])
        body = client.post(
            "/playlists", headers=AUTH, json={"name": "Mostly", "from_queue": True}
        ).json()
        assert body["count"] == 1
        assert body["skipped"] == ["stream"]

    def test_an_empty_queue_is_409_rather_than_an_empty_file(self, client: TestClient):
        response = client.post(
            "/playlists", headers=AUTH, json={"name": "Nothing", "from_queue": True}
        )
        assert response.status_code == 409
        assert "queue" in response.json()["error"]

    def test_a_dead_daemon_is_503(self, client: TestClient, player: FakePlayer):
        player.unavailable = True
        response = client.post(
            "/playlists", headers=AUTH, json={"name": "Nothing", "from_queue": True}
        )
        assert response.status_code == 503


class TestReadingAndListing:
    def test_the_listing_has_counts_and_no_tracks(self, client: TestClient):
        create(client, "Road trip", [AUTOBAHN, KSONG])
        (found,) = client.get("/playlists", headers=AUTH).json()
        assert found["count"] == 2 and found["entries"] == []

    def test_an_empty_library_has_an_empty_listing(self, client: TestClient):
        assert client.get("/playlists", headers=AUTH).json() == []

    def test_one_playlist_carries_its_tracks(self, client: TestClient):
        create(client, "Road trip", [AUTOBAHN])
        body = client.get("/playlists/Road trip", headers=AUTH).json()
        assert [entry["track"]["title"] for entry in body["entries"]] == ["Autobahn"]

    def test_a_cjk_name_round_trips_through_the_url(self, client: TestClient):
        create(client, "廣東歌", [KSONG])
        assert client.get("/playlists/廣東歌", headers=AUTH).json()["count"] == 1

    def test_one_that_is_not_there_is_404(self, client: TestClient):
        assert client.get("/playlists/nope", headers=AUTH).status_code == 404

    @pytest.mark.parametrize("encoded", ["%2F..%2F..%2Fetc%2Fpasswd", "..%2F..%2Fsecret"])
    def test_an_encoded_separator_never_reaches_the_handler(self, client: TestClient, encoded: str):
        """Measured, because it is the reason this route may take a name in its path at all where
        ``DELETE /library`` had to take a body: Starlette decodes the path *before* matching, so an
        encoded slash makes it one segment too many and no route matches. The answer is the router's
        ``404``, from code that never looked at a filesystem."""
        assert client.get(f"/playlists/{encoded}", headers=AUTH).status_code == 404

    @pytest.mark.parametrize(
        "encoded,reason",
        [
            ("%2E%2E", "not a usable"),
            ("%2E", "not a usable"),
            ("%20", "not a usable"),
            ("a%00b", "control character"),
            ("trailing.", "dot or a space"),
            ("evil%5Cwin", "not a path"),
            ("x" * 300, "too long"),
        ],
    )
    def test_a_name_that_is_not_a_filename_is_400_and_says_why(
        self, client: TestClient, encoded: str, reason: str
    ):
        """Never a ``500``, which it was for three of these: ``LibraryError`` had no handler and fell
        through to the ``TuneboxError`` catch-all, so a refused name reported itself as a server fault.
        And never a sanitised retry against a different file — see ``exact_name``."""
        response = client.get(f"/playlists/{encoded}", headers=AUTH)
        assert response.status_code == 400, response.text
        assert reason in response.json()["error"]

    def test_the_same_refusal_on_every_verb_that_takes_a_name(self, client: TestClient):
        for method, path, body in (
            ("GET", "/playlists/%2E%2E", None),
            ("DELETE", "/playlists/%2E%2E", None),
            ("PUT", "/playlists/%2E%2E/play", {}),
            ("POST", "/playlists/%2E%2E/tracks", {"paths": [AUTOBAHN]}),
        ):
            response = client.request(method, path, headers=AUTH, json=body)
            assert response.status_code == 400, (method, path, response.text)


class TestAppendingOverTheAPI:
    def test_it_appends_and_answers_with_the_whole_playlist(self, client: TestClient):
        create(client, "Road trip", [AUTOBAHN])
        response = client.post(
            "/playlists/Road trip/tracks", headers=AUTH, json={"paths": [KSONG]}
        )
        assert response.status_code == 200
        assert [e["track"]["path"] for e in response.json()["entries"]] == [AUTOBAHN, KSONG]

    def test_appending_to_one_that_is_not_there_is_404(self, client: TestClient):
        response = client.post("/playlists/nope/tracks", headers=AUTH, json={"paths": [KSONG]})
        assert response.status_code == 404

    def test_a_path_outside_the_library_is_400(self, client: TestClient):
        create(client, "Road trip", [AUTOBAHN])
        response = client.post(
            "/playlists/Road trip/tracks", headers=AUTH, json={"paths": ["/etc/passwd"]}
        )
        assert response.status_code == 400

    def test_an_empty_list_is_422(self, client: TestClient):
        create(client, "Road trip", [AUTOBAHN])
        response = client.post("/playlists/Road trip/tracks", headers=AUTH, json={"paths": []})
        assert response.status_code == 422


class TestPlayingOne:
    """The verb the whole feature exists for: one request between "a playlist" and "music"."""

    def test_it_replaces_the_queue_and_starts(
        self, client: TestClient, player: FakePlayer
    ):
        player.enqueue_paths([Path("/old/thing.m4a")])
        create(client, "Road trip", [AUTOBAHN, KSONG])
        response = client.put("/playlists/Road trip/play", headers=AUTH, json={})
        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "play"
        assert [item["title"] for item in body["queue"]] == ["01 - Autobahn", "K歌之王"]

    def test_append_adds_without_starting(self, client: TestClient, player: FakePlayer):
        player.enqueue_paths([Path("/old/thing.m4a")])
        create(client, "Road trip", [AUTOBAHN])
        body = client.put(
            "/playlists/Road trip/play", headers=AUTH, json={"mode": "append", "start": False}
        ).json()
        assert len(body["queue"]) == 2
        assert body["state"] == "stop"

    def test_the_answer_is_the_player_not_the_playlist(self, client: TestClient):
        """So the remote can draw the now-playing screen from it rather than polling for what it just
        started — the same document ``PUT /player/play`` answers with."""
        create(client, "Road trip", [AUTOBAHN])
        body = client.put("/playlists/Road trip/play", headers=AUTH, json={}).json()
        assert {"state", "queue", "outputs", "volume"} <= set(body)

    def test_a_playlist_with_nothing_playable_is_409(self, client: TestClient, config: Config):
        TestReadingItBack.write_raw(config.music_root, "Broken", ["../all/gone.m4a"])
        response = client.put("/playlists/Broken/play", headers=AUTH, json={})
        assert response.status_code == 409
        assert "no playable tracks" in response.json()["error"]

    def test_one_that_is_not_there_is_404(self, client: TestClient):
        assert client.put("/playlists/nope/play", headers=AUTH, json={}).status_code == 404

    def test_a_dead_daemon_is_503(self, client: TestClient, player: FakePlayer):
        create(client, "Road trip", [AUTOBAHN])
        player.unavailable = True
        assert client.put("/playlists/Road trip/play", headers=AUTH, json={}).status_code == 503

    def test_it_hands_the_player_paths_and_not_a_daemon_playlist_id(
        self, client: TestClient, config: Config, player: FakePlayer
    ):
        """What reaches the ``Player`` port is absolute paths under the music root.

        Not ``uris=library:playlist:N``, which is OwnTone's own way to expand a playlist into the
        queue and would put the daemon's playlist ids through the port — breaking the drop-in property
        the port exists for (PLAN.md 3.4). Asserted on the fake's queue rather than by reading the
        source, because the paths are the observable consequence.
        """
        create(client, "Road trip", [AUTOBAHN, KSONG])
        client.put("/playlists/Road trip/play", headers=AUTH, json={})
        assert [item.path for item in player.queue()] == [
            str(config.music_root / AUTOBAHN),
            str(config.music_root / KSONG),
        ]


class TestEditingOverTheAPI:
    """The three routes that make a playlist editable from a phone, and their status codes.

    All three answer with the **whole playlist**, which is what lets the remote redraw an open row
    from the response instead of following every tap with a second request.
    """

    def test_removing_by_position(self, client: TestClient):
        create(client, "Mix", [AUTOBAHN, KSONG])
        response = client.request(
            "DELETE", "/playlists/Mix/tracks", headers=AUTH, json={"positions": [0]}
        )
        assert response.status_code == 200
        assert [e["track"]["path"] for e in response.json()["entries"]] == [KSONG]

    def test_removing_the_second_copy_of_a_track(self, client: TestClient):
        create(client, "Mix", [AUTOBAHN, KSONG, AUTOBAHN])
        body = client.request(
            "DELETE", "/playlists/Mix/tracks", headers=AUTH, json={"positions": [2]}
        ).json()
        assert [e["track"]["path"] for e in body["entries"]] == [AUTOBAHN, KSONG]

    def test_removing_by_path_takes_every_copy(self, client: TestClient):
        create(client, "Mix", [AUTOBAHN, KSONG, AUTOBAHN])
        body = client.request(
            "DELETE", "/playlists/Mix/tracks", headers=AUTH, json={"paths": [AUTOBAHN]}
        ).json()
        assert [e["track"]["path"] for e in body["entries"]] == [KSONG]

    def test_a_stale_position_is_409_and_removes_nothing(self, client: TestClient):
        """The remote read the list a couple of seconds before the tap. A stale index means another
        phone has edited the playlist, and deleting whatever is at that index *now* is the one outcome
        nobody asked for — so it is refused and the client refreshes."""
        create(client, "Mix", [AUTOBAHN])
        response = client.request(
            "DELETE", "/playlists/Mix/tracks", headers=AUTH, json={"positions": [4]}
        )
        assert response.status_code == 409
        assert "nothing at position 4" in response.json()["error"]
        assert client.get("/playlists/Mix", headers=AUTH).json()["count"] == 1

    def test_editing_one_that_is_not_there_is_404_and_not_409(self, client: TestClient):
        """The distinction these two routes got wrong at first, and the reason the ``except`` order in
        them is now deliberate: ``PlaylistNotFound`` *is* a ``PlaylistError``, so the ``409`` added for
        a stale position swallowed it and answered "that playlist is in the way" about a playlist that
        does not exist."""
        assert (
            client.request(
                "DELETE", "/playlists/nope/tracks", headers=AUTH, json={"positions": [0]}
            ).status_code
            == 404
        )
        assert client.put("/playlists/nope/tracks/0", headers=AUTH, json={"to": 0}).status_code == 404

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"positions": [0], "paths": [AUTOBAHN]},
            {"positions": []},
            {"paths": []},
            {"positions": [-1]},
            {"positions": [0], "unexpected": 1},
        ],
    )
    def test_a_malformed_removal_is_422(self, client: TestClient, body: dict):
        create(client, "Mix", [AUTOBAHN])
        assert (
            client.request("DELETE", "/playlists/Mix/tracks", headers=AUTH, json=body).status_code
            == 422
        )

    def test_moving_an_entry(self, client: TestClient):
        create(client, "Mix", [AUTOBAHN, KSONG])
        response = client.put("/playlists/Mix/tracks/1", headers=AUTH, json={"to": 0})
        assert response.status_code == 200
        assert [e["track"]["path"] for e in response.json()["entries"]] == [KSONG, AUTOBAHN]

    def test_a_move_outside_the_playlist_is_409(self, client: TestClient):
        create(client, "Mix", [AUTOBAHN, KSONG])
        response = client.put("/playlists/Mix/tracks/0", headers=AUTH, json={"to": 9})
        assert response.status_code == 409
        assert "outside playlist" in response.json()["error"]

    def test_a_negative_destination_is_422(self, client: TestClient):
        create(client, "Mix", [AUTOBAHN])
        assert client.put("/playlists/Mix/tracks/0", headers=AUTH, json={"to": -1}).status_code == 422

    def test_a_non_numeric_position_is_422(self, client: TestClient):
        create(client, "Mix", [AUTOBAHN])
        assert client.put("/playlists/Mix/tracks/x", headers=AUTH, json={"to": 0}).status_code == 422

    def test_renaming(self, client: TestClient):
        create(client, "Mix", [AUTOBAHN])
        response = client.patch("/playlists/Mix", headers=AUTH, json={"name": "Road trip: 2026"})
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "Road trip_ 2026"
        assert [e["track"]["path"] for e in body["entries"]] == [AUTOBAHN]
        assert client.get("/playlists/Mix", headers=AUTH).status_code == 404
        assert client.get("/playlists/Road trip_ 2026", headers=AUTH).status_code == 200

    def test_renaming_onto_an_existing_playlist_is_409(self, client: TestClient):
        create(client, "One", [AUTOBAHN])
        create(client, "Two", [KSONG])
        response = client.patch("/playlists/One", headers=AUTH, json={"name": "Two"})
        assert response.status_code == 409
        assert [e["track"]["path"] for e in client.get("/playlists/Two", headers=AUTH).json()["entries"]] == [KSONG]

    def test_renaming_one_that_is_not_there_is_404(self, client: TestClient):
        assert client.patch("/playlists/nope", headers=AUTH, json={"name": "x"}).status_code == 404

    def test_a_new_name_that_is_not_a_filename_is_400(self, client: TestClient):
        create(client, "Mix", [AUTOBAHN])
        assert client.patch("/playlists/Mix", headers=AUTH, json={"name": ".."}).status_code == 400

    def test_every_edit_answers_with_the_whole_playlist(self, client: TestClient):
        """So the remote can redraw an open row from the response rather than following each tap with a
        second request — which is also what keeps the row's "4 tracks · 1 missing" honest."""
        create(client, "Mix", [AUTOBAHN, KSONG])
        for response in (
            client.post("/playlists/Mix/tracks", headers=AUTH, json={"paths": [AUTOBAHN]}),
            client.put("/playlists/Mix/tracks/0", headers=AUTH, json={"to": 1}),
            client.request("DELETE", "/playlists/Mix/tracks", headers=AUTH, json={"positions": [0]}),
            client.patch("/playlists/Mix", headers=AUTH, json={"name": "Mix"}),
        ):
            assert response.status_code == 200, response.text
            assert {"name", "path", "count", "missing_count", "entries"} <= set(response.json())

    def test_a_dangling_entry_can_be_cleared_from_a_phone(self, client: TestClient, config: Config):
        """It resolves to nothing, so no path names it — before positions there was no way to remove
        one without a terminal, which is the state OwnTone will also not mention."""
        TestReadingItBack.write_raw(config.music_root, "Holes", ["../gone.m4a", f"../{AUTOBAHN}"])
        body = client.get("/playlists/Holes", headers=AUTH).json()
        assert body["missing_count"] == 1
        (bad,) = [entry for entry in body["entries"] if entry["missing"]]
        assert bad["entry"] == "../gone.m4a" and bad["track"] is None
        cleared = client.request(
            "DELETE", "/playlists/Holes/tracks", headers=AUTH, json={"positions": [bad["position"]]}
        ).json()
        assert cleared["missing_count"] == 0 and cleared["count"] == 1

    def test_no_audio_is_touched_by_any_of_it(self, client: TestClient, config: Config):
        create(client, "Mix", [AUTOBAHN, KSONG])
        client.put("/playlists/Mix/tracks/0", headers=AUTH, json={"to": 1})
        client.request("DELETE", "/playlists/Mix/tracks", headers=AUTH, json={"positions": [0, 1]})
        client.patch("/playlists/Mix", headers=AUTH, json={"name": "Empty now"})
        assert (config.music_root / AUTOBAHN).is_file()
        assert (config.music_root / KSONG).is_file()


class TestCreatingWithoutTheQueue:
    """``POST /playlists`` with just a name — the entry point the first version did not have.

    The complaint was "only the queue can create a playlist", and it was accurate: ``paths`` needed a
    client that already knew what it wanted and ``from_queue`` needed the list to be the thing playing.
    A name on its own is now a valid request, and the Library panel's ``⋯`` fills it one track at a time.
    """

    def test_a_name_on_its_own_makes_an_empty_playlist(self, client: TestClient):
        response = client.post("/playlists", headers=AUTH, json={"name": "Starting empty"})
        assert response.status_code == 201
        body = response.json()
        assert body["count"] == 0 and body["entries"] == []
        assert body["name"] == "Starting empty"

    def test_and_then_tracks_can_be_added_to_it(self, client: TestClient):
        client.post("/playlists", headers=AUTH, json={"name": "Mix"})
        response = client.post("/playlists/Mix/tracks", headers=AUTH, json={"paths": [AUTOBAHN]})
        assert response.status_code == 200
        assert [e["track"]["path"] for e in response.json()["entries"]] == [AUTOBAHN]

    def test_it_is_listed_like_any_other(self, client: TestClient):
        client.post("/playlists", headers=AUTH, json={"name": "Mix"})
        (found,) = client.get("/playlists", headers=AUTH).json()
        assert (found["name"], found["count"]) == ("Mix", 0)

    def test_an_empty_one_cannot_be_played(self, client: TestClient):
        """A ``409``, the same answer a playlist whose entries have all gone gets: the request is fine
        and the playlist is real, but there is nothing in it to play."""
        client.post("/playlists", headers=AUTH, json={"name": "Mix"})
        response = client.put("/playlists/Mix/play", headers=AUTH, json={})
        assert response.status_code == 409
        assert "no playable tracks" in response.json()["error"]

    def test_an_empty_queue_is_still_a_409_rather_than_an_empty_playlist(self, client: TestClient):
        """"Save the queue" with nothing in the queue is a mistake, not a request for an empty list —
        which is something you now ask for by name instead."""
        response = client.post(
            "/playlists", headers=AUTH, json={"name": "Nothing", "from_queue": True}
        )
        assert response.status_code == 409


class TestDeletingOverTheAPI:
    def test_it_answers_with_what_it_removed(self, client: TestClient, config: Config):
        create(client, "Road trip", [AUTOBAHN])
        response = client.delete("/playlists/Road trip", headers=AUTH)
        assert response.status_code == 200
        assert response.json()["deleted"] == "Playlists/Road trip.m3u"
        assert (config.music_root / AUTOBAHN).is_file()

    def test_one_that_is_not_there_is_404(self, client: TestClient):
        assert client.delete("/playlists/nope", headers=AUTH).status_code == 404

    def test_the_playing_playlist_is_not_refused(self, client: TestClient):
        """Unlike ``DELETE /library``'s ``409`` for the loaded track: once the queue is loaded it
        holds tracks, not a reference to this file, so deleting the list cannot interrupt anything."""
        create(client, "Road trip", [AUTOBAHN])
        client.put("/playlists/Road trip/play", headers=AUTH, json={})
        assert client.delete("/playlists/Road trip", headers=AUTH).status_code == 200


class TestALibraryDeleteReportsThem:
    def test_the_playlists_it_changed_are_in_the_answer(
        self, client: TestClient, player: FakePlayer
    ):
        create(client, "Road trip", [AUTOBAHN, KSONG])
        player.state = "stop"
        response = client.request("DELETE", "/library", headers=AUTH, json={"paths": [AUTOBAHN]})
        assert response.status_code == 200
        body = response.json()
        assert body["deleted"] == [AUTOBAHN]
        assert body["playlists_updated"] == ["Playlists/Road trip.m3u"]
        assert client.get("/playlists/Road trip", headers=AUTH).json()["count"] == 1

    def test_a_delete_that_touches_no_playlist_says_so(self, client: TestClient):
        create(client, "Road trip", [KSONG])
        body = client.request(
            "DELETE", "/library", headers=AUTH, json={"paths": [AUTOBAHN]}
        ).json()
        assert body["playlists_updated"] == []

    def test_a_refused_delete_leaves_the_playlist_alone(
        self, client: TestClient, config: Config, player: FakePlayer
    ):
        """The ``409`` for the loaded track happens before anything is rewritten, so a delete that
        did not happen cannot have shortened a playlist."""
        create(client, "Road trip", [AUTOBAHN, KSONG])
        client.put("/playlists/Road trip/play", headers=AUTH, json={})
        response = client.request("DELETE", "/library", headers=AUTH, json={"paths": [AUTOBAHN]})
        assert response.status_code == 409
        assert client.get("/playlists/Road trip", headers=AUTH).json()["count"] == 2
