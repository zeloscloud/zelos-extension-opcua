"""Core OPC-UA client wrapper with Zelos SDK integration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Callable, Coroutine
from typing import Any

import zelos_sdk
from asyncua import Client, ua
from asyncua.common.node import Node as UaNode
from asyncua.ua.uatypes import NodeId

from zelos_extension_opcua.node_map import Node, NodeMap, parse_node_id

logger = logging.getLogger(__name__)

# Reconnect backoff: retry fast at first, then back off so a server that is down
# for hours costs one attempt a minute instead of one every three seconds.
RECONNECT_INITIAL = 3.0
RECONNECT_MAX = 60.0

# Ceiling on the disconnect at shutdown. A wedged session must not make the
# process unkillable; past this we give up and let the socket die with us.
SHUTDOWN_TIMEOUT = 3.0

# Nodes per read request. Servers cap MaxNodesPerRead and reject the whole
# request past it; chunking beats reading OperationLimits from every server.
READ_CHUNK = 100

# Consecutive whole-poll failures that force a reconnect. An error we do not
# classify as connection loss would otherwise repeat forever on a dead session.
POLL_FAILURES_BEFORE_RECONNECT = 5

# Status codes that mean the session or transport is gone, not that one node is
# bad. A poll that comes back with any of these triggers a reconnect.
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

# Server variant type -> node map datatype, for coercing text written to a node
# that is not in the map (write_node) and so has no declared datatype.
VARIANT_DATATYPES = {
    ua.VariantType.Boolean: "bool",
    ua.VariantType.SByte: "int8",
    ua.VariantType.Byte: "uint8",
    ua.VariantType.Int16: "int16",
    ua.VariantType.UInt16: "uint16",
    ua.VariantType.Int32: "int32",
    ua.VariantType.UInt32: "uint32",
    ua.VariantType.Int64: "int64",
    ua.VariantType.UInt64: "uint64",
    ua.VariantType.Float: "float32",
    ua.VariantType.Double: "float64",
    ua.VariantType.String: "string",
}

BOOL_TRUE = frozenset({"true", "1", "on", "yes"})
BOOL_FALSE = frozenset({"false", "0", "off", "no"})


def describe_error(error: BaseException) -> str:
    """Message, or the class name when there is no message.

    asyncio.TimeoutError and friends stringify to nothing, which logs as a line
    that names no failure at all.
    """
    return str(error) or type(error).__name__


def coerce_text(text: str, datatype: str) -> float | int | bool | str:
    """Parse an action's text input into a node's datatype.

    Write actions take text, not a number, so bool and string nodes are
    writable at all. Numeric text is parsed as a float and truncated for integer
    types, matching what `encode_value` does with a scale.

    Args:
        text: Raw text from the action parameter
        datatype: Node map data type string

    Returns:
        The parsed value

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

    try:
        number = float(value)
    except ValueError:
        raise ValueError(f"Cannot parse '{text}' as {datatype}") from None
    return number if datatype in ("float32", "float64") else int(number)


def decode_value(value: Any, datatype: str, scale: float = 1.0) -> float | int | bool | str | None:
    """Decode an OPC-UA value to a typed, scaled Python value.

    Args:
        value: Raw OPC-UA value
        datatype: Node map data type string
        scale: Scale factor to apply

    Returns:
        Decoded and scaled value, or None if the input was None
    """
    if value is None:
        return None

    if datatype == "bool":
        return bool(value)
    if datatype == "string":
        return str(value)
    if datatype in ("float32", "float64"):
        return float(value) * scale
    if datatype in INT_DATATYPES:
        return int(value * scale)
    return value


def encode_value(value: float | int | bool | str, datatype: str, scale: float = 1.0) -> Any:
    """Encode a Python value for an OPC-UA write.

    Args:
        value: Value to write
        datatype: Node map data type string
        scale: Scale factor (the value is divided by it)

    Returns:
        Encoded value
    """
    if datatype == "bool":
        return bool(value)
    if datatype == "string":
        return str(value)
    if datatype in ("float32", "float64"):
        return float(value) / scale if scale != 0 else float(value)
    if datatype in INT_DATATYPES:
        return int(value / scale if scale != 0 else value)
    return value


