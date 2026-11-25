import pytest
from PIL import Image

from steel_text.dedup import DedupDecision, merge_tile_results
from steel_text.mock_clients import SimulatedOcr
from steel_text.ocr_stage import run_tiled_ocr
from steel_text.ocr_types import OcrChunk
from steel_text.sample_drawing import GroundTruthText
from steel_text.tiling import Tile, TilingChoice, make_tiles

# A 9,000 x 6,000 px page in 3,500 px tiles with a 300 px overlap:  x: [0,3500] [3200,6700] [5500,9000]
#                                                                    y: [0,3500] [2500,6000]
# t0 t1 t2 on the top row, t3 t4 t5 on the bottom row.
TILES = make_tiles(9000, 6000, tile_size=3500, overlap=300)


def texts(chunks):
    return sorted(c.text for c in chunks)


def test_exact_duplicate_from_two_tiles_is_merged():
    a = OcrChunk("HSS152X152X9.5", (5650, 1500, 6270, 1580), tile_index=1)
    b = OcrChunk("HSS152X152X9.5", (5651, 1499, 6269, 1581), tile_index=2)
    log: list[DedupDecision] = []
    assert texts(merge_tile_results([a, b], TILES, log=log)) == ["HSS152X152X9.5"]
    assert log[0].reason.startswith("same text, same place")


def test_left_fragment_is_dropped():
    full = OcrChunk("SEE NOTE 6", (3300, 900, 3740, 980), tile_index=1)
    frag = OcrChunk("SEE", (3300, 900, 3500, 980), tile_index=0)
    log: list[DedupDecision] = []
    assert texts(merge_tile_results([frag, full], TILES, log=log)) == ["SEE NOTE 6"]
    assert log[0].dropped is frag and log[0].kept is full and log[0].reason.startswith("overlapping box")


def test_cut_part_number_keeps_full_label_with_asterisk():
    full = OcrChunk("WT155*79", (6500, 2000, 6860, 2080), tile_index=2)
    frag = OcrChunk("WT15", (6500, 2000, 6700, 2080), tile_index=1)
    assert texts(merge_tile_results([frag, full], TILES)) == ["WT155*79"]


def test_of_two_readings_as_long_the_larger_box_is_kept():
    larger = OcrChunk("C50", (3250, 4600, 3372, 4682), tile_index=3)
    smaller = OcrChunk("CS0", (3251, 4601, 3371, 4681), tile_index=4)
    assert texts(merge_tile_results([smaller, larger], TILES)) == ["C50"]


def test_same_text_far_apart_is_kept_twice():
    a = OcrChunk("TYP.", (2600, 2200, 2750, 2270), tile_index=0)
    b = OcrChunk("TYP.", (7600, 4300, 7750, 4370), tile_index=5)
    assert texts(merge_tile_results([a, b], TILES)) == ["TYP.", "TYP."]


def test_different_text_close_together_is_kept():
    label = OcrChunk("W310X39", (1500, 2900, 1810, 2980), tile_index=0)
    dim = OcrChunk("1200", (1500, 3100, 1680, 3170), tile_index=3)  # 200 px below: not near
    assert texts(merge_tile_results([label, dim], TILES)) == ["1200", "W310X39"]


def test_label_printed_twice_in_one_tile_stays_twice():
    a = OcrChunk("W310X39", (100, 100, 410, 160), tile_index=0)
    b = OcrChunk("W310X39", (100, 175, 410, 235), tile_index=0)  # just below the first
    assert texts(merge_tile_results([a, b], TILES)) == ["W310X39", "W310X39"]


def test_overlapping_texts_that_one_tile_read_are_both_kept():
    # A stud count printed over a label, in the band tile 0 shares with tile 1. One tile read both,
    # so they are two printed texts, although one box lies inside the other.
    label = OcrChunk("W310X39", (3250, 100, 3480, 160), tile_index=0)
    studs = OcrChunk("(6)", (3400, 110, 3450, 150), tile_index=0)
    assert texts(merge_tile_results([label, studs], TILES)) == ["(6)", "W310X39"]


def test_equal_values_in_neighbouring_table_rows_are_not_merged():
    # Two "10"s in rows 75 px apart, inside the shared area, and each tile read only one of them:
    # they share no area, so they are two printed texts, not one read twice.
    upper = OcrChunk("10", (3250, 1000, 3290, 1030), tile_index=0)
    lower = OcrChunk("10", (3250, 1075, 3290, 1105), tile_index=1)
    assert texts(merge_tile_results([upper, lower], TILES)) == ["10", "10"]


def test_short_label_next_to_a_longer_one_is_kept():
    same_tile = [
        OcrChunk("C50", (100, 100, 160, 130), tile_index=0),
        OcrChunk("C500", (100, 140, 180, 170), tile_index=0),
    ]
    assert texts(merge_tile_results(same_tile, TILES)) == ["C50", "C500"]
    # Read by two tiles in their shared area: the boxes don't overlap, so neither is a piece of the other.
    two_tiles = [
        OcrChunk("C50", (3250, 100, 3310, 130), tile_index=0),
        OcrChunk("C500", (3250, 140, 3330, 170), tile_index=1),
    ]
    assert texts(merge_tile_results(two_tiles, TILES)) == ["C50", "C500"]


def test_text_outside_the_area_two_tiles_share_is_not_merged():
    tiles = [Tile(0, 0, 1000, 1000), Tile(800, 0, 1800, 1000)]  # they share x 800..1000
    left = OcrChunk("C50", (930, 100, 990, 130), tile_index=0)  # in the shared area: read by both
    left_again = OcrChunk("C50", (931, 100, 991, 130), tile_index=1)
    right = OcrChunk("C50", (1005, 100, 1065, 130), tile_index=1)  # a second C50, only tile 1 sees it
    assert texts(merge_tile_results([left, left_again, right], tiles)) == ["C50", "C50"]


def test_chunk_without_a_tile_is_rejected():
    with pytest.raises(ValueError, match="tile_index"):
        merge_tile_results([OcrChunk("C50", (0, 0, 60, 30))], TILES)  # tile_index defaults to -1


def test_tiled_ocr_merges_overlap_duplicates_but_keeps_repeated_labels():
    truth = [
        GroundTruthText("W310X39", "W310X39", (100, 100, 410, 160), "W310X39"),  # printed twice, close
        GroundTruthText("W310X39", "W310X39", (100, 175, 410, 235), "W310X39"),  # together, in tile 0 only
        GroundTruthText("C50", "C50", (850, 500, 970, 560), "C50"),  # in the overlap: read by both tiles
    ]
    page = Image.new("RGB", (1800, 1000), "white")
    ocr = run_tiled_ocr(page, SimulatedOcr(truth), tiling=TilingChoice(1000, 200, "two tiles for the test"))
    assert len(ocr.tiles) == 2 and len(ocr.raw) == 4
    assert texts(ocr.merged) == ["C50", "W310X39", "W310X39"]
