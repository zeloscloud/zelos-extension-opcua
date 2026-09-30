"""Core OPC-UA client wrapper with Zelos SDK integration."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import itertools
import logging
import math
import os
import signal
import socket
import time
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import zelos_sdk
from asyncua import Client, ua
from asyncua.common.node import Node as UaNode
from asyncua.crypto import security_policies, uacrypto
from asyncua.crypto.cert_gen import (
    dump_private_key_as_pem,
    generate_private_key,
    generate_self_signed_app_certificate,
)
from asyncua.ua.uaerrors import UaStructParsingError
from asyncua.ua.uatypes import NodeId
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID
from zelos_sdk import schemas
from zelos_sdk.hooks.logging import TraceLoggingHandler

from zelos_extension_opcua.diagnostics import SERVER, peak_rss_bytes, watch_loop
from zelos_extension_opcua.discovery import (
    HEALTH_EVENT,
    MAX_OPERATIONS,
    TRACE_NAME_BYTES,
    VARIANT_DATATYPES,
    discover,
    operation_limits,
    read_many,
    to_nsu_string,
    value_of,
)
from zelos_extension_opcua.node_map import Node, NodeMap, parse_node_id

logger = logging.getLogger(__name__)

# Reconnect backoff: a server down for hours costs one attempt a minute.
RECONNECT_INITIAL = 3.0
RECONNECT_MAX = 60.0

# Ceiling on the disconnect at shutdown: a wedged session must not make the process unkillable.
SHUTDOWN_TIMEOUT = 3.0

# Unclassified request failures in a row that force a reconnect: never wedge on a dead session.
POLL_FAILURES_BEFORE_RECONNECT = 5

TRANSPORTS = ("subscription", "poll")

# A quiet subscription still answers a Publish this often (keep-alive), and
# lives this many keep-alives without one; the spec floor is 3.
KEEPALIVE_SECONDS = 10.0
LIFETIME_KEEPALIVES = 10

# The server takes no more: later items are refused without asking (an S7 would
# otherwise cost one rejected request per 100 items).
SERVER_FULL_CODES = frozenset(
    (ua.StatusCodes.BadTooManyMonitoredItems, ua.StatusCodes.BadTooManySubscriptions)
)

# Staleness sweep steps per min_update_interval, at least: an item is refreshed
# within min_update_interval * (1 + 1 / SWEEP_STEPS) of its last update.
SWEEP_STEPS = 4

_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# Before: asyncua's decoding of a null DateTime (1601-01-01), and anything a Unix
# ns timestamp cannot hold. After: past int64 ns (OPC PLC sends 9999-12-31).
_NULL_TIME = datetime(1601, 1, 2, tzinfo=UTC)
_MAX_TIME = datetime(2262, 4, 11, tzinfo=UTC)

# The session or transport is gone, not one node: reconnect.
CONNECTION_STATUS_CODES = frozenset(
    getattr(ua.StatusCodes, name)
    for name in (
        "BadSessionClosed",
        "BadSessionIdInvalid",
        "BadSessionNotActivated",
        "BadSecureChannelClosed",
        "BadSecureChannelIdInvalid",
        "BadSecureChannelTokenUnknown",
        "BadConnectionClosed",
        "BadConnectionRejected",
        "BadServerNotConnected",
        "BadNotConnected",
        "BadDisconnect",
        "BadCommunicationError",
        "BadNoCommunication",
        "BadTimeout",
        "BadServerHalted",
        "BadShutdown",
    )
)

SDK_DATATYPES = {
    "bool": zelos_sdk.DataType.Boolean,
    "uint8": zelos_sdk.DataType.UInt8,
    "int8": zelos_sdk.DataType.Int8,
    "uint16": zelos_sdk.DataType.UInt16,
    "int16": zelos_sdk.DataType.Int16,
    "uint32": zelos_sdk.DataType.UInt32,
    "int32": zelos_sdk.DataType.Int32,
    "float32": zelos_sdk.DataType.Float32,
    "uint64": zelos_sdk.DataType.UInt64,
    "int64": zelos_sdk.DataType.Int64,
    "float64": zelos_sdk.DataType.Float64,
    "string": zelos_sdk.DataType.String,
}

INT_DATATYPES = ("uint8", "int8", "uint16", "int16", "uint32", "int32", "uint64", "int64")
INT_RANGES = {f"int{b}": (-(2 ** (b - 1)), 2 ** (b - 1) - 1) for b in (8, 16, 32, 64)} | {
    f"uint{b}": (0, 2**b - 1) for b in (8, 16, 32, 64)
}

# Server health, read in the poll batch: (field, ns=0 Variable).
HEALTH_NODES = (
    ("state", ua.ObjectIds.Server_ServerStatus_State),
    ("current_time", ua.ObjectIds.Server_ServerStatus_CurrentTime),
    ("start_time", ua.ObjectIds.Server_ServerStatus_StartTime),
    ("service_level", ua.ObjectIds.Server_ServiceLevel),
    ("current_session_count",
     ua.ObjectIds.Server_ServerDiagnostics_ServerDiagnosticsSummary_CurrentSessionCount),
    ("cumulated_session_count",
     ua.ObjectIds.Server_ServerDiagnostics_ServerDiagnosticsSummary_CumulatedSessionCount),
    ("rejected_requests_count",
     ua.ObjectIds.Server_ServerDiagnostics_ServerDiagnosticsSummary_RejectedRequestsCount),
    ("security_rejected_requests_count",
     ua.ObjectIds.Server_ServerDiagnostics_ServerDiagnosticsSummary_SecurityRejectedRequestsCount),
    ("current_subscription_count",
     ua.ObjectIds.Server_ServerDiagnostics_ServerDiagnosticsSummary_CurrentSubscriptionCount),
)  # fmt: skip
_COUNT = zelos_sdk.DataType.UInt32
HEALTH_FIELDS = (
    ("state", zelos_sdk.DataType.Int32, ""),
    ("state_name", zelos_sdk.DataType.String, ""),
    ("current_time", zelos_sdk.DataType.TimestampNs, ""),
    ("clock_skew_ms", zelos_sdk.DataType.Float64, "ms"),
    ("start_time", zelos_sdk.DataType.TimestampNs, ""),
    ("service_level", zelos_sdk.DataType.UInt8, ""),
    ("current_session_count", _COUNT, ""),
    ("cumulated_session_count", _COUNT, ""),
    ("rejected_requests_count", _COUNT, ""),
    ("security_rejected_requests_count", _COUNT, ""),
    ("current_subscription_count", _COUNT, ""),
)

BOOL_TRUE = frozenset({"true", "1", "on", "yes"})
BOOL_FALSE = frozenset({"false", "0", "off", "no"})

SECURITY_MODES = {
    "Sign": ua.MessageSecurityMode.Sign,
    "SignAndEncrypt": ua.MessageSecurityMode.SignAndEncrypt,
}
SECURITY_POLICIES = {
    "Basic256Sha256": security_policies.SecurityPolicyBasic256Sha256,
    "Aes128Sha256RsaOaep": security_policies.SecurityPolicyAes128Sha256RsaOaep,
    "Aes256Sha256RsaPss": security_policies.SecurityPolicyAes256Sha256RsaPss,
}
SERVER_CERTIFICATE_POLICIES = ("auto", "strict")

# Generated client identity, reused: servers trust by thumbprint, so regenerating
# revokes that trust. ZELOS_DATA_DIR survives updates; outside the agent, home.
PKI_DIR = (
    Path(os.environ["ZELOS_DATA_DIR"]) / "pki"
    if os.environ.get("ZELOS_DATA_DIR")
    else Path.home() / ".zelos" / "opcua" / "pki"
)
CLIENT_CERT_FILE = "client_cert.der"
CLIENT_KEY_FILE = "client_key.pem"
# Must equal the URI in the certificate's SubjectAltName; servers reject a mismatch.
APPLICATION_URI = "urn:zelos:opcua:client"
# Expiry forces a re-trust on every server, so it is announced ahead of time.
CLIENT_CERT_DAYS = 730
CERT_EXPIRY_WARNING_DAYS = 30

# Server replies that mean it does not trust our certificate.
CERT_REJECTED_CODES = frozenset(
    (ua.StatusCodes.BadCertificateUntrusted, ua.StatusCodes.BadSecurityChecksFailed)
)
# ActivateSession replies that mean the server refused the user identity.
USER_REJECTED_CODES = frozenset(
    (
        ua.StatusCodes.BadIdentityTokenRejected,
        ua.StatusCodes.BadIdentityTokenInvalid,
        ua.StatusCodes.BadUserAccessDenied,
        ua.StatusCodes.BadUserSignatureInvalid,
    )
)


class ConnectionSecurityError(Exception):
    """A secure connect refused on security grounds. Never retried insecurely."""


class Unreachable(Exception):
    """The transport never opened: refused, no route, unknown host or timed out."""


def mark_unreachable(client: Client) -> Client:
    """Make `client` raise Unreachable when its socket never opens.

    Only the socket open can tell "nothing there" from "answered, then failed":
    a timeout or reset after it is the server's.
    """
    open_socket = client.connect_socket

    async def connect_socket() -> None:
        try:
            await open_socket()
        except OSError as e:  # TimeoutError and socket.gaierror included
            if isinstance(e, socket.gaierror):
                reason = "host not found"
            elif isinstance(e, ConnectionRefusedError):
                reason = "connection refused"
            elif isinstance(e, TimeoutError):
                reason = "timed out"
            else:
                reason = e.strerror or describe_error(e)
            raise Unreachable(reason) from e

    client.connect_socket = connect_socket  # type: ignore[method-assign]
    return client


def offered_security(endpoints: Iterable[ua.EndpointDescription]) -> str:
    """`None/None, SignAndEncrypt/Basic256Sha256`: the mode/policy pairs a server offers."""
    offered = {
        f"{ep.SecurityMode.name.rstrip('_')}/{ep.SecurityPolicyUri.rsplit('#', 1)[-1]}"
        for ep in endpoints
    }
    return ", ".join(sorted(offered)) or "no endpoints"


def thumbprint(der: bytes) -> str:
    """SHA-1 certificate thumbprint, uppercase hex, as OPC UA servers show it."""
    return hashlib.sha1(der).hexdigest().upper()


def _is_pem(data: bytes) -> bool:
    return data.lstrip().startswith(b"-----BEGIN")


def load_cert_der(path: str | Path) -> bytes:
    """A DER or PEM certificate file as DER bytes."""
    data = Path(path).expanduser().read_bytes()
    cert = (
        x509.load_pem_x509_certificate(data)
        if _is_pem(data)
        else x509.load_der_x509_certificate(data)
    )
    return cert.public_bytes(serialization.Encoding.DER)


def _load_private_key(path: str | Path) -> uacrypto.CertProperties:
    """Key bytes tagged with their encoding; asyncua otherwise guesses from the suffix."""
    data = Path(path).expanduser().read_bytes()
    encoding = "pem" if _is_pem(data) else "der"
    load = (
        serialization.load_pem_private_key
        if encoding == "pem"
        else serialization.load_der_private_key
    )
    load(data, password=None)  # raises on a corrupt or encrypted key
    return uacrypto.CertProperties(data, extension=encoding)


def _application_uri(cert_der: bytes) -> str:
    """The SubjectAltName URI the session must present, or the default."""
    cert = x509.load_der_x509_certificate(cert_der)
    with contextlib.suppress(x509.ExtensionNotFound):
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        uris = san.value.get_values_for_type(x509.UniformResourceIdentifier)
        if uris:
            return uris[0]
    logger.warning("Client certificate has no application URI; servers may reject it")
    return APPLICATION_URI


def ensure_client_certificate(pki_dir: Path) -> tuple[Path, Path]:
    """The generated client cert and key, created on first use.

    Returns:
        (certificate path, private key path)
    """
    cert_path, key_path = pki_dir / CLIENT_CERT_FILE, pki_dir / CLIENT_KEY_FILE
    if cert_path.is_file() and key_path.is_file():
        cert = x509.load_der_x509_certificate(cert_path.read_bytes())
        if cert.not_valid_after_utc > datetime.now(UTC):
            return cert_path, key_path
        logger.warning(
            "Client certificate %s expired; generating a new one, which the server must trust",
            cert_path,
        )

    pki_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = generate_private_key()
    cert = generate_self_signed_app_certificate(
        key,
        "Zelos OPC-UA Client",
        {},
        [x509.UniformResourceIdentifier(APPLICATION_URI), x509.DNSName(socket.gethostname())],
        [ExtendedKeyUsageOID.CLIENT_AUTH],
        days=CLIENT_CERT_DAYS,
    )
    # Owner-only before the key is written.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(dump_private_key_as_pem(key))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.DER))
    return cert_path, key_path


def validate_security(
    security_mode: str,
    security_policy: str,
    certificate_file: str,
    private_key_file: str,
    server_certificate: str,
    server_certificate_file: str,
    user_certificate_file: str = "",
    user_private_key_file: str = "",
) -> None:
    """Reject a security configuration that could not be honored as written.

    Raises:
        ValueError: With a one-line reason
    """
    secure = security_mode != "None"
    if secure and security_mode not in SECURITY_MODES:
        raise ValueError(f"unknown security_mode '{security_mode}'")
    if secure and security_policy not in SECURITY_POLICIES:
        raise ValueError(
            f"security_mode {security_mode} needs security_policy one of "
            f"{', '.join(SECURITY_POLICIES)}, got '{security_policy}'"
        )
    if bool(certificate_file) != bool(private_key_file):
        raise ValueError("certificate_file and private_key_file must be set together")
    if bool(user_certificate_file) != bool(user_private_key_file):
        raise ValueError("user_certificate_file and user_private_key_file must be set together")
    # The user token signature covers the server nonce, which only a secure channel protects.
    if user_certificate_file and not secure:
        raise ValueError("a user certificate needs security_mode Sign or SignAndEncrypt")
    if server_certificate not in SERVER_CERTIFICATE_POLICIES:
        raise ValueError(f"server_certificate must be auto or strict, got '{server_certificate}'")
    strict = server_certificate == "strict"
    if strict:
        if not secure:
            raise ValueError("server_certificate strict needs security_mode Sign or SignAndEncrypt")
        if not server_certificate_file:
            raise ValueError("server_certificate strict needs server_certificate_file")
    for label, path, load in (
        ("certificate_file", certificate_file, load_cert_der),
        ("private_key_file", private_key_file, _load_private_key),
        ("server_certificate_file", server_certificate_file if strict else "", load_cert_der),
        ("user_certificate_file", user_certificate_file, load_cert_der),
        ("user_private_key_file", user_private_key_file, _load_private_key),
    ):
        if path:
            try:
                load(path)
            except Exception as e:
                raise ValueError(f"{label} '{path}': {describe_error(e)}") from None


def describe_error(error: BaseException) -> str:
    """Message, or the class name: TimeoutError and friends stringify to nothing."""
    return str(error) or type(error).__name__


def coerce_text(text: str, datatype: str) -> float | int | bool | str:
    """Parse a write action's text into a node's datatype.

    Integer text stays exact (int64/uint64 exceed a float's 53 bits); other
    numeric text is parsed as a float and truncated for integer types.

    Raises:
        ValueError: If the text does not parse as the datatype
    """
    if datatype == "string":
        return text

    value = text.strip().lower()
    if datatype == "bool":
        if value in BOOL_TRUE:
            return True
        if value in BOOL_FALSE:
            return False
        raise ValueError(f"Cannot parse '{text}' as bool. Use true/false or 1/0.")

    integer = datatype not in ("float32", "float64")
    try:
        return int(value) if integer else float(value)
    except ValueError:
        pass
    try:
        number = float(value)
    except ValueError:
        raise ValueError(f"Cannot parse '{text}' as {datatype}") from None
    return int(number) if integer else number


def render_text(value: Any) -> str:
    """A scalar as text, for a node whose value type may change; a struct is its str()."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, ua.LocalizedText):
        return value.Text or ""
    if isinstance(value, NodeId | ua.QualifiedName):
        return value.to_string()
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC).isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, ua.StatusCode):
        return value.name
    return str(value)


