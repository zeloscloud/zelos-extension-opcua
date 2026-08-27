# Zelos OPC-UA Extension

Polls an OPC-UA server into Zelos trace events, and exposes read/write/browse as Zelos actions.

## Features

| Feature | Detail |
|---------|--------|
| Batch polling | Every mapped node in one read request per 100 nodes per cycle; a bad node is reported once and skipped |
| Reconnection | Capped exponential backoff (3s, doubling, 60s ceiling), reset on a completed poll |
| Node mapping | JSON file groups nodes into trace events and fields |
| Data types | bool, int8-64, uint8-64, float32/64, string |
| Security | None / Sign / SignAndEncrypt, with Basic256Sha256, Aes128Sha256RsaOaep, Aes256Sha256RsaPss |
| Actions | Read, write, list and browse, live against the polling connection |
| Demo mode | Built-in PLC simulator, no hardware |

Values are read by polling. OPC-UA subscriptions (server-pushed data changes) are not used.

## Quick Start

```bash
just install                  # dependencies + pre-commit hooks
uv run main.py demo           # simulated PLC
uv run main.py trace opc.tcp://192.168.1.100:4840 nodes.json
```

## Configuration

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `demo` | boolean | `false` | Use the built-in PLC simulator |
| `endpoint` | string | `opc.tcp://localhost:4840` | Server endpoint URL |
| `security_mode` | string | `None` | None, Sign, SignAndEncrypt |
| `security_policy` | string | `None` | None, Basic256Sha256, Aes128Sha256RsaOaep, Aes256Sha256RsaPss |
| `username` | string | `""` | Empty for anonymous |
| `password` | string | `""` | Password for authentication |
| `node_map_file` | string | `""` | Path to the JSON node map |
| `poll_interval` | number | `1.0` | Seconds between poll cycles |
| `timeout` | number | `5.0` | Request timeout in seconds |
| `log_level` | string | `INFO` | Logging verbosity; an unknown value falls back to INFO |

A configured `node_map_file` that is missing or unparseable is a startup error: the extension logs one line and exits, rather than running healthy while recording nothing.

## Node Map Format

```json
{
  "name": "my_device",
  "events": {
    "temperature": [
      {"name": "sensor1", "node_id": "ns=2;s=Temperature.Sensor1", "datatype": "float32", "unit": "°C"},
      {"name": "sensor2", "node_id": "ns=2;i=1001", "datatype": "float32", "unit": "°C"}
    ],
    "status": [
      {"name": "running", "node_id": "ns=2;s=Status.Running", "datatype": "bool", "writable": true}
    ]
  }
}
```

`name` is the trace source, event keys are trace events, and node names are fields within them.

### Node Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `node_id` | Yes | - | `ns=<n>;[s\|i\|g\|b]=<identifier>` |
| `name` | Yes | - | Field name in the Zelos event |
| `datatype` | No | `float32` | bool, uint8-64, int8-64, float32, float64, string |
| `unit` | No | `""` | Unit string for display |
| `scale` | No | `1.0` | Read value x scale; write value / scale |
| `writable` | No | `null` | `null` auto-detects from AccessLevel |

### Naming Rules

Map, event and node names are sanitized at load: `. @ : ; = /` and whitespace become `_`, because those characters are path separators in Zelos trace names. After sanitization, a duplicate event name or a duplicate node name anywhere in the map is a hard error - names must stay unambiguous for `read_named_node` / `write_named_node`.

## Actions

Registered under the `OPC-UA/` prefix (the extension's name in `extension.toml`).

| Action | Description |
|--------|-------------|
| `OPC-UA/get_status` | Connection state, poll and error counts |
| `OPC-UA/read_node` | Read by node ID |
| `OPC-UA/write_node` | Write by node ID; value is text, coerced to the server's type |
| `OPC-UA/read_named_node` | Read by node map name |
| `OPC-UA/write_named_node` | Write by node map name; value is text, coerced to the map's datatype (checks writability) |
| `OPC-UA/list_nodes` | All mapped nodes |
| `OPC-UA/list_writable_nodes` | Only writable nodes |
| `OPC-UA/browse_nodes` | Walk the address space from a starting node |

```bash
zelos actions execute OPC-UA/read_named_node --params '{"name":"temp_sensor1"}'
```

Write values are text: `true`/`false`/`1`/`0` for bools, a number for numeric nodes, anything for strings. Unparseable input is rejected before the write is dispatched.

Actions run against the polling loop's live session, so they cost no extra connection - a call made while the extension is stopped raises rather than opening one of its own. They raise on failure; a returned payload always means success.

## CLI Usage

```bash
uv run main.py                                                   # app mode (Zelos App config)
uv run main.py demo                                              # built-in simulator
uv run main.py trace opc.tcp://192.168.1.100:4840 nodes.json     # explicit endpoint + map
uv run main.py trace opc.tcp://server:4840 nodes.json -u admin --password secret
uv run main.py trace opc.tcp://server:4840 nodes.json -s SignAndEncrypt -p Basic256Sha256
uv run main.py trace opc.tcp://192.168.1.100:4840                # no map; use browse_nodes to discover
```

## Development

```bash
just install      # deps + pre-commit hooks
just format       # ruff format + fix
just check        # ruff lint + format check
just test         # pytest
just dev          # run app mode locally
```

## Links

- [Zelos Documentation](https://docs.zeloscloud.io)
- [SDK Guide](https://docs.zeloscloud.io/sdk)
- [asyncua Documentation](https://python-opcua.readthedocs.io/)
- [GitHub Issues](https://github.com/zeloscloud/zelos-extension-opcua/issues)

## License

MIT License - see [LICENSE](LICENSE) for details.
