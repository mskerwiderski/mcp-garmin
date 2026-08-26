"""Sign-up, login and the account page."""
import pytest
from starlette.testclient import TestClient

from garmin_mcp import users
from garmin_mcp.server import build_http_app


@pytest.fixture
def client():
    with TestClient(build_http_app(), base_url="https://testserver") as c:
        yield c


def test_signup_without_an_invite_does_not_exist(client):
    assert client.get("/signup").status_code == 404
    assert client.get("/signup", params={"code": "guessed"}).status_code == 404


def test_signup_with_an_invite_creates_an_account(client):
    code = users.create_invite("Anja")
    page = client.get("/signup", params={"code": code})
    assert page.status_code == 200 and "What this server stores" in page.text

    r = client.post("/signup", data={"code": code, "email": "anja@example.com",
                                     "password": "supersecret123"})
    assert r.status_code == 200 and str(r.url).endswith("/account")
    assert "anja@example.com" in r.text
    assert users.user_id_by_email("anja@example.com") is not None


def test_signup_rejects_a_used_invite(client):
    code = users.create_invite()
    client.post("/signup", data={"code": code, "email": "a@example.com",
                                 "password": "supersecret123"})
    r = client.post("/signup", data={"code": code, "email": "b@example.com",
                                     "password": "supersecret123"})
    assert r.status_code == 400 and "invalid" in r.text


def test_account_requires_a_login(client, user_id):
    r = client.get("/account", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/login")


def test_login_logout_roundtrip(client, user_id):
    bad = client.post("/login", data={"email": "anja@example.com", "password": "x"})
    assert bad.status_code == 401 and "wrong" in bad.text

    ok = client.post("/login", data={"email": "anja@example.com",
                                     "password": "supersecret123"})
    assert ok.status_code == 200 and "Your account" in ok.text
    assert "Not connected yet" in ok.text          # no Garmin tokens yet

    client.get("/logout")
    assert client.get("/account", follow_redirects=False).status_code == 302


def test_login_next_only_accepts_local_paths(client, user_id):
    r = client.post("/login", data={"email": "anja@example.com",
                                    "password": "supersecret123",
                                    "next": "https://evil.example/steal"},
                    follow_redirects=False)
    assert r.headers["location"] == "/account"


def test_account_shows_the_connector_url(client, user_id, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://mcp.garmin.example")
    client.post("/login", data={"email": "anja@example.com",
                                "password": "supersecret123"})
    page = client.get("/account")
    assert "https://mcp.garmin.example/mcp" in page.text


def test_delete_account_removes_everything(client, user_id):
    users.set_garmin_tokens(user_id, "{}", "{}", "Anja")
    client.post("/login", data={"email": "anja@example.com",
                                "password": "supersecret123"})
    r = client.post("/account/delete")
    assert r.status_code == 200 and "Log in" in r.text
    assert users.get_user(user_id) is None
    assert users.get_garmin_tokens(user_id) is None
