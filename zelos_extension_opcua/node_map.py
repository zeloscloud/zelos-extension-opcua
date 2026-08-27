"""Minimal JSON node map for human-readable OPC-UA node names.

The node map format uses user-defined events to group nodes semantically:

{
  "name": "my_device",
  "events": {
    "temperature": [
      {"name": "sensor1", "node_id": "ns=2;s=Temp.S1", "datatype": "float32"},
      {"name": "sensor2", "node_id": "ns=2;i=1001", "datatype": "float32"}
    ],
    "status": [
      {"name": "running", "node_id": "ns=2;s=Status.Running", "datatype": "bool"}
    ]
  }
}

Event names become Zelos trace events. Node names become fields within those events.

Required fields per node: node_id, name
Optional fields: datatype (default: float32), unit, scale (default: 1.0),
  writable (default: None = auto-detect)

Map name, event names, and node names are sanitized at load (see `sanitize_name`)
and must be unique after sanitization - see `NodeMap.from_dict`.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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

# OPC-UA node ID pattern: ns=<namespace>;[s=<string>|i=<int>|g=<guid>|b=<opaque>]
NODE_ID_PATTERN = re.compile(r"^ns=(\d+);([sigb])=(.+)$")


def sanitize_name(name: str, kind: str = "field") -> str:
    """Make a name addressable in the Zelos trace catalog.

    Delegates to the SDK: the name grammar lives in zelos-trace-types and a
    hand-rolled character list drifts from it. OPC-UA identifiers routinely
    carry `. : ; =`, which are separators or syntax in catalog paths.
    """
    return zelos_sdk.sanitize_name(name, kind=kind)


def parse_node_id(node_id: str) -> tuple[int, str, str | int | uuid.UUID | bytes]:
    """Parse an OPC-UA node ID string into its converted identifier.

    The identifier is converted here, at map load, not at connect time. An
    unparseable int/GUID/base64 caught inside connect() surfaced as "Connection
    failed" and sent the operator after the network instead of the map entry.

    Args:
        node_id: Node ID string in format ns=X;Y=Z

    Returns:
        Tuple of (namespace_index, identifier_type, identifier), the identifier
        already typed for asyncua's NodeId (int, uuid.UUID, bytes or str)

    Raises:
        ValueError: If the format or the identifier itself is invalid
    """
    match = NODE_ID_PATTERN.match(node_id)
    if not match:
        msg = f"Invalid node ID format: '{node_id}'. Expected ns=X;[s|i|g|b]=Y"
        raise ValueError(msg)

    namespace = int(match.group(1))
    id_type = match.group(2)
    raw = match.group(3)

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
    def namespace(self) -> int:
        """Get namespace index from node ID."""
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

        Args:
            data: Dictionary with event/node definitions

        Returns:
            NodeMap instance

        Raises:
            ValueError: On a duplicate event name, or a duplicate node name
                anywhere in the map (get_by_name must stay unambiguous)
        """
        events: dict[str, list[Node]] = {}
        seen_nodes: dict[str, str] = {}  # sanitized node name -> owning event

        for raw_event_name, nodes_data in data.get("events", {}).items():
            event_name = sanitize_name(raw_event_name, kind="event")
            if event_name in events:
                msg = f"Duplicate event name '{event_name}' after sanitization"
                raise ValueError(msg)

            nodes = []
            for node_data in nodes_data:
                name = sanitize_name(node_data["name"])
                prior = seen_nodes.get(name)
                if prior is not None:
                    msg = (
                        f"Duplicate node name '{name}' in event '{event_name}' "
                        f"(already defined in event '{prior}'). Node names must be "
                        "unique across the whole map."
                    )
                    raise ValueError(msg)
                seen_nodes[name] = event_name

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
        )

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
        """Find node by name across all events.

        Args:
            name: Sanitized node name, i.e. the name that appears in traces and
                in `list_nodes` output

        Returns:
            Node if found, None otherwise
        """
        for nodes in self.events.values():
            for node in nodes:
                if node.name == name:
                    return node
        return None

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
