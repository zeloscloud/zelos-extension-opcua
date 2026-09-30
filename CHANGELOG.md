# Changelog

All notable changes to Zelos Opcua Extension will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial implementation.
- Multiple servers in one extension (`servers[]`); one unreachable server never stalls the others.
- Subscriptions by default (`advanced.transport`, per-server override): one per distinct interval, sampling = publishing = interval, queue size 1, change of value or status. Refused items or subscriptions, and an undecodable Publish, fall back to polling for the connection with one WARNING.
- `advanced.min_update_interval` (default 60s, per-server override, empty inherits): a subscribed node silent that long is re-read and logged at the Read's ServerTimestamp.
- Node map events may be `{"poll_interval": s, "nodes": [...]}` to override the server's interval.
- Samples are logged at SourceTimestamp, else ServerTimestamp, else receipt; fields of one event with different timestamps are separate rows.
- Live discovery: a server without a node map is browsed on every connect and every scalar variable traced, read-only (`advanced.discovery` turns it off). Colliding names all get `_<hash>` of their `nsu=` id, so a name never re-points; fields added on reconnect start a new trace source segment; a field whose datatype changed is skipped with one WARNING until restart.
- Discovery types Number / Integer / UInteger as float64 / int64 / uint64 and BaseDataType as string (rendered as text: bool, ISO 8601 UTC, hex, StatusCode name); BaseDataType nodes are always polled (`get_status` `polled_variant`).
- `discovered_map` action: discovered nodes as node map json or csv.
- `nsu=<uri>;...` node IDs in maps and actions, resolved against the NamespaceArray on each connect; `browse_nodes` returns `nsu_node_id`.
- `_server` health event per server (state, clock skew, service level, session and rejected-request counts) from its own small Read per interval.
- `auto_config` standalone action (config form's Auto-configure button): localhost well-known ports, Local Discovery Server, mDNS (`zeroconf`).
- Client certificate generated in the extension data directory and reused, or `advanced.certificate_file` / `private_key_file`; `server_certificate` `auto` or `strict` pinning.
- X.509 user identity (`user_certificate_file` / `user_private_key_file`) on Sign / SignAndEncrypt; a server without a Certificate user token policy is refused.
- Reconnect with capped exponential backoff (3s doubling to 60s, reset on a completed request); connection loss detected from asyncua status codes, not message text.
- Names are sanitized (`. @ : ; = /` and whitespace become `_`); duplicates after sanitization are a load error.
- A lost connection resumes its session on the new channel (ActivateSession) and keeps its subscriptions; missed notifications are recovered by Republish. A session the server no longer holds (timed out, server restarted) is replaced, with one INFO saying why. Discovery still re-browses; subscriptions are rebuilt only if the discovered nodes changed.
- Subscription recovery on a live connection: a missed notification (sequence-number gap) is fetched by Republish; a missed keep-alive, an unrecoverable gap or a server-ended subscription recreates that subscription and re-reads its items. One WARNING per event.
- Read MaxAge: polled nodes accept a value up to one poll interval old, staleness re-reads up to `min_update_interval`; health and discovery reads stay fresh (0).
- Discovery `include` / `exclude` globs (`advanced`, per-server override) over the browse path (`*` one segment, `**` any depth); exclude wins; branches no pattern can reach are not browsed; filtered and pruned counts in the discovery INFO line.
- `get_status` reports transport and subscribed / polled counts.
- `demo-server` / `just sim`: simulator with `gateway`, `s7` (enforced limits and session caps) and `device` profiles, `--secure`, `--secure-only`, trust lists, `--shuffle-namespaces`, `--map`, `--nodes N`, request log.

### Changed
- Writes send only the Value (DataValue mask 0x01), no StatusCode or SourceTimestamp: servers that refuse them answered BadWriteNotSupported.
- Session timeout 120 s requested (was asyncua's 1 h). A session left open by a lost connection is re-activated on the new channel and closed before a new one is created; on the 4-session `s7` sim the 4th flap no longer fails with BadTooManySessions.
- An endpoint URL with a user name or password is a startup error and an `auto_config` problem (asyncua logged in with it, in plain text on a None channel).
- A Bad node is logged once per connection (was once per process), so a later outage is logged again.
- Uncertain values are traced (were dropped), subscribed or polled, with one INFO per node per connection naming the status; Bad stays a gap.
- Revised publishing / sampling interval and queue size are logged once per subscription; a slower revised publishing interval paces the staleness sweep.
- Discovery types a vendor DataType deriving from a builtin integer as that integer (was float64): 64-bit values stay exact.
- A server that cannot be reached or refuses the session at start (untrusted certificate, security not offered, user rejected) stops the extension with one ERROR naming it; once connected, drops are retried as before.
- `get_status` reports `state` (`ok`, `connecting`, `disconnected`) and `last_error`.
- Server certificates are checked against a trust list (`server_certificate: trust_list`, the new default; v0.1.1's `auto` means the same): an unknown or changed one is saved to `pki/rejected/` and refused with one ERROR saying how to trust it. `trust_server_certificate` and `list_server_certificates` standalone actions.
- Every secure connect refuses a server certificate outside its validity period or not naming the server's ApplicationUri; `allow_expired_server_certificate` (per server) connects anyway with a WARNING.
- `auto_config` checks the form's servers (unsaved edits, Advanced security included) and names each outcome: found, couldn't connect, no supported security; with none it looks on this machine as before.
- Demo Mode toggle removed from the settings form; `main.py demo` / `just demo` remain.
- Extension icon.
- **Breaking** config: servers under `servers[]` (name, endpoint, node map, poll interval, transport, security, certificate pin), shared settings and security defaults under `advanced`; `default` / empty per-server security inherits. The old flat config is a startup error.
- **Breaking** trace paths: `OPC-UA/<server>/<event>` (source `advanced.prefix`, default `OPC-UA`; cleared, one source per server). The node map `name` no longer names the source.
- **Breaking** actions: registered under `OPC-UA/` (was `zelos_extension_opcua/`), reuse the live connection, and raise on failure instead of returning `success: false`. Every action takes an optional `server`; `get_status` / `list_*` cover all servers when omitted.
- Write actions take text coerced to the node's datatype (`true`/`false`/`1`/`0` for bools), so bool and string nodes are writable.
- A node name may repeat across events (addressed as `<event>/<name>`); only a duplicate within one event is an error.
- Reads are batched (min(MaxNodesPerRead, 100) per request, spread across the interval) with per-node status; a bad or undecodable node costs only itself, logs one error per process, and an undecodable one is dropped from polling until reconnect. Browse is chunked by MaxNodesPerBrowse, capped at 100.
- Shutdown is bounded (~3s): SIGTERM/SIGINT close sessions cleanly and cancel an in-flight connect.
- A missing or unparseable `node_map_file` is a startup error.
- Demo node map: `sensor1`/`sensor2` renamed to `temp_sensor*` / `pressure_sensor*`.
- asyncua 2.0.1 (was 1.1.8): fixes BadServerUriInvalid against .NET-stack (Microsoft OPC PLC) and Unified Automation servers.
- zelos-sdk floor 0.0.12a1; `zelos` app floor `>=26.0.4`.
- Manifest uses `[host] type = "agent"` + `[host.agent]` and `[package]`; `name` is `OPC-UA` (archive slug `opc-ua`); packaging runs `zelos extensions package`.
- CI checks `uv lock --locked`; `just check` enforces `ruff format --check`.
- Log lines use UTC ISO 8601 timestamps with milliseconds.
- Extension logs are the `log` event on the prefix source (`OPC-UA/log`, was source `opcua_log`; `opcua_log` when the prefix is cleared). `log` is a reserved server name.

### Removed
- `scripts/package_extension.py`.
- **Breaking** username/password login (`username`, `password`, `trace -u/--password`): setting either is a startup error; use a user certificate.

### Fixed
- Sign / SignAndEncrypt never engaged (security call not awaited, every session plaintext). A secure mode now gets exactly that mode and policy or refuses; never falls back to None.
- Discovery found nothing on .NET-stack servers (BadNodeNotInView): Browse asks for the current view.
- Discovery re-browses a node refused BadNoContinuationPoints instead of dropping its branch; other Bad browse statuses are one WARNING per connect.
- A garbage array length in a response (seen once: a Null-element Variant array) froze the loop at 100% CPU growing to 10 GB; a length past the bytes left is now a decode error with one WARNING per server. Added a loop stall watchdog (thread stacks to the log after 10s blocked), peak RSS WARNING from 2 GB, `get_status` `peak_rss_mb`.

---

*For maintainers: When releasing, move items from [Unreleased] to a new version section:*

```markdown
## [0.1.0] - 2024-12-15

### Added
- (move items from Unreleased here)
```
