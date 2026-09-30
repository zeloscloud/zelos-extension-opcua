"""Secure sessions end to end against the in-process simulator."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from asyncua.crypto.cert_gen import (
    dump_private_key_as_pem,
    generate_private_key,
    generate_self_signed_app_certificate,
)
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import ExtendedKeyUsageOID

from zelos_extension_opcua import actions
from zelos_extension_opcua import client as client_mod
from zelos_extension_opcua.cli import app
from zelos_extension_opcua.client import OPCUAClient, OPCUARunner, thumbprint
from zelos_extension_opcua.demo.sim_server import Simulator
from zelos_extension_opcua.discovery import HEALTH_EVENT
from zelos_extension_opcua.node_map import NodeMap

SESSION_SERVICES = {"CreateSession", "ActivateSession"}


@pytest.fixture(autouse=True)
def pki(tmp_path, monkeypatch) -> Path:
    """Generated client identity lands in tmp_path, never the real home dir."""
    pki_dir = tmp_path / "pki"
    monkeypatch.setattr(client_mod, "PKI_DIR", pki_dir)
    return pki_dir


def trust_sim(sim: Simulator, pki: Path) -> bytes:
    """Put the simulator's certificate in the trust list; returns its DER."""
    der = sim.server.iserver.certificate.public_bytes(Encoding.DER)
    (pki / client_mod.TRUSTED_DIR).mkdir(parents=True, exist_ok=True)
    (pki / client_mod.TRUSTED_DIR / f"{thumbprint(der)}.der").write_bytes(der)
    return der


def server_cert(directory: Path, uri: str, expired: bool = False) -> tuple[Path, Path]:
    """A simulator certificate and key naming `uri`; `expired`: valid only last year."""
    directory.mkdir(parents=True, exist_ok=True)
    key = generate_private_key()
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "PLC")])
    start = datetime.now(UTC) - timedelta(days=400 if expired else 1)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(uri)]), False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "server.der", directory / "server.pem"
    cert_path.write_bytes(cert.public_bytes(Encoding.DER))
    key_path.write_bytes(dump_private_key_as_pem(key))
    return cert_path, key_path


def secure_client(sim: Simulator, mode: str = "SignAndEncrypt", **kwargs) -> OPCUAClient:
    return OPCUAClient(
        endpoint=sim.endpoint, security_mode=mode, security_policy="Basic256Sha256", **kwargs
    )


def session_requests(sim: Simulator) -> list[str]:
    return [svc for _, svc in sim.request_log if svc in SESSION_SERVICES]


def user_cert(directory: Path, name: str = "operator") -> dict[str, str]:
    """An operator-issued user identity: DER cert and PEM key in `directory`."""
    directory.mkdir(parents=True, exist_ok=True)
    key = generate_private_key()
    cert = generate_self_signed_app_certificate(
        key, name, {}, [x509.UniformResourceIdentifier(f"urn:{name}")],
        [ExtendedKeyUsageOID.CLIENT_AUTH],
    )  # fmt: skip
    cert_path, key_path = directory / f"{name}.der", directory / f"{name}.pem"
    cert_path.write_bytes(cert.public_bytes(Encoding.DER))
    key_path.write_bytes(dump_private_key_as_pem(key))
    return {"user_certificate_file": str(cert_path), "user_private_key_file": str(key_path)}


def errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]


@pytest.mark.parametrize("mode", ["Sign", "SignAndEncrypt"])
async def test_generated_cert_session_and_reuse(pki, mode):
    node_map = NodeMap.from_file(app.get_demo_node_map_path())
    thumbprints = []
    async with Simulator(port=0, secure=True) as sim:
        trust_sim(sim, pki)
        for _ in range(2):
            client = secure_client(sim, mode, node_map=node_map)
            assert await client.connect() is True
            try:
                results = await client._poll_nodes()
                assert sum(len(v) for e, v in results.items() if e != HEALTH_EVENT) == len(
                    node_map.nodes
                )
            finally:
                await client.disconnect()
            thumbprints.append(thumbprint(client._identity[0]))
        assert list(sim.sessions.values()) == [(mode, "Basic256Sha256")] * 2

    assert thumbprints[0] == thumbprints[1]
    assert thumbprints[0] == thumbprint((pki / client_mod.CLIENT_CERT_FILE).read_bytes())
    assert (pki / client_mod.CLIENT_KEY_FILE).stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    ("secure", "policy", "offers"),
    [
        (False, "Basic256Sha256", "it offers None/None"),
        (True, "Aes256Sha256RsaPss", "Sign/Basic256Sha256, SignAndEncrypt/Basic256Sha256"),
    ],
)
async def test_unoffered_security_is_refused_not_downgraded(caplog, secure, policy, offers):
    async with Simulator(port=0, secure=secure) as sim:
        client = OPCUAClient(
            endpoint=sim.endpoint, security_mode="SignAndEncrypt", security_policy=policy
        )
        assert await client.connect() is False
        assert not sim.sessions
        assert session_requests(sim) == []
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any(f"does not offer SignAndEncrypt/{policy}" in e and offers in e for e in errors)


