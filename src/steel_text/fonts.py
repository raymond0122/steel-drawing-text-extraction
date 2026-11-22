"""Font loading for the synthetic drawing and the demo overlays."""

from __future__ import annotations

from PIL import ImageFont as _ImageFont

ImageFont = _ImageFont.FreeTypeFont | _ImageFont.ImageFont  # what load_font returns

# Tried in order. Which of these exists depends on the operating system.
FONT_CANDIDATES = ("arial.ttf", "Arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf")


def load_font(size: int) -> ImageFont:
    """Return the first font in FONT_CANDIDATES that is installed, else Pillow's built-in font.

    Glyph widths differ between these fonts, so the synthetic drawing's text boxes,
    and where tile edges cut them, differ slightly between operating systems.
    """
    for name in FONT_CANDIDATES:
        try:
            return _ImageFont.truetype(name, size)
        except OSError:
            continue
    return _ImageFont.load_default(size=size)
