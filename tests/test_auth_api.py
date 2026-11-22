import pytest
from fastapi.testclient import TestClient

from steel_text.auth.main import app, get_settings
from steel_text.settings import Settings

CREDS = {"username": "minseung", "password": "correct-horse"}


@pytest.fixture
def settings(tmp_path):
    # HS256 wants a key of at least 32 bytes
    return Settings(
        _env_file=None, jwt_secret_key="test-secret-at-least-32-bytes-long", auth_db_path=tmp_path / "auth.db"
    )


@pytest.fixture
def client(settings, monkeypatch):
    monkeypatch.setitem(app.dependency_overrides, get_settings, lambda: settings)
    return TestClient(app)


def login(client):
    client.post("/auth/register", json=CREDS)
    return client.post("/auth/login", json=CREDS).json()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_register_login_and_access_protected_route(client):
    assert client.post("/auth/register", json=CREDS).status_code == 201
    tokens = client.post("/auth/login", json=CREDS).json()
    assert tokens["token_type"] == "bearer"
    r = client.get("/me", headers=bearer(tokens["access_token"]))
    assert r.status_code == 200 and r.json() == {"username": "minseung"}


def test_password_is_stored_hashed(client, settings):
    from steel_text.auth import db

    client.post("/auth/register", json=CREDS)
    user = db.get_user(settings.auth_db_path, "minseung")
    assert user is not None
    stored = user["hashed_password"]
    assert stored != CREDS["password"] and stored.startswith("$pbkdf2-sha256$")


def test_duplicate_username_is_rejected(client):
    client.post("/auth/register", json=CREDS)
    assert client.post("/auth/register", json=CREDS).status_code == 409


def test_wrong_password_and_unknown_user_get_the_same_error(client):
    client.post("/auth/register", json=CREDS)
    wrong_pw = client.post("/auth/login", json={**CREDS, "password": "wrong-password"})
    no_user = client.post("/auth/login", json={"username": "nobody", "password": "whatever-123"})
    assert wrong_pw.status_code == no_user.status_code == 401
    assert wrong_pw.json() == no_user.json()


def test_unknown_user_still_costs_a_password_check(client, monkeypatch):
    from steel_text.auth import main

    calls = []
    monkeypatch.setattr(main, "burn_password_check", lambda: calls.append(1))
    client.post("/auth/register", json=CREDS)
    client.post("/auth/login", json={**CREDS, "password": "wrong-password"})  # known user: a real check
    assert calls == []
    client.post("/auth/login", json={"username": "nobody", "password": "whatever-123"})
    assert calls == [1]


def test_username_is_trimmed_and_blank_is_rejected(client):
    assert client.post("/auth/register", json={**CREDS, "username": "   "}).status_code == 422
    r = client.post("/auth/register", json={**CREDS, "username": "  minseung  "})
    assert r.status_code == 201 and r.json() == {"username": "minseung"}
    assert client.post("/auth/login", json=CREDS).status_code == 200


def test_protected_route_requires_a_token(client):
    assert client.get("/me").status_code == 401
    assert client.get("/me", headers=bearer("not-a-jwt")).status_code == 401


def test_refresh_token_cannot_be_used_as_access_token(client):
    tokens = login(client)
    r = client.get("/me", headers=bearer(tokens["refresh_token"]))
    assert r.status_code == 401 and "access" in r.json()["detail"]


def test_refresh_issues_a_working_access_token(client):
    tokens = login(client)
    r = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200
    assert client.get("/me", headers=bearer(r.json()["access_token"])).status_code == 200


def test_access_token_cannot_be_used_to_refresh(client):
    tokens = login(client)
    assert client.post("/auth/refresh", json={"refresh_token": tokens["access_token"]}).status_code == 401


def test_expired_access_token_is_rejected(client, monkeypatch):
    from datetime import timedelta

    from steel_text.auth import security

    monkeypatch.setattr(security, "ACCESS_TTL", timedelta(seconds=-1))
    tokens = login(client)
    r = client.get("/me", headers=bearer(tokens["access_token"]))
    assert r.status_code == 401 and "expired" in r.json()["detail"]
