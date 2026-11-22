"""Run the whole offline pipeline on the synthetic drawing."""

import pytest

from steel_text.llm_filter import filter_labels
from steel_text.mock_clients import RuleBasedLabeler, SimulatedOcr
from steel_text.ocr_stage import run_tiled_ocr
from steel_text.sample_drawing import make_sample_drawing


@pytest.fixture(scope="module")
def drawing():
    return make_sample_drawing()


@pytest.fixture(scope="module")
def ocr(drawing):
    page, truth = drawing
    return run_tiled_ocr(page, SimulatedOcr(truth), request_sleep_s=0)


def test_tile_edges_create_the_intended_duplicates_and_fragments(ocr):
    raw = [c.text for c in ocr.raw]
    assert len(ocr.tiles) == 6
    assert raw.count("HSS152X152X9.5") == 2  # inside the x overlap band
    assert raw.count("W31OX39") == 2  # inside the y overlap band
    # Cut by a tile edge, so one tile reads only the start of the text. Exactly where
    # it is cut depends on the font's glyph widths, which differ between operating
    # systems ("WT15" with Arial, "WT1" with DejaVu Sans), so only the prefix is checked.
    for full in ("SEE NOTE 6", "WT155*79"):
        assert any(text != full and full.startswith(text) for text in raw), full


def test_dedup_leaves_each_printed_text_exactly_once(drawing, ocr):
    _, truth = drawing
    printed = sorted(t.ocr_text for t in truth if len(t.ocr_text) >= 2)
    assert sorted(c.text for c in ocr.merged) == printed


def test_all_steel_labels_found_and_corrected(drawing, ocr):
    _, truth = drawing
    res = filter_labels(ocr.merged, RuleBasedLabeler(), batch_size=10, sleep=lambda s: None)
    assert res.ok
    assert {e.normalized_label for e in res.entries} == {t.expected_label for t in truth if t.expected_label}
