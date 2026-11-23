"""Tiled OCR: cut the page into tiles, OCR each one, map back to page coordinates, merge."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from PIL import Image

from .dedup import DedupDecision, merge_tile_results
from .ocr_types import OcrChunk, TileOcrClient
from .tiling import Tile, choose_tiling_params, make_tiles

logger = logging.getLogger(__name__)


@dataclass
class TiledOcrResult:
    width: int
    height: int
    tile_size: int
    overlap: int
    tiles: list[Tile]
    raw: list[OcrChunk]
    merged: list[OcrChunk]
    dedup_log: list[DedupDecision] = field(default_factory=list)


def run_tiled_ocr(
    image: Image.Image,
    client: TileOcrClient,
    *,
    adaptive: bool = True,
    tile_size: int = 3500,
    overlap: int = 300,
    max_tiles: int = 20,
    min_chars: int = 2,
    request_sleep_s: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
) -> TiledOcrResult:
    """OCR the page tile by tile and merge the results.

    With `adaptive`, the tile size comes from the page size (see choose_tiling_params);
    otherwise tile_size and overlap are used as given. Lines shorter than
    `min_chars` are dropped: single letters are grid marks, not labels.
    """
    width, height = image.size
    if adaptive:
        tile_size, overlap = choose_tiling_params(
            width, height, large_tile_size=tile_size, overlap=overlap, max_tiles=max_tiles
        )
    tiles = make_tiles(width, height, tile_size=tile_size, overlap=overlap)
    logger.info("page %dx%d -> %d tiles (tile_size=%d, overlap=%d)", width, height, len(tiles), tile_size, overlap)

    raw: list[OcrChunk] = []
    for i, tile in enumerate(tiles):
        if i > 0 and request_sleep_s > 0:
            sleep(request_sleep_s)  # pace requests to stay under the rate limit
        # If a tile still fails after all retries, the exception propagates and
        # the whole page fails: partial OCR would silently lose labels.
        lines = client.read_tile(image.crop(tile.box), tile)

        added = 0
        for line in lines:
            if len(line.text) < min_chars:
                continue
            x0, y0, x1, y1 = line.bbox  # tile coordinates -> page coordinates
            page_box = (x0 + tile.x0, y0 + tile.y0, x1 + tile.x0, y1 + tile.y0)
            raw.append(OcrChunk(text=line.text, bbox=page_box, conf=line.conf, tile_index=i))
            added += 1
        logger.info("tile %d/%d %s: %d lines", i + 1, len(tiles), tile.box, added)

    log: list[DedupDecision] = []
    merged = merge_tile_results(raw, tiles, log=log)
    logger.info("dedup: %d raw -> %d merged (removed %d)", len(raw), len(merged), len(raw) - len(merged))
    return TiledOcrResult(width, height, tile_size, overlap, tiles, raw, merged, log)
