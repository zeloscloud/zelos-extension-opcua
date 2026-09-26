"""Live discovery: browse a server's address space and map every scalar Variable.

Read-only: Browse, BrowseNext and Read only. Nothing is written to disk; the
result is a `NodeMap` built fresh on every connect, so a program change on the
server shows up after the next reconnect instead of going stale.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from asyncua import Client, ua
from asyncua.ua.uatypes import NodeId

from zelos_extension_opcua.node_map import Node, NodeMap, format_nsu_node_id, sanitize_name

# Ceiling on nodes per Browse/Read request, below any advertised OperationLimits:
# it bounds each response's size and latency against the request timeout.
MAX_OPERATIONS = 100

#: The per-server health event. Sanitized map and discovered names never start
#: with `_`, so neither can take it.
HEALTH_EVENT = "_server"

# Longest trace event name, including a `<server>/` prefix (zelos-trace grammar).
TRACE_NAME_BYTES = 128

_IDS = ua.ObjectIds

# Builtin DataType -> node map datatype. Any other DataType (subtypes, enums,
# vendor types) is resolved from the variant type of its current value.
BUILTIN_DATATYPES = {
    _IDS.Boolean: "bool",
    _IDS.SByte: "int8",
    _IDS.Byte: "uint8",
    _IDS.Int16: "int16",
    _IDS.UInt16: "uint16",
    _IDS.Int32: "int32",
    _IDS.UInt32: "uint32",
    _IDS.Int64: "int64",
    _IDS.UInt64: "uint64",
    _IDS.Float: "float32",
    _IDS.Double: "float64",
    _IDS.String: "string",
}

# Variant type -> node map datatype: a value's type when its DataType is not
# builtin, and the coercion for text written to an unmapped node (write_node).
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

_DESCRIBE_ATTRIBUTES = (
    ua.AttributeIds.DataType,
    ua.AttributeIds.ValueRank,
    ua.AttributeIds.AccessLevel,
    ua.AttributeIds.DisplayName,
    ua.AttributeIds.Description,
)
_CURRENT_READ = 0x01  # AccessLevel bit 0
_SCALAR = ua.ValueRank.Scalar
# Any (-2) and ScalarOrOneDimension (-3) may still hold a scalar; open62541
# defaults a variable to Any, so these are decided by the current value.
_MAYBE_SCALAR = (ua.ValueRank.Any, ua.ValueRank.ScalarOrOneDimension)

# String NodeId shapes whose segments name a variable better than its browse
# path (Siemens and CODESYS paths carry PLC/DataBlocksGlobal/DeviceSet noise).
# Each yields the segments; the last must equal the BrowseName or the browse
# path is used instead.
VENDOR_IDS: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], list[str]]], ...] = (
    # Siemens S7-1500: "DB"."tag"
    (re.compile(r'^"(.+)"$'), lambda m: m[1].split('"."')),
    # CODESYS: |var|<device>.Application.PLC_PRG.x
    (re.compile(r"^\|var\|[^.]+\.(.+)$"), lambda m: m[1].split(".")),
    # B&R: ::Task:Var, ::AsGlobalPV:Var
    (re.compile(r"^::([^:]+):(.+)$"), lambda m: [m[1], *m[2].split(".")]),
    # Kepware Channel.Device.Tag, TwinCAT MAIN.var
    (re.compile(r'^[^"|:]+\.[^"|:]+$'), lambda m: m[0].split(".")),
)


def to_nsu_string(node_id: NodeId, namespaces: Sequence[str]) -> str | None:
    """The URI-qualified form of a NodeId, or None if its index is not in `namespaces`."""
    if node_id.NamespaceIndex >= len(namespaces):
        return None
    # to_string omits ns=0, leaving just the identifier part.
    bare = NodeId(node_id.Identifier, 0, node_id.NodeIdType).to_string()
    return format_nsu_node_id(namespaces[node_id.NamespaceIndex], bare)


def node_id_string(node_id: NodeId, namespaces: Sequence[str]) -> str:
    """nsu= for a server namespace (stable across restarts), ns=0 otherwise."""
    if node_id.NamespaceIndex:
        nsu = to_nsu_string(node_id, namespaces)
        if nsu is not None:
            return nsu
    bare = NodeId(node_id.Identifier, 0, node_id.NodeIdType).to_string()
    return f"ns={node_id.NamespaceIndex};{bare}"


async def operation_limits(client: Client) -> tuple[int, int]:
    """(nodes per Browse, nodes per Read): the server's OperationLimits capped at
    MAX_OPERATIONS; a missing or 0 (no limit) value is MAX_OPERATIONS."""
    dvs = await read_many(
        client,
        [
            (NodeId(_IDS.Server_ServerCapabilities_OperationLimits_MaxNodesPerBrowse), None),
            (NodeId(_IDS.Server_ServerCapabilities_OperationLimits_MaxNodesPerRead), None),
        ],
        MAX_OPERATIONS,
    )
    limits = []
    for dv in dvs:
        value = dv.Value.Value if dv.Value and _good(dv) else None
        limits.append(min(int(value), MAX_OPERATIONS) if value else MAX_OPERATIONS)
    return limits[0], limits[1]


def _good(dv: ua.DataValue) -> bool:
    return dv.StatusCode_ is None or dv.StatusCode_.is_good()


async def read_many(
    client: Client, items: Sequence[tuple[NodeId, int | None]], chunk: int
) -> list[ua.DataValue]:
    """Read (node, attribute) pairs, `chunk` per request; attribute None = Value."""
    out: list[ua.DataValue] = []
    for start in range(0, len(items), chunk):
        params = ua.ReadParameters()
        for node_id, attribute in items[start : start + chunk]:
            rv = ua.ReadValueId()
            rv.NodeId = node_id
            rv.AttributeId = ua.AttributeIds.Value if attribute is None else attribute
            params.NodesToRead.append(rv)
        out.extend(await client.uaclient.read(params))
    return out


@dataclass
class Found:
    """A Variable the walk reached."""

    node_id: NodeId
    name: str  # BrowseName.Name
    path: tuple[str, ...]  # BrowseNames from below Objects to the parent
    eu: NodeId | None = None
    eu_range: NodeId | None = None


@dataclass
class Discovery:
    """One connect's discovery result."""

    node_map: NodeMap
    skipped: Counter[str] = field(default_factory=Counter)
    renames: list[str] = field(default_factory=list)
    requests: int = 0
    seconds: float = 0.0


