"""Password hashing and JWT helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

import jwt
from passlib.context import CryptContext

ACCESS_TTL = timedelta(minutes=60)
REFRESH_TTL = timedelta(days=7)
ALGORITHM = "HS256"

TokenKind = Literal["access", "refresh"]

_pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")  # salted, iterated hash


class TokenError(Exception):
    pass


def hash_password(password: str) -> str:
    return _pwd.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return _pwd.verify(password, hashed)


def create_token(username: str, kind: TokenKind, secret: str) -> str:
    """The kind is stored in the token so an access and a refresh token cannot be swapped."""
    ttl = ACCESS_TTL if kind == "access" else REFRESH_TTL
    payload = {"sub": username, "type": kind, "exp": datetime.now(timezone.utc) + ttl}
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_token(token: str, expected_kind: TokenKind, secret: str) -> str:
    """Return the username, or raise TokenError."""
    try:
        payload = jwt.decode(token, secret, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as e:
        raise TokenError("Token has expired") from e
    except jwt.InvalidTokenError as e:
        raise TokenError("Invalid token") from e
    if payload.get("type") != expected_kind:
        raise TokenError(f"Wrong token type: expected an {expected_kind!r} token")
    username = payload.get("sub")
    if not isinstance(username, str) or not username:
        raise TokenError("Token has no subject")
    return username
