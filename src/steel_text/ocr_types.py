"""Data types shared by the OCR stages: what a tile read, and what the page holds after tiling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from PIL import Image

from .geometry import BBox
from .tiling import Tile


@dataclass
class OcrLine:
    """One line of text as returned for a single tile (tile-local coordinates)."""

    text: str
    bbox: BBox


@dataclass
class OcrChunk:
    """One line of text on the page, tagged with the tile that read it.

    Tiled OCR produces these, dedup merges them, and the LLM filter, the web UI
    and the demo overlays read them.
    """

    text: str
    bbox: BBox  # page coordinates
    tile_index: int = -1


class TileReadError(RuntimeError):
    """A tile could not be read, even after retries. Tiled OCR records it and reads the other tiles."""


class TileOcrClient(Protocol):
    """Anything that can read the text in one tile: the real Azure client or the offline simulator."""

    def read_tile(self, tile_img: Image.Image, tile: Tile) -> list[OcrLine]: ...
