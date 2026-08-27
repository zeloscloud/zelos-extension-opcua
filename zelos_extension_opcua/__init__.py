"""Zelos OPC-UA Extension - OPC-UA protocol support for Zelos.

This extension provides:
- OPC-UA client with batch polling and automatic reconnection
- Node map for semantic grouping of OPC-UA nodes
- Zelos SDK integration for trace events and actions
- Demo server for testing without hardware
"""

from zelos_extension_opcua.client import OPCUAClient
from zelos_extension_opcua.node_map import Node, NodeMap

#: Action namespace for this extension. Single source for both surfaces - the
#: live registration (`zelos_sdk.init(name=ACTION_PREFIX)`) and the at-rest
#: inventory the packaging step dumps from `main.py`, which re-exports this.
#: A mismatch would silently produce two unrelated action trees.
#:
#: Matches `name` in `extension.toml`, which is what a user sees in the
#: extension list, so the address they read there is the address they type.
ACTION_PREFIX = "OPC-UA"

__all__ = [
    "ACTION_PREFIX",
    "Node",
    "NodeMap",
    "OPCUAClient",
]

__version__ = "0.1.0"
