# Zelos OPC-UA Extension

Traces OPC-UA servers into Zelos trace events, by subscription or polling, and exposes read/write/browse as Zelos actions.

## Features

| Feature | Detail |
|---------|--------|
| Discovery | A server without a node map is browsed on every connect and every scalar variable traced (read-only) |
| Subscriptions | One per distinct interval; what the server refuses is polled instead, with one WARNING |
| Batch polling | min(MaxNodesPerRead, 100) nodes per Read, the Reads of one interval spread across it; a bad node is reported once and skipped |
| Source timestamps | Every sample is logged at its SourceTimestamp, else ServerTimestamp, else receipt |
| Server health | Event `server` per server: state, clock skew, service level, session and rejected-request counts |
| Auto-configure | The config form's button finds local and mDNS-announced servers and picks their security |
| Reconnection | Capped exponential backoff (3s, doubling, 60s ceiling), reset on a completed request |
| Node mapping | JSON file groups nodes into trace events and fields |
| Data types | bool, int8-64, uint8-64, float32/64, string |
| Security | None / Sign / SignAndEncrypt, with Basic256Sha256, Aes128Sha256RsaOaep, Aes256Sha256RsaPss |
| Multiple servers | Any number of servers in one extension; one unreachable server never stalls the others |
| Actions | Read, write, list and browse, live against the tracing connection |
| Demo mode | Built-in PLC simulator, no hardware |


## Quick Start

```bash
just install                  # dependencies + pre-commit hooks
uv run main.py demo           # simulated PLC
uv run main.py trace opc.tcp://192.168.1.100:4840 nodes.json
```

## Configuration

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `demo` | boolean | `false` | Run the built-in PLC simulator as the only server, `demo` |
| `servers[]` | array | `[]` | One entry per server; at least one unless `demo` |
| `servers[].name` | string | endpoint host | Trace and action name (`plc01`, `192_168_1_10`); must be unique |
| `servers[].endpoint` | string | `opc.tcp://localhost:4840` | Server endpoint URL |
| `servers[].node_map_file` | string | `""` | Path to the JSON node map; empty discovers the address space |
| `servers[].poll_interval` | number | `1.0` | Seconds: subscription sampling/publishing interval, poll period, `_server` period |
| `servers[].transport` | string | `default` | `default` (inherit), `subscription`, `poll` |
| `servers[].min_update_interval` | number \| null | `null` | Seconds; empty inherits the advanced value |
| `servers[].security_mode` | string | `default` | `default` (inherit), None, Sign, SignAndEncrypt |
| `servers[].security_policy` | string | `default` | `default` (inherit), None, Basic256Sha256, Aes128Sha256RsaOaep, Aes256Sha256RsaPss |
| `servers[].user_certificate_file` | string | `""` | User certificate; empty inherits the advanced pair |
| `servers[].user_private_key_file` | string | `""` | Key for this server's user certificate |
| `servers[].server_certificate` | string | `auto` | `auto` accepts the server's certificate; `strict` only `server_certificate_file` |
| `servers[].server_certificate_file` | string | `""` | Pinned server certificate (DER/PEM) for `strict` |
| `advanced.prefix` | string | `OPC-UA` | Trace source every server publishes under; clear for one source per server |
| `advanced.timeout` | number | `5.0` | Request timeout in seconds |
| `advanced.log_level` | string | `INFO` | Logging verbosity; an unknown value falls back to INFO |
| `advanced.discovery` | boolean | `true` | Browse servers without a node map; off, such a server polls only its health |
| `advanced.transport` | string | `subscription` | `subscription`: server-pushed changes, polling what is refused; `poll`: batched Reads only |
| `advanced.min_update_interval` | number | `60` | Seconds a subscribed node may stay silent before it is re-read |
| `advanced.certificate_file` | string | `""` | Client certificate (DER/PEM), shared by every server; empty generates one |
| `advanced.private_key_file` | string | `""` | Unencrypted client key (DER/PEM); set with `certificate_file` |
| `advanced.security_mode` | string | `None` | Default for servers set to `default` |
| `advanced.security_policy` | string | `None` | Default for servers set to `default` |
| `advanced.user_certificate_file` | string | `""` | Default X.509 user certificate (DER/PEM); empty for Anonymous; needs Sign or SignAndEncrypt |
| `advanced.user_private_key_file` | string | `""` | Unencrypted user key (DER/PEM); set with `user_certificate_file` |

