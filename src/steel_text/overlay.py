"""Draw tiles and text boxes for the demo images.

The step images are drawn on a Preview: the page shrunk once to at most 3,000 px,
with page boxes mapped onto it. Drawing on the full page and shrinking each image
afterwards cost seconds and gigabytes on a 14,400 px page. Sizes and line widths
below are in preview pixels. The dedup close-ups are cut from the full page.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from PIL import Image, ImageDraw

from .dedup import DedupDecision
from .fonts import load_font
from .geometry import BBox, intersection_area, region_around
from .ocr_types import OcrChunk

TILE_COLORS = [(230, 57, 70), (29, 120, 116), (69, 123, 157), (244, 162, 97), (131, 56, 236), (42, 157, 143)]
NOT_READ_COLOR = (198, 40, 40)  # red, as in the web UI
PREVIEW_MAX_SIDE = 3000  # full-size 9,000 px PNGs are slow to write and to open


@dataclass(frozen=True)
class Preview:
    """The page shrunk for the step images, and the scale that maps page boxes onto it.

    When the page is already small enough, `image` is the page itself, so the
    draw functions below always draw on a copy.
    """

    image: Image.Image
    scale: float

    @classmethod
    def of(cls, page: Image.Image, max_side: int = PREVIEW_MAX_SIDE) -> Preview:
        scale = min(1.0, max_side / max(page.size))
        if scale < 1.0:
            page = page.resize((round(page.width * scale), round(page.height * scale)), Image.Resampling.LANCZOS)
        return cls(page, scale)

    def box(self, b: BBox) -> BBox:
        """A box in page pixels, in preview pixels."""
        x0, y0, x1, y1 = (round(v * self.scale) for v in b)
        return (x0, y0, x1, y1)


def draw_not_read(img: Image.Image, boxes: Iterable[BBox]) -> Image.Image:
    """Hatch the areas no tile could read in red, so missing text can't pass for an empty area."""
    base = img.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font = load_font(46)
    for x0, y0, x1, y1 in boxes:
        hatch = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
        lines = ImageDraw.Draw(hatch)
        for d in range(-hatch.height, hatch.width, 30):  # 45-degree stripes, 30 px apart
            lines.line([(d, hatch.height), (d + hatch.height, 0)], fill=(*NOT_READ_COLOR, 110), width=5)
        layer.alpha_composite(hatch, (x0, y0))
        draw.rectangle([x0, y0, x1 - 1, y1 - 1], outline=(*NOT_READ_COLOR, 255), width=6)
        draw.text((x0 + 13, y0 + 10), "NOT READ", fill=(*NOT_READ_COLOR, 255), font=font)
    return Image.alpha_composite(base, layer).convert("RGB")


def draw_tile_grid(img: Image.Image, tiles: Sequence[BBox]) -> Image.Image:
    """Each tile gets a translucent fill, so the overlap bands show up darker."""
    base = img.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font = load_font(46)
    for i, (x0, y0, x1, y1) in enumerate(tiles):
        r, g, b = TILE_COLORS[i % len(TILE_COLORS)]
        draw.rectangle([x0, y0, x1, y1], fill=(r, g, b, 40), outline=(r, g, b, 255), width=5)
        draw.text((x0 + 13, y0 + 10), f"tile {i}", fill=(r, g, b, 255), font=font)
    return Image.alpha_composite(base, layer).convert("RGB")


def draw_boxes(
    img: Image.Image, items: Iterable[tuple[BBox, str]], color: tuple[int, int, int], font_size: int = 15
) -> Image.Image:
    img = img.convert("RGB")  # always a new image, so the one passed in is never drawn on
    draw = ImageDraw.Draw(img)
    font = load_font(font_size)
    for bbox, label in items:
        x0, y0, x1, y1 = bbox
        draw.rectangle([x0 - 2, y0 - 2, x1 + 2, y1 + 2], outline=color, width=3)
        if label:
            ty = max(0, y0 - font_size - 6)
            tb = draw.textbbox((x0, ty), label, font=font)
            draw.rectangle([tb[0] - 2, tb[1] - 1, tb[2] + 2, tb[3] + 1], fill=(255, 255, 255))
            draw.text((x0, ty), label, fill=color, font=font)
    return img


