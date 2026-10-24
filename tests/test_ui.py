"""The web remote's app shell — PLAN.md Phase 4.

Two questions are worth testing here and they pull in opposite directions.

The first is *"does the page load at all on a phone"*, which requires that the shell be served
without the shared token, because a browser cannot put a header on the document request it makes on
your behalf. The second is *"did that exemption cost anything"*, which requires proof that it covers
the shell and nothing else — no API route, no unknown path, and nothing reachable by asking for the
shell with a ``..`` in it.

So most of this file is the second question. The exemption is the only hole PLAN.md 3.2's "checked
on *every* route" has ever had, and a test that only proved the page loads would be the test a
mistake here would pass.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tunebox import __version__
from tunebox.api import ui
from tunebox.api.app import create_app
from tunebox.api.auth import TokenAuthMiddleware
from tunebox.config import Config
from tunebox.jobs.store import JobStore

from fakes import FakePlayer, FakeSource, RecordingTagWriter

TOKEN = "test-token-that-is-long-enough"
AUTH = {"X-Tunebox-Token": TOKEN}

#: Exactly the paths the token middleware is allowed to wave through, and the reason each one is
#: there is in :mod:`tunebox.api.ui`. A new entry belongs in a review, not in a diff nobody reads.
SHELL = (
    "/",
    "/sw.js",
    "/manifest.webmanifest",
    "/ui/app.css",
    "/ui/app.js",
    "/ui/icon.svg",
    "/ui/icon-180.png",
    "/ui/icon-192.png",
    "/ui/icon-512.png",
    "/ui/icon-maskable.png",
)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        music_root=tmp_path / "music",
        db_path=tmp_path / "jobs.db",
        api_token=TOKEN,
        ytdlp=("false",),
    )


@pytest.fixture
def store(config: Config) -> JobStore:
    store = JobStore(config.database_path)
    yield store
    store.close()


def build(config: Config, store: JobStore):
    return create_app(
        config,
        store=store,
        source=FakeSource(),
        tag_writer=RecordingTagWriter(),
        run_worker=False,
        player=FakePlayer(),
        keep_output=False,
    )


@pytest.fixture
def client(config: Config, store: JobStore) -> TestClient:
    return TestClient(build(config, store))


# ------------------------------------------------------------------ it is served


class TestTheShellIsServed:
    @pytest.mark.parametrize("path", SHELL)
    def test_without_a_token(self, client: TestClient, path: str):
        """The whole point: a phone's first request cannot carry a header."""
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.content, path

    def test_the_root_is_the_page(self, client: TestClient):
        response = client.get("/")
        assert response.headers["content-type"] == "text/html; charset=utf-8"
        assert "<title>tunebox</title>" in response.text

    @pytest.mark.parametrize(
        "path,content_type",
        [
            ("/ui/app.css", "text/css; charset=utf-8"),
            ("/ui/app.js", "text/javascript; charset=utf-8"),
            ("/sw.js", "text/javascript; charset=utf-8"),
            ("/manifest.webmanifest", "application/manifest+json"),
            ("/ui/icon.svg", "image/svg+xml"),
            ("/ui/icon-192.png", "image/png"),
        ],
    )
    def test_content_types(self, client: TestClient, path: str, content_type: str):
        """A manifest served as ``text/plain`` is a manifest the browser ignores, and an
        installable app that silently is not one is the hardest kind of Phase 4 failure to notice."""
        assert client.get(path).headers["content-type"] == content_type

    def test_the_icons_are_real_pngs(self, client: TestClient):
        for path in ("/ui/icon-180.png", "/ui/icon-192.png", "/ui/icon-512.png", "/ui/icon-maskable.png"):
            assert client.get(path).content.startswith(b"\x89PNG\r\n\x1a\n"), path

    def test_the_document_and_the_worker_are_revalidated(self, client: TestClient):
        """``no-cache`` on the three stable URLs, or an upgrade would not reach a phone that has
        already installed the app."""
        for path in ("/", "/sw.js", "/manifest.webmanifest"):
            assert client.get(path).headers["cache-control"] == "no-cache", path

    def test_versioned_assets_may_be_cached(self, client: TestClient):
        assert client.get("/ui/app.js").headers["cache-control"] == "public, max-age=3600"

    def test_a_token_is_accepted_too(self, client: TestClient):
        """Exempt means "not required", not "rejected" — the page sends the header on API calls and
        a browser that attached it here anyway must not get a 401."""
        assert client.get("/", headers=AUTH).status_code == 200


# --------------------------------------------------------------- it is templated


class TestTemplating:
    @pytest.mark.parametrize("path", ["/", "/sw.js", "/manifest.webmanifest", "/ui/app.js"])
    def test_no_placeholder_survives(self, client: TestClient, path: str):
        text = client.get(path).text
        assert "__TUNEBOX_VERSION__" not in text
        assert "__TUNEBOX_BUILD__" not in text
        assert "__OWNTONE_URL__" not in text
        assert "__PLAYER_PORT__" not in text

    def test_the_version_reaches_the_page_and_the_worker(self, client: TestClient):
        assert __version__ in client.get("/").text
        # The service worker's cache name carries the version; that is the entire upgrade mechanism.
        assert "tunebox-shell-${VERSION}" in client.get("/sw.js").text
        assert f'VERSION = "{__version__}"' in client.get("/sw.js").text

    def test_a_loopback_player_leaves_the_link_to_the_browser(self, client: TestClient):
        """With OwnTone on loopback there is no URL the *phone* could use, so the page is told the
        port and works it out from the host it reached us on."""
        body = client.get("/").text
        assert 'data-owntone-url=""' in body
        assert 'data-player-port="3689"' in body

    def test_a_remote_player_is_linked_directly(self, config: Config, store: JobStore):
        """When the daemon is genuinely on another machine, its own URL is the right link."""
        remote = Config(**{**config.__dict__, "player_url": "http://192.168.50.11:3689"})
        with TestClient(build(remote, store)) as client:
            assert 'data-owntone-url="http://192.168.50.11:3689"' in client.get("/").text

    def test_an_explicit_link_wins(self, config: Config, store: JobStore):
        explicit = Config(**{**config.__dict__, "owntone_ui_url": "http://music.lan:3689/"})
        with TestClient(build(explicit, store)) as client:
            assert 'data-owntone-url="http://music.lan:3689"' in client.get("/").text


# ------------------------------------------------------- the exemption costs nothing


class TestTheCacheKeyIsTheBytes:
    """``/ui/*`` is cached by the phone, so the key has to change when the file does.

    The bug this class exists for was found on the real installation, after an upgrade: the page asked
    for ``/ui/app.js?v=0.1.0``, the assets were served ``max-age=3600``, and editing ``app.js`` without
    releasing a new version left that URL byte-identical as far as the browser was concerned. So the
    host was serving the fix and the phone was running the old script — with a stale Library panel as
    the visible symptom, which is exactly the class of bug that looks like a server bug.

    The key is now :func:`tunebox.api.ui.build_id`, a digest of the shell's own bytes. The document is
    still ``no-cache``, which is what makes a new build id reach the phone at all.
    """

    @staticmethod
    def web_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """A writable copy of the shell, so a test can change an asset and ask what happens."""
        root = tmp_path / "web"
        shutil.copytree(ui.WEB_ROOT, root)
        monkeypatch.setattr(ui, "WEB_ROOT", root)
        return root

    def test_the_page_asks_for_the_build_id_not_the_version(self, client: TestClient):
        page = client.get("/").text
        assert f"/ui/app.js?v={ui.build_id()}" in page
        assert f"/ui/app.js?v={__version__}" not in page, (
            "keying the cache on the version is the bug: it does not change when app.js does"
        )

    def test_the_worker_precaches_the_same_urls_the_page_requests(self, client: TestClient):
        """Two spellings of the same asset would mean the worker caching a URL nobody asks for, so
        the page would download it twice and never once from the cache."""
        page = client.get("/").text
        worker = client.get("/sw.js").text
        assert "`/ui/app.js?v=${BUILD}`" in worker
        assert f'BUILD = "{ui.build_id()}"' in worker
        assert f"/ui/app.js?v={ui.build_id()}" in page

    def test_the_cache_name_carries_it_too(self, client: TestClient):
        """Otherwise the old precached copy survives `activate` and the worker serves it offline."""
        worker = client.get("/sw.js").text
        assert "tunebox-shell-${VERSION}-${BUILD}" in worker

    def test_changing_an_asset_changes_the_build_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        root = self.web_copy(tmp_path, monkeypatch)
        before = ui.build_id()
        assert ui.build_id() == before, "it must be stable, or every restart re-downloads the shell"

        script = root / "app.js"
        script.write_text(script.read_text(encoding="utf-8") + "\n// one more line\n", encoding="utf-8")

        assert ui.build_id() != before

    def test_a_changed_asset_reaches_the_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: Config, store: JobStore
    ):
        """End to end, because this is the property that was broken: edit an asset, restart, and the
        URL in the document is one no browser can have cached."""
        root = self.web_copy(tmp_path, monkeypatch)
        first = TestClient(build(config, store)).get("/").text

        script = root / "app.js"
        script.write_text(script.read_text(encoding="utf-8") + "\n// changed\n", encoding="utf-8")
        second = TestClient(build(config, store)).get("/").text

        asked = lambda page: re.findall(r"/ui/app\.js\?v=([0-9a-f]+)", page)  # noqa: E731
        assert asked(first) and asked(second)
        assert asked(first) != asked(second)

    def test_the_version_is_still_what_the_footer_shows(self, client: TestClient):
        """The build id is for caches; the version is what a person reads and reports a bug against."""
        page = client.get("/").text
        assert f'data-version="{__version__}"' in page


