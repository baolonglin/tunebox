"""Library placement and listing: atomicity-adjacent behaviour, root confinement, and the walk
behind Phase 4's ``GET /library``."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tunebox import library
from tunebox.config import SCRATCH_DIRNAME, STATE_DIRNAME
from tunebox.core.models import TrackTags
from tunebox.errors import LibraryError

TAGS = TrackTags(title="Autobahn", artist="Kraftwerk", album="Autobahn")


def test_destination_for_builds_an_absolute_path_under_the_root(tmp_path: Path):
    destination = library.destination_for(tmp_path, TAGS, "m4a")
    assert destination == (tmp_path / "Kraftwerk" / "Autobahn" / "Autobahn.m4a").resolve()


def test_destination_for_refuses_to_leave_the_root(tmp_path: Path):
    tags = TrackTags(title="t", artist="a")
    with pytest.raises(LibraryError):
        library.destination_for(tmp_path, tags, "m4a/../../../etc/cron.d/x")


def test_place_creates_directories_and_moves(tmp_path: Path):
    source = tmp_path / "scratch" / "track.m4a"
    source.parent.mkdir()
    source.write_bytes(b"audio")

    destination = library.place(source, tmp_path / "music" / "Kraftwerk" / "Autobahn" / "a.m4a")
    assert destination.read_bytes() == b"audio"
    assert not source.exists()


def test_place_does_not_clobber_a_different_track(tmp_path: Path):
    existing = tmp_path / "a.m4a"
    existing.write_bytes(b"first")
    source = tmp_path / "scratch.m4a"
    source.write_bytes(b"second")

    destination = library.place(source, existing)
    assert destination.name == "a (2).m4a"
    assert existing.read_bytes() == b"first"


def test_place_overwrites_when_asked(tmp_path: Path):
    existing = tmp_path / "a.m4a"
    existing.write_bytes(b"first")
    source = tmp_path / "scratch.m4a"
    source.write_bytes(b"second")

    destination = library.place(source, existing, overwrite=True)
    assert destination == existing
    assert existing.read_bytes() == b"second"


def test_place_falls_back_to_copy_across_filesystems(tmp_path: Path, monkeypatch):
    """Simulates scratch and library on different devices; must still end atomically."""
    source = tmp_path / "scratch.m4a"
    source.write_bytes(b"audio")
    destination = tmp_path / "music" / "a.m4a"
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(18, "Invalid cross-device link")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    placed = library.place(source, destination)

    assert placed.read_bytes() == b"audio"
    assert not source.exists()
    assert list(destination.parent.iterdir()) == [destination]  # no .partial left behind


def test_place_refuses_a_missing_source(tmp_path: Path):
    with pytest.raises(LibraryError):
        library.place(tmp_path / "nope.m4a", tmp_path / "music" / "a.m4a")


def test_a_failed_cross_device_copy_leaves_no_partial_file(tmp_path: Path, monkeypatch):
    source = tmp_path / "scratch.m4a"
    source.write_bytes(b"audio")
    destination = tmp_path / "music" / "a.m4a"

    def always_cross_device(src, dst):
        raise OSError(18, "Invalid cross-device link")

    def failing_copy(src, dst):
        Path(dst).write_bytes(b"half")  # a partially written staging file
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", always_cross_device)
    monkeypatch.setattr(library.shutil, "copy2", failing_copy)

    with pytest.raises(OSError):
        library.place(source, destination)

    assert list(destination.parent.iterdir()) == []
    assert source.exists()  # the audio is still in scratch, so the caller can retry


def test_scratch_dir_is_removed_even_on_failure(tmp_path: Path):
    with pytest.raises(RuntimeError):
        with library.scratch_dir(tmp_path / "tmp") as work:
            (work / "leftover.part").write_bytes(b"x")
            created = work
            raise RuntimeError("boom")
    assert not created.exists()


def test_scratch_dir_lives_under_the_given_parent(tmp_path: Path):
    with library.scratch_dir(tmp_path / "tmp") as work:
        assert work.parent == tmp_path / "tmp"


# --------------------------------------------------------------- listing (Phase 4)
#
# The remote needs something to play, and the filesystem is the library database (PLAN.md 3.1), so
# browsing is a directory walk. The interesting properties are all about what must *not* appear in
# it: scratch files, the queue database, and anything a request body asked for outside the root.


def track(root: Path, relative: str, *, body: bytes = b"audio") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def test_list_tracks_reads_the_tags_out_of_the_path(tmp_path: Path):
    """The inverse of ``naming.relative_path``: no file is opened to fill this in."""
    track(tmp_path, "Kraftwerk/Trans-Europe Express/03 - Metal on Metal.m4a")

    page = library.list_tracks(tmp_path)

    assert page.total == 1
    found = page.tracks[0]
    assert found.path == "Kraftwerk/Trans-Europe Express/03 - Metal on Metal.m4a"
    assert (found.artist, found.album, found.track_number) == (
        "Kraftwerk",
        "Trans-Europe Express",
        3,
    )
    assert found.title == "Metal on Metal"
    assert found.size == len(b"audio")


def test_singles_is_not_reported_as_an_album(tmp_path: Path):
    """``Singles`` is the directory this layout uses for album-less tracks, not a record."""
    track(tmp_path, "Kraftwerk/Singles/Autobahn.m4a")
    assert library.list_tracks(tmp_path).tracks[0].album is None


def test_a_title_that_merely_starts_with_digits_keeps_them(tmp_path: Path):
    track(tmp_path, "Artist/Album/99 Luftballons.m4a")
    found = library.list_tracks(tmp_path).tracks[0]
    assert found.track_number is None
    assert found.title == "99 Luftballons"


def test_newest_first(tmp_path: Path):
    """What the remote is for: the track you want to play is the one you just ingested."""
    old = track(tmp_path, "A/Singles/old.m4a")
    new = track(tmp_path, "A/Singles/new.m4a")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))

    assert [t.title for t in library.list_tracks(tmp_path).tracks] == ["new", "old"]


def test_scratch_and_state_directories_are_invisible(tmp_path: Path):
    """Both are dot-directories precisely so scanners skip them; this one must too, or a
    half-downloaded file would be offered as something to play."""
    track(tmp_path, f"{SCRATCH_DIRNAME}/ingest-abc/track.m4a")
    track(tmp_path, f"{STATE_DIRNAME}/whatever.m4a")
    track(tmp_path, "A/Singles/real.m4a")

    assert [t.title for t in library.list_tracks(tmp_path).tracks] == ["real"]


def test_a_partial_file_is_invisible(tmp_path: Path):
    """``place()`` stages a cross-device copy as ``.name.partial``; it is not a track yet."""
    track(tmp_path, "A/Singles/.real.m4a.partial")
    assert library.list_tracks(tmp_path).total == 0


def test_non_audio_files_are_ignored(tmp_path: Path):
    track(tmp_path, "A/Singles/cover.jpg")
    track(tmp_path, "A/Singles/notes.txt")
    track(tmp_path, "A/Singles/real.mp3")
    assert [t.title for t in library.list_tracks(tmp_path).tracks] == ["real"]


def test_a_missing_music_root_is_an_empty_library_not_an_error(tmp_path: Path):
    """``tunebox serve`` may start before the first ingest has created the directory."""
    assert library.list_tracks(tmp_path / "nothing-here").total == 0


def test_the_query_matches_path_and_is_case_insensitive(tmp_path: Path):
    track(tmp_path, "Kraftwerk/Singles/Autobahn.m4a")
    track(tmp_path, "陳奕迅 Eason Chan/Singles/K歌之王.m4a")

    assert library.list_tracks(tmp_path, query="AUTOBAHN").total == 1
    assert library.list_tracks(tmp_path, query="k歌").total == 1
    assert library.list_tracks(tmp_path, query="nothing").total == 0


def test_the_query_ignores_unicode_composition(tmp_path: Path):
    """macOS hands back NFD where ``sanitize_component`` wrote NFC. Matching the bytes would find
    nothing on one machine and everything on another."""
    track(tmp_path, "Bj\u00f6rk/Singles/Jo\u0301ga.m4a")  # NFC artist, NFD title
    assert library.list_tracks(tmp_path, query="Bj\u006f\u0308rk").total == 1
    assert library.list_tracks(tmp_path, query="J\u00f3ga").total == 1


def test_the_page_is_limited_but_the_total_is_not(tmp_path: Path):
    for index in range(5):
        track(tmp_path, f"A/Singles/{index}.m4a")
    page = library.list_tracks(tmp_path, limit=2)
    assert len(page.tracks) == 2
    assert page.total == 5
    assert page.truncated is False


def test_the_walk_gives_up_rather_than_being_slow(tmp_path: Path):
    """A music root set to ``/`` by a typo must not hold a worker thread for minutes. ``truncated``
    is a different fact from ``total``: the answer is incomplete, not merely paged."""
    for index in range(10):
        track(tmp_path, f"A/Singles/{index}.m4a")
    page = library.list_tracks(tmp_path, max_files=4)
    assert page.truncated is True
    assert page.total <= 4


# ------------------------------------------------------- resolving a path from a client


def test_resolve_track_path_accepts_a_relative_path(tmp_path: Path):
    expected = track(tmp_path, "Kraftwerk/Singles/Autobahn.m4a")
    assert library.resolve_track_path(tmp_path, "Kraftwerk/Singles/Autobahn.m4a") == expected


def test_resolve_track_path_accepts_the_absolute_path_a_job_reports(tmp_path: Path):
    """``GET /jobs`` answers with the absolute path the ingest wrote, and the remote's "play this"
    button sends back what it was given."""
    expected = track(tmp_path, "Kraftwerk/Singles/Autobahn.m4a")
    assert library.resolve_track_path(tmp_path, str(expected)) == expected


@pytest.mark.parametrize(
    "given",
    [
        "../../../etc/passwd",
        "Kraftwerk/../../outside.m4a",
        "/etc/passwd",
        "",
        "   ",
    ],
)
def test_resolve_track_path_refuses_to_leave_the_root(tmp_path: Path, given: str):
    with pytest.raises(LibraryError):
        library.resolve_track_path(tmp_path, given)


def test_resolve_track_path_refuses_a_non_music_file(tmp_path: Path):
    track(tmp_path, "A/Singles/notes.txt")
    with pytest.raises(LibraryError):
        library.resolve_track_path(tmp_path, "A/Singles/notes.txt")


def test_resolve_track_path_refuses_a_file_that_is_not_there(tmp_path: Path):
    with pytest.raises(LibraryError):
        library.resolve_track_path(tmp_path, "A/Singles/ghost.m4a")


def test_resolve_track_path_refuses_a_directory(tmp_path: Path):
    (tmp_path / "A" / "Singles").mkdir(parents=True)
    with pytest.raises(LibraryError):
        library.resolve_track_path(tmp_path, "A/Singles")


def test_a_symlink_out_of_the_library_is_refused(tmp_path: Path):
    """The root is confined by ``resolve_within``, which resolves symlinks rather than clamping
    them — so a link planted in the music directory cannot be used to name a file elsewhere."""
    outside = tmp_path / "outside" / "secret.m4a"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"x")
    root = tmp_path / "music"
    root.mkdir()
    (root / "link.m4a").symlink_to(outside)

    with pytest.raises(LibraryError):
        library.resolve_track_path(root, "link.m4a")
