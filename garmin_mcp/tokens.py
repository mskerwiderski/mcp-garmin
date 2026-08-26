"""Persistence for the Garmin OAuth1/OAuth2 token pair.

Login deliberately does NOT happen on the server. Garmin's SSO sits behind
Cloudflare, which since March 2026 answers fresh logins from datacenter IPs
with 429/403; the login runs on the user's own machine (see cli.py) and only
the resulting tokens travel to the server. OAuth1 is valid for about a year
and mints OAuth2 access tokens against connectapi.garmin.com, so the server
never touches sso.garmin.com.

Two sources, in this order:
  GARMIN_TOKENS      base64 of the token JSON (for hosts without a disk)
  GARMIN_TOKENS_FILE or ~/.garmin-mcp/tokens.json (mode 0600)
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path

ENV_BLOB = "GARMIN_TOKENS"
ENV_FILE = "GARMIN_TOKENS_FILE"


@dataclass
class Tokens:
    oauth1: dict
    oauth2: dict
    account: str = ""

    def as_json_pair(self) -> tuple[str, str]:
        """What GarminClient.restore() expects: two JSON strings."""
        return json.dumps(self.oauth1), json.dumps(self.oauth2)

    def to_dict(self) -> dict:
        return {"oauth1": self.oauth1, "oauth2": self.oauth2, "account": self.account}


def token_path() -> Path:
    env = os.environ.get(ENV_FILE)
    if env:
        return Path(env).expanduser()
    return Path.home() / ".garmin-mcp" / "tokens.json"


def _parse(raw: dict) -> Tokens:
    o1, o2 = raw.get("oauth1"), raw.get("oauth2")
    if not isinstance(o1, dict) or not isinstance(o2, dict):
        raise ValueError("token file has no oauth1/oauth2 objects")
    return Tokens(oauth1=o1, oauth2=o2, account=str(raw.get("account") or ""))


def load() -> Tokens | None:
    """Tokens from env or disk; None when nothing is stored yet."""
    blob = os.environ.get(ENV_BLOB)
    if blob:
        return _parse(json.loads(base64.b64decode(blob)))
    path = token_path()
    if not path.exists():
        return None
    return _parse(json.loads(path.read_text("utf-8")))


def save(tokens: Tokens) -> Path:
    """Write to disk with 0600. Never called on the server."""
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(tokens.to_dict(), indent=2), "utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return path


def export_blob(tokens: Tokens) -> str:
    """base64 for the GARMIN_TOKENS environment variable."""
    return base64.b64encode(
        json.dumps(tokens.to_dict()).encode("utf-8")).decode("ascii")


def from_json_pair(oauth1_json: str, oauth2_json: str, account: str = "") -> Tokens:
    return Tokens(json.loads(oauth1_json), json.loads(oauth2_json), account)
