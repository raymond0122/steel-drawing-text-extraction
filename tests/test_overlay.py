from PIL import Image

from steel_text.dedup import DedupDecision
from steel_text.ocr_types import OcrChunk
from steel_text.overlay import Preview, draw_boxes, draw_dedup_closeups, draw_not_read, draw_tile_grid


def test_large_page_is_shrunk_once_and_boxes_follow():
    preview = Preview.of(Image.new("RGB", (900, 600)), max_side=300)
    assert preview.image.size == (300, 200) and preview.scale == 300 / 900
    assert preview.box((30, 60, 330, 90)) == (10, 20, 110, 30)


def test_small_page_is_its_own_preview_and_is_never_drawn_on():
    page = Image.new("RGB", (300, 200), "white")
    preview = Preview.of(page)
    assert preview.image is page and preview.scale == 1.0  # not copied...
    draw_boxes(preview.image, [((10, 10, 50, 30), "C50")], (255, 0, 0))
    draw_tile_grid(preview.image, [(0, 0, 300, 200)])
    draw_not_read(preview.image, [(0, 0, 150, 200)])
    assert page.getcolors() == [(300 * 200, (255, 255, 255))]  # ...so every draw function works on a copy


def test_a_grayscale_page_gets_coloured_boxes():
    preview = Preview.of(Image.new("L", (300, 200), 255))
    boxes = draw_boxes(preview.image, [((10, 10, 50, 30), "C50")], (255, 0, 0))
    assert boxes.mode == "RGB" and boxes.getpixel((8, 20)) == (255, 0, 0)  # on the box's left edge
    assert draw_tile_grid(preview.image, [(0, 0, 300, 200)]).mode == "RGB"
    assert draw_not_read(preview.image, [(0, 0, 150, 200)]).mode == "RGB"


def test_closeup_panel_draws_at_most_max_rows():
    page = Image.new("RGB", (400, 200), "white")
    chunks = [OcrChunk(f"T{i}", (10 + 100 * i, 50, 60 + 100 * i, 80), tile_index=i % 2) for i in range(3)]
    decisions = [DedupDecision(dropped=c, kept=c, reason="test") for c in chunks]
    two = draw_dedup_closeups(page, chunks, chunks, decisions[:2])
    capped = draw_dedup_closeups(page, chunks, chunks, decisions, max_rows=2)
    full = draw_dedup_closeups(page, chunks, chunks, decisions)
    assert two.height < capped.height < full.height  # two rows, plus a line counting the one left out
