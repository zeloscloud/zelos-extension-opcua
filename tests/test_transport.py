"""Subscriptions, polling fallback, the staleness sweep and sample timestamps, end to end."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import zelos_sdk
from asyncua import ua
from asyncua.client.ua_client import UaClient
from asyncua.ua.uaerrors import UaStructParsingError

from zelos_extension_opcua import client as client_mod
from zelos_extension_opcua.cli.app import get_demo_node_map_path
from zelos_extension_opcua.client import (
    SESSION_TIMEOUT_MS,
    OPCUAClient,
    OPCUARunner,
    SharedSource,
    revisions,
    timestamp_ns,
)
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

    async def spy(client, items, chunk, max_age=0.0):
        seen.extend(node_id for node_id, _ in items)
        return await real(client, items, chunk, max_age)

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
    mapped = {t[2] for t in client._poll_targets}
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
    moving = {t[2] for t in client._poll_targets if t[1].name == "moving"}
    assert not moving & set(reads)  # a changing item is never re-read


async def test_read_max_age_per_kind(monkeypatch):
    """Polled chunks accept a value one interval old, staleness refreshes one
    min_update_interval old; health and discovery read fresh."""
    sent: list[tuple[float, set[ua.NodeId], set[int]]] = []  # MaxAge, nodes, attributes
    read = UaClient.read

    async def spy(self, params):
        nodes = params.NodesToRead
        sent.append((params.MaxAge, {r.NodeId for r in nodes}, {r.AttributeId for r in nodes}))
        return await read(self, params)

    monkeypatch.setattr(UaClient, "read", spy)
    static = ua.NodeId("Static", 2)
    node_map = NodeMap.from_dict(
        {"events": {"e": [{"name": "static", "node_id": "ns=2;s=Static"}]}}
    )
    async with Simulator(port=0, node_map=node_map) as sim:
        subscribed = OPCUAClient(
            endpoint=sim.endpoint, node_map=node_map, poll_interval=0.2, min_update_interval=0.5
        )
        async with running(subscribed):
            await wait_until(lambda: any(static in n for a, n, _ in sent if a == 500), 10.0)
        discovered = OPCUAClient(endpoint=sim.endpoint, poll_interval=0.2, transport="poll")
        async with running(discovered):
            await wait_until(lambda: any(static in n for a, n, _ in sent if a == 200), 10.0)

    health = ua.NodeId(ua.ObjectIds.Server_ServerStatus_State)
    value = {ua.AttributeIds.Value}
    assert all(age == 0 for age, nodes, attrs in sent if health in nodes or attrs != value)
    assert {age for age, _, _ in sent} == {0, 200, 500}


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


async def test_publish_decode_failure_during_setup_still_traces_every_node(monkeypatch):
    """A Publish that fails to decode mid-subscription-setup polls every node, none lost."""
    real_limits = client_mod.operation_limits

    async def limits(client):
        browse, read, _ = await real_limits(client)
        return browse, read, 50  # several CreateMonitoredItems calls

    real_guard = OPCUAClient._guard_publish

    def guard(self, client):
        session = client.uaclient.session
        publish, failed = session.publish, []

        async def publish_once_bad(acks):
            if failed:
                return await publish(acks)
            failed.append(True)
            while len(self._monitored) < 50:  # the first chunk is registered
                await asyncio.sleep(0.001)
            raise UaStructParsingError("injected")

        session.publish = publish_once_bad
        real_guard(self, client)

    monkeypatch.setattr(client_mod, "operation_limits", limits)
    monkeypatch.setattr(OPCUAClient, "_guard_publish", guard)
    async with Simulator(port=0, nodes=300) as sim:
        client = OPCUAClient(endpoint=sim.endpoint, poll_interval=0.5)
        async with running(client) as rows:
            await wait_until(lambda: client._jobs, 20.0)
            expected = {(event, node.name) for event, node, _ in client._poll_targets}
            assert len(expected) >= 300

            def traced():
                return {(e, k) for e, _, f in rows.rows for k in f}

            await wait_until(lambda: expected <= traced(), 10.0)
            status = client.status()
            assert (status["subscribed"], status["polled"]) == (0, len(expected))


async def test_shutdown_does_not_wait_out_a_hung_request(monkeypatch):
    """A Read the server never answers is cancelled at stop, not awaited to its timeout."""
    hung = asyncio.Event()
    real = client_mod.read_many

    async def read_many(client, items, chunk, max_age=0.0):
        if hung.is_set():
            await asyncio.Event().wait()
        return await real(client, items, chunk, max_age)

    monkeypatch.setattr(client_mod, "read_many", read_many)
    async with Simulator(port=0) as sim:
        client = OPCUAClient(endpoint=sim.endpoint, poll_interval=0.2, timeout=30.0)
        client.start(SharedSource(zelos_sdk.TraceSource("OPC-UA")))
        runner = OPCUARunner([client])
        task = asyncio.create_task(runner._run_async())
        await wait_until(lambda: client._poll_count >= 2, 10.0)
        hung.set()
        await asyncio.sleep(0.5)  # the next health Read is in flight
        started = time.monotonic()
        runner.stop()
        await asyncio.wait_for(task, 10.0)
        assert time.monotonic() - started < 3.5


async def drop_connections(sim: Simulator, client: OPCUAClient) -> None:
    """Reset every TCP connection, sessions left open as on a network loss; await the reconnect."""
    old = client._client
    for transport in list(sim.server.iserver.asyncio_transports):
        transport.abort()
    await wait_until(lambda: client._connected and client._client is not old, 20.0)


async def test_lost_connections_leave_no_session_behind():
    """The s7 sim holds a lost connection's session, as a PLC does, and caps sessions at 4."""
    async with Simulator("s7", port=0) as sim:
        client = OPCUAClient(endpoint=sim.endpoint, poll_interval=0.2)
        async with running(client):
            await wait_until(lambda: client._connected, 10.0)
            for _ in range(4):  # unclosed, the fourth reconnect finds every slot held
                await drop_connections(sim, client)
                assert sim.held_sessions == 1
            (session,) = sim.server.iserver._external_sessions.values()
            assert session.session_timeout == SESSION_TIMEOUT_MS / 1000
    assert len(sim.sessions) == 1  # resumed every time


