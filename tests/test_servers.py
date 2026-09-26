"""Several servers in one extension, end to end against in-process simulators."""

from __future__ import annotations

import asyncio
import time

import pytest
import zelos_sdk

from zelos_extension_opcua import actions
from zelos_extension_opcua.cli import app
from zelos_extension_opcua.client import (
    SHUTDOWN_TIMEOUT,
    OPCUAClient,
    OPCUARunner,
    SharedSource,
)
from zelos_extension_opcua.demo.sim_server import Simulator
from zelos_extension_opcua.node_map import NodeMap

DEMO_MAP = str(app.get_demo_node_map_path())


async def wait_until(predicate, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"Condition not met within {timeout}s")
        await asyncio.sleep(0.05)


def clients_for(sims: dict[str, Simulator], prefix: str) -> list[OPCUAClient]:
    """Clients through the app's own resolution, started on the prefix's layout."""
    config = {
        "servers": [
            {
                "name": name,
                "endpoint": sim.endpoint,
                "node_map_file": DEMO_MAP,
                "poll_interval": 0.1,
            }
            for name, sim in sims.items()
        ],
        "advanced": {"timeout": 2.0},
    }
    clients = app.build_clients(app.resolve_servers(config, app.resolve_advanced(config)))
    shared = SharedSource(zelos_sdk.TraceSource(prefix)) if prefix else None
    for client in clients:
        client.start(shared)
    return clients


async def test_one_dead_server_does_not_stall_the_other():
    demo_map = NodeMap.from_file(DEMO_MAP)
    press = Simulator(node_map=demo_map, port=0)
    oven = Simulator(node_map=demo_map, port=0)
    await press.start()
    await oven.start()
    oven_port = oven.server.bserver.port
    runner = OPCUARunner(clients_for({"press": press, "oven": oven}, "OPC-UA"))
    task = asyncio.create_task(runner._run_async())
    a, b = runner.clients["press"], runner.clients["oven"]
    try:
        assert {c._source.name for c in (a, b)} == {"OPC-UA"}
        assert a._events["temperature"].name == "press/temperature"
        assert b._events["temperature"].name == "oven/temperature"
        await wait_until(lambda: a._poll_count >= 3 and b._poll_count >= 3, 15.0)

        # Actions dispatch into the shared loop, to the named server.
        read = await asyncio.to_thread(actions_read, runner, "temp_sensor1", "oven")
        assert read["name"] == "temp_sensor1" and isinstance(read["value"], float)
        with pytest.raises(ValueError, match="set server to one of: press, oven"):
            await asyncio.to_thread(actions_read, runner, "temp_sensor1", "")

        await oven.stop()
        await wait_until(lambda: not b._connected, 10.0)
        dead_polls, live_polls = b._poll_count, a._poll_count
        await asyncio.sleep(2.0)  # inside oven's backoff
        assert a._poll_count >= live_polls + 10
        assert b._poll_count == dead_polls and not b._connected

        oven = Simulator(node_map=demo_map, port=oven_port)
        await oven.start()
        await wait_until(lambda: b._connected and b._poll_count > dead_polls, 20.0)
        assert a._connected
    finally:
        actions.set_runner(None)
        started = time.monotonic()
        runner.stop()
        await asyncio.wait_for(task, 10.0)
        elapsed = time.monotonic() - started
        await press.stop()
        await oven.stop()
    assert elapsed < SHUTDOWN_TIMEOUT
    assert not a._connected and not b._connected


def actions_read(runner: OPCUARunner, name: str, server: str) -> dict:
    actions.set_runner(runner)
    return actions.read_named_node(name, server)


async def test_cleared_prefix_gives_each_server_its_own_source():
    demo_map = NodeMap.from_file(DEMO_MAP)
    async with (
        Simulator(node_map=demo_map, port=0) as s1,
        Simulator(node_map=demo_map, port=0) as s2,
    ):
        runner = OPCUARunner(clients_for({"press": s1, "oven": s2}, ""))
        task = asyncio.create_task(runner._run_async())
        try:
            await wait_until(lambda: all(c._poll_count >= 1 for c in runner.clients.values()), 15.0)
        finally:
            runner.stop()
            await asyncio.wait_for(task, 10.0)
    for name, client in runner.clients.items():
        assert client._source.name == name
        assert client._events["temperature"].name == "temperature"


def test_actions_server_selection():
    one = OPCUARunner([OPCUAClient(endpoint="opc.tcp://plc01:4840")])
    two = OPCUARunner([OPCUAClient(name="a"), OPCUAClient(name="b")])
    try:
        actions.set_runner(one)
        assert actions.get_status()["servers"][0]["server"] == "plc01"
        assert actions.list_nodes() == {"nodes": [], "count": 0}

        actions.set_runner(two)
        assert actions.get_status()["count"] == 2
        assert actions.get_status("b")["server"] == "b"
        with pytest.raises(ValueError, match="set server to one of: a, b"):
            actions.read_named_node("x")
        with pytest.raises(ValueError, match="Unknown server 'c'. Servers: a, b"):
            actions.read_node("ns=2;i=1", "c")
    finally:
        actions.set_runner(None)
