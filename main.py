#!/usr/bin/env python3
"""Zelos OPC-UA Extension - CLI entry point.

Modes:

1. App mode (default): configuration comes from the Zelos App
2. Demo mode: built-in PLC simulator, no hardware
3. CLI trace mode: explicit endpoint and node map on the command line

Examples:
    uv run main.py                                          # app mode
    uv run main.py demo                                     # simulated PLC
    uv run main.py demo-server --profile s7                 # standalone simulator
    uv run main.py trace opc.tcp://192.168.1.100:4840 nodes.json
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import rich_click as click
from zelos_sdk.hooks.logging import TraceLoggingHandler

from zelos_extension_opcua import ACTION_PREFIX as _ACTION_PREFIX
from zelos_extension_opcua.cli import app as app_mode

#: Re-exported so a packaging-time action inventory - which reads this entry
#: module - sees the same namespace the live registration uses. See the
#: definition in `zelos_extension_opcua/__init__.py`.
ACTION_PREFIX = _ACTION_PREFIX

# UTC ISO 8601 with ms, matching the SDK's Rust tracing lines in the same log stream
logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03dZ %(levelname)5s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
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


@cli.command("demo-server")
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind address")
@click.option("--port", type=int, default=4840, show_default=True, help="TCP port")
@click.option(
    "--profile",
    type=click.Choice(["demo", "gateway", "s7", "device"]),
    default="demo",
    show_default=True,
    help="Address space and server behavior to simulate",
)
@click.option("--secure", is_flag=True, help="Add Basic256Sha256 Sign/SignAndEncrypt endpoints")
@click.option("--secure-only", is_flag=True, help="With --secure: offer no None endpoint")
@click.option(
    "--trust-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="With --secure: accept only client certs found in this directory",
)
@click.option(
    "--user-cert-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="With --secure: offer Certificate user tokens; accept only user certs in this directory",
)
@click.option(
    "--shuffle-namespaces",
    is_flag=True,
    help="Shift namespace indices per start (ZELOS_SIM_NS_SHIFT=<n> pins the shift)",
)
@click.option(
    "--map",
    "map_path",
    type=click.Path(exists=True, dir_okay=False),
    help="Serve this node map instead of a profile",
)
@click.option(
    "--nodes",
    type=click.IntRange(min=0),
    default=0,
    help="Add N Float variables under Bulk (100 per folder) for measurement",
)
@click.option("--log-requests", is_flag=True, help="Log request counts by service at shutdown")
@click.pass_context
def demo_server(
    ctx: click.Context,
    host: str,
    port: int,
    profile: str,
    secure: bool,
    secure_only: bool,
    trust_dir: Path | None,
    user_cert_dir: Path | None,
    shuffle_namespaces: bool,
    map_path: str | None,
    nodes: int,
    log_requests: bool,
) -> None:
    """Run a standalone OPC-UA simulator until Ctrl-C.

    \b
    Profiles:
        demo     the DemoPLC used by demo mode
        gateway  Kepware-shaped Channel.Device.Tag with _System/_Statistics noise
        s7       S7-1500-shaped "DB"."tag", small OperationLimits, 4-session cap
        device   DI DeviceSet, EU/EURange, array, struct, Bad node, cycle, depth

    \b
    Examples:
        uv run main.py demo-server --profile gateway
        uv run main.py demo-server --secure --trust-dir ./trusted
        uv run main.py demo-server --secure --user-cert-dir ./users
        uv run main.py demo-server --map my_nodes.json
        uv run main.py demo-server --nodes 50000 --log-requests
    """
    import asyncio

    from zelos_extension_opcua.demo.sim_server import run_sim
    from zelos_extension_opcua.node_map import NodeMap

    if (trust_dir or user_cert_dir or secure_only) and not secure:
        raise click.UsageError("--trust-dir, --user-cert-dir and --secure-only require --secure")
    node_map = None
    if map_path:
        explicit = ctx.get_parameter_source("profile") != click.core.ParameterSource.DEFAULT
        if explicit or shuffle_namespaces:
            # Map node ids carry fixed ns indices: a profile or a shift would contradict them.
            raise click.UsageError("--map excludes --profile and --shuffle-namespaces")
        try:
            node_map = NodeMap.from_file(map_path)
        except Exception as e:
            raise click.ClickException(f"Failed to load node map '{map_path}': {e}") from e

    try:
        asyncio.run(
            run_sim(
                log_requests=log_requests,
                profile=profile,
                host=host,
                port=port,
                secure=secure,
                trust_dir=trust_dir,
                user_cert_dir=user_cert_dir,
                shuffle_namespaces=shuffle_namespaces,
                node_map=node_map,
                nodes=nodes,
                secure_only=secure_only,
            )
        )
    except OSError as e:
        raise click.ClickException(
            f"Could not start simulator on {host}:{port} ({e}); one may already be running there"
        ) from e


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
@click.option("--interval", "-i", type=float, default=1.0, help="Poll interval in seconds")
@click.option(
    "--transport",
    type=click.Choice(["subscription", "poll"]),
    default="subscription",
    show_default=True,
    help="Server-pushed changes, polling what is refused; or poll everything",
)
@click.option("--timeout", type=float, default=5.0, help="Request timeout in seconds")
@click.option(
    "--certificate-file",
    type=str,
    default="",
    help="Client certificate (DER/PEM); default generates one under ~/.zelos/opcua/pki",
)
@click.option("--private-key-file", type=str, default="", help="Client private key (DER/PEM)")
@click.option(
    "--server-certificate",
    type=click.Choice(["auto", "strict"]),
    default="auto",
    help="auto accepts the server's certificate; strict requires --server-certificate-file",
)
@click.option(
    "--server-certificate-file", type=str, default="", help="Pinned server certificate (DER/PEM)"
)
@click.option(
    "--user-certificate-file",
    type=str,
    default="",
    help="X.509 user identity certificate (DER/PEM); default Anonymous",
)
@click.option("--user-private-key-file", type=str, default="", help="User private key (DER/PEM)")
def trace(
    endpoint: str,
    node_map_file: str | None,
    security_mode: str,
    security_policy: str,
    interval: float,
    transport: str,
    timeout: float,
    certificate_file: str,
    private_key_file: str,
    server_certificate: str,
    server_certificate_file: str,
    user_certificate_file: str,
    user_private_key_file: str,
) -> None:
    """Trace OPC-UA nodes from the command line.

    ENDPOINT is the server URL (e.g. opc.tcp://192.168.1.100:4840).
    NODE_MAP_FILE is an optional JSON node map.

    \b
    Examples:
        uv run main.py trace opc.tcp://192.168.1.100:4840 nodes.json
        uv run main.py trace opc.tcp://server:4840 -s SignAndEncrypt -p Basic256Sha256
        uv run main.py trace opc.tcp://server:4840 -s SignAndEncrypt -p Basic256Sha256 \\
            --user-certificate-file user.der --user-private-key-file user.pem
    """
    logger.info("Starting OPC-UA trace: %s", endpoint)
    # One server through the same resolution as app mode.
    app_mode.run_config(
        {
            "servers": [
                {
                    "endpoint": endpoint,
                    "node_map_file": node_map_file or "",
                    "poll_interval": interval,
                    "security_mode": security_mode,
                    "security_policy": security_policy,
                    "user_certificate_file": user_certificate_file,
                    "user_private_key_file": user_private_key_file,
                    "server_certificate": server_certificate,
                    "server_certificate_file": server_certificate_file,
                }
            ],
            "advanced": {
                "timeout": timeout,
                "transport": transport,
                "certificate_file": certificate_file,
                "private_key_file": private_key_file,
            },
        }
    )


if __name__ == "__main__":
    cli()
