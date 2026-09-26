"""Simulator address spaces beyond the demo PLC: gateway, s7, device, and served node maps.

Each builder populates an initialized server and returns a `Drifter` that moves
the read-only values. Writable nodes are never drifted, so a client write
persists and reads back.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from asyncua import Server, ua
from asyncua.common.node import Node as UaNode
from asyncua.common.structures104 import new_struct, new_struct_field

from zelos_extension_opcua.node_map import NodeMap

logger = logging.getLogger(__name__)

GATEWAY_URI = "urn:zelos:sim:gateway"
S7_URI = "urn:zelos:sim:s7"
DEVICE_URI = "urn:zelos:sim:device"
DI_URI = "http://opcfoundation.org/UA/DI/"

VT = ua.VariantType
Drift = Callable[[float], Any]
# (name, variant type, initial value, drift fn of seconds since start; None = writable)
Tag = tuple[str, VT, Any, Drift | None]


class Drifter:
    """Writes drifting values into read-only nodes on a fixed cadence."""

    def __init__(self, points: list[tuple[UaNode, VT, Drift]], interval: float = 0.5) -> None:
        """Initialize.

        Args:
            points: (node, variant type, drift fn) per drifting node
            interval: Seconds between updates
        """
        self.points = points
        self.interval = interval
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the update task."""
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the update task."""
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        start = time.monotonic()
        while True:
            t = time.monotonic() - start
            for node, vtype, fn in self.points:
                try:
                    await node.write_value(ua.Variant(fn(t), vtype))
                except Exception as e:
                    logger.debug("drift %s: %s", node.nodeid, e)
            await asyncio.sleep(self.interval)


def _wave(base: float, amp: float, period: float, noise: float = 0.0) -> Drift:
    return lambda t: base + amp * math.sin(2 * math.pi * t / period) + random.gauss(0, noise)


def _const(value: Any) -> Drift:
    return lambda t: value


def _qn(name: str, ns: int) -> ua.QualifiedName:
    return ua.QualifiedName(name, ns)


async def _add_tags(
    parent: UaNode,
    ns: int,
    node_id: Callable[[str], str],
    tags: list[Tag],
    points: list[tuple[UaNode, VT, Drift]],
) -> list[UaNode]:
    nodes = []
    for name, vtype, initial, drift in tags:
        node = await parent.add_variable(
            ua.NodeId(node_id(name), ns), _qn(name, ns), initial, vtype
        )
        if drift is None:
            await node.set_writable()
        else:
            points.append((node, vtype, drift))
        nodes.append(node)
    return nodes


# -- gateway: Kepware-shaped `Channel.Device.Tag` -----------------------------

POWER_METER: list[Tag] = [
    ("Voltage_L1", VT.Float, 230.0, _wave(230.0, 2.0, 60, 0.3)),
    ("Voltage_L2", VT.Float, 229.0, _wave(229.0, 2.0, 67, 0.3)),
    ("Voltage_L3", VT.Float, 231.0, _wave(231.0, 2.0, 73, 0.3)),
    ("Current_L1", VT.Float, 40.0, _wave(40.0, 5.0, 45, 0.5)),
    ("Current_L2", VT.Float, 38.0, _wave(38.0, 5.0, 52, 0.5)),
    ("Current_L3", VT.Float, 41.0, _wave(41.0, 5.0, 49, 0.5)),
    ("Power_Total", VT.Float, 26.2, _wave(26.2, 3.0, 45, 0.2)),
    ("Power_Factor", VT.Float, 0.95, _wave(0.95, 0.02, 90)),
    ("Frequency", VT.Float, 50.0, _wave(50.0, 0.02, 30, 0.005)),
    ("Energy_kWh", VT.Double, 12345.0, lambda t: 12345.0 + 26.2 * t / 3600),
    ("Temperature", VT.Float, 35.0, _wave(35.0, 1.0, 300, 0.1)),
    ("Relay1", VT.Boolean, False, None),
    ("Relay2", VT.Boolean, False, None),
    ("VoltageHighLimit", VT.UInt16, 253, None),
    ("VoltageLowLimit", VT.UInt16, 207, None),
    ("PowerLimit", VT.Int32, 50000, None),
]

GENSET: list[Tag] = [
    ("EngineSpeed", VT.Float, 1800.0, _wave(1800.0, 3.0, 20, 1.0)),
    ("CoolantTemp", VT.Float, 82.0, _wave(82.0, 1.5, 240, 0.1)),
    ("OilPressure", VT.Float, 410.0, _wave(410.0, 8.0, 60, 1.0)),
    ("BatteryVoltage", VT.Float, 27.6, _wave(27.6, 0.1, 120, 0.02)),
    ("GeneratorPower", VT.Float, 80.0, _wave(80.0, 6.0, 90, 0.5)),
    ("Running", VT.Boolean, True, _const(True)),
    # A transient warning code one minute in five.
    ("FaultCode", VT.UInt16, 0, lambda t: 0 if int(t // 60) % 5 else 1502),
    ("PowerSetpoint", VT.Float, 80.0, None),
]

# channel -> device -> tags
GATEWAY_CHANNELS: dict[str, dict[str, list[Tag]]] = {
    "ModbusTCP": {"PowerMeter": POWER_METER},
    "Genset": {"Controller": GENSET},
}


async def build_gateway(server: Server, ns: dict[str, int]) -> Drifter:
    """Kepware-shaped gateway: `_System` / `_Statistics` noise around real tags."""
    idx = ns[GATEWAY_URI]
    points: list[tuple[UaNode, VT, Drift]] = []
    objects = server.nodes.objects

    system = await objects.add_folder(ua.NodeId("_System", idx), _qn("_System", idx))
    await _add_tags(
        system,
        idx,
        lambda n: f"_System.{n}",
        [
            ("_ActiveTagCount", VT.UInt32, 0, _const(sum(len(t) for t in _all_tags()))),
            ("_Time", VT.String, "", lambda t: datetime.now(UTC).strftime("%H:%M:%S")),
        ],
        points,
    )

    for channel, devices in GATEWAY_CHANNELS.items():
        ch = await objects.add_folder(ua.NodeId(channel, idx), _qn(channel, idx))
        stats = await ch.add_folder(
            ua.NodeId(f"{channel}._Statistics", idx), _qn("_Statistics", idx)
        )
        await _add_tags(
            stats,
            idx,
            lambda n, c=channel: f"{c}._Statistics.{n}",
            [
                ("_SuccessfulReads", VT.UInt32, 0, lambda t: int(t * 2)),
                ("_FailedReads", VT.UInt32, 0, _const(0)),
                ("_TxBytes", VT.UInt32, 0, lambda t: int(t * 96)),
            ],
            points,
        )
        for device, tags in devices.items():
            path = f"{channel}.{device}"
            dev = await ch.add_folder(ua.NodeId(path, idx), _qn(device, idx))
            sys_folder = await dev.add_folder(
                ua.NodeId(f"{path}._System", idx), _qn("_System", idx)
            )
            await _add_tags(
                sys_folder,
                idx,
                lambda n, p=path: f"{p}._System.{n}",
                [
                    ("_Enabled", VT.Boolean, True, _const(True)),
                    ("_Error", VT.Boolean, False, _const(False)),
                    ("_ScanRate", VT.UInt32, 1000, _const(1000)),
                ],
                points,
            )
            await _add_tags(dev, idx, lambda n, p=path: f"{p}.{n}", tags, points)
    return Drifter(points)


def _all_tags() -> list[list[Tag]]:
    return [tags for devices in GATEWAY_CHANNELS.values() for tags in devices.values()]


# -- s7: Siemens S7-1500-shaped `"DB"."tag"` ----------------------------------

S7_DBS: dict[str, list[Tag]] = {
    # More than the s7 per-node reference cap, so browsing it needs BrowseNext.
    "DB_Line": [
        ("Speed", VT.Float, 0.0, _wave(1.2, 0.05, 30, 0.01)),
        ("SpeedSetpoint", VT.Float, 1.2, None),
        ("Running", VT.Boolean, True, _const(True)),
        ("PartCount", VT.Int32, 0, lambda t: int(t / 3)),
        ("RejectCount", VT.Int32, 0, lambda t: int(t / 97)),
        ("CycleTime", VT.Float, 3.0, _wave(3.0, 0.1, 40, 0.02)),
        ("Mode", VT.Int16, 1, _const(1)),
        ("Fault", VT.Boolean, False, _const(False)),
        ("MotorCurrent", VT.Float, 6.5, _wave(6.5, 0.4, 25, 0.05)),
        ("MotorTemp", VT.Float, 48.0, _wave(48.0, 2.0, 200, 0.1)),
        ("ConveyorLoad", VT.Float, 55.0, _wave(55.0, 10.0, 60, 1.0)),
        ("EStop", VT.Boolean, False, _const(False)),
        ("BatchId", VT.String, "B-0001", None),
    ],
    "DB_Tank": [
        ("Level", VT.Float, 50.0, _wave(50.0, 20.0, 180, 0.2)),
        ("Temperature", VT.Float, 21.0, _wave(21.0, 1.0, 300, 0.05)),
        ("InletValve", VT.Boolean, False, None),
        ("HighAlarm", VT.Boolean, False, _const(False)),
    ],
    "DB_Energy": [
        ("ActivePower", VT.Float, 12.0, _wave(12.0, 2.0, 60, 0.1)),
        ("EnergyImport", VT.Double, 1000.0, lambda t: 1000.0 + 12.0 * t / 3600),
    ],
}


async def build_s7(server: Server, ns: dict[str, int]) -> Drifter:
    """S7-1500-shaped PLC: `PLC_1/DataBlocksGlobal/"DB"/"DB"."tag"`."""
    idx = ns[S7_URI]
    points: list[tuple[UaNode, VT, Drift]] = []
    plc = await server.nodes.objects.add_object(ua.NodeId("PLC", idx), _qn("PLC_1", idx))
    dbs = await plc.add_object(ua.NodeId("DataBlocksGlobal", idx), _qn("DataBlocksGlobal", idx))
    for db, tags in S7_DBS.items():
        db_node = await dbs.add_object(ua.NodeId(f'"{db}"', idx), _qn(db, idx))
        await _add_tags(db_node, idx, lambda n, d=db: f'"{d}"."{n}"', tags, points)
    return Drifter(points)


# -- device: DI-like identification, EU metadata, awkward shapes --------------


def _unece(code: str) -> int:
    """UNECE unit code -> EUInformation.UnitId (Part 8 5.6.3)."""
    return sum(ord(c) << (8 * i) for i, c in enumerate(reversed(code)))


UNITS_NS = "http://www.opcfoundation.org/UA/units/un/cefact"

# (name, EU display, UNECE code, low, high, drift)
ANALOG = [
    ("Voltage", "V", "VLT", 0.0, 480.0, _wave(400.0, 4.0, 60, 0.5)),
    ("Current", "A", "AMP", 0.0, 100.0, _wave(22.0, 2.0, 45, 0.2)),
    ("Temperature", "degC", "CEL", -40.0, 150.0, _wave(61.0, 1.5, 240, 0.1)),
]

DEEP_LEVELS = 14


async def build_device(server: Server, ns: dict[str, int]) -> Drifter:
    """DI-like device: DeviceSet identity, EU/EURange, array, struct, Bad node, cycle, depth."""
    di, idx = ns[DI_URI], ns[DEVICE_URI]
    points: list[tuple[UaNode, VT, Drift]] = []

    # DI's well-known DeviceSet id.
    device_set = await server.nodes.objects.add_object(ua.NodeId(5001, di), _qn("DeviceSet", di))
    pump = await device_set.add_object(ua.NodeId("Pump01", idx), _qn("Pump01", idx))
    # DI models identification as properties.
    for name, value, vtype in (
        ("Manufacturer", ua.LocalizedText("Zelos Sim"), VT.LocalizedText),
        ("Model", ua.LocalizedText("SIM-PUMP-100"), VT.LocalizedText),
        ("SerialNumber", "SN-000123", VT.String),
    ):
        await pump.add_property(ua.NodeId(f"Pump01.{name}", idx), _qn(name, di), value, vtype)
    # DeviceHealthEnumeration: 0 NORMAL .. 4 MAINTENANCE_REQUIRED
    health = await pump.add_variable(
        ua.NodeId("Pump01.DeviceHealth", idx), _qn("DeviceHealth", di), 0, VT.Int32
    )
    points.append((health, VT.Int32, lambda t: 4 if int(t // 120) % 6 == 5 else 0))

    params = await pump.add_object(ua.NodeId("Pump01.ParameterSet", idx), _qn("ParameterSet", di))
    for name, display, code, low, high, drift in ANALOG:
        nid = f"Pump01.ParameterSet.{name}"
        node = await params.add_variable(ua.NodeId(nid, idx), _qn(name, idx), 0.0, VT.Double)
        eu = ua.EUInformation(
            NamespaceUri=UNITS_NS,
            UnitId=_unece(code),
            DisplayName=ua.LocalizedText(display),
            Description=ua.LocalizedText(display),
        )
        await node.add_property(
            ua.NodeId(f"{nid}.EngineeringUnits", idx),
            _qn("EngineeringUnits", 0),
            eu,
            VT.ExtensionObject,
            ua.NodeId(ua.ObjectIds.EUInformation),
        )
        await node.add_property(
            ua.NodeId(f"{nid}.EURange", idx),
            _qn("EURange", 0),
            ua.Range(low, high),
            VT.ExtensionObject,
            ua.NodeId(ua.ObjectIds.Range),
        )
        points.append((node, VT.Double, drift))

    vib = await params.add_variable(
        ua.NodeId("Pump01.ParameterSet.Vibration", idx),
        _qn("Vibration", idx),
        [0.0] * 4,
        VT.Double,
    )
    await vib.write_value_rank(ua.ValueRank.OneDimension)
    await vib.write_array_dimensions([4])
    points.append((vib, VT.Double, lambda t: [abs(random.gauss(1.0, 0.2)) for _ in range(4)]))

    # A vendor UDT: clients without its type definition see an opaque ExtensionObject.
    dtype, _ = await new_struct(
        server,
        idx,
        "PumpStatus",
        [
            new_struct_field("Running", VT.Boolean),
            new_struct_field("Speed", VT.Double),
            new_struct_field("FaultCode", VT.UInt16),
        ],
    )
    await server.load_data_type_definitions()
    pump_status = ua.PumpStatus  # registered on `ua` by load_data_type_definitions
    status = await params.add_variable(
        ua.NodeId("Pump01.ParameterSet.Status", idx),
        _qn("Status", idx),
        ua.Variant(pump_status(Running=True, Speed=1450.0, FaultCode=0), VT.ExtensionObject),
        datatype=dtype.nodeid,
    )
    points.append(
        (
            status,
            VT.ExtensionObject,
            lambda t: pump_status(Running=True, Speed=1450.0 + random.gauss(0, 5), FaultCode=0),
        )
    )

    broken = await params.add_variable(
        ua.NodeId("Pump01.ParameterSet.FlowSensor", idx), _qn("FlowSensor", idx), 0.0, VT.Double
    )
    await broken.write_value(
        ua.DataValue(
            ua.Variant(0.0, VT.Double), StatusCode_=ua.StatusCode(ua.StatusCodes.BadSensorFailure)
        )
    )

    # Reference cycle: a child that Organizes its own ancestor.
    diag = await pump.add_folder(ua.NodeId("Pump01.Diagnostics", idx), _qn("Diagnostics", idx))
    await diag.add_reference(pump.nodeid, ua.ObjectIds.Organizes)

    node = pump
    path = "Pump01.Deep"
    for level in range(1, DEEP_LEVELS + 1):
        path = f"{path}.Level{level:02d}"
        node = await node.add_folder(ua.NodeId(path, idx), _qn(f"Level{level:02d}", idx))
    deep = await node.add_variable(
        ua.NodeId(f"{path}.DeepValue", idx), _qn("DeepValue", idx), 0.0, VT.Double
    )
    points.append((deep, VT.Double, _wave(10.0, 1.0, 60)))
    return Drifter(points)


# -- --nodes: a large address space for measurement ---------------------------

BULK_URI = "urn:zelos:sim:bulk"
BULK_GROUP = 100  # variables per folder


def _bulk_value(index: int) -> Callable[[ua.NodeId, Any], ua.DataValue]:
    # Computed per read: N drifting nodes cost nothing until they are polled.
    return lambda *_: ua.DataValue(
        ua.Variant(float(index % 100 + math.sin(time.monotonic() + index)), VT.Float)
    )


async def build_bulk(server: Server, count: int) -> None:
    """`count` Float variables `Bulk.GroupNNNN.ValueNNN`, BULK_GROUP per folder.

    Added through one AddNodes call per folder: node-by-node creation took
    ~1.3 ms a variable (65 s for 50k).
    """
    idx = await server.register_namespace(BULK_URI)
    root = await server.nodes.objects.add_folder(ua.NodeId("Bulk", idx), _qn("Bulk", idx))
    session = server.iserver.isession
    bulk: list[tuple[ua.NodeId, int]] = []
    for group in range(-(-count // BULK_GROUP)):
        path = f"Bulk.Group{group:04d}"
        folder = await root.add_folder(ua.NodeId(path, idx), _qn(f"Group{group:04d}", idx))
        items = []
        for i in range(group * BULK_GROUP, min(count, (group + 1) * BULK_GROUP)):
            name = f"Value{i % BULK_GROUP:03d}"
            item = ua.AddNodesItem()
            item.RequestedNewNodeId = ua.NodeId(f"{path}.{name}", idx)
            item.BrowseName = _qn(name, idx)
            item.NodeClass = ua.NodeClass.Variable
            item.ParentNodeId = folder.nodeid
            item.ReferenceTypeId = ua.NodeId(ua.ObjectIds.HasComponent)
            item.TypeDefinition = ua.NodeId(ua.ObjectIds.BaseDataVariableType)
            attrs = ua.VariableAttributes()
            attrs.DisplayName = ua.LocalizedText(name)
            attrs.DataType = ua.NodeId(ua.ObjectIds.Float)
            attrs.Value = ua.Variant(0.0, VT.Float)
            attrs.ValueRank = ua.ValueRank.Scalar
            attrs.AccessLevel = attrs.UserAccessLevel = ua.AccessLevel.CurrentRead.mask
            item.NodeAttributes = attrs
            items.append(item)
            bulk.append((item.RequestedNewNodeId, i))
        for result in await session.add_nodes(items):
            result.StatusCode.check()
    for node_id, i in bulk:
        server.set_attribute_value_callback(node_id, _bulk_value(i))


# -- --map: serve any node map ------------------------------------------------

MAP_VARIANTS: dict[str, VT] = {
    "bool": VT.Boolean,
    "uint8": VT.Byte,
    "int8": VT.SByte,
    "uint16": VT.UInt16,
    "int16": VT.Int16,
    "uint32": VT.UInt32,
    "int32": VT.Int32,
    "float32": VT.Float,
    "uint64": VT.UInt64,
    "int64": VT.Int64,
    "float64": VT.Double,
    "string": VT.String,
}


def _initial(datatype: str) -> Any:
    if datatype == "bool":
        return False
    if datatype == "string":
        return ""
    return 0.0 if datatype.startswith("float") else 0


def _random_value(datatype: str) -> Any:
    # 0..100 fits every numeric datatype, including int8/uint8.
    if datatype == "bool":
        return random.random() < 0.5
    if datatype == "string":
        return f"sim-{random.randint(0, 999)}"
    if datatype.startswith("float"):
        return random.uniform(0.0, 100.0)
    return random.randint(0, 100)


async def build_map(server: Server, node_map: NodeMap) -> Drifter:
    """Serve every map node at its exact node id and datatype.

    `ns=N` indices the map names but the server lacks are filled with
    placeholder namespaces. Map folders live in a namespace registered after
    those, so they cannot collide with a map id.
    """
    for i in range(
        len(await server.get_namespace_array()),
        max((n.namespace for n in node_map.nodes), default=0) + 1,
    ):
        await server.register_namespace(f"urn:zelos:sim:map:ns{i}")
    idx = await server.register_namespace("urn:zelos:sim:map")

    points: list[tuple[UaNode, VT, Drift]] = []
    root = await server.nodes.objects.add_folder(
        ua.NodeId(node_map.name, idx), _qn(node_map.name, idx)
    )
    for event, nodes in node_map.events.items():
        folder = await root.add_folder(ua.NodeId(f"{node_map.name}.{event}", idx), _qn(event, idx))
        for n in nodes:
            vtype = MAP_VARIANTS[n.datatype]
            try:
                var = await folder.add_variable(
                    ua.NodeId(n.identifier, n.namespace),
                    _qn(n.name, n.namespace),
                    _initial(n.datatype),
                    vtype,
                )
            except Exception as e:
                raise ValueError(f"Cannot serve node '{n.name}' ({n.node_id}): {e}") from e
            if n.writable:
                await var.set_writable()
            else:
                points.append((var, vtype, lambda t, d=n.datatype: _random_value(d)))
    return Drifter(points)
