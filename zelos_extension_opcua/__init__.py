"""Zelos OPC-UA Extension - OPC-UA protocol support for Zelos.

This extension provides:
- OPC-UA client with automatic reconnection
- Node map for semantic grouping of OPC-UA nodes
- Zelos SDK integration for trace events and actions
- Demo server for testing without hardware
"""

from zelos_extension_opcua.client import OPCUAClient
from zelos_extension_opcua.node_map import Node, NodeMap

__all__ = [
    "OPCUAClient",
    "Node",
    "NodeMap",
]

__version__ = "0.1.0"