class TestTheExemptionIsNarrow:
    @pytest.mark.parametrize(
        "path",
        ["/healthz", "/search?q=x", "/jobs", "/library", "/player", "/player/queue", "/docs"],
    )
    def test_the_api_still_needs_the_token(self, client: TestClient, path: str):
        assert client.get(path).status_code == 401

    def test_an_unknown_path_is_still_401(self, client: TestClient):
        """A 404 would confirm which paths exist to an unauthenticated caller."""
        assert client.get("/nope").status_code == 401

    def test_an_unknown_asset_is_404_not_a_file(self, client: TestClient):
        assert client.get("/ui/secrets.txt").status_code == 404

    @pytest.mark.parametrize(
        "path",
        [
            "/ui/../app.py",
            "/ui/%2e%2e%2fapp.py",
            "/ui/..%2F..%2Fconfig.py",
            "/ui/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
            "/ui/.",
            "/ui/",
        ],
    )
    def test_traversal_finds_nothing(self, client: TestClient, path: str):
        """The asset route is a dictionary lookup of names decided at startup, so there is no
        filesystem access to traverse — but the *route* accepting a slash would still be a bug."""
        response = client.get(path)
        assert response.status_code in (401, 404), path
        assert b"import" not in response.content

    def test_the_source_tree_is_not_reachable(self, client: TestClient):
        for path in ("/ui/app.py", "/ui/ui.py", "/ui/__init__.py", "/ui/index.html"):
            # index.html is served at "/" and deliberately *not* under /ui/: one URL per asset.
            assert client.get(path).status_code == 404, path

    def test_disabling_the_ui_closes_the_hole_completely(self, config: Config, store: JobStore):
        """``TUNEBOX_UI=0`` must remove the routes *and* the exemption, not one of the two."""
        headless = Config(**{**config.__dict__, "ui_enabled": False})
        with TestClient(build(headless, store)) as client:
            for path in SHELL:
                assert client.get(path).status_code == 401, path
            assert client.get("/healthz", headers=AUTH).status_code == 200


class TestTheMiddlewareGuardsItself:
    @pytest.mark.parametrize("prefix", ["/", ""])
    def test_a_root_prefix_is_refused(self, prefix: str):
        """``startswith("/")`` is true of every path there is. A caller reaching for a prefix when
        it meant one page would otherwise unauthenticate the whole API, silently, and every test
        for the routes it *did* think about would still pass."""
        with pytest.raises(ValueError, match="every route"):
            TokenAuthMiddleware(lambda *a: None, TOKEN, exempt_prefixes=[prefix])

    def test_an_exact_path_is_not_a_prefix(self):
        middleware = TokenAuthMiddleware(lambda *a: None, TOKEN, exempt_paths=["/"])
        assert middleware._is_exempt("/")
        assert not middleware._is_exempt("/healthz")

    def test_the_shell_list_is_what_the_app_exempts(self):
        """Keeps this file's SHELL constant honest about the real exemption."""
        exempt = set(ui.exempt_paths())
        prefixes = ui.exempt_prefixes()
        for path in SHELL:
            assert path in exempt or any(path.startswith(p) for p in prefixes), path


# --------------------------------------------------------------- headers and content


class TestHardening:
    def test_the_csp_allows_nothing_off_box(self, client: TestClient):
        csp = client.get("/").headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert "script-src 'self'" in csp
        assert "connect-src 'self'" in csp
        assert "unsafe-inline" not in csp
        assert "unsafe-eval" not in csp

    def test_form_action_is_blocked(self, client: TestClient):
        """The forms are submitted by JavaScript. Blocking a native submission is free, and it is
        what stops a broken script from putting the shared token into a query string — and therefore
        into the access log PLAN.md 3.2 wants to be able to read."""
        assert "form-action 'none'" in client.get("/").headers["content-security-policy"]

    def test_the_page_cannot_be_framed(self, client: TestClient):
        headers = client.get("/").headers
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert headers["x-frame-options"] == "DENY"

    def test_no_sniffing_and_no_referrer(self, client: TestClient):
        headers = client.get("/").headers
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "no-referrer"

    def test_no_cookie_is_ever_set(self, client: TestClient):
        """Phase 3 measured OwnTone's remote as setting zero cookies and concluded the remote is
        stateless. That stays true: the token lives in the phone's localStorage, and nothing here
        hands out ambient credentials a cross-site request could ride on."""
        for path in SHELL:
            assert "set-cookie" not in client.get(path).headers, path


