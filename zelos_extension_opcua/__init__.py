"""Zelos OPC-UA Extension - OPC-UA protocol support for Zelos.

This extension provides:
- OPC-UA client with batch polling and automatic reconnection
- Node map for semantic grouping of OPC-UA nodes
- Zelos SDK integration for trace events and actions
- Demo server for testing without hardware
"""

from zelos_extension_opcua.client import OPCUAClient
from zelos_extension_opcua.node_map import Node, NodeMap

#: Matches `name` in `extension.toml`, so the action address is the name the user sees.
ACTION_PREFIX = "OPC-UA"

__all__ = [
    "ACTION_PREFIX",
    "Node",
    "NodeMap",
    "OPCUAClient",
]
