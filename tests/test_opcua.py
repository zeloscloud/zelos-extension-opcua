"""Tests for Zelos OPC-UA extension.

Tests core functionality:
- Node map parsing
- Value encoding/decoding
- Simulator physics logic
- Integration tests with demo server
"""

import asyncio
import contextlib
import json
import tempfile
import threading
import time
from pathlib import Path

import pytest

from zelos_extension_opcua.client import (
    OPCUAClient,
    decode_value,
    encode_value,
    parse_node_id_to_ua,
)
from zelos_extension_opcua.node_map import Node, NodeMap, parse_node_id

# =============================================================================
# Node Map Tests
# =============================================================================


class TestNode:
    """Test Node dataclass."""

    def test_defaults(self):
        """Minimal required fields use sensible defaults."""
        node = Node(node_id="ns=2;s=Test", name="test")
        assert node.datatype == "float32"
        assert node.scale == 1.0
        assert node.unit == ""

    def test_valid_node_id_string(self):
        """String identifier node ID is parsed correctly."""
        node = Node(node_id="ns=2;s=Temperature.Sensor1", name="sensor1")
        assert node.namespace == 2
        assert node.identifier_type == "s"
        assert node.identifier == "Temperature.Sensor1"

    def test_valid_node_id_numeric(self):
        """Numeric identifier node ID is parsed correctly."""
        node = Node(node_id="ns=2;i=1001", name="sensor2")
        assert node.namespace == 2
        assert node.identifier_type == "i"
        assert node.identifier == 1001

    def test_invalid_datatype_raises(self):
        """Invalid datatype raises ValueError."""
        with pytest.raises(ValueError, match="Invalid datatype"):
            Node(node_id="ns=2;s=Test", name="test", datatype="invalid")

    def test_invalid_node_id_raises(self):
        """Invalid node ID format raises ValueError."""
        with pytest.raises(ValueError, match="Invalid node ID format"):
            Node(node_id="invalid_format", name="test")

    def test_all_datatypes_accepted(self):
        """All valid datatypes are accepted."""
        valid_types = [
            "bool",
            "uint8",
            "int8",
            "uint16",
            "int16",
            "uint32",
            "int32",
            "float32",
            "uint64",
            "int64",
            "float64",
            "string",
        ]
        for dtype in valid_types:
            node = Node(node_id="ns=2;s=Test", name="test", datatype=dtype)
            assert node.datatype == dtype

    def test_writable_default_none(self):
        """Writable defaults to None (auto-detect)."""
        node = Node(node_id="ns=2;s=Test", name="test")
        assert node.writable is None

    def test_writable_explicit(self):
        """Writable can be explicitly set."""
        assert Node(node_id="ns=2;s=Test", name="test", writable=True).writable is True
        assert Node(node_id="ns=2;s=Test", name="test", writable=False).writable is False


class TestNodeIdParsing:
    """Test node ID parsing functions."""

    def test_parse_string_id(self):
        """Parse string identifier."""
        ns, id_type, identifier = parse_node_id("ns=2;s=Temperature.Sensor1")
        assert ns == 2
        assert id_type == "s"
        assert identifier == "Temperature.Sensor1"

    def test_parse_numeric_id(self):
        """Parse numeric identifier."""
        ns, id_type, identifier = parse_node_id("ns=0;i=85")
        assert ns == 0
        assert id_type == "i"
        assert identifier == "85"

    def test_parse_guid_id(self):
        """Parse GUID identifier."""
        ns, id_type, identifier = parse_node_id("ns=1;g=12345678-1234-5678-1234-567812345678")
        assert ns == 1
        assert id_type == "g"
        assert identifier == "12345678-1234-5678-1234-567812345678"

    def test_invalid_format_raises(self):
        """Invalid format raises ValueError."""
        with pytest.raises(ValueError):
            parse_node_id("invalid")

        with pytest.raises(ValueError):
            parse_node_id("ns=2")

        with pytest.raises(ValueError):
            parse_node_id("i=85")


