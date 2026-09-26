"""Tests for the Zelos OPC-UA extension.

Covers node map parsing and sanitization, value codecs, connection-error
classification, the action surface, simulator physics, and integration against
the real demo OPC-UA server (batch polling, partial failure, reconnection).
"""

import asyncio
import json
import logging
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest
import zelos_sdk
from asyncua import ua

from zelos_extension_opcua import actions
from zelos_extension_opcua.client import (
    OPCUAClient,
    OPCUARunner,
    coerce_text,
    decode_value,
    encode_value,
    is_connection_error,
    parse_node_id_to_ua,
)
from zelos_extension_opcua.node_map import (
    Node,
    NodeMap,
    format_nsu_node_id,
    parse_node_id,
    sanitize_name,
)

DEMO_MAP_PATH = Path(__file__).parent.parent / "zelos_extension_opcua" / "demo" / "plc_device.json"

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
        assert node.writable is None

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
        for dtype in (
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
        ):
            assert Node(node_id="ns=2;s=Test", name="test", datatype=dtype).datatype == dtype

    def test_writable_explicit(self):
        """Writable can be explicitly set."""
        assert Node(node_id="ns=2;s=Test", name="test", writable=True).writable is True
        assert Node(node_id="ns=2;s=Test", name="test", writable=False).writable is False


class TestNodeIdParsing:
    """Test node ID parsing functions."""

    def test_parse_string_id(self):
        """Parse string identifier."""
        assert parse_node_id("ns=2;s=Temperature.Sensor1") == (2, "s", "Temperature.Sensor1")

    def test_parse_numeric_id(self):
        """Parse numeric identifier, converted for asyncua."""
        assert parse_node_id("ns=0;i=85") == (0, "i", 85)

    def test_parse_guid_id(self):
        """Parse GUID identifier, converted for asyncua."""
        guid = "12345678-1234-5678-1234-567812345678"
        assert parse_node_id(f"ns=1;g={guid}") == (1, "g", uuid.UUID(guid))

    def test_parse_nsu_id(self):
        """nsu= carries the percent-decoded URI; the identifier converts as for ns=."""
        assert parse_node_id("nsu=urn:zelos:demo:plc;s=A;B") == ("urn:zelos:demo:plc", "s", "A;B")
        assert parse_node_id("nsu=urn:a%3Bb%25;i=7") == ("urn:a;b%", "i", 7)
        nsu = format_nsu_node_id("urn:a;b%3B", "i=7")
        assert parse_node_id(nsu) == ("urn:a;b%3B", "i", 7)

    def test_invalid_format_raises(self):
        """Invalid format raises ValueError."""
        for bad in ("invalid", "ns=2", "i=85", "nsu=;i=1", "nsu=urn:x", "nsu=urn:x;g=nope"):
            with pytest.raises(ValueError):
                parse_node_id(bad)


class TestNodeMap:
    """Test NodeMap parsing."""

    def test_from_dict_creates_events(self):
        """Events are correctly parsed from dict."""
        data = {
            "events": {
                "temperature": [{"name": "temp1", "node_id": "ns=2;s=Temp.S1"}],
                "pressure": [{"name": "press1", "node_id": "ns=2;s=Press.S1"}],
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
        nodes = NodeMap.from_dict(data).get_event("status")
        assert [n.datatype for n in nodes] == ["float32", "bool", "uint32"]

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
                    {"name": "auto", "node_id": "ns=2;s=Auto"},
                ],
            }
        }
        writable = NodeMap.from_dict(data).writable_nodes
        assert [n.name for n in writable] == ["setpoint"]

    def test_map_name_and_description(self):
        """Name and description are parsed."""
        node_map = NodeMap.from_dict(
            {"name": "my_device", "description": "Test device", "events": {}}
        )
        assert node_map.name == "my_device"
        assert node_map.description == "Test device"

    def test_demo_map_loads(self):
        """The bundled demo map satisfies the collision rules."""
        node_map = NodeMap.from_file(DEMO_MAP_PATH)
        assert node_map.name == "demo_plc"
        assert len(node_map.nodes) == len({n.name for n in node_map.nodes})

    @pytest.mark.parametrize("node_id", ["ns=2;i=abc", "ns=2;g=not-a-guid", "ns=2;b=not_base64!!"])
    def test_unconvertible_identifier_rejected_at_load(self, node_id):
        """A bad identifier is a map error, not a "connection failed" at runtime."""
        with pytest.raises(ValueError, match=node_id.replace("!!", "")):
            NodeMap.from_dict({"events": {"e": [{"name": "n", "node_id": node_id}]}})


