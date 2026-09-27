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
| `main.py` | Click entry point; re-exports `ACTION_PREFIX` |
| `zelos_extension_opcua/__init__.py` | `ACTION_PREFIX = "OPC-UA"` - must match `name` in `extension.toml` |
| `zelos_extension_opcua/actions.py` | Free-function action surface + `register_actions()` |
| `zelos_extension_opcua/client.py` | `OPCUAClient` (one server: connection, subscriptions, batch polling, reconnect) and `OPCUARunner` (the loop, signals, shutdown, action dispatch) |
| `zelos_extension_opcua/node_map.py` | Node/NodeMap parsing, name sanitization, collision rules, json/csv export |
| `zelos_extension_opcua/diagnostics.py` | Import-time asyncua array-length guard, `SERVER` contextvar for its WARNING, loop stall watchdog (`faulthandler` to stderr), peak RSS WARNING; tests in `tests/test_diagnostics.py` |
| `zelos_extension_opcua/discovery.py` | Live discovery (walk, describe, name), OperationLimits, `HEALTH_EVENT` |
| `zelos_extension_opcua/autoconfig.py` | `auto_config` sources: localhost ports, LDS, mDNS |
| `zelos_extension_opcua/cli/app.py` | Config load, `servers[]` / `advanced` resolution, startup validation, `serve()` |
| `zelos_extension_opcua/demo/simulator.py` | Demo OPC-UA server |
| `zelos_extension_opcua/demo/plc_device.json` | Demo node map |
| `zelos_extension_opcua/demo/sim_server.py` | `demo-server` simulator: profiles, security, ns shift, request log, enforced limits and caps |
| `zelos_extension_opcua/demo/profiles.py` | gateway / s7 / device address spaces, `--map` serving |
| `tests/test_opcua.py` | Extension tests |
| `tests/test_sim.py` | Simulator profile and flag tests |
| `tests/test_security.py` | Secure sessions, cert trust and pinning, config errors, per-server security inheritance |
| `tests/test_transport.py` | Subscriptions and source timestamps, refusal fallback on the `s7` caps, staleness refresh, `poll` transport |
| `tests/test_servers.py` | Several servers in one runner: layout, isolation, recovery, action selection |
| `tests/test_discovery.py` | Discovery per profile, limits, health, `discovered_map`, naming, `auto_config` |
| `tests/test_interop.py` | Microsoft OPC PLC (.NET stack) in Docker: None, SignAndEncrypt, user cert, `auto_config`; `ZELOS_INTEROP=1` only |

## Architecture

### Startup

- `cli/app.serve()` is the only place that starts clients: config -> clients -> `set_runner` -> `register_actions` -> `open_sources(prefix)` -> `zelos_sdk.init(name=ACTION_PREFIX)` -> `client.start` -> `runner.run()`.
- Logs: `open_sources` installs the trace handler (INFO+) on the prefix source's `log` event (server name `log` is a config error); cleared prefix, own `opcua_log` source. Earlier records reach stderr only.
- Registration must precede `init()` (later actions may never be advertised); the prefix source must exist before `init()` so it is reused, not duplicated.
- Startup problems (bad config, missing/unparseable node map) are one `logger.error` + `sys.exit(1)`; tracebacks are for bugs.

### Config

- `run_app_mode` checks the raw file for the old flat shape before `load_config`: the schema error would not say the format changed.
- Per server, `default` / empty security inherits `advanced` (user cert + key as a pair); `validate_security` runs on effective values. Explicit None under a secure default sets `downgrade_from` (WARNING per connect).
- `demo` replaces `servers` with one server `demo`, security None, no inheritance; `trace` builds a one-entry config through the same path.
- Prefix and server names go through `sanitize_name(kind="source")`; duplicate server names are a hard error.

### Actions

- Free functions in `actions.py`, bound via `set_runner`. Optional `server`: `ValueError` listing names when omitted with several; `get_status` / `list_*` cover all.
- Failure convention: **raise**. The protocol reads the verdict from the exception, so `{"success": False}` reports success. Input errors `ValueError`; unknown errors become `RuntimeError` after `logger.exception`.
- `auto_config` is the one standalone action (own `asyncio.run`, safe off the loop thread); the schema's `ui:options.autoconfig` names it, keep in step.
- Everything else dispatches via `OPCUARunner._run_coro` into the polling loop. No connect-per-action, no ad-hoc `asyncio.run` fallback (mutating client state from a foreign loop silently lost every later sample); no loop = `RuntimeError("extension is not running")`.
- A dispatch timeout cancels the coroutine, stopping an unsent write. After a Write is `sent` it cannot be recalled: raise a TimeoutError saying the server may have applied it.

### Transport

- `_start_transport` runs at the end of every `connect()`, after discovery, rotation and `_resolve_nodes`.
- Subscriptions: QueueSize 1, DataChangeFilter StatusValue, TimestampsToReturn Both; CreateMonitoredItems chunked by MaxMonitoredItemsPerCall (cap 100). Refusals move items to polling; `SERVER_FULL_CODES` short-circuit later items (S7-1200 caps ~1000 items, 5 subscriptions). An empty subscription is deleted (it holds a slot).
- asyncua's high-level `Subscription` is bypassed: `_publish_callback` closes over the connection's handle map, so a late response from an old session cannot resolve a new handle. asyncua awaits it before the next Publish (backpressure).
- asyncua drops a whole PublishResponse over one undecodable value without naming the item: `_guard_publish` deletes subscriptions and polls everything, where `read_many` isolates it. BaseDataType discovered nodes (`Discovery.variant`) are polled, never subscribed (OPC PLC random Variants trip the guard within seconds).
- Polling: `_schedule` builds `_Job`s run one at a time (late job restarts from now, no burst): health Read, one job per `_read_chunk` phased across the interval, staleness sweep. Never smaller chunks: server cost follows request count. `_read_chunk` = MaxNodesPerRead capped at 100 (missing/0 = 100); servers reject a whole over-limit request.
- Sweep: `_stale` OrderedDict in last-update order (due items are the front, no scan); step = `min_update_interval / max(SWEEP_STEPS, chunks)`, staleness bounded at 1.25x.
- Timestamps: `sample_time_ns` via `log_at`; `_log_samples` groups by (event, time). No skew correction.
- A value that fails decoding (asyncua raises for the whole response) makes `read_many` re-read the chunk item by item, returning BadDecodingError; that node leaves polling until reconnect. `_log_node_failure`: **one ERROR per bad node per process**.
- Measured on OPC PLC (10k nodes/s): server CPU idle 1.8%, one subscription 5.2%, 100-node Reads 7.5%, 25-node Reads 21.4%. Don't tune against the in-process sim (it does monitored-item work inside each write).

