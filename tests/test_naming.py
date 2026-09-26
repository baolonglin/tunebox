"""Naming and path safety — the security-relevant half of the core (PLAN.md 3.2)."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from tunebox.core import naming
from tunebox.core.models import TrackTags
from tunebox.errors import LibraryError


def tags(**kwargs) -> TrackTags:
    base = {"title": "Autobahn", "artist": "Kraftwerk"}
    return TrackTags(**{**base, **kwargs})


class TestRelativePath:
    def test_artist_album_track_layout(self):
        path = naming.relative_path(tags(album="Autobahn"), "m4a")
        assert path == PurePosixPath("Kraftwerk/Autobahn/Autobahn.m4a")

    def test_album_less_tracks_go_to_singles(self):
        assert naming.relative_path(tags(), "m4a") == PurePosixPath("Kraftwerk/Singles/Autobahn.m4a")

    def test_track_number_is_zero_padded_for_correct_sorting(self):
        path = naming.relative_path(tags(album="Autobahn", track_number=3), "m4a")
        assert path.name == "03 - Autobahn.m4a"

    def test_extension_is_normalised(self):
        assert naming.relative_path(tags(), ".MP3").suffix == ".mp3"

    @pytest.mark.parametrize("extension", ["", "m4a/../..", "m4a.exe", "m4a:stream", "."])
    def test_unsafe_extensions_are_refused(self, extension):
        with pytest.raises(LibraryError):
            naming.relative_path(tags(), extension)

    def test_slashes_in_metadata_cannot_create_directories(self):
        path = naming.relative_path(tags(artist="AC/DC", title="Back/In Black"), "m4a")
        assert len(path.parts) == 3
        assert path.parts[0] == "AC_DC"
        assert path.name == "Back_In Black.m4a"

    def test_traversal_in_metadata_is_neutralised(self, tmp_path: Path):
        path = naming.relative_path(tags(artist="../../etc", title="../../../passwd"), "m4a")
        assert len(path.parts) == 3
        assert not any(part in ("..", ".") for part in path.parts)
        # and the result still resolves inside the root
        assert naming.resolve_within(tmp_path, path).is_relative_to(tmp_path.resolve())

    def test_long_components_are_truncated_by_bytes_not_characters(self):
        """ext4 on the Pi enforces 255 *bytes*; 'é' costs two of them."""
        path = naming.relative_path(tags(title="é" * 400), "m4a")
        assert len(path.stem.encode("utf-8")) <= naming.MAX_COMPONENT
        assert path.stem.startswith("é")  # no mangled half-character at the cut

    def test_long_ascii_components_are_truncated(self):
        path = naming.relative_path(tags(title="x" * 400), "m4a")
        assert len(path.stem) <= naming.MAX_COMPONENT

    def test_empty_metadata_falls_back_to_placeholders(self):
        path = naming.relative_path(TrackTags(title="   ", artist="  "), "m4a")
        assert path == PurePosixPath(f"{naming.UNKNOWN_ARTIST}/Singles/{naming.UNKNOWN_TITLE}.m4a")


class TestSanitizeComponent:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Kraftwerk", "Kraftwerk"),
            ("  spaced   out  ", "spaced out"),
            ("trailing dot.", "trailing dot"),
            ("tab\tand\nnewline", "tab and newline"),
            ('quote"colon:pipe|star*', "quote_colon_pipe_star_"),
            ("Bj\u00f6rk", "Bj\u00f6rk"),
            ("...", "FB"),
            ("", "FB"),
            ("..", "FB"),
        ],
    )
    def test_sanitize(self, raw, expected):
        assert naming.sanitize_component(raw, "FB") == expected

    def test_windows_reserved_names_are_prefixed(self):
        assert naming.sanitize_component("CON", "FB") == "_CON"
        assert naming.sanitize_component("nul.mp3", "FB") == "_nul.mp3"

    def test_none_uses_fallback(self):
        assert naming.sanitize_component(None, "FB") == "FB"


class TestResolveWithin:
    def test_resolves_under_root(self, tmp_path: Path):
        resolved = naming.resolve_within(tmp_path, PurePosixPath("Kraftwerk/Autobahn/a.m4a"))
        assert resolved == (tmp_path / "Kraftwerk" / "Autobahn" / "a.m4a").resolve()

    @pytest.mark.parametrize(
        "relative",
        ["../escape.m4a", "Kraftwerk/../../escape.m4a", "/etc/passwd", "a/../../b/c.m4a"],
    )
    def test_escapes_are_refused(self, tmp_path: Path, relative):
        with pytest.raises(LibraryError):
            naming.resolve_within(tmp_path, PurePosixPath(relative))

    def test_symlinked_component_pointing_outside_is_refused(self, tmp_path: Path):
        root = tmp_path / "music"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "Escape").symlink_to(outside, target_is_directory=True)
        with pytest.raises(LibraryError):
            naming.resolve_within(root, PurePosixPath("Escape/track.m4a"))


class TestUniquePath:
    def test_returns_input_when_free(self, tmp_path: Path):
        target = tmp_path / "a.m4a"
        assert naming.unique_path(target) == target

    def test_suffixes_on_collision(self, tmp_path: Path):
        target = tmp_path / "a.m4a"
        target.write_bytes(b"")
        assert naming.unique_path(target).name == "a (2).m4a"
