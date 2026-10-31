# Contributing

## Prerequisites

- [Zelos](https://zeloscloud.io) 26.0.10 or later
- [Zelos CLI](https://docs.zeloscloud.io/getting-started/install-cli/) 0.1.12 or later
- [just](https://github.com/casey/just)
- [uv](https://docs.astral.sh/uv/)

## Layout

`extension.toml` describes everything the extension contributes:

| Part | Files | Manifest |
| ---- | ----- | -------- |
| Agent runtime | `main.py`, the Python package, `pyproject.toml`, `config.schema.json` | `[agent]`, `[config]` |

| Path                                         | Purpose                                                                |
| -------------------------------------------- | ---------------------------------------------------------------------- |
| `main.py`                                    | Entry point; the app, `demo`, `demo-server` and `trace` commands       |
| `zelos_extension_opcua/client.py`            | One client per server: connect, subscribe or poll, reconnect, shutdown |
| `zelos_extension_opcua/actions.py`           | The OPC-UA actions                                                     |
| `zelos_extension_opcua/discovery.py`         | Browse a server's address space and map every scalar variable          |
| `zelos_extension_opcua/node_map.py`          | Node map parsing, event grouping and name rules                        |
| `zelos_extension_opcua/autoconfig.py`        | Find servers for the config form's Auto-configure button               |
| `zelos_extension_opcua/diagnostics.py`       | Guards for asyncua array lengths, event loop stalls and memory use     |
| `zelos_extension_opcua/cli/app.py`           | Config load, `servers[]` and `advanced` resolution, startup validation |
| `zelos_extension_opcua/demo/simulator.py`    | The simulated PLC server demo mode uses                                |
| `zelos_extension_opcua/demo/plc_device.json` | The node map for the simulated PLC                                     |
| `zelos_extension_opcua/demo/sim_server.py`   | The configurable simulator behind `demo-server`                        |
| `zelos_extension_opcua/demo/profiles.py`     | The gateway, S7 and device address spaces, and served node maps        |
| `tests/test_opcua.py`                        | Node map, codecs, actions, and the client against the demo server      |
| `tests/test_transport.py`                    | Subscriptions, polling fallback, staleness sweep and sample timestamps |
| `tests/test_discovery.py`                    | Live discovery, server health and auto-configure                       |
| `tests/test_security.py`                     | Secure sessions and certificate trust                                  |
| `tests/test_servers.py`                      | Several servers in one extension                                       |
| `tests/test_sim.py`                          | The simulator profiles and flags                                       |
| `tests/test_diagnostics.py`                  | The array-length guard and the stall watchdog                          |
| `tests/test_interop.py`                      | Microsoft OPC PLC in Docker; manual                                    |

## Commands

| Command                | Description                                                |
| ---------------------- | ---------------------------------------------------------- |
| `just install`         | Install dependencies and the pre-commit hooks              |
| `just format`          | Format code with ruff                                      |
| `just check`           | Check formatting and lint (ruff)                           |
| `just test`            | Run tests (pytest)                                         |
| `just package`         | Package for the Zelos marketplace                          |
| `just release VERSION` | Bump version, check, test, commit, tag                     |
| `just clean`           | Remove build output, caches and packages                   |
| `just demo`            | Run against the built-in PLC simulator, no hardware        |
| `just sim`             | Run the standalone simulator on `opc.tcp://127.0.0.1:4840` |
| `just e2e-interop`     | Run the interop tests against Microsoft OPC PLC in Docker  |

`just --list` also shows the recipes each part adds. `just install` installs the
pre-commit hooks in `.pre-commit-config.yaml`.

## Testing

`just test` starts real OPC-UA servers from the simulator inside the test process, on loopback ports.
The tests exercise the actual protocol path: batch reads, partial failure, subscriptions, security and reconnection.

The interop tests in `tests/test_interop.py` run against Microsoft OPC PLC, a .NET OPC-UA stack, in Docker.
They are manual, before a release, and they skip unless `ZELOS_INTEROP=1`.
They need Docker and a free port 4840.

| Variable              | Value                                                                       |
| --------------------- | --------------------------------------------------------------------------- |
| `ZELOS_INTEROP`       | `1` to run the tests; `just e2e-interop` sets it                            |
| `ZELOS_INTEROP_IMAGE` | The container image; defaults to `mcr.microsoft.com/iotedge/opc-plc:latest` |

```bash
just e2e-interop
```

## Run it in Zelos

Start the simulator in one terminal:

```bash
just sim
```

Then install the extension from this directory and start it against the simulator:

```bash
zelos extensions install-local .
zelos extensions start local.opc-ua --config '{"servers": [{"endpoint": "opc.tcp://127.0.0.1:4840"}]}'
zelos extensions logs local.opc-ua
```

A local install runs from your project directory, so you do not install again after a code
change:

- Agent runtime: stop and start the extension. After a dependency change, stop it and run
  `zelos extensions reinstall local.opc-ua`.
- `extension.toml`: run `zelos extensions install-local .` again.

A server with no node map is browsed, and every scalar variable it holds is traced. Run `uv run python main.py demo-server --help` for the other profiles, a secured
endpoint, or a node map to serve.

To run it outside the agent, with the simulator still running:

```bash
echo '{"servers": [{"endpoint": "opc.tcp://127.0.0.1:4840"}]}' > config.json
just run
```

`just run` reads `./config.json`, which needs at least one server, and connects to the agent at
`ZELOS_AGENT_URL`, else `http://localhost:2300`. `just demo` starts the simulator and a
connection to it in one process.

## Packaging

```bash
just package
```

The archive lands next to `extension.toml` as `<name>-<version>.tar.gz`, where `<name>` is the
manifest name in lowercase with every other run of characters turned into one hyphen. It holds
`extension.toml`, the README, the icon, the config schema and the paths listed in
`[package] paths`. With an agent runtime it also holds `pyproject.toml`, `uv.lock` and
`actions.json`, the list of actions that run while the extension is stopped.

## Release

Add the release to `CHANGELOG.md` and commit it, then:

```bash
just release 1.0.0
git push --follow-tags
```

The release workflow checks, tests and packages the extension, then attaches the archive to a
GitHub release. CI runs the same check, test and package steps on every push and pull request.

To list the extension in the Zelos marketplace, once: push the project to a GitHub repository,
submit it at [marketplace.zeloscloud.io](https://marketplace.zeloscloud.io/), and install the
Zelos GitHub App on the repository when asked. After approval, every release you publish reaches
the marketplace. See
[Publish to the Marketplace](https://docs.zeloscloud.io/sdk/how-to/package-extensions/#publish-to-the-marketplace).
