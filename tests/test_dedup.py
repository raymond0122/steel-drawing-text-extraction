from steel_text.dedup import DedupDecision, merge_tile_results
from steel_text.ocr_types import OcrChunk
from steel_text.text_norm import is_fragment, tokens


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


def test_tokens_keep_asterisk():
    assert tokens("WT155*79") == ["WT155*79"]
    assert tokens("see  note 6") == ["SEE", "NOTE", "6"]


def test_fragment_check_is_not_plain_substring_matching():
    assert is_fragment("SEE", "SEE NOTE 6")
    assert is_fragment("6", "SEE NOTE 6 FOR DETAILS")
    assert not is_fragment("NOTE 6", "SEE NOTE 6")  # too long to be a cut-off piece
    assert not is_fragment("C50", "C500")
