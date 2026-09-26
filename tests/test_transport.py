"""Subscriptions, polling fallback, the staleness sweep and sample timestamps, end to end."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator

import pytest
import zelos_sdk
from asyncua import ua

from zelos_extension_opcua import client as client_mod
from zelos_extension_opcua.cli.app import get_demo_node_map_path
from zelos_extension_opcua.client import OPCUAClient, OPCUARunner, SharedSource, timestamp_ns
from zelos_extension_opcua.demo import profiles
from zelos_extension_opcua.demo.sim_server import Simulator
from zelos_extension_opcua.demo.simulator import DEMO_NAMESPACE
from zelos_extension_opcua.discovery import HEALTH_EVENT
from zelos_extension_opcua.node_map import NodeMap

SUBSCRIPTION_SERVICES = {"CreateSubscription", "CreateMonitoredItems", "Publish"}


class Rows:
    """Every row a client logs: (event, time_ns or None for host time, fields)."""

    def __init__(self, client: OPCUAClient) -> None:
        self.rows: list[tuple[str, int | None, dict]] = []
        client._events = {name: self._wrap(name, e) for name, e in client._events.items()}
        declare = client._declare

        def declare_wrapped(name, fields):  # events discovery declares on connect
            declare(name, fields)
            client._events[name] = self._wrap(name, client._events[name])

        client._declare = declare_wrapped

    def _wrap(self, name: str, event):
        rows = self.rows

        class Event:
            def log(self, **fields):
                rows.append((name, None, fields))
                event.log(**fields)

            def log_at(self, stamp, **fields):
                rows.append((name, stamp, fields))
                event.log_at(stamp, **fields)

        return Event()

    def of(self, field: str) -> list[tuple[int, object]]:
        """(time, value) of every sample of `field`, in log order."""
        return [(t, f[field]) for _, t, f in self.rows if field in f]

    def fields(self) -> set[str]:
        return {k for e, _, f in self.rows if e != HEALTH_EVENT for k in f}


@contextlib.asynccontextmanager
async def running(client: OPCUAClient) -> AsyncIterator[Rows]:
    """`client` started and run in a runner; stopped on exit."""
    client.start(SharedSource(zelos_sdk.TraceSource("OPC-UA")))
    rows = Rows(client)
    runner = OPCUARunner([client])
    task = asyncio.create_task(runner._run_async())
    try:
        yield rows
    finally:
        runner.stop()
        await asyncio.wait_for(task, 10.0)


async def wait_until(predicate, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"not met within {timeout}s"
        await asyncio.sleep(0.05)


@pytest.fixture
def reads(monkeypatch) -> list[ua.NodeId]:
    """Every NodeId the client reads, connect-time reads included."""
    seen: list[ua.NodeId] = []
    real = client_mod.read_many

    async def spy(client, items, chunk):
        seen.extend(node_id for node_id, _ in items)
        return await real(client, items, chunk)

    monkeypatch.setattr(client_mod, "read_many", spy)
    return seen


def services(sim: Simulator) -> set[str]:
    return {svc for _, svc in sim.request_log}


async def test_subscription_samples_carry_the_source_timestamp(reads):
    node_map = NodeMap.from_file(get_demo_node_map_path())
    async with Simulator(port=0) as sim:
        client = OPCUAClient(endpoint=sim.endpoint, node_map=node_map, poll_interval=0.2)
        async with running(client) as rows:
            await wait_until(lambda: len(rows.of("temp_sensor1")) >= 3, 10.0)
            await sim._updater.stop()  # freeze: the last write is the last notification
            await asyncio.sleep(0.6)
            idx = await sim.server.get_namespace_index(DEMO_NAMESPACE)
            written = await sim.server.get_node(
                ua.NodeId("Temperature.Sensor1", idx)
            ).read_data_value()
            status = client.status()

    stamp, value = rows.of("temp_sensor1")[-1]
    assert stamp == timestamp_ns(written.SourceTimestamp)
    assert value == pytest.approx(written.Value.Value, rel=1e-6)
    assert (status["subscribed"], status["polled"]) == (len(node_map.nodes), 0)
    assert services(sim) >= SUBSCRIPTION_SERVICES
    mapped = {t[2].nodeid for t in client._poll_targets}
    assert not mapped & set(reads)  # only health is read per cycle


@pytest.mark.parametrize("cap", ["monitored_items", "subscriptions"])
async def test_what_the_server_refuses_is_polled(cap, caplog):
    """The s7 profile caps an S7's way: 10 monitored items, 5 subscriptions a session."""
    tags = [(db, name, vtype) for db, t in profiles.S7_DBS.items() for name, vtype, _, _ in t]
    datatypes = {v: k for k, v in profiles.MAP_VARIANTS.items()}
    node_map = None  # discovery: 19 items on one interval, 9 past the item cap
    if cap == "subscriptions":
        # Six intervals of one item: the sixth subscription is refused.
        node_map = NodeMap.from_dict(
            {
                "events": {
                    f"e{i}": {
                        "poll_interval": 0.2 + i / 10,
                        "nodes": [
                            {
                                "name": name,
                                "node_id": f'nsu={profiles.S7_URI};s="{db}"."{name}"',
                                "datatype": datatypes[vtype],
                            }
                        ],
                    }
                    for i, (db, name, vtype) in enumerate(tags[:6])
                }
            }
        )
    expected = {
        "monitored_items": ("BadTooManyMonitoredItems 9", 10, 9, len(tags)),
        "subscriptions": ("BadTooManySubscriptions 1", 5, 1, 6),
    }[cap]
    reason, subscribed, polled, fields = expected

    async with Simulator("s7", port=0) as sim:
        client = OPCUAClient(endpoint=sim.endpoint, node_map=node_map, poll_interval=0.2)
        with caplog.at_level(logging.WARNING, logger="zelos_extension_opcua.client"):
            async with running(client) as rows:
                await wait_until(lambda: len(rows.fields()) == fields, 10.0)
                await asyncio.sleep(0.5)  # several poll cycles: still one warning
                status = client.status()

    warnings = [r.getMessage() for r in caplog.records if "refused" in r.getMessage()]
    assert len(warnings) == 1 and reason in warnings[0], warnings
    assert (status["subscribed"], status["polled"]) == (subscribed, polled)


