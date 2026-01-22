"""Core OPC-UA client wrapper with Zelos SDK integration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import zelos_sdk
from asyncua import Client, ua
from asyncua.ua.uatypes import NodeId

from zelos_extension_opcua.node_map import Node, NodeMap

logger = logging.getLogger(__name__)


def decode_value(value: Any, datatype: str, scale: float = 1.0) -> float | int | bool | str:
    """Decode OPC-UA value to typed Python value.

    Args:
        value: Raw OPC-UA value
        datatype: Data type string
        scale: Scale factor to apply

    Returns:
        Decoded and scaled value
    """
    if value is None:
        return None

    if datatype == "bool":
        return bool(value)
    elif datatype == "string":
        return str(value) if value is not None else ""
    elif datatype in ("float32", "float64"):
        return float(value) * scale
    elif datatype in ("uint8", "int8", "uint16", "int16", "uint32", "int32", "uint64", "int64"):
        return int(value * scale)
    else:
        return value


def encode_value(value: float | int | bool | str, datatype: str, scale: float = 1.0) -> Any:
    """Encode Python value for OPC-UA write.

    Args:
        value: Value to encode
        datatype: Data type string
        scale: Scale factor (value will be divided by scale)

    Returns:
        Encoded value for OPC-UA
    """
    if datatype == "bool":
        return bool(value)
    elif datatype == "string":
        return str(value)
    elif datatype in ("float32", "float64"):
        return float(value) / scale if scale != 0 else float(value)
    elif datatype in ("uint8", "int8", "uint16", "int16", "uint32", "int32", "uint64", "int64"):
        scaled = value / scale if scale != 0 else value
        return int(scaled)
    else:
        return value


def parse_node_id_to_ua(node_id_str: str) -> NodeId:
    """Parse node ID string to asyncua NodeId.

    Args:
        node_id_str: Node ID in format ns=X;[s|i|g|b]=Y

    Returns:
        asyncua NodeId object
    """
    # Parse ns=X;type=Y format
    parts = node_id_str.split(";")
    ns_part = parts[0]
    id_part = parts[1] if len(parts) > 1 else ""

    # Extract namespace
    namespace = int(ns_part.replace("ns=", ""))

    # Extract identifier
    if id_part.startswith("s="):
        identifier = id_part[2:]
        return NodeId(identifier, namespace)
    elif id_part.startswith("i="):
        identifier = int(id_part[2:])
        return NodeId(identifier, namespace)
    elif id_part.startswith("g=") or id_part.startswith("b="):
        identifier = id_part[2:]
        return NodeId(identifier, namespace)
    else:
        raise ValueError(f"Invalid node ID format: {node_id_str}")


class OPCUAClient:
    """OPC-UA client with polling and Zelos SDK integration."""

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
        """Initialize OPC-UA client.

        Args:
            endpoint: OPC-UA server endpoint URL
            security_mode: Security mode (None, Sign, SignAndEncrypt)
            security_policy: Security policy (None, Basic256Sha256, etc.)
            username: Username for authentication (empty for anonymous)
            password: Password for authentication
            timeout: Request timeout in seconds
            node_map: Optional node map for named access
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
        self._running = False
        self._connected = False
        self._poll_count = 0
        self._error_count = 0

        # Zelos SDK trace source
        self._source: zelos_sdk.TraceSourceCacheLast | None = None
        self._schema_emitted = False

        # Cache for node writability
        self._writable_cache: dict[str, bool] = {}

    def _create_client(self) -> Client:
        """Create the OPC-UA client with configured security."""
        client = Client(url=self.endpoint, timeout=self.timeout)

        # Configure security if not None
        if self.security_mode != "None" and self.security_policy != "None":
            # Map security mode strings to ua.MessageSecurityMode
            mode_map = {
                "None": ua.MessageSecurityMode.None_,
                "Sign": ua.MessageSecurityMode.Sign,
                "SignAndEncrypt": ua.MessageSecurityMode.SignAndEncrypt,
            }
            # Security policy URIs
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

        # Set authentication if provided
        if self.username:
            client.set_user(self.username)
            client.set_password(self.password)

        return client

    def _init_trace_source(self) -> None:
        """Initialize Zelos trace source and define schema from node map."""
        source_name = self.node_map.name if self.node_map else "opcua"
        self._source = zelos_sdk.TraceSourceCacheLast(source_name)

        if not self.node_map or not self.node_map.events:
            # No node map - create a generic raw event
            self._source.add_event(
                "raw",
                [
                    zelos_sdk.TraceEventFieldMetadata("node_id", zelos_sdk.DataType.String),
                    zelos_sdk.TraceEventFieldMetadata("value", zelos_sdk.DataType.Float64),
                ],
            )
            return

        # Create events from user-defined event names
        for event_name, nodes in self.node_map.events.items():
            if not nodes:
                continue

            fields = []
            for node in nodes:
                dtype = self._get_sdk_datatype(node.datatype)
                fields.append(zelos_sdk.TraceEventFieldMetadata(node.name, dtype, node.unit))

            self._source.add_event(event_name, fields)

    def _get_sdk_datatype(self, datatype: str) -> zelos_sdk.DataType:
        """Map node datatype to Zelos SDK DataType."""
        mapping = {
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
        return mapping.get(datatype, zelos_sdk.DataType.Float64)

    async def connect(self) -> bool:
        """Connect to OPC-UA server.

        Returns:
            True if connected successfully
        """
        try:
            self._client = self._create_client()
            await self._client.connect()
            self._connected = True
            logger.info(f"Connected to OPC-UA server: {self.endpoint}")
            return True
        except Exception as e:
            logger.error(f"Connection error: {e}")
            self._connected = False
            return False

    async def disconnect(self) -> None:
        """Disconnect from OPC-UA server."""
        if self._client:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
            self._connected = False
            logger.info("Disconnected from OPC-UA server")

    async def read_node(self, node_id: str) -> Any:
        """Read a node value by node ID.

        Args:
            node_id: OPC-UA node ID string

        Returns:
            Node value or None on error
        """
        if not self._client or not self._connected:
            return None

        try:
            ua_node_id = parse_node_id_to_ua(node_id)
            node = self._client.get_node(ua_node_id)
            value = await node.read_value()
            return value
        except Exception as e:
            logger.warning(f"Read error for node {node_id}: {e}")
            return None

    async def write_node(self, node_id: str, value: Any, datatype: str = "float32") -> bool:
        """Write a value to a node.

        Args:
            node_id: OPC-UA node ID string
            value: Value to write
            datatype: Data type for encoding

        Returns:
            True if successful
        """
        if not self._client or not self._connected:
            return False

        try:
            ua_node_id = parse_node_id_to_ua(node_id)
            node = self._client.get_node(ua_node_id)

            # Get the variant type from the node's data type
            dv = await node.read_data_value()
            variant_type = dv.Value.VariantType if dv.Value else None

            # Create variant with appropriate type
            ua_value = (
                ua.Variant(value, variant_type) if variant_type else ua.Variant(value)
            )

            await node.write_value(ua_value)
            return True
        except Exception as e:
            logger.error(f"Write error for node {node_id}: {e}")
            return False

    async def read_node_value(self, node: Node) -> float | int | bool | str | None:
        """Read and decode a node value using its definition.

        Args:
            node: Node definition

        Returns:
            Decoded value or None on error
        """
        raw = await self.read_node(node.node_id)
        if raw is None:
            return None

        return decode_value(raw, node.datatype, node.scale)

    async def write_node_value(self, node: Node, value: float | int | bool | str) -> bool:
        """Write a value to a node using its definition.

        Args:
            node: Node definition
            value: Value to write

        Returns:
            True if successful
        """
        # Check writability
        if node.writable is False:
            logger.warning(f"Node '{node.name}' is not writable")
            return False

        # Auto-detect writability if not set
        if node.writable is None and node.node_id not in self._writable_cache:
            is_writable = await self._check_writable(node.node_id)
            self._writable_cache[node.node_id] = is_writable
            if not is_writable:
                logger.warning(f"Node '{node.name}' is not writable (auto-detected)")
                return False

        if node.writable is None and not self._writable_cache.get(node.node_id, True):
            return False

        encoded = encode_value(value, node.datatype, node.scale)
        return await self.write_node(node.node_id, encoded, node.datatype)

    async def _check_writable(self, node_id: str) -> bool:
        """Check if a node is writable.

        Args:
            node_id: OPC-UA node ID string

        Returns:
            True if writable
        """
        if not self._client or not self._connected:
            return False

        try:
            ua_node_id = parse_node_id_to_ua(node_id)
            node = self._client.get_node(ua_node_id)
            access_level = await node.read_attribute(ua.AttributeIds.AccessLevel)
            # Bit 1 (0x02) = CurrentWrite
            return bool(access_level.Value.Value & 0x02)
        except Exception:
            return True  # Assume writable if can't determine

    async def _poll_nodes(self) -> dict[str, dict[str, Any]]:
        """Poll all nodes in the node map.

        Returns:
            Dictionary of {event_name: {field_name: value}}
        """
        if not self.node_map:
            return {}

        results: dict[str, dict[str, Any]] = {}

        for event_name, nodes in self.node_map.events.items():
            event_results: dict[str, Any] = {}
            for node in nodes:
                value = await self.read_node_value(node)
                if value is not None:
                    event_results[node.name] = value

            if event_results:
                results[event_name] = event_results

        return results

    async def _log_values(self, values: dict[str, dict[str, Any]]) -> None:
        """Log polled values to Zelos trace source.

        Args:
            values: Dictionary of {event_name: {field_name: value}}
        """
        if not self._source:
            return

        for event_name, event_values in values.items():
            if not event_values:
                continue

            event = getattr(self._source, event_name, None)
            if event:
                event.log(**event_values)

    def start(self) -> None:
        """Start the client (initialize trace source)."""
        self._running = True
        self._init_trace_source()
        logger.info("OPCUAClient started")

    def stop(self) -> None:
        """Stop the client."""
        self._running = False
        logger.info("OPCUAClient stopped")

    def run(self) -> None:
        """Run the polling loop (blocking)."""
        asyncio.run(self._run_async())

    async def _ensure_connected(self) -> bool:
        """Ensure connection is established, reconnecting if needed.

        Returns:
            True if connected
        """
        if self._connected and self._client:
            # Verify connection is still alive
            try:
                # Quick check by reading server state
                await self._client.get_node(ua.ObjectIds.Server_ServerStatus_State).read_value()
                return True
            except Exception:
                self._connected = False

        # Connection lost or not established - try to reconnect
        self._connected = False
        if self._client:
            with contextlib.suppress(Exception):
                await self._client.disconnect()

        logger.info(f"Connecting to {self.endpoint}...")
        return await self.connect()

    async def _run_async(self) -> None:
        """Async polling loop with automatic reconnection."""
        reconnect_interval = 3.0  # seconds between reconnect attempts

        try:
            while self._running:
                # Ensure we're connected
                if not await self._ensure_connected():
                    logger.warning(f"Connection failed, retrying in {reconnect_interval}s...")
                    await asyncio.sleep(reconnect_interval)
                    continue

                # Poll nodes
                try:
                    values = await self._poll_nodes()
                    await self._log_values(values)
                    self._poll_count += 1

                    if self._poll_count % 10 == 0:
                        logger.debug(f"Poll #{self._poll_count}: {values}")

                except Exception as e:
                    self._error_count += 1
                    logger.error(f"Poll error: {e}")

                    # Check if this looks like a connection error
                    if self._is_connection_error(e):
                        self._connected = False
                        logger.warning("Connection lost, will reconnect...")
                        continue  # Skip sleep, reconnect immediately

                await asyncio.sleep(self.poll_interval)
        finally:
            await self.disconnect()

    def _is_connection_error(self, error: Exception) -> bool:
        """Check if an exception indicates a connection problem."""
        error_str = str(error).lower()
        connection_indicators = [
            "connection",
            "timeout",
            "refused",
            "reset",
            "broken pipe",
            "not connected",
            "disconnected",
            "closed",
            "badconnection",
            "badsessionclosed",
        ]
        return any(ind in error_str for ind in connection_indicators)

    # SDK Action methods
    @zelos_sdk.action("Get Status", "Get connection and polling status")
    def get_status(self) -> dict[str, Any]:
        """Get current client status."""
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

    @zelos_sdk.action("Read Node", "Read a single node by node ID")
    @zelos_sdk.action.text("node_id", title="Node ID", description="e.g., ns=2;s=Temperature")
    def read_node_action(self, node_id: str) -> dict[str, Any]:
        """Read a node by node ID."""

        async def _read() -> Any:
            if not self._connected:
                await self.connect()
            return await self.read_node(node_id)

        result = asyncio.run(_read())
        return {
            "node_id": node_id,
            "value": result,
            "success": result is not None,
        }

    @zelos_sdk.action("Write Node", "Write a value to a node by node ID")
    @zelos_sdk.action.text("node_id", title="Node ID", description="e.g., ns=2;s=Setpoint")
    @zelos_sdk.action.number("value", title="Value")
    def write_node_action(self, node_id: str, value: float) -> dict[str, Any]:
        """Write a value to a node."""

        async def _write() -> bool:
            if not self._connected:
                await self.connect()
            return await self.write_node(node_id, value)

        success = asyncio.run(_write())
        return {
            "node_id": node_id,
            "value": value,
            "success": success,
        }

    @zelos_sdk.action("Read Named Node", "Read a node by name from the map")
    @zelos_sdk.action.text("name", title="Node Name")
    def read_named_node(self, name: str) -> dict[str, Any]:
        """Read a node by name from the node map."""
        if not self.node_map:
            return {"error": "No node map loaded", "success": False}

        node = self.node_map.get_by_name(name)
        if not node:
            return {"error": f"Node '{name}' not found", "success": False}

        async def _read() -> Any:
            if not self._connected:
                await self.connect()
            return await self.read_node_value(node)

        value = asyncio.run(_read())
        return {
            "name": name,
            "node_id": node.node_id,
            "datatype": node.datatype,
            "value": value,
            "unit": node.unit,
            "success": value is not None,
        }

    @zelos_sdk.action("Write Named Node", "Write a value to a node by name")
    @zelos_sdk.action.text("name", title="Node Name")
    @zelos_sdk.action.number("value", title="Value")
    def write_named_node(self, name: str, value: float) -> dict[str, Any]:
        """Write a value to a node by name from the node map."""
        if not self.node_map:
            return {"error": "No node map loaded", "success": False}

        node = self.node_map.get_by_name(name)
        if not node:
            return {"error": f"Node '{name}' not found", "success": False}

        if node.writable is False:
            return {
                "error": f"Node '{name}' is not writable",
                "success": False,
            }

        async def _write() -> bool:
            if not self._connected:
                await self.connect()
            return await self.write_node_value(node, value)

        success = asyncio.run(_write())
        return {
            "name": name,
            "node_id": node.node_id,
            "datatype": node.datatype,
            "value": value,
            "unit": node.unit,
            "success": success,
        }

    @zelos_sdk.action("List Nodes", "List all nodes in the map")
    def list_nodes(self) -> dict[str, Any]:
        """List all nodes in the node map."""
        if not self.node_map:
            return {"nodes": [], "count": 0}

        nodes = [
            {
                "name": n.name,
                "node_id": n.node_id,
                "datatype": n.datatype,
                "unit": n.unit,
                "writable": n.writable,
            }
            for n in self.node_map.nodes
        ]
        return {"nodes": nodes, "count": len(nodes)}

    @zelos_sdk.action("List Writable Nodes", "List all writable nodes")
    def list_writable_nodes(self) -> dict[str, Any]:
        """List all writable nodes in the node map."""
        if not self.node_map:
            return {"nodes": [], "count": 0}

        nodes = [
            {
                "name": n.name,
                "node_id": n.node_id,
                "datatype": n.datatype,
                "unit": n.unit,
            }
            for n in self.node_map.nodes
            if n.writable is True
            or (n.writable is None and self._writable_cache.get(n.node_id, True))
        ]
        return {"nodes": nodes, "count": len(nodes)}

    @zelos_sdk.action("Browse Nodes", "Browse OPC-UA address space from a starting node")
    @zelos_sdk.action.text(
        "start_node_id",
        title="Start Node ID",
        default="ns=0;i=85",
        description="Node ID to browse from (default: Objects folder)",
    )
    @zelos_sdk.action.number("max_depth", minimum=1, maximum=5, default=1, title="Max Depth")
    def browse_nodes(self, start_node_id: str, max_depth: int) -> dict[str, Any]:
        """Browse OPC-UA address space."""

        async def _browse() -> list[dict]:
            if not self._connected:
                await self.connect()
            if not self._client:
                return []

            results = []
            ua_node_id = parse_node_id_to_ua(start_node_id)
            start_node = self._client.get_node(ua_node_id)

            await self._browse_recursive(start_node, results, int(max_depth), 0)
            return results

        nodes = asyncio.run(_browse())
        return {"nodes": nodes, "count": len(nodes)}

    async def _browse_recursive(
        self, node: Any, results: list[dict], max_depth: int, current_depth: int
    ) -> None:
        """Recursively browse OPC-UA address space."""
        if current_depth >= max_depth:
            return

        try:
            children = await node.get_children()
            for child in children:
                try:
                    browse_name = await child.read_browse_name()
                    node_class = await child.read_node_class()

                    child_info = {
                        "node_id": child.nodeid.to_string(),
                        "browse_name": f"{browse_name.NamespaceIndex}:{browse_name.Name}",
                        "node_class": node_class.name,
                        "depth": current_depth,
                    }

                    # For variables, try to get the data type
                    if node_class == ua.NodeClass.Variable:
                        try:
                            dv = await child.read_data_value()
                            if dv.Value:
                                child_info["value_type"] = dv.Value.VariantType.name
                        except Exception:
                            pass

                    results.append(child_info)

                    # Recurse into objects and folders
                    if node_class in (ua.NodeClass.Object, ua.NodeClass.ObjectType):
                        await self._browse_recursive(child, results, max_depth, current_depth + 1)

                except Exception as e:
                    logger.debug(f"Error browsing child: {e}")
                    continue

        except Exception as e:
            logger.debug(f"Error browsing node: {e}")
