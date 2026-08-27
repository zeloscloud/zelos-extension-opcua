"""Free-floating OPC-UA action functions registered under `<ACTION_PREFIX>/<name>`.

Free functions, not client methods, so the action surface has one shape whether
or not a client happens to be running, and so field decorators can reference
module-level callables (a bound `self` does not exist at decoration time).

The module holds a single client. Unlike CAN, whose config is an array of buses
and whose actions therefore take a `codec` selector, the OPC-UA config describes
exactly one server - a registry keyed by name would be a parameter nobody can
give a second value to. `cli/app.py` calls `set_client` at startup.

Failure convention: raise. The actions protocol derives its verdict from a
raised exception, so a returned {"success": False} reads as a successful run on
the wire and any caller chaining on exit status proceeds on bad data.
"""

from __future__ import annotations

import inspect
import logging
import sys
from typing import TYPE_CHECKING, Any

import zelos_sdk

from .client import coerce_text, describe_error

if TYPE_CHECKING:
    from zelos_sdk.actions import ActionsRegistry

    from .client import OPCUAClient
    from .node_map import Node

logger = logging.getLogger(__name__)

# Populated by cli/app.py (and any other entrypoint that brings up a client).
_client: OPCUAClient | None = None


def set_client(client: OPCUAClient | None) -> None:
    """Bind the client that every action in this module operates on."""
    global _client
    _client = client


def _get_client() -> OPCUAClient:
    if _client is None:
        raise RuntimeError("No OPC-UA client is configured on this extension")
    return _client


def _get_node(client: OPCUAClient, name: str) -> Node:
    if not client.node_map:
        raise ValueError("No node map loaded - set node_map_file in the extension config")
    node = client.node_map.get_by_name(name)
    if node is None:
        raise ValueError(f"Unknown node name '{name}'. Use list_nodes to see the available names.")
    return node


def _run(client: OPCUAClient, coro: Any, what: str, timeout: float | None = None) -> Any:
    """Dispatch an action coroutine, normalizing anything unexpected.

    Self-describing errors (bad input, a UA status code naming the node) are
    re-raised verbatim; everything else becomes a RuntimeError after a logged
    traceback, so the caller sees a sentence rather than an opaque type name.
    """
    try:
        return client._run_coro(coro, timeout)
    except (ValueError, RuntimeError, TimeoutError, OSError):
        raise
    except Exception as e:
        logger.exception("%s failed", what)
        raise RuntimeError(f"{what} failed: {describe_error(e)}") from e


def _write_timeout(client: OPCUAClient) -> float:
    """Dispatch deadline for a write.

    A write is read_data_value then write_value, and the named path may add an
    AccessLevel probe - each bounded by `client.timeout` on its own, so the
    per-request timeout is far too short a ceiling for the whole action.
    """
    return 3 * client.timeout + 1.0


# ─── Status and discovery ───────────────────────────────────────────────────


@zelos_sdk.action("Get Status", "Get connection and polling status")
def get_status() -> dict[str, Any]:
    return _get_client().status()


@zelos_sdk.action("List Nodes", "List all nodes in the map")
def list_nodes() -> dict[str, Any]:
    client = _get_client()
    nodes = [
        {
            "name": n.name,
            "node_id": n.node_id,
            "datatype": n.datatype,
            "unit": n.unit,
            "writable": n.writable,
        }
        for n in (client.node_map.nodes if client.node_map else [])
    ]
    return {"nodes": nodes, "count": len(nodes)}


@zelos_sdk.action("List Writable Nodes", "List all writable nodes")
def list_writable_nodes() -> dict[str, Any]:
    nodes = [
        {"name": n.name, "node_id": n.node_id, "datatype": n.datatype, "unit": n.unit}
        for n in _get_client().known_writable_nodes()
    ]
    return {"nodes": nodes, "count": len(nodes)}


