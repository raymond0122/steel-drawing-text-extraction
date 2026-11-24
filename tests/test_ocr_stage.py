"""Tiled OCR: a tile that can't be read is skipped and recorded, never lost silently."""

import pytest
from PIL import Image

from steel_text.azure_ocr import AzureOcrError
from steel_text.mock_clients import SimulatedOcr
from steel_text.ocr_stage import run_tiled_ocr
from steel_text.ocr_types import TileReadError
from steel_text.sample_drawing import GroundTruthText
from steel_text.tiling import TilingChoice

PAGE = Image.new("RGB", (1800, 1000), "white")
TWO_TILES = TilingChoice(1000, 200, "two tiles for the test")  # x 0-1000 and x 800-1800
TRUTH = [
    GroundTruthText("W310X39", "W310X39", (100, 100, 410, 160), "W310X39"),  # only tile 0 sees it
    GroundTruthText("C50", "C50", (1300, 500, 1420, 560), "C50"),  # only tile 1 sees it
]


def test_a_failed_tile_is_recorded_and_the_other_tiles_are_still_read():
    ocr = run_tiled_ocr(PAGE, SimulatedOcr(TRUTH, fail_calls={1}), tiling=TWO_TILES)
    assert [c.text for c in ocr.merged] == ["W310X39"]
    assert ocr.failed_tiles == [(1, "HTTP 500 (simulated failure)")]
    assert ocr.not_read == [(1, (800, 0, 1800, 1000), "HTTP 500 (simulated failure)")]


def test_a_page_where_no_tile_can_be_read_fails():
    with pytest.raises(TileReadError, match="no tile could be read"):
        run_tiled_ocr(PAGE, SimulatedOcr(TRUTH, fail_calls={0, 1}), tiling=TWO_TILES)


def test_an_azure_error_counts_as_a_failed_tile():
    assert issubclass(AzureOcrError, TileReadError)