def parse_node_id_to_ua(node_id_str: str) -> NodeId:
    """Convert a node ID string to an asyncua NodeId.

    Args:
        node_id_str: Node ID in the form ns=X;[s|i|g|b]=Y

    Returns:
        asyncua NodeId

    Raises:
        ValueError: If the node ID is malformed
    """
    # node_map.parse_node_id is the one implementation: it already returns the
    # identifier as the Python type NodeId infers its NodeIdType from (a str for
    # g= or b= would send a String identifier that no server matches), so map
    # validation and this conversion cannot drift apart.
    namespace, _, identifier = parse_node_id(node_id_str)
    return NodeId(identifier, namespace)


def is_connection_error(error: BaseException) -> bool:
    """Whether an exception means the transport or session is gone.

    Typed, never message text. Verified against a real server: both a killed and
    a gracefully stopped server make the next read raise builtins.ConnectionError,
    and a reconnect against the dead port raises ConnectionRefusedError - both
    OSError subclasses. The one case that needs a fallback is a black-holed
    socket (packets dropped, connection still nominally open), where asyncua
    wraps the timeout in a bare UaError; its __cause__ is the TimeoutError.
    """
    if isinstance(error, (OSError, TimeoutError)):  # ConnectionError is an OSError
        return True
    if isinstance(error, ua.UaStatusCodeError):
        return error.code in CONNECTION_STATUS_CODES
    if isinstance(error, ua.UaError):
        return isinstance(error.__cause__, (TimeoutError, OSError))
    return False


