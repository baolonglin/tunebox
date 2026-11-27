"""What must be true of this repository as something published.

These are not tests of the program. They are tests of the artefact: a public clone must carry no
credential, no identifier belonging to one household, and a licence. Each one exists because the
audit that preceded publishing found the thing it checks for, and a one-off sweep does not stay
swept — the next person to hardcode their own address will do it exactly where the last one did.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def tracked() -> list[Path]:
    """Every file git would publish. Not a directory walk: an ignored file is not published, and
    the whole point of ignoring `.agent-shell/` is that its contents never become public."""
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    return [REPO / name for name in out.split("\0") if name]


def text_files() -> list[Path]:
    """Every publishable text file except this one.

    This file is excluded because it is the one that must contain the patterns, being the one that
    defines them — `"PRIVATE KEY-----"` below is a needle, not a key. Every secret scanner needs
    this exemption for its own rules, and the alternative is splitting each needle across a string
    concatenation so it does not match itself, which hides what is being searched for.

    Worth recording how it surfaced: the suite was green while this file was untracked, because
    `git ls-files` did not list it yet. Staging it turned the scanner into its own first finding.
    """
    files = []
    for path in tracked():
        if path == Path(__file__).resolve():
            continue
        try:
            path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue  # icons, silence.m4a, the recorded fixtures' binary siblings
        files.append(path)
    return files


class TestNoCredentialIsPublished:
    def test_no_private_key_is_committed(self):
        for path in text_files():
            body = path.read_text(encoding="utf-8")
            assert "PRIVATE KEY-----" not in body, path

    def test_the_token_only_ever_appears_as_a_variable(self):
        """`TUNEBOX_TOKEN=` is legitimate in the installer, in a shell example and in an assertion
        that the installer does not print it. What must never appear is a literal on the right of
        that `=`: the token is generated at install time into a mode-600 file, and a committed one
        would be live on somebody's machine."""
        literal = re.compile(r"TUNEBOX_TOKEN=(?!\$|\(|\s|\"\s|$)[A-Za-z0-9_-]{12,}")
        for path in text_files():
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                assert not literal.search(line), f"{path}:{n}: {line.strip()}"

    def test_owntone_admin_password_is_never_a_real_one(self):
        """`owntone-setup.sh` generates one with `secrets.token_urlsafe(18)` and writes it into
        tunebox.env. Documentation shows the key with an ellipsis for a value.

        The threshold is 20 characters because that is what separates the two: a generated password
        is 24, and the deliberately fake values in the suite's own fixtures — `"unsafe"`, testing
        that the conf editor handles a commented-out setting — are short English words. Scanning the
        tests too is the point; a leaked credential would not politely avoid them.
        """
        bad = re.compile(r"""admin_password\s*=\s*["'][^"'…\n]{20,}["']""")
        for path in text_files():
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                assert not bad.search(line), f"{path}:{n}: {line.strip()}"

    def test_session_transcripts_are_not_tracked_and_are_ignored_by_the_repository(self):
        """They can contain anything that was on screen, a token included. Until the publishing
        audit this was excluded only by `.git/info/exclude`, which is local to one clone and is not
        published — so the protection did not travel. It belongs in `.gitignore`."""
        assert not [p for p in tracked() if ".agent-shell" in p.parts]
        assert ".agent-shell/" in (REPO / ".gitignore").read_text(encoding="utf-8")


class TestNoHouseholdIsIdentifiable:
    def test_the_apple_team_id_ships_empty(self):
        """A team id belongs to whoever is signing. It is not a credential — without the private
        key it signs nothing — but it is a stable identifier tied to one Apple ID, and leaving it
        set also *breaks* a stranger's build, which tries to sign with a team it is not in.
        ios/README.md's first step is Xcode writing this value, which is where it should come from."""
        project = REPO / "ios" / "Tunebox.xcodeproj" / "project.pbxproj"
        if not project.exists():
            pytest.skip("the wrapper is not present")
        found = re.findall(r"DEVELOPMENT_TEAM = ([^;]*);", project.read_text(encoding="utf-8"))
        assert found, "the setting should be present and empty, not absent"
        for value in found:
            assert value.strip() in ('""', "''"), f"a team id is committed: {value}"

    def test_the_wrapper_points_at_a_placeholder_address_in_both_places(self):
        """The two edits a clone needs, and the reason they are tested together: an address allowed
        in the plist but not requested in the code is dead config, and the other way round is a
        request App Transport Security refuses — whose symptom is a blank web view, not an error."""
        ios = REPO / "ios" / "Tunebox"
        if not ios.exists():
            pytest.skip("the wrapper is not present")
        addresses = set()
        for name in ("Info.plist", "WebHost.swift"):
            body = (ios / name).read_text(encoding="utf-8")
            found = re.findall(r"\b192\.168\.\d{1,3}\.\d{1,3}\b", body)
            assert found, f"{name} should name the placeholder host"
            addresses.update(found)
        assert addresses == {"192.168.1.100"}, f"the two must agree, and be the placeholder: {addresses}"


class TestItIsLicensed:
    def test_the_licence_file_is_there_and_is_mit(self):
        """A public repository with no licence is "all rights reserved": nobody may use, change or
        redistribute it, which is the opposite of publishing it."""
        body = (REPO / "LICENSE").read_text(encoding="utf-8")
        assert "MIT License" in body
        assert "WITHOUT WARRANTY OF ANY KIND" in body
        assert re.search(r"Copyright \(c\) \d{4}", body)

    def test_the_package_metadata_agrees_with_the_licence_file(self):
        """Two places that can disagree, and a consumer reads the metadata rather than the file."""
        assert 'license = { text = "MIT" }' in (REPO / "pyproject.toml").read_text(encoding="utf-8")

    def test_the_readme_says_what_the_licence_cannot_grant(self):
        """MIT covers the code and says nothing about the material it is pointed at. Publishing a
        yt-dlp front end without stating that is leaving the reader to find out."""
        body = (REPO / "README.md").read_text(encoding="utf-8")
        assert "[LICENSE](LICENSE)" in body
        # Normalised, because the sentence is wrapped at 100 columns like everything else here and
        # "terms of service" happens to straddle a line break. A prose assertion that depends on
        # where the wrap fell is a test that fails the next time somebody reflows a paragraph.
        prose = " ".join(body.split())
        assert "against YouTube's terms of service" in prose
        assert "is yours to answer" in prose
