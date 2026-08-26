"""Encryption at rest for the Garmin tokens of other people.

Same scheme as MyFITContainer: Fernet with a key derived from the application
secret. That protects a stolen backup or volume snapshot - it does NOT protect
against someone who owns the server, because the key sits in the same .env.
Say so out loud rather than pretending otherwise.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

ENV_SECRET = "APP_SECRET"


class MissingSecret(RuntimeError):
    """No APP_SECRET - the server must refuse to start rather than store
    other people's Garmin tokens in the clear."""


def _fernet() -> Fernet:
    secret = os.environ.get(ENV_SECRET, "")
    if not secret:
        raise MissingSecret(
            f"{ENV_SECRET} is not set. Generate one with "
            "`python -c \"import secrets; print(secrets.token_urlsafe(32))\"` "
            "and put it in the .env - without it stored tokens cannot be encrypted.")
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str | None:
    """None when the ciphertext does not belong to the current APP_SECRET -
    changing the secret makes stored tokens unreadable, and the user simply
    reconnects."""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None