class OPCUAClient:
    """OPC-UA client that batch-polls a node map into a Zelos trace source."""

    def __init__(
        self,
        endpoint: str = "opc.tcp://localhost:4840",
        security_mode: str = "None",
        security_policy: str = "None",
        username: str = "",
        password: str = "",
        timeout: float = 5.0,
        node_map: NodeMap | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        """Initialize the client.

        Args:
            endpoint: OPC-UA server endpoint URL
            security_mode: None, Sign or SignAndEncrypt
            security_policy: None, Basic256Sha256, Aes128Sha256RsaOaep, Aes256Sha256RsaPss
            username: Username for authentication (empty for anonymous)
            password: Password for authentication
            timeout: Request timeout in seconds
            node_map: Node map defining what to poll
            poll_interval: Polling interval in seconds
        """
        self.endpoint = endpoint
        self.security_mode = security_mode
        self.security_policy = security_policy
        self.username = username
        self.password = password
        self.timeout = timeout
        self.node_map = node_map
        self.poll_interval = poll_interval

        self._client: Client | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._running = False
        self._connected = False
        self._poll_count = 0
        self._error_count = 0

        # Resolved once per connection: (event name, node definition, asyncua node)
        # in batch order, plus a by-ID index for the read/write actions.
        self._poll_targets: list[tuple[str, Node, UaNode]] = []
        self._ua_nodes: dict[str, UaNode] = {}
        # Node IDs that have already reported a read failure - see _log_node_failure.
        self._failed_nodes: set[str] = set()

        self._source: zelos_sdk.TraceSourceCacheLast | None = None
        # Events by name, captured from add_event. Never getattr on the source:
        # an event named `log`, `events` or `add_event` resolves to the source's
        # own attribute of that name and the values go nowhere.
        self._events: dict[str, Any] = {}
        self._writable_cache: dict[str, bool] = {}

    # ─── Setup ──────────────────────────────────────────────────────────────

    def _create_client(self) -> Client:
        """Create the asyncua client with configured security."""
        client = Client(url=self.endpoint, timeout=self.timeout)

        if self.security_mode != "None" and self.security_policy != "None":
            mode_map = {
                "None": ua.MessageSecurityMode.None_,
                "Sign": ua.MessageSecurityMode.Sign,
                "SignAndEncrypt": ua.MessageSecurityMode.SignAndEncrypt,
            }
            policy_map = {
                "None": None,
                "Basic256Sha256": "http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256",
                "Aes128Sha256RsaOaep": "http://opcfoundation.org/UA/SecurityPolicy#Aes128_Sha256_RsaOaep",
                "Aes256Sha256RsaPss": "http://opcfoundation.org/UA/SecurityPolicy#Aes256_Sha256_RsaPss",
            }
            mode = mode_map.get(self.security_mode, ua.MessageSecurityMode.None_)
            policy = policy_map.get(self.security_policy)
            if policy:
                client.set_security_string(f"{policy},{mode.name}")

        if self.username:
            client.set_user(self.username)
            client.set_password(self.password)

        return client

    def _init_trace_source(self) -> None:
        """Create the trace source and declare one event per node map event."""
        source_name = self.node_map.name if self.node_map else "opcua"
        self._source = zelos_sdk.TraceSourceCacheLast(source_name)
        self._events = {}

        if not self.node_map or not self.node_map.events:
            self._events["raw"] = self._source.add_event(
                "raw",
                [
                    zelos_sdk.TraceEventFieldMetadata("node_id", zelos_sdk.DataType.String),
                    zelos_sdk.TraceEventFieldMetadata("value", zelos_sdk.DataType.Float64),
                ],
            )
            return

        for event_name, nodes in self.node_map.events.items():
            if not nodes:
                continue
            fields = [
                zelos_sdk.TraceEventFieldMetadata(
                    node.name,
                    SDK_DATATYPES.get(node.datatype, zelos_sdk.DataType.Float64),
                    node.unit,
                )
                for node in nodes
            ]
            self._events[event_name] = self._source.add_event(event_name, fields)

    def _resolve_nodes(self) -> None:
        """Resolve every mapped node ID to an asyncua node handle.

        Handles are bound to the client that produced them, so this runs on every
        (re)connect rather than once at startup, and the poll path never calls
        get_node again.
        """
        self._poll_targets = []
        self._ua_nodes = {}
        if not self.node_map or not self._client:
            return
        for event_name, nodes in self.node_map.events.items():
            for node in nodes:
                ua_node = self._client.get_node(parse_node_id_to_ua(node.node_id))
                self._poll_targets.append((event_name, node, ua_node))
                self._ua_nodes[node.node_id] = ua_node

    # ─── Connection ─────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        """Connect to the server and resolve the node map.

        Returns:
            True if connected
        """
        try:
            self._client = self._create_client()
            await self._client.connect()
            self._connected = True
            self._resolve_nodes()
            logger.info("Connected to OPC-UA server: %s", self.endpoint)
            return True
        except Exception as e:
            logger.warning("Connection to %s failed: %s", self.endpoint, describe_error(e))
            self._connected = False
            return False

    async def disconnect(self) -> None:
        """Close the session, ignoring errors from an already-dead socket."""
        if self._client:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
            self._connected = False
            logger.info("Disconnected from OPC-UA server")

    async def _ensure_connected(self) -> bool:
        """Connect if not connected.

        There is deliberately no liveness probe here: reading Server_ServerStatus_State
        every cycle costs a full extra round trip forever to detect a condition the
        next poll reports anyway.
        """
        if self._connected and self._client:
            return True
        if self._client:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
        self._connected = False
        logger.info("Connecting to %s...", self.endpoint)
        return await self.connect()

    def _require_client(self) -> Client:
        if not self._client or not self._connected:
            raise RuntimeError(f"Not connected to {self.endpoint}")
        return self._client

    def _ua_node(self, node_id: str) -> UaNode:
        """Cached node handle, or an ad-hoc one for IDs outside the map."""
        client = self._require_client()
        cached = self._ua_nodes.get(node_id)
        return cached if cached is not None else client.get_node(parse_node_id_to_ua(node_id))

    # ─── Read / write ───────────────────────────────────────────────────────

    async def read_node(self, node_id: str) -> Any:
        """Read a raw value by node ID. Raises on protocol error."""
        return await self._ua_node(node_id).read_value()

    async def write_node(self, node_id: str, value: Any) -> None:
        """Write a value by node ID. Raises on protocol error.

        Two round trips, each bounded by `self.timeout` - callers dispatching
        this must budget for both.
        """
        node = self._ua_node(node_id)
        # Match the server's variant type. A bare Python value lets asyncua guess
        # (int -> Int64), which a Float or UInt32 node rejects as BadTypeMismatch.
        dv = await node.read_data_value()
        variant_type = dv.Value.VariantType if dv.Value else None
        if isinstance(value, str) and variant_type in VARIANT_DATATYPES:
            # Text from the write action: the server's type is the only datatype
            # an unmapped node has.
            value = coerce_text(value, VARIANT_DATATYPES[variant_type])
        await node.write_value(
            ua.Variant(value, variant_type) if variant_type else ua.Variant(value)
        )

    async def read_node_value(self, node: Node) -> float | int | bool | str | None:
        """Read and decode a node using its map definition."""
        return decode_value(await self.read_node(node.node_id), node.datatype, node.scale)

    async def write_node_value(self, node: Node, value: float | int | bool | str) -> None:
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

        await self.write_node(node.node_id, encode_value(value, node.datatype, node.scale))

    async def _check_writable(self, node_id: str) -> bool:
        """Read the AccessLevel bit. Assumes writable when it cannot be read, so
        that a server which hides AccessLevel does not block every write."""
        try:
            access_level = await self._ua_node(node_id).read_attribute(ua.AttributeIds.AccessLevel)
            return bool(access_level.Value.Value & 0x02)  # bit 1 = CurrentWrite
        except Exception:
            return True

    async def browse_children(self, start_node_id: str, max_depth: int) -> list[dict[str, Any]]:
        """Walk the address space from a starting node."""
        results: list[dict[str, Any]] = []
        await self._browse_recursive(self._ua_node(start_node_id), results, max_depth, 0)
        return results

    async def _browse_recursive(
        self, node: UaNode, results: list[dict[str, Any]], max_depth: int, depth: int
    ) -> None:
        if depth >= max_depth:
            return

        for child in await node.get_children():
            try:
                browse_name = await child.read_browse_name()
                node_class = await child.read_node_class()
            except Exception as e:
                # A child we cannot describe is not a reason to abandon the walk.
                logger.debug("Skipping child of %s: %s", node.nodeid.to_string(), e)
                continue

            info: dict[str, Any] = {
                "node_id": child.nodeid.to_string(),
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
                await self._browse_recursive(child, results, max_depth, depth + 1)

    # ─── Polling ────────────────────────────────────────────────────────────

    async def _poll_nodes(self) -> dict[str, dict[str, Any]]:
        """Read every mapped node in one request per READ_CHUNK nodes.

        One request per cycle, not one per node: a 100-node map at 1 Hz would
        otherwise be 100 serial round trips. Per-item Bad status codes come back
        inside the response (asyncua only raises for a service-level failure), so
        one dead node cannot take the whole cycle down with it.

        Returns:
            {event_name: {field_name: value}}
        """
        if not self._poll_targets:
            return {}

        client = self._require_client()
        data_values = []
        for start in range(0, len(self._poll_targets), READ_CHUNK):
            chunk = self._poll_targets[start : start + READ_CHUNK]
            data_values.extend(
                await client.read_attributes(
                    [ua_node for _, _, ua_node in chunk], ua.AttributeIds.Value
                )
            )

        results: dict[str, dict[str, Any]] = {}
        for (event_name, node, _), dv in zip(self._poll_targets, data_values, strict=True):
            status = dv.StatusCode_
            if status is not None and not status.is_good():
                self._log_node_failure(node, status.name)
                continue
            raw = dv.Value.Value if dv.Value else None
            if raw is None:
                continue
            try:
                value = decode_value(raw, node.datatype, node.scale)
            except (TypeError, ValueError) as e:
                # A value that contradicts its declared datatype (a string on a
                # float32 node) is one bad node, not a failed cycle.
                self._log_node_failure(node, f"decode failed: {describe_error(e)}")
                continue
            results.setdefault(event_name, {})[node.name] = value
        return results

    def _log_node_failure(self, node: Node, reason: str) -> None:
        """One ERROR per bad node for the life of the process.

        A node that is misspelled in the map fails every cycle forever; logging
        each failure would flood the log sink - and the trace, via
        TraceLoggingHandler - at the poll rate.
        """
        if node.node_id in self._failed_nodes:
            return
        self._failed_nodes.add(node.node_id)
        logger.error(
            "Node '%s' (%s) unreadable: %s - further reports suppressed",
            node.name,
            node.node_id,
            reason,
        )

    def _log_values(self, values: dict[str, dict[str, Any]]) -> None:
        """Log one trace event per node map event."""
        for event_name, event_values in values.items():
            event = self._events.get(event_name)
            if event is not None and event_values:
                event.log(**event_values)

    # ─── Lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Create the trace source and arm the polling loop."""
        self._running = True
        self._init_trace_source()
        logger.info("OPCUAClient started")

    def stop(self) -> None:
        """Request shutdown. Safe from any thread, including a signal handler."""
        self._running = False
        loop, event = self._loop, self._stop_event
        if event is None:
            return
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(event.set)
        else:
            event.set()
        logger.info("OPCUAClient stopping")

    def run(self) -> None:
        """Run the polling loop until shutdown (blocking)."""
        asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        """Poll until stopped, reconnecting with capped exponential backoff."""
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        remove_signal_handlers = self._install_signal_handlers()
        backoff = RECONNECT_INITIAL
        losses = 0  # connection losses since the last completed poll
        failures = 0  # unclassified poll failures since the last completed poll

        try:
            while self._running and not self._stop_event.is_set():
                if not await self._connect_or_stop():
                    if self._stop_event.is_set():
                        break
                    logger.warning("Retrying %s in %.0fs", self.endpoint, backoff)
                    if await self._wait_or_stop(backoff):
                        break
                    backoff = min(backoff * 2, RECONNECT_MAX)
                    continue

                try:
                    self._log_values(await self._poll_nodes())
                    self._poll_count += 1
                    # Only a completed poll proves the link; a connect that never
                    # yields data must not clear the backoff.
                    backoff = RECONNECT_INITIAL
                    losses = 0
                    failures = 0
                except Exception as e:
                    self._error_count += 1
                    if is_connection_error(e):
                        logger.warning(
                            "Connection to %s lost: %s", self.endpoint, describe_error(e)
                        )
                        self._connected = False
                        losses += 1
                        # The first loss reconnects at once - the data is already
                        # stale. From the second on, back off: a server that accepts
                        # a session and drops it immediately would otherwise spin.
                        if losses > 1:
                            if await self._wait_or_stop(backoff):
                                break
                            backoff = min(backoff * 2, RECONNECT_MAX)
                        continue
                    logger.error("Poll failed: %s", describe_error(e))
                    failures += 1
                    # An error we cannot classify still wedges the extension if the
                    # session is the thing that is broken. Reconnect rather than
                    # poll a dead session forever.
                    if failures >= POLL_FAILURES_BEFORE_RECONNECT:
                        logger.warning("%d poll failures in a row, reconnecting", failures)
                        self._connected = False
                        failures = 0

                if await self._wait_or_stop(self.poll_interval):
                    break
        finally:
            self._running = False
            remove_signal_handlers()
            try:
                await asyncio.wait_for(self.disconnect(), SHUTDOWN_TIMEOUT)
            except TimeoutError:
                logger.warning("Disconnect exceeded %.0fs, abandoning session", SHUTDOWN_TIMEOUT)

    async def _connect_or_stop(self) -> bool:
        """Connect, unless shutdown is requested first.

        A connect to a black-holed endpoint sits for as long as the OS lets it -
        measured well past the manifest's 10s grace - so it is raced against the
        stop event rather than awaited. The caller distinguishes the two False
        cases by checking the stop event.

        Returns:
            True if connected, False on failure or on shutdown
        """
        if self._stop_event is None:
            return await self._ensure_connected()

        connect = asyncio.ensure_future(self._ensure_connected())
        stop = asyncio.ensure_future(self._stop_event.wait())
        try:
            await asyncio.wait({connect, stop}, return_when=asyncio.FIRST_COMPLETED)
            if connect.done():
                return connect.result()
            return False
        finally:
            stop.cancel()
            if not connect.done():
                connect.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await connect

    def _install_signal_handlers(self) -> Callable[[], None]:
        """Wake the poll loop on SIGTERM/SIGINT from inside the loop.

        A signal.signal handler that calls sys.exit unwinds through the running
        loop and drops the OPC-UA session without a clean close, so the handler
        only sets an event and the loop's own finally does the disconnect.

        Returns:
            A callable that removes whatever was installed
        """
        loop, event = self._loop, self._stop_event
        if loop is None or event is None:
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
                # Windows has no add_signal_handler and neither API works off the
                # main thread. Where signal.signal is available, hop back onto the
                # loop; otherwise the owner is expected to call stop().
                with contextlib.suppress(ValueError, OSError):
                    replaced[sig] = signal.signal(
                        sig, lambda *_: loop.call_soon_threadsafe(request_stop)
                    )

        def remove() -> None:
            for sig in installed:
                with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                    loop.remove_signal_handler(sig)
            # Both branches must uninstall, or a stopped client keeps handling
            # signals into a dead loop. A displaced None was not set from Python.
            for sig, prior in replaced.items():
                with contextlib.suppress(ValueError, OSError, TypeError):
                    signal.signal(sig, prior if prior is not None else signal.SIG_DFL)

        return remove

    async def _wait_or_stop(self, seconds: float) -> bool:
        """Wait up to `seconds`.

        Returns:
            True if shutdown was requested during the wait
        """
        if self._stop_event is None:
            await asyncio.sleep(seconds)
            return False
        try:
            await asyncio.wait_for(self._stop_event.wait(), seconds)
        except TimeoutError:
            return False
        return True

    # ─── Action support ─────────────────────────────────────────────────────

    def _run_coro(self, coro: Coroutine[Any, Any, Any], timeout: float | None = None) -> Any:
        """Run an action's coroutine against the live polling loop.

        Dispatched into that loop, so the action reuses the established session
        instead of paying a handshake - and a session slot - per call. There is
        deliberately no ad-hoc `asyncio.run` fallback: it mutated client state
        from a foreign loop, which silently lost every subsequent sample.

        Raises:
            RuntimeError: If the polling loop is not running
            TimeoutError: If the coroutine outlives `timeout`, having cancelled it
        """
        if self._loop is None or not self._loop.is_running():
            coro.close()
            raise RuntimeError("extension is not running")

        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout or self.timeout)
        except TimeoutError:
            # Left running, a timed-out write still lands on the PLC after the
            # action has already reported failure.
            future.cancel()
            raise

    def known_writable_nodes(self) -> list[Node]:
        """Nodes declared writable in the map, plus any auto-detection has proven
        writable so far (an unprobed `writable: null` node is not yet known)."""
        if not self.node_map:
            return []
        return [
            n
            for n in self.node_map.nodes
            if n.writable is True
            or (n.writable is None and self._writable_cache.get(n.node_id, False))
        ]

    def status(self) -> dict[str, Any]:
        """Connection and polling counters."""
        return {
            "connected": self._connected,
            "endpoint": self.endpoint,
            "security_mode": self.security_mode,
            "security_policy": self.security_policy,
            "poll_count": self._poll_count,
            "error_count": self._error_count,
            "poll_interval": self.poll_interval,
            "nodes": len(self.node_map.nodes) if self.node_map else 0,
        }
