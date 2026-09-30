"""OPC-UA actions, free functions registered under `<ACTION_PREFIX>/<name>`.

Free functions, not client methods: one surface whether or not a client runs.
`server` may be omitted with one server. Failures raise: the protocol reads its
verdict from an exception, so {"success": False} would report a successful run.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import sys
import time
from typing import TYPE_CHECKING, Any

import zelos_sdk

from .client import coerce_text, describe_error

if TYPE_CHECKING:
    from zelos_sdk.actions import ActionsRegistry

    from .client import OPCUAClient, OPCUARunner
    from .node_map import Node

logger = logging.getLogger(__name__)

_runner: OPCUARunner | None = None


def set_runner(runner: OPCUARunner | None) -> None:
    """Bind the runner whose clients every action in this module operates on."""
    global _runner
    _runner = runner


def _get_runner() -> OPCUARunner:
    if _runner is None:
        raise RuntimeError("No OPC-UA client is configured on this extension")
    return _runner


def _get_client(server: str = "") -> OPCUAClient:
    """The named server's client; the only one when `server` is empty."""
    clients = _get_runner().clients
    names = ", ".join(clients)
    if not server:
        if len(clients) == 1:
            return next(iter(clients.values()))
        raise ValueError(f"Several servers are configured; set server to one of: {names}")
    # Sanitized like the configured names, so `192.168.1.10` finds `192_168_1_10`.
    client = clients.get(zelos_sdk.sanitize_name(server.strip(), kind="source"))
    if client is None:
        raise ValueError(f"Unknown server '{server}'. Servers: {names}")
    return client


def _get_node(client: OPCUAClient, name: str) -> Node:
    if not client.node_map:
        raise ValueError(f"No node map loaded on server '{client.name}' - set its node_map_file")
    node = client.node_map.get_by_name(name)
    if node is None:
        raise ValueError(f"Unknown node name '{name}'. Use list_nodes to see the available names.")
    return node


def _run(
    client: OPCUAClient,
    coro: Any,
    what: str,
    timeout: float | None = None,
    sent: list[float] | None = None,
) -> Any:
    """Dispatch an action coroutine; anything not self-describing becomes a RuntimeError.

    A timeout after a write went out (`sent`) cannot be recalled: the message
    says its outcome is unknown.
    """
    try:
        return _get_runner()._run_coro(coro, timeout or client.timeout)
    except Exception as e:
        if sent and _timed_out(e):
            raise TimeoutError(
                f"{what} was sent; no response within {time.monotonic() - sent[0]:.1f}s; "
                "the server may have applied it - read the node to confirm"
            ) from e
        if isinstance(e, ValueError | RuntimeError | TimeoutError | OSError):
            raise
        logger.exception("%s failed", what)
        raise RuntimeError(f"{what} failed: {describe_error(e)}") from e


def _timed_out(error: BaseException) -> bool:
    """A timeout, or asyncua's bare Exception raised from one."""
    return isinstance(error, TimeoutError) or isinstance(error.__cause__, TimeoutError)


def _write_timeout(client: OPCUAClient) -> float:
    """Read, write and maybe an AccessLevel probe, each bounded by `client.timeout`."""
    return 3 * client.timeout + 1.0


def _server_field(func: Any) -> Any:
    return zelos_sdk.action.text(
        "server",
        title="Server",
        required=False,
        default="",
        description="Server name; may be omitted when only one is configured",
    )(func)


# ─── Status and discovery ───────────────────────────────────────────────────


@zelos_sdk.action("Get Status", "Get connection and polling status; every server when omitted")
@_server_field
def get_status(server: str = "") -> dict[str, Any]:
    if server:
        return _get_client(server).status()
    statuses = [c.status() for c in _get_runner().clients.values()]
    return {"servers": statuses, "count": len(statuses)}


def _clients_for(server: str) -> list[OPCUAClient]:
    """The named server, or every server when omitted."""
    return [_get_client(server)] if server else list(_get_runner().clients.values())


@zelos_sdk.action("List Nodes", "List the nodes in the map; every server's when omitted")
@_server_field
def list_nodes(server: str = "") -> dict[str, Any]:
    nodes = [
        {
            "server": c.name,
            "event": event,
            "name": n.name,
            "node_id": n.node_id,
            "datatype": n.datatype,
            "unit": n.unit,
            "writable": n.writable,
        }
        for c in _clients_for(server)
        for event, nodes in (c.node_map.events.items() if c.node_map else [])
        for n in nodes
    ]
    return {"nodes": nodes, "count": len(nodes)}


@zelos_sdk.action("List Writable Nodes", "List writable nodes; every server's when omitted")
@_server_field
def list_writable_nodes(server: str = "") -> dict[str, Any]:
    nodes = [
        {
            "server": c.name,
            "name": n.name,
            "node_id": n.node_id,
            "datatype": n.datatype,
            "unit": n.unit,
        }
        for c in _clients_for(server)
        for n in c.known_writable_nodes()
    ]
    return {"nodes": nodes, "count": len(nodes)}