def _tile_color(i: int) -> tuple[int, int, int]:
    return TILE_COLORS[i % len(TILE_COLORS)]


def _crop_with_boxes(
    page: Image.Image, region: BBox, chunks: Sequence[OcrChunk], color: tuple[int, int, int] | None = None
) -> Image.Image:
    img = page.crop(region).convert("RGB")
    draw = ImageDraw.Draw(img)
    for c in chunks:
        x0, y0, x1, y1 = c.bbox
        draw.rectangle(
            [x0 - region[0] - 4, y0 - region[1] - 4, x1 - region[0] + 4, y1 - region[1] + 4],
            outline=color or _tile_color(c.tile_index),
            width=6,
        )
    return img


def _fit(img: Image.Image, max_w: int, max_h: int) -> Image.Image:
    scale = min(max_w / img.width, max_h / img.height)
    return img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.Resampling.LANCZOS)


def draw_dedup_closeups(
    page: Image.Image,
    raw: Sequence[OcrChunk],
    merged: Sequence[OcrChunk],
    decisions: Sequence[DedupDecision],
    *,
    pad: int = 120,
) -> Image.Image:
    """One row per dedup decision: raw OCR boxes (coloured by tile) on the left, the merged result on the right."""
    panel_w, cell_w, cell_h, cap_h, gap = 1900, 860, 230, 92, 36
    title_font, font = load_font(34), load_font(25)
    height = 90 + len(decisions) * (cell_h + cap_h + gap)
    panel = Image.new("RGB", (panel_w, height), "white")
    draw = ImageDraw.Draw(panel)
    draw.text(
        (30, 24), "Dedup close-ups: before (raw OCR, colour = tile)  ->  after (merged)", fill="black", font=title_font
    )

    y = 90
    for n, d in enumerate(decisions, 1):
        region = region_around(d.kept.bbox, d.dropped.bbox, pad, page.width, page.height)
        before_chunks = [c for c in raw if intersection_area(c.bbox, region) > 0]
        after_chunks = [c for c in merged if intersection_area(c.bbox, region) > 0]

        before = _fit(_crop_with_boxes(page, region, before_chunks), cell_w, cell_h)
        after = _fit(_crop_with_boxes(page, region, after_chunks, color=(22, 140, 60)), cell_w, cell_h)
        draw.text((30, y + cell_h // 2 - 18), f"{n}.", fill="black", font=title_font)
        panel.paste(before, (90, y))
        draw.text((90 + cell_w + 30, y + cell_h // 2 - 22), "->", fill="black", font=title_font)
        panel.paste(after, (90 + cell_w + 110, y))
        draw.rectangle([90, y, 90 + before.width, y + before.height], outline=(200, 200, 200), width=2)
        draw.rectangle(
            [90 + cell_w + 110, y, 90 + cell_w + 110 + after.width, y + after.height], outline=(200, 200, 200), width=2
        )

        cy, cx = y + cell_h + 10, 90
        draw.text((cx, cy), "before:", fill="black", font=font)
        cx += int(draw.textlength("before: ", font=font))
        for c in before_chunks:
            seg = f"'{c.text}' (tile {c.tile_index})   "
            draw.text((cx, cy), seg, fill=_tile_color(c.tile_index), font=font)
            cx += int(draw.textlength(seg, font=font))
        draw.text(
            (90, cy + 36),
            f"after: kept '{d.kept.text}', dropped '{d.dropped.text}'  [{d.reason}]",
            fill=(22, 110, 50),
            font=font,
        )
        y += cell_h + cap_h + gap
    return panel
