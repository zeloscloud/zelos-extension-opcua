# CLAUDE.md

## Commands

```bash
just install      # deps + pre-commit hooks
just format       # ruff format + ruff check --fix
just check        # ruff check + ruff format --check
just test         # pytest (116 tests; integration tests start a real demo server)
just dev          # app mode, reads the Zelos App config
just demo         # built-in PLC simulator
```

## Key Files

| Path | Role |
|------|------|
| `main.py` | Click entry point; re-exports `ACTION_PREFIX`, installs `TraceLoggingHandler("opcua_log")` |
| `zelos_extension_opcua/__init__.py` | `ACTION_PREFIX = "OPC-UA"` - must match `name` in `extension.toml` |
| `zelos_extension_opcua/actions.py` | Free-function action surface + `register_actions()` |
| `zelos_extension_opcua/client.py` | Connection, batch polling, reconnect, shutdown |
| `zelos_extension_opcua/node_map.py` | Node/NodeMap parsing, name sanitization, collision rules |
| `zelos_extension_opcua/cli/app.py` | Config load, startup validation, `serve()` |
| `zelos_extension_opcua/demo/simulator.py` | Demo OPC-UA server |
| `zelos_extension_opcua/demo/plc_device.json` | Demo node map |
| `tests/test_opcua.py` | All tests |

## Architecture

### Startup order

`cli/app.serve()` is the only place that starts a client, and the order is load
config -> build client -> `set_client` -> `register_actions` -> `zelos_sdk.init(name=ACTION_PREFIX)`
-> `client.run()`. Registration must precede `init()`; actions registered after
the service is published may never be advertised.

Startup problems (bad config, missing or unparseable node map) are one
`logger.error` line plus `sys.exit(1)`. Tracebacks are for bugs.

### Actions

Free functions in `actions.py`, not client methods, bound to a single client via
`set_client`. The config describes one server, so there is no per-target selector
like CAN's `codec`.

Failure convention: **raise**. The actions protocol reads its verdict from a
raised exception, so `{"success": False}` would report a successful run. Input
errors are `ValueError`, self-describing exceptions propagate verbatim, and
anything else becomes a `RuntimeError` after `logger.exception`.

Async work is dispatched with `OPCUAClient._run_coro`: `run_coroutine_threadsafe`
into the live polling loop, reusing its session. There is no connect-per-action
and no ad-hoc `asyncio.run` fallback - mutating client state from a foreign loop
silently lost every later sample - so a call with no loop running raises
`RuntimeError("extension is not running")`. A dispatch that times out cancels
the coroutine, so a late write cannot land after the action reported failure.

### Polling

One `read_attributes` request per 100 nodes per cycle (servers cap
`MaxNodesPerRead`; chunking is cheaper than reading `OperationLimits` off every
server). Node handles are resolved once per connection into `_poll_targets` and
rebuilt after each reconnect, so the poll path never calls `get_node`.

asyncua returns per-item `DataValue`s with their own `StatusCode`; it raises only
for a service-level failure. A Bad item is skipped, as is one whose value fails
`decode_value` (a string arriving on a float32 node), and `_log_node_failure`
guarantees **one ERROR per bad node per process** - an unbounded per-cycle warning
would flood both the log sink and the trace.

There is no per-cycle health-check read. Disconnection is inferred from poll
errors via `is_connection_error`, which classifies by type: `OSError` /
`TimeoutError`, `UaStatusCodeError` in the session / secure-channel / connection
family, and a bare `UaError` whose `__cause__` is a timeout (the black-holed
socket case). Never by message text. Five consecutive poll failures that
`is_connection_error` does *not* claim still force a reconnect, so an
unclassified error cannot wedge the extension on a dead session.

Reconnect backs off 3s, doubling, capped at 60s, reset on a completed poll (a
connect that never yields data does not clear it).

### Shutdown

`_run_async` installs SIGTERM/SIGINT through `loop.add_signal_handler`, which only
sets an `asyncio.Event`; the poll loop waits on that event with the poll cadence.
`signal.signal` + `sys.exit` is deliberately avoided - it unwinds through the
running loop and drops the OPC-UA session without a clean close.

Connect is raced against the stop event in `_connect_or_stop` and cancelled when
stop wins: a black-holed endpoint parks `connect()` for minutes, far past the
manifest's 10s grace.

The `finally` disconnect is wrapped in `asyncio.wait_for(..., 3.0)`: bounded
cleanup, then give up. `stop()` is thread-safe via `call_soon_threadsafe`.

### Node map

Map name, event names and node names are sanitized at load (`. @ : ; = /` and
whitespace -> `_`). Hand-rolled because zelos-sdk 0.0.10 has no `sanitize_name`;
switch once the floor is >= 0.0.11.

Collisions after sanitization are hard `ValueError`s at load - duplicate event
name, or duplicate node name anywhere in the map. `get_by_name` must stay
unambiguous, and warn-and-clobber would silently drop data.

## Node ID Format

`ns=<namespace>;[s|i|g|b]=<identifier>`

- `ns=2;s=Temperature.Sensor1` string
- `ns=2;i=1001` numeric
- `ns=1;g=12345678-1234-5678-1234-567812345678` GUID (built as `uuid.UUID`)
- `ns=1;b=AQID` opaque, base64 (built as `bytes`)

`node_map.parse_node_id` both parses and converts the identifier (int, `uuid.UUID`,
`bytes`), so a bad one is a `ValueError` at map load rather than a "Connection
failed" line from inside `connect()`. `client.parse_node_id_to_ua` only wraps its
result in a `NodeId` - one implementation, no drift.

## Code Style

- ruff, line length 100, google docstrings
- `contextlib.suppress(Exception)` over bare `try/except: pass`
- comments state the constraint, not the code
