# Zelos OPC-UA Extension

Traces OPC-UA servers into Zelos trace events, by subscription or polling, and exposes read/write/browse as Zelos actions.

## Features

| Feature | Detail |
|---------|--------|
| Transport | Subscriptions by default; refused items are polled in batched Reads |
| Discovery | A server without a node map is browsed on every connect and every scalar variable traced (read-only) |
| Security | None / Sign / SignAndEncrypt, with Basic256Sha256, Aes128Sha256RsaOaep, Aes256Sha256RsaPss; X.509 user login |
| Multiple servers | Any number in one extension; one unreachable server never stalls the others |
| Reconnection | Capped exponential backoff (3s, doubling, 60s ceiling), reset on a completed request |
| Data types | bool, int8-64, uint8-64, float32/64, string |
| Demo mode | Built-in PLC simulator, no hardware |

## Quick Start

```bash
uv run main.py                                                   # app mode (Zelos App config)
uv run main.py demo                                              # built-in simulator
uv run main.py trace opc.tcp://192.168.1.100:4840 nodes.json     # one server: endpoint + map
uv run main.py trace opc.tcp://server:4840 nodes.json -s SignAndEncrypt -p Basic256Sha256
uv run main.py trace opc.tcp://192.168.1.100:4840                # no map: discover and trace everything
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

Trace layout: `OPC-UA/<server>/<event>` (e.g. `OPC-UA/plc01/temperature`); with `advanced.prefix` cleared, one source per server (`plc01/temperature`). Logs go to `opcua_log`.

Startup errors: the pre-`servers[]` flat config; `username` / `password` (no secret is stored in config, use a user certificate); a missing or unparseable `node_map_file`.

### Secure connections

1. Set `security_mode` / `security_policy` (per server or `advanced`). A server not offering that pair is refused with its offerings logged; never falls back to None. A server set to None under a secure default connects with a WARNING.
2. On first connect a client certificate is generated in `$ZELOS_DATA_DIR/pki/` (`~/.zelos/opcua/pki/` for CLI runs); path, SHA-1 thumbprint and expiry are logged. **Trust it on the server.** Valid 2 years; WARNING from 30 days before expiry; an expired one is regenerated and must be trusted again.
3. Optional: `server_certificate: strict` + `server_certificate_file` refuses any server certificate but the pinned one.
4. Optional: X.509 user login with a certificate issued by the server admin. A server without a Certificate user token policy is refused; never falls back to Anonymous.

### Discovery

A server without a `node_map_file` is browsed on every connect (read-only, nothing written to disk), so program changes appear after a reconnect.

| Aspect | Behavior |
|---|---|
| Walk | Forward hierarchical references from `Objects`; skips `Server`, Objects named `_*` (Kepware `_System`, ...), properties |
| Traced | Every scalar variable of a supported type; arrays, structs, other types skipped (one INFO count per connect) |
| Event / field | Event = parent path below `Objects` (`Line1/Motor`), field = BrowseName; vendor string ids (Kepware/TwinCAT dots, Siemens `"DB"."tag"`, CODESYS, B&R `::Task:Var`) name the event from the id |
| Collisions | Every collider is named `<field>_<hash>` (6 hex of SHA-1 of its `nsu=` id, stable); one WARNING per connect; a name never moves to another node |
| Changes on reconnect | New event: traced. New field: new trace segment (joined by the app). Changed datatype: skipped with one WARNING until restart |
| Browse errors | BadNoContinuationPoints: re-browsed alone; other Bad: one WARNING per connect |

`discovered_map` returns the discovered set as node map json (save as `node_map_file` to pin or edit) or csv.

### Transport and timestamps

- `subscription`: one subscription per distinct interval (event `poll_interval`, else server's), queue size 1, change of value or status, no deadband. Refused items (e.g. BadTooManyMonitoredItems) and an undecodable Publish fall back to polling for the connection, one WARNING per connect.
- `poll`: batched Reads of min(MaxNodesPerRead, 100) nodes, spread evenly across the interval.
- A subscribed node silent for `min_update_interval` is re-read (worst case ceil(nodes / 100) Reads per interval) and logged at the Read's ServerTimestamp.
- Samples are logged at SourceTimestamp, else ServerTimestamp, else receipt. No clock-skew correction (see `_server.clock_skew_ms`). Fields with different timestamps are separate rows.

### Server health

Event `_server`, every poll interval, at host time: `state`, `state_name`, `current_time`, `clock_skew_ms` (server minus host), `start_time`, `service_level`, `current_session_count`, `cumulated_session_count`, `rejected_requests_count`, `security_rejected_requests_count`, `current_subscription_count`. Fields the server does not publish are dropped for the connection.

### Diagnostics

A response with an array length past the bytes left in the message is rejected (one WARNING per server); an event loop blocked > 10s dumps every thread's stack to the extension log; peak RSS over 2 GB (then each doubling) logs one WARNING with per-server node and subscription counts.

### Auto-configure

The config form's button runs `auto_config` (extension stopped); it replaces `servers` only.

| Source | What |
|---|---|
| localhost ports | GetEndpoints on 4840, 4841, 48010, 49320 (Kepware), 62541, 53530 (Prosys), 2s each, in parallel |
| Local Discovery Server | FindServers on `opc.tcp://localhost:4840` |
| mDNS | 2s passive browse for `_opcua-tcp._tcp.local.` |

