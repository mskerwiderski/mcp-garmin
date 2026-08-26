"""Connecting a Garmin account: web login, MFA, and the token blob."""
import asyncio

import pytest
from starlette.testclient import TestClient

from garmin_mcp import connect, tokens, users
from garmin_mcp.client import GarminError, LoginState
from garmin_mcp.server import build_http_app


class StubClient:
    """Stands in for GarminClient; no network, no Garmin."""

    behaviour = "ok"          # ok | mfa | error

    def __init__(self):
        self.closed = False

    async def login(self, email, password):
        if self.behaviour == "error":
            raise GarminError("HTTP 429: Too Many Requests")
        return LoginState.NEEDS_MFA if self.behaviour == "mfa" else LoginState.OK

    async def submit_mfa(self, code):
        return LoginState.OK if code == "123456" else LoginState.NEEDS_MFA

    async def fetch_display_name(self):
        return "Stub Athlete"

    def export_tokens(self):
        return '{"oauth_token": "t1", "oauth_token_secret": "s1"}', '{"access_token": "a"}'

    async def aclose(self):
        self.closed = True


@pytest.fixture(autouse=True)
def stub(monkeypatch):
    StubClient.behaviour = "ok"
    monkeypatch.setattr(connect, "GarminClient", StubClient)
    connect._pending.clear()
    return StubClient


@pytest.fixture
def client(user_id):
    with TestClient(build_http_app(), base_url="https://testserver") as c:
        c.post("/login", data={"email": "anja@example.com",
                               "password": "supersecret123"})
        yield c


def test_web_login_stores_only_tokens(client, user_id):
    r = client.post("/account/garmin/login",
                    data={"email": "anja@garmin.example", "password": "garminpw"})
    assert r.status_code == 200 and "Connected as" in r.text
    stored = users.get_garmin_tokens(user_id)
    assert stored[2] == "Stub Athlete"
    assert "garminpw" not in str(stored)


def test_mfa_is_a_second_step(client, user_id, stub):
    stub.behaviour = "mfa"
    r = client.post("/account/garmin/login",
                    data={"email": "a@b.example", "password": "pw"})
    assert "Enter your Garmin code" in r.text
    assert users.get_garmin_tokens(user_id) is None
    assert connect.mfa_pending(user_id)

    wrong = client.post("/account/garmin/mfa", data={"code": "000000"})
    assert wrong.status_code == 400 and "not accepted" in wrong.text

    ok = client.post("/account/garmin/mfa", data={"code": "123456"})
    assert ok.status_code == 200 and "Connected as" in ok.text
    assert users.get_garmin_tokens(user_id)[2] == "Stub Athlete"
    assert not connect.mfa_pending(user_id)


def test_mfa_without_a_pending_login(client, user_id):
    r = client.post("/account/garmin/mfa", data={"code": "123456"})
    assert r.status_code == 400 and "start again" in r.text


def test_rate_limited_login_points_at_the_blob(client, user_id, stub):
    stub.behaviour = "error"
    r = client.post("/account/garmin/login",
                    data={"email": "a@b.example", "password": "pw"})
    assert r.status_code == 400
    assert "token blob" in r.text and "bot protection" in r.text


def test_blob_import(client, user_id):
    blob = tokens.export_blob(tokens.Tokens(
        oauth1={"oauth_token": "t1", "oauth_token_secret": "s1"},
        oauth2={"access_token": "a", "refresh_token": "r", "expires_in": 3600,
                "expires_at": 1.0},
        account="Blob Athlete"))
    r = client.post("/account/garmin/blob", data={"blob": blob})
    assert r.status_code == 200 and "Blob Athlete" in r.text
    assert users.get_garmin_tokens(user_id)[2] == "Blob Athlete"


def test_blob_garbage_is_rejected(client, user_id):
    r = client.post("/account/garmin/blob", data={"blob": "not-a-blob"})
    assert r.status_code == 400 and "garmin-mcp export" in r.text
    assert users.get_garmin_tokens(user_id) is None


def test_disconnect(client, user_id):
    client.post("/account/garmin/login",
                data={"email": "a@b.example", "password": "pw"})
    r = client.post("/account/garmin/disconnect")
    assert "Not connected yet" in r.text
    assert users.get_garmin_tokens(user_id) is None


def test_connecting_needs_a_login(user_id):
    with TestClient(build_http_app(), base_url="https://testserver") as anon:
        r = anon.post("/account/garmin/login", data={"email": "a", "password": "b"},
                      follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"].startswith("/login")


def test_sso_logins_are_serialised(user_id, monkeypatch):
    """Two people logging in at the same moment must not overlap inside
    Garmin's SSO - that is the pattern that gets an IP rate-limited."""
    other = users.create_user("bob@example.com", "anotherlongpw",
                              users.create_invite())
    inside, overlapped = 0, False

    class SlowClient(StubClient):
        async def login(self, email, password):
            nonlocal inside, overlapped
            inside += 1
            overlapped = overlapped or inside > 1
            await asyncio.sleep(0.05)
            inside -= 1
            return LoginState.OK

    monkeypatch.setattr(connect, "GarminClient", SlowClient)

    async def both():
        await asyncio.gather(
            connect.start_login(user_id, "a@b.example", "pw"),
            connect.start_login(other, "c@d.example", "pw"))

    asyncio.run(both())
    assert not overlapped
    assert users.get_garmin_tokens(user_id) and users.get_garmin_tokens(other)
