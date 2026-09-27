"""Is anything reachable from outside the house?

PLAN.md Phase 5's first item is *"confirm nothing is bound to a public interface and no port is
forwarded"*, and Phase 4.5 got half of it: the installer reads the LAN address off the default route
and there is no code path in it that can produce a wildcard bind. What was left unverified is the
half that is not in this repository at all — the router. This module closes as much of that gap as
can honestly be closed from a host on the LAN, and is explicit about the rest.

Three questions, three different kinds of answer:

1. **What did we configure?** Cheap, certain, and already enforced at startup by
   :meth:`tunebox.config.Config.validate_for_serve`. Re-checked here so one command answers the
   whole question rather than leaving the operator to correlate two.

2. **What is actually listening, and on which addresses?** The kernel's socket table, not our
   intent. This is where a second daemon nobody remembers installing shows up — OwnTone, for
   instance, binds every interface by default, which is a real finding and the reason
   ``deploy/owntone-setup.sh`` sets ``trusted_networks = { "localhost" }`` and a password.

3. **Does the router forward anything to us?** Asked over UPnP IGD, which is the only way a host on
   the LAN can inspect its own NAT. The answer is genuinely three-valued, and pretending otherwise
   would be the whole point missed: *mappings found* is a fact, *no mappings* is a fact, and *the
   router did not answer at all* is *not* proof of either — it is consistent with UPnP being
   disabled, which is what we want, and equally consistent with a router that simply ignores us.
   So that case reports ``unknown`` and says which page of the admin UI to look at.

**This module only ever reads.** It calls exactly two actions — ``GetGenericPortMappingEntry`` and
``GetExternalIPAddress`` — and nothing else — never ``AddPortMapping``, and, more to the point,
never ``DeletePortMapping``. Removing a forwarding rule is a change to shared household
infrastructure that might be somebody's game console or a colleague's VPN, and an audit tool that
silently rearranged the router would be far more dangerous than the thing it audits. It reports; a
human decides.

Every piece of I/O is behind a small injectable interface, so the tests drive the whole decision
tree — including a router that answers with forwarded ports — without a socket.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from ..config import Config, binds_every_interface
from .report import FAIL, OK, UNKNOWN, WARN, Finding, format_findings, worst

log = logging.getLogger(__name__)

#: The UPnP device and service types an IGD answers to. Both WAN connection service versions are
#: tried because routers ship one or the other and occasionally both, and PPP is still what a DSL
#: line looks like from the inside.
IGD_SEARCH_TARGETS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
)
WAN_SERVICE_TYPES = (
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)

SSDP_ADDRESS = ("239.255.255.250", 1900)

#: Stop enumerating port mappings here. A home router has a handful; a number this size means either
#: a genuinely alarming configuration or a router that never signals the end of the list, and
#: looping forever inside an audit is not an acceptable third option.
MAX_MAPPINGS = 128


class UpnpError(RuntimeError):
    """The router answered, but with a SOAP fault. Carries the UPnP error code when there is one."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- findings


@dataclass(frozen=True)
class ExposureReport:
    findings: tuple[Finding, ...]
    #: Printed at the end, always: the part of the question this tool cannot answer.
    manual_checks: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        return worst(finding.status for finding in self.findings)

    @property
    def ok(self) -> bool:
        """No failures. Warnings and unknowns do not make this false — see the module docstring:
        "the router did not answer" is the *expected* result on a router with UPnP off, and a check
        that failed for it would be turned off within a week."""
        return not any(f.status == FAIL for f in self.findings)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "findings": [f.as_dict() for f in self.findings],
            "manual_checks": list(self.manual_checks),
        }

    def render(self) -> str:
        return format_findings(self.findings)


# ------------------------------------------------------------------- address arithmetic


