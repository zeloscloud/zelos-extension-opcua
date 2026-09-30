"""Node map, codecs, actions, and integration against the demo server."""

import asyncio
import json
import logging
import socket
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import zelos_sdk
from asyncua import ua
from asyncua.ua.ua_binary import to_binary

from zelos_extension_opcua import actions
from zelos_extension_opcua.client import (
    OPCUAClient,
    OPCUARunner,
    SharedSource,
    coerce_text,
    decode_value,
    encode_value,
    is_connection_error,
    parse_node_id_to_ua,
)
from zelos_extension_opcua.node_map import (
    NodeMap,
    format_nsu_node_id,
    parse_node_id,
)

DEMO_MAP_PATH = Path(__file__).parent.parent / "zelos_extension_opcua" / "demo" / "plc_device.json"

# =============================================================================
# Node map, codecs, error classification
# =============================================================================


def test_node_id_parsing():
    """The identifier is typed for asyncua: a str GUID or opaque id never matches."""
    guid = "12345678-1234-5678-1234-567812345678"
    assert parse_node_id("ns=2;s=Temperature.Sensor1") == (2, "s", "Temperature.Sensor1")
    assert parse_node_id("ns=0;i=85") == (0, "i", 85)
    assert parse_node_id(f"ns=1;g={guid}") == (1, "g", uuid.UUID(guid))
    assert parse_node_id("nsu=urn:zelos:demo:plc;s=A;B") == ("urn:zelos:demo:plc", "s", "A;B")
    assert parse_node_id("nsu=urn:a%3Bb%25;i=7") == ("urn:a;b%", "i", 7)
    assert parse_node_id(format_nsu_node_id("urn:a;b%3B", "i=7")) == ("urn:a;b%3B", "i", 7)
    for bad in ("invalid", "ns=2", "i=85", "nsu=;i=1", "nsu=urn:x", "nsu=urn:x;g=nope"):
        with pytest.raises(ValueError):
            parse_node_id(bad)

    assert parse_node_id_to_ua(f"ns=1;g={guid}").NodeIdType == ua.NodeIdType.Guid
    opaque = parse_node_id_to_ua("ns=1;b=AQID")
    assert (opaque.Identifier, opaque.NodeIdType) == (b"\x01\x02\x03", ua.NodeIdType.ByteString)
    assert parse_node_id_to_ua("ns=2;s=T").NodeIdType == ua.NodeIdType.String


@pytest.mark.parametrize(
    ("node", "match"),
    [
        ({"node_id": "ns=2;s=Test", "datatype": "invalid"}, "Invalid datatype"),
        ({"node_id": "invalid_format"}, "Invalid node ID format"),
        # A bad identifier is a map error, not a "connection failed" at runtime.
        ({"node_id": "ns=2;i=abc"}, "ns=2;i=abc"),
        ({"node_id": "ns=2;g=not-a-guid"}, "ns=2;g=not-a-guid"),
        ({"node_id": "ns=2;b=not_base64!!"}, "ns=2;b=not_base64"),
    ],
)
def test_bad_node_rejected_at_load(node, match):
    with pytest.raises(ValueError, match=match):
        NodeMap.from_dict({"events": {"e": [{"name": "n", **node}]}})


def test_demo_map_loads():
    node_map = NodeMap.from_file(DEMO_MAP_PATH)
    assert node_map.name == "demo_plc"
    assert len(node_map.nodes) == len({n.name for n in node_map.nodes})


def test_names_are_sanitized():
    node_map = NodeMap.from_dict(
        {
            "name": "site.plc:1",
            "events": {"temp/zone a": [{"name": "Sensor.1", "node_id": "ns=2;s=T1"}]},
        }
    )
    assert node_map.name == "site_plc_1"
    # `/` and spaces are legal in event names under the SDK grammar
    assert list(node_map.events) == ["temp/zone a"]
    assert node_map.nodes[0].name == "Sensor_1"
    # Lookups use the name the user sees in the trace.
    assert node_map.get_by_name("Sensor_1") is not None
    assert node_map.get_by_name("Sensor.1") is None


