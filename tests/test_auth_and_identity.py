"""Path-token auth (D3, T8), health (T12), key directory (T13), identity (section 3)."""
from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from starlette.testclient import TestClient

TOKEN = "test_token_that_is_at_least_32_chars_long_abc"


@pytest.fixture()
def client(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "MCP_PATH_TOKEN", TOKEN)
    import main

    monkeypatch.setattr(main.config, "MCP_PATH_TOKEN", TOKEN)
    return TestClient(main.build_app())


def test_healthz_reports_browser_presence(client):
    """T12."""
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert isinstance(body["chromium_present"], bool)
    assert body["identity_mode"] in ("stealth", "declared")
    assert "version" in body


@pytest.mark.parametrize("path", ["/status", "/health", "/healthz"])
def test_health_is_served_at_every_alias(client, path):
    """Replit's fronting infrastructure intercepts /healthz on the published
    app and answers with its own 404, so D5's endpoint needs a name that
    actually reaches the process. /status is the production name; the others
    stay registered so the check does not change name between dev and prod.
    """
    r = client.get(path)
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_root_advertises_the_working_health_path(client):
    assert client.get("/").json()["health"] == "/status"


def test_version_is_reported(client):
    """D5 wants the build identity. "unknown" in production means there is no
    way to tell which build is serving."""
    for path in ("/", "/status"):
        assert client.get(path).json()["version"]


def test_wrong_token_returns_404_not_403(client):
    """T8, and D3's reason: a 403 confirms the path space exists."""
    r = client.post("/mcp/definitely-not-the-token", json={})
    assert r.status_code == 404
    assert "Not Found" in r.text


def test_bare_mcp_path_returns_404(client):
    assert client.post("/mcp").status_code == 404
    assert client.post("/mcp/").status_code == 404


def test_token_prefix_is_not_accepted(client):
    """Guards against a prefix/startswith comparison slipping in."""
    assert client.post(f"/mcp/{TOKEN[:-1]}").status_code == 404
    assert client.post(f"/mcp/{TOKEN}extra").status_code == 404


def test_empty_configured_token_refuses_everything(monkeypatch):
    from app import config
    import main

    monkeypatch.setattr(config, "MCP_PATH_TOKEN", "")
    monkeypatch.setattr(main.config, "MCP_PATH_TOKEN", "")
    c = TestClient(main.build_app())
    assert c.post("/mcp/").status_code == 404
    assert c.post("/mcp/anything").status_code == 404


def test_signature_directory_is_a_valid_key_document(client):
    """T13."""
    r = client.get("/.well-known/http-message-signatures-directory")
    assert r.status_code == 200
    doc = r.json()
    assert "keys" in doc and len(doc["keys"]) >= 1
    key = doc["keys"][0]
    assert key["kty"] == "OKP"
    assert key["crv"] == "Ed25519"
    assert key["alg"] == "EdDSA"
    assert key["kid"]
    # x must be a valid 32-byte Ed25519 public key in base64url.
    raw = base64.urlsafe_b64decode(key["x"] + "=" * (-len(key["x"]) % 4))
    assert len(raw) == 32
    ed25519.Ed25519PublicKey.from_public_bytes(raw)


# --- identity ---------------------------------------------------------------


def test_identity_modes_differ():
    from app import identity

    stealth = identity.base_headers("stealth")
    declared = identity.base_headers("declared")
    assert "Chrome" in stealth["user-agent"]
    assert "RenderFetch" in declared["user-agent"]
    assert "from" in declared


def test_accept_encoding_only_advertises_decodable_codecs():
    """The bug this guards against silently broke tiers 1 and 2.

    Advertising an encoding httpx cannot decode returns undecoded bytes;
    extraction yields nothing and every page falls through to expensive tiers.
    """
    from app import identity

    advertised = {e.strip() for e in identity.ACCEPT_ENCODING.split(",")}
    assert "gzip" in advertised
    for codec, module in (("br", "brotli"), ("zstd", "zstandard")):
        if codec in advertised:
            __import__(module.replace("brotli", "brotli"))  # importable, else the header lies


def test_resolve_mode_prefers_the_override(monkeypatch):
    from app import config, identity

    monkeypatch.setattr(config, "IDENTITY_MODE", "stealth")
    assert identity.resolve_mode("declared") == "declared"
    assert identity.resolve_mode(None) == "stealth"
    assert identity.resolve_mode("nonsense") == "stealth"


def test_declared_mode_signs_requests():
    from app import identity

    headers = identity.headers_for("declared", "GET", "https://example.com/a?b=1")
    assert "signature" in headers
    assert "signature-input" in headers
    assert headers["signature-agent"].startswith('"')
    assert 'keyid="' in headers["signature-input"]
    assert 'alg="ed25519"' in headers["signature-input"]
    assert 'tag="web-bot-auth"' in headers["signature-input"]


def test_stealth_mode_does_not_sign():
    from app import identity

    headers = identity.headers_for("stealth", "GET", "https://example.com/")
    assert "signature" not in headers


def test_signature_verifies_against_the_published_key():
    """The signature must actually verify, or the directory is decoration."""
    from app import identity

    url = "https://example.com/path?q=1"
    headers = identity.headers_for("declared", "GET", url)

    params = headers["signature-input"].split("=", 1)[1]
    sig_b64 = headers["signature"].split(":", 1)[1].rstrip(":")
    signature = base64.b64decode(sig_b64)

    base = "\n".join(
        [
            '"@method": GET',
            '"@authority": example.com',
            '"@path": /path?q=1',
            f'"signature-agent": "{identity.signature_agent_url()}"',
            f'"@signature-params": {params}',
        ]
    )

    jwk = identity.public_jwk()
    raw = base64.urlsafe_b64decode(jwk["x"] + "=" * (-len(jwk["x"]) % 4))
    ed25519.Ed25519PublicKey.from_public_bytes(raw).verify(signature, base.encode())


def test_directory_document_is_json_serialisable():
    from app import identity

    json.dumps(identity.directory_document())
