"""Minimal JSON node map for human-readable OPC-UA node names.

The node map format uses user-defined events to group nodes semantically:

{
  "name": "my_device",
  "events": {
    "temperature": [
      {"name": "sensor1", "node_id": "ns=2;s=Temp.S1", "datatype": "float32"},
      {"name": "sensor2", "node_id": "ns=2;i=1001", "datatype": "float32"}
    ],
    "status": {
      "poll_interval": 0.1,
      "nodes": [
        {"name": "running", "node_id": "ns=2;s=Status.Running", "datatype": "bool"}
      ]
    }
  }
}

Event names become Zelos trace events. Node names become fields within those events.
An event is a node list, or an object with `nodes` and an optional `poll_interval`
(seconds) overriding the server's for that event.

Required fields per node: node_id, name
Optional fields: datatype (default: float32), unit, scale (default: 1.0),
  writable (default: None = auto-detect)

Map name, event names, and node names are sanitized at load (see `sanitize_name`).
Event names must be unique, and node names unique within their event - see
`NodeMap.from_dict`.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import zelos_sdk

logger = logging.getLogger(__name__)

# Supported data types and their sizes
DATATYPES = {
    "bool": 1,
    "uint8": 1,
    "int8": 1,
    "uint16": 2,
    "int16": 2,
    "uint32": 4,
    "int32": 4,
    "float32": 4,
    "uint64": 8,
    "int64": 8,
    "float64": 8,
    "string": 0,  # Variable length
}

# Floor for any poll_interval: Siemens' minimum sampling interval, and the schema's.
MIN_POLL_INTERVAL = 0.1

# ns=<index> or nsu=<percent-encoded uri>, then [s=<string>|i=<int>|g=<guid>|b=<opaque>].
# A literal ';' in the URI must be %3B-encoded (OPC 10000-6), so the URI ends at the first ';'.
NODE_ID_PATTERN = re.compile(r"^(?:ns=(\d+)|nsu=([^;]+));([sigb])=(.+)$")


def sanitize_name(name: str, kind: str = "field") -> str:
    """Make a name addressable in the Zelos trace catalog.

    Delegates to the SDK: the name grammar lives in zelos-trace-types and a
    hand-rolled character list drifts from it. OPC-UA identifiers routinely
    carry `. : ; =`, which are separators or syntax in catalog paths.
    """
    return zelos_sdk.sanitize_name(name, kind=kind)


def parse_node_id(node_id: str) -> tuple[int | str, str, str | int | uuid.UUID | bytes]:
    """Parse an OPC-UA node ID string into its converted identifier.

    The identifier is converted here, at map load, not at connect time. An
    unparseable int/GUID/base64 caught inside connect() surfaced as "Connection
    failed" and sent the operator after the network instead of the map entry.

    Args:
        node_id: Node ID string, ns=X;Y=Z or nsu=<uri>;Y=Z

    Returns:
        Tuple of (namespace, identifier_type, identifier). The namespace is the
        index for ns=, or the percent-decoded URI for nsu=, which the client
        resolves against the server's NamespaceArray. The identifier is already
        typed for asyncua's NodeId (int, uuid.UUID, bytes or str).

    Raises:
        ValueError: If the format or the identifier itself is invalid
    """
    match = NODE_ID_PATTERN.match(node_id)
    if not match:
        msg = f"Invalid node ID format: '{node_id}'. Expected ns=X|nsu=URI;[s|i|g|b]=Y"
        raise ValueError(msg)

    index, uri, id_type, raw = match.groups()
    namespace: int | str = int(index) if index is not None else unquote(uri)

    if id_type == "i":
        try:
            return namespace, id_type, int(raw)
        except ValueError as e:
            raise ValueError(f"Invalid numeric identifier in node ID '{node_id}': {e}") from e
    if id_type == "g":
        try:
            return namespace, id_type, uuid.UUID(raw)
        except ValueError as e:
            raise ValueError(f"Invalid GUID in node ID '{node_id}': {e}") from e
    if id_type == "b":
        try:
            return namespace, id_type, base64.b64decode(raw, validate=True)
        except Exception as e:
            raise ValueError(f"Invalid base64 opaque ID in node ID '{node_id}': {e}") from e
    return namespace, id_type, raw


def format_nsu_node_id(uri: str, identifier: str) -> str:
    """Build `nsu=<uri>;<identifier>`, the inverse of parse_node_id's nsu= branch.

    '%' is encoded too, so a URI that already holds a %XX sequence round-trips.
    """
    return f"nsu={uri.replace('%', '%25').replace(';', '%3B')};{identifier}"


@dataclass
class Node:
    """A single OPC-UA node definition."""

    node_id: str
    name: str
    datatype: str = "float32"
    unit: str = ""
    scale: float = 1.0
    description: str = ""
    writable: bool | None = None  # None = auto-detect

    def __post_init__(self) -> None:
        """Validate node definition."""
        if self.datatype not in DATATYPES:
            msg = f"Invalid datatype '{self.datatype}'. Must be one of {list(DATATYPES)}"
            raise ValueError(msg)

        # Format and identifier both, so a bad node ID fails the map load.
        parse_node_id(self.node_id)

    @property
    def namespace(self) -> int | str:
        """Namespace index (ns=) or URI (nsu=) from the node ID."""
        ns, _, _ = parse_node_id(self.node_id)
        return ns

    @property
    def identifier_type(self) -> str:
        """Get identifier type from node ID (s=string, i=numeric, g=guid, b=opaque)."""
        _, id_type, _ = parse_node_id(self.node_id)
        return id_type

    @property
    def identifier(self) -> str | int | uuid.UUID | bytes:
        """Get identifier value from node ID, typed for asyncua."""
        _, _, identifier = parse_node_id(self.node_id)
        return identifier


@dataclass
class NodeMap:
    """Collection of node definitions organized by user-defined events."""

    events: dict[str, list[Node]] = field(default_factory=dict)
    name: str = "opcua"
    description: str = ""
    # Event name -> poll_interval seconds, for events that override the server's.
    intervals: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_file(cls, path: str | Path) -> NodeMap:
        """Load node map from JSON file.

        Args:
            path: Path to JSON file

        Returns:
            NodeMap instance
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Node map file not found: {path}")

        with path.open() as f:
            data = json.load(f)

        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NodeMap:
        """Load node map from dictionary.

        Names are sanitized first, then checked for collisions. A collision is a
        hard error rather than a warning: silently clobbering one node with
        another produces a trace that is missing data while looking healthy.

        A node name may repeat across events, as a field does across trace
        events: N identical devices under one gateway would otherwise need N
        renamed copies of every tag. `get_by_name` takes `<event>/<name>` for those.

        Args:
            data: Dictionary with event/node definitions

        Returns:
            NodeMap instance

        Raises:
            ValueError: On a duplicate event name, a duplicate node name within
                one event, or a malformed event object
        """
        events: dict[str, list[Node]] = {}
        intervals: dict[str, float] = {}

        for raw_event_name, event_data in data.get("events", {}).items():
            event_name = sanitize_name(raw_event_name, kind="event")
            if event_name in events:
                msg = f"Duplicate event name '{event_name}' after sanitization"
                raise ValueError(msg)
            nodes_data = event_data
            if isinstance(event_data, dict):
                # A misspelled key would otherwise be dropped without a word.
                unknown = sorted(set(event_data) - {"nodes", "poll_interval"})
                if unknown:
                    raise ValueError(f"Event '{event_name}': unknown keys {', '.join(unknown)}")
                nodes_data = event_data.get("nodes", [])
                if "poll_interval" in event_data:
                    interval = event_data["poll_interval"]
                    if (
                        isinstance(interval, bool)
                        or not isinstance(interval, (int, float))
                        or interval < MIN_POLL_INTERVAL
                    ):
                        raise ValueError(
                            f"Event '{event_name}': poll_interval must be a number of "
                            f"seconds >= {MIN_POLL_INTERVAL}, got {interval!r}"
                        )
                    intervals[event_name] = float(interval)

            nodes = []
            seen: set[str] = set()
            for node_data in nodes_data:
                name = sanitize_name(node_data["name"])
                if name in seen:
                    msg = f"Duplicate node name '{name}' in event '{event_name}'"
                    raise ValueError(msg)
                seen.add(name)

                nodes.append(
                    Node(
                        node_id=node_data["node_id"],
                        name=name,
                        datatype=node_data.get("datatype", "float32"),
                        unit=node_data.get("unit", ""),
                        scale=node_data.get("scale", 1.0),
                        description=node_data.get("description", ""),
                        writable=node_data.get("writable"),
                    )
                )
            events[event_name] = nodes

        return cls(
            events=events,
            name=sanitize_name(data.get("name", "opcua"), kind="source"),
            description=data.get("description", ""),
            intervals=intervals,
        )

    def to_dict(self) -> dict[str, Any]:
        """The map in its JSON file shape; `from_dict` reads it back."""
        events: dict[str, Any] = {}
        for event, nodes in self.events.items():
            rows = [
                {
                    "name": n.name,
                    "node_id": n.node_id,
                    "datatype": n.datatype,
                    "unit": n.unit,
                    "description": n.description,
                    "writable": n.writable,
                }
                for n in nodes
            ]
            interval = self.intervals.get(event)
            events[event] = rows if interval is None else {"poll_interval": interval, "nodes": rows}
        return {"name": self.name, "description": self.description, "events": events}

    def to_csv(self) -> str:
        """One row per node: event,name,node_id,datatype,unit,description."""
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(("event", "name", "node_id", "datatype", "unit", "description"))
        for event, nodes in self.events.items():
            for n in nodes:
                writer.writerow((event, n.name, n.node_id, n.datatype, n.unit, n.description))
        return buf.getvalue()

    @property
    def nodes(self) -> list[Node]:
        """Flat list of all nodes across all events."""
        all_nodes = []
        for nodes in self.events.values():
            all_nodes.extend(nodes)
        return all_nodes

    @property
    def event_names(self) -> list[str]:
        """List of all event names."""
        return list(self.events.keys())

    def get_event(self, event_name: str) -> list[Node]:
        """Get all nodes for an event.

        Args:
            event_name: Name of the event

        Returns:
            List of nodes for this event
        """
        return self.events.get(event_name, [])

    def get_by_name(self, name: str) -> Node | None:
        """Find a node by name, or by `<event>/<name>`.

        Args:
            name: Sanitized node name, i.e. the name that appears in traces and
                in `list_nodes` output, optionally qualified by its event

        Returns:
            Node if found, None otherwise

        Raises:
            ValueError: If a bare name is in several events
        """
        event, _, bare = name.rpartition("/")
        matches = [
            (event_name, node)
            for event_name, nodes in self.events.items()
            if not event or event_name == event
            for node in nodes
            if node.name == bare
        ]
        if len(matches) > 1:
            choices = ", ".join(f"{e}/{n.name}" for e, n in matches)
            raise ValueError(f"Node name '{name}' is in several events; use one of: {choices}")
        return matches[0][1] if matches else None

    def get_by_node_id(self, node_id: str) -> Node | None:
        """Find node by OPC-UA node ID.

        Args:
            node_id: OPC-UA node ID

        Returns:
            Node if found, None otherwise
        """
        for nodes in self.events.values():
            for node in nodes:
                if node.node_id == node_id:
                    return node
        return None

    @property
    def writable_nodes(self) -> list[Node]:
        """Flat list of all writable nodes."""
        return [n for n in self.nodes if n.writable is True]

    @property
    def writable_names(self) -> list[str]:
        """List of all writable node names."""
        return [n.name for n in self.writable_nodes]