class TestTheAssetsThemselves:
    """Read the files, not the routes: these are the mistakes a browser would find and a unit test
    would not."""

    @staticmethod
    def read(name: str) -> str:
        return (ui.WEB_ROOT / name).read_text(encoding="utf-8")

    @classmethod
    def code(cls, name: str) -> str:
        """The file with its comments removed, so these tests check behaviour and not prose.

        Needed rather than tidy: the modules below explain themselves at length, and those
        explanations legitimately contain the shapes being asserted against — an example URL, the
        words ``<script>``. Asserting over the comments would either fail on documentation or force
        the documentation to avoid naming what it documents.
        """
        text = cls.read(name)
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        return re.sub(r"^[ \t]*//.*$", "", text, flags=re.M)

    def test_nothing_is_fetched_from_the_internet(self):
        """PLAN.md's non-goals include any internet-reachable endpoint, and a remote that pulled a
        font or a thumbnail from a CDN would make the house's music browser phone home on every
        search. The only absolute URLs allowed in the shell are XML namespaces."""
        for name in ("index.html", "app.css", "app.js", "sw.js", "manifest.webmanifest"):
            for match in re.findall(r"https?://[^\s\"'()]+", self.code(name)):
                assert match.startswith("http://www.w3.org/"), f"{name} reaches out to {match}"

    def test_every_asset_the_page_asks_for_exists(self):
        """A renamed icon is a 404 on a phone and nowhere else."""
        referenced = set(re.findall(r"/ui/([A-Za-z0-9._-]+)", self.read("index.html")))
        assert referenced, "the page should reference its own assets"
        assert referenced <= set(ui.UI_ASSETS), referenced - set(ui.UI_ASSETS)

    def test_the_worker_precaches_only_real_assets(self):
        referenced = set(re.findall(r"/ui/([A-Za-z0-9._-]+)", self.read("sw.js")))
        assert referenced <= set(ui.UI_ASSETS), referenced - set(ui.UI_ASSETS)

    def test_the_worker_does_not_cache_the_api(self):
        """A cached now-playing screen is worse than none: it shows a track that stopped ten minutes
        ago with a pause button that does nothing."""
        text = self.code("sw.js")
        assert "isShell" in text
        for route in ("/player", "/jobs", "/library", "/search", "/ingest"):
            assert f'"{route}"' not in text, f"the service worker mentions {route}"

    def test_the_page_has_no_inline_script(self):
        """Which is what lets the CSP be ``script-src 'self'`` with no nonce and no unsafe-inline."""
        html = self.code("index.html")
        assert not re.search(r"<script(?![^>]*\ssrc=)", html)
        assert not re.search(r"\son[a-z]+\s*=", html), "inline event handler in the markup"

    def test_the_manifest_is_installable(self):
        """The Phase 4 exit criterion is one icon on the phone; these are the fields that decide
        whether a browser offers to create it."""
        manifest = json.loads(
            self.read("manifest.webmanifest").replace("__TUNEBOX_VERSION__", __version__)
        )
        assert manifest["start_url"] == "/"
        assert manifest["scope"] == "/"
        assert manifest["display"] == "standalone"
        assert manifest["name"] and manifest["short_name"]
        sizes = {icon["sizes"] for icon in manifest["icons"]}
        assert {"192x192", "512x512"} <= sizes
        assert any(icon.get("purpose") == "maskable" for icon in manifest["icons"])
        for icon in manifest["icons"]:
            name = icon["src"].split("?")[0].removeprefix("/ui/")
            assert name in ui.UI_ASSETS, name

    def test_the_apple_touch_icon_is_declared(self):
        """iOS ignores the manifest's icons for a home-screen app and reads this link instead, so
        without it the icon is a screenshot of the page."""
        assert 'rel="apple-touch-icon"' in self.read("index.html")

    def test_the_worker_is_registered_at_the_root_scope(self):
        """A worker under ``/ui/`` could not control the page at ``/``, and the failure mode is an
        app that installs and then will not open offline."""
        assert '"/sw.js", { scope: "/" }' in self.read("app.js")

    def test_hidden_beats_the_stylesheet(self):
        """Found by rendering the page in a real browser, which is the only thing that would have.

        The whole UI is one document whose sections are toggled with the ``hidden`` attribute — but
        ``hidden`` works through a *user-agent* rule, and any author rule setting ``display`` on the
        same element wins on specificity. ``.gate`` sets ``display: grid``, so unlocking left the
        token card rendered on top of the app: the attribute changed and the layout did not. Every
        test in this file passed, because every one of them asserted on the DOM.
        """
        css = self.code("app.css")
        assert re.search(r"\[hidden\]\s*\{\s*display:\s*none\s*!important", css)


class TestTheTokenReveal:
    """"Show token" in the footer — the bridge to a home-screen icon.

    Adding the page to the home screen cannot carry the token across, for two reasons that compound:
    ``tokenFromFragment`` strips it from the address bar as soon as it reads it, so the URL the
    shortcut captures has none, and iOS gives an installed web app its own storage container, so
    ``readStoredToken`` finds nothing there either. Without this control the only way to fill the
    gate in the installed app is to go and read ``tunebox.env`` on the host.

    It puts the shared secret on screen, so the assertions below are mostly about it being on screen
    *only* when asked.
    """

    @staticmethod
    def read(name: str) -> str:
        return (ui.WEB_ROOT / name).read_text(encoding="utf-8")

    def test_the_reveal_ships_hidden(self):
        """The default state of a footer that displays a credential is "not displaying it"."""
        markup = self.read("index.html")
        reveal = re.search(r'<p id="token-reveal"[^>]*>', markup)
        assert reveal, "the reveal element should be in the markup"
        assert "hidden" in reveal.group(0)

    def test_the_button_starts_collapsed(self):
        markup = self.read("index.html")
        button = re.search(r'<button[^>]*id="btn-show-token"[^>]*>', markup, flags=re.S)
        assert button
        assert 'aria-expanded="false"' in button.group(0)

    def test_no_token_is_baked_into_the_shipped_markup(self):
        """The reveal is filled in by script from the token already on the device. If a real token
        were ever pasted into the template it would be served to anyone on the LAN, because the
        shell is the one thing exempt from auth."""
        assert re.search(r'<code id="token-text"></code>', self.read("index.html"))

    def test_hiding_it_again_empties_the_element(self):
        """Not merely ``hidden``: a token left in the DOM is a token in the next screenshot of an
        inspector, and nothing needs it there once the panel is closed."""
        script = TestTheAssetsThemselves.code("app.js")
        assert 'text.textContent = ""' in script

    def test_it_reveals_the_token_in_use_not_one_it_fetched(self):
        """``state.token`` is what the page authenticated with, so what is shown is by construction
        the value that works. There is no endpoint that hands out the token, and there must not be."""
        script = TestTheAssetsThemselves.code("app.js")
        assert "text.textContent = state.token" in script
        assert "/api/token" not in script

    def test_it_is_selectable_in_one_gesture(self):
        """``user-select: all`` is the entire point: a long-press has to take the whole 43-character
        token, not the word the browser guesses at."""
        css = self.read("app.css")
        block = css[css.index(".token-reveal code") :]
        assert "user-select: all" in block
        assert "-webkit-user-select: all" in block, "Safari is the browser this exists for"
        assert "word-break: break-all" in block, "the token has no spaces to wrap at"

    def test_the_footer_still_offers_the_way_out(self):
        """Revealing a credential and forgetting it belong next to each other."""
        markup = self.read("index.html")
        assert 'id="btn-forget"' in markup
        assert 'id="btn-show-token"' in markup


class TestTheSilentRoom:
    """Two things the page must say about a speaker that is not carrying the audio.

    Both exist because of a measured three-minute silence on the real hardware: OwnTone reported
    ``state: play`` and advanced the clock while every speaker had failed to start, so the phone drew
    a confident now-playing screen for a room with no sound in it. A DOM-level assertion cannot see
    that; what it *can* pin is that the page has somewhere to say it and something to say.
    """

    @staticmethod
    def code(name: str) -> str:
        return TestTheAssetsThemselves.code(name)

    def test_the_now_playing_panel_has_a_place_to_say_it(self):
        markup = self.code("index.html")
        panel = markup[markup.index('id="panel-now"') : markup.index('id="panel-add"')]
        assert 'id="speaker-alert"' in panel
        # Announced, but politely: it is not an error the user caused, and `alert` would interrupt.
        alert = re.search(r'<p[^>]*id="speaker-alert"[^>]*>', panel)
        assert alert and 'role="status"' in alert.group(0)
        assert alert and "hidden" in alert.group(0), "it must not be visible before the first poll"

    def test_the_message_names_the_symptom_and_not_the_field(self):
        """"nothing is coming out" is the fact somebody in the room can check. A message about
        outputs not being selected describes the daemon's data model instead."""
        script = self.code("app.js")
        assert "playing_without_output" in script
        assert "nothing is coming out" in script

    def test_the_wake_button_says_it_is_working(self):
        """The select request is *slow on purpose* — the server retries a sleeping speaker — so a
        button that looked untouched for four seconds would read as a button that does nothing."""
        script = self.code("app.js")
        assert "Waking the speaker" in script
        # And it must refuse a second tap while the first is in flight.
        waking = script[script.index("async function waking()") :]
        assert "disabled = true" in waking[: waking.index("finally")]
        assert "disabled = false" in waking


