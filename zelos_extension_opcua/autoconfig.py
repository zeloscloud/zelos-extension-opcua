"""Find OPC UA servers for the config form's Auto-configure button: check the
form's servers, or with none, look on this machine.

Read-only and session-less: GetEndpoints and FindServers over a None channel,
plus a passive mDNS browse. No subnet sweep, no ports beyond `WELL_KNOWN_PORTS`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import zelos_sdk
from asyncua import Client, ua

from zelos_extension_opcua.client import (
    SECURITY_MODES,
    SECURITY_POLICIES,
    Unreachable,
    has_userinfo,
    mark_unreachable,
    offered_security,
)

logger = logging.getLogger(__name__)

# Default ports of common servers, probed on localhost only.
WELL_KNOWN_PORTS = (
    4840,  # IANA OPC UA, most servers and Local Discovery Servers
    4841,  # a second server beside one on 4840
    48010,  # Unified Automation demo / C++ SDK
    49320,  # Kepware KEPServerEX
    62541,  # OPC Foundation .NET reference server
    53530,  # Prosys Simulation Server
)
LDS_URL = "opc.tcp://localhost:4840"
MDNS_SERVICE = "_opcua-tcp._tcp.local."
# A refused port answers at once; this bounds a port that accepts and stalls.
PROBE_TIMEOUT = 2.0
MDNS_SECONDS = 2.0

UNREACHABLE, FAILED, CREDENTIALS = "unreachable", "failed", "credentials"

# Strongest first; only the policies this extension can connect with.
POLICY_RANK = ("Aes256Sha256RsaPss", "Aes128Sha256RsaOaep", "Basic256Sha256")
_POLICY_NAMES = {cls.URI: name for name, cls in SECURITY_POLICIES.items()}


@dataclass
class FoundServer:
    """One server as its GetEndpoints reply describes it."""

    url: str  # the URL that answered
    endpoints: list[ua.EndpointDescription]

    @property
    def key(self) -> str:
        """ApplicationUri, or the URL for a server that sends none."""
        return self.endpoints[0].Server.ApplicationUri or self.url


async def _endpoints(url: str) -> FoundServer | str:
    """The server at `url`, or why not: UNREACHABLE, FAILED when it answered
    but not with endpoints, CREDENTIALS (never contacted: asyncua would log in)."""
    if has_userinfo(url):
        return CREDENTIALS
    try:
        client = mark_unreachable(Client(url, timeout=PROBE_TIMEOUT))
        # Past the socket timeout: a socket that never opened raises Unreachable first.
        endpoints = await asyncio.wait_for(
            client.connect_and_get_server_endpoints(), PROBE_TIMEOUT + 1.0
        )
    except Unreachable as e:
        logger.debug("cannot connect to %s: %s", url, e)
        return UNREACHABLE
    except Exception as e:
        logger.debug("no OPC UA server at %s: %s", url, e)
        return FAILED
    return FoundServer(url, endpoints) if endpoints else FAILED


async def _lds_urls() -> list[str]:
    """Discovery URLs the Local Discovery Server knows of."""
    client = Client(LDS_URL, timeout=PROBE_TIMEOUT)
    try:
        servers = await asyncio.wait_for(client.connect_and_find_servers(), PROBE_TIMEOUT)
    except Exception as e:
        logger.debug("no discovery server at %s: %s", LDS_URL, e)
        return []
    return [
        url
        for app in servers
        if app.ApplicationType != ua.ApplicationType.DiscoveryServer
        for url in app.DiscoveryUrls or []
        if url.startswith(ua.OPC_TCP_SCHEME)
    ]


async def _mdns_urls() -> list[str]:
    """opc.tcp URLs announced over mDNS within MDNS_SECONDS."""
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

    names: set[str] = set()

    def on_change(zeroconf: Any, service_type: str, name: str, state_change: Any) -> None:
        if state_change is ServiceStateChange.Added:
            names.add(name)

    urls = []
    try:
        azc = AsyncZeroconf()
    except Exception as e:  # no usable interface, or multicast refused
        logger.debug("mDNS unavailable: %s", e)
        return []
    try:
        browser = AsyncServiceBrowser(azc.zeroconf, MDNS_SERVICE, handlers=[on_change])
        await asyncio.sleep(MDNS_SECONDS)
        await browser.async_cancel()
        for name in sorted(names):
            info = AsyncServiceInfo(MDNS_SERVICE, name)
            if not await info.async_request(azc.zeroconf, 1000) or not info.port:
                continue
            path = (info.properties or {}).get(b"path", b"").decode(errors="replace")
            for host in info.parsed_addresses()[:1]:
                host = f"[{host}]" if ":" in host else host
                urls.append(f"opc.tcp://{host}:{info.port}{path}")
    finally:
        with contextlib.suppress(Exception):
            await azc.async_close()
    return urls


def _offers(endpoints: list[ua.EndpointDescription], mode: str, policy: str) -> bool:
    """Whether the server offers `mode`/`policy`; mode None needs no policy."""
    if mode == "None":
        return any(ep.SecurityMode == ua.MessageSecurityMode.None_ for ep in endpoints)
    return any(
        ep.SecurityMode == SECURITY_MODES.get(mode)
        and _POLICY_NAMES.get(ep.SecurityPolicyUri) == policy
        for ep in endpoints
    )


def _security(
    endpoints: list[ua.EndpointDescription], advanced: tuple[str, str] = ("None", "None")
) -> tuple[str, str] | None:
    """ "default" when the server offers the form's Advanced security, else its strongest
    supported secure pair, SignAndEncrypt first; None when it offers nothing usable.

    Never an explicit None: under a secure Advanced that is a silent downgrade. A
    server that accepts only None stays "default" and gets a note.
    """
    if _offers(endpoints, *advanced):
        return "default", "default"
    for mode in ("SignAndEncrypt", "Sign"):
        for policy in POLICY_RANK:
            if _offers(endpoints, mode, policy):
                return mode, policy
    return ("default", "default") if _offers(endpoints, "None", "None") else None


def _server_entry(found: FoundServer, security: tuple[str, str], taken: set[str]) -> dict[str, Any]:
    # The host that answered, with the path the server publishes: a server's own
    # EndpointUrl often names a hostname this machine cannot resolve.
    reached = urlparse(found.url)
    path = urlparse(found.endpoints[0].EndpointUrl).path
    endpoint = f"{reached.scheme}://{reached.netloc}{path}"
    label = found.endpoints[0].Server.ApplicationName.Text or reached.hostname or "server"
    name = base = zelos_sdk.sanitize_name(label, kind="source")
    k = 2
    while name in taken:
        name, k = f"{base}_{k}", k + 1
    taken.add(name)
    return {
        "name": name,
        "endpoint": endpoint,
        "security_mode": security[0],
        "security_policy": security[1],
    }


class _Report:
    """Per-endpoint clauses for the action's message."""

    def __init__(self, advanced: dict[str, Any]) -> None:
        self.advanced = (
            advanced.get("security_mode") or "None",
            advanced.get("security_policy") or "None",
        )
        self.found: list[str] = []
        self.problems: list[str] = []
        self.unreachable: list[str] = []
        self.secure = False  # a found server connects with security: trust note

    def check(self, found: FoundServer, entry: dict[str, Any], where: str) -> bool:
        """Record `found` for `entry` ("default" security filled when the server does not
        offer the Advanced one); False with the reason when it cannot connect as set."""
        url = entry["endpoint"]
        mode = entry.get("security_mode") or "default"
        if mode == "default":
            security = _security(found.endpoints, self.advanced)
            if security is None:
                self.problems.append(f"{url} offers no security policy this extension supports.")
                return False
            if security[0] != "default":
                mode = security[0]
                entry["security_mode"], entry["security_policy"] = security
            elif not _offers(found.endpoints, *self.advanced):
                self.problems.append(
                    f"{url} accepts only security None; set its Security Mode to None to "
                    "connect without security."
                )
        policy = entry.get("security_policy") or "default"
        effective = (
            self.advanced[0] if mode == "default" else mode,
            self.advanced[1] if policy == "default" else policy,
        )
        if mode != "default" and not _offers(found.endpoints, *effective):
            self.problems.append(
                f"{url} does not offer {'/'.join(effective)}; it offers "
                f"{offered_security(found.endpoints)}."
            )
            return False
        self.secure |= effective[0] != "None" and _offers(found.endpoints, *effective)
        name = found.endpoints[0].Server.ApplicationName.Text or urlparse(url).hostname
        shown = "security default" if mode == "default" else f"{mode}/{policy}"
        self.found.append(f"{name} at {url} ({shown}, {where})")
        return True

    def result(self, config: dict[str, Any], fallback: str) -> dict[str, Any]:
        if self.unreachable:
            self.problems.append(f"Couldn't connect to {', '.join(self.unreachable)}.")
        if not self.found:
            return {"status": "error", "message": " ".join(self.problems) or fallback}
        notes = [f"Found {'; '.join(self.found)}.", *self.problems]
        if self.secure:
            notes.append(
                "Secure servers need this extension's client certificate trusted on the "
                "server; it is generated on the first connect and its thumbprint logged."
            )
        return {"status": "success", "message": " ".join(notes), "config": config}


