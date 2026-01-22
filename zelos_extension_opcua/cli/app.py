"""App mode runner for Zelos OPC-UA extension.

This module handles running the extension when launched from the Zelos App
with configuration loaded from config.json, including demo mode support.
"""

from __future__ import annotations

import logging
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

import zelos_sdk
from zelos_sdk.extensions import load_config

from zelos_extension_opcua.client import OPCUAClient
from zelos_extension_opcua.node_map import NodeMap

if TYPE_CHECKING:
    from typing import Any

logger = logging.getLogger(__name__)

# Demo server settings
DEMO_HOST = "127.0.0.1"
DEMO_PORT = 4840


def get_demo_node_map_path() -> Path:
    """Get path to the bundled demo node map."""
    with resources.as_file(
        resources.files("zelos_extension_opcua.demo").joinpath("plc_device.json")
    ) as path:
        return path


def start_demo_server() -> None:
    """Start the demo OPC-UA server in a background thread."""
    from zelos_extension_opcua.demo.simulator import start_demo_server_thread

    start_demo_server_thread(DEMO_HOST, DEMO_PORT)
    logger.info(f"Demo server started on opc.tcp://{DEMO_HOST}:{DEMO_PORT}")


def run_app_mode(demo: bool = False) -> None:
    """Run the extension in app mode with configuration from Zelos App.

    Args:
        demo: If True, use built-in demo mode with simulated PLC
    """
    # Load configuration
    config = load_config()

    # Demo mode overrides config
    if demo or config.get("demo", False):
        logger.info("Demo mode: using built-in PLC simulator")
        start_demo_server()

        # Override connection settings for demo
        config["endpoint"] = f"opc.tcp://{DEMO_HOST}:{DEMO_PORT}/freeopcua/server/"

        # Use bundled demo node map
        demo_map_path = get_demo_node_map_path()
        config["node_map_file"] = str(demo_map_path)

    # Set log level
    log_level = config.get("log_level", "INFO")
    logging.getLogger().setLevel(getattr(logging, log_level))

    # Load node map if provided
    node_map = None
    map_file = config.get("node_map_file")
    if map_file:
        map_path = Path(map_file)
        if map_path.exists():
            try:
                node_map = NodeMap.from_file(map_path)
                logger.info(f"Loaded node map with {len(node_map.nodes)} nodes")
            except Exception as e:
                logger.error(f"Failed to load node map: {e}")
        else:
            logger.warning(f"Node map file not found: {map_file}")

    # Create client
    client_kwargs: dict[str, Any] = {
        "endpoint": config.get("endpoint", "opc.tcp://localhost:4840"),
        "security_mode": config.get("security_mode", "None"),
        "security_policy": config.get("security_policy", "None"),
        "username": config.get("username", ""),
        "password": config.get("password", ""),
        "timeout": config.get("timeout", 5.0),
        "node_map": node_map,
        "poll_interval": config.get("poll_interval", 1.0),
    }

    client = OPCUAClient(**client_kwargs)

    # Register actions with SDK
    zelos_sdk.actions_registry.register(client)

    # Start and run
    client.start()
    client.run()