def gate(client: OPCUAClient) -> asyncio.Event:
    """Holds the client's connects while cleared: an outage with the server up."""
    opened = asyncio.Event()
    opened.set()
    create = client._create_client

    async def gated():
        await opened.wait()
        return await create()

    client._create_client = gated
    return opened


def infos(caplog, text: str) -> list[str]:
    return [
        m for r in caplog.records if r.levelno == logging.INFO and text in (m := r.getMessage())
    ]


async def test_lost_connection_resumes_the_session_and_its_subscriptions(caplog):
    """A 2 s outage: the session is re-activated, nothing re-created, a value
    written meanwhile is traced at its source timestamp."""
    caplog.set_level(logging.INFO, logger="zelos_extension_opcua.client")
    node_map = NodeMap.from_file(get_demo_node_map_path())
    async with Simulator(port=0) as sim:
        client = OPCUAClient(endpoint=sim.endpoint, node_map=node_map, poll_interval=0.2)
        opened = gate(client)
        async with running(client) as rows:
            await wait_until(lambda: client._jobs, 10.0)
            subs = set(client._subs)
            opened.clear()
            for transport in list(sim.server.iserver.asyncio_transports):
                transport.abort()
            await wait_until(lambda: not client._connected, 10.0)
            since = len(sim.request_log)
            stamp = datetime.now(UTC)
            value = ua.DataValue(ua.Variant(42.5, ua.VariantType.Float), SourceTimestamp=stamp)
            await sim.server.get_node("ns=2;s=Temperature.Setpoint").write_value(value)
            await asyncio.sleep(2.0)
            opened.set()
            await wait_until(lambda: (timestamp_ns(stamp), 42.5) in rows.of("setpoint"), 10.0)
            seen = len(rows.of("temp_sensor1"))
            await wait_until(lambda: len(rows.of("temp_sensor1")) >= seen + 3, 5.0)
            assert sim.held_sessions == 1
    after = {svc for _, svc in sim.request_log[since:]}
    assert {"ActivateSession", "Republish"} <= after  # the response lost with the link
    assert not after & {"CreateSession", "CreateSubscription", "CreateMonitoredItems"}
    assert set(client._subs) == subs
    assert len(infos(caplog, "Reconnected; session and subscriptions kept")) == 1


async def test_expired_session_falls_back_to_a_new_one(monkeypatch, caplog):
    """An outage past the session timeout: one INFO, a new session, values resume."""
    caplog.set_level(logging.INFO, logger="zelos_extension_opcua.client")
    monkeypatch.setattr(client_mod, "SESSION_TIMEOUT_MS", 1000)
    node_map = NodeMap.from_file(get_demo_node_map_path())
    async with Simulator(port=0) as sim:
        sim.server.iserver.min_session_timeout_ms = 1000
        client = OPCUAClient(endpoint=sim.endpoint, node_map=node_map, poll_interval=0.2)
        opened = gate(client)
        async with running(client) as rows:
            await wait_until(lambda: client._jobs, 10.0)
            opened.clear()
            for transport in list(sim.server.iserver.asyncio_transports):
                transport.abort()
            await wait_until(lambda: not client._connected and not sim.held_sessions, 10.0)
            opened.set()
            await wait_until(lambda: client._connected, 10.0)
            seen = len(rows.of("temp_sensor1"))
            await wait_until(lambda: len(rows.of("temp_sensor1")) >= seen + 3, 5.0)
            assert sim.held_sessions == 1
    assert infos(caplog, "not resumed") == [
        "[127_0_0_1] Previous session not resumed (BadSessionIdInvalid); creating a new one"
    ]
    assert len(sim.sessions) == 2


