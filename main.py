#!/usr/bin/env python3
"""Zelos OPC-UA Extension - CLI entry point.

This module provides the command-line interface for the OPC-UA extension.
It can run in several modes:

1. App mode (default): Loads configuration from config.json when run from Zelos App
2. Demo mode: Uses built-in PLC simulator (no hardware required)
3. CLI trace mode: Direct command-line usage with explicit arguments

Examples:
    # Run from Zelos App (uses config.json)
    uv run main.py

    # Demo mode (simulated PLC)
    uv run main.py demo

    # CLI trace mode
    uv run main.py trace opc.tcp://192.168.1.100:4840 nodes.json
"""

from __future__ import annotations

import logging
import signal
import sys
from types import FrameType
from typing import TYPE_CHECKING

import rich_click as click
import zelos_sdk
from zelos_sdk.hooks.logging import TraceLoggingHandler

if TYPE_CHECKING:
    from zelos_extension_opcua.client import OPCUAClient

# Configure logging - INFO level prevents debug noise
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Global client reference for shutdown handler
_client: OPCUAClient | None = None


def shutdown_handler(signum: int, frame: FrameType | None) -> None:
    """Handle graceful shutdown on SIGTERM or SIGINT."""
    logger.info("Shutting down...")
    if _client:
        _client.stop()
    sys.exit(0)


def set_shutdown_client(client: OPCUAClient) -> None:
    """Set the client for shutdown handling."""
    global _client
    _client = client


@click.group(invoke_without_command=True)
@click.option("--demo", is_flag=True, help="Run in demo mode with simulated PLC")
@click.pass_context
def cli(ctx: click.Context, demo: bool) -> None:
    """Zelos OPC-UA Extension - Read, write, and monitor OPC-UA nodes.

    When run without a subcommand, starts in app mode using configuration
    from the Zelos App (config.json).

    Use --demo flag or 'demo' subcommand for simulated PLC.
    Use 'trace' subcommand for direct CLI access without Zelos App.
    """
    ctx.ensure_object(dict)
    ctx.obj["shutdown_handler"] = set_shutdown_client
    ctx.obj["demo"] = demo

    if ctx.invoked_subcommand is None:
        # App mode - run with Zelos App configuration
        run_app_mode(ctx, demo=demo)


def run_app_mode(ctx: click.Context, demo: bool = False) -> None:
    """Run in app mode with Zelos SDK initialization."""
    # Initialize SDK
    zelos_sdk.init(name="zelos_extension_opcua", actions=True)

    # Add trace logging handler
    handler = TraceLoggingHandler("zelos_extension_opcua_logger")
    logging.getLogger().addHandler(handler)

    # Register signal handlers
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    # Import and run app mode
    from zelos_extension_opcua.cli.app import run_app_mode as _run_app_mode

    _run_app_mode(demo=demo)


@cli.command()
@click.pass_context
def demo(ctx: click.Context) -> None:
    """Run demo mode with simulated PLC.

    Starts a local OPC-UA server with simulated PLC data
    and connects to it. No hardware required.

    The simulated PLC includes:
    - Temperature sensors and setpoint
    - Pressure sensors
    - Motor speed, current, and running state
    - Production and error counters
    - Digital inputs and outputs
    - Tank level and voltage
    - Energy accumulator
    """
    run_app_mode(ctx, demo=True)


@cli.command()
@click.argument("endpoint", type=str)
@click.argument("node_map_file", type=click.Path(exists=True), required=False)
@click.option(
    "--security-mode",
    "-s",
    type=click.Choice(["None", "Sign", "SignAndEncrypt"]),
    default="None",
    help="OPC-UA security mode",
)
@click.option(
    "--security-policy",
    "-p",
    type=click.Choice(["None", "Basic256Sha256", "Aes128Sha256RsaOaep", "Aes256Sha256RsaPss"]),
    default="None",
    help="OPC-UA security policy",
)
@click.option("--username", "-u", type=str, default="", help="Username for authentication")
@click.option("--password", type=str, default="", help="Password for authentication")
@click.option(
    "--interval", "-i", type=float, default=1.0, help="Poll interval in seconds"
)
@click.option("--timeout", type=float, default=5.0, help="Request timeout in seconds")
@click.pass_context
def trace(
    ctx: click.Context,
    endpoint: str,
    node_map_file: str | None,
    security_mode: str,
    security_policy: str,
    username: str,
    password: str,
    interval: float,
    timeout: float,
) -> None:
    """Trace OPC-UA nodes from command line.

    ENDPOINT is the OPC-UA server endpoint URL (e.g., opc.tcp://192.168.1.100:4840).

    NODE_MAP_FILE is an optional path to a JSON node map file.

    \b
    Examples:
        # Connect with node map
        uv run main.py trace opc.tcp://192.168.1.100:4840 nodes.json

        # Connect with authentication
        uv run main.py trace opc.tcp://192.168.1.100:4840 nodes.json -u admin --password secret

        # Connect with security
        uv run main.py trace opc.tcp://server:4840 -s SignAndEncrypt

        # Connect without node map (browse mode)
        uv run main.py trace opc.tcp://192.168.1.100:4840
    """
    from zelos_extension_opcua.client import OPCUAClient
    from zelos_extension_opcua.node_map import NodeMap

    # Initialize SDK for CLI mode
    zelos_sdk.init(name="zelos_extension_opcua", actions=True)

    # Add trace logging handler
    handler = TraceLoggingHandler("zelos_extension_opcua_logger")
    logging.getLogger().addHandler(handler)

    # Register signal handlers
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    # Load node map if provided
    node_map = None
    if node_map_file:
        try:
            node_map = NodeMap.from_file(node_map_file)
            logger.info(f"Loaded node map with {len(node_map.nodes)} nodes")
        except Exception as e:
            raise click.ClickException(f"Invalid node map: {e}") from e

    # Build client
    global _client
    _client = OPCUAClient(
        endpoint=endpoint,
        security_mode=security_mode,
        security_policy=security_policy,
        username=username,
        password=password,
        timeout=timeout,
        node_map=node_map,
        poll_interval=interval,
    )

    # Register actions
    zelos_sdk.actions_registry.register(_client)

    logger.info(f"Starting OPC-UA trace: {endpoint}")
    _client.start()
    _client.run()


if __name__ == "__main__":
    cli()
