# Zelos OPC-UA

A Zelos extension for OPC-UA. Trace PLCs, gateways, and other OPC-UA servers by subscription or polling, and read, write, and browse nodes from the Zelos App.

## Features

- 📡 **Subscriptions or polling**: Server-pushed changes by default; refused items are polled in batched Reads
- 🔍 **Discovery**: No node map needed; every scalar variable is found and traced
- 📄 **Node map files**: Pin node ids, names, units, and scaling in a simple JSON file
- 🔒 **Secure connections**: Signed and encrypted sessions, X.509 user login
- 🏭 **Multiple servers**: Any number in one extension; one unreachable server never stalls the others
- ✏️ **Read, write & browse actions**: Interactive node access from the Zelos App
- 🧪 **Demo mode**: Built-in PLC simulator for testing without hardware

## Quick Start

From the CLI, on the agent that can reach the server:

```bash
zelos extensions install zeloscloud/zelos-extension-opcua
zelos extensions start zeloscloud.zelos-extension-opcua \
  --config '{"servers": [{"endpoint": "opc.tcp://192.168.1.100:4840"}]}'
```

In the app:

1. **Install** the extension from the Zelos App
2. **Configure** your servers (endpoint, and a node map file or nothing: discovery), or press Auto-configure
3. **Start** the extension to begin streaming data
4. **View** real-time node values in your Zelos App

## Configuration

### Servers

| Setting | Default | Description |
|---------|---------|-------------|
| Name | endpoint host | Trace name (`plc01`); unique |
| Server Endpoint | `opc.tcp://localhost:4840` | Server endpoint URL |
| Node Map File | | JSON [node map](#node-map); empty = [discovery](#discovery) |
| Poll Interval | `1.0` | Seconds between updates |
| Transport | `subscription` | `subscription` (server pushes changes) or `poll` |
| Security Mode / Policy | `None` | See [Secure connections](#secure-connections) |

### Advanced

Applies to every server unless the server overrides it.

| Setting | Default | Description |
|---------|---------|-------------|
| `prefix` | `OPC-UA` | Trace source: `OPC-UA/plc01/temperature`; cleared = one source per server |
| `timeout` | `5.0` | Request timeout (s) |
| `log_level` | `INFO` | Logging verbosity |
| `discovery` | on | Browse servers without a node map |
| `include` / `exclude` | | Discovery path globs (`Line1/**`) |
| `min_update_interval` | `60` | Re-read a subscribed node silent this long (s) |
| `certificate_file` / `private_key_file` | | Client certificate; empty generates one |
| `user_certificate_file` / `user_private_key_file` | | X.509 user login; empty = Anonymous |

Every server must connect at start, or the extension stops with an error naming it. Once running, a dropped connection is retried with backoff and its session resumed.

### Secure connections

Set Security Mode / Policy, then trust the client certificate (path logged on first connect) on the server, and the server's certificate here with `trust_server_certificate`.

### Discovery

A server without a node map is browsed on connect and every scalar variable traced. `include` / `exclude` narrow it; `discovered_map` exports the result as a node map.

### Auto-configure

The config form's Auto-configure button checks the servers in the form, or with none, finds servers on this machine (common ports, Local Discovery Server, mDNS) and adds them.

## Node Map

```json
{
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

Event keys become trace events, node names their fields. An event may set its own `poll_interval`.

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `node_id` | Yes | | `ns=2;s=Tag`, or `nsu=<uri>;s=Tag` to pin the namespace by URI |
| `name` | Yes | | Field name |
| `datatype` | No | `float32` | bool, int8-64, uint8-64, float32, float64, string |
| `unit` | No | | Display unit |
| `scale` | No | `1.0` | Read value x scale |
| `writable` | No | auto | Detected from the server's access level |

## Actions

| Action | Description |
|--------|-------------|
| `get_status` | Connection state, transport, counters |
| `read_node` / `write_node` | Read or write by node id |
| `read_named_node` / `write_named_node` | Read or write by node map name |
| `list_nodes` / `list_writable_nodes` | Mapped or discovered nodes |
| `browse_nodes` | Walk the address space from a node |
| `discovered_map` | Discovered nodes as a node map (json or csv) |
| `auto_config` | Find servers for the config form |
| `trust_server_certificate` / `list_server_certificates` | Manage trusted server certificates |

With several servers, pass `server` to pick one.

```bash
zelos actions execute OPC-UA/read_named_node --params '{"name":"sensor1","server":"plc01"}'
```

## Development

```bash
just install      # deps + pre-commit hooks
just format       # ruff format + fix
just check        # ruff lint + format check
just test         # pytest
just dev          # run app mode locally
just sim          # standalone simulator: --profile demo|gateway|s7|device, --secure
```

## Links

- [Zelos Documentation](https://docs.zeloscloud.io)
- [SDK Guide](https://docs.zeloscloud.io/sdk)
- [asyncua Documentation](https://python-opcua.readthedocs.io/)
- [GitHub Issues](https://github.com/zeloscloud/zelos-extension-opcua/issues)

## CLI Usage

The extension includes a command-line interface for tracing without the Zelos App. No installation required, just use `uv run`:

```bash
uv run main.py                                                   # app mode (Zelos App config)
uv run main.py demo                                              # built-in simulator
uv run main.py trace opc.tcp://192.168.1.100:4840 nodes.json     # one server: endpoint + map
uv run main.py trace opc.tcp://server:4840 nodes.json -s SignAndEncrypt -p Basic256Sha256
uv run main.py trace opc.tcp://192.168.1.100:4840                # no map: discover and trace everything
```

## Support

For help and support:
- 📖 [Zelos Documentation](https://docs.zeloscloud.io)
- 🐛 [GitHub Issues](https://github.com/zeloscloud/zelos-extension-opcua/issues)
- 📧 help@zeloscloud.io

## License

MIT License - see [LICENSE](LICENSE) for details.

---

**Built with [Zelos](https://zeloscloud.io)**