async def test_restarted_server_gets_a_new_session(caplog):
    caplog.set_level(logging.INFO, logger="zelos_extension_opcua.client")
    node_map = NodeMap.from_file(get_demo_node_map_path())
    async with Simulator(port=0) as sim:
        client = OPCUAClient(endpoint=sim.endpoint, node_map=node_map, poll_interval=0.2)
        port = sim.server.bserver.port
        async with running(client) as rows:
            await wait_until(lambda: client._jobs, 10.0)
            await sim.stop()
            async with Simulator(port=port) as restarted:
                await wait_until(lambda: restarted.held_sessions == 1, 15.0)
                seen = len(rows.of("temp_sensor1"))
                await wait_until(lambda: len(rows.of("temp_sensor1")) >= seen + 3, 5.0)
    assert len(infos(caplog, "Previous session not resumed (BadSessionIdInvalid)")) == 1
    assert not infos(caplog, "session and subscriptions kept")


async def test_bad_node_is_reported_again_on_a_new_connection(caplog):
    node_map = NodeMap.from_dict({"events": {"e": [{"name": "gone", "node_id": "ns=0;s=Gone"}]}})
    async with Simulator(port=0) as sim:
        client = OPCUAClient(
            endpoint=sim.endpoint, node_map=node_map, poll_interval=0.2, transport="poll"
        )

        def reports() -> int:
            return sum("'gone'" in r.getMessage() for r in caplog.records)

        async with running(client):
            await wait_until(lambda: reports() == 1, 10.0)
            await asyncio.sleep(0.5)  # several polls: still one
            assert reports() == 1
            await drop_connections(sim, client)
            await wait_until(lambda: reports() == 2, 10.0)


async def test_revised_publishing_interval_is_honored(monkeypatch, caplog):
    """A slower revised publishing interval is logged once and paces the staleness sweep."""
    real = UaClient.create_subscription

    async def revise(self, params, callback):
        result = await real(self, params, callback)
        result.RevisedPublishingInterval = 2000.0
        return result

    monkeypatch.setattr(UaClient, "create_subscription", revise)
    caplog.set_level(logging.INFO, logger="zelos_extension_opcua.client")
    node_map = NodeMap.from_file(get_demo_node_map_path())
    async with Simulator(port=0) as sim:
        client = OPCUAClient(
            endpoint=sim.endpoint, node_map=node_map, poll_interval=0.2, min_update_interval=1.0
        )
        async with running(client):
            await wait_until(lambda: client._jobs, 10.0)
            assert client._stale_after == 2.0
            subscriptions = len(client._subs)
    revised = [r.getMessage() for r in caplog.records if "server revised" in r.getMessage()]
    assert len(revised) == subscriptions >= 1  # one line per subscription
    assert all(m.endswith(" -> 2000 ms") and "publishing" in m for m in revised)


def test_revisions_report_sampling_and_queue_size():
    good, refused = ua.StatusCode(), ua.StatusCode(ua.StatusCodes.BadNodeIdUnknown)
    items = [
        ua.MonitoredItemCreateResult(good, RevisedSamplingInterval=100.0, RevisedQueueSize=1),
        ua.MonitoredItemCreateResult(good, RevisedSamplingInterval=250.0, RevisedQueueSize=1),
        ua.MonitoredItemCreateResult(good, RevisedSamplingInterval=250.0, RevisedQueueSize=5),
        ua.MonitoredItemCreateResult(refused, RevisedSamplingInterval=0.0),
    ]
    subscription = ua.CreateSubscriptionResult(RevisedPublishingInterval=100.0)
    assert revisions(100.0, subscription, items) == [
        "sampling 100 ms -> 250 ms (2 items)",
        "queue size 1 -> 5 (1 items)",
    ]
    assert revisions(100.0, subscription, items[:1]) == []


