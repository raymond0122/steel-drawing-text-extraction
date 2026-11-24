"""Open a page image with a size limit, so one upload can't exhaust the server's memory.

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
    """Open a page image as RGB, refusing one over MAX_PAGE_PIXELS before its pixels are decoded.

    Raises PageTooLargeError, or OSError for a file PIL can't read.
    """
    try:
        img = Image.open(source)  # reads only the header
    except Image.DecompressionBombError as e:
        raise PageTooLargeError(str(e)) from e
    with img:  # closes a file PIL opened itself; the decoded pixels stay usable
        if img.width * img.height > MAX_PAGE_PIXELS:
            raise PageTooLargeError(f"{img.width} x {img.height} px is over the limit of {MAX_PAGE_PIXELS:,} pixels")
        if img.mode == "RGB":
            img.load()  # convert() would decode the page and then copy it: twice the memory at its peak
            return img
        return img.convert("RGB")
