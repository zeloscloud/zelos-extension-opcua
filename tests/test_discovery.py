"""Live discovery, server health and auto-configure, end to end against the simulator."""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from pathlib import Path

import pytest
import zelos_sdk
from asyncua import ua
from zelos_sdk.extensions.actions import get_standalone_actions

from zelos_extension_opcua import ACTION_PREFIX, actions
from zelos_extension_opcua.cli import app
from zelos_extension_opcua.client import OPCUAClient, OPCUARunner
from zelos_extension_opcua.demo import profiles
from zelos_extension_opcua.demo.sim_server import Simulator
from zelos_extension_opcua.discovery import (
    HEALTH_EVENT,
    Found,
    assign_names,
    read_many,
    vendor_segments,
)
from zelos_extension_opcua.node_map import NodeMap


async def discovered(sim: Simulator, caplog=None) -> tuple[OPCUAClient, dict]:
    """A connected no-map client and its first poll."""
    client = OPCUAClient(endpoint=sim.endpoint, name="plc")
    client.start(zelos_sdk.TraceSource("OPC-UA"))
    assert await client.connect() is True
    return client, await client._poll_nodes()


async def test_gateway_discovery_and_health():
    async with Simulator("gateway", port=0) as sim:
        client, values = await discovered(sim)
        try:
            # Kepware Channel.Device.Tag; _System/_Statistics and Server not followed.
            assert set(client.node_map.events) == {"Genset/Controller", "ModbusTCP/PowerMeter"}
            meter = client.node_map.events["ModbusTCP/PowerMeter"]
            assert {n.name for n in meter} == {t[0] for t in profiles.POWER_METER}
            assert all(n.writable is False and n.node_id.startswith("nsu=") for n in meter)
            assert 200 < values["ModbusTCP/PowerMeter"]["Voltage_L1"] < 260

            health = values[HEALTH_EVENT]
            assert health["state_name"] == "Running" and health["state"] == 0
            assert abs(health["clock_skew_ms"]) < 2000  # asyncua ticks CurrentTime at 1 Hz
            assert health["current_session_count"] == 1
            assert {"current_time", "start_time", "service_level"} <= set(health)
            assert client._events[HEALTH_EVENT].name == "plc/_server"
            client._log_values(values)
        finally:
            await client.disconnect()


async def test_device_units_and_skip_summary(caplog):
    async with Simulator("device", port=0) as sim:
        with caplog.at_level(logging.INFO, logger="zelos_extension_opcua.client"):
            client, _ = await discovered(sim)
        await client.disconnect()
    params = {n.name: n for n in client.node_map.events["Pump01/ParameterSet"]}
    assert (params["Voltage"].unit, params["Temperature"].unit) == ("V", "degC")
    assert "range 0..480" in params["Voltage"].description
    summaries = [r.getMessage() for r in caplog.records if "Discovered" in r.getMessage()]
    assert len(summaries) == 1 and summaries[0].endswith("skipped: array 1, struct 1")


async def test_s7_honors_operation_limits():
    async with Simulator("s7", port=0) as sim:
        client, values = await discovered(sim)
        await client.disconnect()
    assert (client._browse_chunk, client._read_chunk) == (10, 20)
    services = [svc for _, svc in sim.request_log]
    assert "BrowseNext" in services
    # "DB"."tag" names the event by its DB, not PLC_1/DataBlocksGlobal/DB.
    assert set(client.node_map.events) == {"DB_Energy", "DB_Line", "DB_Tank"}
    # 19 tags + 9 health nodes: over MaxNodesPerRead, so chunked, never rejected.
    assert sum(len(v) for e, v in values.items() if e != HEALTH_EVENT) == 19


async def test_discovery_off_does_not_browse():
    async with Simulator("gateway", port=0) as sim:
        config = {"servers": [{"endpoint": sim.endpoint}], "advanced": {"discovery": False}}
        [client] = app.build_clients(app.resolve_servers(config, app.resolve_advanced(config)))
        client.start(zelos_sdk.TraceSource("OPC-UA"))
        assert await client.connect() is True
        await client.disconnect()
    assert "Browse" not in {svc for _, svc in sim.request_log}
    assert set(client._events) == {HEALTH_EVENT, "raw"}


async def test_discovered_map_action_round_trips(tmp_path):
    async with Simulator("gateway", port=0) as sim:
        runner = OPCUARunner([OPCUAClient(endpoint=sim.endpoint, name="gw", poll_interval=0.1)])
        runner.clients["gw"].start(zelos_sdk.TraceSource("OPC-UA"))
        task = asyncio.create_task(runner._run_async())
        actions.set_runner(runner)
        try:
            await wait_until(lambda: runner.clients["gw"]._poll_count > 0, 10.0)
            exported = await asyncio.to_thread(actions.discovered_map, "json")
            csv = await asyncio.to_thread(actions.discovered_map, "csv")
            with pytest.raises(ValueError, match="discovered nodes are read-only"):
                actions.write_named_node("ModbusTCP/PowerMeter/Relay1", "true")
        finally:
            actions.set_runner(None)
            runner.stop()
            await asyncio.wait_for(task, 10.0)

        assert csv["csv"].splitlines()[0] == "event,name,node_id,datatype,unit,description"
        assert len(csv["csv"].splitlines()) - 1 == exported["count"] == 24
        # The export is a node_map_file: saved and loaded, it polls the same nodes.
        path = tmp_path / "gw.json"
        path.write_text(json.dumps(exported["map"]))
        pinned = OPCUAClient(endpoint=sim.endpoint, node_map=NodeMap.from_file(path))
        assert await pinned.connect() is True
        values = await pinned._poll_nodes()
        await pinned.disconnect()
    assert sum(len(v) for e, v in values.items() if e != HEALTH_EVENT) == 24


