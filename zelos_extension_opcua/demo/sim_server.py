"""Configurable OPC-UA simulator for exercising real-world server behaviors.

Profiles pick an address space (see `PROFILES`). Cross-cutting options add a
secured endpoint with an optional trust list, shift namespace indices between
starts, or serve an arbitrary node map. Every service request is recorded per
session in `Simulator.request_log`.

asyncua 2.0.1 has no hook for its per-connection `UaProcessor`, so `_SimServer`
re-implements `Server.start` to swap in `_SimProcessor`. The processor adds what
asyncua lacks: the request log, enforced OperationLimits, a session cap,
RequestedMaxReferencesPerNode, BrowseNext with continuation points, per-session
caps on subscriptions, monitored items and continuation points, and
ServerTimestamp on Read.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import logging
import os
import shutil
import signal
import socket
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from asyncua import Server, ua
from asyncua.common.utils import ServiceError
from asyncua.crypto import uacrypto
from asyncua.crypto.cert_gen import setup_self_signed_certificate
from asyncua.server.binary_server_asyncio import BinaryServer, OPCUAProtocol
from asyncua.server.uaprocessor import UaProcessor
from asyncua.ua.ua_binary import struct_from_binary
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import ExtendedKeyUsageOID

from zelos_extension_opcua.demo import profiles
from zelos_extension_opcua.demo.simulator import DEMO_NAMESPACE, populate_demo
from zelos_extension_opcua.node_map import NodeMap

logger = logging.getLogger(__name__)

NS_SHIFT_ENV = "ZELOS_SIM_NS_SHIFT"
SHUTDOWN_TIMEOUT = 3.0


@dataclass(frozen=True)
class Limits:
    """Server limits, advertised in OperationLimits and enforced by `_SimProcessor`."""

    max_nodes_per_browse: int
    max_nodes_per_read: int
    max_references_per_node: int  # beyond it a Browse result carries a continuation point
    max_sessions: int
    max_subscriptions: int = 0  # per session; 0 = no cap
    max_monitored_items: int = 0  # per session; 0 = no cap
    max_continuation_points: int = 0  # per session; 0 = no cap


async def _build_demo(server: Server, ns: dict[str, int]) -> Any:
    _, updater, _ = await populate_demo(server, ns[DEMO_NAMESPACE])
    return updater


@dataclass(frozen=True)
class Profile:
    """One simulated server shape."""

    server_name: str
    uris: tuple[str, ...]
    build: Callable[[Server, dict[str, int]], Awaitable[Any]]
    limits: Limits | None = None


PROFILES: dict[str, Profile] = {
    "demo": Profile("Zelos Demo PLC Server", (DEMO_NAMESPACE,), _build_demo),
    "gateway": Profile("Zelos Sim Gateway", (profiles.GATEWAY_URI,), profiles.build_gateway),
    # S7-1500 layout: DI at ns=2, the PLC namespace at ns=3.
    "s7": Profile(
        "Zelos Sim S7",
        (profiles.DI_URI, profiles.S7_URI),
        profiles.build_s7,
        Limits(
            max_nodes_per_browse=10,
            max_nodes_per_read=20,
            max_references_per_node=10,
            max_sessions=4,
            # S7-1200-shaped caps, scaled to this address space so overflow is reachable.
            max_subscriptions=5,
            max_monitored_items=10,
            max_continuation_points=3,
        ),
    ),
    "device": Profile(
        "Zelos Sim Device", (profiles.DI_URI, profiles.DEVICE_URI), profiles.build_device
    ),
}


def _service_name(typeid: ua.NodeId) -> str:
    name = ua.ObjectIdNames.get(typeid.Identifier, str(typeid))
    return name.removesuffix("Request_Encoding_DefaultBinary")


class _SimProcessor(UaProcessor):
    """UaProcessor that logs requests and enforces the profile's limits."""

    def __init__(self, sim: Simulator, *args: Any) -> None:
        super().__init__(*args)
        self._sim = sim
        self._continuations: dict[bytes, list[ua.ReferenceDescription]] = {}
        self._next_cp = 0

    def _session_id(self) -> str | None:
        return self.session.session_id.to_string() if self.session else None

    async def _process_message(self, typeid, requesthdr, seqhdr, body):
        service = _service_name(typeid)
        limits = self._sim.limits
        try:
            if service in ("Browse", "BrowseNext"):
                return await self._browse(service, requesthdr, seqhdr, body)
            if service == "Read":
                return await self._read(requesthdr, seqhdr, body)
            if service == "CreateMonitoredItems" and limits and limits.max_monitored_items:
                return await self._create_monitored_items(requesthdr, seqhdr, body)
            if (
                service == "CreateSubscription"
                and self.session
                and limits
                and limits.max_subscriptions
                and len(self._subscriptions()) >= limits.max_subscriptions
            ):
                raise ServiceError(ua.StatusCodes.BadTooManySubscriptions)
            if service == "ActivateSession":
                identity = await self._check_identity(body)
            if (
                service == "CreateSession"
                and limits
                and len(self._sim.open_sessions) >= limits.max_sessions
            ):
                raise ServiceError(ua.StatusCodes.BadTooManySessions)
            result = await super()._process_message(typeid, requesthdr, seqhdr, body)
            if service == "CreateSession":
                self._on_session_created()
                await self._sim.publish_diagnostics()
            elif service == "ActivateSession":
                self._sim.identities[self._session_id()] = identity
            elif service == "CloseSession":
                self._sim.open_sessions.discard(self)
                await self._sim.publish_diagnostics()
            return result
        finally:
            self._sim.request_log.append((self._session_id(), service))

    async def _check_identity(self, body) -> str:
        """The user identity as recorded; a certificate outside `user_cert_dir` is denied.

        Checked ahead of asyncua, which verifies the token signature but accepts
        any certificate.
        """
        params = struct_from_binary(ua.ActivateSessionParameters, body.copy())
        token = params.UserIdentityToken
        if not isinstance(token, ua.X509IdentityToken) or not self._sim.user_cert_dir:
            # asyncua rejects a token type it was not configured to offer.
            return type(token).__name__.removesuffix("IdentityToken").lower()
        if token.CertificateData not in await _certs_in(self._sim.user_cert_dir):
            logger.warning("rejected unknown user certificate")
            raise ServiceError(ua.StatusCodes.BadUserAccessDenied)
        return f"certificate:{hashlib.sha1(token.CertificateData).hexdigest().upper()}"

    def _on_session_created(self) -> None:
        policy = self._connection.security_policy
        mode = getattr(policy, "Mode", ua.MessageSecurityMode.None_)
        record = (mode.name.rstrip("_"), policy.URI.rsplit("#", 1)[-1])
        self._sim.sessions[self._session_id()] = record
        self._sim.open_sessions.add(self)
        logger.info("session %s: mode=%s policy=%s", self._session_id(), *record)

    async def close(self) -> None:
        """Drop the session from the open set, then clean up as asyncua does."""
        self._sim.open_sessions.discard(self)
        await super().close()
        with contextlib.suppress(Exception):  # the server may be stopping
            await self._sim.publish_diagnostics()

    def _check_session(self) -> None:
        """asyncua's session checks and activity stamps, for services handled here."""
        if not self.session:
            raise ServiceError(ua.StatusCodes.BadSessionIdInvalid)
        if not self.session.is_activated():
            raise ServiceError(ua.StatusCodes.BadSessionNotActivated)
        self.session_last_activity = time.monotonic()
        self.session.touch()

    async def _read(self, requesthdr, seqhdr, body) -> bool:
        # asyncua returns the stored DataValue, stamped at the last write; a real
        # server stamps ServerTimestamp at the read.
        self._check_session()
        params = struct_from_binary(ua.ReadParameters, body)
        limits = self._sim.limits
        if limits and len(params.NodesToRead) > limits.max_nodes_per_read:
            raise ServiceError(ua.StatusCodes.BadTooManyOperations)
        now = datetime.now(UTC)
        response = ua.ReadResponse()
        response.Results = [
            dataclasses.replace(dv, ServerTimestamp=now, ServerPicoseconds=None)
            if rv.AttributeId == ua.AttributeIds.Value
            else dv
            for rv, dv in zip(params.NodesToRead, await self.session.read(params), strict=True)
        ]
        self.send_response(requesthdr.RequestHandle, seqhdr, response)
        return True

    def _subscriptions(self) -> list[Any]:
        service = self.session.subscription_service
        return [
            s for s in service.subscriptions.values() if s.session_id == self.session.session_id
        ]

    async def _create_monitored_items(self, requesthdr, seqhdr, body) -> bool:
        # Items past the session's cap get BadTooManyMonitoredItems, as an S7 does.
        self._check_session()
        params = struct_from_binary(ua.CreateMonitoredItemsParameters, body)
        held = sum(len(s.monitored_item_srv._monitored_items) for s in self._subscriptions())
        room = max(0, self._sim.limits.max_monitored_items - held)
        requested = params.ItemsToCreate
        params.ItemsToCreate = requested[:room]
        results = await self.session.create_monitored_items(params) if room else []
        results += [
            ua.MonitoredItemCreateResult(
                StatusCode=ua.StatusCode(ua.StatusCodes.BadTooManyMonitoredItems)
            )
            for _ in requested[room:]
        ]
        response = ua.CreateMonitoredItemsResponse()
        response.Results = results
        self.send_response(requesthdr.RequestHandle, seqhdr, response)
        return True

    async def _browse(self, service, requesthdr, seqhdr, body) -> bool:
        # asyncua ignores RequestedMaxReferencesPerNode and has no BrowseNext;
        # this pages the results.
        self._check_session()
        limits = self._sim.limits

        if service == "Browse":
            params = struct_from_binary(ua.BrowseParameters, body)
            if limits and len(params.NodesToBrowse) > limits.max_nodes_per_browse:
                raise ServiceError(ua.StatusCodes.BadTooManyOperations)
            cap = min(
                (c for c in (params.RequestedMaxReferencesPerNode, self._server_ref_cap()) if c),
                default=0,
            )
            results = await self.session.browse(params)
            for r in results:
                if r.StatusCode.is_good():
                    r.StatusCode, r.References, r.ContinuationPoint = self._page(r.References, cap)
            response = ua.BrowseResponse()
            response.Results = results
        else:
            params = struct_from_binary(ua.BrowseNextParameters, body)
            results = []
            for cp in params.ContinuationPoints:
                r = ua.BrowseResult()
                refs = self._continuations.pop(cp, None)
                if refs is None:
                    r.StatusCode = ua.StatusCode(ua.StatusCodes.BadContinuationPointInvalid)
                elif not params.ReleaseContinuationPoints:
                    # Paging state is per request; a later page uses the server cap.
                    _, r.References, r.ContinuationPoint = self._page(refs, self._server_ref_cap())
                results.append(r)
            response = ua.BrowseNextResponse()
            response.Parameters.Results = results
        self.send_response(requesthdr.RequestHandle, seqhdr, response)
        return True

    def _server_ref_cap(self) -> int:
        return self._sim.limits.max_references_per_node if self._sim.limits else 0

    def _page(
        self, refs: list[ua.ReferenceDescription], cap: int
    ) -> tuple[ua.StatusCode, list[ua.ReferenceDescription], bytes | None]:
        """(status, first page, continuation point or None); a point past the
        session's cap is BadNoContinuationPoints with no references, per Part 4."""
        if not cap or len(refs) <= cap:
            return ua.StatusCode(), refs, None
        limits = self._sim.limits
        if limits and 0 < limits.max_continuation_points <= len(self._continuations):
            return ua.StatusCode(ua.StatusCodes.BadNoContinuationPoints), [], None
        self._next_cp += 1
        cp = self._next_cp.to_bytes(4, "little")
        self._continuations[cp] = refs[cap:]
        return ua.StatusCode(), refs[:cap], cp


