# CLAUDE.md

## Commands

```bash
just install      # deps + pre-commit hooks
just format       # ruff format + ruff check --fix
just check        # ruff check + ruff format --check
just test         # pytest (integration tests start a real demo server)
just e2e-interop  # Microsoft OPC PLC in Docker on :4840; manual, pre-release
just dev          # app mode, reads the Zelos App config
just demo         # built-in PLC simulator
just sim *ARGS    # standalone simulator: --profile demo|gateway|s7|device, --secure, --map, --nodes N
```

## Key Files

| Path | Role |
|------|------|
| `main.py` | Click entry point; re-exports `ACTION_PREFIX`, installs `TraceLoggingHandler("opcua_log")` |
| `zelos_extension_opcua/__init__.py` | `ACTION_PREFIX = "OPC-UA"` - must match `name` in `extension.toml` |
| `zelos_extension_opcua/actions.py` | Free-function action surface + `register_actions()` |
| `zelos_extension_opcua/client.py` | `OPCUAClient` (one server: connection, batch polling, reconnect) and `OPCUARunner` (the loop, signals, shutdown, action dispatch) |
| `zelos_extension_opcua/node_map.py` | Node/NodeMap parsing, name sanitization, collision rules, json/csv export |
| `zelos_extension_opcua/discovery.py` | Live discovery (walk, describe, name), OperationLimits, `HEALTH_EVENT` |
| `zelos_extension_opcua/autoconfig.py` | `auto_config` sources: localhost ports, LDS, mDNS |
| `zelos_extension_opcua/cli/app.py` | Config load, `servers[]` / `advanced` resolution, startup validation, `serve()` |
| `zelos_extension_opcua/demo/simulator.py` | Demo OPC-UA server |
| `zelos_extension_opcua/demo/plc_device.json` | Demo node map |
| `zelos_extension_opcua/demo/sim_server.py` | `demo-server` simulator: profiles, security, ns shift, request log, enforced limits |
| `zelos_extension_opcua/demo/profiles.py` | gateway / s7 / device address spaces, `--map` serving |
| `tests/test_opcua.py` | Extension tests |
| `tests/test_sim.py` | Simulator profile and flag tests |
| `tests/test_security.py` | Secure sessions, cert trust and pinning, config errors, per-server security inheritance |
| `tests/test_servers.py` | Several servers in one runner: layout, isolation, recovery, action selection |
| `tests/test_discovery.py` | Discovery per profile, limits, health, `discovered_map`, naming, `auto_config` |
| `tests/test_interop.py` | Microsoft OPC PLC (.NET stack) in Docker: None, SignAndEncrypt, user cert, `auto_config`; `ZELOS_INTEROP=1` only |

## Architecture

### Startup order

`cli/app.serve()` is the only place that starts clients, and the order is load
config -> resolve servers -> build one client per server -> `set_runner` ->
`register_actions` -> `init_global_source(prefix)` -> `zelos_sdk.init(name=ACTION_PREFIX)`
-> `client.start(shared source)` -> `runner.run()`. Registration must precede
`init()`; actions registered after the service is published may never be
advertised. The prefix source must exist before `init()`, which then reuses it
as the global source instead of adding an empty second one of the same name.

Startup problems (bad config, missing or unparseable node map) are one
`logger.error` line plus `sys.exit(1)`. Tracebacks are for bugs.

### Config

`servers[]` plus `advanced`. `run_app_mode` checks the raw file for the old flat
shape before `load_config`: the new schema would reject it with a message that
does not say the format changed. Per server, `default` / empty security settings
inherit `advanced` (the user cert and key as a pair) and `validate_security`
runs on the effective values, errors naming the server. An explicit None under a
secure default sets `downgrade_from`, a WARNING on every connect. `demo` replaces
`servers` with the one server `demo`, security None, no inheritance. `trace`
builds a one-entry config through the same path.

Names: `advanced.prefix` and server names go through `zelos_sdk.sanitize_name`
(kind `source`); a server name defaults to the endpoint host. Two servers with
one name are a hard error. Prefix set: one source, events `<server>/<event>`;
cleared: a source per server, events unprefixed.

### Actions

Free functions in `actions.py`, not client methods, bound to the runner via
`set_runner`. Every action that targets a server takes an optional `server`
(sanitized like the configured names): omitted is fine with one server and a
`ValueError` listing the names with several. `get_status` / `list_*` cover every
server when it is omitted.

Failure convention: **raise**. The actions protocol reads its verdict from a
raised exception, so `{"success": False}` would report a successful run. Input
errors are `ValueError`, self-describing exceptions propagate verbatim, and
anything else becomes a `RuntimeError` after `logger.exception`.