class _Counter:
    """Wraps the request calls to count round trips for the summary line."""

    def __init__(self, client: Client) -> None:
        self.client = client
        self.requests = 0

    async def browse(self, params: Any) -> list[ua.BrowseResult]:
        self.requests += 1
        return await self.client.uaclient.browse(params)

    async def browse_next(self, params: Any) -> list[ua.BrowseResult]:
        self.requests += 1
        return await self.client.uaclient.browse_next(params)

    async def read(self, items: Sequence[tuple[NodeId, int | None]], chunk: int) -> list:
        self.requests += -(-len(items) // chunk)
        return await read_many(self.client, items, chunk)


async def _browse_batch(rpc: _Counter, node_ids: list[NodeId]) -> list[list[Any]]:
    """Forward hierarchical references of each node, following BrowseNext to the end."""
    params = ua.BrowseParameters()
    params.View = ua.ViewDescription()
    params.RequestedMaxReferencesPerNode = 0
    for node_id in node_ids:
        desc = ua.BrowseDescription()
        desc.NodeId = node_id
        desc.BrowseDirection = ua.BrowseDirection.Forward
        desc.ReferenceTypeId = NodeId(_IDS.HierarchicalReferences)
        desc.IncludeSubtypes = True
        desc.NodeClassMask = ua.NodeClass.Object | ua.NodeClass.Variable
        desc.ResultMask = (
            ua.BrowseResultMask.ReferenceTypeId
            | ua.BrowseResultMask.NodeClass
            | ua.BrowseResultMask.BrowseName
        )
        params.NodesToBrowse.append(desc)
    results = await rpc.browse(params)
    refs = [list(r.References or []) for r in results]
    pending = {i: r.ContinuationPoint for i, r in enumerate(results) if r.ContinuationPoint}
    while pending:
        next_params = ua.BrowseNextParameters()
        next_params.ReleaseContinuationPoints = False
        next_params.ContinuationPoints = list(pending.values())
        page = await rpc.browse_next(next_params)
        cont = {}
        for i, r in zip(pending, page, strict=True):
            refs[i].extend(r.References or [])
            if r.ContinuationPoint:
                cont[i] = r.ContinuationPoint
        pending = cont
    return refs


async def walk(rpc: _Counter, chunk: int) -> list[Found]:
    """Breadth-first from Objects over forward hierarchical references.

    Not followed: the Server object (i=2253, the server's own diagnostics), any
    Object whose BrowseName starts with `_` (Kepware's _System, _Statistics,
    _CommunicationSerialization, _Hints branches; `_` Variables are kept), and
    HasProperty targets, which are metadata - EngineeringUnits / EURange of a
    Variable are recorded for describe. Variables are browsed too: struct
    members hang off their parent Variable. A node reached twice is visited
    once, which also breaks reference cycles.
    """
    objects = NodeId(_IDS.ObjectsFolder)
    visited = {objects, NodeId(_IDS.Server)}
    frontier: list[tuple[NodeId, tuple[str, ...], Found | None]] = [(objects, (), None)]
    found: list[Found] = []
    while frontier:
        next_frontier: list[tuple[NodeId, tuple[str, ...], Found | None]] = []
        for start in range(0, len(frontier), chunk):
            batch = frontier[start : start + chunk]
            pages = await _browse_batch(rpc, [node_id for node_id, _, _ in batch])
            for (_, path, parent), refs in zip(batch, pages, strict=True):
                for ref in refs:
                    name = ref.BrowseName.Name
                    if ref.ReferenceTypeId == NodeId(_IDS.HasProperty):
                        if parent is not None and name == "EngineeringUnits":
                            parent.eu = _local(ref.NodeId)
                        elif parent is not None and name == "EURange":
                            parent.eu_range = _local(ref.NodeId)
                        continue
                    if getattr(ref.NodeId, "ServerIndex", 0):  # on another server
                        continue
                    node_id = _local(ref.NodeId)
                    if node_id in visited:
                        continue
                    if ref.NodeClass == ua.NodeClass.Object:
                        if name.startswith("_"):
                            continue
                        visited.add(node_id)
                        next_frontier.append((node_id, (*path, name), None))
                    elif ref.NodeClass == ua.NodeClass.Variable:
                        visited.add(node_id)
                        var = Found(node_id, name, path)
                        found.append(var)
                        next_frontier.append((node_id, (*path, name), var))
        frontier = next_frontier
    return found


def _local(node_id: Any) -> NodeId:
    """A plain NodeId: ExpandedNodeId does not compare equal to one."""
    return NodeId(node_id.Identifier, node_id.NamespaceIndex, node_id.NodeIdType)


def _text(dv: ua.DataValue) -> str:
    value = dv.Value.Value if dv.Value and _good(dv) else None
    return (value.Text or "") if isinstance(value, ua.LocalizedText) else ""


async def describe(
    rpc: _Counter, found: list[Found], chunk: int
) -> tuple[list[tuple[Found, str, str, str]], Counter[str]]:
    """(variable, datatype, unit, description) per traceable Variable, and skip counts.

    Value is read only where the DataType is not builtin or the rank may be an
    array: on a gateway a Value read can cost a device read.
    """
    n = len(_DESCRIBE_ATTRIBUTES)
    items: list[tuple[NodeId, int | None]] = [
        (var.node_id, attr) for var in found for attr in _DESCRIBE_ATTRIBUTES
    ]
    props = [
        (var, key, ref)
        for var in found
        for key, ref in (("eu", var.eu), ("range", var.eu_range))
        if ref
    ]
    items.extend((ref, None) for _, _, ref in props)
    dvs = await rpc.read(items, chunk)
    prop_values: dict[tuple[int, str], Any] = {
        (id(var), key): dv.Value.Value
        for (var, key, _), dv in zip(props, dvs[len(found) * n :], strict=True)
        if dv.Value and _good(dv)
    }

    skipped: Counter[str] = Counter()
    typed: list[tuple[Found, str | None, list[ua.DataValue]]] = []
    for i, var in enumerate(found):
        attrs = dvs[i * n : (i + 1) * n]
        dtype, rank, access = (
            dv.Value.Value if dv.Value and _good(dv) else None for dv in attrs[:3]
        )
        if access is not None and not access & _CURRENT_READ:
            skipped["not readable"] += 1
            continue
        if rank is not None and rank not in (_SCALAR, *_MAYBE_SCALAR):
            skipped["array"] += 1
            continue
        datatype = None
        if isinstance(dtype, NodeId) and dtype.NamespaceIndex == 0 and rank not in _MAYBE_SCALAR:
            datatype = BUILTIN_DATATYPES.get(dtype.Identifier)
        typed.append((var, datatype, attrs))

    unresolved = [var.node_id for var, datatype, _ in typed if datatype is None]
    values = iter(await rpc.read([(nid, None) for nid in unresolved], chunk))

    out = []
    for var, datatype, attrs in typed:
        if datatype is None:
            datatype, reason = _from_value(next(values))
            if datatype is None:
                skipped[reason] += 1
                continue
        eu = prop_values.get((id(var), "eu"))
        unit = (eu.DisplayName.Text or "") if isinstance(eu, ua.EUInformation) else ""
        display = _text(attrs[3])
        description = _text(attrs[4]) or (display if display != var.name else "")
        rng = prop_values.get((id(var), "range"))
        if isinstance(rng, ua.Range):
            description = f"{description} (range {rng.Low:g}..{rng.High:g})".lstrip()
        out.append((var, datatype, unit, description))
    return out, skipped


def _from_value(dv: ua.DataValue) -> tuple[str | None, str]:
    """Datatype from the current value's variant, or (None, skip reason)."""
    variant = dv.Value if _good(dv) else None
    if variant is None or variant.Value is None:
        return None, "no value"
    if isinstance(variant.Value, list):
        return None, "array"
    if variant.VariantType == ua.VariantType.ExtensionObject:
        return None, "struct"
    datatype = VARIANT_DATATYPES.get(variant.VariantType)
    return (datatype, "") if datatype else (None, "unsupported type")


def vendor_segments(node_id: NodeId, browse_name: str) -> list[str] | None:
    """Segments a vendor string id encodes, when they agree with the BrowseName."""
    if node_id.NodeIdType != ua.NodeIdType.String:
        return None
    for pattern, split in VENDOR_IDS:
        match = pattern.match(node_id.Identifier)
        if match:
            segments = split(match)
            if len(segments) >= 2 and all(segments) and segments[-1] == browse_name:
                return segments
            return None
    return None


def _fit(parts: list[str], max_bytes: int) -> str:
    """Segments joined by `/`, dropping leading ones past `max_bytes`: the tail
    is the specific part, the head the shared one."""
    event = ""
    for part in reversed(parts):
        joined = f"{part}/{event}" if event else part
        if len(joined.encode()) > max_bytes:
            break
        event = joined
    return event or parts[-1].encode()[:max_bytes].decode(errors="ignore")


def namespace_alias(uri: str) -> str:
    """Last segment of a namespace URI with a letter in it, sanitized; "" if none.

    `http://opcfoundation.org/UA/DI/` -> `DI`, `urn:zelos:sim:gateway` -> `gateway`.
    """
    rest = re.sub(r"^[A-Za-z][\w+.-]*:", "", uri)  # scheme
    segments = [s for s in re.split(r"[/:#?]", rest) if re.search(r"[A-Za-z]", s)]
    return sanitize_name(segments[-1], kind="field") if segments else ""


def assign_names(
    described: list[tuple[Found, str, str, str]],
    namespaces: Sequence[str],
    max_event_bytes: int = TRACE_NAME_BYTES,
) -> tuple[dict[str, list[Node]], list[str]]:
    """Events and fields for the described Variables, and the renames made.

    Event = the recognized vendor id's leading segments, else the parent path
    below Objects; field = BrowseName. Both sanitized, segments joined by `/`.
    An event over `max_event_bytes` keeps its trailing segments.

    Collisions are resolved in node id order, so the result does not depend on
    browse order: the first keeps the name; a later one gets `_<alias>` of its
    namespace URI when the namespace differs from the first's, else (or when the
    alias is empty or also taken) `_2`, `_3`, ... The URI, not the index: indexes
    can shift across server restarts and would rename a signal between runs.
    """
    rows = sorted(
        ((node_id_string(var.node_id, namespaces), var, datatype, unit, desc)
         for var, datatype, unit, desc in described),
        key=lambda row: row[0],
    )  # fmt: skip
    events: dict[str, list[Node]] = {}
    owners: dict[tuple[str, str], int] = {}  # (event, field) -> namespace index
    renames: list[str] = []
    for node_id, var, datatype, unit, desc in rows:
        ns = var.node_id.NamespaceIndex
        segments = vendor_segments(var.node_id, var.name)
        parts = segments[:-1] if segments else list(var.path) or ["Objects"]
        event = _fit([sanitize_name(p, kind="field") for p in parts], max_event_bytes)
        name = sanitize_name(var.name, kind="field")
        if (event, name) in owners:
            alias = namespace_alias(namespaces[ns]) if ns < len(namespaces) else ""
            candidate = f"{name}_{alias}" if alias and owners[(event, name)] != ns else ""
            k = 2
            while not candidate or (event, candidate) in owners:
                candidate, k = f"{name}_{k}", k + 1
            renames.append(f"{event}/{name} -> {candidate} ({node_id})")
            name = candidate
        owners[(event, name)] = ns
        events.setdefault(event, []).append(
            Node(
                node_id=node_id,
                name=name,
                datatype=datatype,
                unit=unit,
                description=desc,
                writable=False,
            )
        )
    return dict(sorted(events.items())), renames


async def discover(
    client: Client,
    namespaces: Sequence[str],
    browse_chunk: int,
    read_chunk: int,
    name: str,
    max_event_bytes: int = TRACE_NAME_BYTES,
) -> Discovery:
    """Walk, describe and name the server's Variables.

    Args:
        client: A connected session
        namespaces: The server's NamespaceArray, for nsu= ids
        browse_chunk: Nodes per Browse request
        read_chunk: Nodes per Read request
        name: The discovered map's name
        max_event_bytes: Longest event name the trace takes after its prefix
    """
    started = time.monotonic()
    rpc = _Counter(client)
    found = await walk(rpc, browse_chunk)
    described, skipped = await describe(rpc, found, read_chunk)
    events, renames = assign_names(described, namespaces, max_event_bytes)
    return Discovery(
        node_map=NodeMap(events=events, name=name, description="discovered"),
        skipped=skipped,
        renames=renames,
        requests=rpc.requests,
        seconds=time.monotonic() - started,
    )
