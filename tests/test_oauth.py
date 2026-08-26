"""The whole connector handshake, as claude.ai and ChatGPT walk it."""
import base64
import hashlib
import urllib.parse

import pytest
from starlette.testclient import TestClient

from garmin_mcp import oauth
from garmin_mcp.server import build_http_app

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "a" * 64


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MCP_PASSPHRASE", "letmein")
    monkeypatch.setenv("PUBLIC_URL", "https://mcp-garmin.example")
    oauth.STORE.clients.clear()
    oauth.STORE.tokens.clear()
    oauth.STORE.codes.clear()
    with TestClient(build_http_app()) as c:
        yield c


def test_discovery_documents(client):
    prm = client.get("/.well-known/oauth-protected-resource").json()
    assert prm["resource"] == "https://mcp-garmin.example/mcp"
    asm = client.get("/.well-known/oauth-authorization-server").json()
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert asm["registration_endpoint"].endswith("/oauth/register")


def test_unauthorized_mcp_points_at_the_metadata(client):
    r = client.post("/mcp", json={})
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]


def test_register_rejects_http_redirect(client):
    r = client.post("/oauth/register", json={"redirect_uris": ["http://evil.test/cb"]})
    assert r.status_code == 400


def _register(client) -> str:
    r = client.post("/oauth/register",
                    json={"client_name": "Claude", "redirect_uris": [REDIRECT]})
    assert r.status_code == 201
    return r.json()["client_id"]


def _authorize_params(client_id: str) -> dict:
    return {"response_type": "code", "client_id": client_id,
            "redirect_uri": REDIRECT, "code_challenge": _challenge(VERIFIER),
            "code_challenge_method": "S256", "state": "xyz", "scope": "mcp"}


def test_full_authorization_code_flow(client):
    client_id = _register(client)
    params = _authorize_params(client_id)

    page = client.get("/oauth/authorize", params=params)
    assert page.status_code == 200 and "passphrase" in page.text

    wrong = client.post("/oauth/authorize",
                        data={**params, "decision": "allow", "passphrase": "nope"})
    assert wrong.status_code == 401

    ok = client.post("/oauth/authorize",
                     data={**params, "decision": "allow", "passphrase": "letmein"},
                     follow_redirects=False)
    assert ok.status_code == 302
    q = urllib.parse.parse_qs(urllib.parse.urlparse(ok.headers["location"]).query)
    assert q["state"] == ["xyz"]
    code = q["code"][0]

    tok = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": client_id, "code_verifier": VERIFIER}).json()
    assert tok["token_type"] == "Bearer"
    assert oauth.validate_access_token(tok["access_token"])

    # the code is single use
    again = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": client_id, "code_verifier": VERIFIER})
    assert again.json()["error"] == "invalid_grant"

    refreshed = client.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
        "client_id": client_id}).json()
    assert refreshed["access_token"] != tok["access_token"]
    assert not oauth.validate_access_token(tok["access_token"])   # rotated away


def test_wrong_pkce_verifier_is_rejected(client):
    client_id = _register(client)
    params = _authorize_params(client_id)
    ok = client.post("/oauth/authorize",
                     data={**params, "decision": "allow", "passphrase": "letmein"},
                     follow_redirects=False)
    code = urllib.parse.parse_qs(
        urllib.parse.urlparse(ok.headers["location"]).query)["code"][0]
    bad = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": client_id, "code_verifier": "b" * 64})
    assert bad.json()["error"] == "invalid_grant"


def test_deny_redirects_with_access_denied(client):
    client_id = _register(client)
    params = _authorize_params(client_id)
    r = client.post("/oauth/authorize",
                    data={**params, "decision": "deny", "passphrase": "letmein"},
                    follow_redirects=False)
    assert "error=access_denied" in r.headers["location"]


def test_registered_clients_survive_a_restart(client, monkeypatch):
    client_id = _register(client)
    oauth.STORE.clients.clear()
    oauth.STORE.bind(oauth.state_path())
    assert client_id in oauth.STORE.clients


def test_authorize_without_passphrase_configured(monkeypatch):
    monkeypatch.delenv("MCP_PASSPHRASE", raising=False)
    oauth.STORE.clients.clear()
    with TestClient(build_http_app()) as c:
        assert c.get("/oauth/authorize", params={"client_id": "x"}).status_code == 503
