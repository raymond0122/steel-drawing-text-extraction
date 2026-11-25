import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from steel_text import page_image
from steel_text.auth.main import get_settings as auth_settings
from steel_text.auth.security import create_token
from steel_text.page_image import PageTooLargeError, open_page, shrink
from steel_text.settings import Settings
from steel_text.web import server


def png_bytes(width, height, mode="L"):
    return png(Image.new(mode, (width, height), "white")).getvalue()


def png(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


@pytest.mark.parametrize(
    "mode, opened_as, white",
    [
        ("L", "L", 255),  # grayscale stays grayscale: a third of the memory
        ("1", "L", 255),
        ("RGB", "RGB", (255, 255, 255)),
        ("RGBA", "RGB", (255, 255, 255)),
        ("LA", "L", 255),
        ("P", "RGB", (255, 255, 255)),
    ],
)
def test_page_is_opened_as_grayscale_or_rgb(mode, opened_as, white):
    page = open_page(io.BytesIO(png_bytes(20, 10, mode)))
    assert page.mode == opened_as and page.size == (20, 10)
    assert page.getpixel((0, 0)) == white  # decoded, not just the header


@pytest.mark.parametrize("mode, opened_as", [("RGBA", "RGB"), ("LA", "L"), ("P", "RGB")])
def test_a_transparent_background_becomes_white_paper_with_the_ink_kept(mode, opened_as):
    img = Image.new(mode, (20, 10))  # every pixel 0: black, and transparent where there is alpha
    if mode == "P":
        img.info["transparency"] = 0  # palette entry 0 is the transparent one
        img.putpalette([0, 0, 0, 10, 10, 10])
        img.putpixel((5, 5), 1)  # ink: palette entry 1, opaque
    else:
        img.putpixel((5, 5), (10, 10, 10, 255) if mode == "RGBA" else (10, 255))
    page = open_page(png(img))
    ink = (10, 10, 10) if opened_as == "RGB" else 10
    paper = (255, 255, 255) if opened_as == "RGB" else 255
    assert page.mode == opened_as and page.getpixel((5, 5)) == ink and page.getpixel((0, 0)) == paper


def test_a_16_bit_scan_is_scaled_not_clipped():
    img = Image.new("I;16", (20, 10), 60_000)  # paper
    img.putpixel((5, 5), 3_000)  # ink
    page = open_page(png(img))
    assert page.mode == "L" and page.getpixel((0, 0)) == 233 and page.getpixel((5, 5)) == 11


def test_page_file_is_closed_once_opened(tmp_path):
    path = tmp_path / "page.png"
    path.write_bytes(png_bytes(20, 10, "RGB"))
    page = open_page(path)
    path.unlink()  # Windows refuses to delete a file that is still open
    assert page.getpixel((5, 5)) == (255, 255, 255)


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
    client = TestClient(server.app, base_url="http://127.0.0.1")
    r = client.post("/api/run", params={"live": True}, content=png_bytes(20, 20), headers=login)
    assert r.status_code == 413 and "too large" in r.json()["detail"]


def test_a_page_is_shrunk_to_the_side_asked_for_and_stays_grayscale():
    small, scale = shrink(Image.new("L", (9000, 6000)), 4000)
    assert small.size == (4000, 2667) and small.mode == "L" and scale == 4000 / 9000
    page = Image.new("L", (300, 200))
    same, scale = shrink(page, 4000)
    assert same is page and scale == 1.0  # already small enough: not copied