@pytest.mark.parametrize(
    ("events", "match"),
    [
        (
            {
                "zone.a": [{"name": "n1", "node_id": "ns=2;s=A"}],
                "zone:a": [{"name": "n2", "node_id": "ns=2;s=B"}],
            },
            "Duplicate event name",
        ),
        (
            {
                "e": [
                    {"name": "a.b", "node_id": "ns=2;s=A"},
                    {"name": "a:b", "node_id": "ns=2;s=B"},
                ]
            },
            "Duplicate node name 'a_b'",
        ),
    ],
)
def test_collision_after_sanitization_raises(events, match):
    with pytest.raises(ValueError, match=match):
        NodeMap.from_dict({"events": events})


def test_node_name_across_events_is_qualified():
    node_map = NodeMap.from_dict(
        {
            "events": {
                "a": [{"name": "sensor1", "node_id": "ns=2;s=A"}],
                "b/c": [{"name": "sensor1", "node_id": "ns=2;s=B"}],
            }
        }
    )
    assert node_map.get_by_name("b/c/sensor1").node_id == "ns=2;s=B"
    assert node_map.get_by_name("nonexistent") is None
    with pytest.raises(ValueError, match="use one of: a/sensor1, b/c/sensor1"):
        node_map.get_by_name("sensor1")


def test_value_codec():
    for raw, datatype, expected in [
        (1000, "uint16", 1000),
        (-50000, "int32", -50000),
        (True, "bool", True),
        (3.5, "float32", 3.5),
        ("hello", "string", "hello"),
        (None, "float32", None),
    ]:
        assert decode_value(raw, datatype) == expected
        if raw is not None:
            assert encode_value(raw, datatype) == raw
    assert decode_value(1000, "uint16", scale=0.1) == 100
    assert decode_value(100.0, "float32", scale=2.0) == 200.0
    assert encode_value(100, "uint16", scale=0.1) == 1000
    assert encode_value(200.0, "float32", scale=2.0) == 100.0


def test_wide_ints_stay_exact_and_in_range():
    """int64/uint64 never round-trip through a float's 53 bits; a wider value is rejected."""
    assert decode_value(2**63 - 1, "int64") == 2**63 - 1
    assert decode_value(-(2**63), "int64") == -(2**63)
    assert decode_value(2**64 - 1, "uint64") == 2**64 - 1
    assert decode_value(2**53 + 1, "uint64") == 2**53 + 1
    written = encode_value(coerce_text("9007199254740993", "uint64"), "uint64")
    assert written == 2**53 + 1
    with pytest.raises(ValueError, match="out of range for int16"):
        decode_value(-8258223195483293696, "int16")


def test_render_text():
    """A BaseDataType node's value, whatever its type, lands as text."""
    for raw, expected in [
        (True, "true"),
        (1.5, "1.5"),
        (ua.LocalizedText("on", "en"), "on"),
        (ua.NodeId(5, 2), "ns=2;i=5"),
        (ua.QualifiedName("x", 2), "2:x"),
        (datetime(2026, 1, 2, 3, 4, 5), "2026-01-02T03:04:05+00:00"),
        (b"\x01\xff", "01ff"),
        (ua.StatusCode(ua.StatusCodes.BadSensorFailure), "BadSensorFailure"),
        (ua.ExtensionObject(), "ExtensionObject(TypeId="),
    ]:
        assert decode_value(raw, "string").startswith(expected)


def test_coerce_text():
    """Write actions take text, so bool and string nodes are writable at all."""
    for text, datatype, expected in [
        ("FALSE", "bool", False),
        ("1", "bool", True),
        ("12.5", "float32", 12.5),
        ("-7", "int16", -7),
        ("Test Device", "string", "Test Device"),
    ]:
        result = coerce_text(text, datatype)
        assert result == expected and type(result) is type(expected)
    for text, datatype in [("maybe", "bool"), ("abc", "float32"), ("n/a", "uint16")]:
        with pytest.raises(ValueError, match="Cannot parse"):
            coerce_text(text, datatype)


