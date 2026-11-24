"""Experiment 4, geometry only (no requests): which texts no tile holds whole, for three tile layouts.

    python experiments/tiling/layout_geometry.py

  app      what the app does: 3,500 px tiles every 3,200 px, the last one moved back to the page edge
  even     the same number of tiles, spread evenly, so every seam overlaps the same
  wide     the app's layout with a 450 px overlap, wider than the widest beam label (342 px)

Texts are the 82 beam labels of the answer key, and the text lines of the whole page read
at 6,000 px (results/whole_6000.json), whose boxes no tile edge has cut.
"""

from __future__ import annotations

import json
import random
import statistics
from collections.abc import Callable
from pathlib import Path

from steel_text.tiling import Tile, make_tiles

HERE = Path(__file__).resolve().parent
W, H, TILE = 14_400, 9_600, 3_500


def even_tiles(width: int, height: int, *, tile_size: int = TILE, overlap: int = 300) -> list[Tile]:
    """As many tiles per side as make_tiles cuts, spread evenly over the page."""

    def starts(side: int) -> list[int]:
        n = len({t.x0 for t in make_tiles(side, 1, tile_size=tile_size, overlap=overlap)})
        return [0] if n == 1 else [round(i * (side - tile_size) / (n - 1)) for i in range(n)]

    return [
        Tile(x, y, min(x + tile_size, width), min(y + tile_size, height)) for y in starts(height) for x in starts(width)
    ]


LAYOUTS: dict[str, Callable[[int, int], list[Tile]]] = {
    "app": lambda w, h: make_tiles(w, h, tile_size=TILE, overlap=300),
    "even": lambda w, h: even_tiles(w, h),
    "wide": lambda w, h: make_tiles(w, h, tile_size=TILE, overlap=450),
}


def held_whole(box: list[int], tiles: list[Tile]) -> bool:
    return any(t.x0 <= box[0] and t.y0 <= box[1] and box[2] <= t.x1 and box[3] <= t.y1 for t in tiles)


def cut(boxes: list[list[int]], tiles: list[Tile], dx: int = 0, dy: int = 0) -> int:
    """How many of the boxes, moved by (dx, dy), no tile holds whole."""
    return sum(not held_whole([b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy], tiles) for b in boxes)


def texts() -> tuple[list[list[int]], list[list[int]]]:
    labels = [g["bbox"] for g in json.loads((HERE / "ground_truth.json").read_text(encoding="utf-8"))]
    whole = json.loads((HERE / "results" / "whole_6000.json").read_text(encoding="utf-8"))
    return labels, [ln["bbox"] for ln in whole["lines"] if len(ln["text"].strip()) >= 2]


def main() -> None:
    labels, lines = texts()
    print("The sheet as it is:")
    for name, make in LAYOUTS.items():
        ts = make(W, H)
        print(f"  {name:5} {len(ts)} tiles, not held whole: {cut(labels, ts)} labels, {cut(lines, ts)} lines")

    # Where the seams fall depends on where the drawing sits on the page, so move it 400 ways.
    placements = {
        "flush with the right and bottom edges of a page larger by dx, dy": lambda dx, dy: (W + dx, H + dy),
        "anywhere on a page 2,000 px larger each way": lambda dx, dy: (W + 2000, H + 2000),
    }
    for title, page in placements.items():
        print(f"\nThe drawing {title}, 400 placements:")
        for name, make in LAYOUTS.items():
            lab, ln, n = [], [], []
            for dx in range(0, 2000, 100):
                for dy in range(0, 2000, 100):
                    ts = make(*page(dx, dy))
                    lab.append(cut(labels, ts, dx, dy))
                    ln.append(cut(lines, ts, dx, dy))
                    n.append(len(ts))
            print(
                f"  {name:5} labels cut in {sum(x > 0 for x in lab):3}/400 placements (at most {max(lab)}), "
                f"lines cut {statistics.mean(ln):4.1f} on average, {statistics.mean(n):.1f} tiles"
            )

    random.seed(0)
    sizes = [(random.randint(3_500, 19_000), random.randint(3_500, 13_500)) for _ in range(5_000)]
    app, wide = (sum(len(LAYOUTS[k](w, h)) for w, h in sizes) for k in ("app", "wide"))
    print(f"\nTiles over 5,000 random page sizes: app {app}, wide {wide} ({wide / app - 1:+.1%}); even always as app")


if __name__ == "__main__":
    main()