class _SimProtocol(OPCUAProtocol):
    sim: Simulator

    def connection_made(self, transport) -> None:
        super().connection_made(transport)
        if self.processor is None:  # refused at max_connections
            return
        # Swapped before the receive task runs its first message.
        self.processor = _SimProcessor(self.sim, self.iserver, transport, self.limits)
        self.processor.set_policies(self.policies)


class _SimBinaryServer(BinaryServer):
    sim: Simulator

    def _make_protocol(self) -> _SimProtocol:
        proto = _SimProtocol(
            iserver=self.iserver,
            policies=self._policies,
            clients=self.clients,
            closing_tasks=self.closing_tasks,
            limits=self.limits,
        )
        proto.sim = self.sim
        return proto


class _SimServer(Server):
    sim: Simulator

    async def start(self) -> None:
        """`Server.start` (asyncua 2.0.1) with `_SimBinaryServer` in place of BinaryServer."""
        await self._setup_server_nodes()
        await self.iserver.start()
        try:
            host, port = self._get_bind_socket_info()
            self.bserver = _SimBinaryServer(self.iserver, host, port, self.limits)
            self.bserver.sim = self.sim
            self.bserver.set_policies(self._policies)
            await self.bserver.start()
        except Exception:
            await self.iserver.stop()
            raise