Trace layout: one source `OPC-UA` with events `<server>/<event>` (e.g. `OPC-UA/plc01/temperature`); with `advanced.prefix` cleared, each server is its own source with unprefixed events (`plc01/temperature`). Logs stay in `opcua_log`.
Security is validated per server on its effective settings; a server set to None under a secure default connects, with a WARNING on every connect. A config in the pre-`servers[]` flat format is a startup error.

### Secure connections

With Sign or SignAndEncrypt and no certificate configured, a client certificate is generated in the extension data directory (`$ZELOS_DATA_DIR/pki/`; `~/.zelos/opcua/pki/` for CLI runs) on first connect and reused; its path, SHA-1 thumbprint and expiry are logged. Trust it on the server. It is valid for 2 years; a WARNING starts 30 days before expiry, and an expired one is regenerated and must be trusted again.
`strict` refuses any server certificate but the pinned one. A server that does not offer the requested mode and policy is refused with its offerings logged; the extension never falls back to None.
User login is by X.509 certificate issued by the server admin; username/password is not supported, by design: no secret is stored in config. A server without a Certificate user token policy is refused with the token types it offers; the extension never falls back to Anonymous. A config that still sets `username` or `password` is a startup error.

A configured `node_map_file` that is missing or unparseable is a startup error: the extension logs one line and exits, rather than running healthy while recording nothing.

### Discovery

A server without a `node_map_file` is browsed on every connect (Browse, BrowseNext and Read only; nothing written to disk), so program changes appear after a reconnect. The walk follows forward hierarchical references from `Objects`, skipping the `Server` object, Objects whose BrowseName starts with `_` (Kepware `_System`, `_Statistics`, ...) and properties. Every scalar variable of a supported type is traced, read-only; arrays, structs and other types are skipped and counted in one INFO line per connect. A node refused BadNoContinuationPoints (a server holding few continuation points) is browsed again on its own; one the server cannot browse at all is skipped with one WARNING per connect naming it.

