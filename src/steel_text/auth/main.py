"""JWT authentication API.

uvicorn steel_text.auth.main:app --reload     then open http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import logging
import secrets
from functools import lru_cache
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, StringConstraints

from ..settings import Settings, load_settings
from . import db
from .security import (
    TokenError,
    burn_password_check,
    create_token,
    decode_token,
    hash_password,
    verify_password,
)

logger = logging.getLogger(__name__)

# Requests are served only when addressed to this machine by name. A web page elsewhere can point
# its own domain at 127.0.0.1 (DNS rebinding) and call the API from the visitor's browser; those
# requests carry that domain as their Host, so they are refused.
LOCAL_HOSTS = ["127.0.0.1", "localhost"]

app = FastAPI(
    title="Steel text extraction: auth API", description="Register, login, refresh, and a protected endpoint."
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)
bearer = HTTPBearer(auto_error=False)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read once per process: the signing key must not change while the server runs.

    Without JWT_SECRET_KEY, a random key is made here, so tokens stop working on restart.
    """
    settings = load_settings()
    if not settings.jwt_secret_key:
        logger.warning("JWT_SECRET_KEY not set: using a random key, tokens stop working on restart")
        settings = settings.model_copy(update={"jwt_secret_key": secrets.token_urlsafe(32)})
    return settings


SettingsDep = Annotated[Settings, Depends(get_settings)]


# -- request and response bodies --------------------------------------------------


class Credentials(BaseModel):
    # Spaces around the username are removed before the length check, so "   " is rejected.
    username: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    password: str = Field(min_length=8, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str


class User(BaseModel):
    username: str


class AccessToken(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"


class TokenPair(AccessToken):
    refresh_token: str


# -- routes ----------------------------------------------------------------------


def current_user(
    settings: SettingsDep, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)] = None
) -> str:
    """Dependency for protected routes: requires a valid *access* token."""
    if creds is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Missing bearer token", headers={"WWW-Authenticate": "Bearer"}
        )
    try:
        return decode_token(creds.credentials, "access", settings.jwt_secret_key)
    except TokenError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e), headers={"WWW-Authenticate": "Bearer"}) from e


@app.post("/auth/register", status_code=status.HTTP_201_CREATED)
def register(body: Credentials, settings: SettingsDep) -> User:
    if not db.create_user(settings.auth_db_path, body.username, hash_password(body.password)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Username already exists")
    return User(username=body.username)


@app.post("/auth/login")
def login(body: Credentials, settings: SettingsDep) -> TokenPair:
    user = db.get_user(settings.auth_db_path, body.username)
    if user is None:
        burn_password_check()
        password_ok = False
    else:
        password_ok = verify_password(body.password, user["hashed_password"])
    # Same error, and the same time, for an unknown user and a wrong password:
    # neither may reveal which usernames exist.
    if user is None or not password_ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
    return TokenPair(
        access_token=create_token(user["username"], "access", settings.jwt_secret_key),
        refresh_token=create_token(user["username"], "refresh", settings.jwt_secret_key),
    )


@app.post("/auth/refresh")
def refresh(body: RefreshRequest, settings: SettingsDep) -> AccessToken:
    try:
        username = decode_token(body.refresh_token, "refresh", settings.jwt_secret_key)
    except TokenError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e)) from e
    return AccessToken(access_token=create_token(username, "access", settings.jwt_secret_key))


@app.get("/me")
def me(username: Annotated[str, Depends(current_user)]) -> User:
    """Protected: only reachable with a valid access token."""
    return User(username=username)