def namespace_shift() -> int:
    """Placeholder namespaces to register ahead of a profile's own.

    `ZELOS_SIM_NS_SHIFT` makes it deterministic; otherwise it varies per start.
    """
    env = os.environ.get(NS_SHIFT_ENV)
    return int(env) if env else 1 + time.time_ns() // 1_000_000 % 4


async def _certs_in(directory: Path) -> set[bytes]:
    """DER of every certificate in `directory`. Re-read per session so a cert
    dropped into the dir is accepted without restart."""
    certs = set()
    for path in directory.iterdir():
        with contextlib.suppress(Exception):
            certs.add((await uacrypto.load_certificate(path)).public_bytes(Encoding.DER))
    return certs


def _trust_list_validator(trust_dir: Path) -> Callable[..., Awaitable[None]]:
    async def validate(cert, app_description) -> None:
        if cert.public_bytes(Encoding.DER) not in await _certs_in(trust_dir):
            logger.warning("rejected untrusted client cert: %s", cert.subject.rfc4514_string())
            raise ServiceError(ua.StatusCodes.BadCertificateUntrusted)

    return validate


class Simulator:
    """A running simulated OPC-UA server.

    Use as an async context manager. `port=0` binds a free port; read the bound
    one from `endpoint`.
    """

    def __init__(
        self,
        profile: str = "demo",
        host: str = "127.0.0.1",
        port: int = 4840,
        secure: bool = False,
        trust_dir: Path | None = None,
        user_cert_dir: Path | None = None,
        shuffle_namespaces: bool = False,
        node_map: NodeMap | None = None,
        nodes: int = 0,
        secure_only: bool = False,
    ) -> None:
        """Configure; nothing binds until `start`.

        Args:
            profile: Key of `PROFILES`; ignored when `node_map` is given
            host: Bind address
            port: TCP port, 0 for any free port
            secure: Add Basic256Sha256 Sign and SignAndEncrypt endpoints
            trust_dir: With `secure`, only client certs in this dir are accepted
            user_cert_dir: With `secure`, offer Certificate user tokens and accept
                only user certs in this dir
            shuffle_namespaces: Register placeholder namespaces first (see `namespace_shift`)
            node_map: Serve this map instead of a profile
            nodes: Add this many Float variables under `Bulk` (see `profiles.build_bulk`)
            secure_only: With `secure`, offer no None endpoint
        """
        self.profile = PROFILES[profile]
        self.host = host
        self.port = port
        self.secure = secure
        self.trust_dir = trust_dir
        self.user_cert_dir = user_cert_dir
        self.shuffle_namespaces = shuffle_namespaces
        self.node_map = node_map
        self.nodes = nodes
        self.secure_only = secure_only
        self.limits = None if node_map else self.profile.limits
        #: (session id or None before CreateSession, service name) per request
        self.request_log: list[tuple[str | None, str]] = []
        #: session id -> (SecurityMode, SecurityPolicy) negotiated
        self.sessions: dict[str, tuple[str, str]] = {}
        #: session id -> "anonymous", "username" or "certificate:<SHA-1>", once activated
        self.identities: dict[str, str] = {}
        self.open_sessions: set[_SimProcessor] = set()
        self.server: _SimServer | None = None
        self._updater: Any = None
        self._pki: Path | None = None

    @property
    def endpoint(self) -> str:
        """Client URL, with the bound port."""
        port = self.server.bserver.port if self.server and self.server.bserver else self.port
        return f"opc.tcp://{self.host}:{port}/freeopcua/server/"

    async def start(self) -> None:
        """Build the address space and start listening."""
        server = _SimServer()
        server.sim = self
        await server.init()
        server.set_endpoint(f"opc.tcp://{self.host}:{self.port}/freeopcua/server/")
        server.set_server_name(self.profile.server_name)
        # Unique per instance, as a real server's is: discovery dedupes on it.
        await server.set_application_uri(f"urn:zelos:sim:{uuid.uuid4().hex[:12]}")
        await self._setup_security(server)

        shift = namespace_shift() if self.shuffle_namespaces else 0
        for i in range(shift):
            await server.register_namespace(f"urn:zelos:sim:shift:{i}")
        if shift:
            logger.info("namespace shift: %d", shift)

        if self.node_map:
            self._updater = await profiles.build_map(server, self.node_map)
        else:
            # An odd shift also reverses the profile's own namespace order.
            uris = self.profile.uris[::-1] if shift % 2 else self.profile.uris
            ns = {uri: await server.register_namespace(uri) for uri in uris}
            self._updater = await self.profile.build(server, ns)

        if self.nodes:
            await profiles.build_bulk(server, self.nodes)
        if self.limits:
            await self._advertise_limits(server)

        self.server = server
        await server.start()
        await self.publish_diagnostics()
        await self._updater.start()
        logger.info("OPC-UA simulator listening on %s", self.endpoint)

    async def _setup_security(self, server: Server) -> None:
        tokens = [ua.AnonymousIdentityToken, ua.UserNameIdentityToken]
        if self.user_cert_dir:
            tokens.append(ua.X509IdentityToken)
        server.set_identity_tokens(tokens)
        if not self.secure:
            server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
            return
        self._pki = Path(tempfile.mkdtemp(prefix="zelos-opcua-sim-"))
        key, cert = self._pki / "server_key.pem", self._pki / "server_cert.der"
        await setup_self_signed_certificate(
            key,
            cert,
            server.get_application_uri(),
            socket.gethostname(),
            [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH],
            {"commonName": "Zelos OPC-UA Simulator"},
        )
        await server.load_certificate(str(cert))
        await server.load_private_key(str(key))
        server.set_security_policy(
            [
                *([] if self.secure_only else [ua.SecurityPolicyType.NoSecurity]),
                ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt,
                ua.SecurityPolicyType.Basic256Sha256_Sign,
            ]
        )
        if self.trust_dir:
            server.set_certificate_validator(_trust_list_validator(self.trust_dir))
        logger.info("server certificate: %s", cert)

    async def _advertise_limits(self, server: Server) -> None:
        ids = ua.ObjectIds
        for node_id, value in (
            (ids.Server_ServerCapabilities_OperationLimits_MaxNodesPerBrowse,
             self.limits.max_nodes_per_browse),
            (ids.Server_ServerCapabilities_OperationLimits_MaxNodesPerRead,
             self.limits.max_nodes_per_read),
        ):  # fmt: skip
            await server.get_node(node_id).write_value(ua.Variant(value, ua.VariantType.UInt32))

    async def publish_diagnostics(self) -> None:
        """ServerDiagnosticsSummary counters, which asyncua leaves empty."""
        if not self.server:
            return
        prefix = "Server_ServerDiagnostics_ServerDiagnosticsSummary_"
        for name, value in (
            ("CurrentSessionCount", len(self.open_sessions)),
            ("CumulatedSessionCount", len(self.sessions)),
            ("RejectedRequestsCount", 0),
            ("SecurityRejectedRequestsCount", 0),
            ("CurrentSubscriptionCount", 0),
        ):
            await self.server.write_attribute_value(
                ua.NodeId(getattr(ua.ObjectIds, prefix + name)),
                ua.DataValue(ua.Variant(value, ua.VariantType.UInt32)),
            )

    async def stop(self) -> None:
        """Stop within `SHUTDOWN_TIMEOUT`, then give up."""
        try:
            async with asyncio.timeout(SHUTDOWN_TIMEOUT):
                if self._updater:
                    await self._updater.stop()
                if self.server:
                    await self.server.stop()
        except TimeoutError:
            logger.warning("simulator shutdown exceeded %.0fs; abandoning", SHUTDOWN_TIMEOUT)
        finally:
            if self._pki:
                shutil.rmtree(self._pki, ignore_errors=True)

    async def __aenter__(self) -> Simulator:
        try:
            await self.start()
        except BaseException:
            await self.stop()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    def request_summary(self) -> str:
        """One line: request counts by service, and session count."""
        counts = Counter(service for _, service in self.request_log)
        services = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        return f"requests: {services or 'none'}; sessions: {len(self.sessions)}"


async def run_sim(log_requests: bool = False, **kwargs: Any) -> None:
    """Run a `Simulator` until SIGINT/SIGTERM."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    sim = Simulator(**kwargs)
    async with sim:
        await stop.wait()
        logger.info("stopping simulator")
    if log_requests:
        logger.info(sim.request_summary())
