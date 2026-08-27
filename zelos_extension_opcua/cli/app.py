"""App mode runner for the Zelos OPC-UA extension.

Runs the extension from the Zelos App's saved configuration, including demo mode.
Startup problems here are reported as a single logger.error line plus exit 1;
tracebacks are for bugs, and this layer's failures are configuration.
"""

from __future__ import annotations

import logging
import sys
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

import zelos_sdk
from zelos_sdk.extensions import load_config

from zelos_extension_opcua import ACTION_PREFIX
from zelos_extension_opcua import actions as opcua_actions
from zelos_extension_opcua.client import OPCUAClient
from zelos_extension_opcua.node_map import NodeMap

if TYPE_CHECKING:
    from typing import Any

logger = logging.getLogger(__name__)

DEMO_HOST = "127.0.0.1"
DEMO_PORT = 4840


def get_demo_node_map_path() -> Path:
    """Path to the bundled demo node map."""
    with resources.as_file(
        resources.files("zelos_extension_opcua.demo").joinpath("plc_device.json")
    ) as path:
        return path


def start_demo_server() -> None:
    """Start the demo OPC-UA server in a background thread."""
    from zelos_extension_opcua.demo.simulator import start_demo_server_thread

    start_demo_server_thread(DEMO_HOST, DEMO_PORT)
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


def load_node_map(map_file: str | None) -> NodeMap | None:
    """Load the configured node map, or exit.

    A configured map that is missing or unparseable is fatal: continuing with an
    empty map records nothing while the extension reports itself healthy.
    """
    if not map_file:
        logger.warning("No node_map_file configured - no nodes will be polled")
        return None
    try:
        node_map = NodeMap.from_file(Path(map_file))
    except Exception as e:
        logger.error("Failed to load node map '%s': %s", map_file, e)
        sys.exit(1)
    logger.info("Loaded node map '%s' with %d nodes", node_map.name, len(node_map.nodes))
    return node_map


def serve(client: OPCUAClient) -> None:
    """Publish the action surface, then poll until shutdown.

    Actions are registered before `init()`: the registry is what init publishes,
    so anything registered afterwards may never be advertised.
    """
    opcua_actions.set_client(client)
    opcua_actions.register_actions(zelos_sdk.actions_registry)
    zelos_sdk.init(name=ACTION_PREFIX, actions=True)

    client.start()
    client.run()


def run_app_mode(demo: bool = False) -> None:
    """Run the extension with configuration from the Zelos App.

    Args:
        demo: If True, run the built-in PLC simulator and point at it
    """
    config = load_config()

    if demo or config.get("demo", False):
        logger.info("Demo mode: using built-in PLC simulator")
        start_demo_server()
        config["endpoint"] = f"opc.tcp://{DEMO_HOST}:{DEMO_PORT}/freeopcua/server/"
        config["node_map_file"] = str(get_demo_node_map_path())

    apply_log_level(config.get("log_level", "INFO"))

    client_kwargs: dict[str, Any] = {
        "endpoint": config.get("endpoint", "opc.tcp://localhost:4840"),
        "security_mode": config.get("security_mode", "None"),
        "security_policy": config.get("security_policy", "None"),
        "username": config.get("username", ""),
        "password": config.get("password", ""),
        "timeout": config.get("timeout", 5.0),
        "node_map": load_node_map(config.get("node_map_file")),
        "poll_interval": config.get("poll_interval", 1.0),
    }

    serve(OPCUAClient(**client_kwargs))