def decode_value(value: Any, datatype: str, scale: float = 1.0) -> float | int | bool | str | None:
    """An OPC-UA value as the node's datatype, scaled; None stays None.

    Raises:
        ValueError: An integer out of the datatype's range
    """
    if value is None:
        return None

    if datatype == "bool":
        return bool(value)
    if datatype == "string":
        return render_text(value)
    if datatype in ("float32", "float64"):
        return float(value) * scale
    if datatype in INT_DATATYPES:
        # One out-of-range value would fail the whole event at emit. Unscaled
        # ints stay exact: a float has 53 bits.
        exact = scale == 1 and isinstance(value, int)
        out, (low, high) = int(value if exact else value * scale), INT_RANGES[datatype]
        if not low <= out <= high:
            raise ValueError(f"{out} out of range for {datatype}")
        return out
    return value


def encode_value(value: float | int | bool | str, datatype: str, scale: float = 1.0) -> Any:
    """A Python value for an OPC-UA write, divided by `scale`."""
    if datatype == "bool":
        return bool(value)
    if datatype == "string":
        return str(value)
    if datatype in ("float32", "float64"):
        return float(value) / scale if scale != 0 else float(value)
    if datatype in INT_DATATYPES:
        # Unscaled ints stay exact: a float has 53 bits.
        return int(value if scale in (0, 1) else value / scale)
    return value


