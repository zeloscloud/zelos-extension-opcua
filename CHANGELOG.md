# Changelog

All notable changes to Zelos Opcua Extension will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial implementation
- Reconnect with capped exponential backoff (3s doubling to 60s, reset on a
  completed poll) and typed connection-loss detection from asyncua status codes
  instead of exception-string matching.
- Node names, event names, and the source name are sanitized (`. @ : ; = /` and
  whitespace become `_`) before reaching the trace catalog; duplicate names
  after sanitization are a hard error at node map load.
- `demo-server` / `just sim`: standalone simulator with `gateway`, `s7` and `device`
  profiles, `--secure` with an optional trust list, `--shuffle-namespaces`, `--map`,
  and a per-session request log.
- Client certificate handling: a self-signed client certificate generated in
  the extension data directory and reused, or `advanced.certificate_file` /
  `private_key_file`; `advanced.server_certificate` `auto` or `strict` pinning.
- X.509 user identity: `advanced.user_certificate_file` / `user_private_key_file`
  (operator-issued) log in with a user certificate on a Sign or SignAndEncrypt
  channel; a server without a Certificate user token policy is refused.
- `nsu=<uri>;...` node IDs in maps and actions, resolved against the server's NamespaceArray on each connect; `browse_nodes` returns an `nsu_node_id` per result.
- Live discovery: a server without a node map is browsed on every connect and every
  scalar variable traced, read-only; `advanced.discovery` turns it off.
  `discovered_map` returns the discovered set as node map json or csv.
- Server health event `server` per server (state, clock skew, service level,
  session and rejected-request counts), read in the poll's own request.
- `auto_config` standalone action behind the config form's Auto-configure button:
  localhost well-known ports, the Local Discovery Server and mDNS (`zeroconf`).
- Simulator `--nodes N` (large address space) and `--secure-only`.

### Changed
- Browse and poll requests are chunked by the server's MaxNodesPerBrowse /
  MaxNodesPerRead, capped at 100.
- A node name may repeat across events in a node map (`<event>/<name>` in the
  named actions); only a duplicate within one event is an error.
- zelos-sdk floor 0.0.12a1.
- Config format: servers are listed under `servers[]` (name, endpoint, node map,
  poll interval, per-server security and certificate pin) with shared settings and
  security defaults under `advanced` (`prefix`, `timeout`, `log_level`, client
  certificate, default security mode/policy and user certificate). A server's
  `default` / empty security settings inherit the advanced default. The old flat
  config is a startup error.
- Trace paths are now `OPC-UA/<server>/<event>` (source `advanced.prefix`, default
  `OPC-UA`); clearing the prefix gives each server its own source. The node map
  `name` no longer names the source.
- Every action takes an optional `server`; `get_status` and `list_nodes` /
  `list_writable_nodes` cover every server when it is omitted.
- Actions moved from client methods to a free-function surface registered under
  the `OPC-UA/` prefix (was `zelos_extension_opcua/`). Actions now reuse the
  live server connection instead of opening a new one per invocation, and
  failures raise instead of returning `success: false` payloads.
- Polling issues one batched read per cycle with per-node status handling; a
  failing node no longer aborts the cycle and logs at most one error per
  process lifetime.
- Shutdown is loop-native and bounded: SIGTERM/SIGINT close the OPC UA session
  cleanly with a 3s cap instead of exiting from the signal handler, and a
  shutdown during a connect attempt cancels it instead of waiting the connect
  out (an unreachable endpoint used to hold the process well past the grace
  period).
- Write actions take text instead of a number, coerced to the node's datatype
  (`true`/`false`/`1`/`0` for bools), so bool and string nodes are writable.
- Polling chunks its batched read at 100 nodes per request, so a large node map
  cannot exceed a server's `MaxNodesPerRead`.
- `zelos` app compatibility floor raised to `>=26.0.4`, the first release that
  parses the `[host]` / `[package]` manifest syntax.
- A configured node map file that is missing or unparseable is now a startup
  error instead of a warning that silently records nothing.
- Demo node map: `sensor1`/`sensor2` renamed to `temp_sensor1`/`temp_sensor2`
  and `pressure_sensor1`/`pressure_sensor2` (name uniqueness rule).
- `asyncua` floor raised to `>=1.1.8` (batch-read status handling verified
  against 1.1.8).
- Manifest migrated from the legacy `[runtime]` section to `[host] type = "agent"`
  plus `[host.agent]`, with an explicit `[package]` section defining the archive
  contents. Manifest `name` is now `OPC-UA`, so the archive slug is `opc-ua`.
- Packaging runs `zelos extensions package`, which builds the archive from the
  manifest instead of a hand-rolled script.
- `zelos-sdk` floor raised to `>=0.0.11a1`: name sanitization delegates to the
  SDK's `sanitize_name` (the catalog name grammar), and packaging can inventory
  standalone actions.
- CI verifies `uv.lock` is in sync with `pyproject.toml` (`uv lock --locked`), and
  `just check` now enforces `ruff format --check` alongside `ruff check`.
- Log lines use UTC ISO 8601 timestamps with milliseconds, matching the SDK's Rust tracing format.

### Removed
- `scripts/package_extension.py`, superseded by `zelos extensions package`.
- Username/password login (`username`, `password`, `trace -u/--password`): no
  secret is stored in config. A config still setting either is a startup error;
  use a user certificate.

### Fixed
- Sign / SignAndEncrypt never engaged: the security call was never awaited, so
  every session was plaintext. A secure mode now establishes exactly that mode and
  policy or refuses to connect; it never falls back to None.

---

*For maintainers: When releasing, move items from [Unreleased] to a new version section:*

```markdown
## [0.1.0] - 2024-12-15

### Added
- (move items from Unreleased here)
```
