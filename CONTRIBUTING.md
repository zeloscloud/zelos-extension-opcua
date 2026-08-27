# Contributing

## Development Workflow

```bash
just install  # dependencies + pre-commit hooks
just dev      # run app mode locally
just demo     # run against the built-in PLC simulator
just test     # pytest
just check    # ruff lint + format check
just format   # auto-fix
```

1. Make your changes
2. `just format`
3. `just test`
4. Commit (pre-commit hooks run ruff automatically)

## Project Structure

```
zelos-extension-opcua/
├── extension.toml                  # Extension manifest (name, version, host, package paths)
├── config.schema.json              # Configuration UI schema
├── main.py                         # Click entry point
├── pyproject.toml                  # Dependencies, ruff and pytest config
├── uv.lock                         # Locked dependency versions
├── Justfile                        # Development commands
├── LICENSE
├── README.md                       # User documentation
├── CHANGELOG.md
├── CLAUDE.md                       # Architecture notes
├── CONTRIBUTING.md                 # This file
├── .pre-commit-config.yaml
├── zelos_extension_opcua/
│   ├── __init__.py                 # ACTION_PREFIX, package exports
│   ├── actions.py                  # Action functions + register_actions()
│   ├── client.py                   # Connection, batch polling, reconnect, shutdown
│   ├── node_map.py                 # Node/NodeMap parsing and name rules
│   ├── cli/
│   │   ├── __init__.py
│   │   └── app.py                  # Config load, startup validation, serve()
│   └── demo/
│       ├── __init__.py
│       ├── simulator.py            # Demo OPC-UA server
│       └── plc_device.json         # Demo node map
├── tests/
│   └── test_opcua.py               # Unit + integration tests
├── scripts/
│   └── bump_version.py             # Updates version numbers
├── assets/
│   └── icon.svg                    # Marketplace icon
├── .github/
│   ├── workflows/
│   │   ├── CI.yml
│   │   └── release.yml
│   └── dependabot.yml
└── .vscode/
```

## Common Tasks

### Add a Dependency

```bash
uv add package-name        # runtime
uv add --dev package-name  # dev
```

### Package for the Marketplace

```bash
just package   # zelos extensions package .
```

Produces a `.tar.gz` for the Zelos Marketplace. CI does this automatically.

### Create a Release

```bash
just release 1.0.0
git push --follow-tags
```

## Testing

Integration tests start the real demo OPC-UA server on a loopback port, so they
exercise the actual protocol path (batch reads, partial failure, reconnection).

```bash
just test                        # everything
uv run pytest -v                 # verbose
uv run pytest -k test_name       # one test
```

Keep tests targeted. Prefer one integration test that proves a behavior end to
end over several unit tests that assert on internals.

```python
# tests/test_opcua.py
async def test_write_readonly_raises(client):
    node = client.node_map.get_by_name("input1")
    with pytest.raises(ValueError, match="not writable"):
        await client.write_node_value(node, True)
```

## Code Quality

- ruff, line length 100, google docstrings
- Type hints on every function signature
- Comments state the constraint or the failure prevented, not what the code does

## Getting Help

- [Zelos Docs](https://docs.zeloscloud.io)
- [SDK Guide](https://docs.zeloscloud.io/sdk)
- [GitHub Issues](https://github.com/zeloscloud/zelos-extension-opcua/issues)

## License

MIT - see [LICENSE](LICENSE)