Deduplicated by ApplicationUri, named by ApplicationName. Security: `default` if the server accepts None, else its strongest policy with SignAndEncrypt (then Sign); trust the client certificate on the server.

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

Event keys are trace events, node names their fields; the map `name` is optional and not part of trace paths. An event is a node list or `{"poll_interval": s, "nodes": [...]}` (seconds, >= 0.1).

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

Names are sanitized at load (`. @ : ; =` and whitespace, and `/` in node names, become `_`). A duplicate event name, or node name within one event, is a load error; a node name in several events is addressed as `<event>/<name>`.

## Actions

| Action | Description |
|--------|-------------|
| `OPC-UA/get_status` | Connection state, transport with subscribed / polled counts, poll and error counts, process `peak_rss_mb`; every server's when `server` is omitted |
| `OPC-UA/read_node` | Read by node ID |
| `OPC-UA/write_node` | Write by node ID; value is text, coerced to the server's type |
| `OPC-UA/read_named_node` | Read by node map name |
| `OPC-UA/write_named_node` | Write by node map name; value is text, coerced to the map's datatype (checks writability); discovered nodes are rejected, use `write_node` |
| `OPC-UA/list_nodes` | Mapped or discovered nodes, each with its `server` and `event`; every server's when omitted |
| `OPC-UA/list_writable_nodes` | Only writable nodes; every server's when omitted |
| `OPC-UA/browse_nodes` | Walk the address space from a starting node |
| `OPC-UA/discovered_map` | The discovered nodes as node map json or csv text (`format`) |
| `OPC-UA/auto_config` | Standalone: find servers for the config form (see Auto-configure) |

Every action takes an optional `server`, required when several servers are configured. Write values are text: `true`/`false`/`1`/`0` for bools, a number for numeric nodes; unparseable input is rejected before sending. Actions use the live session (none while stopped) and raise on failure.

```bash
zelos actions execute OPC-UA/read_named_node --params '{"name":"temp_sensor1","server":"plc01"}'
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

`just sim [ARGS]`: standalone server on `opc.tcp://127.0.0.1:4840/freeopcua/server/` until Ctrl-C.

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

Example: `just sim --profile s7 --secure --trust-dir ./trusted --log-requests`.

## Links

- [Zelos Documentation](https://docs.zeloscloud.io)
- [SDK Guide](https://docs.zeloscloud.io/sdk)
- [asyncua Documentation](https://python-opcua.readthedocs.io/)
- [GitHub Issues](https://github.com/zeloscloud/zelos-extension-opcua/issues)

## License

MIT License - see [LICENSE](LICENSE) for details.