async def test_strict_server_certificate(tmp_path, caplog):
    async with Simulator(port=0, secure=True) as sim:
        # Pinned as PEM: the server hands out DER, so this also covers format handling.
        server_der = sim.server.iserver.certificate.public_bytes(Encoding.DER)
        pinned = tmp_path / "server.pem"
        pinned.write_bytes(x509.load_der_x509_certificate(server_der).public_bytes(Encoding.PEM))
        client = secure_client(
            sim, server_certificate="strict", server_certificate_file=str(pinned)
        )
        assert await client.connect() is True
        await client.disconnect()
        assert len(sim.sessions) == 1

        other, _ = client_mod.ensure_client_certificate(tmp_path / "other")
        before = len(sim.request_log)
        client = secure_client(sim, server_certificate="strict", server_certificate_file=str(other))
        assert await client.connect() is False
        assert [s for _, s in sim.request_log[before:] if s in SESSION_SERVICES] == []
        assert len(sim.sessions) == 1

    expected, actual = thumbprint(other.read_bytes()), thumbprint(server_der)
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any(actual in e and expected in e for e in errors)


async def test_untrusted_cert_stops_the_start(tmp_path, pki, caplog):
    """Rejected at start: exit with what to do; trusted, the next start connects."""
    trust = tmp_path / "trusted"
    trust.mkdir()
    async with Simulator(port=0, secure=True, trust_dir=trust) as sim:
        trust_sim(sim, pki)
        client = secure_client(sim)
        client.start()
        runner = OPCUARunner([client])
        await asyncio.wait_for(runner._run_async(), 10.0)
        assert runner.failed_at_start == [client]
        assert not sim.sessions
        cert = pki / client_mod.CLIENT_CERT_FILE
        assert [e for e in errors(caplog) if e.startswith("Server")] == [
            f"Server '127_0_0_1' ({sim.endpoint}): cannot connect: server rejected this "
            f"extension's client certificate {cert} (SHA-1 {thumbprint(cert.read_bytes())}); "
            "trust it on the server, then start the extension again"
        ]

        shutil.copy(cert, trust)
        client = secure_client(sim)
        client.start()
        runner = OPCUARunner([client])
        task = asyncio.create_task(runner._run_async())
        try:
            while not client._connected:
                assert not task.done()
                await asyncio.sleep(0.05)
            assert client.status()["state"] == "ok"
        finally:
            runner.stop()
            await asyncio.wait_for(task, 10.0)
    assert list(sim.sessions.values()) == [("SignAndEncrypt", "Basic256Sha256")]


async def test_server_trust_list(tmp_path, pki, caplog):
    """Unknown certificate: refused at start and saved; trusted, connects; replaced, refused."""
    async with Simulator(port=0, secure=True) as sim:
        # v0.1.1's `auto` is the trust list.
        client = secure_client(sim, server_certificate="auto")
        client.start()
        runner = OPCUARunner([client])
        await asyncio.wait_for(runner._run_async(), 10.0)
        assert runner.failed_at_start == [client]
        assert not sim.sessions
        der = sim.server.iserver.certificate.public_bytes(Encoding.DER)
        rejected = pki / "rejected" / f"{thumbprint(der)}.der"
        assert rejected.read_bytes() == der
        name = client_mod.certificate_name(der)
        assert sim.server.get_application_uri() in name
        assert [e for e in errors(caplog) if e.startswith("Server")] == [
            f"Server '127_0_0_1' ({sim.endpoint}): cannot connect: server certificate "
            f"{name} is not trusted; "
            f"saved to {rejected}; move it into {pki / 'trusted'} or run the "
            "trust_server_certificate action, then start the extension again"
        ]
        listed = actions.list_server_certificates()
        assert [c["thumbprint"] for c in listed["rejected"]] == [thumbprint(der)]

        result = actions.trust_server_certificate()  # the only one rejected
        assert result["thumbprint"] == thumbprint(der) and not rejected.exists()
        client = secure_client(sim)
        assert await client.connect() is True
        await client.disconnect()
        port = sim.server.bserver.port

    caplog.clear()
    async with Simulator(port=port, secure=True) as sim:  # same endpoint, new certificate
        assert await client.connect() is False
        new = sim.server.iserver.certificate.public_bytes(Encoding.DER)
        assert (pki / "rejected" / f"{thumbprint(new)}.der").is_file()
        assert any("is not trusted" in e and thumbprint(new) in e for e in errors(caplog))
        assert len(sim.sessions) == 0