class TestPlaybackModeControls:
    """The tri-state repeat button and the shuffle toggle (TODO.md, *Playback modes*).

    Three properties, each of which has a failure mode a browser would show and a Python unit test
    would not: the state has to be in the *text* (this stylesheet's own rule is that nothing conveys
    state by colour alone, and a three-way control is the hardest case for it), the repeat cycle has
    to cover all three modes and come back, and the command has to send an absolute mode rather than
    asking the server to advance one.
    """

    @staticmethod
    def code(name: str) -> str:
        return TestTheAssetsThemselves.code(name)

    def test_both_controls_are_on_the_now_playing_panel(self):
        markup = self.code("index.html")
        panel = markup[markup.index('id="panel-now"') : markup.index('id="panel-add"')]
        assert 'id="btn-repeat"' in panel
        assert 'id="btn-shuffle"' in panel

    def test_the_repeat_button_ships_in_a_known_state(self):
        """``data-mode`` is what the stylesheet highlights on, so an absent one would draw "off" as
        if it were on until the first poll arrived."""
        button = re.search(r'<button[^>]*id="btn-repeat"[^>]*>', self.code("index.html"))
        assert button and 'data-mode="off"' in button.group(0)

    def test_the_shuffle_toggle_says_it_is_a_toggle(self):
        """``aria-pressed`` on the two-state control and *not* on the tri-state one: "pressed" cannot
        express which of three modes is on, so repeat carries its state in its label instead."""
        markup = self.code("index.html")
        shuffle = re.search(r'<button[^>]*id="btn-shuffle"[^>]*>', markup)
        repeat = re.search(r'<button[^>]*id="btn-repeat"[^>]*>', markup)
        assert shuffle and 'aria-pressed="false"' in shuffle.group(0)
        assert repeat and "aria-pressed" not in repeat.group(0)

    def test_every_mode_has_a_label_and_the_cycle_closes(self):
        script = self.code("app.js")
        labels = dict(re.findall(r'(\w+): "(Repeat[^"]*)"', script))
        assert set(labels) == {"off", "all", "single"}
        # The three labels must differ, or the button would report two modes with one word.
        assert len(set(labels.values())) == 3
        # Read out of ``REPEAT_NEXT`` specifically rather than by scanning the file for
        # ``word: "off"``. The looser pattern caught every other object that happens to hold a
        # repeat mode — ``state.local`` has one, since the phone output keeps its own — and would
        # therefore fail for a reason that has nothing to do with whether the cycle closes.
        table = re.search(r"const REPEAT_NEXT = \{([^}]*)\}", script)
        assert table, "REPEAT_NEXT is where the cycle is defined"
        cycle = dict(re.findall(r'(\w+): "(off|all|single)"', table.group(1)))
        assert cycle == {"off": "all", "all": "single", "single": "off"}

    def test_each_label_names_the_mode_it_belongs_to(self):
        """Swapping two labels is the mistake this catches, and it is invisible to every other test
        here: the key set is still the three modes, the three words are still distinct, and the
        button then confidently says "Repeat one song" while the queue is what repeats. ``all`` and
        ``single`` are the two TODO.md items, so getting them the wrong way round would ship exactly
        the feature that was not asked for."""
        script = self.code("app.js")
        labels = dict(re.findall(r'(\w+): "(Repeat[^"]*)"', script))
        assert labels["off"] == "Repeat off"
        assert labels["all"] == "Repeat the queue"
        assert labels["single"] == "Repeat one song"

    def test_the_highlight_follows_the_mode_rather_than_the_initial_markup(self):
        """``data-mode`` is written on every render, not only shipped in the HTML. Without that
        assignment the label would change and the border would stay stuck on "off" forever — a state
        cue that is wrong is worse than one that is absent, and the stylesheet's whole justification
        for the highlight is that it agrees with the text."""
        script = self.code("app.js")
        assert "repeat.dataset.mode = mode" in script

    def test_the_state_is_in_the_text_not_only_the_style(self):
        """Both labels name their state, and the CSS highlight is additive — a phone in a bright
        kitchen, and a screen reader, both get the answer from the same words."""
        script = self.code("app.js")
        assert 'player.shuffle ? "Shuffle on" : "Shuffle off"' in script
        assert "repeat.textContent = REPEAT_LABEL[mode]" in script

    def test_it_sends_a_mode_and_never_asks_the_server_to_cycle(self):
        """The session is shared (PLAN.md 2), so an absolute mode is what makes two phones converge.
        A ``/player/repeat/cycle`` route would also be a second place that knows the order.

        Asserted as "the mode is computed from ``REPEAT_NEXT`` and then *sent*" rather than as one
        exact line, because the handler now has to choose between two outputs first (the house
        speaker or this phone) and the absolute-mode property is about neither of them."""
        script = self.code("app.js")
        assert "REPEAT_NEXT[repeatMode(state.player)]" in script
        assert 'command("/player/repeat", { mode })' in script
        assert "/player/repeat/cycle" not in script
        assert "!(state.player && state.player.shuffle)" in script
        assert 'command("/player/shuffle", { enabled })' in script

    def test_an_unexpected_mode_from_the_daemon_does_not_wedge_the_button(self):
        """``repeat`` is nullable in the API and the daemon is the one filling it in. An unknown value
        draws as "off" and advances to "all", which costs one tap; a lookup that returned
        ``undefined`` would leave a button whose label was blank and whose command was invalid."""
        script = self.code("app.js")
        assert "hasOwnProperty.call(REPEAT_NEXT, mode)" in script

    def test_the_highlight_is_a_second_signal_rather_than_the_only_one(self):
        css = self.code("app.css")
        block = css[css.index(".modes") :]
        assert '[data-mode]:not([data-mode="off"])' in block
        assert 'button[aria-pressed="true"]' in block


class TestTappingTheQueuePlaysTheSong:
    """The queue's rows are the play button (TODO.md, *Tapping the queue plays the song*).

    The bug this replaces was not a crash: the rows rendered, showed the right titles and highlighted
    the current track — and did nothing when tapped, so reaching track seven meant ⏭ six times. What
    a Python test can hold to account is the part a browser would otherwise be the only witness to:
    that the tappable thing is a real ``<button>`` (a click handler on a ``<div>`` is unreachable by
    keyboard and silent to a screen reader), that it sends the item's **id** rather than its position,
    and that the row for the track already loaded is not exempt.
    """

    @staticmethod
    def code(name: str) -> str:
        return TestTheAssetsThemselves.code(name)

    def test_the_row_itself_is_the_control(self):
        script = self.code("app.js")
        assert "queue.map((item) => queueRow(item, player.item_id))" in script
        assert "function queueRow(" in script

    def test_the_tappable_row_is_a_real_button(self):
        """The whole accessibility story of this feature. A ``<div>`` with a click handler is not in
        the tab order, is not announced as a control and ignores Enter and Space — and it looks
        identical in a screenshot, which is why this is asserted rather than eyeballed."""
        script = self.code("app.js")
        assert 'activate ? make("button", "row-main") : make("div", "row-main")' in script
        body = script[script.index("function row(") : script.index("function fill(")]
        assert 'main.type = "button"' in body
        assert 'main.addEventListener("click", activate)' in body

    def test_it_sends_the_item_id_and_not_a_position(self):
        """The queue is shared (PLAN.md 2), so a position is only true of the poll it came from while
        an id either names the track that was tapped or is answered 404."""
        script = self.code("app.js")
        assert "`/player/queue/${encodeURIComponent(itemId)}/play`" in script
        assert "playQueueItem(item.id)" in script
        assert "/position" not in script

    def test_the_id_is_escaped_on_its_way_into_the_path(self):
        """It comes from the daemon, which is not the same as being safe to interpolate: the one
        rule of this file is that nothing from elsewhere is trusted, and a path segment is a place
        where a ``/`` or a ``?`` would change which route is called."""
        assert "encodeURIComponent(itemId)" in self.code("app.js")

    def test_a_row_with_no_id_is_not_pretending_to_be_playable(self):
        """``QueueItem.id`` is an empty string when the daemon sent none. There is nothing to send
        for such a row, so it must not draw as a button that silently fails."""
        assert "item.id ? () => playQueueItem(item.id) : undefined" in self.code("app.js")

    def test_the_current_row_is_still_tappable_and_says_what_it_will_do(self):
        """A dead row is the bug being fixed, so the loaded track is not exempt — but the daemon
        restarts it rather than resuming, and the label is where that is admitted."""
        script = self.code("app.js")
        assert 'current ? `Restart “${title}”` : `Play “${title}”`' in script
        assert 'main.setAttribute("aria-current", "true")' in script

    def test_every_row_carries_an_accessible_name(self):
        """The button's own text is the title and artist, which reads as a label by accident rather
        than by design; an explicit one is what makes "Play" audible at all."""
        script = self.code("app.js")
        body = script[script.index("function queueRow(") : script.index("function playQueueItem(")]
        assert 'main.setAttribute("aria-label"' in body

    def test_it_redraws_from_the_response_rather_than_guessing(self):
        """Rule 2 of app.js. Optimistic local state would be wrong regularly here — two phones and
        OwnTone's own web UI touch this queue."""
        script = self.code("app.js")
        body = script[script.index("function playQueueItem(") :]
        assert "return command(" in body[: body.index("}")]

    def test_the_button_does_not_look_like_a_button(self):
        """A filled, bordered control the full width of every row would read as a wall of buttons.
        What is kept is the behaviour, not the chrome — and the focus ring, which is not negotiable."""
        css = self.code("app.css")
        block = css[css.index("button.row-main") : css.index(".row-sub")]
        assert "border: 0" in block
        assert "background: none" in block
        assert "text-align: left" in block, "a button centres its text; a list of titles must not"
        assert "min-height: var(--tap)" in block, "a row is still a thumb target"
        assert ":focus-visible" not in block, "the page-wide focus ring already covers it"

    def test_the_focus_ring_still_applies_to_it(self):
        """It is a ``button``, so the page-wide rule covers it — asserted because the temptation when
        un-styling a button is to remove its outline along with its border."""
        css = self.code("app.css")
        assert ":is(a, button, input, [tabindex]):focus-visible" in css
        block = css[css.index("button.row-main") :]
        assert "outline: none" not in block


