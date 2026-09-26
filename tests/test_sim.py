"""Simulator profiles and flags, each against a real in-process server."""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import socket

import pytest
from asyncua import Client, ua
from asyncua.crypto.cert_gen import setup_self_signed_certificate
from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256
from cryptography.x509.oid import ExtendedKeyUsageOID

from zelos_extension_opcua.cli.app import get_demo_node_map_path
from zelos_extension_opcua.client import OPCUAClient
from zelos_extension_opcua.demo import profiles
from zelos_extension_opcua.demo.sim_server import NS_SHIFT_ENV, Simulator
from zelos_extension_opcua.discovery import HEALTH_EVENT
from zelos_extension_opcua.node_map import NodeMap


async def test_gateway_is_kepware_shaped():
    async with Simulator("gateway", port=0) as sim, Client(sim.endpoint) as c:
        idx = await c.get_namespace_index(profiles.GATEWAY_URI)
        device = c.get_node(ua.NodeId("ModbusTCP.PowerMeter", idx))
        ids = {n.nodeid.Identifier for n in await device.get_children()}
        assert "ModbusTCP.PowerMeter._System" in ids
        assert all(re.fullmatch(r"ModbusTCP\.PowerMeter\.\w+", i) for i in ids)
        volts = c.get_node(ua.NodeId("ModbusTCP.PowerMeter.Voltage_L1", idx))
        assert 200 < await volts.read_value() < 260


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


async def test_device_describes_and_misbehaves():
    async with Simulator("device", port=0) as sim, Client(sim.endpoint) as c:
        idx = await c.get_namespace_index(profiles.DEVICE_URI)
        volts = c.get_node(ua.NodeId("Pump01.ParameterSet.Voltage", idx))
        eu = await (await volts.get_child("0:EngineeringUnits")).read_value()
        rng = await (await volts.get_child("0:EURange")).read_value()
        assert eu.DisplayName.Text == "V"
        assert (rng.Low, rng.High) == (0.0, 480.0)

        vib = c.get_node(ua.NodeId("Pump01.ParameterSet.Vibration", idx))
        assert len(await vib.read_value()) == 4

        flow = c.get_node(ua.NodeId("Pump01.ParameterSet.FlowSensor", idx))
        dv = await flow.read_data_value(raise_on_bad_status=False)
        assert dv.StatusCode.is_bad()


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


@pytest.mark.parametrize("trusted", [True, False])
async def test_secure_session_and_trust_list(tmp_path, trusted):
    app_uri = "urn:zelos:test:client"
    key, cert = tmp_path / "key.pem", tmp_path / "cert.der"
    await setup_self_signed_certificate(
        key,
        cert,
        app_uri,
        socket.gethostname(),
        [ExtendedKeyUsageOID.CLIENT_AUTH],
        {"commonName": "test client"},
    )
    trust = tmp_path / "trusted"
    trust.mkdir()
    if trusted:
        shutil.copy(cert, trust / "client.der")

    async with Simulator(port=0, secure=True, trust_dir=trust) as sim:
        client = Client(sim.endpoint)
        client.application_uri = app_uri
        await client.set_security(
            SecurityPolicyBasic256Sha256,
            str(cert),
            str(key),
            mode=ua.MessageSecurityMode.SignAndEncrypt,
        )
        if trusted:
            async with client:
                pass
            assert list(sim.sessions.values()) == [("SignAndEncrypt", "Basic256Sha256")]
        else:
            with pytest.raises(ua.UaStatusCodeError, match="BadCertificateUntrusted"):
                await client.connect()
            with contextlib.suppress(Exception):
                await client.disconnect()
            assert not sim.sessions


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


async def test_request_log_records_services_per_session():
    async with Simulator(port=0) as sim:
        async with Client(sim.endpoint) as c:
            await c.nodes.objects.get_children()
            await c.nodes.server_state.read_value()
        (sid,) = sim.sessions
        services = {svc for s, svc in sim.request_log if s == sid}
        assert {"CreateSession", "ActivateSession", "Browse", "Read", "CloseSession"} <= services
