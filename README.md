# Zelos OPC-UA Extension

OPC-UA protocol extension for Zelos with node mapping, data type support, and interactive actions.

## Features

- **OPC-UA Client** - Connect to any OPC-UA server with automatic reconnection
- **Node Mapping** - Define semantic events from OPC-UA nodes using JSON
- **All Data Types** - bool, int8-64, uint8-64, float32/64, string
- **Security Support** - None, Sign, and SignAndEncrypt modes with multiple policies
- **Interactive Actions** - Read/write nodes, browse address space, get status
- **Demo Mode** - Built-in PLC simulator for testing without hardware

## Quick Start

```bash
# Install dependencies
just install

# Run demo mode (simulated PLC)
uv run main.py demo

# Connect to a real server
uv run main.py trace opc.tcp://192.168.1.100:4840 nodes.json
```

## Configuration

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `demo` | boolean | `false` | Use built-in PLC simulator |
| `endpoint` | string | `opc.tcp://localhost:4840` | OPC-UA server endpoint URL |
| `security_mode` | string | `None` | Security mode: None, Sign, SignAndEncrypt |
| `security_policy` | string | `None` | Security policy: None, Basic256Sha256, etc. |
| `username` | string | `""` | Username for authentication |
| `password` | string | `""` | Password for authentication |
| `node_map_file` | string | `""` | Path to JSON node map file |
| `poll_interval` | number | `1.0` | Polling interval in seconds |
| `timeout` | number | `5.0` | Request timeout in seconds |
| `log_level` | string | `INFO` | Logging verbosity |

## Node Map Format

Node maps define how OPC-UA nodes are grouped into Zelos trace events:

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

### Node Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `node_id` | Yes | - | OPC-UA node ID (ns=X;s=name or ns=X;i=number) |
| `name` | Yes | - | Field name in Zelos event |
| `datatype` | No | `float32` | Data type (see below) |
| `unit` | No | `""` | Unit string for display |
| `scale` | No | `1.0` | Scale factor (value × scale) |
| `writable` | No | `null` | Override writability (null = auto-detect) |

### Supported Data Types

| Type | Size | Description |
|------|------|-------------|
| `bool` | 1 bit | Boolean |
| `uint8` / `int8` | 8-bit | Byte |
| `uint16` / `int16` | 16-bit | Word |
| `uint32` / `int32` | 32-bit | Double word |
| `uint64` / `int64` | 64-bit | Quad word |
| `float32` | 32-bit | IEEE 754 single |
| `float64` | 64-bit | IEEE 754 double |
| `string` | Variable | UTF-8 string |

## Actions

| Action | Description |
|--------|-------------|
| Get Status | Connection state, poll/error counts |
| Read Node | Read by node ID |
| Write Node | Write by node ID |
| Read Named | Read by name from node map |
| Write Named | Write by name (checks writability) |
| List Nodes | All mapped nodes |
| List Writable | Only writable nodes |
| Browse Nodes | Browse OPC-UA address space |

## Development

```bash
just install      # Install deps + pre-commit hooks
just check        # Run ruff linter
just format       # Auto-format code
just test         # Run pytest
just dev          # Run extension locally
```

## CLI Usage

```bash
# Demo mode with built-in PLC simulator
uv run main.py demo

# Connect to server with node map
uv run main.py trace opc.tcp://192.168.1.100:4840 nodes.json

# Connect with authentication
uv run main.py trace opc.tcp://server:4840 nodes.json -u admin --password secret

# Connect with security
uv run main.py trace opc.tcp://server:4840 nodes.json -s SignAndEncrypt -p Basic256Sha256

# Connect without node map (use Browse action to discover nodes)
uv run main.py trace opc.tcp://192.168.1.100:4840
```

## Links

- [Zelos Documentation](https://docs.zeloscloud.io)
- [SDK Guide](https://docs.zeloscloud.io/sdk)
- [asyncua Documentation](https://python-opcua.readthedocs.io/)
- [GitHub Issues](https://github.com/zeloscloud/zelos-extension-opcua/issues)

## License

MIT License - see [LICENSE](LICENSE) for details.
