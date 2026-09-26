"""Find OPC UA servers for the config form's Auto-configure button.

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

from zelos_extension_opcua.client import SECURITY_POLICIES

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


async def _endpoints(url: str) -> FoundServer | None:
    client = Client(url, timeout=PROBE_TIMEOUT)
    try:
        endpoints = await asyncio.wait_for(client.connect_and_get_server_endpoints(), PROBE_TIMEOUT)
    except Exception as e:
        logger.debug("no OPC UA server at %s: %s", url, e)
        return None
    return FoundServer(url, endpoints) if endpoints else None


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


def _security(endpoints: list[ua.EndpointDescription]) -> tuple[str, str] | None:
    """ "default" when None is offered, else the strongest supported pair, SignAndEncrypt first.

    Never an explicit None: the action cannot see Advanced, and an explicit None
    would silently downgrade a secure Advanced default. "default" follows it
    (None out of the box).
    """
    offered = {(ep.SecurityMode, _POLICY_NAMES.get(ep.SecurityPolicyUri)) for ep in endpoints}
    if any(mode == ua.MessageSecurityMode.None_ for mode, _ in offered):
        return "default", "default"
    for mode in (ua.MessageSecurityMode.SignAndEncrypt, ua.MessageSecurityMode.Sign):
        for policy in POLICY_RANK:
            if (mode, policy) in offered:
                return mode.name, policy
    return None


def _server_entry(found: FoundServer, taken: set[str]) -> dict[str, Any] | None:
    security = _security(found.endpoints)
    if security is None:
        return None
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


async def find_servers() -> tuple[list[dict[str, Any]], list[str]]:
    """`servers[]` entries for every server found, and notes for the user."""
    probes = [_endpoints(f"opc.tcp://localhost:{port}") for port in WELL_KNOWN_PORTS]
    local, lds, mdns = await asyncio.gather(asyncio.gather(*probes), _lds_urls(), _mdns_urls())
    remote = await asyncio.gather(*(_endpoints(url) for url in (*lds, *mdns)))

    unique: dict[str, FoundServer] = {}
    for found in (*local, *remote):  # a localhost answer wins over a remote name
        if found is not None and found.key not in unique:
            unique[found.key] = found

    servers, notes, taken = [], [], set()
    for found in unique.values():
        entry = _server_entry(found, taken)
        if entry is None:
            notes.append(f"{found.url} offers no security policy this extension supports")
            continue
        servers.append(entry)
    if any(s["security_mode"] == "default" for s in servers):
        notes.append(
            "Servers that accept security None are set to 'default': they follow "
            "Advanced > Security Mode (None unless changed there)"
        )
    if any(s["security_mode"] != "default" for s in servers):
        notes.append(
            "Secure servers need this extension's client certificate trusted on the server; "
            "it is generated on the first connect and its thumbprint logged"
        )
    return servers, notes