### Connection

- No separate liveness probe: the health Read is one (midpoint = host time for `clock_skew_ms`; a Bad/empty health node is dropped for the connection).
- `is_connection_error` classifies by type, never message text: `OSError` / `TimeoutError`, session/channel/connection `UaStatusCodeError`, or `__cause__` timeout/`OSError` (asyncua 2.0.1 raises bare `Exception` on a black-holed socket). Five unclassified consecutive failures still force a reconnect.
- Backoff 3s doubling to 60s, reset on a completed request (not on connect).
- asyncua 2.0 supervisor: `watchdog_intervall` = request timeout (the 1s default drops slow servers); `auto_reconnect` must stay off, reconnect and re-discovery are ours. A Bad StatusChangeNotification (incl. supervisor BadShutdown) marks disconnected.

### Discovery

- Browse View Timestamp must be null: asyncua defaults it to now, which .NET servers answer with BadNodeNotInView.
- BadNoContinuationPoints nodes are re-browsed alone after the batch (one point held at a time; `s7` sim holds 3).
- Typing (`field_datatype`): concrete builtin exactly; Number/Integer/UInteger widest; BaseDataType always string via `render_text`, so a type change never fails the node.
- Collisions (`assign_names`): every collider gets `_<hash>` of its nsu= id, never an ordinal, so a name never re-points. Unbounded by design: a limit needs measured data.
- Source rotation: a declared event's schema is fixed per `TraceSource` instance, each construction is a new segment, the app joins them. Added fields rotate at most once per reconnect: `flush()`, construct same name, replay every client's `_fields`, switch every `SharedSource` writer, no await in between. `SharedSource` holds the only reference. A changed datatype is skipped (types not reconciled across segments). The log handler moves with it (under the handler lock). `opcua_log` never rotates.
- Measured (`--nodes`, localhost): 10k vars discover 1.3s, poll 139 ms/cycle, 177 MB; 50k 6.8s, 734 ms/cycle, 323 MB (baseline 122 MB).

### Shutdown

- `OPCUARunner` owns the one loop; each client is a task, all share one stop event. Signals via `loop.add_signal_handler` only set it; never `signal.signal` + `sys.exit` (drops sessions uncleanly).
- `_run_async` races connect against stop: a black-holed `connect()` parks for minutes, past the manifest's 10s grace.
- Disconnects run concurrently under `wait_for(..., 3.0)`: ~3s total. A crashing client task sets stop. `runner.stop()` is thread-safe.

### Node map

- Names via `sanitize_name` (event names keep `/`); trace names capped at 128 bytes including `<server>/` (discovery keeps trailing segments).
- Duplicates after sanitization are hard `ValueError`s (warn-and-clobber drops data). Repeats across events are allowed; `get_by_name` takes `<event>/<name>` and raises on an ambiguous bare name.
- Health event `_server` cannot collide: sanitized names never start with `_`.

### Security

- `_apply_security` reads GetEndpoints, refuses a missing mode/policy pair, then passes the server certificate explicitly: with `server_certificate=None` asyncua silently switches to a None channel. `strict` compares the pin first for a readable error; the real check is OPN encryption.
- Generated client cert in `PKI_DIR` is never rotated early (servers trust by thumbprint); `application_uri` comes from its SAN.
- No secrets in config. User cert: `load_client_certificate` / `load_private_key` set the USER identity (app cert goes to `set_security`). Needs a secure mode and a Certificate token policy, checked before connect because asyncua otherwise invents one.

### Simulator

- asyncua has no `UaProcessor` hook, so `_SimServer.start` re-implements `Server.start` (asyncua 2.0.1) to install `_SimProcessor`, which adds enforced limits, session/subscription/item/continuation caps, BrowseNext, ServerTimestamp on Read, diagnostics counts.
- On an asyncua bump recheck `Server.start`, `OPCUAProtocol.connection_made`, and `UaProcessor._process_message` around a request (`_browse` mirrors its session checks and activity stamps).
- Unique ApplicationUri per start (`auto_config` dedupes on it). `--nodes` uses one AddNodes per folder with read-time callbacks (node-by-node costs ~1.3 ms each).

## Node ID Format

`ns=<n>;[s|i|g|b]=<id>` (string, numeric, GUID as `uuid.UUID`, base64 opaque as `bytes`), or `nsu=<uri>;...` (`%3B`/`%25` escaped) resolved against the NamespaceArray on each connect (unknown URI = one ERROR, node skipped).

`node_map.parse_node_id` parses and converts, so a bad id fails at map load, not inside `connect()`; `client.parse_node_id_to_ua` only wraps it.

## Code Style

- ruff, line length 100, google docstrings
- `contextlib.suppress(Exception)` over bare `try/except: pass`
- comments state the constraint, not the code
