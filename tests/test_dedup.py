from PIL import Image

from steel_text.dedup import DedupDecision, merge_tile_results
from steel_text.mock_clients import SimulatedOcr
from steel_text.ocr_stage import run_tiled_ocr
from steel_text.ocr_types import OcrChunk
from steel_text.sample_drawing import GroundTruthText
from steel_text.text_norm import is_fragment, tokens
from steel_text.tiling import Tile


def texts(chunks):
    return sorted(c.text for c in chunks)


def test_exact_duplicate_from_two_tiles_is_merged():
    a = OcrChunk("HSS152X152X9.5", (5650, 1500, 6270, 1580), tile_index=1)
    b = OcrChunk("HSS152X152X9.5", (5651, 1499, 6269, 1581), tile_index=2)
    assert texts(merge_tile_results([a, b])) == ["HSS152X152X9.5"]


def test_left_fragment_is_dropped():
    full = OcrChunk("SEE NOTE 6", (3300, 900, 3740, 980), tile_index=1)
    frag = OcrChunk("SEE", (3300, 900, 3500, 980), tile_index=0)
    log: list[DedupDecision] = []
    assert texts(merge_tile_results([frag, full], log=log)) == ["SEE NOTE 6"]
    assert log[0].dropped is frag and log[0].reason.startswith("fragment")


def test_cut_part_number_keeps_full_label_with_asterisk():
    full = OcrChunk("WT155*79", (6500, 2000, 6860, 2080), tile_index=2)
    frag = OcrChunk("WT15", (6500, 2000, 6700, 2080), tile_index=1)
    assert texts(merge_tile_results([frag, full])) == ["WT155*79"]


def test_same_text_far_apart_is_kept_twice():
    a = OcrChunk("TYP.", (2600, 2200, 2750, 2270))
    b = OcrChunk("TYP.", (7600, 4300, 7750, 4370))
    assert texts(merge_tile_results([a, b])) == ["TYP.", "TYP."]


def test_different_text_close_together_is_kept():
    label = OcrChunk("W310X39", (1500, 2900, 1810, 2980))
    dim = OcrChunk("1200", (1500, 3100, 1680, 3170))  # 200 px below: not near
    assert texts(merge_tile_results([label, dim])) == ["1200", "W310X39"]


def test_higher_confidence_wins_between_conflicting_readings():
    good = OcrChunk("C50", (2000, 4600, 2120, 4680), conf=0.95)
    bad = OcrChunk("CS0", (2001, 4601, 2121, 4681), conf=0.40)
    assert texts(merge_tile_results([bad, good])) == ["C50"]


def test_label_printed_twice_in_one_tile_stays_twice():
    a = OcrChunk("W310X39", (100, 100, 410, 160), tile_index=0)
    b = OcrChunk("W310X39", (100, 175, 410, 235), tile_index=0)  # centres 75 px apart
    assert texts(merge_tile_results([a, b])) == ["W310X39", "W310X39"]


def test_short_label_next_to_a_longer_one_is_kept():
    same_tile = [OcrChunk("C50", (0, 0, 60, 30), tile_index=0), OcrChunk("C500", (0, 40, 80, 70), tile_index=0)]
    assert texts(merge_tile_results(same_tile)) == ["C50", "C500"]
    # From two tiles the boxes still don't overlap, so neither is a piece of the other.
    two_tiles = [OcrChunk("C50", (0, 0, 60, 30), tile_index=0), OcrChunk("C500", (0, 40, 80, 70), tile_index=1)]
    assert texts(merge_tile_results(two_tiles)) == ["C50", "C500"]


def test_text_outside_the_area_two_tiles_share_is_not_merged():
    tiles = [Tile(0, 0, 1000, 1000), Tile(800, 0, 1800, 1000)]  # they share x 800..1000
    left = OcrChunk("C50", (930, 100, 990, 130), conf=0.9, tile_index=0)  # in the shared area: read by both
    left_again = OcrChunk("C50", (931, 100, 991, 130), conf=0.8, tile_index=1)
    right = OcrChunk("C50", (1005, 100, 1065, 130), conf=0.8, tile_index=1)  # a second C50, only tile 1 sees it
    assert texts(merge_tile_results([left, left_again, right], tiles=tiles)) == ["C50", "C50"]


def test_log_points_at_the_chunk_that_survived():
    # Rule 1 ignores the text: B loses to A, then C replaces A. B's decision must now say C was kept.
    a = OcrChunk("AAAAAAAA", (0, 0, 300, 60), conf=0.5, tile_index=0)
    b = OcrChunk("BBBBBBB", (1, 0, 301, 60), conf=0.4, tile_index=1)
    c = OcrChunk("CCCCCC", (0, 1, 300, 61), conf=0.9, tile_index=2)
    log: list[DedupDecision] = []
    merged = merge_tile_results([a, b, c], log=log)
    assert merged == [c] and len(log) == 2
    assert all(d.kept is c for d in log)


def test_tiled_ocr_merges_overlap_duplicates_but_keeps_repeated_labels():
    truth = [
        GroundTruthText("W310X39", "W310X39", (100, 100, 410, 160), "W310X39"),  # printed twice, close
        GroundTruthText("W310X39", "W310X39", (100, 175, 410, 235), "W310X39"),  # together, in tile 0 only
        GroundTruthText("C50", "C50", (850, 500, 970, 560), "C50"),  # in the overlap: read by both tiles
    ]
    page = Image.new("RGB", (1800, 1000), "white")
    ocr = run_tiled_ocr(page, SimulatedOcr(truth), adaptive=False, tile_size=1000, overlap=200, request_sleep_s=0)
    assert len(ocr.tiles) == 2 and len(ocr.raw) == 4
    assert texts(ocr.merged) == ["C50", "W310X39", "W310X39"]


def test_tokens_keep_asterisk():
    assert tokens("WT155*79") == ["WT155*79"]
    assert tokens("see  note 6") == ["SEE", "NOTE", "6"]


def test_fragment_check_is_not_plain_substring_matching():
    assert is_fragment("SEE", "SEE NOTE 6")
    assert is_fragment("6", "SEE NOTE 6 FOR DETAILS")
    assert not is_fragment("NOTE 6", "SEE NOTE 6")  # too long to be a cut-off piece
    assert not is_fragment("C50", "C500")
