"""The connector handshake as claude.ai walks it, now with accounts."""
import base64
import hashlib
import urllib.parse

import pytest
from starlette.testclient import TestClient

from garmin_mcp import oauth, users
from garmin_mcp.server import build_http_app

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "a" * 64


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://mcp-garmin.example")
    with TestClient(build_http_app(), base_url="https://testserver") as c:
        yield c


@pytest.fixture
def logged_in(client, user_id):
    r = client.post("/login", data={"email": "anja@example.com",
                                    "password": "supersecret123"})
    assert r.status_code == 200 and "/account" in str(r.url)
    return user_id


def _register(client) -> str:
    r = client.post("/oauth/register",
                    json={"client_name": "Claude", "redirect_uris": [REDIRECT]})
    assert r.status_code == 201
    return r.json()["client_id"]


def _params(client_id: str) -> dict:
    return {"response_type": "code", "client_id": client_id,
            "redirect_uri": REDIRECT, "code_challenge": _challenge(VERIFIER),
            "code_challenge_method": "S256", "state": "xyz", "scope": "mcp"}


def test_discovery_documents(client):
    prm = client.get("/.well-known/oauth-protected-resource").json()
    assert prm["resource"] == "https://mcp-garmin.example/mcp"
    asm = client.get("/.well-known/oauth-authorization-server").json()
    assert asm["code_challenge_methods_supported"] == ["S256"]


def test_unauthorized_mcp_points_at_the_metadata(client):
    r = client.post("/mcp", json={})
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]


def test_register_rejects_http_redirect(client):
    r = client.post("/oauth/register", json={"redirect_uris": ["http://evil.test/cb"]})
    assert r.status_code == 400


def test_authorize_without_a_session_goes_to_the_login(client, user_id):
    client_id = _register(client)
    r = client.get("/oauth/authorize", params=_params(client_id),
                   follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login?next=%2Foauth%2Fauthorize")


def test_full_authorization_code_flow(client, logged_in):
    client_id = _register(client)
    params = _params(client_id)

    page = client.get("/oauth/authorize", params=params)
    assert page.status_code == 200
    assert "anja@example.com" in page.text and "passphrase" not in page.text.lower()

    ok = client.post("/oauth/authorize", data={**params, "decision": "allow"},
                     follow_redirects=False)
    assert ok.status_code == 302
    q = urllib.parse.parse_qs(urllib.parse.urlparse(ok.headers["location"]).query)
    assert q["state"] == ["xyz"]

    tok = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": q["code"][0],
        "redirect_uri": REDIRECT, "client_id": client_id,
        "code_verifier": VERIFIER}).json()
    assert oauth.access_token_user(tok["access_token"]) == logged_in

    again = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": q["code"][0],
        "redirect_uri": REDIRECT, "client_id": client_id, "code_verifier": VERIFIER})
    assert again.json()["error"] == "invalid_grant"

    refreshed = client.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
        "client_id": client_id}).json()
    assert oauth.access_token_user(refreshed["access_token"]) == logged_in
    assert oauth.access_token_user(tok["access_token"]) is None      # rotated away


def test_wrong_pkce_verifier_is_rejected(client, logged_in):
    client_id = _register(client)
    params = _params(client_id)
    ok = client.post("/oauth/authorize", data={**params, "decision": "allow"},
                     follow_redirects=False)
    code = urllib.parse.parse_qs(
        urllib.parse.urlparse(ok.headers["location"]).query)["code"][0]
    bad = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": client_id, "code_verifier": "b" * 64})
    assert bad.json()["error"] == "invalid_grant"


def test_deny_redirects_with_access_denied(client, logged_in):
    client_id = _register(client)
    r = client.post("/oauth/authorize", data={**_params(client_id), "decision": "deny"},
                    follow_redirects=False)
    assert "error=access_denied" in r.headers["location"]


def test_disabling_an_account_kills_its_tokens(client, logged_in):
    client_id = _register(client)
    params = _params(client_id)
    ok = client.post("/oauth/authorize", data={**params, "decision": "allow"},
                     follow_redirects=False)
    code = urllib.parse.parse_qs(
        urllib.parse.urlparse(ok.headers["location"]).query)["code"][0]
    tok = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": client_id, "code_verifier": VERIFIER}).json()
    assert oauth.access_token_user(tok["access_token"]) == logged_in
    users.set_status(logged_in, "disabled")
    assert oauth.access_token_user(tok["access_token"]) is None


def test_registered_clients_survive_a_restart(client, logged_in):
    client_id = _register(client)
    with TestClient(build_http_app(), base_url="https://testserver") as fresh:
        page = fresh.get("/oauth/authorize", params=_params(client_id),
                         follow_redirects=False)
        assert page.status_code == 302        # known client, only the login is missing
