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

### Changed
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

### Removed
- `scripts/package_extension.py`, superseded by `zelos extensions package`.

---

*For maintainers: When releasing, move items from [Unreleased] to a new version section:*

```markdown
## [0.1.0] - 2024-12-15

### Added
- (move items from Unreleased here)
```
