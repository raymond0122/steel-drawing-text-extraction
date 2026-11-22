"""Split a large page image into overlapping tiles."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tile:
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def box(self) -> tuple[int, int, int, int]:
        return (self.x0, self.y0, self.x1, self.y1)


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


def choose_tiling_params(
    width: int,
    height: int,
    *,
    no_tiling_max_dim: int = 4000,
    medium_max_dim: int = 8000,
    medium_tile_size: int = 3000,
    large_tile_size: int = 3500,
    overlap: int = 300,
    max_tiles: int = 20,
) -> tuple[int, int]:
    """Pick (tile_size, overlap) from the image size.

    - long side <= 4000 px: one tile, no overlap
    - long side <= 8000 px: 3000 px tiles
    - larger:               3500 px tiles
    If the grid would exceed max_tiles, grow the tile size by 15% until it fits.
    """
    max_dim = max(width, height)
    if max_dim <= no_tiling_max_dim:
        tile_size, ov = max_dim + 100, 0
    elif max_dim <= medium_max_dim:
        tile_size, ov = medium_tile_size, overlap
    else:
        tile_size, ov = large_tile_size, overlap

    for _ in range(20):
        if len(make_tiles(width, height, tile_size=tile_size, overlap=ov)) <= max_tiles:
            break
        tile_size = int(tile_size * 1.15) + 1
    return tile_size, ov