`auto_config` is the one standalone action (`standalone=True`, listed in the
packaged `actions.json`): it needs no client, so it runs its own `asyncio.run`,
which is safe because actions are called off the polling loop's thread. The
schema's root `ui:options.autoconfig` names it; keep the two in step.

Async work is dispatched with `OPCUARunner._run_coro`: `run_coroutine_threadsafe`
into the one polling loop, reusing its session. There is no connect-per-action
and no ad-hoc `asyncio.run` fallback - mutating client state from a foreign loop
silently lost every later sample - so a call with no loop running raises
`RuntimeError("extension is not running")`. A dispatch that times out cancels
the coroutine, so a late write cannot land after the action reported failure.

### Polling

One `read_attributes` request per `_read_chunk` nodes per cycle: the server's
MaxNodesPerRead (read once per connect with MaxNodesPerBrowse), capped at 100,
missing or 0 = 100. A server rejects the whole request past its limit (the `s7`
sim enforces 20). The health nodes (`HEALTH_NODES`, ns=0 `ua.ObjectIds`) lead
the first request, whose midpoint is the host time for `clock_skew_ms`; one that
is Bad or empty is dropped for the connection. Node handles are resolved once
per connection into `_poll_targets` and rebuilt after each reconnect, so the
poll path never calls `get_node`.

asyncua returns per-item `DataValue`s with their own `StatusCode` (verified on
2.0.1); it raises only for a service-level failure, or when it cannot parse one
item's value (a 2-D Variant array, some nested Variants), which fails the whole
response - `read_many` then re-reads that chunk item by item and returns the
culprit as BadDecodingError. A Bad item is skipped, as is one whose value fails
`decode_value` (a string arriving on a float32 node, an integer wider than its
field: abstract-typed nodes are typed by one sample), and `_log_node_failure`
guarantees **one ERROR per bad node per process** - an unbounded per-cycle warning
would flood both the log sink and the trace.

There is no per-cycle health-check read. Disconnection is inferred from poll
errors via `is_connection_error`, which classifies by type: `OSError` /
`TimeoutError`, `UaStatusCodeError` in the session / secure-channel / connection
family, and any other exception whose `__cause__` is a timeout or `OSError`
(the black-holed socket: asyncua 2.0.1 raises a bare `Exception` from the
`TimeoutError`). Never by message text. Five consecutive poll failures that
`is_connection_error` does *not* claim still force a reconnect, so an
unclassified error cannot wedge the extension on a dead session.

Reconnect backs off 3s, doubling, capped at 60s, reset on a completed poll (a
connect that never yields data does not clear it).

asyncua 2.0 starts a connection supervisor on every `connect()`: it reads
ServerStatus every `watchdog_intervall` with that interval as the timeout, and on
a miss marks the client disconnected (the next request raises `ConnectionError`,
which is ours to handle). `watchdog_intervall` is set to the request timeout, as
the 1s default would drop sessions to any slower server. Its `auto_reconnect`
is off by default and must stay off: reconnect and re-discovery are ours.

### Discovery

A server with no `node_map_file` (and `advanced.discovery` on) is browsed on
every connect, before `_resolve_nodes`, and `node_map` replaced. Walk: BFS over
forward HierarchicalReferences from Objects, `_browse_chunk` nodes per Browse
(View Timestamp null: asyncua defaults it to now, which .NET servers answer with
BadNodeNotInView),
BrowseNext to the end, visited set; not followed: Server (i=2253), Objects
named `_*`, HasProperty (EngineeringUnits / EURange are recorded). Variables are
browsed too (struct members, EU properties). Describe: one batched Read of
DataType, ValueRank, AccessLevel, DisplayName, Description + EU values; Value
only where the DataType is not builtin or the rank is Any/ScalarOrOneDimension.
Naming and collisions: `discovery.assign_names`. Unbounded by design: a limit
needs measured data.

A declared trace event's schema is fixed on its `TraceSource` instance (the SDK
rejects a re-add), but every construction is a new segment, and the app joins
sequential segments of one path. So a reconnect whose discovery adds fields to a
declared event rotates the source, at most once per reconnect: `flush()` the
old, construct one of the same name, replay every client's declared events
(`_fields`) on it, and switch every writer - all clients of a `SharedSource`
(the prefix source), else just this server's - with no await in between.
`SharedSource` holds the only reference to the source, so dropping it lets the
SDK end the old segment. A new event is just added; a removed field is just no
longer written; a field whose datatype changed is left out with one WARNING
(types are not reconciled across segments). The prefix source comes from
`init_global_source`, which the SDK keeps for the process, so its first segment
stops being written but is not ended. `opcua_log` is a separate source and never
rotates.

