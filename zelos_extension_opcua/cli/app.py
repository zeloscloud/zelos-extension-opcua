"""App mode runner for the Zelos OPC-UA extension.

Runs the extension from the Zelos App's saved configuration, including demo mode.
Startup problems here are reported as a single logger.error line plus exit 1;
tracebacks are for bugs, and this layer's failures are configuration.
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
    OPCUAClient,
    OPCUARunner,
    SharedSource,
    default_server_name,
)
from zelos_extension_opcua.node_map import NodeMap

logger = logging.getLogger(__name__)

DEMO_HOST = "127.0.0.1"
DEMO_PORT = 4840

DEFAULT_PREFIX = "OPC-UA"

#: Defaults of the schema's `advanced` object: settings shared by every server.
ADVANCED_DEFAULTS: dict[str, Any] = {
    "prefix": DEFAULT_PREFIX,
    "timeout": 5.0,
    "log_level": "INFO",
    "certificate_file": "",
    "private_key_file": "",
    "security_mode": "None",
    "security_policy": "None",
    "user_certificate_file": "",
    "user_private_key_file": "",
    "discovery": True,
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
}

TOP_LEVEL_KEYS = frozenset({"demo", "servers", "advanced"})

#: Shared client settings every OPCUAClient takes from `advanced`.
_SHARED = ("timeout", "certificate_file", "private_key_file", "discovery")


def resolve_advanced(config: dict[str, Any]) -> dict[str, Any]:
    """Merge the `advanced` object over its defaults.

    An absent `prefix` takes the default; a present-but-empty one clears it.
    """
    return {**ADVANCED_DEFAULTS, **(config.get("advanced") or {})}


def read_raw_config() -> dict[str, Any]:
    """config.json as saved, before schema defaults and validation.

    The shape check must see the file first: an old config fails the new
    schema with a message that does not say the format changed.
    """
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
    """A server's effective security: its own value, else the advanced default.

    The user certificate and key inherit as a pair: mixing the server's cert with
    the default's key would fail as a key mismatch rather than as a config error.
    """
    effective = {}
    for key in ("security_mode", "security_policy"):
        effective[key] = advanced[key] if server[key] in ("", "default") else server[key]
    user = ("user_certificate_file", "user_private_key_file")
    source = advanced if not any(server[k] for k in user) else server
    effective.update({k: source[k] for k in user})
    return effective


def resolve_servers(config: dict[str, Any], advanced: dict[str, Any]) -> list[dict[str, Any]]:
    """OPCUAClient arguments per configured server, or exit.

    Security is resolved and validated per server on its effective settings, so
    an error names the server whose settings are wrong.
    """
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
    # asyncua emits a record per request; follow the root level only into DEBUG,
    # where the user has explicitly asked for the firehose.
    logging.getLogger("asyncua").setLevel(
        logging.DEBUG if level <= logging.DEBUG else logging.WARNING
    )


def load_node_map(map_file: str | None, server: str = "", discovery: bool = True) -> NodeMap | None:
    """Load the configured node map, or exit.

    A configured map that is missing or unparseable is fatal: continuing with an
    empty map records nothing while the extension reports itself healthy.
    """
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


def serve(clients: list[OPCUAClient], prefix: str) -> None:
    """Publish the action surface, then poll every server until shutdown.

    Actions are registered before `init()`: the registry is what init publishes,
    so anything registered afterwards may never be advertised. The prefix source
    is created before `init()` too, which then reuses it as the global source
    instead of adding an empty second one of the same name.
    """
    runner = OPCUARunner(clients)
    opcua_actions.set_runner(runner)
    opcua_actions.register_actions(zelos_sdk.actions_registry)
    if prefix:
        logger.info("Trace prefix: %s", prefix)
        shared = SharedSource(zelos_sdk.init_global_source(prefix))
    else:
        logger.info("Trace prefix cleared: one trace source per server")
        shared = None
    zelos_sdk.init(name=ACTION_PREFIX, actions=True)

    for client in clients:
        client.start(shared)
    runner.run()


def run_config(config: dict[str, Any], demo: bool = False) -> None:
    """Resolve an app-shaped config (demo, servers[], advanced) and serve it."""
    reject_legacy_shape(config)
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
