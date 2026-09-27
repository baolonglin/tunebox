"""Handing the token to a phone — :mod:`tunebox.pairing` and ``tunebox pair``.

Two properties carry the weight here, and neither is about QR codes.

The first is that the token stays in the URL **fragment**. That is the reason it never reaches
uvicorn's access log (PLAN.md 3.2), and it is a one-character difference from the query-string form
that would silently undo it — so it is asserted directly rather than trusted to review.

The second is that this output is the credential, so it must not be printable into a pipe by
accident. ``deploy/install.sh`` has guarded its link with ``[ -t 1 ]`` from the start; the command
has to hold the same line or the guard is decorative.
"""

from __future__ import annotations

import pytest

from tunebox import pairing
from tunebox.cli import main
from tunebox.config import Config
from tunebox.errors import ConfigError

#: 43 characters, the shape ``secrets.token_urlsafe(32)`` produces and what the installer writes.
TOKEN = "3al9Z0x2b_8Gbnkp3slYwFrc1q4zHYNxFUBnuXqW8Sg"
HOST = "192.168.50.158"


def config(**overrides) -> Config:
    base = {"api_host": HOST, "api_port": 8080, "api_token": TOKEN}
    return Config(**{**base, **overrides})


class TestThePairingUrl:
    def test_the_token_is_in_the_fragment_not_the_query_string(self):
        """The whole reason the link is safe to show: a fragment is never sent to the server, so it
        cannot appear in the access log. ``?token=`` would be logged on every single request."""
        url = pairing.pair_url(config()).url
        assert url == f"http://{HOST}:8080/#token={TOKEN}"
        assert "?" not in url
        assert url.index("#") < url.index("token=")

    def test_it_refuses_a_config_with_no_token(self):
        """Same check ``serve`` makes, so a link this command will print is a link the service will
        honour. Printing a pairing URL for a service that would refuse to boot is worse than an
        error."""
        with pytest.raises(ConfigError, match="TUNEBOX_TOKEN is not set"):
            pairing.pair_url(config(api_token=None))

    def test_it_refuses_a_short_token(self):
        with pytest.raises(ConfigError, match="too short"):
            pairing.pair_url(config(api_token="abc"))

    def test_it_refuses_every_interface_even_when_serve_would_allow_it(self):
        """``TUNEBOX_ALLOW_ALL_INTERFACES`` lets the service bind 0.0.0.0 inside a container. There
        is still no address there to put in a URL, and ``http://0.0.0.0:8080`` on a phone is a
        confusing failure rather than an obvious one."""
        with pytest.raises(pairing.PairingError, match="every interface"):
            pairing.pair_url(config(api_host="0.0.0.0", allow_all_interfaces=True))

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
    def test_a_loopback_bind_warns_instead_of_failing(self, host: str):
        """Loopback is the *default*, so this is the state a developer pairing from a fresh checkout
        is actually in. It is a real link — it works in a browser on this machine — so it is a
        warning, not an error; but a phone cannot reach it and nothing else would say so."""
        pair = pairing.pair_url(config(api_host=host))
        assert pair.url.startswith(f"http://{host}:8080/#token=")
        assert pair.warnings
        assert "cannot reach it" in pair.warnings[0]

    def test_a_token_with_url_metacharacters_is_escaped(self):
        """``token_urlsafe`` never emits one, but ``TUNEBOX_TOKEN`` can be set by hand, and a token
        that arrives at the browser altered locks the phone out with a 401 and no explanation."""
        url = pairing.pair_url(config(api_token="a/b+c=d&e" + "x" * 8)).url
        assert "&" not in url.split("#token=")[1]
        assert "%2F" in url and "%2B" in url


