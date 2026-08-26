import base64
import json
import os

from garmin_mcp import tokens


def _sample() -> tokens.Tokens:
    return tokens.Tokens(
        oauth1={"oauth_token": "t1", "oauth_token_secret": "s1", "mfa_token": None},
        oauth2={"access_token": "a", "refresh_token": "r", "expires_in": 3600,
                "expires_at": 1.0},
        account="Test Athlete")


def test_save_load_roundtrip():
    path = tokens.save(_sample())
    assert oct(os.stat(path).st_mode)[-3:] == "600"
    loaded = tokens.load()
    assert loaded.account == "Test Athlete"
    assert loaded.oauth1["oauth_token"] == "t1"
    o1, o2 = loaded.as_json_pair()
    assert json.loads(o1)["oauth_token_secret"] == "s1"
    assert json.loads(o2)["access_token"] == "a"


def test_env_blob_wins_over_file(monkeypatch):
    tokens.save(_sample())
    other = _sample()
    other.account = "From Env"
    monkeypatch.setenv(tokens.ENV_BLOB, tokens.export_blob(other))
    assert tokens.load().account == "From Env"


def test_export_blob_is_base64_json():
    blob = tokens.export_blob(_sample())
    assert json.loads(base64.b64decode(blob))["account"] == "Test Athlete"


def test_load_without_anything_is_none():
    assert tokens.load() is None