@pytest.mark.parametrize(
    ("expired", "uri", "allow", "refusal"),
    [
        (True, "urn:plc", False, "expired"),
        (True, "urn:plc", True, None),
        (False, "urn:other", False, "names ApplicationUri urn:other, the server reports urn:plc"),
    ],
)
async def test_server_certificate_validation(tmp_path, pki, caplog, expired, uri, allow, refusal):
    """Validity period and ApplicationUri, checked before any session; trust list or not."""
    cert = server_cert(tmp_path / "server", uri, expired)
    async with Simulator(port=0, secure=True, certificate=cert, application_uri="urn:plc") as sim:
        trust_sim(sim, pki)
        client = secure_client(sim, allow_expired_server_certificate=allow)
        assert await client.connect() is (refusal is None)
        await client.disconnect()
        assert len(sim.sessions) == (refusal is None)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    if refusal:
        assert any(refusal in e for e in errors(caplog))
    else:
        assert any("expired" in w and "connecting anyway" in w for w in warnings)


SECURE = {"security_mode": "SignAndEncrypt", "security_policy": "Basic256Sha256"}
PLC = {"endpoint": "opc.tcp://plc:4840"}


def resolve(config: dict) -> list[OPCUAClient]:
    return app.build_clients(app.resolve_servers(config, app.resolve_advanced(config)))


@pytest.mark.parametrize(
    ("advanced", "server", "reason"),
    [
        ({}, {"security_mode": "Sign", "security_policy": "None"}, "'plc': invalid configuration"),
        ({"certificate_file": "c.der"}, {}, "set together"),
        ({}, {"server_certificate": "strict"}, "'plc': invalid configuration: server"),
        (
            {},
            {
                "security_mode": "None",
                "server_certificate": "strict",
                "server_certificate_file": "s",
            },
            "strict needs security_mode",
        ),
        ({"user_private_key_file": "u.pem"}, {}, "user_private_key_file must be set"),
        ({}, {"endpoint": "opc.tcp://ok:4841"}, "opc.tcp://ok:4840 and opc.tcp://ok:4841 both"),
        (
            {},
            {"name": " ok "},
            "opc.tcp://ok:4840 and opc.tcp://plc:4840 both resolve to name 'ok'",
        ),
        ({}, {"name": "log"}, "opc.tcp://plc:4840: name 'log' is reserved"),
    ],
)
def test_invalid_server_config_exits(caplog, advanced, server, reason):
    """Validated per server on its effective settings; an error names the server."""
    config = {
        "advanced": {**SECURE, **advanced},
        "servers": [{"endpoint": "opc.tcp://ok:4840"}, {**PLC, **server}],
    }
    with pytest.raises(SystemExit) as exc:
        resolve(config)
    assert exc.value.code == 1
    assert any(reason in e for e in errors(caplog))


def test_inherited_user_cert_on_downgraded_server_exits(tmp_path, caplog):
    config = {
        "advanced": {**SECURE, **user_cert(tmp_path)},
        "servers": [{"endpoint": "opc.tcp://ok:4840"}, {**PLC, "security_mode": "None"}],
    }
    with pytest.raises(SystemExit):
        resolve(config)
    assert errors(caplog) == [
        "Server 'plc': invalid configuration: "
        "a user certificate needs security_mode Sign or SignAndEncrypt"
    ]