@zelos_sdk.action(
    "Discovered Map",
    "The nodes discovered on the last connect, as a node map (json) or csv text; "
    "save it as a node_map_file to pin or edit them",
)
@_server_field
@zelos_sdk.action.select("format", title="Format", choices=["json", "csv"], default="json")
def discovered_map(format: str = "json", server: str = "") -> dict[str, Any]:
    client = _get_client(server)
    if not client.discovery:
        raise ValueError(f"Server '{client.name}' polls a node map or has discovery off")
    if not _get_runner().is_running():
        raise RuntimeError("extension is not running")
    node_map = client.node_map
    if node_map is None:
        raise RuntimeError(f"Server '{client.name}' has not connected yet; nothing discovered")
    count = len(node_map.nodes)
    if format == "csv":
        return {"server": client.name, "format": "csv", "count": count, "csv": node_map.to_csv()}
    return {"server": client.name, "format": "json", "count": count, "map": node_map.to_dict()}


@zelos_sdk.action("Browse Nodes", "Browse OPC-UA address space from a starting node")
@_server_field
@zelos_sdk.action.text(
    "start_node_id",
    title="Start Node ID",
    default="ns=0;i=85",
    description="Node ID to browse from (default: Objects folder)",
)
@zelos_sdk.action.number("max_depth", minimum=1, maximum=5, default=1, title="Max Depth")
def browse_nodes(start_node_id: str, max_depth: int, server: str = "") -> dict[str, Any]:
    client = _get_client(server)
    # Many round trips: the per-request timeout is the wrong ceiling.
    nodes = _run(
        client,
        client.browse_children(start_node_id, int(max_depth)),
        f"Browse from '{start_node_id}'",
        timeout=30.0,
    )
    return {"nodes": nodes, "count": len(nodes)}


# ─── Read / write by node ID ────────────────────────────────────────────────


@zelos_sdk.action("Read Node", "Read a single node by node ID")
@_server_field
@zelos_sdk.action.text("node_id", title="Node ID", description="e.g., ns=2;s=Temperature")
def read_node(node_id: str, server: str = "") -> dict[str, Any]:
    client = _get_client(server)
    value = _run(client, client.read_node(node_id), f"Read of '{node_id}'")
    return {"node_id": node_id, "value": value}


@zelos_sdk.action("Write Node", "Write a value to a node by node ID")
@_server_field
@zelos_sdk.action.text("node_id", title="Node ID", description="e.g., ns=2;s=Setpoint")
@zelos_sdk.action.text(
    "value", title="Value", description="Coerced to the node's type; true/false for bools"
)
def write_node(node_id: str, value: str, server: str = "") -> dict[str, Any]:
    # Text: a number field cannot write a bool or string node. Coerced to the variant type.
    client = _get_client(server)
    sent: list[float] = []
    _run(
        client,
        client.write_node(node_id, value, sent),
        f"Write to '{node_id}'",
        timeout=_write_timeout(client),
        sent=sent,
    )
    return {"node_id": node_id, "value": value}


# ─── Read / write by node map name ──────────────────────────────────────────


@zelos_sdk.action("Read Named Node", "Read a node by name from the map")
@_server_field
@zelos_sdk.action.text("name", title="Node Name")
def read_named_node(name: str, server: str = "") -> dict[str, Any]:
    client = _get_client(server)
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
@_server_field
@zelos_sdk.action.text("name", title="Node Name")
@zelos_sdk.action.text(
    "value", title="Value", description="Coerced to the node's datatype; true/false for bools"
)
def write_named_node(name: str, value: str, server: str = "") -> dict[str, Any]:
    client = _get_client(server)
    node = _get_node(client, name)
    if client.discovery:
        raise ValueError(
            f"Node '{name}' was discovered, and discovered nodes are read-only by name; "
            f"use write_node with node_id {node.node_id}"
        )
    # Before dispatch: a connect failure would report the wrong reason.
    if node.writable is False:
        raise ValueError(f"Node '{name}' is not writable")
    typed = coerce_text(str(value), node.datatype)
    sent: list[float] = []
    _run(
        client,
        client.write_node_value(node, typed, sent),
        f"Write to '{name}'",
        timeout=_write_timeout(client),
        sent=sent,
    )
    return {
        "name": name,
        "node_id": node.node_id,
        "datatype": node.datatype,
        "unit": node.unit,
        "value": typed,
    }


# ─── Config-form hook (standalone: runs with the extension stopped) ─────────


@zelos_sdk.action(
    "Auto-configure",
    "Check each server in the form at its endpoint (unsaved edits included; older apps: "
    "the saved config) and fill in security left at default. With none, find servers on "
    "this machine (well-known ports, the local discovery server) and announced over "
    "mDNS. Review, then save and start.",
    # Read-only and session-less, and the form wants it before a first start.
    standalone=True,
)
@zelos_sdk.action.object(
    "config",
    properties={},
    title="Config",
    description="The config form's current (possibly unsaved) data. Empty: the saved config",
    required=False,
)
def auto_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """`config` keys replace the form's: only `servers`, so Advanced survives.

    Its own asyncio.run: called from the actions thread, never the polling loop.
    """
    from .autoconfig import check_servers, probe

    if config is None:
        config = _saved_config()
    servers = [s for s in config.get("servers") or [] if isinstance(s, dict)]
    advanced = config.get("advanced") or {}
    return asyncio.run(check_servers(servers, advanced) if servers else probe(advanced))


def _saved_config() -> dict[str, Any]:
    """The saved config, or {} when there is none."""
    try:
        from zelos_sdk.extensions import load_config

        return load_config() or {}
    except Exception:  # no config yet, or it does not validate
        return {}


# ─── Registration helper ────────────────────────────────────────────────────


def register_actions(registry: ActionsRegistry) -> list[str]:
    """Register every @action function here by bare name; `init(name=ACTION_PREFIX)`
    adds the `OPC-UA/` prefix. Returns the registered names."""
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