async def wait_until(predicate, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"not met within {timeout}s"
        await asyncio.sleep(0.05)


async def test_undecodable_value_costs_only_its_item():
    """asyncua failing to parse one value must not fail the other items read with it."""

    class Session:
        async def read(self, params):
            ids = [rv.NodeId.Identifier for rv in params.NodesToRead]
            if "bad" in ids:
                raise struct.error("bad char in struct format")
            return [ua.DataValue(ua.Variant(i)) for i in ids]

    client = type("C", (), {"uaclient": Session()})()
    items = [(ua.NodeId(i, 2), None) for i in ("a", "bad", "c")]
    dvs = await read_many(client, items, 100)
    assert [dv.Value.Value for dv in dvs if dv.StatusCode.is_good()] == ["a", "c"]
    assert dvs[1].StatusCode.value == ua.StatusCodes.BadDecodingError


def found(identifier: str | int, name: str, ns: int = 2, path: tuple = ("Folder",)) -> tuple:
    return (Found(ua.NodeId(identifier, ns), name, path), "float32", "", "")


def test_collisions_are_renamed_deterministically():
    rows = [
        found("A.x", "x"),  # dotted id: event A
        found(7, "x", path=("A",)),  # browse path A: same event and field
        found("A.x", "x", ns=3),  # same id in another namespace
        found(8, "v", path=("Server",)),  # no longer reserved
    ]
    namespaces = [
        "http://opcfoundation.org/UA/",
        "urn:s",
        "urn:a",
        "http://opcfoundation.org/UA/DI/",
    ]
    events, renames = assign_names(rows, namespaces)
    again, _ = assign_names(rows[::-1], namespaces)
    assert events == again
    assert {e: [n.name for n in nodes] for e, nodes in events.items()} == {
        # nsu= id order: DI's A.x keeps x; urn:a i=7 takes the alias; urn:a A.x
        # would take x_a again, so falls back to _2.
        "A": ["x", "x_a", "x_2"],
        "Server": ["v"],
    }
    assert len(renames) == 2

    # Index shift across a server restart: same URIs, same names.
    shifted = [(Found(ua.NodeId(f.node_id.Identifier, f.node_id.NamespaceIndex + 1), f.name,
                      f.path), *rest) for f, *rest in rows]  # fmt: skip
    moved, _ = assign_names(shifted, [namespaces[0], "urn:new", *namespaces[1:]])
    assert moved == events


@pytest.mark.parametrize(
    ("identifier", "name", "segments"),
    [
        ("Channel1.Device1.Tag1", "Tag1", ["Channel1", "Device1", "Tag1"]),
        ('"DB_Line"."Speed"', "Speed", ["DB_Line", "Speed"]),
        ("MAIN.fbPump.rSpeed", "rSpeed", ["MAIN", "fbPump", "rSpeed"]),
        ("|var|CODESYS Control.Application.PLC_PRG.x", "x", ["Application", "PLC_PRG", "x"]),
        ("::Task1:counter", "counter", ["Task1", "counter"]),
        ("Temperature.Sensor1", "TempSensor", None),  # BrowseName disagrees: browse path
    ],
)
def test_vendor_ids(identifier, name, segments):
    assert vendor_segments(ua.NodeId(identifier, 2), name) == segments


async def test_auto_config_finds_local_servers():
    # Real well-known ports: auto_config probes nothing else on localhost.
    async with (
        Simulator("gateway", port=4840),
        Simulator("demo", port=48010, secure=True, secure_only=True),
    ):
        result = await asyncio.to_thread(actions.auto_config)
    assert result["status"] == "success"
    local = {
        s["endpoint"]: (s["security_mode"], s["security_policy"])
        for s in result["config"]["servers"]
        if s["endpoint"].startswith("opc.tcp://localhost:")
    }
    assert local == {
        "opc.tcp://localhost:4840/freeopcua/server/": ("None", "None"),
        "opc.tcp://localhost:48010/freeopcua/server/": ("SignAndEncrypt", "Basic256Sha256"),
    }
    assert "trusted on the server" in result["message"]

    schema = json.loads((Path(__file__).parents[1] / "config.schema.json").read_text())
    assert schema["ui:options"]["autoconfig"] == f"{ACTION_PREFIX}/auto_config"
    assert "auto_config" in get_standalone_actions()


async def test_reconnect_rebrowses():
    """A program change on the server shows up after the next reconnect."""
    sim = Simulator("gateway", port=0)
    await sim.start()
    port = sim.server.bserver.port
    try:
        client, _ = await discovered(sim)
        await sim.stop()
        sim = Simulator("gateway", port=port, nodes=3)
        await sim.start()
        client._connected = False
        assert await client._ensure_connected() is True
        values = await client._poll_nodes()
        await client.disconnect()
    finally:
        await sim.stop()
    assert len(values["Bulk/Group0000"]) == 3
    assert client._events["Bulk/Group0000"].name == "plc/Bulk/Group0000"