class TestAddCannotBeTappedTwice:
    """The Add button on a search result greys itself out the moment it is tapped.

    A tap on a phone is easily a double-tap, and the round trip behind this one includes a yt-dlp
    metadata request — long enough that nothing appears to have happened. The second tap cannot
    achieve anything either way, because ``POST /ingest`` dedupes an identical request back to the
    same job, so what it offers is work that will not be done.
    """

    @staticmethod
    def source() -> str:
        """The body of ``addButton``, comments stripped — the assertions below are about order."""
        code = TestTheAssetsThemselves.code("app.js")
        start = code.index("function addButton(")
        return code[start : code.index("\n  }", start)]

    def test_the_results_list_uses_it(self):
        """Not a bare ``button("Add", …)`` inline in the row: the disabling lives in the factory so a
        second caller cannot forget it, which is why there is exactly one construction site."""
        code = TestTheAssetsThemselves.code("app.js")
        assert "[addButton(item.url)]" in code
        assert code.count('button("Add"') == 1, "the only Add button should be the one in addButton()"
        assert 'button("Add"' in self.source(), "and that one should be inside the factory"

    def test_it_is_disabled_before_the_request_not_after(self):
        """The whole point is the gap between the tap and the answer. Disabling after the ``await``
        would leave it live for exactly the period this exists to cover."""
        body = self.source()
        assert body.index("node.disabled = true") < body.index("await ingest(")

    def test_the_label_says_what_the_colour_says(self):
        """This stylesheet's rule is that nothing conveys state by colour alone, and a greyed-out
        button still reading "Add" is indistinguishable from a broken one."""
        body = self.source()
        assert "Adding…" in body
        assert 'node.textContent = "Added"' in body

    def test_a_failure_gives_the_button_back(self):
        """A 401 or a stopped worker is precisely when somebody wants to try again, so the disabling
        must not outlive a request that achieved nothing."""
        body = self.source()
        assert "node.disabled = false" in body
        assert body.index('node.textContent = "Added"') < body.index("node.disabled = false"), (
            "success should return before the re-enable, or a queued track offers Add again"
        )

    def test_the_submit_button_is_held_for_the_round_trip(self):
        """With a pasted link, the search form's own button *is* the add button — same double-tap,
        same subprocess on the host. Unlike Add it comes back, because a search leaves nothing done."""
        code = TestTheAssetsThemselves.code("app.js")
        assert 'form.querySelector(\'button[type="submit"]\')' in code
        assert "go.disabled = true" in code
        assert re.search(r"finally \{\s*go\.disabled = false;", code), (
            "restoring it in `finally` is what survives an early return or a thrown error"
        )


class TestDeleteAsksFirst:
    """Delete in the Library panel — the only irreversible thing the remote can do.

    A confirmation step is not optional here, and it is not optional for a reason that is about
    thumbs rather than about principle: Play and Queue sit at the right-hand edge of every row, the
    file this removes is the only irreplaceable state the system has (PLAN.md 3.1), and a backup is
    the only undo. So the assertions below are mostly about how *far* Delete is from where a tap
    lands: behind the row's ``⋯``, and then behind a button that names the track.
    """

    @staticmethod
    def source() -> str:
        """The body of ``libraryRow``, comments stripped — these tests are about order."""
        code = TestTheAssetsThemselves.code("app.js")
        start = code.index("function libraryRow(")
        return code[start : code.index("\n  }", start)]

    def test_delete_is_not_a_third_button_next_to_play(self):
        """The row's own actions are Play and Queue. Delete is built into the confirm panel, which
        ships hidden, so a mis-tap on a moving list cannot reach it."""
        body = self.source()
        assert 'button("Play"' in body and 'button("Queue"' in body
        assert body.index("panel.hidden = true") < body.index('button("Delete"')
        assert body.index('button("Queue"') < body.index("row-confirm")

    def test_every_delete_button_is_behind_a_confirmation(self):
        """Two now — the library's, and the playlist's — and neither is reachable in one tap.

        The count is asserted rather than left open on purpose: it is the number of places in this
        page that can destroy something, and a third appearing without this test being read is exactly
        the regression the class exists to catch. What they share is the shape — each is built *after*
        its panel has been hidden — and what they do not share is the stakes, which is why the library
        one names the file and warns that it cannot be put back, while the playlist one says that no
        music is removed.
        """
        code = TestTheAssetsThemselves.code("app.js")
        assert code.count('button("Delete"') == 2
        for body in (self.source(), TestPlaylists.source()):
            assert body.index("panel.hidden = true") < body.index('button("Delete"')
            assert 'button("Cancel"' in body

    def test_the_disclosure_is_announced(self):
        """``⋯`` is not a label. Without ``aria-label`` and ``aria-expanded`` a screen reader gets
        an unnamed button that changes nothing it can perceive."""
        body = self.source()
        assert 'more.setAttribute("aria-label"' in body
        assert 'more.setAttribute("aria-expanded", "false")' in body
        assert 'more.setAttribute("aria-controls", panel.id)' in body
        assert "panel.id = `row-confirm-" in body, "aria-controls needs an id that resolves"

    def test_the_confirmation_names_the_track_and_says_it_is_final(self):
        """"Are you sure?" is a question nobody reads. The track's own title is what tells somebody
        they are about to delete the wrong one."""
        body = self.source()
        warn = body[body.index('"row-warn"') : body.index("const actions")]
        assert "${track.title}" in warn
        assert "removed from the disk" in warn

    def test_cancel_is_offered_beside_it(self):
        body = self.source()
        assert 'button("Cancel"' in body
        assert body.index('button("Delete"') < body.index('button("Cancel"')

    def test_it_is_not_a_native_confirm_dialog(self):
        """A browser may suppress ``window.confirm``, and in an installed web app the modal looks
        like the page has crashed. The confirmation is part of the row instead."""
        code = TestTheAssetsThemselves.code("app.js")
        assert "window.confirm" not in code
        assert not re.search(r"(?<![.\w])confirm\(", code)

    def test_the_request_is_a_body_not_a_path(self):
        """``DELETE /library/{path}`` is the shape this deliberately is not: a library path holds
        slashes and, in this household, CJK — and encoding one twice is how a delete names the wrong
        file."""
        code = TestTheAssetsThemselves.code("app.js")
        assert 'api("/library", { method: "DELETE", body: { paths: [track.path] } })' in code
        assert "`/library/" not in code

    def test_both_halves_of_the_screen_are_redrawn_afterwards(self):
        """The queue may have been holding the file that just disappeared, so the listing alone is
        not enough.

        ``refreshTransport`` rather than ``refreshPlayer``: the queue that was holding it might be
        the *phone's*, which no amount of asking the server would correct — so the delete also has
        to take the track out of the local queue, which is what ``localForget`` is for."""
        code = TestTheAssetsThemselves.code("app.js")
        body = code[code.index("async function deleteTrack(") :]
        assert "await loadLibrary()" in body
        assert "await refreshTransport()" in body
        assert "localForget(track.path)" in body