class TestNodeMap:
    """Test NodeMap parsing."""

    def test_from_dict_creates_events(self):
        """Events are correctly parsed from dict."""
        data = {
            "events": {
                "temperature": [{"name": "sensor1", "node_id": "ns=2;s=Temp.S1"}],
                "pressure": [{"name": "sensor1", "node_id": "ns=2;s=Press.S1"}],
            }
        }
        node_map = NodeMap.from_dict(data)
        assert set(node_map.event_names) == {"temperature", "pressure"}
        assert len(node_map.nodes) == 2

    def test_mixed_datatypes_in_event(self):
        """Single event can contain different datatypes."""
        data = {
            "events": {
                "status": [
                    {"name": "temp", "node_id": "ns=2;s=Temp", "datatype": "float32"},
                    {"name": "running", "node_id": "ns=2;s=Running", "datatype": "bool"},
                    {"name": "count", "node_id": "ns=2;s=Count", "datatype": "uint32"},
                ]
            }
        }
        node_map = NodeMap.from_dict(data)
        nodes = node_map.get_event("status")
        assert nodes[0].datatype == "float32"
        assert nodes[1].datatype == "bool"
        assert nodes[2].datatype == "uint32"

    def test_from_file(self):
        """Node map loads from JSON file."""
        data = {"events": {"test": [{"name": "node", "node_id": "ns=2;s=Test"}]}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            f.flush()
            node_map = NodeMap.from_file(f.name)
        assert len(node_map.nodes) == 1
        Path(f.name).unlink()

    def test_get_by_name(self):
        """Find node by name across events."""
        data = {
            "events": {
                "a": [{"name": "temp", "node_id": "ns=2;s=Temp"}],
                "b": [{"name": "press", "node_id": "ns=2;s=Press"}],
            }
        }
        node_map = NodeMap.from_dict(data)
        assert node_map.get_by_name("temp").node_id == "ns=2;s=Temp"
        assert node_map.get_by_name("press").node_id == "ns=2;s=Press"
        assert node_map.get_by_name("nonexistent") is None

    def test_get_by_node_id(self):
        """Find node by node ID."""
        data = {
            "events": {
                "test": [
                    {"name": "temp", "node_id": "ns=2;s=Temperature"},
                    {"name": "press", "node_id": "ns=2;i=1001"},
                ]
            }
        }
        node_map = NodeMap.from_dict(data)
        assert node_map.get_by_node_id("ns=2;s=Temperature").name == "temp"
        assert node_map.get_by_node_id("ns=2;i=1001").name == "press"
        assert node_map.get_by_node_id("ns=2;s=Unknown") is None

    def test_writable_nodes(self):
        """writable_nodes returns only explicitly writable nodes."""
        data = {
            "events": {
                "sensors": [
                    {"name": "temp", "node_id": "ns=2;s=Temp", "writable": False},
                    {"name": "setpoint", "node_id": "ns=2;s=Setpoint", "writable": True},
                    {"name": "auto", "node_id": "ns=2;s=Auto"},  # writable=None
                ],
            }
        }
        node_map = NodeMap.from_dict(data)
        writable = node_map.writable_nodes
        assert len(writable) == 1
        assert writable[0].name == "setpoint"

    def test_map_name_and_description(self):
        """Name and description are parsed."""
        data = {
            "name": "my_device",
            "description": "Test device",
            "events": {},
        }
        node_map = NodeMap.from_dict(data)
        assert node_map.name == "my_device"
        assert node_map.description == "Test device"


# =============================================================================
# Value Encoding/Decoding Tests
# =============================================================================


class TestValueCodec:
    """Test value encoding and decoding."""

    @pytest.mark.parametrize(
        "datatype,raw,expected",
        [
            ("uint16", 1000, 1000),
            ("int16", -100, -100),
            ("uint32", 100000, 100000),
            ("int32", -50000, -50000),
            ("bool", True, True),
            ("bool", False, False),
            ("float32", 3.14, 3.14),
            ("float64", 3.14159265359, 3.14159265359),
            ("string", "hello", "hello"),
        ],
    )
    def test_decode_basic(self, datatype, raw, expected):
        """Basic decoding for various types."""
        result = decode_value(raw, datatype)
        if datatype.startswith("float"):
            assert abs(result - expected) < 0.0001
        else:
            assert result == expected

    def test_decode_with_scale(self):
        """Scale factor is applied after decoding."""
        assert decode_value(1000, "uint16", scale=0.1) == 100
        assert decode_value(100.0, "float32", scale=2.0) == 200.0

    def test_decode_none_returns_none(self):
        """None input returns None."""
        assert decode_value(None, "float32") is None

    @pytest.mark.parametrize(
        "datatype,value,expected",
        [
            ("uint16", 1000, 1000),
            ("int16", -100, -100),
            ("bool", True, True),
            ("bool", False, False),
            ("string", "test", "test"),
        ],
    )
    def test_encode_basic(self, datatype, value, expected):
        """Basic encoding for various types."""
        assert encode_value(value, datatype) == expected

    def test_encode_with_scale(self):
        """Scale factor is applied before encoding."""
        assert encode_value(100, "uint16", scale=0.1) == 1000
        assert encode_value(200.0, "float32", scale=2.0) == 100.0

    def test_roundtrip(self):
        """Encode then decode returns original value."""
        for value, datatype in [
            (1234, "uint16"),
            (-100, "int16"),
            (100000, "uint32"),
            (3.14159, "float32"),
            (True, "bool"),
            ("hello", "string"),
        ]:
            encoded = encode_value(value, datatype)
            decoded = decode_value(encoded, datatype)
            if datatype.startswith("float"):
                assert abs(decoded - value) < 0.0001
            else:
                assert decoded == value


class TestNodeIdToUA:
    """Test node ID conversion to asyncua NodeId."""

    def test_string_identifier(self):
        """String identifier is converted correctly."""
        node_id = parse_node_id_to_ua("ns=2;s=Temperature.Sensor1")
        assert node_id.NamespaceIndex == 2
        assert node_id.Identifier == "Temperature.Sensor1"

    def test_numeric_identifier(self):
        """Numeric identifier is converted correctly."""
        node_id = parse_node_id_to_ua("ns=0;i=85")
        assert node_id.NamespaceIndex == 0
        assert node_id.Identifier == 85

    def test_invalid_format_raises(self):
        """Invalid format raises ValueError."""
        with pytest.raises(ValueError):
            parse_node_id_to_ua("invalid")


# =============================================================================
# Simulator Tests (no network)
# =============================================================================


class TestPLCSimulator:
    """Test simulator physics logic."""

    def test_update_returns_all_fields(self):
        """Update returns complete value dictionary."""
        from zelos_extension_opcua.demo.simulator import PLCSimulator

        sim = PLCSimulator()
        values = sim.update(dt=0.1)

        # Check all expected fields exist
        expected = {
            "temp_sensor1",
            "temp_sensor2",
            "temp_setpoint",
            "pressure1",
            "pressure2",
            "motor_speed",
            "motor_speed_setpoint",
            "motor_current",
            "motor_running",
            "production_count",
            "error_count",
            "input1",
            "input2",
            "output1",
            "output2",
            "voltage",
            "level",
            "energy_total",
            "device_name",
            "status_message",
        }
        assert set(values.keys()) == expected

    def test_temperature_near_setpoint(self):
        """Temperature stays near setpoint."""
        from zelos_extension_opcua.demo.simulator import PLCSimulator

        sim = PLCSimulator()
        values = sim.update(dt=0.1)

        # Should be within ±10°C of setpoint (25°C)
        assert 15 < values["temp_sensor1"] < 35
        assert 12 < values["temp_sensor2"] < 35

    def test_pressure_positive(self):
        """Pressure values are positive."""
        from zelos_extension_opcua.demo.simulator import PLCSimulator

        sim = PLCSimulator()
        values = sim.update(dt=0.1)

        assert values["pressure1"] > 0
        assert values["pressure2"] > 0

    def test_motor_speed_respects_running_state(self):
        """Motor speed only increases when running."""
        from zelos_extension_opcua.demo.simulator import PLCSimulator

        sim = PLCSimulator()

        # Motor not running - speed should be zero/decreasing
        sim.motor_running = False
        sim.motor_speed = 100
        values = sim.update(dt=1.0)
        assert values["motor_speed"] < 100

        # Motor running - speed should increase toward setpoint
        sim.motor_running = True
        sim.motor_speed = 0
        values = sim.update(dt=1.0)
        assert values["motor_speed"] > 0

    def test_voltage_near_24v(self):
        """Voltage stays near 24V."""
        from zelos_extension_opcua.demo.simulator import PLCSimulator

        sim = PLCSimulator()
        values = sim.update(dt=0.1)

        assert 22 < values["voltage"] < 26

    def test_level_in_valid_range(self):
        """Tank level stays in 0-100% range."""
        from zelos_extension_opcua.demo.simulator import PLCSimulator

        sim = PLCSimulator()
        for _ in range(100):
            values = sim.update(dt=0.1)
            assert 0 <= values["level"] <= 100

    def test_energy_accumulates(self):
        """Energy increases over time when motor is running."""
        from zelos_extension_opcua.demo.simulator import PLCSimulator

        sim = PLCSimulator()
        sim.motor_running = True
        sim.motor_speed = 1500

        e1 = sim.energy_total
        sim.update(dt=1.0)
        e2 = sim.energy_total

        assert e2 > e1


# =============================================================================
# Integration Tests with Demo Server
# =============================================================================


class DemoServer:
    """Helper to run demo server in background thread."""

    def __init__(self, host: str = "127.0.0.1", port: int = 14840):
        self.host = host
        self.port = port
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self):
        """Start server in background thread."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Wait for server to be ready
        time.sleep(1.5)

    def _run(self):
        """Run server event loop."""
        from zelos_extension_opcua.demo.simulator import run_demo_server

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        with contextlib.suppress(Exception):
            self._loop.run_until_complete(run_demo_server(self.host, self.port))

    def stop(self):
        """Stop the server."""
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    @property
    def endpoint(self) -> str:
        """Get server endpoint URL."""
        return f"opc.tcp://{self.host}:{self.port}/freeopcua/server/"


@pytest.fixture(scope="module")
def demo_server():
    """Fixture that starts demo server for integration tests."""
    server = DemoServer(port=14840)
    server.start()
    yield server
    server.stop()


@pytest.fixture
def node_map():
    """Load the demo PLC node map."""
    map_path = (
        Path(__file__).parent.parent
        / "zelos_extension_opcua"
        / "demo"
        / "plc_device.json"
    )
    return NodeMap.from_file(str(map_path))


@pytest.fixture
def client(demo_server, node_map):
    """Create a connected OPCUAClient."""
    client = OPCUAClient(
        endpoint=demo_server.endpoint,
        node_map=node_map,
    )

    async def connect():
        await client.connect()

    asyncio.get_event_loop().run_until_complete(connect())
    yield client

    async def disconnect():
        await client.disconnect()

    asyncio.get_event_loop().run_until_complete(disconnect())


class TestDemoServerIntegration:
    """Integration tests against the demo server."""

    def test_read_float32_node(self, client):
        """Read float32 node (temperature)."""
        node = client.node_map.get_by_name("sensor1")
        assert node is not None
        assert node.datatype == "float32"

        async def read():
            return await client.read_node_value(node)

        value = asyncio.get_event_loop().run_until_complete(read())
        assert value is not None
        assert 10 < value < 50  # Reasonable temperature range

    def test_read_bool_node(self, client):
        """Read bool node (motor running)."""
        node = client.node_map.get_by_name("running")
        assert node is not None
        assert node.datatype == "bool"

        async def read():
            return await client.read_node_value(node)

        value = asyncio.get_event_loop().run_until_complete(read())
        assert value in (True, False)

    def test_read_uint32_node(self, client):
        """Read uint32 node (production count)."""
        node = client.node_map.get_by_name("production_count")
        assert node is not None
        assert node.datatype == "uint32"

        async def read():
            return await client.read_node_value(node)

        value = asyncio.get_event_loop().run_until_complete(read())
        assert value is not None
        assert isinstance(value, int)
        assert value >= 0

    def test_read_float64_node(self, client):
        """Read float64 node (total energy)."""
        node = client.node_map.get_by_name("total_energy")
        assert node is not None
        assert node.datatype == "float64"

        async def read():
            return await client.read_node_value(node)

        value = asyncio.get_event_loop().run_until_complete(read())
        assert value is not None
        assert isinstance(value, float)
        assert value >= 0

    def test_read_string_node(self, client):
        """Read string node (device name)."""
        node = client.node_map.get_by_name("device_name")
        assert node is not None
        assert node.datatype == "string"

        async def read():
            return await client.read_node_value(node)

        value = asyncio.get_event_loop().run_until_complete(read())
        assert value is not None
        assert isinstance(value, str)
        assert len(value) > 0

    def test_write_float32_node(self, client):
        """Write float32 node (temperature setpoint)."""
        node = client.node_map.get_by_name("setpoint")
        assert node is not None
        assert node.writable is True

        async def write_and_read():
            # Write new value
            success = await client.write_node_value(node, 30.0)
            assert success is True

            # Read back
            value = await client.read_node_value(node)
            return value

        value = asyncio.get_event_loop().run_until_complete(write_and_read())
        assert abs(value - 30.0) < 0.1

    def test_write_bool_node(self, client):
        """Write bool node (motor running)."""
        node = client.node_map.get_by_name("running")
        assert node is not None
        assert node.writable is True

        async def write_and_read():
            # Write True
            success = await client.write_node_value(node, True)
            assert success is True
            value = await client.read_node_value(node)
            assert value is True

            # Write False
            success = await client.write_node_value(node, False)
            assert success is True
            value = await client.read_node_value(node)
            assert value is False

        asyncio.get_event_loop().run_until_complete(write_and_read())

    def test_write_string_node(self, client):
        """Write string node (device name)."""
        node = client.node_map.get_by_name("device_name")
        assert node is not None
        assert node.writable is True

        async def write_and_read():
            # Write new value
            success = await client.write_node_value(node, "Test Device")
            assert success is True

            # Read back
            value = await client.read_node_value(node)
            return value

        value = asyncio.get_event_loop().run_until_complete(write_and_read())
        assert value == "Test Device"

    def test_write_readonly_fails(self, client):
        """Writing to read-only node should fail."""
        node = client.node_map.get_by_name("input1")
        assert node is not None
        assert node.writable is False

        async def try_write():
            return await client.write_node_value(node, True)

        success = asyncio.get_event_loop().run_until_complete(try_write())
        assert success is False

    def test_poll_all_events(self, client):
        """Poll all nodes and verify event structure."""

        async def poll():
            return await client._poll_nodes()

        results = asyncio.get_event_loop().run_until_complete(poll())

        # Should have events from node map
        assert "temperature" in results
        assert "pressure" in results
        assert "motor" in results
        assert "counters" in results
        assert "digital_io" in results
        assert "analog" in results
        assert "status" in results

        # Temperature event should have sensors
        assert "sensor1" in results["temperature"]
        assert "sensor2" in results["temperature"]

        # Check values are reasonable
        assert 10 < results["temperature"]["sensor1"] < 50


# =============================================================================
# Action Tests
# =============================================================================


class TestReconnection:
    """Tests for connection error detection."""

    def test_is_connection_error_timeout(self):
        """Timeout errors are detected as connection errors."""
        client = OPCUAClient()
        assert client._is_connection_error(Exception("Connection timeout")) is True
        assert client._is_connection_error(Exception("BadConnection")) is True

    def test_is_connection_error_refused(self):
        """Connection refused errors are detected."""
        client = OPCUAClient()
        assert client._is_connection_error(Exception("Connection refused")) is True
        assert client._is_connection_error(Exception("connection reset by peer")) is True
        assert client._is_connection_error(Exception("BadSessionClosed")) is True

    def test_is_connection_error_false_for_other(self):
        """Non-connection errors return False."""
        client = OPCUAClient()
        assert client._is_connection_error(Exception("Invalid node")) is False
        assert client._is_connection_error(Exception("Value out of range")) is False
        assert client._is_connection_error(ValueError("bad value")) is False


class TestActionsUnit:
    """Unit tests for SDK actions (no network)."""

    @pytest.fixture
    def client_with_map(self):
        """Create client with node map but no connection."""
        data = {
            "name": "test_device",
            "events": {
                "sensors": [
                    {"name": "temp", "node_id": "ns=2;s=Temp", "datatype": "float32"},
                    {
                        "name": "press",
                        "node_id": "ns=2;s=Press",
                        "datatype": "float32",
                        "writable": False,
                    },
                ],
                "controls": [
                    {
                        "name": "setpoint",
                        "node_id": "ns=2;s=Setpoint",
                        "datatype": "float32",
                        "writable": True,
                    },
                    {
                        "name": "output",
                        "node_id": "ns=2;s=Output",
                        "datatype": "bool",
                        "writable": True,
                    },
                ],
            },
        }
        node_map = NodeMap.from_dict(data)
        return OPCUAClient(node_map=node_map)

    def test_get_status_returns_info(self, client_with_map):
        """Get Status action returns expected fields."""
        result = client_with_map.get_status()
        assert "connected" in result
        assert "endpoint" in result
        assert "security_mode" in result
        assert "poll_count" in result
        assert "nodes" in result
        assert result["nodes"] == 4

    def test_list_nodes_returns_all(self, client_with_map):
        """List Nodes action returns all nodes."""
        result = client_with_map.list_nodes()
        assert result["count"] == 4
        names = [n["name"] for n in result["nodes"]]
        assert "temp" in names
        assert "press" in names
        assert "setpoint" in names
        assert "output" in names

    def test_list_writable_nodes_filters(self, client_with_map):
        """List Writable Nodes only returns writable ones."""
        result = client_with_map.list_writable_nodes()
        # Only explicitly writable nodes
        assert result["count"] == 2
        names = [n["name"] for n in result["nodes"]]
        assert "setpoint" in names
        assert "output" in names
        assert "press" not in names  # explicitly writable=False

    def test_list_nodes_no_map(self):
        """List Nodes with no map returns empty."""
        client = OPCUAClient()
        result = client.list_nodes()
        assert result["count"] == 0
        assert result["nodes"] == []

    def test_list_writable_no_map(self):
        """List Writable with no map returns empty."""
        client = OPCUAClient()
        result = client.list_writable_nodes()
        assert result["count"] == 0

    def test_read_named_no_map(self):
        """Read Named Node with no map returns error."""
        client = OPCUAClient()
        result = client.read_named_node("anything")
        assert result["success"] is False
        assert "error" in result

    def test_read_named_not_found(self, client_with_map):
        """Read Named Node with unknown name returns error."""
        result = client_with_map.read_named_node("nonexistent")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_write_named_no_map(self):
        """Write Named Node with no map returns error."""
        client = OPCUAClient()
        result = client.write_named_node("anything", 100)
        assert result["success"] is False
        assert "error" in result

    def test_write_named_not_found(self, client_with_map):
        """Write Named Node with unknown name returns error."""
        result = client_with_map.write_named_node("nonexistent", 100)
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_write_named_not_writable(self, client_with_map):
        """Write Named Node to read-only node returns error."""
        result = client_with_map.write_named_node("press", 100)
        assert result["success"] is False
        assert "not writable" in result["error"]


class TestActionsIntegration:
    """Integration tests for SDK actions with demo server."""

    def test_list_nodes_action(self, client):
        """List Nodes action returns all demo nodes."""
        result = client.list_nodes()
        assert result["count"] > 0
        names = [n["name"] for n in result["nodes"]]
        assert "sensor1" in names
        assert "running" in names
        assert "production_count" in names

    def test_list_writable_action(self, client):
        """List Writable action returns writable nodes."""
        result = client.list_writable_nodes()
        names = [n["name"] for n in result["nodes"]]
        assert "setpoint" in names
        assert "speed_setpoint" in names
        assert "output1" in names

    def test_get_status_action(self, client):
        """Get Status action returns info."""
        result = client.get_status()
        assert result["connected"] is True
        assert "endpoint" in result
        assert result["nodes"] > 0

    def test_write_named_readonly_fails(self, client):
        """Write Named to read-only node fails gracefully."""
        result = client.write_named_node("input1", True)
        assert result["success"] is False
        assert "not writable" in result["error"]