def parse_node_id_to_ua(node_id_str: str, namespaces: Sequence[str] = ()) -> NodeId:
    """A node ID string as an asyncua NodeId; nsu= resolves against `namespaces`.

    Raises:
        ValueError: If the node ID is malformed, or its nsu= URI is not in `namespaces`
    """
    # parse_node_id types the identifier (a str g= or b= would never match).
    namespace, _, identifier = parse_node_id(node_id_str)
    if isinstance(namespace, str):
        if namespace not in namespaces:
            raise ValueError(f"Namespace URI '{namespace}' is not in the server's NamespaceArray")
        namespace = namespaces.index(namespace)
    return NodeId(identifier, namespace)


def uses_nsu(node_id: str) -> bool:
    """Whether a (valid) node ID is URI-qualified and so needs the NamespaceArray."""
    return isinstance(parse_node_id(node_id)[0], str)


def timestamp_ns(stamp: datetime | None) -> int | None:
    """An OPC UA DateTime as Unix ns; None when absent, null or out of range."""
    if stamp is None:
        return None
    stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
    if not _NULL_TIME <= stamp < _MAX_TIME:
        return None
    # Integer microseconds: a float of seconds drops sub-microsecond digits.
    return (stamp - _UNIX_EPOCH) // timedelta(microseconds=1) * 1000


def sample_time_ns(dv: ua.DataValue, received_ns: int, refresh: bool = False) -> int:
    """When a sample is logged: SourceTimestamp, else ServerTimestamp, else receipt.

    A refresh read skips SourceTimestamp: the value is confirmed current at the
    server's time, not at its last change.
    """
    source = None if refresh else timestamp_ns(dv.SourceTimestamp)
    return source or timestamp_ns(dv.ServerTimestamp) or received_ns


def is_connection_error(error: BaseException) -> bool:
    """Whether an exception means the transport or session is gone. Typed, never message text.

    A black-holed socket surfaces as a bare Exception whose __cause__ is the TimeoutError.
    """
    if isinstance(error, (OSError, TimeoutError)):  # ConnectionError is an OSError
        return True
    if isinstance(error, ua.UaStatusCodeError):
        return error.code in CONNECTION_STATUS_CODES
    return isinstance(error.__cause__, (TimeoutError, OSError))


def default_server_name(endpoint: str) -> str:
    """The endpoint host as a trace name: `plc01`, `192_168_1_10`."""
    host = urlparse(endpoint).hostname or endpoint
    return zelos_sdk.sanitize_name(host, kind="source")


class _ServerLog(logging.LoggerAdapter):
    """Prefixes every record with the server name; several servers share one log."""

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        return f"[{self.extra['server']}] {msg}", kwargs


#: Log event on the prefix source (`<prefix>/log`); a server may not take this name.
LOG_EVENT = "log"
#: Log source when the prefix is cleared.
LOG_SOURCE_NAME = "opcua_log"


def install_log_handler(source: zelos_sdk.TraceSource | str) -> TraceLoggingHandler:
    """Route INFO and above into the trace; DEBUG would flood it with library chatter."""
    handler = TraceLoggingHandler(source)
    handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(handler)
    return handler


class SharedSource:
    """A trace source and the clients writing it, rotated as one, with the log handler."""

    def __init__(
        self, source: zelos_sdk.TraceSource, log_handler: TraceLoggingHandler | None = None
    ) -> None:
        self.source = source
        self.clients: list[OPCUAClient] = []
        self.log_handler = log_handler


def _field(node: Node) -> tuple[str, zelos_sdk.DataType, str]:
    return node.name, SDK_DATATYPES.get(node.datatype, zelos_sdk.DataType.Float64), node.unit


# (event name, node definition, asyncua node)
Target = tuple[str, Node, UaNode]


@dataclass
class _Job:
    """A periodic request on the connection: health, a poll chunk, the sweep."""

    due: float  # monotonic
    period: float
    run: Callable[[], Awaitable[None]]


