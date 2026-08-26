"""The HTTP transport as a remote client sees it, and the tenancy boundary."""
import pytest
from starlette.testclient import TestClient

from garmin_mcp import oauth, users
from garmin_mcp.server import build_http_app

HEADERS = {"Accept": "application/json, text/event-stream",
           "Content-Type": "application/json",
           "MCP-Protocol-Version": "2025-06-18"}

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1"}}}


@pytest.fixture
def client():
    with TestClient(build_http_app(), base_url="https://testserver") as c:
        yield c


def _auth(token):
    return {**HEADERS, "Authorization": f"Bearer {token}"}


def _token_for(user_id: int) -> str:
    return oauth._mint_pair("client-1", user_id, "mcp")["access_token"]


def test_health(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_no_token_is_401(client):
    assert client.post("/mcp", json=INIT, headers=HEADERS).status_code == 401


def test_unknown_token_is_401(client, user_id):
    assert client.post("/mcp", json=INIT, headers=_auth("made-up")).status_code == 401


def test_oauth_token_initializes(client, user_id):
    r = client.post("/mcp", json=INIT, headers=_auth(_token_for(user_id)))
    assert r.status_code == 200
    assert r.json()["result"]["serverInfo"]["name"] == "garmin-connect"


def test_tools_list_over_http(client, user_id):
    token = _token_for(user_id)
    client.post("/mcp", json=INIT, headers=_auth(token))
    r = client.post("/mcp", headers=_auth(token),
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert {"list_activities", "get_daily_health", "get_challenge"} <= names


def test_a_user_without_garmin_gets_a_useful_error(client, user_id):
    token = _token_for(user_id)
    client.post("/mcp", json=INIT, headers=_auth(token))
    r = client.post("/mcp", headers=_auth(token), json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "whoami", "arguments": {}}})
    assert "not connected yet" in str(r.json())


def test_two_accounts_never_see_each_other(client, user_id, monkeypatch):
    """The tenancy boundary: the same process, two bearers, two identities."""
    other = users.create_user("bob@example.com", "anotherlongpw",
                              users.create_invite("Bob"))
    users.set_garmin_tokens(user_id, '{"oauth_token": "anja"}', "{}", "Anja")
    users.set_garmin_tokens(other, '{"oauth_token": "bob"}', "{}", "Bob")

    seen = []
    from garmin_mcp import session as sessions

    class Stub:
        def __init__(self, account):
            self.account = account

        async def client(self):
            seen.append(self.account)
            raise sessions.NotConnected(f"stub for {self.account}")

    def fake_session_for_user(uid):
        return Stub(users.get_garmin_tokens(uid)[2])

    monkeypatch.setattr(sessions, "session_for_user", fake_session_for_user)

    for uid in (user_id, other):
        token = _token_for(uid)
        client.post("/mcp", json=INIT, headers=_auth(token))
        client.post("/mcp", headers=_auth(token), json={
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "whoami", "arguments": {}}})
    assert seen == ["Anja", "Bob"]
