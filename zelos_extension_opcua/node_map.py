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
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


def parse_node_id(node_id: str) -> tuple[int, str, str]:
    """Parse an OPC-UA node ID string.

    Args:
        node_id: Node ID string in format ns=X;Y=Z

    Returns:
        Tuple of (namespace_index, identifier_type, identifier)

    Raises:
        ValueError: If node_id format is invalid
    """
    match = NODE_ID_PATTERN.match(node_id)
    if not match:
        msg = f"Invalid node ID format: '{node_id}'. Expected ns=X;[s|i|g|b]=Y"
        raise ValueError(msg)

    namespace = int(match.group(1))
    id_type = match.group(2)
    identifier = match.group(3)

    return namespace, id_type, identifier


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

        # Validate node ID format
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
    def identifier(self) -> str | int:
        """Get identifier value from node ID."""
        _, id_type, identifier = parse_node_id(self.node_id)
        if id_type == "i":
            return int(identifier)
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

        Args:
            data: Dictionary with event/node definitions

        Returns:
            NodeMap instance
        """
        events: dict[str, list[Node]] = {}

        for event_name, nodes_data in data.get("events", {}).items():
            nodes = []
            for node_data in nodes_data:
                node = Node(
                    node_id=node_data["node_id"],
                    name=node_data["name"],
                    datatype=node_data.get("datatype", "float32"),
                    unit=node_data.get("unit", ""),
                    scale=node_data.get("scale", 1.0),
                    description=node_data.get("description", ""),
                    writable=node_data.get("writable"),
                )
                nodes.append(node)
            events[event_name] = nodes

        return cls(
            events=events,
            name=data.get("name", "opcua"),
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
            name: Node name

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