class TestTheQrCode:
    def test_it_encodes_the_pairing_url(self):
        """Verified against an independent decoder (zxing-cpp) during development: the symbol reads
        back as the exact fragment URL. Here the cheap invariant is asserted — a symbol was drawn,
        and it was drawn from the URL that :func:`pair_url` built."""
        url = pairing.pair_url(config()).url
        assert pairing.qr(url).strip()
        # Same payload, same symbol; a different payload, a different one. Guards against the QR
        # being built from something other than what is printed underneath it.
        assert pairing.qr(url) == pairing.qr(url)
        assert pairing.qr(url) != pairing.qr(url.replace("#token=", "#token=z"))

    def test_it_fits_in_a_terminal(self):
        """The reason ``_ERROR_LEVEL`` is ``m`` and ``compact=True`` is set. Raising the error level
        to segno's default, or dropping compact, makes the symbol wrap at 80 columns — and a wrapped
        QR code is not a QR code. This test is the only thing that would notice."""
        lines = pairing.qr(pairing.pair_url(config()).url).splitlines()
        assert len(lines) <= 30, "too tall for a terminal window"
        assert max(len(line) for line in lines) <= 80, "wraps at 80 columns and becomes unscannable"

    def test_the_rendered_block_shows_the_link_as_well(self):
        """A terminal font that renders half-blocks badly, or a camera that will not focus, must not
        be a dead end."""
        text = pairing.render(config())
        assert f"http://{HOST}:8080/#token={TOKEN}" in text
        assert "Add to Home Screen" in text

    def test_no_qr_still_gives_the_link(self):
        text = pairing.render(config(), with_qr=False)
        assert f"http://{HOST}:8080/#token={TOKEN}" in text
        assert "█" not in text

    def test_the_loopback_warning_reaches_the_rendered_output(self):
        assert "warning:" in pairing.render(config(api_host="127.0.0.1"), with_qr=False)


class TestTheCommand:
    """``tunebox pair``. ``capsys`` makes stdout a non-tty, which is exactly the case the guard is
    for, so the refusal is the *default* in these tests and ``--force`` is the deviation."""

    @pytest.fixture(autouse=True)
    def env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TUNEBOX_TOKEN", TOKEN)
        monkeypatch.setenv("TUNEBOX_API_HOST", HOST)
        monkeypatch.setenv("TUNEBOX_API_PORT", "8080")
        monkeypatch.setenv("TUNEBOX_MUSIC_ROOT", str(tmp_path))

    def test_it_refuses_to_print_into_a_pipe(self, capsys):
        """Redirecting a command into a log file is a normal thing to do, and this output is the
        shared secret. Refusing is recoverable; a token sitting in a log nobody reads is not."""
        code = main(["pair"])
        captured = capsys.readouterr()
        assert code == 1
        assert captured.out == ""
        assert "not a terminal" in captured.err

    def test_the_refusal_does_not_leak_the_token_it_declined_to_print(self, capsys):
        main(["pair"])
        captured = capsys.readouterr()
        assert TOKEN not in captured.out
        assert TOKEN not in captured.err

    def test_force_prints_the_qr_and_the_link(self, capsys):
        code = main(["pair", "--force"])
        out = capsys.readouterr().out
        assert code == 0
        assert "█" in out, "no QR code was drawn"
        assert f"#token={TOKEN}" in out

    def test_no_qr_prints_only_the_link(self, capsys):
        code = main(["pair", "--no-qr", "--force"])
        out = capsys.readouterr().out
        assert code == 0
        assert "█" not in out
        assert f"#token={TOKEN}" in out

    def test_a_tty_needs_no_force(self, capsys, monkeypatch):
        monkeypatch.setattr("sys.stdout.isatty", lambda: True)
        assert main(["pair"]) == 0
        assert f"#token={TOKEN}" in capsys.readouterr().out

    def test_a_missing_token_is_a_clean_error_not_a_traceback(self, capsys, monkeypatch):
        monkeypatch.delenv("TUNEBOX_TOKEN")
        code = main(["pair", "--force"])
        assert code == 1
        assert "TUNEBOX_TOKEN is not set" in capsys.readouterr().err

    def test_it_is_in_the_help(self):
        from tunebox.cli import build_parser

        with pytest.raises(SystemExit):
            build_parser().parse_args(["pair", "--nonsense"])
        assert build_parser().parse_args(["pair"]).func is not None
