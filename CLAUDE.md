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
| `tests/test_security.py` | Secure sessions, client and server cert trust, pinning, validity/ApplicationUri checks, config errors, per-server security inheritance |
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
- `auto_config(config)`: the app passes the live form; none = saved config; no servers = probe localhost/LDS/mDNS. Keeps entries as entered; fills `default` security only when the server lacks the form's Advanced one.
- Standalone: `auto_config` (own `asyncio.run`, safe off the loop thread; the schema's `ui:options.autoconfig` names it, keep in step) and the server certificate trust actions.
- Everything else dispatches via `OPCUARunner._run_coro` into the polling loop. No connect-per-action, no ad-hoc `asyncio.run` fallback (mutating client state from a foreign loop silently lost every later sample); no loop = `RuntimeError("extension is not running")`.
- A dispatch timeout cancels the coroutine, stopping an unsent write. After a Write is `sent` it cannot be recalled: raise a TimeoutError saying the server may have applied it.

### Transport

- `_start_transport` runs at the end of every `connect()`, after discovery, rotation and `_resolve_nodes`.
- Subscriptions: QueueSize 1, DataChangeFilter StatusValue, TimestampsToReturn Both; CreateMonitoredItems chunked by MaxMonitoredItemsPerCall (cap 100). Refusals move items to polling; `SERVER_FULL_CODES` short-circuit later items (S7-1200 caps ~1000 items, 5 subscriptions). An empty subscription is deleted (it holds a slot).
- asyncua's high-level `Subscription` is bypassed: `_publish_callback` closes over the connection's handle map, so a late response from an old session cannot resolve a new handle. asyncua awaits it before the next Publish (backpressure).
- asyncua drops a whole PublishResponse over one undecodable value without naming the item: `_guard_publish` deletes subscriptions and polls everything, where `read_many` isolates it. BaseDataType discovered nodes (`Discovery.variant`) are polled, never subscribed (OPC PLC random Variants trip the guard within seconds).
- Polling: `_schedule` builds `_Job`s run one at a time (late job restarts from now, no burst): health Read, one job per `_read_chunk` phased across the interval, staleness sweep. Never smaller chunks: server cost follows request count. `_read_chunk` = MaxNodesPerRead capped at 100 (missing/0 = 100); servers reject a whole over-limit request.
- Read MaxAge (ms): poll chunks their interval, sweep `min_update_interval`, everything else 0 (health, discovery, actions, the Read after a recreate).
- Sweep: `_stale` OrderedDict in last-update order (due items are the front, no scan); step = `min_update_interval / max(SWEEP_STEPS, chunks)`, staleness bounded at 1.25x.
- Timestamps: `sample_time_ns` via `log_at`; `_log_samples` groups by (event, time). No skew correction.
- A value that fails decoding (asyncua raises for the whole response) makes `read_many` re-read the chunk item by item, returning BadDecodingError; that node leaves polling until reconnect. `_log_node_failure`: **one ERROR per bad node per connection**. Uncertain is traced (`_decode`), one INFO per node per connection; health and discovery take it too (`value_of`).
- Measured on OPC PLC (10k nodes/s): server CPU idle 1.8%, one subscription 5.2%, 100-node Reads 7.5%, 25-node Reads 21.4%. Don't tune against the in-process sim (it does monitored-item work inside each write).

### Connection

- No separate liveness probe: the health Read is one (midpoint = host time for `clock_skew_ms`; a Bad/empty health node is dropped for the connection).
- `is_connection_error` classifies by type, never message text: `OSError` / `TimeoutError`, session/channel/connection `UaStatusCodeError`, or `__cause__` timeout/`OSError` (asyncua 2.0.1 raises bare `Exception` on a black-holed socket). Five unclassified consecutive failures still force a reconnect.
- Backoff 3s doubling to 60s, reset on a completed request (not on connect).
- Sessions: 120 s requested (`SESSION_TIMEOUT_MS`). One asyncua `Client` per connection; a lost connection `_detach`es the session (socket aborted, nothing sent: a black-holed link may still carry a CloseSession) and `_resume_on_open` re-activates it on the next channel, in asyncua's `_try_resume_persisted_session` slot (after OpenSecureChannel, before CreateSession). Kept: subscriptions (lifetime >= the session timeout), handles, jobs; `_rebind` points them at the new client and restarts Publish; the gap is Republished; one INFO. `_ready` is false from a new session or a rebuild until `_start_transport` completes, and during a recreate: a resumed session that is not ready is rebuilt (a link lost mid-setup). `SESSION_GONE_CODES` or a refusal: one INFO, fresh session, full rebuild. A channel failure keeps it detached for the next attempt.
- Discovery re-browses on every connect; on a resumed session a changed node map, variant set or NamespaceArray deletes the old subscriptions and rebuilds, otherwise nothing is re-created. Targets hold NodeIds, not asyncua Nodes (bound to one client); `_ua_node` binds per call.
- A deliberate reconnect (5 unclassified failures, a refused recreate) sets `_fresh`: CloseSession on the old channel, or if dead activated and closed on the next (Part 4: CloseSession only on the session's channel).
- asyncua privates relied on, recheck on a bump: `_server_nonce`, `_policy_ids`, `_try_resume_persisted_session`, `_start_renew_loop`, `session._subscription_callbacks`. On a closed channel `_guard_publish` parks the Publish loop until the client is torn down (asyncua logs a traceback per second otherwise).
- Subscriptions: revised values logged once per subscription (`revisions`); `_stale_after` = max(min_update_interval, revised publishing interval).
- Recovery (`_Sub` per subscription): `on_publish` checks sequence numbers (a keep-alive carries the next, a notification its own; lower resyncs) and Republishes each missing one the PublishResult's AvailableSequenceNumbers lists (at most `MAX_REPUBLISH`) inside asyncua's publish loop (it awaits a coroutine callback), acking recovered ones via the `_guard_publish` wrapper. A Republish cut by the link leaves the message unacked and `expected` at the gap: the resumed session Republishes it. `_watch` (job, every 1 s, no request) recreates a stalled one: silent past publishing x (keep-alive count + 1) + 1 s (.NET client margin), a gap left open, or a server StatusChange outside `LINK_LOST_CODES` (e.g. BadTimeout). Recreate: raw DeleteSubscriptions (asyncua's WARNs on an already-gone id) + `_subscribe` + one Read; any refusal reconnects. Logs: one WARNING per lost link (a StatusChange for every subscription is logged once); asyncua's "connection is closed" WARNINGs on teardown are filtered.
- asyncua's server answers Republish of an unknown message Good + empty (`not returned`). `Simulator.drop_notifications` / `retain_dropped` lose Publish responses (client sees BadTimeout); `delete_subscriptions()` drops them server-side.
- Writes: DataValue with Value only (`StatusCode=None`); asyncua's `write_value` adds StatusCode and SourceTimestamp.
- Endpoint userinfo (`user:pw@`) is refused (`has_userinfo`) at config and in `auto_config`: asyncua turns it into a UserName login. Never echo such an endpoint.
- asyncua 2.0 supervisor: `watchdog_intervall` = request timeout (the 1s default drops slow servers); `auto_reconnect` must stay off, reconnect and re-discovery are ours. A Bad StatusChangeNotification in `LINK_LOST_CODES` (incl. supervisor BadShutdown) marks disconnected; others recreate that subscription.

### Discovery

- Browse View Timestamp must be null: asyncua defaults it to now, which .NET servers answer with BadNodeNotInView.
- Filters (`PathFilter`): globs over the sanitized browse path, not the trace name: they differ on vendor-id servers, and only the browse path lets `walk` prune exactly (a branch no include can reach, or matching an exclude, is not browsed). An include matching a branch takes its subtree. A filtered node is not marked visited: another parent may take it.
- BadNoContinuationPoints nodes are re-browsed alone after the batch (one point held at a time; `s7` sim holds 3).
- Typing (`field_datatype`): concrete builtin exactly; vendor subtypes of builtin integers as that integer (`integer_subtypes`, inverse HasSubtype, one Browse per level); Number/Integer/UInteger widest; BaseDataType always string via `render_text`, so a type change never fails the node.
- Collisions (`assign_names`): every collider gets `_<hash>` of its nsu= id, never an ordinal, so a name never re-points. Unbounded by design: a limit needs measured data.
- Source rotation: a declared event's schema is fixed per `TraceSource` instance, each construction is a new segment, the app joins them. Added fields rotate at most once per reconnect: `flush()`, construct same name, replay every client's `_fields`, switch every `SharedSource` writer, no await in between. `SharedSource` holds the only reference. A changed datatype is skipped (types not reconciled across segments). The log handler moves with it (under the handler lock). `opcua_log` never rotates.
- Measured (`--nodes`, localhost): 10k vars discover 1.3s, poll 139 ms/cycle, 177 MB; 50k 6.8s, 734 ms/cycle, 323 MB (baseline 122 MB).

### Shutdown

- `OPCUARunner` owns the one loop; each client is a task, all share one stop event. Signals via `loop.add_signal_handler` only set it; never `signal.signal` + `sys.exit` (drops sessions uncleanly).
- First contact is required: the runner waits for every client's first connect; any failure logs one ERROR per server and `serve()` exits 1. After a server has connected, drops use the backoff. `mark_unreachable` wraps asyncua's socket open so "never opened" (refused, no route, DNS, timeout) is typed, not guessed from a later timeout.
- `_run_async` races connect against stop: a black-holed `connect()` parks for minutes, past the manifest's 10s grace.
- Disconnects run concurrently under `wait_for(..., 3.0)`: ~3s total. A crashing client task sets stop. `runner.stop()` is thread-safe.

### Node map

- Names via `sanitize_name` (event names keep `/`); trace names capped at 128 bytes including `<server>/` (discovery keeps trailing segments).
- Duplicates after sanitization are hard `ValueError`s (warn-and-clobber drops data). Repeats across events are allowed; `get_by_name` takes `<event>/<name>` and raises on an ambiguous bare name.
- Health event `_server` cannot collide: sanitized names never start with `_`.

### Security

- `_apply_security` reads GetEndpoints, refuses a missing mode/policy pair, then passes the server certificate explicitly: with `server_certificate=None` asyncua silently switches to a None channel. The certificate checked is the one OPN is encrypted to, so a substitute cannot complete the handshake.
- Server certificate, before any session: first `strict` pin or `trust_list` (DER match against any file in `PKI_DIR/trusted`, read per connect); a miss is written to `rejected/<SHA-1>.der`, even if expired, and raised naming every problem, so one fix suffices. Then validity (`allow_expired_server_certificate` turns the refusal into a WARNING). ApplicationUri mismatch on a trusted certificate: WARNING (GetEndpoints is unauthenticated; the exact DER is trusted). `certificate_problems` compares directly, no asyncua validator. No chain/CRL check.
- `auto` (v0.1.1) is accepted as `trust_list`: kept in the schema enum (`ui:enumDisabled`) so saved configs validate.
- `ConnectionSecurityError.fix` / `OPCUAClient.fix`: what the user does, appended to the ERROR (at start: ", then start the extension again").
- `trust_server_certificate` (explicit thumbprint; missing or unknown lists the rejected, as other OPC UA tools require a selection) / `list_server_certificates` are standalone (files only): at start an untrusted server stops the extension.
- Generated client cert in `PKI_DIR` is never rotated early (servers trust by thumbprint); `application_uri` comes from its SAN.
- No secrets in config. User cert: `load_client_certificate` / `load_private_key` set the USER identity (app cert goes to `set_security`). Needs a secure mode and a Certificate token policy, checked before connect because asyncua otherwise invents one.

### Simulator

- asyncua has no `UaProcessor` hook, so `_SimServer.start` re-implements `Server.start` (asyncua 2.0.1) to install `_SimProcessor`, which adds enforced limits, session/subscription/item/continuation caps, BrowseNext, ServerTimestamp on Read, diagnostics counts.
- Session cap counts `iserver._external_sessions` (a lost connection's session with subscriptions stays until timeout, as on a PLC). On an asyncua bump recheck `Server.start`, `OPCUAProtocol.connection_made`, and `UaProcessor._process_message` around a request (`_browse` mirrors its session checks and activity stamps).
- Unique ApplicationUri and server certificate per start (`auto_config` dedupes on the URI); `Simulator(certificate=, application_uri=)` fixes them for certificate tests. `--nodes` uses one AddNodes per folder with read-time callbacks (node-by-node costs ~1.3 ms each).

## Node ID Format

`ns=<n>;[s|i|g|b]=<id>` (string, numeric, GUID as `uuid.UUID`, base64 opaque as `bytes`), or `nsu=<uri>;...` (`%3B`/`%25` escaped) resolved against the NamespaceArray on each connect (unknown URI = one ERROR, node skipped).

`node_map.parse_node_id` parses and converts, so a bad id fails at map load, not inside `connect()`; `client.parse_node_id_to_ua` only wraps it.

## Code Style

- ruff, line length 100, google docstrings
- `contextlib.suppress(Exception)` over bare `try/except: pass`
- comments state the constraint, not the code
