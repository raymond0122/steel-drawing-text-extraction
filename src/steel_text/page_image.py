"""Open a page image with a size limit, so an upload can't exhaust memory.

Large drawings exceed PIL's default limit (about 89 million pixels), so the
limit is raised here rather than switched off: an A0 sheet scanned at 400 dpi
is about 250 million pixels.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import BinaryIO

from PIL import Image

MAX_PAGE_PIXELS = 300_000_000
Image.MAX_IMAGE_PIXELS = MAX_PAGE_PIXELS  # PIL itself refuses anything over twice this


class PageTooLargeError(ValueError):
    pass


def open_page(source: str | Path | BinaryIO) -> Image.Image:
    """Open a page image as RGB, refusing anything over MAX_PAGE_PIXELS.

    Image.open reads only the header, so the size is checked before the pixels
    are decoded. Raises PageTooLargeError, or OSError for a file PIL can't read.
    """
    try:
        with warnings.catch_warnings():  # sizes between the two limits warn; the check below decides
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            img = Image.open(source)
    except Image.DecompressionBombError as e:
        raise PageTooLargeError(str(e)) from e
    width, height = img.size
    if width * height > MAX_PAGE_PIXELS:
        raise PageTooLargeError(f"{width} x {height} px is over the limit of {MAX_PAGE_PIXELS:,} pixels")
    return img.convert("RGB")
