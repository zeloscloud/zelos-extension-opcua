# CLAUDE.md

## Build & Development

```bash
just install      # Install deps + pre-commit hooks
just check        # Run ruff linter
just format       # Auto-format code
just test         # Run pytest
just dev          # Run extension locally
```

## Demo Mode

```bash
uv run main.py demo    # Start with simulated PLC
```

## Code Style

- **Linter**: ruff (strict)
- **Pre-commit**: Runs ruff-check + ruff-format on commit
- Use `contextlib.suppress(Exception)` instead of `try: ... except: pass`
- Imports must be sorted (ruff handles this)

## Key Files

- `zelos_extension_opcua/client.py` - OPC-UA client with Zelos SDK actions
- `zelos_extension_opcua/node_map.py` - Node definitions and parsing
- `zelos_extension_opcua/demo/simulator.py` - Demo OPC-UA server
- `zelos_extension_opcua/demo/plc_device.json` - Demo node map
- `zelos_extension_opcua/cli/app.py` - App mode runner
- `tests/test_opcua.py` - All tests (unit + integration)

## Architecture

### Node Map
- JSON file defines events and their OPC-UA nodes
- Events become Zelos trace events
- Node names become fields within events
- Supports all OPC-UA data types (bool, int8-64, uint8-64, float32/64, string)

### Client
- Uses asyncua library for OPC-UA protocol
- Automatic reconnection on connection loss
- Polling loop with configurable interval
- Zelos SDK integration for trace events and actions

### Actions
- Get Status: Connection info, poll/error counts
- Read/Write Node: By node ID
- Read/Write Named: By name from node map
- List Nodes/Writable: Enumerate mapped nodes
- Browse Nodes: Discover OPC-UA address space

## Testing

Tests use a real OPC-UA server (demo mode) for integration tests:

```bash
uv run pytest -v
```

## Node ID Format

OPC-UA node IDs follow the format: `ns=<namespace>;[s|i|g|b]=<identifier>`

- `ns=2;s=Temperature.Sensor1` - String identifier
- `ns=2;i=1001` - Numeric identifier
- `ns=1;g=12345678-1234-5678-1234-567812345678` - GUID identifier