def test_endpoint_credentials_are_refused(caplog):
    """asyncua would log in with them, in plain text on a None channel; never echoed."""
    config = {"servers": [{"endpoint": "opc.tcp://admin:secret@plc:4840"}]}
    with pytest.raises(SystemExit):
        resolve(config)
    result = actions.auto_config(config)  # never contacted
    assert errors(caplog) == [
        "Server 'plc': remove the user name / password from the endpoint URL; "
        "user name login is not supported, use a user certificate"
    ]
    assert result["status"] == "error" and result["message"].startswith("plc: remove the user")
    assert "secret" not in result["message"]


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        (
            {"endpoint": "opc.tcp://plc:4840", "username": "admin", "advanced": {}},
            "Config format changed: servers are now listed under servers[]",
        ),
        ({"servers": []}, "No servers configured"),
    ],
)
def test_config_shape_exits(caplog, config, reason):
    with pytest.raises(SystemExit) as exc:
        resolve(config)
    assert exc.value.code == 1
    assert len(errors(caplog)) == 1 and reason in errors(caplog)[0]


def test_defaults_match_schema():
    schema = json.loads((Path(__file__).parents[1] / "config.schema.json").read_text())
    props = schema["properties"]
    for defaults, obj in (
        (app.ADVANCED_DEFAULTS, props["advanced"]),
        (app.SERVER_DEFAULTS, props["servers"]["items"]),
    ):
        assert {k: v["default"] for k, v in obj["properties"].items()} == defaults


@pytest.mark.parametrize(("days", "warns"), [(10, True), (client_mod.CLIENT_CERT_DAYS, False)])
def test_cert_expiry_is_announced(monkeypatch, caplog, days, warns):
    monkeypatch.setattr(client_mod, "CLIENT_CERT_DAYS", days)
    caplog.set_level(logging.INFO)
    OPCUAClient(endpoint="opc.tcp://unused:4840")._client_identity()
    assert any("expires" in r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
    warned = any(
        "expires" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    )
    assert warned is warns


@pytest.mark.parametrize("trusted", [True, False])
async def test_user_certificate_identity(tmp_path, pki, caplog, trusted):
    users = tmp_path / "users"
    users.mkdir()
    identity = user_cert(users if trusted else tmp_path / "other")
    user_der = Path(identity["user_certificate_file"]).read_bytes()
    node_map = NodeMap.from_file(app.get_demo_node_map_path())
    async with Simulator(port=0, secure=True, user_cert_dir=users) as sim:
        trust_sim(sim, pki)
        client = secure_client(sim, node_map=node_map, **identity)
        assert await client.connect() is trusted
        if trusted:
            try:
                results = await client._poll_nodes()
                assert sum(len(v) for e, v in results.items() if e != HEALTH_EVENT) == len(
                    node_map.nodes
                )
            finally:
                await client.disconnect()
            assert list(sim.identities.values()) == [f"certificate:{thumbprint(user_der)}"]
        else:
            assert sim.identities == {}
            assert any(
                f"rejected user certificate SHA-1 {thumbprint(user_der)} (BadUserAccessDenied)" in e
                for e in errors(caplog)
            )


async def test_user_certificate_refused_without_server_policy(tmp_path, caplog):
    async with Simulator(port=0, secure=True) as sim:
        client = secure_client(sim, **user_cert(tmp_path))
        assert await client.connect() is False
        assert session_requests(sim) == []
    assert any(
        "does not accept user certificates" in e and "it offers Anonymous, UserName" in e
        for e in errors(caplog)
    )


async def test_per_server_security_inherits_or_overrides(pki, caplog):
    """Secure default inherited by one server, explicitly downgraded on the other."""
    demo_map = str(app.get_demo_node_map_path())
    async with Simulator(port=0, secure=True) as sec, Simulator(port=0) as plain:
        trust_sim(sec, pki)
        config = {
            "advanced": SECURE,
            "servers": [
                {"name": "sec", "endpoint": sec.endpoint, "node_map_file": demo_map},
                {
                    "name": "plain",
                    "endpoint": plain.endpoint,
                    "node_map_file": demo_map,
                    "security_mode": "None",
                },
            ],
        }
        runner = OPCUARunner(resolve(config))
        for client in runner.clients.values():
            client.start()
        task = asyncio.create_task(runner._run_async())
        try:
            async with asyncio.timeout(15.0):
                while not all(c._poll_count for c in runner.clients.values()):
                    await asyncio.sleep(0.05)
        finally:
            runner.stop()
            await asyncio.wait_for(task, 10.0)
        assert list(sec.sessions.values()) == [("SignAndEncrypt", "Basic256Sha256")]
        assert list(plain.sessions.values()) == [("None", "None")]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    downgrades = [w for w in warnings if "overrides the advanced default SignAndEncrypt" in w]
    assert downgrades and all(w.startswith("[plain] ") for w in downgrades)
