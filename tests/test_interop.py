"""Interop against Microsoft OPC PLC (.NET stack) in Docker. Manual, pre-release.

Run with `just e2e-interop`; skipped unless ZELOS_INTEROP=1. Needs docker and a
free port 4840 (auto_config probes well-known ports only).
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import zelos_sdk
from asyncua import Client
from test_security import user_cert

from zelos_extension_opcua import actions
from zelos_extension_opcua import client as client_mod
from zelos_extension_opcua.client import OPCUAClient, OPCUARunner, SharedSource, timestamp_ns
from zelos_extension_opcua.discovery import HEALTH_EVENT
from zelos_extension_opcua.node_map import NodeMap

pytestmark = pytest.mark.skipif(
    os.environ.get("ZELOS_INTEROP") != "1", reason="set ZELOS_INTEROP=1 (needs docker)"
)

IMAGE = os.environ.get("ZELOS_INTEROP_IMAGE", "mcr.microsoft.com/iotedge/opc-plc:latest")
CONTAINER = "zelos-opcua-interop"
ENDPOINT = "opc.tcp://localhost:4840"


def b64(path: str | Path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode()


@pytest.fixture(scope="module")
def opcplc(tmp_path_factory):
    """OPC PLC on localhost:4840 trusting the generated client cert and one user
    cert, no auto-accept; yields that user identity."""
    assert shutil.which("docker"), "docker not on PATH"
    pki = tmp_path_factory.mktemp("pki")
    app_cert, _ = client_mod.ensure_client_certificate(pki)
    identity = user_cert(tmp_path_factory.mktemp("users"))
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    subprocess.run(
        ["docker", "run", "-d", "--name", CONTAINER, "-p", "4840:50000", IMAGE,
         "--pn=50000", "--ut", "--sn=10", "--fn=5",
         f"--tb={b64(app_cert)}", f"--tub={b64(identity['user_certificate_file'])}"],
        check=True, capture_output=True,
    )  # fmt: skip
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(client_mod, "PKI_DIR", pki)
            deadline = time.monotonic() + 60
            while not asyncio.run(_answers()):
                assert time.monotonic() < deadline, "OPC PLC did not come up within 60s"
                time.sleep(1)
            yield identity
    finally:
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)


async def _answers() -> bool:
    try:
        return bool(await Client(ENDPOINT, timeout=2).connect_and_get_server_endpoints())
    except Exception:
        return False


async def polled(**kwargs) -> tuple[bool, list[dict]]:
    """Connect a no-map client, then two polls 1.5s apart."""
    client = OPCUAClient(endpoint=ENDPOINT, name="plc", **kwargs)
    client.start(SharedSource(zelos_sdk.TraceSource("OPC-UA")))
    if not await client.connect():
        return False, []
    try:
        first = await client._poll_nodes()
        await asyncio.sleep(1.5)
        return True, [first, await client._poll_nodes()]
    finally:
        await client.disconnect()


def assert_telemetry(polls: list[dict]) -> None:
    first, second = polls
    assert first[HEALTH_EVENT]["state_name"] == "Running"
    assert len(first["OpcPlc/Telemetry/Slow"]) >= 10
    assert (
        second["OpcPlc/Telemetry/Fast"]["FastUInt1"] > first["OpcPlc/Telemetry/Fast"]["FastUInt1"]
    )


async def test_none_discovers_and_polls(opcplc):
    ok, polls = await polled()
    assert ok
    assert_telemetry(polls)


async def test_sign_and_encrypt_with_generated_cert(opcplc):
    ok, polls = await polled(security_mode="SignAndEncrypt", security_policy="Basic256Sha256")
    assert ok
    assert_telemetry(polls)


async def test_user_certificate_login(opcplc, tmp_path):
    secure = {"security_mode": "SignAndEncrypt", "security_policy": "Basic256Sha256"}
    assert (await polled(**secure, **opcplc))[0] is True
    assert (await polled(**secure, **user_cert(tmp_path / "stranger", "stranger")))[0] is False


FAST = NodeMap.from_dict(
    {
        "events": {
            "fast": [
                {
                    "name": f"FastUInt{i}",
                    "node_id": f"nsu=http://microsoft.com/Opc/OpcPlc/;s=FastUInt{i}",
                    "datatype": "uint32",
                }
                for i in range(1, 6)
            ]
        }
    }
)


@pytest.mark.parametrize("node_map", [FAST, None], ids=["fast_map", "discovered"])
async def test_subscriptions_deliver_fast_nodes_at_source_time(opcplc, monkeypatch, node_map):
    """Mapped fast nodes stay subscribed; discovering everything, a Publish that
    asyncua cannot decode (OPC PLC's random Variants) moves it all to polling."""
    stamped: list[tuple[int, int | None]] = []  # FastUInt1: (logged at, SourceTimestamp)

    client = OPCUAClient(endpoint=ENDPOINT, name="plc", node_map=node_map)
    real = client._log_samples

    def log_samples(samples, received_ns, refresh=False):
        samples = list(samples)
        for _, node, dv in samples:
            if node.name == "FastUInt1":
                at = client_mod.sample_time_ns(dv, received_ns, refresh)
                stamped.append((at, timestamp_ns(dv.SourceTimestamp)))
        real(samples, received_ns, refresh)

    monkeypatch.setattr(client, "_log_samples", log_samples)
    client.start(SharedSource(zelos_sdk.TraceSource("OPC-UA")))
    runner = OPCUARunner([client])
    task = asyncio.create_task(runner._run_async())
    try:
        deadline = time.monotonic() + 20
        while len(stamped) < 3:
            assert time.monotonic() < deadline, stamped
            await asyncio.sleep(0.1)
        status = client.status()
    finally:
        runner.stop()
        await asyncio.wait_for(task, 10.0)

    assert status["subscribed"] + status["polled"] == status["nodes"] > 0
    if node_map is FAST:
        assert status["subscribed"] == 5
    assert all(source is not None and at == source for at, source in stamped)
    assert len({at for at, _ in stamped}) == len(stamped)


async def test_auto_config_finds_it(opcplc):
    result = await asyncio.to_thread(actions.auto_config)
    found = {
        s["endpoint"]: (s["security_mode"], s["security_policy"])
        for s in result["config"]["servers"]
    }
    assert found.get(f"{ENDPOINT}/") == ("None", "None"), found
