import pytest

from steel_text import geometry as g
from steel_text.tiling import MAX_TILE_SIZE, OVERLAP, TileLimitError, choose_tiling, make_tiles


def test_poly_to_bbox_accepts_both_azure_formats():
    flat = [10, 20, 110, 20, 110, 60, 10, 60]
    points = [{"x": 10, "y": 20}, {"x": 110, "y": 20}, {"x": 110, "y": 60}, {"x": 10, "y": 60}]
    assert g.poly_to_bbox(flat) == (10, 20, 110, 60)
    assert g.poly_to_bbox(points) == (10, 20, 110, 60)
    assert g.poly_to_bbox([]) is None


def test_poly_to_bbox_encloses_corners_that_fall_between_pixels():
    assert g.poly_to_bbox([10.6, 20.4, 110.2, 20.4, 110.2, 60.7, 10.6, 60.7]) == (10, 20, 111, 61)


def test_overlap_ratio_catches_fragment_that_iou_misses():
    long_text = (0, 0, 1000, 50)
    fragment = (0, 0, 200, 50)  # fully inside the long box
    assert g.iou(long_text, fragment) == 0.2
    assert g.overlap_ratio_min(long_text, fragment) == 1.0


def test_tiles_cover_whole_image_with_overlap():
    w, h = 9000, 6000
    tiles = make_tiles(w, h, tile_size=3500, overlap=300)
    assert len(tiles) == 6
    for x in range(0, w, 250):
        for y in range(0, h, 250):
            assert any(t.x0 <= x < t.x1 and t.y0 <= y < t.y1 for t in tiles), (x, y)
    xs = sorted({(t.x0, t.x1) for t in tiles})
    assert xs[0][1] - xs[1][0] == 300  # neighbouring columns overlap by 300 px


def test_small_page_is_not_tiled():
    tile_size, overlap, reason = choose_tiling(3000, 2000)
    assert overlap == 0 and "sent whole" in reason
    assert [t.box for t in make_tiles(3000, 2000, tile_size=tile_size, overlap=overlap)] == [(0, 0, 3000, 2000)]


def test_a_page_larger_than_one_tile_is_cut_into_3500_px_tiles():
    assert choose_tiling(3500, 2000).overlap == 0  # fits in one tile
    assert choose_tiling(3600, 2000)[:2] == (3500, 350)
    assert choose_tiling(9000, 6000)[:2] == (3500, 350)
    assert "3,500 px tiles" in choose_tiling(9000, 6000).reason
    assert choose_tiling(9000, 6000, overlap=500)[:2] == (3500, 500)


def test_the_overlap_is_longer_than_the_longest_beam_label():
    import json
    from pathlib import Path

    answer_key = Path(__file__).resolve().parents[1] / "experiments" / "tiling" / "ground_truth.json"
    labels = json.loads(answer_key.read_text(encoding="utf-8"))
    longest = max(max(g["bbox"][2] - g["bbox"][0], g["bbox"][3] - g["bbox"][1]) for g in labels)
    assert longest == 342 and OVERLAP > longest  # the rule behind OVERLAP, on the test sheet


def test_tile_size_grows_until_under_the_cap():
    tile_size, overlap, reason = choose_tiling(14400, 9600, max_tiles=4)
    assert 3500 < tile_size <= MAX_TILE_SIZE and "grew" in reason
    assert len(make_tiles(14400, 9600, tile_size=tile_size, overlap=overlap)) <= 4


def test_tiles_never_grow_past_what_azure_accepts():
    assert choose_tiling(14400, 9600, max_tiles=2).tile_size == MAX_TILE_SIZE  # two 10,000 px tiles cover it
    with pytest.raises(TileLimitError, match="at least 2 tiles"):
        choose_tiling(14400, 9600, max_tiles=1)  # one tile would have to be 14,400 px
