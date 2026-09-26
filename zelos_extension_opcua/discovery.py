"""Live discovery: browse a server's address space and map every scalar Variable.

Read-only: Browse, BrowseNext and Read only. Nothing is written to disk; the
result is a `NodeMap` built fresh on every connect, so a program change on the
server shows up after the next reconnect instead of going stale.
"""

from __future__ import annotations

import re
import struct
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from asyncua import Client, ua
from asyncua.common.utils import NotEnoughData
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

# Concrete builtin DataType -> node map datatype, kept exactly.
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

# Abstract numeric DataType -> the widest exact type of its family: any subtype
# may arrive, and values coerce into it.
ABSTRACT_DATATYPES = {
    _IDS.Number: "float64",
    _IDS.Integer: "int64",
    _IDS.UInteger: "uint64",
}

# Variant type -> node map datatype: the coercion for text written to an
# unmapped node (write_node), and the value kind of an untyped node.
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


# Raised client-side while parsing a complete response: the channel is intact.
_DECODE_ERRORS = (struct.error, ValueError, NotEnoughData)
_NO_CONTINUATION_POINTS = ua.StatusCodes.BadNoContinuationPoints
_BAD_DECODING = ua.StatusCodes.BadDecodingError


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


async def operation_limits(client: Client) -> tuple[int, int, int]:
    """(nodes per Browse, nodes per Read, items per CreateMonitoredItems): the
    server's OperationLimits capped at MAX_OPERATIONS; a missing or 0 (no limit)
    value is MAX_OPERATIONS."""
    ops = "Server_ServerCapabilities_OperationLimits_"
    dvs = await read_many(
        client,
        [
            (NodeId(getattr(_IDS, ops + name)), None)
            for name in ("MaxNodesPerBrowse", "MaxNodesPerRead", "MaxMonitoredItemsPerCall")
        ],
        MAX_OPERATIONS,
    )
    limits = []
    for dv in dvs:
        value = dv.Value.Value if dv.Value and _good(dv) else None
        limits.append(min(int(value), MAX_OPERATIONS) if value else MAX_OPERATIONS)
    return limits[0], limits[1], limits[2]


def _good(dv: ua.DataValue) -> bool:
    return dv.StatusCode is None or dv.StatusCode.is_good()


async def read_many(
    client: Client, items: Sequence[tuple[NodeId, int | None]], chunk: int
) -> list[ua.DataValue]:
    """Read (node, attribute) pairs, `chunk` per request; attribute None = Value.

    asyncua fails a whole response on one value it cannot decode (a 2-D Variant
    array, some nested Variants); that chunk is re-read item by item and the
    undecodable item comes back as BadDecodingError.
    """
    out: list[ua.DataValue] = []
    for start in range(0, len(items), chunk):
        batch = items[start : start + chunk]
        try:
            out.extend(await _read(client, batch))
        except _DECODE_ERRORS:
            for item in batch:
                try:
                    out.extend(await _read(client, [item]))
                except _DECODE_ERRORS:
                    out.append(ua.DataValue(StatusCode=ua.StatusCode(_BAD_DECODING)))
    return out


async def _read(client: Client, items: Sequence[tuple[NodeId, int | None]]) -> list[ua.DataValue]:
    # Default is Source only; the refresh sweep logs at ServerTimestamp.
    params = ua.ReadParameters(TimestampsToReturn=ua.TimestampsToReturn.Both)
    for node_id, attribute in items:
        rv = ua.ReadValueId()
        rv.NodeId = node_id
        rv.AttributeId = ua.AttributeIds.Value if attribute is None else attribute
        params.NodesToRead.append(rv)
    return await client.uaclient.read(params)


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
    browse_failed: list[str] = field(default_factory=list)  # `path (status)`
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


async def _browse_batch(
    rpc: _Counter, node_ids: list[NodeId], retry: bool = True
) -> tuple[list[list[Any]], dict[int, str]]:
    """Forward hierarchical references of each node, following BrowseNext to the end.

    One request needs a continuation point per large node, and a small server
    holds few (5-10): past its cap a node gets BadNoContinuationPoints and no
    references. Those are browsed again one per request once the batch's own
    points are released, so at most one is held.

    Returns:
        (references per node, index -> status name of each node that failed)
    """
    params = ua.BrowseParameters()
    params.View = ua.ViewDescription()
    # asyncua defaults the view Timestamp to now; the .NET stack reads that as a
    # historical view and answers BadNodeNotInView. Null means the current view.
    params.View.Timestamp = ua.get_win_epoch()
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
    refs: list[list[Any]] = [[] for _ in node_ids]
    failed: dict[int, str] = {}
    no_point: list[int] = []
    pending: dict[int, bytes] = {}
    for i, r in enumerate(results):
        if r.StatusCode.value == _NO_CONTINUATION_POINTS and retry:
            no_point.append(i)
        elif not r.StatusCode.is_good():
            failed[i] = r.StatusCode.name
        else:
            refs[i].extend(r.References or [])
            if r.ContinuationPoint:
                pending[i] = r.ContinuationPoint
    while pending:
        next_params = ua.BrowseNextParameters()
        next_params.ReleaseContinuationPoints = False
        next_params.ContinuationPoints = list(pending.values())
        page = await rpc.browse_next(next_params)
        cont = {}
        for i, r in zip(pending, page, strict=True):
            if not r.StatusCode.is_good():
                failed[i] = r.StatusCode.name  # the pages so far are kept
                continue
            refs[i].extend(r.References or [])
            if r.ContinuationPoint:
                cont[i] = r.ContinuationPoint
        pending = cont
    for i in no_point:
        ([refs[i]], again) = await _browse_batch(rpc, [node_ids[i]], retry=False)
        if again:
            failed[i] = again[0]
    return refs, failed


