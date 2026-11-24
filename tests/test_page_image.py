import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from steel_text import page_image
from steel_text.auth.main import get_settings as auth_settings
from steel_text.auth.security import create_token
from steel_text.page_image import PageTooLargeError, open_page
from steel_text.settings import Settings
from steel_text.web import server


def png_bytes(width, height):
    buf = io.BytesIO()
    Image.new("L", (width, height)).save(buf, format="PNG")
    return buf.getvalue()


def test_page_is_opened_as_rgb():
    assert open_page(io.BytesIO(png_bytes(20, 10))).mode == "RGB"


def test_page_over_the_pixel_limit_is_refused(monkeypatch):
    monkeypatch.setattr(page_image, "MAX_PAGE_PIXELS", 100)
    with pytest.raises(PageTooLargeError, match="20 x 20 px"):
        open_page(io.BytesIO(png_bytes(20, 20)))


def test_upload_over_the_pixel_limit_is_a_413(monkeypatch):
    monkeypatch.setattr(page_image, "MAX_PAGE_PIXELS", 100)
    monkeypatch.setitem(server.app.dependency_overrides, server.live_settings, lambda: Settings(_env_file=None))
    auth = Settings(_env_file=None, jwt_secret_key="test-secret-at-least-32-bytes-long")
    monkeypatch.setitem(server.app.dependency_overrides, auth_settings, lambda: auth)  # uploads are live: log in
    login = {"Authorization": f"Bearer {create_token('tester', 'access', auth.jwt_secret_key)}"}
    r = TestClient(server.app).post("/api/run", params={"live": True}, content=png_bytes(20, 20), headers=login)
    assert r.status_code == 413 and "too large" in r.json()["detail"]