@pytest.mark.parametrize("transport", ["subscription", "poll"])
async def test_uncertain_values_are_traced_bad_are_not(transport, caplog):
    """Uncertain is usable (Part 8, as Kepware keeps it); Bad is a gap."""
    node_map = NodeMap.from_dict(
        {
            "events": {
                "e": [
                    {"name": name, "node_id": f"ns=2;s={name}", "writable": True}
                    for name in ("uncertain", "bad")
                ]
            }
        }
    )
    caplog.set_level(logging.INFO, logger="zelos_extension_opcua.client")
    async with Simulator(port=0, node_map=node_map) as sim:
        for name, code, value in (
            ("uncertain", ua.StatusCodes.UncertainLastUsableValue, 7.5),
            ("bad", ua.StatusCodes.BadSensorFailure, 9.5),
        ):
            node = sim.server.get_node(ua.NodeId(name, 2))
            vtype = (await node.read_data_value()).Value.VariantType
            await sim.server.write_attribute_value(
                node.nodeid, ua.DataValue(ua.Variant(value, vtype), ua.StatusCode(code))
            )
        client = OPCUAClient(
            endpoint=sim.endpoint, node_map=node_map, poll_interval=0.2, transport=transport
        )
        async with running(client) as rows:
            await wait_until(lambda: rows.of("uncertain"), 10.0)
            await asyncio.sleep(0.6)  # several cycles: one report each

    assert {v for _, v in rows.of("uncertain")} == {7.5} and not rows.of("bad")
    messages = [r.getMessage() for r in caplog.records]
    assert sum("'uncertain'" in m and "UncertainLastUsableValue" in m for m in messages) == 1
    assert sum("'bad'" in m and "BadSensorFailure" in m for m in messages) == 1


def subscription_warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "Subscription" in r.getMessage()
    ]


async def test_lost_notification_is_recovered_by_republish(caplog):
    """A lost Publish response comes back via Republish at its own source timestamps."""
    caplog.set_level(logging.WARNING, logger="zelos_extension_opcua.client")
    node_map = NodeMap.from_file(get_demo_node_map_path())
    async with Simulator(port=0) as sim:
        client = OPCUAClient(endpoint=sim.endpoint, node_map=node_map, poll_interval=0.2)
        async with running(client) as rows:
            await wait_until(lambda: client._jobs, 10.0)
            subs = set(client._subs)
            sim.drop_notifications = 1
            await wait_until(lambda: subscription_warnings(caplog), 10.0)
            assert set(client._subs) == subs  # not recreated
    [lost] = sim.dropped
    [warning] = subscription_warnings(caplog)
    assert warning.endswith(f"notifications {lost.SequenceNumber} missed, recovered by Republish")
    items = [i for n in lost.NotificationData for i in n.MonitoredItems]
    assert items
    for item in items:
        name = client._monitored[item.ClientHandle][1].name
        assert timestamp_ns(item.Value.SourceTimestamp) in {t for t, _ in rows.of(name)}


async def test_subscription_deleted_by_the_server_is_recreated(monkeypatch, caplog):
    """Silent past its keep-alive: recreated (one WARNING each), values resume."""
    monkeypatch.setattr(client_mod, "KEEPALIVE_SECONDS", 1.0)
    caplog.set_level(logging.WARNING, logger="zelos_extension_opcua.client")
    node_map = NodeMap.from_file(get_demo_node_map_path())
    async with Simulator(port=0) as sim:
        client = OPCUAClient(endpoint=sim.endpoint, node_map=node_map, poll_interval=0.2)
        async with running(client) as rows:
            await wait_until(lambda: client._jobs, 10.0)
            old = set(client._subs)
            deadline = max(s.deadline for s in client._subs.values())
            assert await sim.delete_subscriptions() == len(old)
            deleted = time.monotonic()
            await wait_until(
                lambda: len(client._subs) == len(old) and not old & set(client._subs), 10.0
            )
            latency = time.monotonic() - deleted
            seen = len(rows.of("temp_sensor1"))
            await wait_until(lambda: len(rows.of("temp_sensor1")) >= seen + 3, 5.0)
    assert latency <= deadline + client_mod.WATCH_SECONDS + 0.5
    warnings = subscription_warnings(caplog)
    assert len(warnings) == len(old)
    assert all(w.endswith(f"no keep-alive in {deadline:g} s; recreating it") for w in warnings)


async def test_unrecoverable_notification_recreates_the_subscription(caplog):
    """Republish answering BadMessageNotAvailable falls back to recreating (one WARNING)."""
    caplog.set_level(logging.WARNING, logger="zelos_extension_opcua.client")
    node_map = NodeMap.from_file(get_demo_node_map_path())
    async with Simulator(port=0) as sim:
        client = OPCUAClient(endpoint=sim.endpoint, node_map=node_map, poll_interval=0.2)
        async with running(client) as rows:
            await wait_until(lambda: client._jobs, 10.0)
            subs = set(client._subs)
            sim.retain_dropped, sim.drop_notifications = False, 1
            await wait_until(lambda: len(set(client._subs) - subs) == 1, 10.0)
            seen = len(rows.of("temp_sensor1"))
            await wait_until(lambda: len(rows.of("temp_sensor1")) >= seen + 3, 5.0)
    [lost] = sim.dropped
    assert "Republish" in services(sim)
    [warning] = subscription_warnings(caplog)
    assert warning.endswith(
        f"notifications {lost.SequenceNumber} lost (BadMessageNotAvailable); recreating it"
    )
