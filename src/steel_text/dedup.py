"""Merge OCR results from overlapping tiles.

Overlapping tiles cause two kinds of noise:
  * duplicates: text inside an overlap band is read by two (or four) tiles
  * fragments:  text crossing a tile edge comes back cut off, e.g. "SEE"
                from one tile and "SEE NOTE 6" from its neighbour

Both come from two tiles reading the same area, so only chunks from two
different tiles, inside the area both tiles cover, are compared. One OCR pass
doesn't read a text twice: chunks from the same tile are always kept, even a
label printed twice close together.

Two passes remove them. Every decision is recorded in `log` (DedupDecision)
so the demo can show why each chunk was dropped.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from . import geometry as g
from .ocr_types import OcrChunk
from .text_norm import digit_tokens, is_fragment, norm_text, tokens
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


def _better(a: OcrChunk, b: OcrChunk) -> OcrChunk:
    """Which of two conflicting chunks to keep: confidence, then text length, then box size."""
    if a.conf != b.conf:
        return a if a.conf > b.conf else b
    la, lb = len(norm_text(a.text)), len(norm_text(b.text))
    if la != lb:
        return a if la > lb else b
    if g.area(a.bbox) != g.area(b.bbox):
        return a if g.area(a.bbox) > g.area(b.bbox) else b
    return a


def _from_overlap(a: OcrChunk, b: OcrChunk, tiles: Sequence[Tile] | None) -> bool:
    """Could a and b be two readings of one text? Only if two different tiles read them where they overlap.

    Chunks with no tile (tile_index -1) are always compared. Without `tiles`,
    any two different tiles count.
    """
    if a.tile_index < 0 or b.tile_index < 0:
        return True
    if a.tile_index == b.tile_index:
        return False
    if tiles is None:
        return True
    shared = g.intersection_box(tiles[a.tile_index].box, tiles[b.tile_index].box)
    return shared is not None and g.intersection_area(a.bbox, shared) > 0 and g.intersection_area(b.bbox, shared) > 0


def _redirect(log: list[DedupDecision] | None, old: OcrChunk, new: OcrChunk) -> None:
    """`new` has replaced `old`: earlier decisions that kept `old` now point at `new`."""
    for d in log or ():
        if d.kept is old:
            d.kept = new


def _near(a: g.BBox, b: g.BBox, cfg: DedupConfig) -> tuple[bool, float, float]:
    """Two boxes are 'near' if ANY of the three metrics says so."""
    iou = g.iou(a, b)
    ov = g.overlap_ratio_min(a, b)
    near = ov >= cfg.overlap_min or iou >= cfg.iou or g.center_distance(a, b) <= cfg.center_dist_px
    return near, ov, iou


def _resolve(c: OcrChunk, k: OcrChunk, cfg: DedupConfig) -> tuple[OcrChunk, str] | None:
    """Compare a new chunk c with an already-kept chunk k.

    Returns (winner, reason) if they are the same text, or None to keep both.
    """
    near, ov, iou = _near(c.bbox, k.bbox, cfg)
    if not near:
        return None
    tc, tk = norm_text(c.text), norm_text(k.text)

    # 1) almost the same box -> duplicate regardless of text
    if iou >= cfg.very_high_iou:
        return _better(c, k), f"same box (IoU={iou:.2f})"

    # 2) same text nearby
    if tc and tc == tk:
        return _better(c, k), "same text nearby"

    # 3) one box inside the other, or strong overlap
    if g.contains(c.bbox, k.bbox) or g.contains(k.bbox, c.bbox) or ov >= cfg.overlap_min:
        short, long_ = (c, k) if len(tc) <= len(tk) else (k, c)
        if is_fragment(short.text, long_.text):
            return long_, f"fragment (overlap={ov:.2f})"
        return _better(c, k), f"overlapping box (overlap={ov:.2f})"

    # 4) substring relationship with slightly different, overlapping boxes.
    #    Boxes that don't overlap are two texts, e.g. "C50" printed just above "C500".
    if tc and tk and (tc in tk or tk in tc) and g.intersection_area(c.bbox, k.bbox) > 0:
        return (c if len(tc) >= len(tk) else k), "substring overlapping"

    # 5) shares a number, one side is a short phrase ("TE 6" vs "SEE NOTE 6")
    ts_c, ts_k = tokens(tc), tokens(tk)
    if digit_tokens(ts_c) & digit_tokens(ts_k):
        if (len(ts_c) <= 2 and len(ts_k) >= 3) or (len(ts_k) <= 2 and len(ts_c) >= 3):
            return (c if len(tc) >= len(tk) else k), "short piece sharing a number"

    return None  # different text that happens to be close: keep both


def dedup_chunks(
    chunks: list[OcrChunk],
    cfg: DedupConfig = DedupConfig(),
    log: list[DedupDecision] | None = None,
    tiles: Sequence[Tile] | None = None,
) -> list[OcrChunk]:
    """Pass 1. Longest text first, so full strings are kept before their fragments show up."""
    ordered = sorted(chunks, key=lambda c: (len(norm_text(c.text)), c.conf, g.area(c.bbox)), reverse=True)
    kept: list[OcrChunk] = []
    for c in ordered:
        for idx, k in enumerate(kept):
            res = _resolve(c, k, cfg) if _from_overlap(c, k, tiles) else None
            if res is None:
                continue
            winner, reason = res
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


def drop_leftover_fragments(
    chunks: list[OcrChunk],
    cfg: DedupConfig = DedupConfig(),
    log: list[DedupDecision] | None = None,
    tiles: Sequence[Tile] | None = None,
) -> list[OcrChunk]:
    """Pass 2. Remove short strings that are still fragments of a nearby longer string."""
    ordered = sorted(chunks, key=lambda c: len(norm_text(c.text)), reverse=True)
    kept: list[OcrChunk] = []
    for c in ordered:
        tc = norm_text(c.text)
        drop_for = None
        for k in kept:
            tk = norm_text(k.text)
            if not tc or not tk or tc == tk or not _from_overlap(c, k, tiles) or not _near(c.bbox, k.bbox, cfg)[0]:
                continue
            if (tc in tk and g.intersection_area(c.bbox, k.bbox) > 0) or is_fragment(c.text, k.text):
                drop_for = k
                break
        if drop_for is None:
            kept.append(c)
        else:
            _redirect(log, c, drop_for)
            if log is not None:
                log.append(DedupDecision(dropped=c, kept=drop_for, reason="leftover fragment"))
    return kept


def merge_tile_results(
    chunks: list[OcrChunk],
    cfg: DedupConfig = DedupConfig(),
    log: list[DedupDecision] | None = None,
    tiles: Sequence[Tile] | None = None,
) -> list[OcrChunk]:
    """Both passes. Pass the tiles the chunks came from, so only their overlap areas are compared."""
    return drop_leftover_fragments(dedup_chunks(chunks, cfg, log, tiles), cfg, log, tiles)
