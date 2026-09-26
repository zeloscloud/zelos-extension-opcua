"""Simulator profiles and flags, each against a real in-process server."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from asyncua import Client, ua

from zelos_extension_opcua.cli.app import get_demo_node_map_path
from zelos_extension_opcua.client import OPCUAClient
from zelos_extension_opcua.demo import profiles
from zelos_extension_opcua.demo.sim_server import NS_SHIFT_ENV, Simulator
from zelos_extension_opcua.discovery import HEALTH_EVENT
from zelos_extension_opcua.node_map import NodeMap


async def test_s7_limits_are_advertised_and_enforced():
    async with Simulator("s7", port=0) as sim, Client(sim.endpoint) as c:
        ids = ua.ObjectIds
        browse = c.get_node(ids.Server_ServerCapabilities_OperationLimits_MaxNodesPerBrowse)
        read = c.get_node(ids.Server_ServerCapabilities_OperationLimits_MaxNodesPerRead)
        assert await browse.read_value() == 10
        assert await read.read_value() == 20

        idx = await c.get_namespace_index(profiles.S7_URI)
        line = c.get_node(ua.NodeId('"DB_Line"', idx))
        tags = await line.get_children()
        assert len(tags) == len(profiles.S7_DBS["DB_Line"])  # needs BrowseNext
        assert any(svc == "BrowseNext" for _, svc in sim.request_log)
        assert tags[0].nodeid.Identifier == '"DB_Line"."Speed"'

        with pytest.raises(ua.UaStatusCodeError, match="BadTooManyOperations"):
            await c.read_values(tags + tags)

        # `c` holds one of the 4 sessions.
        async with contextlib.AsyncExitStack() as stack:
            for _ in range(3):
                await stack.enter_async_context(Client(sim.endpoint))
            with pytest.raises(ua.UaStatusCodeError, match="BadTooManySessions"):
                await stack.enter_async_context(Client(sim.endpoint))


async def test_shuffle_moves_index_not_uri(monkeypatch):
    indices = []
    for shift in ("1", "2"):
        monkeypatch.setenv(NS_SHIFT_ENV, shift)
        async with (
            Simulator("gateway", port=0, shuffle_namespaces=True) as sim,
            Client(sim.endpoint) as c,
        ):
            idx = await c.get_namespace_index(profiles.GATEWAY_URI)
            node = c.get_node(ua.NodeId("ModbusTCP.PowerMeter.Frequency", idx))
            assert 49 < await node.read_value() < 51
            indices.append(idx)
    assert indices[0] != indices[1]


async def test_map_polls_and_writes_persist():
    node_map = NodeMap.from_file(get_demo_node_map_path())
    async with Simulator(port=0, node_map=node_map) as sim:
        client = OPCUAClient(endpoint=sim.endpoint, node_map=node_map)
        assert await client.connect() is True
        try:
            results = await client._poll_nodes()
            assert sum(len(v) for e, v in results.items() if e != HEALTH_EVENT) == len(
                node_map.nodes
            )

            setpoint = node_map.get_by_name("setpoint")
            await client.write_node_value(setpoint, 42.0)
            await asyncio.sleep(0.6)  # > one drift tick
            assert await client.read_node_value(setpoint) == 42.0
        finally:
            await client.disconnect()
