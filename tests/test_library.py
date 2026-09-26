"""Library placement: atomicity-adjacent behaviour and root confinement."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tunebox import library
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
