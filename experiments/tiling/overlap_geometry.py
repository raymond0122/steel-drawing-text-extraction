"""Experiment 5, geometry only (no requests): how long the beam labels are, and what each overlap cuts and costs.

    python experiments/tiling/overlap_geometry.py

A text no longer than the overlap that crosses a tile edge is always whole in one of the two
tiles, so the overlap to beat is the longest label. With the app's layout at each overlap, the
drawing is placed 400 ways on a page 2,000 px larger each way, since that moves the seams.
"""

from __future__ import annotations

import random
import statistics

from layout_geometry import cut, texts

from steel_text.tiling import make_tiles

W, H, TILE = 14_400, 9_600, 3_500
OVERLAPS = [150, 200, 250, 300, 343, 350, 400, 450, 500, 600, 800]


def main() -> None:
    labels, lines = texts()
    along = sorted(max(b[2] - b[0], b[3] - b[1]) for b in labels)  # the side a seam has to cross
    longest_line = max(max(b[2] - b[0], b[3] - b[1]) for b in lines)
    print(
        f"{len(labels)} beam labels, length along the text: median {statistics.median(along)} px, "
        f"longest {along[-1]} px; {len(lines)} text lines, longest {longest_line} px\n"
    )

    random.seed(0)
    sizes = [(random.randint(3_500, 19_000), random.randint(3_500, 13_500)) for _ in range(3_000)]
    base = sum(len(make_tiles(w, h, tile_size=TILE, overlap=300)) for w, h in sizes)
    print("overlap | sheet as it is: tiles, labels cut | 400 placements: with a label cut | tiles, 3,000 page sizes")
    for o in OVERLAPS:
        sheet = make_tiles(W, H, tile_size=TILE, overlap=o)
        page = make_tiles(W + 2000, H + 2000, tile_size=TILE, overlap=o)
        placements = [cut(labels, page, dx, dy) for dx in range(0, 2000, 100) for dy in range(0, 2000, 100)]
        tiles = sum(len(make_tiles(w, h, tile_size=TILE, overlap=o)) for w, h in sizes)
        print(
            f"  {o:4}  |  {len(sheet):2} tiles, {cut(labels, sheet)} labels  |  "
            f"{sum(n > 0 for n in placements):3}/400  |  {tiles / base - 1:+.1%} against 300 px"
        )


if __name__ == "__main__":
    main()
