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
        assert controls == {"panel-now", "panel-add", "panel-library"}
        labelled = {target for attribute, target, _ in refs if attribute == "aria-labelledby"}
        assert labelled == {"tab-now", "tab-add", "tab-library"}

    def test_a_disclosure_button_controls_something_that_exists(self):
        """``aria-expanded`` without a resolvable ``aria-controls`` tells a screen reader that
        something expanded and gives it nowhere to look."""
        ids, refs = self.parse()
        disclosures = {
            target for attribute, target, role in refs if attribute == "aria-controls" and role != "tab"
        }
        assert "token-reveal" in disclosures, "the Show token button should control the reveal"
        assert disclosures <= ids