def classify_address(host: str) -> tuple[str, str]:
    """``(kind, note)`` for an address we might be listening on.

    ``kind`` is one of ``wildcard``, ``loopback``, ``private``, ``public`` or ``hostname``.
    Names are *not* resolved: this runs in an audit, and a DNS lookup would make the answer depend
    on a server that may be the very thing that is misconfigured.
    """
    value = (host or "").strip().strip("[]")
    if binds_every_interface(value):
        return "wildcard", "every interface, including the one facing the router"
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return "hostname", f"{value!r} is a name, not an address; not resolved here on purpose"
    if address.is_loopback:
        return "loopback", "this host only — not reachable from the LAN at all"
    if address.is_link_local:
        return "private", "link-local, so not routable off this segment"
    if address.is_private:
        return "private", "a LAN address, which is what PLAN.md 3.2 requires"
    return "public", "a globally routable address: packets from the internet can arrive here"


# ------------------------------------------------------------------- the socket table


@dataclass(frozen=True)
class Listener:
    address: str
    port: int
    process: str | None = None

    @property
    def wildcard(self) -> bool:
        return binds_every_interface(self.address)

    @property
    def kind(self) -> str:
        return classify_address(self.address)[0]

    def __str__(self) -> str:
        where = f"{self.address}:{self.port}"
        return f"{where} ({self.process})" if self.process else where


@dataclass(frozen=True)
class ListenerTable:
    listeners: tuple[Listener, ...] = ()
    command: str = ""
    #: Why the table is empty, when it is empty for a reason other than "nothing is listening".
    error: str | None = None


#: ``(argv) -> (returncode, stdout)``. Injected so the tests can feed real captured output from both
#: platforms without either tool having to exist on the machine running them.
CommandRunner = Callable[[Sequence[str]], "tuple[int, str]"]


