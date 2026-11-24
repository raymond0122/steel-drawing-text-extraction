"""Merge OCR results from overlapping tiles.

Two overlapping tiles both read the text in the area they share, which causes:
  * duplicates: the same text read whole by two (or four) tiles
  * fragments:  text crossing a tile edge comes back cut off, e.g. "SEE"
                from one tile and "SEE NOTE 6" from its neighbour

So two chunks are compared only when two different tiles read them, inside
the area both tiles cover. One OCR pass doesn't read a text twice: chunks from
the same tile are always kept, even a label printed twice close together.

Every decision is recorded in `log` (DedupDecision), so the demo and the web
UI can show why each chunk was dropped.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from . import geometry as g
from .ocr_types import OcrChunk
from .tiling import Tile


@dataclass
class DedupDecision:
    dropped: OcrChunk
    kept: OcrChunk
    reason: str

    def __str__(self) -> str:
        return (
            f"dropped '{self.dropped.text}' (tile {self.dropped.tile_index}), "
            f"kept '{self.kept.text}' (tile {self.kept.tile_index}): {self.reason}"
        )


@dataclass(frozen=True)
class DedupConfig:
    # Kept strict on purpose: looser values merge different labels that sit close together.
    iou: float = 0.50  # with center_dist_px: how near two readings of the same text must be
    overlap_min: float = 0.80  # share of the smaller box the other must cover: a cut-off piece, or another reading
    center_dist_px: float = 80.0


def merge_tile_results(
    chunks: Sequence[OcrChunk],
    tiles: Sequence[Tile],
    *,
    cfg: DedupConfig = DedupConfig(),
    log: list[DedupDecision] | None = None,
) -> list[OcrChunk]:
    """Drop duplicates and cut-off fragments; return the chunks that remain.

    `tiles` are the tiles the chunks came from: each chunk's tile_index points
    into it. The best reading goes first (longest text, then the larger box), and
    of two readings of one text the first is kept. So a full string is already
    kept when its fragments come up.
    """
    for c in chunks:
        if not 0 <= c.tile_index < len(tiles):
            raise ValueError(f"chunk {c.text!r} has tile_index {c.tile_index}, but there are {len(tiles)} tiles")

    ordered = sorted(chunks, key=lambda c: (len(_norm_text(c.text)), g.area(c.bbox)), reverse=True)
    kept: list[OcrChunk] = []
    for c in ordered:
        for k in kept:
            reason = _duplicate_reason(c, k, cfg) if _read_twice(c, k, tiles) else None
            if reason is not None:
                if log is not None:
                    log.append(DedupDecision(dropped=c, kept=k, reason=reason))
                break
        else:
            kept.append(c)
    return kept


def _read_twice(a: OcrChunk, b: OcrChunk, tiles: Sequence[Tile]) -> bool:
    """Could a and b be one text read twice? Only if two different tiles read them where they overlap."""
    if a.tile_index == b.tile_index:
        return False
    shared = g.intersection_box(tiles[a.tile_index].box, tiles[b.tile_index].box)
    return shared is not None and g.intersection_area(a.bbox, shared) > 0 and g.intersection_area(b.bbox, shared) > 0


def _duplicate_reason(c: OcrChunk, k: OcrChunk, cfg: DedupConfig) -> str | None:
    """Why c is another reading of the kept chunk k, or None if they are two different texts."""
    near = g.iou(c.bbox, k.bbox) >= cfg.iou or g.center_distance(c.bbox, k.bbox) <= cfg.center_dist_px
    text = _norm_text(c.text)
    if near and text and text == _norm_text(k.text):
        return "same text nearby"

    overlap = g.overlap_ratio_min(c.bbox, k.bbox)
    if overlap >= cfg.overlap_min:
        # one box mostly covers the other: a cut-off piece of the same text, or a different reading of it
        return f"overlapping box (overlap={overlap:.2f})"

    return None  # different text that happens to be close: keep both


def _norm_text(s: str) -> str:
    """Uppercase, the multiplication sign as X, whitespace collapsed."""
    return re.sub(r"\s+", " ", s.strip().upper().replace("×", "X"))
