"""Tiled OCR: cut the page into tiles, OCR each one, map back to page coordinates, merge."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from PIL import Image

from .dedup import DedupConfig, DedupDecision, merge_tile_results
from .geometry import BBox
from .ocr_types import OcrChunk, TileOcrClient, TileReadError
from .tiling import DEFAULT_MAX_TILES, Tile, TilingChoice, choose_tiling, make_tiles

logger = logging.getLogger(__name__)


@dataclass
class TiledOcrResult:
    width: int
    height: int
    tiling: TilingChoice
    tiles: list[Tile]
    raw: list[OcrChunk]
    merged: list[OcrChunk]
    dedup_log: list[DedupDecision] = field(default_factory=list)
    failed_tiles: list[tuple[int, str]] = field(default_factory=list)  # (tile index, error)
    dedup_config: DedupConfig = field(default_factory=DedupConfig)  # the thresholds `merged` was made with

    @property
    def not_read(self) -> list[tuple[int, BBox, str]]:
        """The areas of the page no tile read: tile index, its box, and why."""
        return [(i, self.tiles[i].box, error) for i, error in self.failed_tiles]


def run_tiled_ocr(
    image: Image.Image,
    client: TileOcrClient,
    *,
    max_tiles: int = DEFAULT_MAX_TILES,
    tiling: TilingChoice | None = None,
    dedup: DedupConfig = DedupConfig(),
    min_chars: int = 2,
    request_sleep_s: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> TiledOcrResult:
    """OCR the page tile by tile and merge the results.

    The tile size comes from the page size (see choose_tiling), unless `tiling`
    is given. Lines shorter than `min_chars` are dropped: single letters are
    grid marks, not labels. `request_sleep_s` is the pause between tiles.

    A tile that still fails after its retries is skipped and recorded in
    `failed_tiles`, so the rest of the page is still read and every output can
    mark the gap. If every tile fails, there is nothing to show: TileReadError.
    """
    width, height = image.size
    if tiling is None:
        tiling = choose_tiling(width, height, max_tiles=max_tiles)
    tiles = make_tiles(width, height, tile_size=tiling.tile_size, overlap=tiling.overlap)
    logger.info("page %dx%d -> %d tiles: %s", width, height, len(tiles), tiling.reason)

    raw: list[OcrChunk] = []
    failed: list[tuple[int, str]] = []
    for i, tile in enumerate(tiles):
        if i > 0 and request_sleep_s > 0:
            sleep(request_sleep_s)  # pace requests to stay under the rate limit
        try:
            lines = client.read_tile(image.crop(tile.box), tile)
        except TileReadError as e:
            failed.append((i, str(e)))
            logger.warning("tile %d/%d %s not read, its text is missing: %s", i + 1, len(tiles), tile.box, e)
            continue

        added = 0
        for line in lines:
            if len(line.text) < min_chars:
                continue
            x0, y0, x1, y1 = line.bbox  # tile coordinates -> page coordinates
            page_box = (x0 + tile.x0, y0 + tile.y0, x1 + tile.x0, y1 + tile.y0)
            raw.append(OcrChunk(text=line.text, bbox=page_box, tile_index=i))
            added += 1
        logger.info("tile %d/%d %s: %d lines", i + 1, len(tiles), tile.box, added)

    if len(failed) == len(tiles):
        raise TileReadError(f"no tile could be read; the first error: {failed[0][1]}")

    log: list[DedupDecision] = []
    merged = merge_tile_results(raw, tiles, cfg=dedup, log=log)
    logger.info("dedup: %d raw -> %d merged (removed %d)", len(raw), len(merged), len(raw) - len(merged))
    return TiledOcrResult(width, height, tiling, tiles, raw, merged, log, failed, dedup)