async def check_servers(servers: list[dict[str, Any]], advanced: dict[str, Any]) -> dict[str, Any]:
    """The form's servers, each checked at its endpoint and kept as entered; security
    left at "default" is filled in when the server does not offer the Advanced one."""
    results = await asyncio.gather(*(_endpoints(s.get("endpoint", "")) for s in servers))
    report, out = _Report(advanced), []
    for entry, result in zip(servers, results, strict=True):
        entry = dict(entry)
        out.append(entry)
        if result == CREDENTIALS:
            name = entry.get("name") or urlparse(entry.get("endpoint", "")).hostname
            report.problems.append(
                f"{name}: remove the user name / password from the endpoint URL; user name "
                "login is not supported, use a user certificate."
            )
        elif result == UNREACHABLE:
            report.unreachable.append(entry.get("endpoint", ""))
        elif result == FAILED:
            report.problems.append(
                f"{entry.get('endpoint', '')} did not answer as an OPC UA server."
            )
        else:
            report.check(result, entry, "already in the form")
    return report.result({"servers": out}, "")


async def probe(advanced: dict[str, Any]) -> dict[str, Any]:
    """Servers on this machine's well-known ports, its LDS and mDNS, each added."""
    probes = [_endpoints(f"opc.tcp://localhost:{port}") for port in WELL_KNOWN_PORTS]
    local, lds, mdns = await asyncio.gather(asyncio.gather(*probes), _lds_urls(), _mdns_urls())
    remote = await asyncio.gather(*(_endpoints(url) for url in (*lds, *mdns)))

    unique: dict[str, FoundServer] = {}
    for found in (*local, *remote):  # a localhost answer wins over a remote name
        if isinstance(found, FoundServer) and found.key not in unique:
            unique[found.key] = found

    report, servers, taken = _Report(advanced), [], set()
    for found in unique.values():
        entry = _server_entry(found, ("default", "default"), taken)
        if report.check(found, entry, "added"):
            servers.append(entry)
    ports = ", ".join(map(str, WELL_KNOWN_PORTS))
    return report.result(
        {"servers": servers},
        f"No OPC UA server answered on this machine's usual ports ({ports}) or over mDNS. "
        "Add a server with its endpoint.",
    )
