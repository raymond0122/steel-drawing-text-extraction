"""Split a large page image into overlapping tiles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

# Tiles are cut at full resolution, so the text keeps its size and only the image sent
# per request changes. On a 14,400 x 9,600 px test sheet, tiles of 2,000 to 4,500 px read
# equally well and larger ones read worse (docs/DESIGN.md), so 3,500 px leaves a margin.
TILE_SIZE = 3500
OVERLAP = 300
TILE_GROWTH = 1.15  # when the grid has more than max_tiles tiles
MAX_TILE_SIZE = 10_000  # Azure Document Intelligence rejects an image over 10,000 px on a side
DEFAULT_MAX_TILES = 20


class TileLimitError(ValueError):
    """The page can't be covered by max_tiles tiles of at most MAX_TILE_SIZE px."""


@dataclass(frozen=True)
class Tile:
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def box(self) -> tuple[int, int, int, int]:
        return (self.x0, self.y0, self.x1, self.y1)


class TilingChoice(NamedTuple):
    tile_size: int
    overlap: int
    reason: str  # one sentence on why, shown by the demo and the web UI


def make_tiles(width: int, height: int, *, tile_size: int, overlap: int) -> list[Tile]:
    """Cover the whole image with tile_size squares that overlap by `overlap` px.

    Tiles at the right/bottom edge are shifted back inside the image instead of
    being cut short, so every tile keeps the full tile size when possible.
    """
    if tile_size <= 0 or not 0 <= overlap < tile_size:
        raise ValueError("need tile_size > 0 and 0 <= overlap < tile_size")
    stride = tile_size - overlap

    tiles: list[Tile] = []
    seen = set()
    for y0 in range(0, max(1, height), stride):
        for x0 in range(0, max(1, width), stride):
            x1, y1 = x0 + tile_size, y0 + tile_size
            if x1 > width:
                x1, x0 = width, max(0, width - tile_size)
            if y1 > height:
                y1, y0 = height, max(0, height - tile_size)
            key = (x0, y0, x1, y1)
            if key not in seen:  # small images can produce the same tile twice
                seen.add(key)
                tiles.append(Tile(*key))
    return tiles


def choose_tiling(width: int, height: int, *, max_tiles: int = DEFAULT_MAX_TILES) -> TilingChoice:
    """Pick the tile size and overlap for a page.

    A page that fits in one TILE_SIZE tile is sent whole. A larger one is cut into
    TILE_SIZE tiles that overlap by OVERLAP px. If that grid would have more than
    max_tiles tiles, the tile size grows by 15% until it fits, but never past
    MAX_TILE_SIZE: a page that still needs more tiles raises TileLimitError.
    """
    if max_tiles < 1:
        raise TileLimitError("max_tiles must be at least 1")
    side = max(width, height)
    if side <= TILE_SIZE:
        reason = f"The longest side is {side:,} px, which fits in one {TILE_SIZE:,} px tile, so the page is sent whole."
        return TilingChoice(side, 0, reason)

    tile_size = TILE_SIZE
    reason = (
        f"The longest side is {side:,} px, so the page is cut into {tile_size:,} px tiles that overlap by {OVERLAP} px."
    )
    count = len(make_tiles(width, height, tile_size=tile_size, overlap=OVERLAP))
    while count > max_tiles:
        if tile_size == MAX_TILE_SIZE:
            raise TileLimitError(
                f"a {width:,} x {height:,} px page needs at least {count} tiles "
                f"of at most {MAX_TILE_SIZE:,} px, but max_tiles is {max_tiles}"
            )
        tile_size = min(int(tile_size * TILE_GROWTH) + 1, MAX_TILE_SIZE)
        count = len(make_tiles(width, height, tile_size=tile_size, overlap=OVERLAP))
    if tile_size != TILE_SIZE:
        reason += (
            f" That grid had more than {max_tiles} tiles, "
            f"so the tile size grew in {TILE_GROWTH - 1:.0%} steps to {tile_size:,} px."
        )
    return TilingChoice(tile_size, OVERLAP, reason)
