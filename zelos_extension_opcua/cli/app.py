"""App mode: run from the Zelos App's saved config, or demo mode.

Startup problems are one logger.error line plus exit 1; tracebacks are for bugs.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from importlib import resources
from pathlib import Path
from typing import Any

import zelos_sdk
from zelos_sdk.extensions import ConfigValidationError, load_config

from zelos_extension_opcua import ACTION_PREFIX
from zelos_extension_opcua import actions as opcua_actions
from zelos_extension_opcua.client import (
    LOG_EVENT,
    LOG_SOURCE_NAME,
    OPCUAClient,
    OPCUARunner,
    SharedSource,
    default_server_name,
    install_log_handler,
)
from zelos_extension_opcua.node_map import NodeMap

logger = logging.getLogger(__name__)

DEMO_HOST = "127.0.0.1"
DEMO_PORT = 4840

#: Defaults of the schema's `advanced` object: settings shared by every server.
ADVANCED_DEFAULTS: dict[str, Any] = {
    "prefix": "OPC-UA",
    "timeout": 5.0,
    "log_level": "INFO",
    "certificate_file": "",
    "private_key_file": "",
    "security_mode": "None",
    "security_policy": "None",
    "user_certificate_file": "",
    "user_private_key_file": "",
    "discovery": True,
    "transport": "subscription",
    "min_update_interval": 60.0,
}

#: Defaults of one `servers[]` entry. "default" / "" inherit from `advanced`.
SERVER_DEFAULTS: dict[str, Any] = {
    "name": "",
    "endpoint": "opc.tcp://localhost:4840",
    "node_map_file": "",
    "poll_interval": 1.0,
    "security_mode": "default",
    "security_policy": "default",
    "user_certificate_file": "",
    "user_private_key_file": "",
    "server_certificate": "auto",
    "server_certificate_file": "",
    "transport": "default",
    "min_update_interval": None,
}

TOP_LEVEL_KEYS = frozenset({"demo", "servers", "advanced"})

#: Shared client settings every OPCUAClient takes from `advanced`.
_SHARED = ("timeout", "certificate_file", "private_key_file", "discovery")


def resolve_advanced(config: dict[str, Any]) -> dict[str, Any]:
    """`advanced` over its defaults; an absent `prefix` is the default, an empty one clears it."""
    return {**ADVANCED_DEFAULTS, **(config.get("advanced") or {})}


def read_raw_config() -> dict[str, Any]:
    """config.json as saved: an old config fails the schema without saying the format changed."""
    path = Path(os.environ.get("ZELOS_CONFIG_PATH") or "config.json")
    return json.loads(path.read_text()) if path.is_file() else {}


def reject_legacy_shape(config: dict[str, Any]) -> None:
    """Exit on a config from before `servers[]`, rather than misread it."""
    legacy = sorted(k for k in config if k not in TOP_LEVEL_KEYS)
    if legacy:
        logger.error(
            "Config format changed: servers are now listed under servers[] with shared "
            "settings under advanced (found top-level %s); reconfigure the extension",
            ", ".join(legacy),
        )
        sys.exit(1)


def trace_name(value: str) -> str:
    """A prefix or server name as the trace catalog addresses it."""
    value = value.strip()
    return zelos_sdk.sanitize_name(value, kind="source") if value else ""


def _inherit(server: dict[str, Any], advanced: dict[str, Any]) -> dict[str, Any]:
    """A server's effective settings: its own, else ("default" / "" / None) the advanced one.

    The user cert and key inherit as a pair: a mix would fail as a key mismatch.
    """
    effective = {
        k: advanced[k] if server[k] in ("", "default", None) else server[k]
        for k in ("security_mode", "security_policy", "transport", "min_update_interval")
    }
    user = ("user_certificate_file", "user_private_key_file")
    source = advanced if not any(server[k] for k in user) else server
    effective.update({k: source[k] for k in user})
    return effective


def resolve_servers(config: dict[str, Any], advanced: dict[str, Any]) -> list[dict[str, Any]]:
    """OPCUAClient arguments per configured server, or exit."""
    reject_legacy_shape(config)
    servers = config.get("servers") or []
    if not servers:
        logger.error("No servers configured: add one under servers, or enable demo")
        sys.exit(1)

    resolved: list[dict[str, Any]] = []
    seen: dict[str, str] = {}  # name -> endpoint
    for i, entry in enumerate(servers):
        server = {**SERVER_DEFAULTS, **entry}
        endpoint = str(server["endpoint"]).strip()
        if not endpoint:
            logger.error("servers[%d] has no endpoint", i)
            sys.exit(1)
        name = trace_name(str(server["name"])) or default_server_name(endpoint)
        if name in (LOG_EVENT, LOG_SOURCE_NAME):
            logger.error("Server %s: name '%s' is reserved for the extension's log", endpoint, name)
            sys.exit(1)
        if name in seen:
            logger.error(
                "Servers %s and %s both resolve to name '%s'; set a distinct name on one",
                seen[name],
                endpoint,
                name,
            )
            sys.exit(1)
        seen[name] = endpoint

        effective = _inherit(server, advanced)
        secure_default = advanced["security_mode"] not in ("", "None")
        resolved.append(
            {
                "name": name,
                "endpoint": endpoint,
                "node_map_file": server["node_map_file"],
                "poll_interval": server["poll_interval"],
                "server_certificate": server["server_certificate"],
                "server_certificate_file": server["server_certificate_file"],
                **effective,
                **{k: advanced[k] for k in _SHARED},
                # Explicit per-server None under a secure default: allowed, but loud.
                "downgrade_from": advanced["security_mode"]
                if secure_default and effective["security_mode"] == "None"
                else "",
            }
        )
    return resolved


def demo_server_kwargs(advanced: dict[str, Any]) -> dict[str, Any]:
    """The built-in simulator as the one server `demo`: security None, no inheritance."""
    return {
        **{k: advanced[k] for k in _SHARED},
        "transport": advanced["transport"],
        "min_update_interval": advanced["min_update_interval"],
        "name": "demo",
        "endpoint": f"opc.tcp://{DEMO_HOST}:{DEMO_PORT}/freeopcua/server/",
        "node_map_file": str(get_demo_node_map_path()),
    }


def build_clients(servers: list[dict[str, Any]]) -> list[OPCUAClient]:
    """One client per server, or exit on a server's inconsistent security or node map."""
    clients = []
    for kwargs in servers:
        kwargs = dict(kwargs)
        name = kwargs["name"]
        node_map = load_node_map(kwargs.pop("node_map_file"), name, kwargs["discovery"])
        try:
            clients.append(OPCUAClient(node_map=node_map, **kwargs))
        except ValueError as e:
            logger.error("Server '%s': invalid security configuration: %s", name, e)
            sys.exit(1)
    return clients


