#!/usr/bin/env python3
"""Zelos OPC-UA Extension - CLI entry point.

Modes:

1. App mode (default): configuration comes from the Zelos App
2. Demo mode: built-in PLC simulator, no hardware
3. CLI trace mode: explicit endpoint and node map on the command line

Examples:
    uv run main.py                                          # app mode
    uv run main.py demo                                     # simulated PLC
    uv run main.py trace opc.tcp://192.168.1.100:4840 nodes.json
"""

from __future__ import annotations

import logging
import sys

import rich_click as click
from zelos_sdk.hooks.logging import TraceLoggingHandler

from zelos_extension_opcua import ACTION_PREFIX as _ACTION_PREFIX
from zelos_extension_opcua.cli import app as app_mode

#: Re-exported so a packaging-time action inventory - which reads this entry
#: module - sees the same namespace the live registration uses. See the
#: definition in `zelos_extension_opcua/__init__.py`.
ACTION_PREFIX = _ACTION_PREFIX

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# asyncua logs a record per request at INFO, which at a 1 Hz poll is a permanent
# stream into the trace through the handler below. Only DEBUG re-opens it - see
# cli/app.apply_log_level.
logging.getLogger("asyncua").setLevel(logging.WARNING)

# Capture logs at INFO and above into the trace. DEBUG is excluded so verbose
# library chatter cannot flood the backend.
_handler = TraceLoggingHandler("opcua_log")
_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(_handler)


@click.group(invoke_without_command=True)
@click.option("--demo", is_flag=True, help="Run in demo mode with simulated PLC")
@click.pass_context
def cli(ctx: click.Context, demo: bool) -> None:
    """Zelos OPC-UA Extension - read, write, and monitor OPC-UA nodes.

    Without a subcommand this starts in app mode using the configuration saved
    by the Zelos App. Use --demo or the 'demo' subcommand for a simulated PLC,
    and 'trace' for direct CLI access.
    """
    if ctx.invoked_subcommand is None:
        app_mode.run_app_mode(demo=demo)


@cli.command()
def demo() -> None:
    """Run against the built-in PLC simulator.

    Starts a local OPC-UA server with simulated temperature, pressure, motor,
    counter, digital I/O, analog and energy nodes, then traces it. No hardware
    required.
    """
    app_mode.run_app_mode(demo=True)


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
@click.option("--interval", "-i", type=float, default=1.0, help="Poll interval in seconds")
@click.option("--timeout", type=float, default=5.0, help="Request timeout in seconds")
def trace(
    endpoint: str,
    node_map_file: str | None,
    security_mode: str,
    security_policy: str,
    username: str,
    password: str,
    interval: float,
    timeout: float,
) -> None:
    """Trace OPC-UA nodes from the command line.

    ENDPOINT is the server URL (e.g. opc.tcp://192.168.1.100:4840).
    NODE_MAP_FILE is an optional JSON node map.

    \b
    Examples:
        uv run main.py trace opc.tcp://192.168.1.100:4840 nodes.json
        uv run main.py trace opc.tcp://server:4840 nodes.json -u admin --password secret
        uv run main.py trace opc.tcp://server:4840 -s SignAndEncrypt -p Basic256Sha256
    """
    from zelos_extension_opcua.client import OPCUAClient
    from zelos_extension_opcua.node_map import NodeMap

    node_map = None
    if node_map_file:
        try:
            node_map = NodeMap.from_file(node_map_file)
        except Exception as e:
            logger.error("Failed to load node map '%s': %s", node_map_file, e)
            sys.exit(1)
        logger.info("Loaded node map '%s' with %d nodes", node_map.name, len(node_map.nodes))

    logger.info("Starting OPC-UA trace: %s", endpoint)
    app_mode.serve(
        OPCUAClient(
            endpoint=endpoint,
            security_mode=security_mode,
            security_policy=security_policy,
            username=username,
            password=password,
            timeout=timeout,
            node_map=node_map,
            poll_interval=interval,
        )
    )


if __name__ == "__main__":
    cli()