def test_connection_error_classification():
    """By exception type, never message text."""
    from asyncua.ua.uaerrors import BadNodeIdUnknown, BadSecureChannelClosed, BadSessionIdInvalid

    black_holed = Exception("Unhandled exception while sending request to OPC UA server")
    black_holed.__cause__ = TimeoutError()
    for error in (
        ConnectionError("Connection is closed"),
        ConnectionRefusedError(61, "refused"),
        TimeoutError(),
        BadSessionIdInvalid(),
        BadSecureChannelClosed(),
        black_holed,
    ):
        assert is_connection_error(error) is True, error
    for error in (BadNodeIdUnknown(), ua.UaError("something else"), ValueError("bad")):
        assert is_connection_error(error) is False, error


def test_plc_simulator():
    from zelos_extension_opcua.demo.simulator import PLCSimulator

    sim = PLCSimulator()
    sim.motor_running, sim.motor_speed = False, 100
    assert sim.update(dt=1.0)["motor_speed"] < 100
    sim.motor_running, sim.motor_speed = True, 1500
    energy = sim.energy_total
    sim.update(dt=1.0)
    assert sim.energy_total > energy


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


class TestDemoServerIntegration:
    """Integration tests against the demo server."""

    async def test_read_and_write_node_types(self, client):
        """Every mapped datatype reads back as its Python type, and writes round-trip."""
        for name, check in [
            ("temp_sensor1", lambda v: isinstance(v, float) and 10 < v < 50),
            ("production_count", lambda v: isinstance(v, int) and v >= 0),
            ("total_energy", lambda v: isinstance(v, float) and v >= 0),
            ("device_name", lambda v: isinstance(v, str) and v),
        ]:
            assert check(await client.read_node_value(client.node_map.get_by_name(name))), name
        for name, value in [("setpoint", 30.0), ("running", True), ("running", False),
                            ("device_name", "Test Device")]:  # fmt: skip
            node = client.node_map.get_by_name(name)
            await client.write_node_value(node, value)
            assert await client.read_node_value(node) == value
        with pytest.raises(ValueError, match="not writable"):
            await client.write_node_value(client.node_map.get_by_name("input1"), True)

    async def test_write_sends_the_value_only(self, client, monkeypatch):
        """Encoding mask 0x01: no StatusCode or timestamps, which some servers refuse."""
        sent = []
        write = client._client.uaclient.session.write

        async def spy(params):
            sent.extend(params.NodesToWrite)
            return await write(params)

        monkeypatch.setattr(client._client.uaclient.session, "write", spy)
        await client.write_node_value(client.node_map.get_by_name("setpoint"), 31.0)
        (dv,) = [w.Value for w in sent]
        assert to_binary(ua.DataValue, dv)[0] == 0x01
        node = client._ua_nodes["ns=2;s=Temperature.Setpoint"]
        assert dv.Value.VariantType == (await node.read_data_value()).Value.VariantType

    async def test_poll_is_one_read_per_chunk(self, client, monkeypatch):
        calls = 0
        original = client._client.uaclient.read

        async def counting(*args, **kwargs):
            nonlocal calls
            calls += 1
            return await original(*args, **kwargs)

        monkeypatch.setattr(client._client.uaclient, "read", counting)
        samples = await client._read_targets(client._poll_targets)
        assert calls == 1
        assert len(samples) == len(client.node_map.nodes)
        events = set(await client._poll_nodes())
        assert events >= {"temperature", "pressure", "motor", "counters", "digital_io", "status"}


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