class TestPlaylists:
    """The Playlists panel: save the queue, play a playlist, append one track, delete the list.

    A playlist is an ``.m3u`` file in the music root that tunebox wrote, so there is no local state
    here to get out of step — every one of these is a request and a redraw. The assertions worth having
    are therefore about the two things a reading of the API cannot establish: that the panel exists and
    is wired at all, and that its delete is as far from a thumb as the library's is while saying
    something different, because it destroys no audio.
    """

    @staticmethod
    def source() -> str:
        """The body of ``playlistRow``, which is where the row's buttons are decided."""
        code = TestTheAssetsThemselves.code("app.js")
        start = code.index("function playlistRow(")
        return code[start : code.index("\n  }", start)]

    def test_the_panel_is_a_tab_like_the_others(self):
        markup = TestTheAssetsThemselves.code("index.html")
        assert 'id="tab-playlists"' in markup and 'id="panel-playlists"' in markup
        assert 'aria-controls="panel-playlists"' in markup
        assert 'aria-labelledby="tab-playlists"' in markup

    def test_the_script_knows_about_the_tab(self):
        """``TABS`` drives the roving tabindex and the arrow keys; a panel missing from it is a tab
        that cannot be reached from the keyboard."""
        code = TestTheAssetsThemselves.code("app.js")
        assert '"playlists"' in re.findall(r"const TABS = \[([^\]]+)\]", code)[0]

    def test_opening_the_tab_re_reads_the_directory(self):
        """Same rule as the Library panel, and for the same reason: a playlist can have been created
        from a terminal or another phone since this panel was last drawn."""
        code = TestTheAssetsThemselves.code("app.js")
        assert 'if (name === "playlists") loadPlaylists();' in code

    def test_creation_offers_both_ways_in(self):
        """Two buttons on one name field, because "save the queue" alone was the wrong place to stop:
        it means a playlist can only be built by first making it the thing that is playing."""
        code = TestTheAssetsThemselves.code("app.js")
        assert "body: { name, ...body }" in code
        assert "{ from_queue: true }" in code, "the submit button saves the queue"
        assert 'empty.addEventListener("click"' in code, "and the other one starts an empty list"
        markup = TestTheAssetsThemselves.code("index.html")
        assert 'id="pl-name"' in markup and 'for="pl-name"' in markup
        assert 'id="btn-pl-empty"' in markup

    def test_a_playlist_with_holes_in_it_says_so(self):
        """OwnTone skips an entry it cannot resolve and says nothing, so a list that plays eight of
        its ten tracks would otherwise be silent about it. The count is on the row, and the entry
        itself is in the expanded list with a line saying what is wrong with it."""
        code = TestTheAssetsThemselves.code("app.js")
        subtitle = code[code.index("function playlistSubtitle(") : code.index("function playlistRow(")]
        assert "missing_count" in subtitle
        assert "${missing} missing" in subtitle
        rows = code[code.index("function entryRow(") :]
        assert "entry.missing" in rows
        assert "This file is not there" in rows

    def test_a_row_can_be_opened_to_see_what_is_in_it(self):
        """The complaint this answers: a playlist used to be a name with a count beside it, so there
        was no way to find out which songs were in one. The name is now the control, and it is a real
        ``<button>`` with ``aria-expanded`` rather than a div with a click handler."""
        code = TestTheAssetsThemselves.code("app.js")
        body = self.source()
        assert "() => toggle()" in body, "the row's activate handler expands it"
        assert 'main.setAttribute("aria-expanded", "false")' in body
        assert 'main.setAttribute("aria-controls", tracks.id)' in body
        assert "async function drawEntries(" in code
        assert 'api(`/playlists/${encodeURIComponent(name)}`)' in code

    def test_an_edit_does_not_collapse_the_playlist_being_edited(self):
        """Every edit redraws the list from the response, so without remembering which row was open
        the first ✕ would close the playlist under the thumb that pressed it."""
        code = TestTheAssetsThemselves.code("app.js")
        assert "openPlaylist: null," in code, "the open row is state, not DOM"
        assert "if (state.openPlaylist === playlist.name) toggle();" in self.source()

    def test_entries_are_numbered_from_one_and_sent_from_zero(self):
        """What the API takes is the entry's ``position``, which is the only handle that survives a
        playlist holding the same track twice — an index into the *playable* tracks would not, since a
        missing entry occupies a position too."""
        code = TestTheAssetsThemselves.code("app.js")
        rows = code[code.index("function entryRow(") : code.index("function playPlaylist(")]
        assert "`${index + 1}. ${title}`" in rows
        assert "entry.position" in rows
        assert "body: { to: entry.position - 1 }" in rows
        assert "body: { to: entry.position + 1 }" in rows
        assert "positions: [entry.position]" in rows

    def test_the_ends_of_the_list_cannot_be_moved_past(self):
        code = TestTheAssetsThemselves.code("app.js")
        rows = code[code.index("function entryRow(") : code.index("function playPlaylist(")]
        assert "up.disabled = index === 0" in rows
        assert "down.disabled = last" in rows

    def test_removing_a_track_from_a_playlist_asks_no_confirmation(self):
        """Unlike the Library panel's delete, and the difference is the point: this removes a line of
        text and the audio stays on disk, so a confirmation strip would be ceremony that teaches people
        to tap through the one that matters."""
        code = TestTheAssetsThemselves.code("app.js")
        rows = code[code.index("function entryRow(") : code.index("function playPlaylist(")]
        assert 'button("✕"' in rows
        assert "row-confirm" not in rows
        assert 'method: "DELETE"' in rows

    def test_a_playlist_can_be_renamed(self):
        """Also here because nothing else can do it — OwnTone's web UI cannot edit these files either.
        The server sanitises the name, so the answer's name is the one the next request must use."""
        body = self.source()
        assert '"row-rename"' in body
        assert 'method: "PATCH"' in body
        assert "state.openPlaylist = renamed.name" in body, "the row should follow its new name"

    def test_adding_a_track_to_a_playlist_lives_in_the_library_row(self):
        """Where the track is, rather than on the Playlists panel: the thing somebody is looking at
        when they want to file a track is the track."""
        code = TestTheAssetsThemselves.code("app.js")
        assert "function addToPlaylistControl(" in code
        assert "/tracks`, {" in code
        body = code[code.index("function addToPlaylistControl(") :]
        assert 'picker.setAttribute("aria-label"' in body, "a bare <select> has no accessible name"

    def test_the_picker_offers_existing_playlists_only(self):
        """No free-text "new playlist" field in a library row: creating one lives on the Playlists
        panel, and two ways to create would be two names for the same thing."""
        code = TestTheAssetsThemselves.code("app.js")
        body = code[code.index("function addToPlaylistControl(") :]
        assert "state.playlists" in body
        assert "No playlists yet" in body, "an empty set should say so rather than offer nothing"

    def test_the_row_offers_play_and_queue(self):
        body = self.source()
        assert 'button("Play"' in body and 'button("Queue"' in body
        assert '"replace", true' in body and '"append", false' in body

    def test_the_confirmation_says_that_no_music_is_removed(self):
        """The one way this delete must *not* read like the library's: a playlist is a list, and the
        tracks it named are all still there afterwards."""
        body = self.source()
        warn = body[body.index('"row-warn"') : body.index("const actions")]
        assert "${playlist.name}" in warn
        assert "No music is removed" in warn