def _run(argv: Sequence[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, never a shell string
            list(argv),
            capture_output=True,
            timeout=30,
            check=False,
            # Decoded permissively rather than with ``text=True``: a process name in the socket
            # table can contain any byte at all, and an audit must not die of one.
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return 127, ""
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("%s failed: %s", argv[0], exc)
        return 1, ""
    return completed.returncode, completed.stdout


#: The default runner, named so other modules can pass it explicitly rather than reaching for a
#: private one. :mod:`tunebox.ops.health` does exactly that.
DEFAULT_RUNNER: CommandRunner = _run


def read_listeners(
    *, platform: str | None = None, run: CommandRunner = _run
) -> ListenerTable:
    """Every TCP socket in the LISTEN state, from whichever tool this OS has.

    ``lsof`` on macOS and ``ss`` on Linux, with ``netstat`` as the last resort. Parsing a command's
    output is not elegant, but the alternative on both platforms is a C API, and the property that
    matters here is that this is the *kernel's* answer rather than ours: the whole point of the check
    is to catch a listener nobody configured.
    """
    system = platform or sys.platform
    if system == "darwin":
        attempts: tuple[tuple[tuple[str, ...], Callable[[str], list[Listener]]], ...] = (
            (("lsof", "-nP", "-iTCP", "-sTCP:LISTEN"), _parse_lsof),
            (("netstat", "-an", "-p", "tcp"), _parse_netstat),
        )
    else:
        attempts = (
            # -p wants privilege for *other* users' processes but never fails without it; the
            # addresses, which are what this check is about, are world-readable either way.
            (("ss", "-Hltnp"), _parse_ss),
            (("netstat", "-ltn"), _parse_netstat),
        )

    last_error: str | None = None
    for argv, parse in attempts:
        returncode, output = run(argv)
        if returncode == 127:
            last_error = f"{argv[0]} is not installed"
            continue
        if returncode != 0 and not output.strip():
            last_error = f"{' '.join(argv)} exited {returncode}"
            continue
        return ListenerTable(tuple(parse(output)), command=" ".join(argv))
    return ListenerTable((), error=last_error or "no way to read the socket table on this host")


def _split_host_port(text: str) -> tuple[str, int] | None:
    """``"192.168.1.2:8080"``, ``"[::]:3689"``, ``"*:22"``, ``"*.8080"`` -> ``(host, port)``.

    The ``*.8080`` spelling is macOS ``netstat``, which uses a dot as the separator *and* also uses
    dots inside IPv4 addresses, so the port is taken from the right-hand side in every case.
    """
    value = text.strip()
    if not value:
        return None
    separator = ":" if ":" in value else "."
    host, _, port = value.rpartition(separator)
    if not host:
        return None
    try:
        return host.strip().strip("[]") or "*", int(port)
    except ValueError:
        return None


def _parse_lsof(output: str) -> list[Listener]:
    found: list[Listener] = []
    for line in output.splitlines()[1:]:  # drop the header
        fields = line.split()
        if len(fields) < 9 or "LISTEN" not in line:
            continue
        # NAME is the second-to-last field once "(LISTEN)" is accounted for; taking it by position
        # from the right survives the optional columns lsof adds for some file types.
        name = fields[-2] if fields[-1] == "(LISTEN)" else fields[-1]
        parsed = _split_host_port(name)
        if parsed is None:
            continue
        found.append(Listener(address=parsed[0], port=parsed[1], process=fields[0]))
    return found


_SS_PROCESS = re.compile(r'"([^"]+)"')


def _parse_ss(output: str) -> list[Listener]:
    found: list[Listener] = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        if fields[0] not in ("LISTEN", "LISTEN-ONLY") and not line.startswith("LISTEN"):
            continue
        parsed = _split_host_port(fields[3])
        if parsed is None:
            continue
        match = _SS_PROCESS.search(line)
        found.append(
            Listener(address=parsed[0], port=parsed[1], process=match.group(1) if match else None)
        )
    return found


def _parse_netstat(output: str) -> list[Listener]:
    found: list[Listener] = []
    for line in output.splitlines():
        if "LISTEN" not in line:
            continue
        fields = line.split()
        if len(fields) < 4:
            continue
        # Column 3 on Linux, column 4 on macOS. Both are "the field before the peer address", so
        # the local address is found by looking for the first one that parses as host:port.
        for candidate in fields[3:5] + fields[1:4]:
            parsed = _split_host_port(candidate)
            if parsed is not None and parsed[1] > 0:
                found.append(Listener(address=parsed[0], port=parsed[1]))
                break
    return found


# ------------------------------------------------------------------------ the router


@dataclass(frozen=True)
class PortMapping:
    """One forwarding rule as the router describes it."""

    protocol: str
    external_port: int
    internal_client: str
    internal_port: int
    description: str = ""
    enabled: bool = True
    remote_host: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "external_port": self.external_port,
            "internal_client": self.internal_client,
            "internal_port": self.internal_port,
            "description": self.description,
            "enabled": self.enabled,
            "remote_host": self.remote_host,
        }

    def __str__(self) -> str:
        state = "" if self.enabled else " (disabled)"
        label = f" — {self.description}" if self.description else ""
        return (
            f"{self.protocol} *:{self.external_port} -> "
            f"{self.internal_client}:{self.internal_port}{state}{label}"
        )


@dataclass(frozen=True)
class WanService:
    """The service on the router that knows about NAT, and where to POST to it."""

    service_type: str
    control_url: str
    device: str = ""


class RouterProbe(Protocol):
    """The three I/O operations a UPnP audit needs. Faked wholesale in the tests."""

    def discover(self) -> Sequence[str]:
        """LOCATION URLs of any InternetGatewayDevice that answers SSDP."""

    def get(self, url: str) -> str:
        """Fetch a device description document."""

    def soap(
        self, control_url: str, service_type: str, action: str, arguments: Mapping[str, str]
    ) -> dict[str, str]:
        """Invoke one SOAP action, returning its output arguments. Raises :class:`UpnpError`."""


class SsdpRouterProbe:
    """The real one: SSDP multicast, then plain HTTP to whatever answered.

    Short timeouts throughout. This runs inside a health check that must finish, and a router that
    needs more than a couple of seconds to describe itself is a router this audit will report as
    ``unknown`` — which is the same conclusion it would reach after waiting thirty.
    """

    def __init__(self, *, timeout: float = 2.0, retries: int = 2) -> None:
        self.timeout = timeout
        self.retries = retries

    def discover(self) -> list[str]:
        locations: list[str] = []
        seen: set[str] = set()
        for target in IGD_SEARCH_TARGETS:
            for location in self._search(target):
                if location not in seen:
                    seen.add(location)
                    locations.append(location)
            if locations:
                break  # an IGD:1 answer is enough; asking for :2 as well only costs time
        return locations

    def _search(self, target: str) -> list[str]:
        request = (
            "M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {SSDP_ADDRESS[0]}:{SSDP_ADDRESS[1]}\r\n"
            'MAN: "ssdp:discover"\r\n'
            f"MX: {int(max(1, self.timeout))}\r\n"
            f"ST: {target}\r\n"
            "\r\n"
        ).encode("ascii")

        found: list[str] = []
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError as exc:  # pragma: no cover - a host with no IPv4 stack
            log.debug("cannot open an SSDP socket: %s", exc)
            return found
        with sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.settimeout(self.timeout)
            try:
                for _ in range(max(1, self.retries)):
                    # UDP multicast is lossy by design, so the request is sent more than once
                    # rather than concluding "no router" from one dropped datagram.
                    sock.sendto(request, SSDP_ADDRESS)
                while True:
                    try:
                        data, _ = sock.recvfrom(65535)
                    except socket.timeout:
                        break
                    location = _header(data.decode("utf-8", "replace"), "location")
                    if location:
                        found.append(location)
            except OSError as exc:
                log.debug("SSDP search failed: %s", exc)
        return found

    def get(self, url: str) -> str:
        request = urllib.request.Request(url, headers={"User-Agent": "tunebox/exposure"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
            return response.read(1_000_000).decode("utf-8", "replace")

    def soap(
        self, control_url: str, service_type: str, action: str, arguments: Mapping[str, str]
    ) -> dict[str, str]:
        body = _soap_envelope(service_type, action, arguments)
        request = urllib.request.Request(
            control_url,
            data=body,
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPAction": f'"{service_type}#{action}"',
                "User-Agent": "tunebox/exposure",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                return _parse_soap_response(response.read(1_000_000).decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            # A SOAP fault arrives as HTTP 500 with the interesting part in the body, so the body
            # is read rather than the status reported: "500" would lose the error code that
            # distinguishes "no such mapping index" (the end of the list) from a real failure.
            detail = exc.read(100_000).decode("utf-8", "replace") if exc.fp else ""
            raise _fault(detail, fallback=f"HTTP {exc.code} from {control_url}") from exc


def _header(text: str, name: str) -> str | None:
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().lower() == name:
            return value.strip()
    return None


def _soap_envelope(service_type: str, action: str, arguments: Mapping[str, str]) -> bytes:
    body = "".join(f"<{key}>{_escape(value)}</{key}>" for key, value in arguments.items())
    return (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{service_type}">{body}</u:{action}></s:Body>'
        "</s:Envelope>"
    ).encode("utf-8")


def _escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _localname(tag: str) -> str:
    return tag.rpartition("}")[2]


def _parse_soap_response(text: str) -> dict[str, str]:
    root = _parse_xml(text)
    fault = _find(root, "Fault")
    if fault is not None:
        raise _fault(text, fallback="the router returned a SOAP fault with no detail")
    body = _find(root, "Body")
    container = body if body is not None else root
    for child in container:
        # The single child of <Body> is <u:ActionResponse>; its children are the output arguments.
        return {_localname(item.tag): (item.text or "") for item in child}
    return {}


def _fault(text: str, *, fallback: str) -> UpnpError:
    """Turn a SOAP fault document into a :class:`UpnpError`, keeping the UPnP error code.

    The code is what makes enumeration terminate cleanly: 713 ``SpecifiedArrayIndexInvalid`` is how
    a well-behaved router says "that was the last mapping", and treating it as an error would report
    every router as broken.
    """
    try:
        root = _parse_xml(text)
    except ValueError:
        return UpnpError(fallback)
    code_element = _find(root, "errorCode")
    description = _find(root, "errorDescription")
    code = None
    if code_element is not None and (code_element.text or "").strip().isdigit():
        code = int((code_element.text or "").strip())
    message = (description.text or "").strip() if description is not None else ""
    if code is None and not message:
        return UpnpError(fallback)
    return UpnpError(f"UPnP error {code}: {message or 'no description'}", code=code)


def _parse_xml(text: str) -> ET.Element:
    try:
        # ElementTree's parser does not expand external entities and has no DTD retrieval, so the
        # billion-laughs and XXE shapes that make XML parsing dangerous do not apply. The input is
        # still untrusted — it comes from a device on the LAN — hence the size caps on every read.
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"unparseable XML: {exc}") from exc


def _find(root: ET.Element, name: str) -> ET.Element | None:
    if _localname(root.tag) == name:
        return root
    for element in root.iter():
        if _localname(element.tag) == name:
            return element
    return None


def find_wan_service(description: str, location: str) -> WanService | None:
    """Pick the NAT-aware service out of a device description document.

    Relative control URLs are resolved against ``URLBase`` when the document has one and against the
    LOCATION otherwise, which is what the UPnP spec says and what routers in the wild actually need:
    a description served from ``http://192.168.1.1:5000/rootDesc.xml`` with ``controlURL`` of
    ``/ctl/IPConn`` is the common case, and joining those wrongly makes every probe fail.
    """
    try:
        root = _parse_xml(description)
    except ValueError as exc:
        log.debug("device description at %s is not XML: %s", location, exc)
        return None

    base_element = _find(root, "URLBase")
    base = (base_element.text or "").strip() if base_element is not None else ""
    base = base or location
    device_name = ""
    friendly = _find(root, "friendlyName")
    if friendly is not None:
        device_name = (friendly.text or "").strip()

    services: dict[str, str] = {}
    for service in root.iter():
        if _localname(service.tag) != "service":
            continue
        service_type = ""
        control = ""
        for child in service:
            if _localname(child.tag) == "serviceType":
                service_type = (child.text or "").strip()
            elif _localname(child.tag) == "controlURL":
                control = (child.text or "").strip()
        if service_type and control:
            services.setdefault(service_type, control)

    for wanted in WAN_SERVICE_TYPES:
        if wanted in services:
            return WanService(
                service_type=wanted,
                control_url=urllib.parse.urljoin(base, services[wanted]),
                device=device_name,
            )
    return None


def list_port_mappings(probe: RouterProbe, service: WanService) -> list[PortMapping]:
    """Every forwarding rule the router will admit to, by walking the mapping table by index.

    ``GetGenericPortMappingEntry`` is the only portable way to *enumerate*: the alternative,
    ``GetSpecificPortMappingEntry``, requires already knowing the port — which is the thing being
    looked for. The walk stops on the first fault, because that is how the end of the table is
    signalled (713, usually) and a router that faults for another reason mid-list has nothing more
    to tell us anyway.
    """
    mappings: list[PortMapping] = []
    for index in range(MAX_MAPPINGS):
        try:
            answer = probe.soap(
                service.control_url,
                service.service_type,
                "GetGenericPortMappingEntry",
                {"NewPortMappingIndex": str(index)},
            )
        except UpnpError as exc:
            log.debug("mapping enumeration stopped at index %s: %s", index, exc)
            break
        except (OSError, ValueError, urllib.error.URLError) as exc:
            log.debug("mapping enumeration failed at index %s: %s", index, exc)
            break
        if not answer:
            break
        mapping = _mapping_from(answer)
        if mapping is None:
            break
        mappings.append(mapping)
    return mappings


def _mapping_from(answer: Mapping[str, str]) -> PortMapping | None:
    try:
        external = int(answer.get("NewExternalPort", ""))
        internal = int(answer.get("NewInternalPort", ""))
    except ValueError:
        return None
    enabled = (answer.get("NewEnabled", "1") or "1").strip() not in ("0", "false", "False")
    return PortMapping(
        protocol=(answer.get("NewProtocol") or "?").strip().upper(),
        external_port=external,
        internal_client=(answer.get("NewInternalClient") or "").strip(),
        internal_port=internal,
        description=(answer.get("NewPortMappingDescription") or "").strip(),
        enabled=enabled,
        remote_host=(answer.get("NewRemoteHost") or "").strip(),
    )


def external_address(probe: RouterProbe, service: WanService) -> str | None:
    try:
        answer = probe.soap(
            service.control_url, service.service_type, "GetExternalIPAddress", {}
        )
    except (UpnpError, OSError, ValueError, urllib.error.URLError) as exc:
        log.debug("GetExternalIPAddress failed: %s", exc)
        return None
    return (answer.get("NewExternalIPAddress") or "").strip() or None


def discover_wan_service(probe: RouterProbe) -> WanService | None:
    """Find the router and the service on it that knows about port mappings, or ``None``."""
    try:
        locations = list(probe.discover())
    except OSError as exc:
        log.debug("SSDP discovery failed: %s", exc)
        return None
    for location in locations:
        try:
            description = probe.get(location)
        except (OSError, urllib.error.URLError, ValueError) as exc:
            log.debug("cannot fetch %s: %s", location, exc)
            continue
        service = find_wan_service(description, location)
        if service is not None:
            return service
    return None


# ---------------------------------------------------------------------------- the audit


def audit(
    config: Config,
    *,
    probe: RouterProbe | None = None,
    check_router: bool = True,
    platform: str | None = None,
    run: CommandRunner = _run,
) -> ExposureReport:
    """Answer all three questions and return one report.

    ``check_router=False`` skips the UPnP half, which is what the daily health check does: it costs
    two seconds of multicast waiting and the answer changes only when somebody reconfigures the
    router, so it belongs in a command a person runs rather than in a job that runs every night.
    """
    findings: list[Finding] = []
    findings.append(_configured_bind(config))

    table = read_listeners(platform=platform, run=run)
    findings.extend(_listener_findings(config, table))

    manual = [
        "Open the router's admin page and confirm that 'Port forwarding' / 'Virtual server' / "
        "'DMZ' lists nothing pointing at this host. UPnP can only report the mappings it made "
        "itself — a rule typed in by hand is invisible to it.",
        "Confirm UPnP/NAT-PMP is switched off on the router. With it on, any device on the LAN "
        "can open a port to the internet without asking anybody (PLAN.md 3.2).",
    ]

    if check_router:
        findings.extend(
            _router_findings(
                config,
                probe if probe is not None else SsdpRouterProbe(),
                _local_addresses(config, table),
            )
        )
    else:
        findings.append(
            Finding(
                name="router",
                status=UNKNOWN,
                detail="not checked (--no-router)",
                fix="run `tunebox exposure` without --no-router to query the router over UPnP",
            )
        )

    return ExposureReport(findings=tuple(findings), manual_checks=tuple(manual))


def _configured_bind(config: Config) -> Finding:
    kind, note = classify_address(config.api_host)
    data = {"host": config.api_host, "port": config.api_port, "kind": kind}
    if kind == "wildcard":
        return Finding(
            name="configured bind",
            status=FAIL,
            detail=f"TUNEBOX_API_HOST is {config.api_host or '(empty)'} — {note}",
            fix="set TUNEBOX_API_HOST to this host's LAN address and restart the service",
            data=data,
        )
    if kind == "public":
        return Finding(
            name="configured bind",
            status=FAIL,
            detail=f"TUNEBOX_API_HOST is {config.api_host} — {note}",
            fix="this service must never be reachable from the internet (PLAN.md 3.2); bind the "
            "LAN address instead, and use Tailscale if remote access is genuinely wanted",
            data=data,
        )
    if kind == "hostname":
        return Finding(
            name="configured bind",
            status=UNKNOWN,
            detail=f"TUNEBOX_API_HOST is {config.api_host} — {note}",
            fix="set it to a literal LAN address so what it resolves to cannot change under you",
            data=data,
        )
    return Finding(
        name="configured bind",
        status=OK,
        detail=f"{config.api_host}:{config.api_port} — {note}",
        data=data,
    )


def _listener_findings(config: Config, table: ListenerTable) -> list[Finding]:
    if table.error is not None:
        return [
            Finding(
                name="listening sockets",
                status=UNKNOWN,
                detail=f"could not read the socket table: {table.error}",
                fix="install lsof (macOS) or iproute2 (Debian), then re-run",
            )
        ]

    findings: list[Finding] = []
    ours = [listener for listener in table.listeners if listener.port == config.api_port]
    findings.append(_own_listener_finding(config, ours, table))

    player = [listener for listener in table.listeners if listener.port == config.player_port]
    if player:
        findings.append(_player_listener_finding(config, player))

    others = [
        listener
        for listener in table.listeners
        if listener.wildcard and listener.port not in (config.api_port, config.player_port)
    ]
    if others:
        # Deduplicated before counting: a service listening on both address families appears twice
        # in the socket table and is one service, and a count that disagreed with the list printed
        # next to it would be the sort of detail that makes a reader distrust the whole report.
        labels = sorted({str(listener) for listener in others})
        findings.append(
            Finding(
                name="other wildcard listeners",
                status=WARN,
                detail=(
                    f"{len(labels)} other service(s) listen on every interface: "
                    + ", ".join(labels[:12])
                    + (f", and {len(labels) - 12} more" if len(labels) > 12 else "")
                ),
                fix="none of these are tunebox's, but each is reachable from the whole LAN, and "
                "from the internet if the router ever forwards its port. Worth knowing about.",
                data={"listeners": labels},
            )
        )
    return findings


def _own_listener_finding(
    config: Config, ours: Iterable[Listener], table: ListenerTable
) -> Finding:
    ours = list(ours)
    if not ours:
        return Finding(
            name="tunebox listener",
            status=UNKNOWN,
            detail=f"nothing is listening on port {config.api_port}; the service is not running",
            fix="this check can only confirm what a *running* service bound. Start it and re-run.",
            data={"command": table.command},
        )
    wildcard = [listener for listener in ours if listener.wildcard]
    if wildcard:
        return Finding(
            name="tunebox listener",
            status=FAIL,
            detail="tunebox is listening on every interface: "
            + ", ".join(str(listener) for listener in wildcard),
            fix="set TUNEBOX_API_HOST to the LAN address and restart. If this is a container with "
            "one interface, TUNEBOX_ALLOW_ALL_INTERFACES=1 is the deliberate way to say so.",
            data={"listeners": [str(listener) for listener in ours]},
        )
    public = [listener for listener in ours if listener.kind == "public"]
    if public:
        return Finding(
            name="tunebox listener",
            status=FAIL,
            detail="tunebox is listening on a globally routable address: "
            + ", ".join(str(listener) for listener in public),
            fix="bind a LAN address instead (PLAN.md 3.2: no public exposure, ever)",
            data={"listeners": [str(listener) for listener in ours]},
        )
    return Finding(
        name="tunebox listener",
        status=OK,
        detail="listening only where it should: "
        + ", ".join(str(listener) for listener in ours),
        data={"listeners": [str(listener) for listener in ours]},
    )


def _player_listener_finding(config: Config, player: Sequence[Listener]) -> Finding:
    wildcard = [listener for listener in player if listener.wildcard]
    if not wildcard:
        return Finding(
            name="player listener",
            status=OK,
            detail="the player daemon is not on a wildcard address: "
            + ", ".join(str(listener) for listener in player),
            data={"listeners": [str(listener) for listener in player]},
        )
    return Finding(
        name="player listener",
        status=WARN,
        detail=(
            "the player daemon listens on every interface ("
            + ", ".join(str(listener) for listener in wildcard)
            + "), which is OwnTone's default and cannot be changed in its config"
        ),
        # Measured in PLAN.md 3.2: the default `trusted_networks = { "lan" }` skips the password for
        # every device on the home network, so the socket being open is only half the problem and
        # the half that *can* be fixed is the authorisation, not the bind.
        fix="that is a warning rather than a failure because the exposure is LAN-wide, not "
        "internet-wide. Close the authorisation half with deploy/owntone-setup.sh, which sets "
        'trusted_networks = { "localhost" } and a real admin_password.',
        data={"listeners": [str(listener) for listener in player]},
    )


def _router_findings(
    config: Config, probe: RouterProbe, local: frozenset[str]
) -> list[Finding]:
    service = discover_wan_service(probe)
    if service is None:
        return [
            Finding(
                name="router (UPnP)",
                status=UNKNOWN,
                detail=(
                    "no UPnP Internet Gateway Device answered. That is consistent with UPnP being "
                    "switched off, which is what PLAN.md 3.2 asks for — but it is not proof: a "
                    "router can forward ports with UPnP disabled, and rules added by hand are "
                    "never visible this way"
                ),
                fix="check the router's port-forwarding page by hand, once",
            )
        ]

    findings = [
        Finding(
            name="router (UPnP)",
            status=WARN,
            detail=(
                f"UPnP is enabled on {service.device or 'the router'} — it answered discovery and "
                "described its NAT service"
            ),
            fix="switch UPnP off on the router unless something in the house needs it. While it is "
            "on, any device on the LAN can open a port to the internet unattended.",
            data={"service": service.service_type, "control_url": service.control_url},
        )
    ]

    mappings = list_port_mappings(probe, service)
    findings.append(_mapping_finding(config, mappings, local))

    external = external_address(probe, service)
    if external:
        findings.append(_nat_finding(config, external))
    return findings


def _mapping_finding(
    config: Config, mappings: Sequence[PortMapping], local: frozenset[str]
) -> Finding:
    if not mappings:
        return Finding(
            name="port forwarding",
            status=OK,
            detail="the router reports no port mappings at all",
            data={"mappings": []},
        )

    ours = [
        mapping
        for mapping in mappings
        if mapping.internal_client in local
        or mapping.internal_port in (config.api_port, config.player_port)
    ]
    data = {"mappings": [mapping.as_dict() for mapping in mappings]}
    if ours:
        return Finding(
            name="port forwarding",
            status=FAIL,
            detail="the router forwards a port to this host: "
            + "; ".join(str(mapping) for mapping in ours),
            fix="delete that rule on the router. PLAN.md 3.2: no port forwarding, ever — this "
            "service runs yt-dlp and ffmpeg on request. Deliberately not deleted automatically: "
            "it is shared household infrastructure and the rule may not be about tunebox at all.",
            data=data,
        )
    return Finding(
        name="port forwarding",
        status=WARN,
        detail=f"the router forwards {len(mappings)} port(s), none to this host: "
        + "; ".join(str(mapping) for mapping in mappings[:8]),
        fix="nothing here reaches tunebox, so this is information rather than a problem with it",
        data=data,
    )


def _nat_finding(config: Config, external: str) -> Finding:
    kind, _ = classify_address(external)
    if kind == "public" and external.strip() == config.api_host.strip():
        return Finding(
            name="NAT",
            status=FAIL,
            detail=(
                f"this host's configured address {config.api_host} *is* the WAN address "
                f"{external}: there is no NAT in front of it"
            ),
            fix="a host with a public address is on the internet whether a port is forwarded or "
            "not. Put it behind NAT, or firewall the port, before running this service on it.",
            data={"external_ip": external},
        )
    return Finding(
        name="NAT",
        status=OK,
        detail=f"the WAN address is {external}, which is not this host's address",
        data={"external_ip": external},
    )


def _local_addresses(config: Config, table: ListenerTable) -> frozenset[str]:
    """This host's own addresses, for matching against a port mapping's internal client.

    Taken from the configuration and from the socket table rather than from a DNS lookup of the
    hostname. Two reasons, and the second is the real one: a name lookup can be wrong or slow, and an
    audit must not depend on the connectivity it is auditing. The addresses tunebox is *configured*
    to use and the addresses something is actually listening on are both facts already in hand.
    """
    found = {config.api_host.strip()}
    found.update(
        listener.address for listener in table.listeners if not listener.wildcard
    )
    return frozenset(address for address in found if address and address != "*")