A node that `read_many` returns as BadDecodingError leaves `_poll_targets` until
the next reconnect, or its chunk is re-read item by item every cycle.

Measured against `--nodes` (in-process client, subprocess sim, localhost):
10k variables discover in 1.3s (606 requests), poll 139 ms/cycle (101 reads),
177 MB RSS; 50k in 6.8s (3010 requests), 734 ms/cycle (501 reads), 323 MB RSS
(baseline 122 MB). The asyncua sim dominates the poll time.

### Shutdown

`OPCUARunner` owns the one asyncio loop: every client polls in it as a task with
its own connect/backoff state, and all share one stop event. `_run_async`
installs SIGTERM/SIGINT through `loop.add_signal_handler`, which only sets that
event; each poll loop waits on it with its poll cadence. `signal.signal` +
`sys.exit` is deliberately avoided - it unwinds through the running loop and
drops the OPC-UA sessions without a clean close.

Connect is raced against the stop event in `_connect_or_stop` and cancelled when
stop wins: a black-holed endpoint parks `connect()` for minutes, far past the
manifest's 10s grace.

Each client's `finally` disconnect is wrapped in `asyncio.wait_for(..., 3.0)`;
they run concurrently, so shutdown is bounded at ~3s in total, not per server.
A client task that raises (a bug) sets the stop event so the rest close
cleanly. `runner.stop()` is thread-safe via `call_soon_threadsafe`.

### Node map

Map name, event names and node names are sanitized at load by
`zelos_sdk.sanitize_name` (event names keep `/`). Trace names are capped at 128
bytes including `<server>/`; discovery keeps an event's trailing segments.

Collisions after sanitization are hard `ValueError`s at load - duplicate event
name, or duplicate node name within one event; warn-and-clobber would silently
drop data. A name may repeat across events (a gateway's identical devices):
`get_by_name` takes `<event>/<name>` and raises on an ambiguous bare name.
The health event is `_server`: sanitized names never start with `_`.

### Security

A secure mode connects with exactly that mode and policy or not at all.
`_apply_security` reads GetEndpoints, refuses (listing the offerings) when the
pair is absent, then passes the endpoint's server certificate to `set_security`
explicitly - with `server_certificate=None` asyncua switches the client to a
None channel to fetch it. `strict` compares against the pin first for a readable
error; the real check is asyncua encrypting OPN to that certificate.

The generated client cert in `PKI_DIR` is reused, never rotated early: servers
trust by thumbprint. `application_uri` is read from the cert's SAN URI.

No secret values in config: user identity is Anonymous or an operator-issued
X.509 user cert (asyncua's `load_client_certificate` / `load_private_key` set the
USER identity; the app cert goes to `set_security`). A user cert needs a secure
mode and a Certificate token policy on the endpoint, checked before connect
because asyncua otherwise invents one.

### Simulator

asyncua has no hook for its per-connection `UaProcessor`, so `_SimServer.start`
re-implements `Server.start` (asyncua 2.0.1) to install `_SimProcessor`. It
records every request, and adds what asyncua lacks: enforced OperationLimits,
a session cap, RequestedMaxReferencesPerNode, BrowseNext, and
ServerDiagnosticsSummary session counts. On an asyncua bump recheck `start`,
`OPCUAProtocol.connection_made` and what `UaProcessor._process_message` does
around a request (`_browse` mirrors its session checks and activity stamps).
Each start gets a unique ApplicationUri (`auto_config` dedupes on it). `--nodes`
adds variables through one AddNodes call per folder with read-time value
callbacks: node-by-node creation cost ~1.3 ms each.

## Node ID Format

`ns=<namespace>;[s|i|g|b]=<identifier>`

- `ns=2;s=Temperature.Sensor1` string
- `ns=2;i=1001` numeric
- `ns=1;g=12345678-1234-5678-1234-567812345678` GUID (built as `uuid.UUID`)
- `ns=1;b=AQID` opaque, base64 (built as `bytes`)
- `nsu=urn:zelos:demo:plc;s=Motor.Speed` URI-qualified (`%3B`/`%25` escaped); resolved against the server NamespaceArray on each connect, unknown URI = one ERROR + node skipped

`node_map.parse_node_id` both parses and converts the identifier (int, `uuid.UUID`,
`bytes`), so a bad one is a `ValueError` at map load rather than a "Connection
failed" line from inside `connect()`. `client.parse_node_id_to_ua` only wraps its
result in a `NodeId` - one implementation, no drift.

## Code Style

- ruff, line length 100, google docstrings
- `contextlib.suppress(Exception)` over bare `try/except: pass`
- comments state the constraint, not the code