@zelos_sdk.action("Browse Nodes", "Browse OPC-UA address space from a starting node")
@zelos_sdk.action.text(
    "start_node_id",
    title="Start Node ID",
    default="ns=0;i=85",
    description="Node ID to browse from (default: Objects folder)",
)
@zelos_sdk.action.number("max_depth", minimum=1, maximum=5, default=1, title="Max Depth")
def browse_nodes(start_node_id: str, max_depth: int) -> dict[str, Any]:
    client = _get_client()
    # A deep browse is many round trips; the per-request timeout is the wrong
    # ceiling for the whole walk.
    nodes = _run(
        client,
        client.browse_children(start_node_id, int(max_depth)),
        f"Browse from '{start_node_id}'",
        timeout=30.0,
    )
    return {"nodes": nodes, "count": len(nodes)}


# ─── Read / write by node ID ────────────────────────────────────────────────


@zelos_sdk.action("Read Node", "Read a single node by node ID")
@zelos_sdk.action.text("node_id", title="Node ID", description="e.g., ns=2;s=Temperature")
def read_node(node_id: str) -> dict[str, Any]:
    client = _get_client()
    value = _run(client, client.read_node(node_id), f"Read of '{node_id}'")
    return {"node_id": node_id, "value": value}


@zelos_sdk.action("Write Node", "Write a value to a node by node ID")
@zelos_sdk.action.text("node_id", title="Node ID", description="e.g., ns=2;s=Setpoint")
@zelos_sdk.action.text(
    "value", title="Value", description="Coerced to the node's type; true/false for bools"
)
def write_node(node_id: str, value: str) -> dict[str, Any]:
    # Text, not number: a number field cannot write a bool or a string node at
    # all. The client coerces using the variant type it reads back.
    client = _get_client()
    _run(
        client,
        client.write_node(node_id, value),
        f"Write to '{node_id}'",
        timeout=_write_timeout(client),
    )
    return {"node_id": node_id, "value": value}


# ─── Read / write by node map name ──────────────────────────────────────────


@zelos_sdk.action("Read Named Node", "Read a node by name from the map")
@zelos_sdk.action.text("name", title="Node Name")
def read_named_node(name: str) -> dict[str, Any]:
    client = _get_client()
    node = _get_node(client, name)
    value = _run(client, client.read_node_value(node), f"Read of '{name}'")
    return {
        "name": name,
        "node_id": node.node_id,
        "datatype": node.datatype,
        "unit": node.unit,
        "value": value,
    }


@zelos_sdk.action("Write Named Node", "Write a value to a node by name")
@zelos_sdk.action.text("name", title="Node Name")
@zelos_sdk.action.text(
    "value", title="Value", description="Coerced to the node's datatype; true/false for bools"
)
def write_named_node(name: str, value: str) -> dict[str, Any]:
    # Text, not number: a number field cannot write a bool or a string node at
    # all. The map's datatype decides how it is parsed.
    client = _get_client()
    node = _get_node(client, name)
    # Answered from the map, before dispatching: a declared read-only node needs
    # no connection to reject, and a connect failure here would report the wrong
    # reason. The client repeats the check for the auto-detect path.
    if node.writable is False:
        raise ValueError(f"Node '{name}' is not writable")
    typed = coerce_text(str(value), node.datatype)
    _run(
        client,
        client.write_node_value(node, typed),
        f"Write to '{name}'",
        timeout=_write_timeout(client),
    )
    return {
        "name": name,
        "node_id": node.node_id,
        "datatype": node.datatype,
        "unit": node.unit,
        "value": typed,
    }


# ─── Registration helper ────────────────────────────────────────────────────


def register_actions(registry: ActionsRegistry) -> list[str]:
    """Register every @action-decorated free function in this module by its bare
    function name. The leading `OPC-UA/` segment consumers see comes from
    `zelos_sdk.init(name=ACTION_PREFIX, actions=True)`, which concatenates the
    service name at serve time.

    Returns:
        The registered names, without the service prefix
    """
    module = sys.modules[__name__]
    registered: list[str] = []
    for name, obj in inspect.getmembers(module):
        if name.startswith("_"):
            continue
        if inspect.isfunction(obj) and hasattr(obj, "_action"):
            registry.register(obj, name=name)
            registered.append(name)
    logger.info("Registered %d OPC-UA actions", len(registered))
    return registered
