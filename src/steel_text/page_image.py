"""Open a page image with a size limit, so one upload can't exhaust the server's memory, and shrink one for display.

PIL's default limit (about 89 million pixels) is too small for large drawings, so
it is raised to ours rather than switched off: an A0 sheet at 400 dpi is about
250 million pixels. PIL has only a process-wide setting for this, so it is set
once, on import: setting it around each call would race between the web
server's threads.
"""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from PIL import Image

MAX_PAGE_PIXELS = 300_000_000
Image.MAX_IMAGE_PIXELS = MAX_PAGE_PIXELS  # PIL refuses anything over twice this by itself


class PageTooLargeError(ValueError):
    pass


def open_page(source: str | Path | BinaryIO) -> Image.Image:
    """Open a page image as grayscale ("L") or RGB, refusing one over MAX_PAGE_PIXELS before it is decoded.

    A grayscale page stays grayscale: as RGB it would hold each pixel's one value
    three times, three times the memory for nothing. A 1-bit scan becomes "L", and a
    16-bit one is scaled to 8 bits: a plain conversion would turn every value over
    255, nearly all of a scan, white. A transparent background becomes white, the
    paper it stands for: dropping the transparency would leave the colour stored
    under it, usually black. Every other page is opened as RGB. Raises
    PageTooLargeError, or OSError for a file PIL can't read.
    """
    try:
        img = Image.open(source)  # reads only the header
    except Image.DecompressionBombError as e:
        raise PageTooLargeError(str(e)) from e
    with img:  # closes a file PIL opened itself; the decoded pixels stay usable
        if img.width * img.height > MAX_PAGE_PIXELS:
            raise PageTooLargeError(f"{img.width} x {img.height} px is over the limit of {MAX_PAGE_PIXELS:,} pixels")
        if img.mode in ("L", "RGB") and not img.has_transparency_data:
            img.load()  # convert() would decode the page and then copy it: twice the memory at its peak
            return img
        return _to_gray_or_rgb(img)


def _to_gray_or_rgb(img: Image.Image) -> Image.Image:
    if img.mode.startswith("I"):  # 16-bit grayscale: 65,535 is white
        return img.point(lambda v: v * (1 / 257)).convert("L")
    mode = "L" if img.mode in ("1", "L", "LA") else "RGB"
    if not img.has_transparency_data:
        return img.convert(mode)
    alpha = img.getchannel("A") if "A" in img.getbands() else img.convert("RGBA").getchannel("A")
    page = Image.new(mode, img.size, "white")
    page.paste(img.convert(mode), mask=alpha)
    return page


def shrink(page: Image.Image, max_side: int) -> tuple[Image.Image, float]:
    """The page shrunk so its longer side is at most max_side, and the scale used.

    A page already that small comes back as itself, at scale 1. BOX (each new pixel
    the average of the ones it covers) is enough for a preview, and about 4x faster
    than LANCZOS on a 9,000 px page.
    """
    scale = min(1.0, max_side / max(page.size))
    if scale == 1.0:
        return page, scale
    size = (max(1, round(page.width * scale)), max(1, round(page.height * scale)))
    return page.resize(size, Image.Resampling.BOX), scale