class OPCUAClient:
    """Subscribes to or polls one server's nodes into a trace source; runs in an OPCUARunner."""

    def __init__(
        self,
        endpoint: str = "opc.tcp://localhost:4840",
        name: str = "",
        security_mode: str = "None",
        security_policy: str = "None",
        timeout: float = 5.0,
        node_map: NodeMap | None = None,
        poll_interval: float = 1.0,
        certificate_file: str = "",
        private_key_file: str = "",
        server_certificate: str = "auto",
        server_certificate_file: str = "",
        user_certificate_file: str = "",
        user_private_key_file: str = "",
        downgrade_from: str = "",
        discovery: bool = True,
        transport: str = "subscription",
        min_update_interval: float = 60.0,
    ) -> None:
        """
        Args:
            endpoint: OPC-UA server endpoint URL
            name: Server name in the trace and the actions; default the endpoint host
            security_mode: None, Sign or SignAndEncrypt
            security_policy: None, Basic256Sha256, Aes128Sha256RsaOaep, Aes256Sha256RsaPss
            timeout: Request timeout in seconds
            node_map: Node map defining what to poll
            poll_interval: Polling interval in seconds
            certificate_file: Client certificate (DER or PEM); empty to generate one
            private_key_file: Client private key (DER or PEM), unencrypted
            server_certificate: auto accepts the server's certificate, strict pins it
            server_certificate_file: With strict, the only server certificate accepted
            user_certificate_file: X.509 user identity certificate; empty for Anonymous
            user_private_key_file: Private key for the user certificate
            downgrade_from: The secure default this server's None mode overrides;
                warned on every connect
            discovery: Without a node map, browse the server on every connect and
                trace every scalar Variable found
            transport: subscription (monitored items, polling what the server
                refuses) or poll
            min_update_interval: A subscribed item silent this long is re-read

        Raises:
            ValueError: If the security or transport settings are invalid
        """
        if transport not in TRANSPORTS:
            raise ValueError(f"transport must be one of {', '.join(TRANSPORTS)}, got '{transport}'")
        if min_update_interval <= 0:
            raise ValueError(f"min_update_interval must be positive, got {min_update_interval}")
        validate_security(
            security_mode,
            security_policy,
            certificate_file,
            private_key_file,
            server_certificate,
            server_certificate_file,
            user_certificate_file,
            user_private_key_file,
        )
        self.endpoint = endpoint
        self.name = name or default_server_name(endpoint)
        self._log = _ServerLog(logger, {"server": self.name})
        self.downgrade_from = downgrade_from
        self.security_mode = security_mode
        self.security_policy = security_policy
        self.timeout = timeout
        self.node_map = node_map
        # Replaces node_map on every connect; never written to disk.
        self.discovery = discovery and node_map is None
        self.poll_interval = poll_interval
        self.transport = transport
        self.min_update_interval = min_update_interval
        self.certificate_file = certificate_file
        self.private_key_file = private_key_file
        self.server_certificate_file = server_certificate_file
        self.user_certificate_file = user_certificate_file
        self.user_private_key_file = user_private_key_file
        self._pinned_server_cert = (
            load_cert_der(server_certificate_file) if server_certificate == "strict" else None
        )
        # (cert DER, key, application URI), resolved on the first secure connect.
        self._identity: tuple[bytes, uacrypto.CertProperties, str] | None = None
        self._cert_path: Path | None = None
        self._server_thumbprint: str | None = None

        self._client: Client | None = None
        # The runner's, bound in _run_async.
        self._stop_event: asyncio.Event | None = None
        self._running = False
        self._connected = False
        # Why the last connect failed; whether it was the server refusing our certificate.
        self.last_error: str | None = None
        self._cert_rejected = False
        self.ever_connected = False
        # In the runner's first connect: the runner reports a failure. Set when it is over.
        self._at_start = False
        self._contacted = asyncio.Event()
        self._poll_count = 0
        self._error_count = 0

        # Resolved once per connection, in map order, plus a by-ID index for the
        # read/write actions.
        self._poll_targets: list[Target] = []
        # Health fields still read this connection: (field, asyncua node).
        self._health_targets: list[tuple[str, UaNode]] = []
        # Per connection: targets by interval; client handle -> subscribed
        # target; handles in last-update order, oldest first (see _sweep); live
        # subscriptions; polled item count; the periodic requests.
        self._groups: dict[float, list[Target]] = {}
        self._monitored: dict[int, Target] = {}
        self._stale: OrderedDict[int, float] = OrderedDict()
        self._subscription_ids: list[int] = []
        self._polled = 0
        self._jobs: list[_Job] = []
        # A Publish failed to decode this connection; applied once setup is done
        # (see _poll_everything).
        self._subscribing = False
        self._publish_failed = False
        # Discovered node ids declared BaseDataType, and how many were polled for
        # it this connection (see _start_transport).
        self._variant: set[str] = set()
        self._polled_variant = 0
        # Nodes per Browse / Read / CreateMonitoredItems request, from OperationLimits.
        self._browse_chunk = self._read_chunk = self._monitor_chunk = MAX_OPERATIONS
        self._ua_nodes: dict[str, UaNode] = {}
        # Server NamespaceArray, read lazily once per connection: indexes are only
        # stable within a session, so nsu= IDs re-resolve after every reconnect.
        self._namespaces: list[str] | None = None
        # Node IDs that have already reported a read failure - see _log_node_failure.
        self._failed_nodes: set[str] = set()

        self._shared: SharedSource | None = None
        # Map event name -> trace event, captured from add_event. Never getattr on
        # the source: an event named `log` resolves to the source's own method.
        self._events: dict[str, Any] = {}
        # Event name -> declared fields, replayed on a rotated source.
        self._fields: dict[str, list[tuple[str, zelos_sdk.DataType, str]]] = {}
        # Declared field name -> datatype per node map event.
        self._schemas: dict[str, dict[str, str]] = {}
        self._event_prefix = ""
        self._writable_cache: dict[str, bool] = {}

    # ─── Setup ──────────────────────────────────────────────────────────────

    async def _create_client(self) -> Client:
        """The asyncua client with configured security.

        Raises:
            ConnectionSecurityError: If the requested security cannot be established
        """
        # The supervisor's 1s default watchdog drops sessions to slower servers.
        # Its auto_reconnect stays off: reconnect is ours.
        client = mark_unreachable(
            Client(url=self.endpoint, timeout=self.timeout, watchdog_intervall=self.timeout)
        )
        if self.security_mode != "None":
            await self._apply_security(client)

        if self.user_certificate_file:
            # asyncua's `load_client_certificate` is the USER identity; the app cert
            # went to set_security.
            key = _load_private_key(self.user_private_key_file)
            await client.load_client_certificate(
                load_cert_der(self.user_certificate_file), extension="der"
            )
            await client.load_private_key(key.path_or_content, extension=key.extension)

        return client

    def _client_identity(self) -> tuple[bytes, uacrypto.CertProperties, str]:
        """Client cert DER, key and application URI; generated on first need."""
        if self._identity is None:
            if self.certificate_file:
                cert_path, key_path = Path(self.certificate_file), Path(self.private_key_file)
            else:
                cert_path, key_path = ensure_client_certificate(PKI_DIR)
            cert_der = load_cert_der(cert_path)
            self._cert_path = cert_path.expanduser()
            self._identity = (cert_der, _load_private_key(key_path), _application_uri(cert_der))
            expires = x509.load_der_x509_certificate(cert_der).not_valid_after_utc
            self._log.info(
                "Client certificate %s (SHA-1 %s, expires %s); trust it on the server to "
                "allow secure sessions",
                cert_path.expanduser(),
                thumbprint(cert_der),
                expires.date().isoformat(),
            )
            if expires - datetime.now(UTC) < timedelta(days=CERT_EXPIRY_WARNING_DAYS):
                self._log.warning(
                    "Client certificate %s expires %s; servers will reject it after that. %s",
                    cert_path.expanduser(),
                    expires.date().isoformat(),
                    "Delete it to generate a new one, then trust the new one on the server"
                    if not self.certificate_file
                    else "Replace it and trust the new one on the server",
                )
        return self._identity

    async def _apply_security(self, client: Client) -> None:
        """Configure `client` for exactly the configured mode and policy, or raise.

        GetEndpoints is unauthenticated, so the pin compare is only the readable
        refusal; the real check is OPN encrypted to the certificate passed to
        set_security. With server_certificate=None asyncua would downgrade to None.
        """
        mode = SECURITY_MODES[self.security_mode]
        policy = SECURITY_POLICIES[self.security_policy]
        cert_der, key, client.application_uri = self._client_identity()

        endpoints = await client.connect_and_get_server_endpoints()
        endpoint = next(
            (
                ep
                for ep in endpoints
                if ep.EndpointUrl.startswith(ua.OPC_TCP_SCHEME)
                and ep.SecurityMode == mode
                and ep.SecurityPolicyUri == policy.URI
            ),
            None,
        )
        if endpoint is None:
            raise ConnectionSecurityError(
                f"server does not offer {self.security_mode}/{self.security_policy}; "
                f"it offers {offered_security(endpoints)}"
            )

        # asyncua invents a policy when the server has none for the token type.
        if self.user_certificate_file and not any(
            t.TokenType == ua.UserTokenType.Certificate for t in endpoint.UserIdentityTokens
        ):
            offered = sorted({t.TokenType.name for t in endpoint.UserIdentityTokens})
            raise ConnectionSecurityError(
                f"server does not accept user certificates on {self.security_mode}/"
                f"{self.security_policy}; it offers {', '.join(offered) or 'no user tokens'}"
            )

        # A chained ServerCertificate is trimmed to the leaf.
        server_cert = uacrypto.der_from_x509(uacrypto.x509_from_der(endpoint.ServerCertificate))
        actual = thumbprint(server_cert)
        if self._pinned_server_cert is not None and server_cert != self._pinned_server_cert:
            raise ConnectionSecurityError(
                f"server certificate SHA-1 {actual} does not match pinned "
                f"{thumbprint(self._pinned_server_cert)} ({self.server_certificate_file})"
            )
        if actual != self._server_thumbprint:
            self._server_thumbprint = actual
            self._log.info("Server certificate SHA-1 %s", actual)

        await client.set_security(
            policy,
            certificate=cert_der,
            private_key=key,
            server_certificate=server_cert,
            mode=mode,
        )

    @property
    def _source(self) -> zelos_sdk.TraceSource | None:
        return self._shared.source if self._shared else None

    def _init_trace_source(self, shared: SharedSource | None = None) -> None:
        """Declare the health event and one event per node map event.

        Args:
            shared: The prefix's shared source, events nested as `<server>/<event>`;
                None for a source of this server's own with unprefixed events
        """
        if shared is None:
            self._shared, self._event_prefix = SharedSource(zelos_sdk.TraceSource(self.name)), ""
        else:
            self._shared, self._event_prefix = shared, f"{self.name}/"
        if self not in self._shared.clients:
            self._shared.clients.append(self)
        self._events, self._fields, self._schemas = {}, {}, {}
        self._declare(HEALTH_EVENT, list(HEALTH_FIELDS))

        if self.node_map and self.node_map.events:
            self._declare_events(self.node_map)
        elif not self.discovery:
            self._declare(
                "raw",
                [
                    ("node_id", zelos_sdk.DataType.String, ""),
                    ("value", zelos_sdk.DataType.Float64, ""),
                ],
            )

    def _declare(self, event_name: str, fields: list[tuple[str, zelos_sdk.DataType, str]]) -> None:
        assert self._source is not None
        self._fields[event_name] = fields
        self._events[event_name] = self._source.add_event(
            f"{self._event_prefix}{event_name}",
            [zelos_sdk.TraceEventFieldMetadata(*f) for f in fields],
        )

    def _declare_events(
        self, node_map: NodeMap
    ) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        """Declare the map's new events; record fields added to declared ones.

        A declared schema is fixed on its source: an added field takes a rotation.
        A field whose datatype changed is left out of `node_map`.

        Returns:
            (event -> fields added, event -> fields left out for a type change)
        """
        added: dict[str, list[str]] = {}
        changed: dict[str, list[str]] = {}
        for event_name, nodes in node_map.events.items():
            if not nodes or self._source is None:
                continue
            schema = self._schemas.get(event_name)
            if schema is None:
                self._schemas[event_name] = {n.name: n.datatype for n in nodes}
                self._declare(event_name, [_field(n) for n in nodes])
                continue
            bad = [n for n in nodes if schema.get(n.name, n.datatype) != n.datatype]
            if bad:
                changed[event_name] = [n.name for n in bad]
                node_map.events[event_name] = [n for n in nodes if n not in bad]
            new = [n for n in nodes if n.name not in schema]
            if new:
                added[event_name] = [n.name for n in new]
                schema.update((n.name, n.datatype) for n in new)
                self._fields[event_name] = self._fields[event_name] + [_field(n) for n in new]
        return added, changed

    def _rotate_source(self) -> None:
        """Move every client on this source to a new segment of the same name.

        Synchronous: no await between the last write to the old source and the switch.
        """
        shared = self._shared
        assert shared is not None
        handler = shared.log_handler
        # The handler lock holds other threads' records until the log event is back.
        with handler.lock if handler else contextlib.nullcontext():
            name = shared.source.name
            shared.source.flush()
            # Release every reference to the old source and its events first, so its
            # segment ends before the new one starts.
            shared.source = None
            for client in shared.clients:
                client._events = {}
            if handler:
                handler.trace_source = handler.trace_event = None
            source = zelos_sdk.TraceSource(name)
            events = {
                client: {
                    name: source.add_event(
                        f"{client._event_prefix}{name}",
                        [zelos_sdk.TraceEventFieldMetadata(*f) for f in fields],
                    )
                    for name, fields in client._fields.items()
                }
                for client in shared.clients
            }
            shared.source = source
            for client, client_events in events.items():
                client._events = client_events
            if handler:
                handler.trace_source = source
                handler.trace_event = source.add_event(LOG_EVENT, schemas.Log)

    async def _discover(self) -> None:
        """Browse the server and replace node_map with what it holds now."""
        result = await discover(
            self._require_client(),
            await self._namespace_array(),
            self._browse_chunk,
            self._read_chunk,
            self.name,
            TRACE_NAME_BYTES - len(self._event_prefix.encode()),
        )
        added, changed = self._declare_events(result.node_map)
        self.node_map = result.node_map
        self._variant = result.variant
        skipped = ", ".join(f"{k} {v}" for k, v in sorted(result.skipped.items()))
        self._log.info(
            "Discovered %d variables in %d events (%.1fs, %d requests); skipped: %s",
            len(result.node_map.nodes),
            len(result.node_map.events),
            result.seconds,
            result.requests,
            skipped or "none",
        )
        if result.browse_failed:
            self._log.warning(
                "Browse failed, branches skipped or incomplete: %s", "; ".join(result.browse_failed)
            )
        if result.renames:
            self._log.warning("Discovered names collided; renamed: %s", "; ".join(result.renames))
        if changed:
            self._log.warning(
                "Discovered fields changed datatype, not traced until restart: %s",
                "; ".join(f"{e}: {', '.join(f)}" for e, f in changed.items()),
            )
        if added:
            self._rotate_source()
            self._log.info(
                "Discovered fields added, trace source '%s' rotated: %s",
                self._source.name if self._source else "",
                "; ".join(f"{e}: {', '.join(f)}" for e, f in added.items()),
            )

    async def _resolve_nodes(self) -> None:
        """Resolve every mapped node ID to a handle, on every connect: handles and
        nsu= indexes are per session. An unpublished URI skips its nodes (one ERROR);
        a guessed index would log another node's data under this name."""
        self._poll_targets = []
        self._ua_nodes = {}
        if not self.node_map or not self._client:
            return
        nodes = self.node_map.nodes
        namespaces = (
            await self._namespace_array() if any(uses_nsu(n.node_id) for n in nodes) else []
        )
        missing: dict[str, list[str]] = {}  # URI -> node names
        for event_name, event_nodes in self.node_map.events.items():
            for node in event_nodes:
                ns = node.namespace
                if isinstance(ns, str) and ns not in namespaces:
                    missing.setdefault(ns, []).append(node.name)
                    continue
                ua_node = self._client.get_node(parse_node_id_to_ua(node.node_id, namespaces))
                self._poll_targets.append((event_name, node, ua_node))
                self._ua_nodes[node.node_id] = ua_node
        for uri, names in missing.items():
            self._log.error(
                "Namespace URI '%s' not in the server's NamespaceArray; skipping nodes: %s",
                uri,
                ", ".join(names),
            )
        if nodes and not self._poll_targets:
            self._log.error("No mapped node resolved on %s; polling nothing", self.endpoint)

    # ─── Connection ─────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        """Connect, discover, resolve and subscribe; False on failure (logged)."""
        if self.downgrade_from:
            self._log.warning(
                "security_mode None overrides the advanced default %s: this session is "
                "neither signed nor encrypted",
                self.downgrade_from,
            )
        try:
            self._client = await self._create_client()
            await self._client.connect()
            self._connected = True
            self._namespaces = None
            (
                self._browse_chunk,
                self._read_chunk,
                self._monitor_chunk,
            ) = await operation_limits(self._client)
            if self.discovery:
                await self._discover()
            await self._resolve_nodes()
            self._health_targets = [
                (name, self._client.get_node(node_id)) for name, node_id in HEALTH_NODES
            ]
            # After discovery and any rotation: the events are declared.
            await self._start_transport()
            self._log.info("Connected to OPC-UA server: %s", self.endpoint)
            self.last_error, self.ever_connected = None, True
            return True
        except Exception as e:
            self._connected = False
            self.last_error, level = self._connect_failure(e)
            if not self._at_start:  # at start the runner reports it
                hint = "; trust it on the server" if self._cert_rejected else ""
                self._log.log(
                    level, "Connection to %s failed: %s%s", self.endpoint, self.last_error, hint
                )
            return False

    def _connect_failure(self, error: Exception) -> tuple[str, int]:
        """Why a connect failed, and the level to log it at: a security refusal is an ERROR."""
        self._cert_rejected = False
        if isinstance(error, Unreachable):
            return str(error), logging.WARNING
        if isinstance(error, ConnectionSecurityError):
            return str(error), logging.ERROR
        code = error.code if isinstance(error, ua.UaStatusCodeError) else None
        if code in CERT_REJECTED_CODES and self._identity is not None:
            self._cert_rejected = True
            return (
                f"server rejected this extension's client certificate {self._cert_path} "
                f"(SHA-1 {thumbprint(self._identity[0])})"
            ), logging.ERROR
        if code in USER_REJECTED_CODES:
            user = "anonymous login"
            if self.user_certificate_file:
                user_der = load_cert_der(self.user_certificate_file)
                user = f"user certificate SHA-1 {thumbprint(user_der)}"
            return f"server rejected {user} ({ua.StatusCode(code).name})", logging.ERROR
        return describe_error(error), logging.WARNING

    async def disconnect(self) -> None:
        """Close the session, ignoring errors from an already-dead socket."""
        if self._client:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
            self._connected = False
            if self.ever_connected:
                self._log.info("Disconnected from OPC-UA server")

    async def _ensure_connected(self) -> bool:
        """Connect if not connected. No separate liveness probe: the health Read is one."""
        if self._connected and self._client:
            return True
        if self._client:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
        self._connected = False
        self._log.info("Connecting to %s...", self.endpoint)
        return await self.connect()

    def _require_client(self) -> Client:
        if not self._client or not self._connected:
            raise RuntimeError(f"Not connected to {self.endpoint}")
        return self._client

    async def _namespace_array(self) -> list[str]:
        """The server's NamespaceArray, read once per connection."""
        if self._namespaces is None:
            self._namespaces = await self._require_client().get_namespace_array()
        return self._namespaces

    async def _ua_node(self, node_id: str) -> UaNode:
        """Cached node handle, or an ad-hoc one for IDs outside the map."""
        client = self._require_client()
        cached = self._ua_nodes.get(node_id)
        if cached is not None:
            return cached
        namespaces = await self._namespace_array() if uses_nsu(node_id) else []
        return client.get_node(parse_node_id_to_ua(node_id, namespaces))

    # ─── Read / write ───────────────────────────────────────────────────────

    async def read_node(self, node_id: str) -> Any:
        """Read a raw value by node ID. Raises on protocol error."""
        return await (await self._ua_node(node_id)).read_value()

    async def write_node(self, node_id: str, value: Any, sent: list[float] | None = None) -> None:
        """Write a value by node ID: two round trips, each bounded by `self.timeout`.

        `sent` gets the monotonic time the Write goes out: past it, a timeout
        cannot say whether it applied.
        """
        node = await self._ua_node(node_id)
        # A bare Python value lets asyncua guess (int -> Int64): BadTypeMismatch.
        dv = await node.read_data_value()
        variant_type = dv.Value.VariantType if dv.Value else None
        if isinstance(value, str) and variant_type in VARIANT_DATATYPES:
            value = coerce_text(value, VARIANT_DATATYPES[variant_type])
        variant = ua.Variant(value, variant_type) if variant_type else ua.Variant(value)
        if sent is not None:
            sent.append(time.monotonic())
        await node.write_value(variant)

    async def read_node_value(self, node: Node) -> float | int | bool | str | None:
        """Read and decode a node using its map definition."""
        return decode_value(await self.read_node(node.node_id), node.datatype, node.scale)

    async def write_node_value(
        self, node: Node, value: float | int | bool | str, sent: list[float] | None = None
    ) -> None:
        """Encode and write a node using its map definition.

        Raises:
            ValueError: If the node is marked, or detected, as read-only
        """
        if node.writable is False:
            raise ValueError(f"Node '{node.name}' is not writable")

        if node.writable is None:
            if node.node_id not in self._writable_cache:
                self._writable_cache[node.node_id] = await self._check_writable(node.node_id)
            if not self._writable_cache[node.node_id]:
                raise ValueError(f"Node '{node.name}' is not writable (auto-detected)")

        await self.write_node(node.node_id, encode_value(value, node.datatype, node.scale), sent)

    async def _check_writable(self, node_id: str) -> bool:
        """The CurrentWrite bit; True when unreadable, or a server hiding it blocks every write."""
        try:
            ua_node = await self._ua_node(node_id)
            access_level = await ua_node.read_attribute(ua.AttributeIds.AccessLevel)
            return bool(access_level.Value.Value & 0x02)  # bit 1 = CurrentWrite
        except Exception:
            return True

    async def browse_children(self, start_node_id: str, max_depth: int) -> list[dict[str, Any]]:
        """Walk the address space from a starting node."""
        results: list[dict[str, Any]] = []
        start = await self._ua_node(start_node_id)
        namespaces = await self._namespace_array()
        await self._browse_recursive(start, namespaces, results, max_depth, 0)
        return results

    async def _browse_recursive(
        self,
        node: UaNode,
        namespaces: list[str],
        results: list[dict[str, Any]],
        max_depth: int,
        depth: int,
    ) -> None:
        if depth >= max_depth:
            return

        for child in await node.get_children():
            try:
                browse_name = await child.read_browse_name()
                node_class = await child.read_node_class()
            except Exception as e:
                self._log.debug("Skipping child of %s: %s", node.nodeid.to_string(), e)
                continue

            info: dict[str, Any] = {
                "node_id": child.nodeid.to_string(),
                # Stable across server restarts, unlike the ns= index; copy this into a map.
                "nsu_node_id": to_nsu_string(child.nodeid, namespaces),
                "browse_name": f"{browse_name.NamespaceIndex}:{browse_name.Name}",
                "node_class": node_class.name,
                "depth": depth,
            }
            if node_class == ua.NodeClass.Variable:
                with contextlib.suppress(Exception):
                    dv = await child.read_data_value()
                    if dv.Value:
                        info["value_type"] = dv.Value.VariantType.name
            results.append(info)

            if node_class in (ua.NodeClass.Object, ua.NodeClass.ObjectType):
                await self._browse_recursive(child, namespaces, results, max_depth, depth + 1)

    # ─── Transport ──────────────────────────────────────────────────────────

    async def _start_transport(self) -> None:
        """Subscribe or poll every resolved node, and schedule this connection's jobs.

        One subscription per distinct interval; what the server refuses is polled
        for the connection (one WARNING). BaseDataType nodes are polled: asyncua
        drops a whole Publish over one value it cannot decode.
        """
        intervals = self.node_map.intervals if self.node_map else {}
        groups: dict[float, list[tuple[int, Target]]] = {}
        for handle, target in enumerate(self._poll_targets, 1):
            groups.setdefault(intervals.get(target[0], self.poll_interval), []).append(
                (handle, target)
            )
        self._groups = {i: [t for _, t in items] for i, items in groups.items()}
        client = self._require_client()
        self._monitored, self._stale, self._subscription_ids = {}, OrderedDict(), []
        self._subscribing, self._publish_failed = True, False
        on_publish = self._publish_callback(client, self._monitored, self._stale)
        if self.transport == "subscription":
            self._guard_publish(client)
        polled: dict[float, list[Target]] = {}
        refused: Counter[str] = Counter()
        full: str | None = None
        self._polled_variant = 0
        for interval, items in groups.items():
            if self.transport == "poll":
                polled[interval] = [t for _, t in items]
                continue
            variant = [t for _, t in items if t[1].node_id in self._variant]
            if variant:
                polled[interval] = variant
                self._polled_variant += len(variant)
                items = [(h, t) for h, t in items if t[1].node_id not in self._variant]
                if not items:
                    continue
            rejected, full = await self._subscribe(client, interval, items, on_publish, full)
            for target, reason in rejected:
                polled.setdefault(interval, []).append(target)
                refused[reason] += 1
        if refused:
            self._log.warning(
                "Server refused %d of %d monitored items (%s); polling those this connection",
                refused.total(),
                len(self._poll_targets),
                ", ".join(f"{name} {n}" for name, n in refused.most_common()),
            )
        if self._polled_variant:
            self._log.info(
                "Polling %d BaseDataType nodes this connection: their value type may change",
                self._polled_variant,
            )
        self._polled = sum(len(t) for t in polled.values())
        self._jobs = self._schedule(polled)
        self._subscribing = False
        if self._publish_failed:
            await self._poll_everything(client)

    async def _subscribe(
        self,
        client: Client,
        interval: float,
        items: list[tuple[int, Target]],
        on_publish: Callable[[ua.PublishResult], None],
        full: str | None,
    ) -> tuple[list[tuple[Target, str]], str | None]:
        """One subscription publishing and sampling at `interval`.

        Args:
            full: A status from an earlier refusal meaning the server takes no more

        Returns:
            (refused (target, status name) pairs, `full` as it now stands)
        """
        if full:
            return [(t, full) for _, t in items], full
        ms = interval * 1000.0
        keepalive = max(1, math.ceil(KEEPALIVE_SECONDS / interval))
        params = ua.CreateSubscriptionParameters(
            RequestedPublishingInterval=ms,
            RequestedLifetimeCount=keepalive * LIFETIME_KEEPALIVES,
            RequestedMaxKeepAliveCount=keepalive,
            MaxNotificationsPerPublish=0,
            PublishingEnabled=True,
            Priority=0,
        )
        try:
            subscription = await client.uaclient.create_subscription(params, on_publish)
        except ua.UaStatusCodeError as e:
            if is_connection_error(e):
                raise
            name = ua.StatusCode(e.code).name
            return [(t, name) for _, t in items], name if e.code in SERVER_FULL_CODES else None
        self._subscription_ids.append(subscription.SubscriptionId)

        change = ua.DataChangeFilter(Trigger=ua.DataChangeTrigger.StatusValue)
        refused: list[tuple[Target, str]] = []
        for start in range(0, len(items), self._monitor_chunk):
            chunk = items[start : start + self._monitor_chunk]
            if full:
                refused += [(t, full) for _, t in chunk]
                continue
            request = ua.CreateMonitoredItemsParameters(
                SubscriptionId=subscription.SubscriptionId,
                TimestampsToReturn=ua.TimestampsToReturn.Both,
                ItemsToCreate=[
                    ua.MonitoredItemCreateRequest(
                        ItemToMonitor=ua.ReadValueId(
                            NodeId=target[2].nodeid, AttributeId=ua.AttributeIds.Value
                        ),
                        MonitoringMode=ua.MonitoringMode.Reporting,
                        RequestedParameters=ua.MonitoringParameters(
                            ClientHandle=handle,
                            SamplingInterval=ms,
                            Filter=change,
                            QueueSize=1,
                            DiscardOldest=True,
                        ),
                    )
                    for handle, target in chunk
                ],
            )
            # Registered first: the initial values can be published before the reply.
            self._monitored.update(chunk)
            try:
                results = await client.uaclient.create_monitored_items(request)
            except ua.UaStatusCodeError as e:
                if is_connection_error(e):
                    raise
                results = [
                    ua.MonitoredItemCreateResult(StatusCode=ua.StatusCode(e.code)) for _ in chunk
                ]
            now = time.monotonic()
            for (handle, target), result in zip(chunk, results, strict=True):
                if result.StatusCode.is_good():
                    self._stale[handle] = now
                    continue
                del self._monitored[handle]
                name = result.StatusCode.name
                refused.append((target, name))
                if result.StatusCode.value in SERVER_FULL_CODES:
                    full = name
        if len(refused) == len(items):
            # An empty subscription still holds one of the server's few slots.
            with contextlib.suppress(ua.UaStatusCodeError):
                await client.uaclient.delete_subscriptions([subscription.SubscriptionId])
                self._subscription_ids.remove(subscription.SubscriptionId)
        return refused, full

    def _guard_publish(self, client: Client) -> None:
        """Poll everything for the connection once a Publish fails to decode.

        asyncua drops the whole response without naming the item; polling isolates it.
        """
        session = client.uaclient.session
        publish = session.publish

        async def guarded(acks: list[ua.SubscriptionAcknowledgement]) -> ua.PublishResponse:
            try:
                return await publish(acks)
            except UaStructParsingError:
                if client is self._client:
                    self._publish_failed = True
                    await self._poll_everything(client)
                raise

        session.publish = guarded

    async def _poll_everything(self, client: Client) -> None:
        """Delete this connection's subscriptions and poll every target.

        Deferred while `_start_transport` subscribes: its jobs would replace these,
        leaving chunks already subscribed neither subscribed nor polled.
        """
        if self._subscribing or not self._monitored:
            return
        self._log.warning(
            "A Publish response could not be decoded; polling all %d subscribed items "
            "this connection",
            len(self._monitored),
        )
        self._monitored.clear()
        self._stale.clear()
        self._polled = len(self._poll_targets)
        self._jobs = self._schedule(self._groups)
        ids, self._subscription_ids = self._subscription_ids, []
        # Best effort: a failure here is the connection's, seen by the next request.
        with contextlib.suppress(Exception):
            await client.uaclient.delete_subscriptions(ids)

    def _publish_callback(
        self, client: Client, monitored: dict[int, Target], stale: OrderedDict[int, float]
    ) -> Callable[[ua.PublishResult], None]:
        """This connection's Publish handler: a late response from an old session
        cannot resolve a new handle. asyncua awaits it before the next Publish."""

        def on_publish(result: ua.PublishResult) -> None:
            received = time.time_ns()
            now = time.monotonic()
            samples = []
            for notification in result.NotificationMessage.NotificationData or []:
                if isinstance(notification, ua.DataChangeNotification):
                    for item in notification.MonitoredItems:
                        target = monitored.get(item.ClientHandle)
                        if target is None:
                            continue
                        samples.append((target[0], target[1], item.Value))
                        stale[item.ClientHandle] = now
                        stale.move_to_end(item.ClientHandle)
                elif (
                    isinstance(notification, ua.StatusChangeNotification)
                    and not notification.Status.is_good()
                    and client is self._client
                ):
                    # Timed out server-side, or asyncua's supervisor saw the link go.
                    self._log.warning(
                        "Subscription %d ended (%s), reconnecting",
                        result.SubscriptionId,
                        notification.Status.name,
                    )
                    self._connected = False
            self._log_samples(samples, received)

        return on_publish

    def _schedule(self, polled: dict[float, list[Target]]) -> list[_Job]:
        """This connection's periodic requests.

        An interval's chunks are spread evenly across it; never smaller chunks:
        server cost follows request count.
        """
        now = time.monotonic()
        jobs = [_Job(now, self.poll_interval, self._poll_health)]
        for interval, targets in polled.items():
            chunks = [
                targets[i : i + self._read_chunk] for i in range(0, len(targets), self._read_chunk)
            ]
            step = interval / len(chunks)
            jobs += [
                _Job(now + i * step, interval, lambda c=chunk: self._poll_chunk(c))
                for i, chunk in enumerate(chunks)
            ]
        if self._monitored:
            chunks = -(-len(self._monitored) // self._read_chunk)
            step = self.min_update_interval / max(SWEEP_STEPS, chunks)
            jobs.append(_Job(now + step, step, self._sweep))
        return jobs

    async def _poll_health(self) -> None:
        self._log_values({HEALTH_EVENT: await self._read_health()})
        self._poll_count += 1

    async def _poll_chunk(self, targets: list[Target]) -> None:
        self._log_samples(await self._read_targets(targets), time.time_ns())

    async def _sweep(self) -> None:
        """Re-read up to one Read of subscribed items silent for min_update_interval.

        `_stale` is in last-update order, so the due items are the front run: no scan.
        """
        now = time.monotonic()
        due = [
            handle
            for handle, _ in itertools.islice(
                itertools.takewhile(
                    lambda item: now - item[1] >= self.min_update_interval, self._stale.items()
                ),
                self._read_chunk,
            )
        ]
        if not due:
            return
        samples = await self._read_targets([self._monitored[h] for h in due])
        received, now = time.time_ns(), time.monotonic()
        for handle in due:
            if handle in self._stale:  # not if everything moved to polling meanwhile
                self._stale[handle] = now
                self._stale.move_to_end(handle)
        self._log_samples(samples, received, refresh=True)

    async def _read_health(self) -> dict[str, Any]:
        """Health fields from one Read; the request midpoint is the host time for skew."""
        if not self._health_targets:
            return {}
        sent = time.time()
        items = [(n.nodeid, None) for _, n in self._health_targets]
        data_values = await read_many(self._require_client(), items, self._read_chunk)
        return self._health_values(data_values, (sent + time.time()) / 2)

    async def _read_targets(self, targets: list[Target]) -> list[tuple[str, Node, ua.DataValue]]:
        """(event name, node, DataValue) per target, `_read_chunk` per request.

        A BadDecodingError item is removed from `targets`, or its chunk would be
        re-read item by item every cycle.
        """
        data_values = await read_many(
            self._require_client(), [(t[2].nodeid, None) for t in targets], self._read_chunk
        )
        samples = [(t[0], t[1], dv) for t, dv in zip(targets, data_values, strict=True)]
        undecodable = {
            t[1].node_id
            for t, dv in zip(targets, data_values, strict=True)
            if dv.StatusCode is not None and dv.StatusCode.value == ua.StatusCodes.BadDecodingError
        }
        if undecodable:
            targets[:] = [t for t in targets if t[1].node_id not in undecodable]
        return samples

    async def _poll_nodes(self) -> dict[str, dict[str, Any]]:
        """Health and every mapped node, read now and returned unlogged.

        Returns:
            {event_name: {field_name: value}}
        """
        results: dict[str, dict[str, Any]] = {}
        if self._health_targets:
            results[HEALTH_EVENT] = await self._read_health()
        for event_name, node, dv in await self._read_targets(self._poll_targets):
            value = self._decode(node, dv)
            if value is not None:
                results.setdefault(event_name, {})[node.name] = value
        return results

    def _decode(self, node: Node, dv: ua.DataValue) -> Any:
        """A sample's value in its node's datatype; None if Bad, empty or undecodable."""
        status = dv.StatusCode
        if status is not None and not status.is_good():
            self._log_node_failure(node, status.name)
            return None
        raw = dv.Value.Value if dv.Value else None
        if raw is None:
            return None
        try:
            return decode_value(raw, node.datatype, node.scale)
        except (TypeError, ValueError) as e:
            self._log_node_failure(node, f"decode failed: {describe_error(e)}")
            return None

    def _log_samples(
        self,
        samples: Iterable[tuple[str, Node, ua.DataValue]],
        received_ns: int,
        refresh: bool = False,
    ) -> None:
        """Log samples at `sample_time_ns`, one row per event and time: merged, all
        but one field would be logged at a time it was not sampled."""
        rows: dict[tuple[str, int], dict[str, Any]] = {}
        for event_name, node, dv in samples:
            value = self._decode(node, dv)
            if value is not None:
                key = (event_name, sample_time_ns(dv, received_ns, refresh))
                rows.setdefault(key, {})[node.name] = value
        for (event_name, stamp), fields in sorted(rows.items(), key=lambda row: row[0][1]):
            event = self._events.get(event_name)
            if event is not None:
                event.log_at(stamp, **fields)

    def _health_values(self, data_values: list[ua.DataValue], host_time: float) -> dict[str, Any]:
        """Health fields; a node Bad or empty is dropped for the connection (one DEBUG)."""
        values: dict[str, Any] = {}
        missing = []
        for (name, _), dv in zip(self._health_targets, data_values, strict=True):
            raw = value_of(dv)
            if raw is None:
                missing.append(name)
                continue
            if name == "state":
                values["state"] = int(raw)
                with contextlib.suppress(ValueError):
                    values["state_name"] = ua.ServerState(int(raw)).name
            elif isinstance(raw, datetime):
                stamp = (raw if raw.tzinfo else raw.replace(tzinfo=UTC)).timestamp()
                values[name] = int(stamp * 1e9)
                if name == "current_time":
                    values["clock_skew_ms"] = (stamp - host_time) * 1000.0
            else:
                values[name] = int(raw)
        if missing:
            self._log.debug("Server does not publish health fields: %s", ", ".join(missing))
            self._health_targets = [t for t in self._health_targets if t[0] not in missing]
        return values

    def _log_node_failure(self, node: Node, reason: str) -> None:
        """One ERROR per bad node per process: per cycle would flood the log and the trace."""
        if node.node_id in self._failed_nodes:
            return
        self._failed_nodes.add(node.node_id)
        self._log.error(
            "Node '%s' (%s) unreadable: %s - further reports suppressed",
            node.name,
            node.node_id,
            reason,
        )

    def _log_values(self, values: dict[str, dict[str, Any]]) -> None:
        """Log one row per event at host time: the health event."""
        for event_name, event_values in values.items():
            event = self._events.get(event_name)
            if event is not None and event_values:
                event.log(**event_values)

    # ─── Lifecycle ──────────────────────────────────────────────────────────

    def start(self, shared: SharedSource | None = None) -> None:
        """Declare the trace events; `shared` is the prefix source, None for the server's own."""
        self._running = True
        self._init_trace_source(shared)
        self._log.info("Client started (%s)", self.endpoint)

    async def _run_async(self, stop_event: asyncio.Event) -> None:
        """Run this connection's jobs, one request at a time, until `stop_event`.

        State is per client: one dead server never stalls another in the loop.
        """
        self._stop_event = stop_event
        SERVER.set(self.name)
        self._at_start = not self.ever_connected
        backoff = RECONNECT_INITIAL
        losses = 0  # connection losses since the last completed request
        failures = 0  # unclassified failures since the last completed request

        try:
            while self._running and not stop_event.is_set():
                # Raced: a black-holed connect sits far past the manifest's grace.
                _, connected = await self._or_stop(self._ensure_connected())
                first, self._at_start = self._at_start, False
                self._contacted.set()
                if not connected:
                    if stop_event.is_set() or first:
                        break  # at start the runner stops the extension
                    self._log.warning("Retrying %s in %.0fs", self.endpoint, backoff)
                    if await self._wait_or_stop(backoff):
                        break
                    backoff = min(backoff * 2, RECONNECT_MAX)
                    continue

                # A handful of jobs (one per 100 polled items): min(), no heap.
                job = min(self._jobs, key=lambda j: j.due)
                if await self._wait_or_stop(max(0.0, job.due - time.monotonic())):
                    break
                if not self._connected:
                    continue  # a subscription ended during the wait
                # Behind schedule, the next run starts from now rather than bursting.
                job.due = max(job.due + job.period, time.monotonic())
                try:
                    # Raced like connect: a hung request would outlast the shutdown bound.
                    if (await self._or_stop(job.run()))[0]:
                        break
                    # Only a completed request proves the link; a connect that
                    # never yields data must not clear the backoff.
                    backoff = RECONNECT_INITIAL
                    losses = 0
                    failures = 0
                except Exception as e:
                    self._error_count += 1
                    if is_connection_error(e):
                        self._log.warning(
                            "Connection to %s lost: %s", self.endpoint, describe_error(e)
                        )
                        self._connected = False
                        losses += 1
                        # The first loss reconnects at once; from the second, back off,
                        # or a server that drops every new session would spin.
                        if losses > 1:
                            if await self._wait_or_stop(backoff):
                                break
                            backoff = min(backoff * 2, RECONNECT_MAX)
                        continue
                    self._log.error("Poll failed: %s", describe_error(e))
                    failures += 1
                    if failures >= POLL_FAILURES_BEFORE_RECONNECT:
                        self._log.warning("%d poll failures in a row, reconnecting", failures)
                        self._connected = False
                        failures = 0
        finally:
            self._at_start = False
            self._contacted.set()
            self._running = False
            try:
                await asyncio.wait_for(self.disconnect(), SHUTDOWN_TIMEOUT)
            except TimeoutError:
                self._log.warning("Disconnect exceeded %.0fs, abandoning session", SHUTDOWN_TIMEOUT)

    async def _or_stop(self, coro: Awaitable[Any]) -> tuple[bool, Any]:
        """(True, None) if shutdown wins, `coro` cancelled; else (False, its result)."""
        assert self._stop_event is not None
        work = asyncio.ensure_future(coro)
        stop = asyncio.ensure_future(self._stop_event.wait())
        try:
            await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
            if work.done():
                return False, work.result()
            return True, None
        finally:
            stop.cancel()
            if not work.done():
                work.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await work

    async def _wait_or_stop(self, seconds: float) -> bool:
        """Wait up to `seconds`; True if shutdown was requested meanwhile."""
        assert self._stop_event is not None
        try:
            await asyncio.wait_for(self._stop_event.wait(), seconds)
        except TimeoutError:
            return False
        return True

    # ─── Action support ─────────────────────────────────────────────────────

    def known_writable_nodes(self) -> list[Node]:
        """Nodes declared writable, plus those auto-detection has proven writable so far."""
        if not self.node_map:
            return []
        return [
            n
            for n in self.node_map.nodes
            if n.writable is True
            or (n.writable is None and self._writable_cache.get(n.node_id, False))
        ]

    @property
    def state(self) -> str:
        """ok, connecting (the first connect) or disconnected (was up, retrying)."""
        if self._connected:
            return "ok"
        return "disconnected" if self.ever_connected else "connecting"

    def status(self) -> dict[str, Any]:
        """Connection and polling counters."""
        return {
            "server": self.name,
            "connected": self._connected,
            "state": self.state,
            "last_error": self.last_error,
            "endpoint": self.endpoint,
            "security_mode": self.security_mode,
            "security_policy": self.security_policy,
            "poll_count": self._poll_count,
            "error_count": self._error_count,
            "poll_interval": self.poll_interval,
            "transport": self.transport,
            "min_update_interval": self.min_update_interval,
            "subscribed": len(self._monitored),
            "polled": self._polled,
            "polled_variant": self._polled_variant,
            "nodes": len(self.node_map.nodes) if self.node_map else 0,
            "discovery": self.discovery,
            "peak_rss_mb": rss >> 20 if (rss := peak_rss_bytes()) is not None else None,
        }


class OPCUARunner:
    """The one event loop every client polls in: signals, stop, action dispatch.

    Disconnects run concurrently: shutdown costs SHUTDOWN_TIMEOUT in total, not per server.
    """

    def __init__(self, clients: Sequence[OPCUAClient]) -> None:
        """`clients`: one per server, names unique (checked at config load)."""
        self.clients = {c.name: c for c in clients}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event = asyncio.Event()
        # Servers whose first connect failed: the caller exits 1.
        self.failed_at_start: list[OPCUAClient] = []

    def is_running(self) -> bool:
        """Whether the polling loop is up."""
        return self._loop is not None and self._loop.is_running()

    def run(self) -> None:
        """Run every client until shutdown (blocking)."""
        asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        self._loop = asyncio.get_running_loop()
        remove_signal_handlers = self._install_signal_handlers()
        tasks = [asyncio.create_task(c._run_async(self._stop_event)) for c in self.clients.values()]
        watchdog = asyncio.create_task(watch_loop(self._describe))
        try:
            if await self._failed_at_start():
                return
            await asyncio.gather(*tasks)
        finally:
            watchdog.cancel()
            # A client that raised is a bug; stop the rest cleanly.
            self._stop_event.set()
            await asyncio.gather(*tasks, watchdog, return_exceptions=True)
            remove_signal_handlers()

    async def _failed_at_start(self) -> bool:
        """After every client's first connect, log each server that failed it.

        First contact is a hard requirement: a server that cannot be reached or
        refuses us at start is a config error, fixed before starting again. Once a
        server has connected, drops go through the reconnect backoff.
        """
        first = asyncio.gather(*(c._contacted.wait() for c in self.clients.values()))
        stop = asyncio.ensure_future(self._stop_event.wait())
        try:
            await asyncio.wait({first, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            first.cancel()
            stop.cancel()
        if self._stop_event.is_set():
            return False
        self.failed_at_start = [c for c in self.clients.values() if not c._connected]
        for c in self.failed_at_start:
            hint = (
                "; trust it on the server, then start the extension again"
                if c._cert_rejected
                else ""
            )
            logger.error(
                "Server '%s' (%s): cannot connect: %s%s", c.name, c.endpoint, c.last_error, hint
            )
        return bool(self.failed_at_start)

    def _describe(self) -> str:
        return ", ".join(
            f"{c.name}: {len(c.node_map.nodes) if c.node_map else 0} nodes, "
            f"{len(c._monitored)} subscribed"
            for c in self.clients.values()
        )

    def stop(self) -> None:
        """Request shutdown. Safe from any thread, including a signal handler."""
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._stop_event.set)
        else:
            self._stop_event.set()
        logger.info("OPC-UA extension stopping")

    def _install_signal_handlers(self) -> Callable[[], None]:
        """Set the stop event on SIGTERM/SIGINT; returns the uninstaller.

        sys.exit from a signal.signal handler would drop the sessions uncleanly.
        """
        loop, event = self._loop, self._stop_event
        if loop is None:
            return lambda: None

        def request_stop() -> None:
            logger.info("Shutdown signal received, stopping OPC-UA extension")
            event.set()

        installed: list[int] = []
        replaced: dict[int, Any] = {}  # signal -> the handler we displaced
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, request_stop)
                installed.append(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                # Windows, or off the main thread: signal.signal where it works,
                # else the owner calls stop().
                with contextlib.suppress(ValueError, OSError):
                    replaced[sig] = signal.signal(
                        sig, lambda *_: loop.call_soon_threadsafe(request_stop)
                    )

        def remove() -> None:
            for sig in installed:
                with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                    loop.remove_signal_handler(sig)
            # A stopped runner must not handle signals into a dead loop. A
            # displaced None was not set from Python.
            for sig, prior in replaced.items():
                with contextlib.suppress(ValueError, OSError, TypeError):
                    signal.signal(sig, prior if prior is not None else signal.SIG_DFL)

        return remove

    def _run_coro(self, coro: Coroutine[Any, Any, Any], timeout: float) -> Any:
        """Run an action's coroutine in the live polling loop, on its session.

        No ad-hoc `asyncio.run` fallback: mutating client state from a foreign loop
        silently lost every later sample.

        Raises:
            RuntimeError: If the polling loop is not running
            TimeoutError: If the coroutine outlives `timeout`, having cancelled it
                (which stops a request not yet sent, never one already sent)
        """
        if self._loop is None or not self._loop.is_running():
            coro.close()
            raise RuntimeError("extension is not running")

        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise
