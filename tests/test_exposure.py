"""``tunebox exposure`` — PLAN.md Phase 5's first item.

The interesting thing to test here is not the parsing, it is the **three-valued answer**. A check
that says "fine" when it means "I could not tell" is worse than no check at all, and every test in
:class:`TestTheAudit` is about keeping those two apart: a router that never answers is ``unknown``, a
router that answers with a rule pointing at this host is ``fail``, and only a router that
affirmatively reports an empty mapping table is ``ok``.

The socket-table parsers are tested against **real captured output** from both platforms — the macOS
lines come from the machine this was developed on and the ``ss`` lines from a Debian host — because
the one thing a hand-written sample cannot capture is the column that shifts.

There is also a test that looks odd until you think about what this tool does: it asserts that the
audit never invokes ``AddPortMapping`` or ``DeletePortMapping``. An audit that "helpfully" removed a
forwarding rule would be changing shared household infrastructure that might be somebody's console
or a colleague's VPN, and no exception in the transport can ever be allowed to make that happen.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tunebox.config import Config
from tunebox.ops import exposure
from tunebox.ops.report import FAIL, OK, UNKNOWN, WARN

# Real ``lsof -nP -iTCP -sTCP:LISTEN`` output, trimmed. Note the two shapes that break naive
# parsers: an IPv6 address in brackets, and the wildcard spelled ``*``.
LSOF = """\
COMMAND     PID     USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME
launchd       1     root    7u  IPv6 0x3f2a1b4c      0t0  TCP *:22 (LISTEN)
rapportd    652 robinlin    9u  IPv4 0x8c1d2e3f      0t0  TCP *:56861 (LISTEN)
Python    43917 robinlin    8u  IPv4 0x1a2b3c4d      0t0  TCP 192.168.50.158:8080 (LISTEN)
owntone   44012 robinlin   12u  IPv4 0x5e6f7a8b      0t0  TCP *:3689 (LISTEN)
ControlCe   712 robinlin   22u  IPv6 0x9a8b7c6d      0t0  TCP [::1]:7000 (LISTEN)
"""

# Real ``ss -Hltnp`` output from a Debian host (the Raspberry Pi shape).
SS = """\
LISTEN 0      4096   192.168.50.99:8080      0.0.0.0:*    users:(("python3",pid=812,fd=9))
LISTEN 0      128          0.0.0.0:22        0.0.0.0:*    users:(("sshd",pid=611,fd=3))
LISTEN 0      1024               *:3689            *:*    users:(("owntone",pid=904,fd=12))
LISTEN 0      4096            [::]:5353          [::]:*   users:(("avahi-daemon",pid=498,fd=13))
"""

#: A stand-in for "some address out on the internet". Deliberately not this household's real WAN
#: address, which has no business being in a git history — and deliberately *not* RFC 5737's
#: documentation range either: Python's :mod:`ipaddress` reports 203.0.113.0/24 as private, so it
#: would exercise the wrong branch of :func:`~tunebox.ops.exposure.classify_address`.
PUBLIC_IP = "93.184.216.34"

IGD_DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>
    <friendlyName>RT-AX58U-6A00</friendlyName>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:Layer3Forwarding:1</serviceType>
        <controlURL>/ctl/L3F</controlURL>
      </service>
    </serviceList>
    <deviceList>
      <device>
        <deviceType>urn:schemas-upnp-org:device:WANDevice:1</deviceType>
        <deviceList>
          <device>
            <deviceType>urn:schemas-upnp-org:device:WANConnectionDevice:1</deviceType>
            <serviceList>
              <service>
                <serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
                <controlURL>/ctl/IPConn</controlURL>
              </service>
            </serviceList>
          </device>
        </deviceList>
      </device>
    </deviceList>
  </device>
</root>
"""

FAULT = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
 <s:Body><s:Fault>
  <faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring>
  <detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">
   <errorCode>713</errorCode><errorDescription>SpecifiedArrayIndexInvalid</errorDescription>
  </UPnPError></detail>
 </s:Fault></s:Body>
</s:Envelope>
"""


def config(tmp_path: Path, **kwargs) -> Config:
    return Config(music_root=tmp_path / "music", api_host="192.168.50.158", **kwargs)


class FakeRouter:
    """A UPnP gateway that answers exactly what a test tells it to, and records every action."""

    def __init__(
        self,
        *,
        mappings: list[dict[str, str]] | None = None,
        external_ip: str = PUBLIC_IP,
        silent: bool = False,
        description: str = IGD_DESCRIPTION,
    ) -> None:
        self.mappings = mappings or []
        self.external_ip = external_ip
        self.silent = silent
        self.description = description
        self.actions: list[str] = []

    def discover(self) -> list[str]:
        return [] if self.silent else ["http://192.168.50.1:52881/rootDesc.xml"]

    def get(self, url: str) -> str:
        return self.description

    def soap(self, control_url, service_type, action, arguments):
        self.actions.append(action)
        if action == "GetExternalIPAddress":
            return {"NewExternalIPAddress": self.external_ip}
        if action == "GetGenericPortMappingEntry":
            index = int(arguments["NewPortMappingIndex"])
            if index >= len(self.mappings):
                raise exposure.UpnpError("UPnP error 713: SpecifiedArrayIndexInvalid", code=713)
            return self.mappings[index]
        raise AssertionError(f"the audit invoked {action!r}, which it must never do")


def mapping(external: int, client: str, internal: int, description: str = "") -> dict[str, str]:
    return {
        "NewRemoteHost": "",
        "NewExternalPort": str(external),
        "NewProtocol": "tcp",
        "NewInternalPort": str(internal),
        "NewInternalClient": client,
        "NewEnabled": "1",
        "NewPortMappingDescription": description,
        "NewLeaseDuration": "0",
    }


def runner_for(output: str, *, returncode: int = 0):
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        return returncode, output

    run.calls = calls  # type: ignore[attr-defined]
    return run


# --------------------------------------------------------------------- address arithmetic


class TestClassifyAddress:
    @pytest.mark.parametrize(
        "host,kind",
        [
            ("0.0.0.0", "wildcard"),
            ("", "wildcard"),
            ("::", "wildcard"),
            ("*", "wildcard"),
            ("0", "wildcard"),
            ("127.0.0.1", "loopback"),
            ("::1", "loopback"),
            ("192.168.50.158", "private"),
            ("10.1.2.3", "private"),
            ("172.16.9.9", "private"),
            ("169.254.1.1", "private"),
            (PUBLIC_IP, "public"),
            ("8.8.8.8", "public"),
            ("tunebox.local", "hostname"),
        ],
    )
    def test_it_separates_the_five_cases(self, host, kind):
        assert exposure.classify_address(host)[0] == kind

    def test_a_name_is_never_resolved(self):
        """An audit that did a DNS lookup would depend on the very infrastructure it is auditing —
        and on a host where resolution is broken it would report a *different* problem than the
        real one."""
        kind, note = exposure.classify_address("some-host.lan")
        assert kind == "hostname"
        assert "not resolved" in note


# ------------------------------------------------------------------------ socket table


class TestSocketTable:
    def test_lsof_output_is_parsed(self):
        table = exposure.read_listeners(platform="darwin", run=runner_for(LSOF))
        by_port = {listener.port: listener for listener in table.listeners}
        assert by_port[8080].address == "192.168.50.158"
        assert by_port[8080].process == "Python"
        assert by_port[8080].wildcard is False
        assert by_port[22].wildcard is True
        assert by_port[7000].address == "::1"
        assert by_port[7000].kind == "loopback"

    def test_ss_output_is_parsed(self):
        table = exposure.read_listeners(platform="linux", run=runner_for(SS))
        by_port = {listener.port: listener for listener in table.listeners}
        assert by_port[8080].address == "192.168.50.99"
        assert by_port[8080].process == "python3"
        assert by_port[22].wildcard is True
        assert by_port[3689].wildcard is True
        assert by_port[5353].address == "::"

    def test_it_falls_back_when_the_first_tool_is_missing(self):
        """``lsof`` is not installed on a minimal Debian and ``ss`` is not on macOS. Exit 127 has to
        mean "try the next one", not "nothing is listening" — which would read as a clean audit."""
        attempts: list[list[str]] = []

        def run(argv):
            attempts.append(list(argv))
            if argv[0] == "ss":
                return 127, ""
            return 0, "tcp  0 0 0.0.0.0:8080  0.0.0.0:*  LISTEN\n"

        table = exposure.read_listeners(platform="linux", run=run)
        assert [argv[0] for argv in attempts] == ["ss", "netstat"]
        assert table.listeners[0].port == 8080

    def test_no_tool_at_all_is_unknown_not_empty(self):
        table = exposure.read_listeners(platform="linux", run=lambda argv: (127, ""))
        assert table.listeners == ()
        assert table.error is not None


# -------------------------------------------------------------------------- the audit


class TestTheAudit:
    def find(self, report, name):
        for finding in report.findings:
            if finding.name == name:
                return finding
        raise AssertionError(f"no {name!r} finding in {[f.name for f in report.findings]}")

    def test_a_lan_bind_with_a_quiet_router_is_ok_but_not_silent(self, tmp_path: Path):
        report = exposure.audit(
            config(tmp_path),
            probe=FakeRouter(silent=True),
            platform="darwin",
            run=runner_for(LSOF),
        )
        assert report.ok
        assert self.find(report, "configured bind").status == OK
        assert self.find(report, "tunebox listener").status == OK
        # The whole point: no UPnP answer is *not* a pass.
        router = self.find(report, "router (UPnP)")
        assert router.status == UNKNOWN
        assert "not proof" in router.detail
        assert report.manual_checks, "the unanswerable half must always be printed"

    def test_a_wildcard_bind_fails(self, tmp_path: Path):
        report = exposure.audit(
            Config(music_root=tmp_path, api_host="0.0.0.0", allow_all_interfaces=True),
            probe=FakeRouter(silent=True),
            platform="darwin",
            run=runner_for(LSOF),
        )
        assert not report.ok
        assert self.find(report, "configured bind").status == FAIL

    def test_a_public_bind_fails(self, tmp_path: Path):
        report = exposure.audit(
            Config(music_root=tmp_path, api_host=PUBLIC_IP),
            probe=FakeRouter(silent=True),
            platform="darwin",
            run=runner_for(LSOF),
        )
        assert self.find(report, "configured bind").status == FAIL
        assert not report.ok

    def test_a_listener_on_every_interface_fails_even_when_the_config_is_right(
        self, tmp_path: Path
    ):
        """The config and the kernel can disagree — someone started the service by hand with
        ``--host 0.0.0.0``, or an old process is still running. The socket table wins."""
        wildcard = LSOF.replace("192.168.50.158:8080", "*:8080")
        report = exposure.audit(
            config(tmp_path),
            probe=FakeRouter(silent=True),
            platform="darwin",
            run=runner_for(wildcard),
        )
        assert self.find(report, "configured bind").status == OK
        assert self.find(report, "tunebox listener").status == FAIL
        assert not report.ok

    def test_a_service_that_is_not_running_is_unknown(self, tmp_path: Path):
        without = "\n".join(
            line for line in LSOF.splitlines() if "8080" not in line
        )
        report = exposure.audit(
            config(tmp_path),
            probe=FakeRouter(silent=True),
            platform="darwin",
            run=runner_for(without),
        )
        finding = self.find(report, "tunebox listener")
        assert finding.status == UNKNOWN
        assert report.ok, "not running is not a security failure"

    def test_the_player_daemon_is_a_warning_not_a_failure(self, tmp_path: Path):
        """OwnTone binds every interface and cannot be told not to. PLAN.md 3.2 measured that its
        real problem is authorisation (``trusted_networks``), not the socket — so this must not fail
        an audit whose subject is internet exposure."""
        report = exposure.audit(
            config(tmp_path),
            probe=FakeRouter(silent=True),
            platform="darwin",
            run=runner_for(LSOF),
        )
        finding = self.find(report, "player listener")
        assert finding.status == WARN
        assert "owntone-setup.sh" in (finding.fix or "")
        assert report.ok

    def test_a_forwarded_port_to_this_host_fails(self, tmp_path: Path):
        router = FakeRouter(mappings=[mapping(8080, "192.168.50.158", 8080, "tunebox")])
        report = exposure.audit(
            config(tmp_path), probe=router, platform="darwin", run=runner_for(LSOF)
        )
        finding = self.find(report, "port forwarding")
        assert finding.status == FAIL
        assert "8080" in finding.detail
        assert not report.ok

    def test_a_forwarded_port_to_this_hosts_ports_fails_even_from_another_address(
        self, tmp_path: Path
    ):
        """A rule can name an address this host used to have, or a hostname-based reservation. The
        port is the other half of the match, and matching either is what makes this useful."""
        router = FakeRouter(mappings=[mapping(9000, "192.168.50.77", 8080, "old reservation")])
        report = exposure.audit(
            config(tmp_path), probe=router, platform="darwin", run=runner_for(LSOF)
        )
        assert self.find(report, "port forwarding").status == FAIL

    def test_forwarding_elsewhere_is_a_warning(self, tmp_path: Path):
        router = FakeRouter(mappings=[mapping(8567, "192.168.50.83", 8567, "set-top box")])
        report = exposure.audit(
            config(tmp_path), probe=router, platform="darwin", run=runner_for(LSOF)
        )
        finding = self.find(report, "port forwarding")
        assert finding.status == WARN
        assert report.ok

    def test_an_empty_mapping_table_is_the_only_ok(self, tmp_path: Path):
        router = FakeRouter(mappings=[])
        report = exposure.audit(
            config(tmp_path), probe=router, platform="darwin", run=runner_for(LSOF)
        )
        assert self.find(report, "port forwarding").status == OK
        # UPnP answering at all is still a warning: with it on, anything on the LAN can forward a
        # port unattended.
        assert self.find(report, "router (UPnP)").status == WARN

    def test_no_nat_at_all_fails(self, tmp_path: Path):
        """A host whose own address is the WAN address is on the internet whether or not a port is
        forwarded. Rare at home, and catastrophic for this service."""
        public = Config(music_root=tmp_path, api_host=PUBLIC_IP)
        report = exposure.audit(
            public,
            probe=FakeRouter(external_ip=PUBLIC_IP),
            platform="darwin",
            run=runner_for(LSOF),
        )
        finding = self.find(report, "NAT")
        assert finding.status == FAIL
        assert not report.ok

    def test_it_never_changes_the_router(self, tmp_path: Path):
        """The safety property. ``FakeRouter.soap`` raises for any action other than the two reads,
        so this fails loudly if a future change adds a "helpful" fix."""
        router = FakeRouter(mappings=[mapping(8080, "192.168.50.158", 8080)])
        exposure.audit(config(tmp_path), probe=router, platform="darwin", run=runner_for(LSOF))
        assert set(router.actions) <= {"GetGenericPortMappingEntry", "GetExternalIPAddress"}
        assert "AddPortMapping" not in router.actions
        assert "DeletePortMapping" not in router.actions

    def test_no_router_flag_asks_nothing(self, tmp_path: Path):
        router = FakeRouter()
        report = exposure.audit(
            config(tmp_path),
            probe=router,
            check_router=False,
            platform="darwin",
            run=runner_for(LSOF),
        )
        assert router.actions == []
        assert self.find(report, "router").status == UNKNOWN


# ------------------------------------------------------------------------- UPnP details


class TestUpnp:
    def test_a_relative_control_url_is_resolved_against_the_location(self):
        """The common real shape: ``controlURL`` is ``/ctl/IPConn`` and the document came from a
        port that is not 80. Joining these wrongly makes every probe silently fail."""
        service = exposure.find_wan_service(
            IGD_DESCRIPTION, "http://192.168.50.1:52881/rootDesc.xml"
        )
        assert service is not None
        assert service.control_url == "http://192.168.50.1:52881/ctl/IPConn"
        assert service.service_type.endswith("WANIPConnection:1")
        assert service.device == "RT-AX58U-6A00"

    def test_url_base_wins_when_present(self):
        document = IGD_DESCRIPTION.replace(
            "<device>", "<URLBase>http://192.168.1.1:5000/</URLBase><device>", 1
        )
        service = exposure.find_wan_service(document, "http://192.168.1.1:52881/desc.xml")
        assert service is not None
        assert service.control_url == "http://192.168.1.1:5000/ctl/IPConn"

    def test_a_device_with_no_wan_service_is_none(self):
        assert exposure.find_wan_service("<root><device/></root>", "http://x/") is None

    def test_junk_is_not_a_crash(self):
        assert exposure.find_wan_service("<not xml", "http://x/") is None

    def test_a_fault_carries_its_error_code(self):
        """713 is how a router says "that was the last mapping". Losing the code would turn the end
        of the list into an error and every router into a broken one."""
        error = exposure._fault(FAULT, fallback="unused")
        assert error.code == 713
        assert "SpecifiedArrayIndexInvalid" in str(error)

    def test_enumeration_stops_at_the_end_of_the_table(self):
        router = FakeRouter(
            mappings=[mapping(1, "192.168.50.10", 1), mapping(2, "192.168.50.11", 2)]
        )
        service = exposure.find_wan_service(IGD_DESCRIPTION, "http://192.168.50.1/d.xml")
        assert service is not None
        found = exposure.list_port_mappings(router, service)
        assert [entry.external_port for entry in found] == [1, 2]
        # Three calls: two that answered and the one that faulted.
        assert router.actions.count("GetGenericPortMappingEntry") == 3

    def test_enumeration_is_bounded(self):
        """A router that never signals the end must not loop forever inside an audit."""

        class Endless(FakeRouter):
            def soap(self, control_url, service_type, action, arguments):
                self.actions.append(action)
                return mapping(1, "192.168.50.10", 1)

        service = exposure.find_wan_service(IGD_DESCRIPTION, "http://192.168.50.1/d.xml")
        assert service is not None
        found = exposure.list_port_mappings(Endless(), service)
        assert len(found) == exposure.MAX_MAPPINGS

    def test_a_soap_response_is_parsed(self):
        body = """<?xml version="1.0"?>
        <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>
        <u:GetExternalIPAddressResponse xmlns:u="urn:schemas-upnp-org:service:WANIPConnection:1">
        <NewExternalIPAddress>203.0.113.9</NewExternalIPAddress>
        </u:GetExternalIPAddressResponse></s:Body></s:Envelope>"""
        assert exposure._parse_soap_response(body) == {"NewExternalIPAddress": "203.0.113.9"}

    def test_the_envelope_escapes_its_arguments(self):
        """The index is ours, but the escaping has to be right anyway: this is the one place tunebox
        generates XML, and an unescaped ``&`` would produce a request the router rejects."""
        body = exposure._soap_envelope("urn:svc", "Act", {"A": "1 & 2 <3>"}).decode()
        assert "1 &amp; 2 &lt;3&gt;" in body
        assert 'SOAPAction' not in body  # that is a header, not part of the body


# ---------------------------------------------------------------------------- the CLI


class TestExposureCommand:
    def test_it_prints_json_and_exits_zero_when_nothing_is_forwarded(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        from tunebox.cli import main

        monkeypatch.setenv("TUNEBOX_MUSIC_ROOT", str(tmp_path / "music"))
        monkeypatch.setenv("TUNEBOX_API_HOST", "192.168.50.158")
        monkeypatch.setattr(
            exposure, "read_listeners", lambda **kwargs: exposure.ListenerTable((), command="fake")
        )
        monkeypatch.setattr(exposure, "SsdpRouterProbe", lambda **kwargs: FakeRouter(mappings=[]))

        assert main(["exposure", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert {finding["name"] for finding in payload["findings"]} >= {
            "configured bind",
            "port forwarding",
        }

    def test_it_exits_one_when_a_port_is_forwarded_here(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        from tunebox.cli import main

        monkeypatch.setenv("TUNEBOX_MUSIC_ROOT", str(tmp_path / "music"))
        monkeypatch.setenv("TUNEBOX_API_HOST", "192.168.50.158")
        monkeypatch.setattr(
            exposure, "read_listeners", lambda **kwargs: exposure.ListenerTable((), command="fake")
        )
        monkeypatch.setattr(
            exposure,
            "SsdpRouterProbe",
            lambda **kwargs: FakeRouter(mappings=[mapping(8080, "192.168.50.158", 8080)]),
        )
        assert main(["exposure"]) == 1
        out = capsys.readouterr().out
        assert "port forwarding" in out
        assert "Still to check by hand" in out