async def walk(rpc: _Counter, chunk: int) -> tuple[list[Found], list[str]]:
    """Breadth-first from Objects over forward hierarchical references.

    Not followed: the Server object (i=2253, the server's own diagnostics), any
    Object whose BrowseName starts with `_` (Kepware's _System, _Statistics,
    _CommunicationSerialization, _Hints branches; `_` Variables are kept), and
    HasProperty targets, which are metadata - EngineeringUnits / EURange of a
    Variable are recorded for describe. Variables are browsed too: struct
    members hang off their parent Variable. A node reached twice is visited
    once, which also breaks reference cycles.

    Returns:
        (Variables found, `path (status)` of each node whose browse failed)
    """
    objects = NodeId(_IDS.ObjectsFolder)
    visited = {objects, NodeId(_IDS.Server)}
    frontier: list[tuple[NodeId, tuple[str, ...], Found | None]] = [(objects, (), None)]
    found: list[Found] = []
    failed: list[str] = []
    while frontier:
        next_frontier: list[tuple[NodeId, tuple[str, ...], Found | None]] = []
        for start in range(0, len(frontier), chunk):
            batch = frontier[start : start + chunk]
            pages, bad = await _browse_batch(rpc, [node_id for node_id, _, _ in batch])
            failed += [
                f"{'/'.join(batch[i][1]) or 'Objects'} ({status})" for i, status in bad.items()
            ]
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
    return found, failed


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

    Value is read only where the DataType does not fix the type or the rank may
    be an array: on a gateway a Value read can cost a device read.
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
    typed: list[tuple[Found, Any, bool, list[ua.DataValue]]] = []
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
        # The value is needed where the declaration does not fix the type, or
        # the rank leaves scalar vs array to it.
        typed.append((var, dtype, declared_datatype(dtype) is None or rank in _MAYBE_SCALAR, attrs))

    unresolved = [var.node_id for var, _, needs_value, _ in typed if needs_value]
    values = iter(await rpc.read([(nid, None) for nid in unresolved], chunk))

    out = []
    for var, dtype, needs_value, attrs in typed:
        datatype, reason = field_datatype(dtype, next(values) if needs_value else None)
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


def declared_datatype(dtype: Any) -> str | None:
    """Datatype a DataType declaration fixes: concrete builtin or abstract numeric."""
    if not isinstance(dtype, NodeId) or dtype.NamespaceIndex != 0:
        return None
    return BUILTIN_DATATYPES.get(dtype.Identifier) or ABSTRACT_DATATYPES.get(dtype.Identifier)


def field_datatype(dtype: Any, dv: ua.DataValue | None) -> tuple[str | None, str]:
    """A Variable's field datatype, or (None, skip reason).

    The declaration wins where it fixes one. Otherwise (BaseDataType, other
    abstract or vendor types) the value's kind at its widest: bool, float64 for
    any number, string. `dv` is the current value, None when not read.
    """
    declared = declared_datatype(dtype)
    if dv is None:
        return declared, ""
    if dv.StatusCode is not None and dv.StatusCode.value == _BAD_DECODING:
        return None, "undecodable"
    variant = dv.Value if _good(dv) else None
    if variant is None or variant.Value is None:
        return None, "no value"
    if isinstance(variant.Value, list):
        return None, "array"
    if declared:
        return declared, ""
    if variant.VariantType == ua.VariantType.ExtensionObject:
        return None, "struct"
    kind = VARIANT_DATATYPES.get(variant.VariantType)
    if kind is None:
        return None, "unsupported type"
    return (kind if kind in ("bool", "string") else "float64"), ""


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
    found, browse_failed = await walk(rpc, browse_chunk)
    described, skipped = await describe(rpc, found, read_chunk)
    events, renames = assign_names(described, namespaces, max_event_bytes)
    return Discovery(
        node_map=NodeMap(events=events, name=name, description="discovered"),
        skipped=skipped,
        renames=renames,
        browse_failed=browse_failed,
        requests=rpc.requests,
        seconds=time.monotonic() - started,
    )