async def test_static_node_is_refreshed_at_the_server_timestamp(reads):
    node_map = NodeMap.from_dict(
        {
            "name": "m",
            "events": {
                "e": [
                    {"name": "static", "node_id": "ns=2;s=Static", "writable": True},
                    {"name": "moving", "node_id": "ns=2;s=Moving"},
                ]
            },
        }
    )
    async with Simulator(port=0, node_map=node_map) as sim:
        created = await sim.server.get_node(ua.NodeId("Static", 2)).read_data_value()
        client = OPCUAClient(
            endpoint=sim.endpoint, node_map=node_map, poll_interval=0.2, min_update_interval=1.0
        )
        async with running(client) as rows:
            started = time.time_ns()
            await wait_until(lambda: len(rows.of("static")) >= 3, 10.0)

    (first, _), (second, _), (third, _) = rows.of("static")[:3]
    assert first == timestamp_ns(created.SourceTimestamp)  # the initial notification
    # Refreshes: logged at the read's ServerTimestamp, about min_update_interval apart.
    assert started < second < third <= time.time_ns()
    assert 0.9e9 < third - second < 1.25e9 + 0.5e9
    moving = {t[2].nodeid for t in client._poll_targets if t[1].name == "moving"}
    assert not moving & set(reads)  # a changing item is never re-read


async def test_poll_transport_never_subscribes():
    node_map = NodeMap.from_file(get_demo_node_map_path())
    async with Simulator(port=0, node_map=node_map) as sim:
        client = OPCUAClient(
            endpoint=sim.endpoint, node_map=node_map, poll_interval=0.2, transport="poll"
        )
        async with running(client) as rows:
            await wait_until(lambda: len(rows.fields()) == len(node_map.nodes), 10.0)
            status = client.status()

    assert not SUBSCRIPTION_SERVICES & services(sim)
    assert (status["subscribed"], status["polled"]) == (0, len(node_map.nodes))
    assert all(t is not None for e, t, _ in rows.rows if e != HEALTH_EVENT)