class TestSanitization:
    """Names that reach a trace must not contain catalog separators."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Temp.Sensor", "Temp_Sensor"),
            ("plc@site", "plc_site"),
            ("ns:2", "ns_2"),
            ("a;b=c", "a_b_c"),
            ("path/to/node", "path_to_node"),
            ("has space", "has space"),
            ("plain_name", "plain_name"),
        ],
    )
    def test_sanitize_name(self, raw, expected):
        assert sanitize_name(raw) == expected

    def test_event_names_keep_slash_paths(self):
        # `/` is the intra-source path separator: legal in events, not fields
        assert sanitize_name("motor/status", kind="event") == "motor/status"
        assert sanitize_name("motor/status", kind="field") == "motor_status"

    def test_applied_to_map_event_and_node_names(self):
        """Sanitization covers the source name, event names and field names."""
        node_map = NodeMap.from_dict(
            {
                "name": "site.plc:1",
                "events": {"temp/zone a": [{"name": "Sensor.1", "node_id": "ns=2;s=T1"}]},
            }
        )
        assert node_map.name == "site_plc_1"
        # `/` and spaces are legal in event names under the SDK grammar
        assert node_map.event_names == ["temp/zone a"]
        assert node_map.nodes[0].name == "Sensor_1"

    def test_get_by_name_uses_sanitized_name(self):
        """Lookups use the name the user sees in the trace."""
        node_map = NodeMap.from_dict(
            {"events": {"e": [{"name": "Sensor.1", "node_id": "ns=2;s=T1"}]}}
        )
        assert node_map.get_by_name("Sensor_1") is not None
        assert node_map.get_by_name("Sensor.1") is None

    def test_duplicate_event_name_raises(self):
        """Two events that sanitize to the same name are a hard error."""
        with pytest.raises(ValueError, match="Duplicate event name"):
            NodeMap.from_dict(
                {
                    "events": {
                        "zone.a": [{"name": "n1", "node_id": "ns=2;s=A"}],
                        "zone:a": [{"name": "n2", "node_id": "ns=2;s=B"}],
                    }
                }
            )

    def test_duplicate_node_name_within_event_raises(self):
        with pytest.raises(ValueError, match="Duplicate node name 'temp'"):
            NodeMap.from_dict(
                {
                    "events": {
                        "e": [
                            {"name": "temp", "node_id": "ns=2;s=A"},
                            {"name": "temp", "node_id": "ns=2;s=B"},
                        ]
                    }
                }
            )

    def test_node_name_across_events_is_qualified(self):
        node_map = NodeMap.from_dict(
            {
                "events": {
                    "a": [{"name": "sensor1", "node_id": "ns=2;s=A"}],
                    "b/c": [{"name": "sensor1", "node_id": "ns=2;s=B"}],
                }
            }
        )
        assert node_map.get_by_name("b/c/sensor1").node_id == "ns=2;s=B"
        with pytest.raises(ValueError, match="use one of: a/sensor1, b/c/sensor1"):
            node_map.get_by_name("sensor1")

    def test_collision_only_after_sanitization_raises(self):
        with pytest.raises(ValueError, match="Duplicate node name 'a_b'"):
            NodeMap.from_dict(
                {
                    "events": {
                        "e": [
                            {"name": "a.b", "node_id": "ns=2;s=A"},
                            {"name": "a:b", "node_id": "ns=2;s=B"},
                        ]
                    }
                }
            )


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
            decoded = decode_value(encode_value(value, datatype), datatype)
            if datatype.startswith("float"):
                assert abs(decoded - value) < 0.0001
            else:
                assert decoded == value


class TestCoerceText:
    """Write actions take text, so bool and string nodes are writable at all."""

    @pytest.mark.parametrize(
        "text,datatype,expected",
        [
            ("true", "bool", True),
            ("FALSE", "bool", False),
            ("1", "bool", True),
            ("0", "bool", False),
            ("12.5", "float32", 12.5),
            ("42", "uint16", 42),
            ("-7", "int16", -7),
            ("Test Device", "string", "Test Device"),
        ],
    )
    def test_parses_per_datatype(self, text, datatype, expected):
        result = coerce_text(text, datatype)
        assert result == expected
        assert isinstance(result, type(expected))

    @pytest.mark.parametrize(
        "text,datatype", [("maybe", "bool"), ("abc", "float32"), ("n/a", "uint16")]
    )
    def test_unparseable_raises(self, text, datatype):
        with pytest.raises(ValueError, match="Cannot parse"):
            coerce_text(text, datatype)


class TestNodeIdToUA:
    """Test node ID conversion to asyncua NodeId."""

    def test_string_identifier(self):
        node_id = parse_node_id_to_ua("ns=2;s=Temperature.Sensor1")
        assert node_id.NamespaceIndex == 2
        assert node_id.Identifier == "Temperature.Sensor1"
        assert node_id.NodeIdType == ua.NodeIdType.String

    def test_numeric_identifier(self):
        node_id = parse_node_id_to_ua("ns=2;i=1001")
        assert node_id.NamespaceIndex == 2
        assert node_id.Identifier == 1001
        # asyncua narrows numeric IDs to TwoByte / FourByte / Numeric by width.
        assert node_id.NodeIdType in (
            ua.NodeIdType.TwoByte,
            ua.NodeIdType.FourByte,
            ua.NodeIdType.Numeric,
        )

    def test_guid_identifier_is_a_uuid(self):
        """A str GUID would be sent as a String identifier and never match."""
        raw = "12345678-1234-5678-1234-567812345678"
        node_id = parse_node_id_to_ua(f"ns=1;g={raw}")
        assert node_id.Identifier == uuid.UUID(raw)
        assert node_id.NodeIdType == ua.NodeIdType.Guid

    def test_opaque_identifier_is_bytes(self):
        node_id = parse_node_id_to_ua("ns=1;b=AQID")
        assert node_id.Identifier == b"\x01\x02\x03"
        assert node_id.NodeIdType == ua.NodeIdType.ByteString

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError):
            parse_node_id_to_ua("invalid")

    def test_invalid_guid_raises(self):
        with pytest.raises(ValueError, match="Invalid GUID"):
            parse_node_id_to_ua("ns=1;g=not-a-guid")


# =============================================================================
# Connection Error Classification
# =============================================================================


class TestConnectionErrorClassification:
    """Transport loss is classified by exception type, never by message text."""

    def test_socket_errors(self):
        """What a killed or stopped server actually produces (verified live)."""
        assert is_connection_error(ConnectionError("Connection is closed")) is True
        assert is_connection_error(ConnectionRefusedError(61, "refused")) is True
        assert is_connection_error(TimeoutError()) is True
        assert is_connection_error(TimeoutError()) is True

    def test_session_status_codes(self):
        """Session and channel status codes mean reconnect."""
        from asyncua.ua.uaerrors import BadSecureChannelClosed, BadSessionIdInvalid

        assert is_connection_error(BadSessionIdInvalid()) is True
        assert is_connection_error(BadSecureChannelClosed()) is True

    def test_node_level_status_codes_are_not_connection_errors(self):
        """A bad node id is a node problem, not a transport problem."""
        from asyncua.ua.uaerrors import BadNodeIdUnknown, BadTypeMismatch

        assert is_connection_error(BadNodeIdUnknown()) is False
        assert is_connection_error(BadTypeMismatch()) is False

    def test_generic_uaerror_wrapping_a_timeout(self):
        """A black-holed socket surfaces as a bare UaError caused by a timeout."""
        err = ua.UaError("Failed to send request to OPC UA server")
        err.__cause__ = TimeoutError()
        assert is_connection_error(err) is True
        assert is_connection_error(ua.UaError("something else")) is False

    def test_unrelated_errors(self):
        assert is_connection_error(ValueError("bad value")) is False
        assert is_connection_error(KeyError("missing")) is False


# =============================================================================
# Simulator Tests (no network)
# =============================================================================


class TestPLCSimulator:
    """Test simulator physics logic."""

    def test_update_returns_all_fields(self):
        """Update returns complete value dictionary."""
        from zelos_extension_opcua.demo.simulator import PLCSimulator

        values = PLCSimulator().update(dt=0.1)
        assert set(values.keys()) == {
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

    def test_temperature_near_setpoint(self):
        """Temperature stays near setpoint."""
        from zelos_extension_opcua.demo.simulator import PLCSimulator

        values = PLCSimulator().update(dt=0.1)
        assert 15 < values["temp_sensor1"] < 35
        assert 12 < values["temp_sensor2"] < 35

    def test_pressure_positive(self):
        """Pressure values are positive."""
        from zelos_extension_opcua.demo.simulator import PLCSimulator

        values = PLCSimulator().update(dt=0.1)
        assert values["pressure1"] > 0
        assert values["pressure2"] > 0

    def test_motor_speed_respects_running_state(self):
        """Motor speed only increases when running."""
        from zelos_extension_opcua.demo.simulator import PLCSimulator

        sim = PLCSimulator()
        sim.motor_running = False
        sim.motor_speed = 100
        assert sim.update(dt=1.0)["motor_speed"] < 100

        sim.motor_running = True
        sim.motor_speed = 0
        assert sim.update(dt=1.0)["motor_speed"] > 0

    def test_voltage_near_24v(self):
        """Voltage stays near 24V."""
        from zelos_extension_opcua.demo.simulator import PLCSimulator

        assert 22 < PLCSimulator().update(dt=0.1)["voltage"] < 26

    def test_level_in_valid_range(self):
        """Tank level stays in 0-100% range."""
        from zelos_extension_opcua.demo.simulator import PLCSimulator

        sim = PLCSimulator()
        for _ in range(100):
            assert 0 <= sim.update(dt=0.1)["level"] <= 100

    def test_energy_accumulates(self):
        """Energy increases over time when motor is running."""
        from zelos_extension_opcua.demo.simulator import PLCSimulator

        sim = PLCSimulator()
        sim.motor_running = True
        sim.motor_speed = 1500

        before = sim.energy_total
        sim.update(dt=1.0)
        assert sim.energy_total > before


# =============================================================================
# Integration Tests with Demo Server
# =============================================================================


class DemoServer:
    """Helper to run the demo server in a background thread."""

    def __init__(self, host: str = "127.0.0.1", port: int = 14840):
        self.host = host
        self.port = port
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutdown_event: asyncio.Event | None = None

    def start(self):
        """Start the server and block until it accepts connections."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._wait_for_port(up=True)

    def _wait_for_port(self, up: bool, timeout: float = 15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            try:
                sock.connect((self.host, self.port))
                if up:
                    return
            except OSError:
                if not up:
                    return
            finally:
                sock.close()
            time.sleep(0.1)
        raise TimeoutError(f"Port {self.port} never went {'up' if up else 'down'}")

    def _run(self):
        from zelos_extension_opcua.demo.simulator import run_demo_server

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._shutdown_event = asyncio.Event()

        try:
            self._loop.run_until_complete(
                run_demo_server(self.host, self.port, self._shutdown_event)
            )
        except Exception:
            pass
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()

    def stop(self):
        """Stop the server and block until the port is free."""
        if self._loop and self._shutdown_event:
            self._loop.call_soon_threadsafe(self._shutdown_event.set)
            if self._thread:
                self._thread.join(timeout=10.0)
            self._wait_for_port(up=False)

    @property
    def endpoint(self) -> str:
        return f"opc.tcp://{self.host}:{self.port}/freeopcua/server/"


@pytest.fixture(scope="module")
def demo_server():
    """Demo server shared by every integration test in this module."""
    server = DemoServer(port=14840)
    server.start()
    yield server
    server.stop()


@pytest.fixture
def node_map():
    """The bundled demo PLC node map."""
    return NodeMap.from_file(DEMO_MAP_PATH)


@pytest.fixture
async def client(demo_server, node_map):
    """A connected OPCUAClient, torn down after the test."""
    client = OPCUAClient(endpoint=demo_server.endpoint, node_map=node_map)
    assert await client.connect() is True
    yield client
    await client.disconnect()


@pytest.fixture
async def bound_client(client):
    """`client`, bound as the actions module's client for the test."""
    actions.set_runner(OPCUARunner([client]))
    yield client
    actions.set_runner(None)


class TestDemoServerIntegration:
    """Integration tests against the demo server."""

    @pytest.mark.parametrize(
        "name,datatype,check",
        [
            ("temp_sensor1", "float32", lambda v: 10 < v < 50),
            ("running", "bool", lambda v: v in (True, False)),
            ("production_count", "uint32", lambda v: isinstance(v, int) and v >= 0),
            ("total_energy", "float64", lambda v: isinstance(v, float) and v >= 0),
            ("device_name", "string", lambda v: isinstance(v, str) and v),
        ],
    )
    async def test_read_node_types(self, client, name, datatype, check):
        """Every mapped datatype reads back as the right Python type."""
        node = client.node_map.get_by_name(name)
        assert node.datatype == datatype
        assert check(await client.read_node_value(node))

    async def test_write_float32_node(self, client):
        """Write and read back a float32 setpoint."""
        node = client.node_map.get_by_name("setpoint")
        await client.write_node_value(node, 30.0)
        assert abs(await client.read_node_value(node) - 30.0) < 0.1

    async def test_write_bool_node(self, client):
        """Write and read back a bool."""
        node = client.node_map.get_by_name("running")
        await client.write_node_value(node, True)
        assert await client.read_node_value(node) is True
        await client.write_node_value(node, False)
        assert await client.read_node_value(node) is False

    async def test_write_string_node(self, client):
        """Write and read back a string."""
        node = client.node_map.get_by_name("device_name")
        await client.write_node_value(node, "Test Device")
        assert await client.read_node_value(node) == "Test Device"

    async def test_write_readonly_raises(self, client):
        """A read-only node raises rather than reporting a quiet failure."""
        node = client.node_map.get_by_name("input1")
        assert node.writable is False
        with pytest.raises(ValueError, match="not writable"):
            await client.write_node_value(node, True)

    async def test_poll_all_events(self, client):
        """One batch read covers every event in the map."""
        results = await client._poll_nodes()
        assert set(results) >= {
            "temperature",
            "pressure",
            "motor",
            "counters",
            "digital_io",
            "analog",
            "status",
        }
        assert 10 < results["temperature"]["temp_sensor1"] < 50
        assert "pressure_sensor1" in results["pressure"]

    async def test_poll_uses_one_request(self, client, monkeypatch):
        """The cycle is one read request, not one per node."""
        calls = 0
        original = client._client.read_attributes

        async def counting(*args, **kwargs):
            nonlocal calls
            calls += 1
            return await original(*args, **kwargs)

        monkeypatch.setattr(client._client, "read_attributes", counting)
        await client._poll_nodes()
        assert calls == 1
        assert len(client._poll_targets) == len(client.node_map.nodes)


class TestNamespaceUriIds:
    """nsu= IDs resolve against the live NamespaceArray, not a stored index."""

    async def test_nsu_map_polls_the_same_nodes(self, demo_server, client):
        data = json.loads(DEMO_MAP_PATH.read_text())
        text = json.dumps(data).replace('"ns=2;', '"nsu=urn:zelos:demo:plc;')
        nsu_client = OPCUAClient(
            endpoint=demo_server.endpoint, node_map=NodeMap.from_dict(json.loads(text))
        )
        assert await nsu_client.connect() is True
        try:
            assert [t[2].nodeid for t in nsu_client._poll_targets] == [
                t[2].nodeid for t in client._poll_targets
            ]
            ns_values, nsu_values = await client._poll_nodes(), await nsu_client._poll_nodes()
            assert {e: set(v) for e, v in nsu_values.items()} == {
                e: set(v) for e, v in ns_values.items()
            }
            assert nsu_values["status"]["device_name"] == ns_values["status"]["device_name"]

            # Raw-ID actions, and browse output round-trips through nsu_node_id.
            name = await nsu_client.read_node("nsu=urn:zelos:demo:plc;s=Status.DeviceName")
            assert name == ns_values["status"]["device_name"]
            browsed = await nsu_client.browse_children("ns=0;i=85", 3)
            entry = next(n for n in browsed if n["node_id"] == "ns=2;s=Temperature.Sensor1")
            assert entry["nsu_node_id"] == "nsu=urn:zelos:demo:plc;s=Temperature.Sensor1"
        finally:
            await nsu_client.disconnect()

    async def test_unknown_uri_is_skipped_and_the_loop_lives(self, demo_server, caplog):
        node_map = NodeMap.from_dict(
            {
                "name": "nsu_missing",
                "events": {
                    "mix": [
                        {"name": "good", "node_id": "ns=2;s=Temperature.Sensor1"},
                        {"name": "lost", "node_id": "nsu=urn:not:there;s=Temperature.Sensor1"},
                    ]
                },
            }
        )
        client = OPCUAClient(endpoint=demo_server.endpoint, node_map=node_map, poll_interval=0.1)
        client.start()
        runner = OPCUARunner([client])
        task = asyncio.create_task(runner._run_async())
        try:
            await _wait_until(lambda: client._poll_count >= 2, 15.0)
            assert not task.done()
            assert [t[1].name for t in client._poll_targets] == ["good"]
            errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
            assert any("urn:not:there" in m and "lost" in m for m in errors)
            assert set((await client._poll_nodes())["mix"]) == {"good"}
        finally:
            runner.stop()
            await asyncio.wait_for(task, 10.0)


class TestBatchPollPartialFailure:
    """A single bad node must not cost the cycle, or flood the log."""

    @pytest.fixture
    async def client_with_bogus_node(self, demo_server):
        node_map = NodeMap.from_dict(
            {
                "name": "partial",
                "events": {
                    "mix": [
                        {"name": "good", "node_id": "ns=2;s=Temperature.Sensor1"},
                        {"name": "bogus", "node_id": "ns=2;s=Does.Not.Exist"},
                        {"name": "also_good", "node_id": "ns=2;s=Analog.Voltage"},
                    ]
                },
            }
        )
        client = OPCUAClient(endpoint=demo_server.endpoint, node_map=node_map)
        assert await client.connect() is True
        yield client
        await client.disconnect()

    async def test_good_nodes_survive_a_bad_one(self, client_with_bogus_node, caplog):
        """The good nodes are delivered and the bad one is reported once."""
        with caplog.at_level(logging.ERROR, logger="zelos_extension_opcua.client"):
            results = await client_with_bogus_node._poll_nodes()

            assert set(results["mix"]) == {"good", "also_good"}
            errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
            assert len(errors) == 1
            assert "Does.Not.Exist" in errors[0].getMessage()

            # Second cycle still delivers, and stays silent about the same node.
            assert set((await client_with_bogus_node._poll_nodes())["mix"]) == {
                "good",
                "also_good",
            }
            assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1

    async def test_undecodable_value_costs_only_its_node(self, demo_server, caplog):
        """A string arriving on a float32 node must not abort the cycle."""
        node_map = NodeMap.from_dict(
            {
                "name": "decode",
                "events": {
                    "mix": [
                        {"name": "good", "node_id": "ns=2;s=Temperature.Sensor1"},
                        {"name": "wrong_type", "node_id": "ns=2;s=Status.DeviceName"},
                    ]
                },
            }
        )
        client = OPCUAClient(endpoint=demo_server.endpoint, node_map=node_map)
        assert await client.connect() is True
        try:
            with caplog.at_level(logging.ERROR, logger="zelos_extension_opcua.client"):
                for _ in range(3):
                    assert set((await client._poll_nodes())["mix"]) == {"good"}
                errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
                assert len(errors) == 1
                assert "decode failed" in errors[0].getMessage()
        finally:
            await client.disconnect()


class TestReconnection:
    """The poll loop survives a server that goes away and comes back."""

    async def test_reconnects_after_server_restart(self):
        """Values flow, stop the server, restart it, values flow again."""
        port = 14841
        node_map = NodeMap.from_dict(
            {
                "name": "reconnect_test",
                "events": {
                    "temperature": [{"name": "t1", "node_id": "ns=2;s=Temperature.Sensor1"}]
                },
            }
        )
        server = DemoServer(port=port)
        server.start()

        client = OPCUAClient(
            endpoint=server.endpoint, node_map=node_map, poll_interval=0.2, timeout=2.0
        )
        client.start()
        runner = OPCUARunner([client])
        task = asyncio.create_task(runner._run_async())
        try:
            await _wait_until(lambda: client._poll_count >= 2, 15.0)

            server.stop()
            await _wait_until(lambda: not client._connected, 15.0)
            assert not task.done()  # the loop survived the disconnect

            polls_before = client._poll_count
            server = DemoServer(port=port)
            server.start()

            # Reconnect is backed off (3s initial), so allow a few attempts.
            await _wait_until(lambda: client._connected and client._poll_count > polls_before, 30.0)
            assert (await client._poll_nodes())["temperature"]["t1"] > 0
        finally:
            runner.stop()
            await asyncio.wait_for(task, 10.0)
            server.stop()

    async def test_stop_ends_the_loop_without_a_signal(self):
        """stop() alone drains the loop and the finally disconnects."""
        client = OPCUAClient(endpoint="opc.tcp://127.0.0.1:14999", poll_interval=0.1)
        client.start()
        runner = OPCUARunner([client])
        task = asyncio.create_task(runner._run_async())
        await asyncio.sleep(0.5)  # let the first (failing) connect attempt land
        runner.stop()
        await asyncio.wait_for(task, 5.0)
        assert client._connected is False

    async def test_stop_cancels_an_in_flight_connect(self):
        """Shutdown must not wait out a connect to an unreachable endpoint."""
        cancelled = False

        async def slow_connect():
            nonlocal cancelled
            try:
                await asyncio.sleep(30.0)
            except asyncio.CancelledError:
                cancelled = True
                raise
            return False

        client = OPCUAClient(endpoint="opc.tcp://10.255.255.1:4840", timeout=20.0)
        client._ensure_connected = slow_connect
        client.start()
        runner = OPCUARunner([client])
        task = asyncio.create_task(runner._run_async())
        await asyncio.sleep(0.2)
        runner.stop()
        await asyncio.wait_for(task, 2.0)
        assert cancelled

    async def test_unclassified_poll_failures_force_a_reconnect(self):
        """An error is_connection_error does not claim must not wedge the loop."""
        connects = 0

        async def fake_connect():
            nonlocal connects
            connects += 1
            client._connected = True
            return True

        async def failing_poll():
            raise ValueError("not a connection error")

        client = OPCUAClient(endpoint="opc.tcp://127.0.0.1:14999", poll_interval=0.01)
        client._ensure_connected = fake_connect
        client._poll_nodes = failing_poll
        client.start()
        runner = OPCUARunner([client])
        task = asyncio.create_task(runner._run_async())
        try:
            await _wait_until(lambda: connects >= 2, 5.0)
        finally:
            runner.stop()
            await asyncio.wait_for(task, 5.0)
        assert client._error_count >= 5


class TestTraceSourceEvents:
    """Trace layout per prefix; events held by name, never getattr on the source."""

    @pytest.mark.parametrize(
        ("shared", "source", "event"), [(True, "OPC-UA", "plc01/log"), (False, "plc01", "log")]
    )
    def test_layout(self, shared, source, event):
        # `log` is also a TraceSource method: getattr would have returned that.
        node_map = NodeMap.from_dict(
            {"name": "m", "events": {"log": [{"name": "v", "node_id": "ns=2;i=1"}]}}
        )
        client = OPCUAClient(endpoint="opc.tcp://plc01:4840", node_map=node_map)
        client._init_trace_source(zelos_sdk.TraceSource("OPC-UA") if shared else None)
        assert client._source.name == source
        assert client._events["log"].name == event
        client._log_values({"log": {"v": 3.5}})


async def _wait_until(predicate, timeout: float, interval: float = 0.1) -> None:
    """Poll `predicate` until true or fail the test."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"Condition not met within {timeout}s")


# =============================================================================
# Action Tests
# =============================================================================


class TestActionsUnit:
    """Actions with no connection: input validation raises, it does not report."""

    @pytest.fixture
    def offline_client(self):
        node_map = NodeMap.from_dict(
            {
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
        )
        client = OPCUAClient(node_map=node_map)
        actions.set_runner(OPCUARunner([client]))
        yield client
        actions.set_runner(None)

    def test_registered_action_names(self):
        """register_actions publishes exactly the documented surface."""
        from zelos_sdk.actions import ActionsRegistry

        assert sorted(actions.register_actions(ActionsRegistry())) == [
            "auto_config",
            "browse_nodes",
            "discovered_map",
            "get_status",
            "list_nodes",
            "list_writable_nodes",
            "read_named_node",
            "read_node",
            "write_named_node",
            "write_node",
        ]

    def test_get_status(self, offline_client):
        statuses = actions.get_status()
        assert statuses["count"] == 1
        result = statuses["servers"][0]
        assert result["connected"] is False
        assert result["nodes"] == 4
        assert "endpoint" in result and "security_mode" in result

    def test_list_nodes(self, offline_client):
        result = actions.list_nodes()
        assert result["count"] == 4
        assert {n["name"] for n in result["nodes"]} == {"temp", "press", "setpoint", "output"}

    def test_list_writable_nodes_filters(self, offline_client):
        result = actions.list_writable_nodes()
        assert {n["name"] for n in result["nodes"]} == {"setpoint", "output"}

    def test_no_client_raises(self):
        actions.set_runner(None)
        with pytest.raises(RuntimeError, match="No OPC-UA client"):
            actions.get_status()

    def test_no_node_map_raises(self):
        actions.set_runner(OPCUARunner([OPCUAClient()]))
        try:
            with pytest.raises(ValueError, match="No node map loaded"):
                actions.read_named_node("anything")
            with pytest.raises(ValueError, match="No node map loaded"):
                actions.write_named_node("anything", 100)
        finally:
            actions.set_runner(None)

    def test_unknown_name_raises(self, offline_client):
        with pytest.raises(ValueError, match="Unknown node name"):
            actions.read_named_node("nonexistent")
        with pytest.raises(ValueError, match="Unknown node name"):
            actions.write_named_node("nonexistent", 100)

    def test_readonly_node_raises(self, offline_client):
        """The writability check fires before any connection is attempted."""
        with pytest.raises(ValueError, match="not writable"):
            actions.write_named_node("press", 100)


class TestActionDispatch:
    """`_run_coro` is the only path from an action to the event loop."""

    async def test_timeout_cancels_the_coroutine(self):
        """A timed-out write must not land on the PLC after the action failed."""
        landed = False

        async def slow_write():
            nonlocal landed
            await asyncio.sleep(0.3)
            landed = True

        runner = OPCUARunner([OPCUAClient()])
        runner._loop = asyncio.get_running_loop()
        with pytest.raises(TimeoutError):
            await asyncio.to_thread(runner._run_coro, slow_write(), 0.1)
        # Past when the write would have completed had it not been cancelled.
        await asyncio.sleep(0.6)
        assert landed is False

    def test_without_a_running_loop_raises(self):
        """No ad-hoc connect: it drove the client from a foreign event loop."""

        async def never_runs():
            raise AssertionError("must not run")

        with pytest.raises(RuntimeError, match="not running"):
            OPCUARunner([OPCUAClient()])._run_coro(never_runs(), 1.0)


class TestActionsIntegration:
    """Actions against the demo server."""

    async def test_list_nodes(self, bound_client):
        names = {n["name"] for n in actions.list_nodes()["nodes"]}
        assert {"temp_sensor1", "running", "production_count"} <= names

    async def test_list_writable(self, bound_client):
        names = {n["name"] for n in actions.list_writable_nodes()["nodes"]}
        assert {"setpoint", "speed_setpoint", "output1"} <= names

    async def test_get_status(self, bound_client):
        result = actions.get_status("127.0.0.1")  # sanitized like the configured name
        assert result["server"] == "127_0_0_1"
        assert result["connected"] is True
        assert result["nodes"] > 0

    async def test_write_readonly_raises(self, bound_client):
        with pytest.raises(ValueError, match="not writable"):
            actions.write_named_node("input1", 1)

    async def test_action_reuses_the_polling_connection(self, demo_server, node_map):
        """An action called while polling rides the live session, not a new one."""
        client = OPCUAClient(endpoint=demo_server.endpoint, node_map=node_map, poll_interval=0.2)
        client.start()
        runner = OPCUARunner([client])
        actions.set_runner(runner)
        task = asyncio.create_task(runner._run_async())
        try:
            await _wait_until(lambda: client._poll_count >= 1, 15.0)
            session = client._client

            # The SDK calls actions from its own thread; to_thread reproduces that
            # so _run_coro takes the run_coroutine_threadsafe path.
            result = await asyncio.to_thread(actions.read_named_node, "temp_sensor1")
            assert 10 < result["value"] < 50
            assert client._client is session
            assert client._poll_count > 0
        finally:
            actions.set_runner(None)
            runner.stop()
            await asyncio.wait_for(task, 10.0)
