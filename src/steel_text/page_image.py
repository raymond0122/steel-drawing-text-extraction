"""Open a page image with a size limit, so one upload can't exhaust the server's memory.

PIL's default limit (about 89 million pixels) is too small for large drawings, so
it is raised to ours rather than switched off: an A0 sheet at 400 dpi is about
250 million pixels.
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
    """Open a page image as RGB, refusing one over MAX_PAGE_PIXELS before its pixels are decoded.

    Raises PageTooLargeError, or OSError for a file PIL can't read.
    """
    try:
        img = Image.open(source)  # reads only the header
    except Image.DecompressionBombError as e:
        raise PageTooLargeError(str(e)) from e
    if img.width * img.height > MAX_PAGE_PIXELS:
        raise PageTooLargeError(f"{img.width} x {img.height} px is over the limit of {MAX_PAGE_PIXELS:,} pixels")
    return img.convert("RGB")