class TestTheAddToPlaylistControlIsBuiltCorrectly:
    """``addToPlaylistControl`` run for real under deno, against a DOM just big enough to hold it.

    Behavioural rather than a reading of the source, for one reason: this control is built *lazily*,
    when a library row's ``⋯`` is first opened, so none of the DOM-level assertions elsewhere in this
    file ever see it. The two things worth proving are that the dropdown is populated from the cached
    names rather than from a fetch, and that an empty set produces a sentence instead of an empty
    dropdown with an Add button that could only fail.
    """

    FUNCTIONS = ("make", "button", "addToPlaylistControl")

    HARNESS = r"""
class FakeNode {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.className = "";
    this.textContent = "";
    this.disabled = false;
  }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener() {}
  *walk() { for (const c of this.children) { yield c; yield* c.walk(); } }
}

const document = { createElement: (tag) => new FakeNode(tag) };
const state = { playlists: [] };
const el = () => new FakeNode("p");
const api = () => {};
const attempt = () => {};

__FUNCTIONS__

const describe = (box) => ({
  className: box.className,
  tags: [...box.walk()].map((n) => n.tagName),
  options: [...box.walk()].filter((n) => n.tagName === "OPTION").map((n) => n.textContent),
  selectLabelled: [...box.walk()].some(
    (n) => n.tagName === "SELECT" && n.attributes["aria-label"]
  ),
  buttons: [...box.walk()].filter((n) => n.tagName === "BUTTON").map((n) => n.textContent),
  text: [...box.walk()].filter((n) => n.tagName === "P").map((n) => n.textContent),
});

const result = {};
result.empty = describe(addToPlaylistControl({ title: "Autobahn", path: "a.m4a" }));
state.playlists = ["Road trip", "廣東歌"];
result.populated = describe(addToPlaylistControl({ title: "Autobahn", path: "a.m4a" }));
console.log(JSON.stringify(result));
"""

    @classmethod
    def run(cls) -> dict:
        deno = shutil.which("deno")
        if deno is None:  # pragma: no cover - deno is a declared requirement, but not here
            pytest.skip("deno is not installed")
        code = TestTheAssetsThemselves.code("app.js")
        sources = []
        for name in cls.FUNCTIONS:
            start = code.index(f"  function {name}(")
            sources.append(code[start : code.index("\n  }\n", start) + 4])
        script = cls.HARNESS.replace("__FUNCTIONS__", "\n".join(sources))
        completed = subprocess.run(
            [deno, "eval", script], capture_output=True, text=True, timeout=60
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    def test_the_dropdown_holds_every_cached_playlist(self):
        populated = self.run()["populated"]
        assert populated["options"] == ["Road trip", "廣東歌"]
        assert populated["buttons"] == ["Add to playlist"]

    def test_the_dropdown_carries_an_accessible_name(self):
        """A bare ``<select>`` beside a button called "Add to playlist" announces nothing about what
        it is choosing *for*, and this one sits inside a list of a hundred identical rows."""
        assert self.run()["populated"]["selectLabelled"] is True

    def test_no_playlists_yet_is_a_sentence_and_not_an_empty_dropdown(self):
        empty = self.run()["empty"]
        assert empty["options"] == [] and empty["buttons"] == []
        assert empty["text"] and "No playlists yet" in empty["text"][0]

    def test_it_is_marked_so_the_strip_only_builds_it_once(self):
        """``reveal()`` looks for ``.row-add`` before inserting: a strip closed and reopened must not
        stack a second dropdown."""
        assert self.run()["populated"]["className"] == "row-add"
        code = TestTheAssetsThemselves.code("app.js")
        assert 'panel.querySelector(".row-add")' in code


class TestTheLibraryPanelDoesNotGoStale:
    """The Library panel is re-read rather than cached once — the bug this class exists for.

    Found in use, not in a test: after an ingest reached **done** in the Ingests list, the Library
    panel still showed the library as it had been when the page was unlocked. The cause was a single
    condition, ``if (name === "library" && !el("library").childElementCount) loadLibrary()``, and the
    reason no assertion caught it is that both halves were individually reasonable — the guard avoided
    a directory walk per tab switch, and the panel *was* loaded once at unlock. Its worst case was the
    empty library, because ``fill()`` inserts an ``<li class="empty">`` placeholder that counts as a
    child: the panel said "The library is empty — add something first", you added something, and it
    went on saying so.

    Reproduced and then verified in headless Chrome against a live ``tunebox serve`` with a fake
    source: before the fix, two tracks on disk and "The library is empty" on screen through two tab
    switches; after it, the track appears while the Library tab is open and untouched.
    """

    @staticmethod
    def code() -> str:
        return TestTheAssetsThemselves.code("app.js")

    def test_selecting_the_tab_always_re_reads(self):
        code = self.code()
        assert 'if (name === "library") refreshLibrary();' in code
        assert "childElementCount" not in code, (
            "a listing drawn once is a listing that is wrong after the next ingest"
        )

    def test_a_finished_ingest_refreshes_it_without_a_tab_switch(self):
        """Somebody watching the ingest finish should not have to know that the other panel needs
        poking. ``refreshJobs`` already polls while anything is in flight, so this is free."""
        code = self.code()
        assert "noticeFinishedIngests(jobs)" in code
        body = code[code.index("function noticeFinishedIngests(") :]
        body = body[: body.index("\n  }")]
        assert 'job.state === "done"' in body
        assert "refreshLibrary()" in body

    def test_the_first_poll_is_not_treated_as_news(self):
        """Every past ingest is ``done`` on the first poll after unlocking. Reloading the library for
        those would be a second, pointless walk on every page load."""
        code = self.code()
        body = code[code.index("function noticeFinishedIngests(") :]
        body = body[: body.index("\n  }")]
        assert "if (previous === null) return" in body
        assert "finishedJobs: null" in code, "the sentinel has to start as null to be detectable"

    def test_a_refresh_will_not_rebuild_the_list_under_an_open_delete_confirmation(self):
        """The listing is newest-first, so an ingest landing mid-confirmation shifts every row down
        by one — and the tap that was about to confirm a delete would land on the row that replaced
        it. A deferred refresh costs a tab switch; a mis-aimed delete costs a file."""
        code = self.code()
        body = code[code.index("function refreshLibrary(") :]
        body = body[: body.index("\n  }")]
        assert '.row-confirm:not([hidden])' in body
        assert "return" in body

    def test_the_delete_path_redraws_directly(self):
        """``deleteTrack`` calls ``loadLibrary`` rather than ``refreshLibrary``: the confirmation
        panel that would block the guard is the one belonging to the row just deleted."""
        code = self.code()
        body = code[code.index("async function deleteTrack(") :]
        assert "await loadLibrary()" in body


class TestTheScriptsParse:
    """Syntax-check the JavaScript with the JS runtime this project already depends on.

    ``deno`` is not an optional extra here — ``tunebox doctor`` requires it, because yt-dlp-ejs needs
    a JS runtime or every download fails with HTTP 403 (PLAN.md 0.3). So using it to compile the two
    hand-written scripts costs no new dependency, and it closes the one gap a Python test suite
    otherwise leaves wide open: a typo in ``app.js`` is invisible to every test above and obvious to
    anybody holding a phone.

    ``new Function(source)`` *compiles* without executing, so this catches syntax errors without
    needing a DOM, a network or a service-worker scope.
    """

    CHECK = (
        "for (const p of Deno.args) {\n"
        "  new Function(await Deno.readTextFile(p));\n"
        "}\n"
    )

    @pytest.mark.parametrize("name", ["app.js", "sw.js"])
    def test_it_compiles(self, name: str):
        deno = shutil.which("deno")
        if deno is None:  # pragma: no cover - deno is a declared requirement, but not here
            pytest.skip("deno is not installed")
        completed = subprocess.run(
            [deno, "eval", "--allow-read", self.CHECK, str(ui.WEB_ROOT / name)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr


class TestTheMarkupAndTheScriptAgree:
    """Cross-check ids and ARIA references between ``index.html`` and ``app.js``.

    This is the class that would have caught the mistakes worth catching. A renamed id is not an
    error in either file on its own: the HTML is still valid, the script still parses, and
    ``document.getElementById`` simply returns ``null`` — so the button silently stops working and
    nothing anywhere says why. Likewise an ``aria-controls`` pointing at an id that no longer exists
    is invisible to everyone except the person using a screen reader.
    """

    @staticmethod
    def parse() -> tuple[set[str], list[tuple[str, str, str]]]:
        """Returns ``(ids, references)``, where a reference is (attribute, id, role of the source).

        The role travels with the reference because ``aria-controls`` means different things on a
        ``role="tab"`` and on a disclosure button, and the tablist assertions below are about the
        former only.
        """

        class Collector(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self.ids: set[str] = set()
                self.refs: list[tuple[str, str, str]] = []

            def handle_starttag(self, tag, attrs):
                values = dict(attrs)
                if values.get("id"):
                    self.ids.add(values["id"])
                role = values.get("role") or ""
                for attribute in ("aria-controls", "aria-labelledby", "for"):
                    target = values.get(attribute)
                    if target:
                        # `for` on a <label> and on an <output> both name element ids.
                        self.refs.append((attribute, target, role))

        collector = Collector()
        collector.feed((ui.WEB_ROOT / "index.html").read_text(encoding="utf-8"))
        return collector.ids, collector.refs

    def test_every_id_the_script_looks_up_exists(self):
        ids, _ = self.parse()
        script = (ui.WEB_ROOT / "app.js").read_text(encoding="utf-8")
        wanted = set(re.findall(r'\bel\("([^"]+)"\)', script))
        # The two template-literal lookups are `tab-${tab}` and `panel-${tab}`, over the tab names
        # declared in the same file — expanded here rather than parsed, because a regex that read
        # JavaScript control flow would be the least trustworthy thing in this suite.
        for tab in re.findall(r'const TABS = \[([^\]]+)\]', script)[0].replace('"', "").split(","):
            wanted |= {f"tab-{tab.strip()}", f"panel-{tab.strip()}"}
        assert wanted, "the script should look up its own elements"
        assert wanted <= ids, f"app.js looks up ids that are not in the markup: {wanted - ids}"

    def test_every_aria_reference_resolves(self):
        ids, refs = self.parse()
        assert refs, "the page should have labelled controls"
        dangling = {(attribute, target) for attribute, target, _ in refs if target not in ids}
        assert not dangling, dangling

    def test_the_tabs_and_panels_point_at_each_other(self):
        """A tablist where ``aria-controls`` and ``aria-labelledby`` disagree announces the wrong
        section, which is worse than announcing none.

        Scoped to ``role="tab"`` elements. ``aria-controls`` is also the correct attribute for a
        disclosure button elsewhere on the page — "Show token" is one — so asserting over every
        occurrence on the page would make adding one look like a tablist bug.
        """
        ids, refs = self.parse()
        controls = {target for attribute, target, role in refs if role == "tab"}
        assert controls == {"panel-now", "panel-add", "panel-library", "panel-playlists"}
        labelled = {target for attribute, target, _ in refs if attribute == "aria-labelledby"}
        assert labelled == {"tab-now", "tab-add", "tab-library", "tab-playlists"}

    def test_a_disclosure_button_controls_something_that_exists(self):
        """``aria-expanded`` without a resolvable ``aria-controls`` tells a screen reader that
        something expanded and gives it nowhere to look."""
        ids, refs = self.parse()
        disclosures = {
            target for attribute, target, role in refs if attribute == "aria-controls" and role != "tab"
        }
        assert "token-reveal" in disclosures, "the Show token button should control the reveal"
        assert disclosures <= ids



class TestAPollKeepsFocusOnTheQueue:
    """A poll must not take keyboard or screen-reader focus off a queue row.

    The rows became ``<button>``s so that a keyboard and VoiceOver could reach them — and every poll
    (every two seconds) redraws the queue. A rebuilt list destroys the focused node, a browser sends
    focus to ``<body>`` when that happens, and so the feature's own users were thrown back to the top
    of the page before they could press anything.

    This one is behavioural, not a reading of the source: the real ``renderQueue`` and the helpers it
    calls are lifted out of ``app.js`` and run under deno against a fake DOM just big enough to have
    a focused element and to lose it the way a browser does.
    """

    FUNCTIONS = ("clear", "make", "row", "fill", "queueRow", "renderQueue")

    HARNESS = r"""
class FakeNode {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.attributes = {};
    this.className = "";
    this.textContent = "";
  }
  get firstChild() { return this.children[0] || null; }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  removeChild(child) {
    this.children.splice(this.children.indexOf(child), 1);
    child.parentNode = null;
    // What a browser does: focus inside a removed subtree falls back to <body>.
    if (child.contains(document.activeElement)) document.activeElement = document.body;
    return child;
  }
  contains(node) { for (; node; node = node.parentNode) if (node === this) return true; return false; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener() {}
  focus() { document.activeElement = this; }
  *walk() { for (const c of this.children) { yield c; yield* c.walk(); } }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    const test = selector === "[data-item-id]"
      ? (n) => n.dataset.itemId !== undefined
      : (n) => n.className.split(" ").includes(selector.slice(1));
    return [...this.walk()].filter(test);
  }
}

const document = { body: new FakeNode("body"), createElement: (tag) => new FakeNode(tag) };
document.activeElement = document.body;
const nodes = { queue: new FakeNode("ul"), "queue-count": new FakeNode("span"), "btn-clear": new FakeNode("button") };
Object.values(nodes).forEach((n) => document.body.appendChild(n));
const el = (id) => nodes[id];
const state = { queueKey: null };
const playQueueItem = () => {};

__FUNCTIONS__

const item = (id) => ({ id, title: `Song ${id}`, artist: "A", album: "B", path: `/${id}.mp3` });
const rowFor = (id) => nodes.queue.querySelectorAll("[data-item-id]").find((n) => n.dataset.itemId === id);
const focusedId = () => document.activeElement.dataset.itemId || document.activeElement.tagName;

const result = {};
renderQueue({ item_id: "1", queue: [item("1"), item("2"), item("3")] });
const before = rowFor("2");
before.focus();

renderQueue({ item_id: "1", queue: [item("1"), item("2"), item("3")] });
result.unchanged = { focused: focusedId(), sameNode: document.activeElement === before };

renderQueue({ item_id: "3", queue: [item("2"), item("3")] });
result.changed = { focused: focusedId(), sameNode: document.activeElement === before };

renderQueue({ item_id: "3", queue: [item("3")] });
result.removed = { focused: focusedId() };

console.log(JSON.stringify(result));
"""

    @classmethod
    def run(cls) -> dict:
        deno = shutil.which("deno")
        if deno is None:  # pragma: no cover - deno is a declared requirement, but not here
            pytest.skip("deno is not installed")
        code = TestTheAssetsThemselves.code("app.js")
        sources = []
        for name in cls.FUNCTIONS:
            start = code.index(f"  function {name}(")
            sources.append(code[start : code.index("\n  }\n", start) + 4])
        script = cls.HARNESS.replace("__FUNCTIONS__", "\n".join(sources))
        completed = subprocess.run(
            [deno, "eval", script], capture_output=True, text=True, timeout=60
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    def test_an_unchanged_poll_leaves_the_focused_row_alone(self):
        """The same node, not merely the same id: moving focus at all makes a screen reader
        announce the row again, so VoiceOver would re-read "Play …" every two seconds."""
        assert self.run()["unchanged"] == {"focused": "2", "sameNode": True}

    def test_a_changed_queue_puts_focus_back_on_the_same_track(self):
        """The row above it went away and the current track moved, so the list really was rebuilt
        — and focus followed the item id to its new button rather than falling to ``<body>``."""
        assert self.run()["changed"] == {"focused": "2", "sameNode": False}

    def test_a_row_that_is_gone_takes_its_focus_with_it(self):
        """Nothing to restore to, and nothing else is guessed at: focus is where a browser puts it."""
        assert self.run()["removed"] == {"focused": "BODY"}

    def test_every_playable_row_carries_its_id(self):
        code = TestTheAssetsThemselves.code("app.js")
        body = code[code.index("function queueRow(") : code.index("function playQueueItem(")]
        assert "main.dataset.itemId = item.id" in body