Names: the event is the parent path below `Objects` (`Line1/Motor`), the field the BrowseName. Vendor string ids name the event instead when their last segment is the BrowseName: Kepware `Channel.Device.Tag` and TwinCAT `MAIN.var` (dots), Siemens `"DB"."tag"`, CODESYS `|var|<device>.Application.PLC_PRG.x`, B&R `::Task:Var`. Two variables with one event and field keep the first by node id; later ones get `_<alias>` (another namespace: the URI's last segment, `http://opcfoundation.org/UA/DI/` -> `DI`, stable across restarts) or `_2`, `_3`, with one WARNING per connect. An event added by a reconnect is traced; a field added to an existing event starts a new segment of the trace source (one INFO line; the app joins sequential segments). A field whose datatype changed is skipped until restart, with one WARNING.

`discovered_map` returns the discovered set as a node map (json, `nsu=` ids, `writable: false`) or csv; save the json as a `node_map_file` to pin or edit it.

### Transport and timestamps

`subscription` (default): per server, one subscription per distinct interval (an event's own `poll_interval`, else the server's), sampling = publishing = interval, queue size 1, reporting a change of value or status (no deadband). Items the server refuses (a per-item Bad status such as BadTooManyMonitoredItems or BadNodeIdUnknown, a refused subscription such as BadTooManySubscriptions, an overload) are polled for the rest of the connection, with one WARNING per connect naming the counts. A Publish response the client cannot decode moves every item to polling for the connection, with one WARNING: polling isolates the undecodable node, a Publish cannot. Subscriptions are rebuilt on every connect, after discovery.

`poll`: every node is read in batched Reads (min(MaxNodesPerRead, 100) nodes each) every interval; the Reads of one interval are spread evenly across it. Server cost follows the request count, so batches are never smaller.

A subscription reports changes only. A subscribed node silent for `min_update_interval` (a static value, or a server that stopped reporting it) is re-read in batched Reads; worst case, every node static, ceil(nodes / 100) Reads per `min_update_interval`. The refresh is logged at the Read's ServerTimestamp: the value is confirmed current then.

Every sample is logged at its SourceTimestamp, else ServerTimestamp, else the time it was received. No clock-skew correction is applied; `_server.clock_skew_ms` shows it. Fields of one event with different timestamps are separate rows, so a subscription's rows hold only the fields that changed at that time.

### Server health

Every server logs event `_server` at its poll interval, from one small Read, at host time: `state` / `state_name` (ServerStatus.State), `current_time`, `clock_skew_ms` (server CurrentTime minus host time at the read's midpoint), `start_time`, `service_level`, and from ServerDiagnosticsSummary `current_session_count`, `cumulated_session_count`, `rejected_requests_count`, `security_rejected_requests_count`, `current_subscription_count`. A field the server does not publish (diagnostics off) is dropped for the connection.

### Auto-configure

The config form's Auto-configure button runs `auto_config` with the extension stopped. It replaces `servers` only, so Advanced survives.

| Source | What |
|---|---|
| localhost ports | GetEndpoints on 4840, 4841, 48010, 49320 (Kepware), 62541, 53530 (Prosys), 2s each, in parallel |
| Local Discovery Server | FindServers on `opc.tcp://localhost:4840` |
| mDNS | 2s passive browse for `_opcua-tcp._tcp.local.` |

Servers are deduplicated by ApplicationUri and named after their ApplicationName. Security is None when offered, else the strongest supported policy with SignAndEncrypt (then Sign); the generated client certificate must then be trusted on the server. No subnet sweep, no other ports.

## Node Map Format

```json
{
  "name": "my_device",
  "events": {
    "temperature": [
      {"name": "sensor1", "node_id": "ns=2;s=Temperature.Sensor1", "datatype": "float32", "unit": "°C"},
      {"name": "sensor2", "node_id": "ns=2;i=1001", "datatype": "float32", "unit": "°C"}
    ],
    "status": {
      "poll_interval": 0.2,
      "nodes": [
        {"name": "running", "node_id": "ns=2;s=Status.Running", "datatype": "bool", "writable": true}
      ]
    }
  }
}
```

`name` is the trace source, event keys are trace events, and node names are fields within them. An event is a node list, or an object with `nodes` and an optional `poll_interval` (seconds, at least 0.1) overriding the server's for that event.

### Node Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `node_id` | Yes | - | `ns=<n>;[s\|i\|g\|b]=<identifier>`, or `nsu=<uri>;...` to pin the namespace by URI (stable across server restarts; `;` in the URI as `%3B`) |
| `name` | Yes | - | Field name in the Zelos event |
| `datatype` | No | `float32` | bool, uint8-64, int8-64, float32, float64, string |
| `unit` | No | `""` | Unit string for display |
| `scale` | No | `1.0` | Read value x scale; write value / scale |
| `writable` | No | `null` | `null` auto-detects from AccessLevel |

### Naming Rules

Map, event and node names are sanitized at load: `. @ : ; =` and whitespace become `_` (and `/` in node names), because those characters are path separators in Zelos trace names. After sanitization, a duplicate event name, or a duplicate node name within one event, is a hard error. A node name in several events is addressed as `<event>/<name>` in `read_named_node` / `write_named_node`.

## Actions

Registered under the `OPC-UA/` prefix (the extension's name in `extension.toml`).

| Action | Description |
|--------|-------------|
| `OPC-UA/get_status` | Connection state, transport with subscribed / polled counts, poll and error counts; every server's when `server` is omitted |
| `OPC-UA/read_node` | Read by node ID |
| `OPC-UA/write_node` | Write by node ID; value is text, coerced to the server's type |
| `OPC-UA/read_named_node` | Read by node map name |
| `OPC-UA/write_named_node` | Write by node map name; value is text, coerced to the map's datatype (checks writability); discovered nodes are rejected, use `write_node` |
| `OPC-UA/list_nodes` | Mapped or discovered nodes, each with its `server` and `event`; every server's when omitted |
| `OPC-UA/list_writable_nodes` | Only writable nodes; every server's when omitted |
| `OPC-UA/browse_nodes` | Walk the address space from a starting node |
| `OPC-UA/discovered_map` | The discovered nodes as node map json or csv text (`format`) |
| `OPC-UA/auto_config` | Standalone: find servers for the config form (see Auto-configure) |

Every action takes an optional `server` (a server name). It may be omitted with one server; with several, the other actions reject an omitted `server` and list the names.

```bash
zelos actions execute OPC-UA/read_named_node --params '{"name":"temp_sensor1","server":"plc01"}'
```

Write values are text: `true`/`false`/`1`/`0` for bools, a number for numeric nodes, anything for strings. Unparseable input is rejected before the write is dispatched.

Actions run against the extension's live session, so they cost no extra connection - a call made while the extension is stopped raises rather than opening one of its own. They raise on failure; a returned payload always means success.

## CLI Usage

```bash
uv run main.py                                                   # app mode (Zelos App config)
uv run main.py demo                                              # built-in simulator
uv run main.py trace opc.tcp://192.168.1.100:4840 nodes.json     # one server: endpoint + map
uv run main.py trace opc.tcp://server:4840 nodes.json -s SignAndEncrypt -p Basic256Sha256
uv run main.py trace opc.tcp://192.168.1.100:4840                # no map: discover and trace everything
```

## Development

```bash
just install      # deps + pre-commit hooks
just format       # ruff format + fix
just check        # ruff lint + format check
just test         # pytest
just dev          # run app mode locally
just sim          # standalone simulator (see Simulator)
```

## Simulator

`just sim [ARGS]` runs a standalone server on `opc.tcp://127.0.0.1:4840/freeopcua/server/` until Ctrl-C.

| Profile | Exercises |
|---|---|
| `demo` | The demo-mode PLC (`ns=2;s=Temperature.Sensor1`, ...) |
| `gateway` | Kepware-shaped `ns=2;s=Channel.Device.Tag` (power meter, genset) with `_System` / `_Statistics` noise |
| `s7` | S7-1500-shaped `ns=3;s="DB"."tag"`; enforced MaxNodesPerBrowse 10, MaxNodesPerRead 20, 10 references per node (BrowseNext), 3 continuation points, 4 sessions, 5 subscriptions and 10 monitored items per session |
| `device` | DI `DeviceSet` identity, EngineeringUnits + EURange, a Double[4] array, a vendor struct, an abstract Number node, a Bad-status node, a reference cycle, a 14-level branch |

| Flag | Effect |
|---|---|
| `--secure` | Adds Basic256Sha256 Sign and SignAndEncrypt endpoints (self-signed server cert per start) |
| `--trust-dir DIR` | With `--secure`: client certs not in `DIR` are rejected (`BadCertificateUntrusted`) |
| `--user-cert-dir DIR` | With `--secure`: offer Certificate user tokens; user certs not in `DIR` are rejected (`BadUserAccessDenied`) |
| `--shuffle-namespaces` | Registers 1-4 placeholder namespaces first, so indices move between starts; `ZELOS_SIM_NS_SHIFT=<n>` pins it |
| `--map FILE` | Serves any node map at its exact node ids; read-only values drift, writes persist |
| `--secure-only` | With `--secure`: no None endpoint |
| `--nodes N` | Adds N Float variables `Bulk.GroupNNNN.ValueNNN` (100 per folder), computed on read |
| `--log-requests` | Logs request counts by service at shutdown |

```bash
just sim --profile s7 --log-requests
just sim --secure --trust-dir ./trusted
just sim --map my_nodes.json
```

## Links

- [Zelos Documentation](https://docs.zeloscloud.io)
- [SDK Guide](https://docs.zeloscloud.io/sdk)
- [asyncua Documentation](https://python-opcua.readthedocs.io/)
- [GitHub Issues](https://github.com/zeloscloud/zelos-extension-opcua/issues)

## License

MIT License - see [LICENSE](LICENSE) for details.