def get_demo_node_map_path() -> Path:
    """Path to the bundled demo node map."""
    with resources.as_file(
        resources.files("zelos_extension_opcua.demo").joinpath("plc_device.json")
    ) as path:
        return path


def start_demo_server() -> None:
    """Start the demo OPC-UA server in a background thread."""
    from zelos_extension_opcua.demo.simulator import start_demo_server_thread

    try:
        start_demo_server_thread(DEMO_HOST, DEMO_PORT)
    except RuntimeError as e:
        logger.error("%s", e)
        sys.exit(1)
    logger.info(f"Demo server started on opc.tcp://{DEMO_HOST}:{DEMO_PORT}")


def apply_log_level(level_name: str) -> None:
    """Apply the configured log level, falling back to INFO on a bad value."""
    level = getattr(logging, level_name.upper(), None)
    if not isinstance(level, int):
        logger.warning("Invalid log level '%s', using INFO", level_name)
        level = logging.INFO
    logging.getLogger().setLevel(level)
    # asyncua logs a record per request: only DEBUG opens it.
    logging.getLogger("asyncua").setLevel(
        logging.DEBUG if level <= logging.DEBUG else logging.WARNING
    )


def load_node_map(map_file: str | None, server: str = "", discovery: bool = True) -> NodeMap | None:
    """Load the configured node map, or exit: an empty map records nothing while looking healthy."""
    if not map_file:
        if discovery:
            logger.info("[%s] No node_map_file: tracing every variable discovered", server)
        else:
            logger.warning("[%s] No node_map_file and discovery off: polling nothing", server)
        return None
    try:
        node_map = NodeMap.from_file(Path(map_file))
    except Exception as e:
        logger.error("[%s] Failed to load node map '%s': %s", server, map_file, e)
        sys.exit(1)
    logger.info(
        "[%s] Loaded node map '%s' with %d nodes", server, node_map.name, len(node_map.nodes)
    )
    return node_map


def open_sources(prefix: str) -> SharedSource | None:
    """The prefix's shared source with the log on its `log` event, following rotation;
    cleared, None and the log on its own source. Earlier records reach stderr only."""
    if not prefix:
        install_log_handler(LOG_SOURCE_NAME)
        logger.info("Trace prefix cleared: one trace source per server")
        return None
    source = zelos_sdk.TraceSource(prefix)
    shared = SharedSource(source, install_log_handler(source))
    logger.info("Trace prefix: %s", prefix)
    return shared


def serve(clients: list[OPCUAClient], prefix: str) -> None:
    """Publish the action surface, then poll every server until shutdown.

    Actions register before `init()`: registered later they may never be
    advertised. The prefix source is a plain TraceSource, not init's global one,
    which the SDK holds for the process: rotation must be able to drop it so its
    segment ends. init's global of the same name stays empty.
    """
    runner = OPCUARunner(clients)
    opcua_actions.set_runner(runner)
    opcua_actions.register_actions(zelos_sdk.actions_registry)
    shared = open_sources(prefix)
    zelos_sdk.init(name=ACTION_PREFIX, actions=True)

    for client in clients:
        client.start(shared)
    runner.run()


def run_config(config: dict[str, Any], demo: bool = False) -> None:
    """Resolve an app-shaped config (demo, servers[], advanced) and serve it."""
    advanced = resolve_advanced(config)
    apply_log_level(advanced["log_level"])
    prefix = trace_name(str(advanced["prefix"]))

    if demo or config.get("demo", False):
        logger.info("Demo mode: using built-in PLC simulator")
        start_demo_server()
        servers = [demo_server_kwargs(advanced)]
    else:
        servers = resolve_servers(config, advanced)

    serve(build_clients(servers), prefix)


def run_app_mode(demo: bool = False) -> None:
    """Run the extension with configuration from the Zelos App.

    Args:
        demo: If True, run the built-in PLC simulator as the only server
    """
    reject_legacy_shape(read_raw_config())
    try:
        config = load_config()
    except ConfigValidationError as e:
        logger.error("Invalid configuration: %s", "; ".join(m.strip(" •") for m in e.errors))
        sys.exit(1)
    run_config(config, demo)
