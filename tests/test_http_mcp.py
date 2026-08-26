"""The HTTP transport as a remote client sees it: bearer, then JSON-RPC."""
import pytest
from starlette.testclient import TestClient

from garmin_mcp import oauth
from garmin_mcp.server import build_http_app

HEADERS = {"Accept": "application/json, text/event-stream",
           "Content-Type": "application/json",
           "MCP-Protocol-Version": "2025-06-18"}

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1"}}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MCP_TOKEN", "s3cret")
    oauth.STORE.tokens.clear()
    with TestClient(build_http_app()) as c:
        yield c


def _auth(token="s3cret"):
    return {**HEADERS, "Authorization": f"Bearer {token}"}


def test_health(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_no_token_is_401(client):
    assert client.post("/mcp", json=INIT, headers=HEADERS).status_code == 401


def test_wrong_token_is_401(client):
    assert client.post("/mcp", json=INIT, headers=_auth("nope")).status_code == 401


def test_static_token_initializes(client):
    r = client.post("/mcp", json=INIT, headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["serverInfo"]["name"] == "garmin-connect"
    assert "Garmin Connect" in body["result"]["instructions"]


def test_tools_list_over_http(client):
    client.post("/mcp", json=INIT, headers=_auth())
    r = client.post("/mcp", headers=_auth(),
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert "list_activities" in names and "get_daily_health" in names


def test_an_oauth_access_token_also_works(client):
    minted = oauth._mint_pair("client-1", "mcp")
    r = client.post("/mcp", json=INIT, headers=_auth(minted["access_token"]))
    assert r.status_code == 200


def test_a_tool_without_tokens_says_so(client):
    client.post("/mcp", json=INIT, headers=_auth())
    r = client.post("/mcp", headers=_auth(), json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "whoami", "arguments": {}}})
    body = r.json()
    text = str(body)
    assert "garmin-mcp login" in text