async def test_bad_nodes_cost_only_themselves_and_report_once(demo_server, caplog):
    """A missing node and a string on a float32 node: the rest delivered, one ERROR each."""
    node_map = NodeMap.from_dict(
        {
            "name": "partial",
            "events": {
                "mix": [
                    {"name": "good", "node_id": "ns=2;s=Temperature.Sensor1"},
                    {"name": "bogus", "node_id": "ns=2;s=Does.Not.Exist"},
                    {"name": "wrong_type", "node_id": "ns=2;s=Status.DeviceName"},
                    {"name": "also_good", "node_id": "ns=2;s=Analog.Voltage"},
                ]
            },
        }
    )
    client = OPCUAClient(endpoint=demo_server.endpoint, node_map=node_map)
    assert await client.connect() is True
    try:
        with caplog.at_level(logging.ERROR, logger="zelos_extension_opcua.client"):
            for _ in range(3):
                assert set((await client._poll_nodes())["mix"]) == {"good", "also_good"}
        errors = sorted(r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
        assert len(errors) == 2
        assert "Does.Not.Exist" in errors[0] and "decode failed" in errors[1]
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
        client._poll_health = failing_poll
        client._jobs = client._schedule({})
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
        shared_source = SharedSource(zelos_sdk.TraceSource("OPC-UA")) if shared else None
        client._init_trace_source(shared_source)
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

    def test_listing(self, offline_client):
        status = actions.get_status()["servers"][0]
        assert (status["connected"], status["nodes"]) == (False, 4)
        assert status["peak_rss_mb"] > 0
        assert {n["name"] for n in actions.list_nodes()["nodes"]} == {
            "temp",
            "press",
            "setpoint",
            "output",
        }
        assert {n["name"] for n in actions.list_writable_nodes()["nodes"]} == {"setpoint", "output"}

    def test_input_errors_raise(self, offline_client):
        with pytest.raises(ValueError, match="Unknown node name"):
            actions.read_named_node("nonexistent")
        # Checked from the map before any connection is attempted.
        with pytest.raises(ValueError, match="not writable"):
            actions.write_named_node("press", 100)
        actions.set_runner(OPCUARunner([OPCUAClient()]))
        with pytest.raises(ValueError, match="No node map loaded"):
            actions.write_named_node("anything", 100)
        actions.set_runner(None)
        with pytest.raises(RuntimeError, match="No OPC-UA client"):
            actions.get_status()


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


async def test_action_reuses_the_polling_connection(demo_server, node_map):
    """An action called while polling rides the live session, not a new one."""
    client = OPCUAClient(endpoint=demo_server.endpoint, node_map=node_map, poll_interval=0.2)
    client.start()
    runner = OPCUARunner([client])
    actions.set_runner(runner)
    task = asyncio.create_task(runner._run_async())
    try:
        await _wait_until(lambda: client._poll_count >= 1, 15.0)
        session = client._client

        # The SDK calls actions from its own thread: the run_coroutine_threadsafe path.
        result = await asyncio.to_thread(actions.read_named_node, "temp_sensor1")
        assert 10 < result["value"] < 50
        assert client._client is session
        status = actions.get_status("127.0.0.1")  # sanitized like the configured name
        assert (status["server"], status["connected"]) == ("127_0_0_1", True)
    finally:
        actions.set_runner(None)
        runner.stop()
        await asyncio.wait_for(task, 10.0)


@pytest.mark.parametrize("sends", [False, True])
def test_write_timeout_after_send_is_unknown_outcome(monkeypatch, sends):
    """A timeout once the Write went out cannot claim failure: it may have applied."""

    class Runner:
        def _run_coro(self, coro, timeout):
            return asyncio.run(asyncio.wait_for(coro, timeout))

    async def write(sent):
        if sends:
            sent.append(time.monotonic())
        await asyncio.sleep(1)

    monkeypatch.setattr(actions, "_get_runner", Runner)
    sent: list[float] = []
    with pytest.raises(TimeoutError) as raised:
        actions._run(SimpleNamespace(timeout=0.05), write(sent), "Write to 'x'", sent=sent)
    assert ("may have applied it" in str(raised.value)) is sends
