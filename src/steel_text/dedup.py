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

from collections.abc import Sequence
from dataclasses import dataclass

from . import geometry as g
from .ocr_types import OcrChunk
from .text_norm import is_fragment, norm_text
from .tiling import Tile


@dataclass
class DedupDecision:
    dropped: OcrChunk
    kept: OcrChunk  # the chunk that survived the merge; updated if a later chunk replaces it
    reason: str

    def __str__(self) -> str:
        return (
            f"dropped '{self.dropped.text}' (tile {self.dropped.tile_index}), "
            f"kept '{self.kept.text}' (tile {self.kept.tile_index}): {self.reason}"
        )


@dataclass(frozen=True)
class DedupConfig:
    # Kept strict on purpose: looser values merge different labels that sit close together.
    iou: float = 0.50
    overlap_min: float = 0.80
    very_high_iou: float = 0.90  # only near-identical boxes
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
    into it. Longest text goes first, so a full string is already kept when its
    fragments come up.
    """
    for c in chunks:
        if not 0 <= c.tile_index < len(tiles):
            raise ValueError(f"chunk {c.text!r} has tile_index {c.tile_index}, but there are {len(tiles)} tiles")

    ordered = sorted(chunks, key=lambda c: (len(norm_text(c.text)), c.conf, g.area(c.bbox)), reverse=True)
    kept: list[OcrChunk] = []
    for c in ordered:
        for idx, k in enumerate(kept):
            match = _same_text(c, k, cfg) if _read_twice(c, k, tiles) else None
            if match is None:
                continue
            winner, reason = match
            loser = k if winner is c else c
            if winner is c:
                kept[idx] = c
                _redirect(log, k, c)
            if log is not None:
                log.append(DedupDecision(dropped=loser, kept=winner, reason=reason))
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


def _same_text(c: OcrChunk, k: OcrChunk, cfg: DedupConfig) -> tuple[OcrChunk, str] | None:
    """If c and k are two readings of one text, return the one to keep and why. None keeps both."""
    iou = g.iou(c.bbox, k.bbox)
    overlap = g.overlap_ratio_min(c.bbox, k.bbox)
    near = iou >= cfg.iou or overlap >= cfg.overlap_min or g.center_distance(c.bbox, k.bbox) <= cfg.center_dist_px
    if not near:
        return None

    # 1) almost the same box: two readings of one text, whatever they say
    if iou >= cfg.very_high_iou:
        return _better(c, k), f"same box (IoU={iou:.2f})"

    # 2) same text close by
    tc, tk = norm_text(c.text), norm_text(k.text)
    if tc and tc == tk:
        return _better(c, k), "same text nearby"

    # 3) one box inside the other, or mostly covering it: a cut-off fragment, or another reading
    if g.contains(c.bbox, k.bbox) or g.contains(k.bbox, c.bbox) or overlap >= cfg.overlap_min:
        short, long_ = (c, k) if len(tc) <= len(tk) else (k, c)
        if is_fragment(short.text, long_.text):
            return long_, f"fragment (overlap={overlap:.2f})"
        return _better(c, k), f"overlapping box (overlap={overlap:.2f})"

    return None  # different text that happens to be close: keep both


def _better(a: OcrChunk, b: OcrChunk) -> OcrChunk:
    """Which of two readings to keep: higher confidence, then longer text, then the larger box."""
    if a.conf != b.conf:
        return a if a.conf > b.conf else b
    la, lb = len(norm_text(a.text)), len(norm_text(b.text))
    if la != lb:
        return a if la > lb else b
    if g.area(a.bbox) != g.area(b.bbox):
        return a if g.area(a.bbox) > g.area(b.bbox) else b
    return a


def _redirect(log: list[DedupDecision] | None, old: OcrChunk, new: OcrChunk) -> None:
    """`new` has replaced `old`: earlier decisions that kept `old` now point at `new`."""
    for d in log or ():
        if d.kept is old:
            d.kept = new
