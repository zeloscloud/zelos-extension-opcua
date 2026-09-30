"""Live discovery, server health and auto-configure, end to end against the simulator."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import socket
import struct
import time
from collections.abc import Sequence
from pathlib import Path

import pytest
import zelos_sdk
from asyncua import ua
from asyncua.client.ua_client import UaClient
from zelos_sdk.extensions.actions import get_standalone_actions

from zelos_extension_opcua import ACTION_PREFIX, actions, autoconfig
from zelos_extension_opcua.cli import app
from zelos_extension_opcua.client import SECURITY_POLICIES, OPCUAClient, OPCUARunner, SharedSource
from zelos_extension_opcua.demo import profiles
from zelos_extension_opcua.demo.sim_server import Simulator
from zelos_extension_opcua.discovery import (
    HEALTH_EVENT,
    Found,
    assign_names,
    field_datatype,
    read_many,
    vendor_segments,
)
from zelos_extension_opcua.node_map import NodeMap


async def discovered(sim: Simulator, **kwargs) -> tuple[OPCUAClient, dict]:
    """A connected no-map client and its first poll."""
    client = OPCUAClient(endpoint=sim.endpoint, name="plc", **kwargs)
    client.start(SharedSource(zelos_sdk.TraceSource("OPC-UA")))
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


@pytest.mark.parametrize(
    ("include", "exclude", "events", "summary"),
    [
        (["ModbusTCP/PowerMeter/**"], [], {"ModbusTCP/PowerMeter": None}, "pruned "),
        ([], ["**/Controller"], {"ModbusTCP/PowerMeter": None}, "pruned 1"),
        (["ModbusTCP/**"], ["**/Voltage_*"], {"ModbusTCP/PowerMeter": 3}, "filtered 3"),
    ],
    ids=["include_one_device", "exclude_branch", "exclude_beats_include"],
)
async def test_discovery_filters(monkeypatch, caplog, include, exclude, events, summary):
    """Filters narrow the map; a branch no pattern leaves is never browsed."""
    browsed: list[str] = []
    browse = UaClient.browse

    async def spy(self, params):
        browsed.extend(str(d.NodeId.Identifier) for d in params.NodesToBrowse)
        return await browse(self, params)

    monkeypatch.setattr(UaClient, "browse", spy)
    async with Simulator("gateway", port=0) as sim:
        with caplog.at_level(logging.INFO, logger="zelos_extension_opcua.client"):
            client, _ = await discovered(sim, include=include, exclude=exclude)
        await client.disconnect()
    assert set(client.node_map.events) == set(events)
    tags = {n.name for n in client.node_map.events["ModbusTCP/PowerMeter"]}
    dropped = {t[0] for t in profiles.POWER_METER if t[0].startswith("Voltage_")}
    expected = {t[0] for t in profiles.POWER_METER}
    assert tags == (expected - dropped if events["ModbusTCP/PowerMeter"] else expected)
    assert not [n for n in browsed if n.startswith("Genset.")]  # the device, nor its tags
    [line] = [r.getMessage() for r in caplog.records if "Discovered" in r.getMessage()]
    assert summary in line


async def test_device_units_types_and_skip_summary(caplog):
    async with Simulator("device", port=0) as sim:
        with caplog.at_level(logging.INFO, logger="zelos_extension_opcua.client"):
            client, _ = await discovered(sim)
        # Abstract Number: an Int32 then a Double sample both land as float64.
        idx = await sim.server.get_namespace_index(profiles.DEVICE_URI)
        load = ua.NodeId("Pump01.ParameterSet.Load", idx)
        samples = []
        for sample in (ua.Variant(7, ua.VariantType.Int32), ua.Variant(2.5, ua.VariantType.Double)):
            # A read-time value: asyncua refuses a write whose variant type changes.
            sim.server.set_attribute_value_callback(load, lambda *_, v=sample: ua.DataValue(v))
            polled = (await client._poll_nodes())["Pump01/ParameterSet"]
            samples.append((polled["Load"], polled["Mode"]))
        subscribed = {t[1].name for t in client._monitored.values()}
        await client.disconnect()
    assert [(type(v), v) for v, _ in samples] == [(float, 7.0), (float, 2.5)]
    # BaseDataType: text, whatever the value's type.
    assert [mode for _, mode in samples] == ["0", "0"]
    params = {n.name: n for n in client.node_map.events["Pump01/ParameterSet"]}
    assert (params["Load"].datatype, params["Mode"].datatype) == ("float64", "string")
    # BaseDataType is polled; concrete and Number stay subscribed.
    assert {"Voltage", "Load"} <= subscribed and "Mode" not in subscribed
    assert client.status()["polled_variant"] == 1
    assert (params["Voltage"].unit, params["Temperature"].unit) == ("V", "degC")
    assert "range 0..480" in params["Voltage"].description
    summaries = [r.getMessage() for r in caplog.records if "Discovered" in r.getMessage()]
    assert len(summaries) == 1 and summaries[0].endswith("skipped: array 1, struct 1")


_ID = ua.ObjectIds
_VT = ua.VariantType


@pytest.mark.parametrize(
    ("dtype", "sample", "expected"),
    [
        (_ID.Number, None, "float64"),
        (_ID.Integer, None, "int64"),
        (_ID.UInteger, None, "uint64"),
        (_ID.UInt64, None, "uint64"),  # concrete: exact, not widened
        (_ID.UInt64, (2**64 - 1, _VT.UInt64), "uint64"),  # rank -2, scalar value
        (_ID.BaseDataType, (True, _VT.Boolean), "string"),  # value type may change
        (_ID.BaseDataType, (ua.LocalizedText("on"), _VT.LocalizedText), "string"),
        (_ID.Enumeration, (3, _VT.Int32), "float64"),  # other untyped: by kind
    ],
)
def test_field_datatype(dtype, sample, expected):
    dv = ua.DataValue(ua.Variant(*sample)) if sample else None
    assert field_datatype(ua.NodeId(dtype), dv) == (expected, "")


async def test_vendor_integer_subtypes_stay_exact():
    """A DataType deriving from Int64 / UInt64 (two levels here) is typed as it, not float64."""
    async with Simulator("device", port=0) as sim:
        server = sim.server
        idx = await server.register_namespace("urn:test:vendor")
        counter = await server.get_node(ua.ObjectIds.UInt64).add_data_type(idx, "Counter64")
        wide = await counter.add_data_type(idx, "WideCounter")
        signed = await server.get_node(ua.ObjectIds.Int64).add_data_type(idx, "Ticks")
        folder = await server.nodes.objects.add_folder(idx, "Vendor")
        for name, value, vtype, dtype in (
            ("wide", 2**64 - 1, ua.VariantType.UInt64, wide),
            ("ticks", -(2**62) - 1, ua.VariantType.Int64, signed),
        ):
            await folder.add_variable(idx, name, ua.Variant(value, vtype), datatype=dtype.nodeid)
        client, values = await discovered(sim)
        await client.disconnect()
    types = {n.name: n.datatype for n in client.node_map.events["Vendor"]}
    assert types == {"wide": "uint64", "ticks": "int64"}
    assert values["Vendor"] == {"wide": 2**64 - 1, "ticks": -(2**62) - 1}  # no float round trip


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
        client.start(SharedSource(zelos_sdk.TraceSource("OPC-UA")))
        assert await client.connect() is True
        await client.disconnect()
    assert "Browse" not in {svc for _, svc in sim.request_log}
    assert set(client._events) == {HEALTH_EVENT, "raw"}


async def test_discovered_map_action_round_trips(tmp_path):
    async with Simulator("gateway", port=0) as sim:
        runner = OPCUARunner([OPCUAClient(endpoint=sim.endpoint, name="gw", poll_interval=0.1)])
        runner.clients["gw"].start(SharedSource(zelos_sdk.TraceSource("OPC-UA")))
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


async def test_undecodable_node_leaves_polling(monkeypatch):
    """Not re-read item by item every cycle: dropped until the next reconnect."""
    reads = []

    async def fake_read_many(client, items, chunk, max_age=0.0):
        reads.append(len(items))
        return [ua.DataValue(StatusCode=ua.StatusCode(ua.StatusCodes.BadDecodingError))] + [
            ua.DataValue(ua.Variant(1.0)) for _ in items[1:]
        ]

    node_map = NodeMap.from_dict(
        {"name": "m", "events": {"e": [{"name": n, "node_id": f"ns=2;s={n}"} for n in "ab"]}}
    )
    client = OPCUAClient(node_map=node_map)
    client._client, client._connected = object(), True
    client._poll_targets = [("e", n, n.node_id) for n in node_map.nodes]
    monkeypatch.setattr("zelos_extension_opcua.client.read_many", fake_read_many)
    await client._poll_nodes()
    await client._poll_nodes()
    assert reads == [2, 1]


def found(identifier: str | int, name: str, ns: int = 2, path: tuple = ("Folder",)) -> tuple:
    return (Found(ua.NodeId(identifier, ns), name, path), "float32", "", "")


NAMESPACES = ["http://opcfoundation.org/UA/", "urn:s", "urn:a"]


def names(rows: list, namespaces: Sequence[str] = NAMESPACES) -> dict[str, dict[str, str]]:
    """event -> {node id: field}."""
    events, _ = assign_names(rows, namespaces)
    return {e: {n.node_id: n.name for n in nodes} for e, nodes in events.items()}


def suffix(nsu: str, digits: int = 6) -> str:
    return "_" + hashlib.sha1(nsu.encode()).hexdigest()[:digits]


def test_collisions_hash_every_collider():
    rows = [
        found(1, "Flow.Rate", path=("A",)),  # sanitized to Flow_Rate
        found(2, "Flow_Rate", path=("A",)),
        found(3, "Level", path=("A",)),
        found("B.x", "x", ns=1),  # same field in another namespace
        found(4, "x", ns=2, path=("B",)),
    ]
    got = names(rows)
    assert got == {
        "A": {
            "nsu=urn:a;i=1": "Flow_Rate" + suffix("nsu=urn:a;i=1"),
            "nsu=urn:a;i=2": "Flow_Rate" + suffix("nsu=urn:a;i=2"),
            "nsu=urn:a;i=3": "Level",
        },
        "B": {
            "nsu=urn:s;s=B.x": "x" + suffix("nsu=urn:s;s=B.x"),
            "nsu=urn:a;i=4": "x" + suffix("nsu=urn:a;i=4"),
        },
    }
    _, renames = assign_names(rows, NAMESPACES)
    assert len(renames) == 4
    assert names(rows[::-1]) == got

    # Index shift across a server restart: same URIs, same names.
    shifted = [(Found(ua.NodeId(f.node_id.Identifier, f.node_id.NamespaceIndex + 1), f.name,
                      f.path), *rest) for f, *rest in rows]  # fmt: skip
    assert names(shifted, [NAMESPACES[0], "urn:new", *NAMESPACES[1:]]) == got


def test_collision_names_never_move():
    """A new collider leaves existing hashed names alone and ends a plain one."""
    two = [found(1, "Flow.Rate", path=("A",)), found(2, "Flow_Rate", path=("A",))]
    before = names(two)["A"]
    after = names([*two, found(3, "Flow:Rate", path=("A",))])["A"]
    assert {k: after[k] for k in before} == before
    assert len(set(after.values())) == 3

    alone = names([found(5, "Speed", path=("M",))])["M"]
    assert alone == {"nsu=urn:a;i=5": "Speed"}
    both = names([found(5, "Speed", path=("M",)), found(6, "Speed", path=("M",))])["M"]
    assert "Speed" not in both.values()
    assert both["nsu=urn:a;i=5"] == "Speed" + suffix("nsu=urn:a;i=5")


def test_truncation_collisions_are_hashed():
    long = "f" * 200  # both truncate to the same 128 bytes
    rows = [found(1, long + "1", path=("A",)), found(2, long + "2", path=("A",))]
    got = names(rows)["A"]
    for nsu, name in got.items():
        assert name == "f" * (128 - 7) + suffix(nsu)
    # Events cut to their trailing segment: A/B/E and C/B/E both become B/E.
    rows = [found(1, "x", path=("A", "B", "E")), found(2, "x", path=("C", "B", "E"))]
    events, _ = assign_names(rows, NAMESPACES, max_event_bytes=4)
    assert [n.name for n in events["B/E"]] == ["x" + suffix(f"nsu=urn:a;i={i}") for i in (1, 2)]


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


async def test_auto_config_finds_local_servers(monkeypatch):
    # Real well-known ports: auto_config probes nothing else on localhost.
    monkeypatch.setattr(autoconfig, "PROBE_TIMEOUT", 10.0)  # a loaded machine is slow
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
        "opc.tcp://localhost:4840/freeopcua/server/": ("default", "default"),
        "opc.tcp://localhost:48010/freeopcua/server/": ("SignAndEncrypt", "Basic256Sha256"),
    }
    assert "trusted on the server" in result["message"]
    assert (
        "at opc.tcp://localhost:4840/freeopcua/server/ (security default, added)"
        in (result["message"])
    )

    schema = json.loads((Path(__file__).parents[1] / "config.schema.json").read_text())
    assert schema["ui:options"]["autoconfig"] == f"{ACTION_PREFIX}/auto_config"
    assert "auto_config" in get_standalone_actions()


def closed_endpoint() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"opc.tcp://127.0.0.1:{s.getsockname()[1]}"


async def test_auto_config_checks_the_forms_servers():
    """Found, and never opened, each named; a refused port never reads as found."""
    gone = closed_endpoint()
    async with Simulator("gateway", port=0) as sim:
        form = [{"name": "gw", "endpoint": sim.endpoint}, {"name": "gone", "endpoint": gone}]
        result = await asyncio.to_thread(actions.auto_config, {"servers": form})
        assert result["config"]["servers"] == form
        assert result["message"] == (
            f"Found Zelos Sim Gateway at {sim.endpoint} (security default, already in the "
            f"form). Couldn't connect to {gone}."
        )
        result = await asyncio.to_thread(actions.auto_config, {"servers": form[1:]})
        assert result == {"status": "error", "message": f"Couldn't connect to {gone}."}


async def test_auto_config_secure_only_server(monkeypatch):
    async with Simulator(port=0, secure=True, secure_only=True) as sim:
        form = {"servers": [{"endpoint": sim.endpoint}]}
        result = await asyncio.to_thread(actions.auto_config, form)
        assert result["config"]["servers"] == [
            {
                "endpoint": sim.endpoint,
                "security_mode": "SignAndEncrypt",
                "security_policy": "Basic256Sha256",
            }
        ]
        assert "(SignAndEncrypt/Basic256Sha256, already in the form)" in result["message"]
        # The form's Advanced default is offered: the entry keeps following it.
        advanced = {"security_mode": "SignAndEncrypt", "security_policy": "Basic256Sha256"}
        result = await asyncio.to_thread(actions.auto_config, {**form, "advanced": advanced})
        assert result["config"]["servers"] == form["servers"]
        assert "(security default, already in the form)" in result["message"]

        monkeypatch.setattr(autoconfig, "POLICY_RANK", ("Aes256Sha256RsaPss",))
        result = await asyncio.to_thread(actions.auto_config, form)
    assert result == {
        "status": "error",
        "message": f"{sim.endpoint} offers no security policy this extension supports.",
    }


async def test_auto_config_empty_form_finds_nothing(monkeypatch):
    port = int(closed_endpoint().rsplit(":", 1)[1])
    monkeypatch.setattr(autoconfig, "WELL_KNOWN_PORTS", (port,))
    monkeypatch.setattr(autoconfig, "LDS_URL", f"opc.tcp://127.0.0.1:{port}")

    async def no_mdns() -> list[str]:
        return []

    monkeypatch.setattr(autoconfig, "_mdns_urls", no_mdns)
    result = await asyncio.to_thread(actions.auto_config, {"servers": []})
    assert result == {
        "status": "error",
        "message": f"No OPC UA server answered on this machine's usual ports ({port}) or "
        "over mDNS. Add a server with its endpoint.",
    }


def test_auto_config_never_downgrades():
    """A server accepting None gets "default": a secure Advanced default still applies."""
    from zelos_extension_opcua.autoconfig import _security

    def ep(mode, policy):
        return ua.EndpointDescription(SecurityMode=mode, SecurityPolicyUri=policy)

    none = ep(ua.MessageSecurityMode.None_, "http://opcfoundation.org/UA/SecurityPolicy#None")
    secure = ep(ua.MessageSecurityMode.SignAndEncrypt, SECURITY_POLICIES["Basic256Sha256"].URI)
    mode, policy = _security([none, secure])
    assert _security([secure]) == ("SignAndEncrypt", "Basic256Sha256")

    config = {
        "servers": [
            {"endpoint": "opc.tcp://a:4840", "security_mode": mode, "security_policy": policy}
        ],
        "advanced": {"security_mode": "SignAndEncrypt", "security_policy": "Basic256Sha256"},
    }
    [server] = app.resolve_servers(config, app.resolve_advanced(config))
    assert (server["security_mode"], server["downgrade_from"]) == ("SignAndEncrypt", "")


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


class RecordingSource:
    """A real TraceSource that records every write per instance."""

    made: list[RecordingSource] = []

    def __init__(self, name: str) -> None:
        self._source = REAL_SOURCE(name)
        self.name = name
        self.writes: list[tuple[str, dict]] = []
        self.stamped: list[tuple[str, dict]] = []
        RecordingSource.made.append(self)

    def add_event(self, name, fields):
        event, writes, stamped = self._source.add_event(name, fields), self.writes, self.stamped

        class Event:
            def log(self, **values):
                writes.append((name, values))
                event.log(**values)

            def log_at(self, stamp, **values):  # subscription rows and log records
                stamped.append((name, values))
                event.log_at(stamp, **values)

        Event.name = event.name
        return Event()

    def flush(self) -> None:
        self._source.flush()


REAL_SOURCE = zelos_sdk.TraceSource
METER = "ModbusTCP/PowerMeter"


# asyncua's server calls a coroutine unawaited when a monitored node is deleted.
@pytest.mark.filterwarnings("ignore:coroutine 'MonitoredItemService:RuntimeWarning")
async def test_added_field_rotates_the_shared_source(monkeypatch, caplog):
    """A PLC program change adds a tag to a live folder: every writer moves to a new segment."""
    monkeypatch.setattr(zelos_sdk, "TraceSource", RecordingSource)
    RecordingSource.made = []
    shared = SharedSource(zelos_sdk.TraceSource("OPC-UA"))
    plc, peer = (OPCUAClient(endpoint="", name=n) for n in ("plc", "peer"))
    async with Simulator("gateway", port=0) as sim:
        for client in (plc, peer):
            client.endpoint = sim.endpoint
            client.start(shared)
            assert await client.connect() is True
            client._log_values(await client._poll_nodes())
        [old] = RecordingSource.made

        server, idx = sim.server, await sim.server.get_namespace_index(profiles.GATEWAY_URI)
        meter = server.get_node(ua.NodeId("ModbusTCP.PowerMeter", idx))
        relay = server.get_node(ua.NodeId("ModbusTCP.PowerMeter.Relay1", idx))
        await server.delete_nodes([relay])
        await meter.add_variable(
            relay.nodeid, ua.QualifiedName("Relay1", idx), 1.0, ua.VariantType.Float
        )
        await meter.add_variable(
            ua.NodeId("ModbusTCP.PowerMeter.Added", idx),
            ua.QualifiedName("Added", idx),
            2.5,
            ua.VariantType.Float,
        )

        await plc.disconnect()
        with caplog.at_level(logging.INFO, logger="zelos_extension_opcua.client"):
            assert await plc._ensure_connected() is True
        # Resubscribed after the rotation, the added node included.
        assert plc.status()["subscribed"] == len(plc._poll_targets) == len(plc.node_map.nodes)
        writes_before = len(old.writes)
        plc._log_values(await plc._poll_nodes())
        peer._log_values(await peer._poll_nodes())
        await plc.disconnect()
        await peer.disconnect()

    [_, new] = RecordingSource.made
    assert new.name == "OPC-UA" and shared.source is new
    assert len(old.writes) == writes_before
    assert all(c._source is new for c in (plc, peer))
    logged = dict(new.writes)
    assert logged[f"plc/{METER}"]["Added"] == 2.5
    assert "Relay1" not in logged[f"plc/{METER}"] and "Voltage_L1" in logged[f"plc/{METER}"]
    assert {f"plc/{HEALTH_EVENT}", f"peer/{HEALTH_EVENT}", f"peer/{METER}"} <= set(logged)

    messages = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert (
        logging.INFO,
        f"[plc] Discovered fields added, trace source 'OPC-UA' rotated: {METER}: Added",
    ) in messages
    assert (
        logging.WARNING,
        f"[plc] Discovered fields changed datatype, not traced until restart: {METER}: Relay1",
    ) in messages


@pytest.mark.parametrize(("prefix", "source"), [("OPC-UA", "OPC-UA"), ("", "opcua_log")])
def test_log_records_follow_the_prefix_source(monkeypatch, prefix, source):
    """Logs land on `<prefix>/log` and move with a rotation; cleared, on `opcua_log`."""
    monkeypatch.setattr(zelos_sdk, "TraceSource", RecordingSource)
    RecordingSource.made = []
    root = logging.getLogger()
    before = list(root.handlers)
    log = logging.getLogger("zelos_extension_opcua.test")
    try:
        shared = app.open_sources(prefix)
        [first] = RecordingSource.made
        log.warning("one")
        assert first.name == source and ("log", "one") in logs(first)
        if shared:
            OPCUAClient(endpoint="", name="plc").start(shared)
            shared.clients[0]._rotate_source()
            [_, new] = RecordingSource.made
            log.warning("two")
            assert shared.source is new and logs(new) == [("log", "two")]
            assert ("log", "two") not in logs(first)
    finally:
        root.handlers[:] = before


def logs(source: RecordingSource) -> list[tuple[str, str]]:
    return [(e, v["message"]) for e, v in source.stamped if "message" in v]


async def test_browse_survives_a_continuation_point_cap(caplog):
    """s7 holds 3 continuation points a session; a Browse of 10 large folders needs 5."""
    async with Simulator("s7", port=0, nodes=500) as sim:
        client = OPCUAClient(endpoint=sim.endpoint, name="plc", transport="poll")
        client.start(SharedSource(zelos_sdk.TraceSource("OPC-UA")))
        with caplog.at_level(logging.WARNING, logger="zelos_extension_opcua.client"):
            assert await client.connect() is True
        await client.disconnect()
    groups = [e for e in client.node_map.events if e.startswith("Bulk/")]
    assert len(groups) == 5 and all(len(client.node_map.events[e]) == 100 for e in groups)
    assert len(client.node_map.nodes) == 19 + 500
    assert not [r for r in caplog.records if r.name.startswith("zelos") and r.levelno >= 30]
