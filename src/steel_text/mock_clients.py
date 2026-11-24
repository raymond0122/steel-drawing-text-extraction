"""Offline stand-ins for Azure OCR and the LLM, so the demo runs without API keys.

These are simulations for demonstration only:
  * SimulatedOcr reads the known text positions of the synthetic drawing and
    mimics what OCR does at tile edges: text cut by an edge comes back as a
    partial string with a clipped box, and boxes jitter by a pixel or two.
  * RuleBasedLabeler mimics the LLM's output format with simple patterns.
"""

from __future__ import annotations

import random
import re
from collections.abc import Iterable, Sequence
from typing import Any

from PIL import Image

from .llm_filter import LlmCallError
from .ocr_types import OcrLine, TileReadError
from .sample_drawing import GroundTruthText
from .tiling import Tile


class SimulatedOcr:
    """Reads the synthetic drawing's known text. `fail_calls` makes chosen tiles fail (0-based, in reading order)."""

    def __init__(
        self,
        truth: Iterable[GroundTruthText],
        *,
        jitter_px: int = 2,
        min_visible_height: float = 0.6,
        fail_calls: set[int] | None = None,
    ):
        self.truth = list(truth)
        self.jitter_px = jitter_px
        self.min_visible_height = min_visible_height
        self.fail_calls = fail_calls or set()
        self.calls = 0

    def read_tile(self, tile_img: Image.Image, tile: Tile) -> list[OcrLine]:
        call, self.calls = self.calls, self.calls + 1
        if call in self.fail_calls:
            raise TileReadError("HTTP 500 (simulated failure)")
        lines: list[OcrLine] = []
        for i, t in enumerate(self.truth):
            x0, y0, x1, y1 = t.bbox
            vx0, vy0 = max(x0, tile.x0), max(y0, tile.y0)
            vx1, vy1 = min(x1, tile.x1), min(y1, tile.y1)
            if vx1 <= vx0 or vy1 <= vy0:
                continue  # not in this tile
            if (vy1 - vy0) / max(1, y1 - y0) < self.min_visible_height:
                continue  # text cut horizontally is unreadable

            text = t.ocr_text
            visible = (vx1 - vx0) / max(1, x1 - x0)
            if visible < 0.98:  # cut by a vertical tile edge: keep the visible characters only
                n = int(len(text) * visible)
                text = (text[len(text) - n :] if x0 < tile.x0 else text[:n]).strip()
                if not text:
                    continue

            rng = random.Random(hash((tile.box, i)))
            jx0, jy0, jx1, jy1 = (v + rng.randint(-self.jitter_px, self.jitter_px) for v in (vx0, vy0, vx1, vy1))
            box = (jx0 - tile.x0, jy0 - tile.y0, jx1 - tile.x0, jy1 - tile.y0)
            lines.append(OcrLine(text=text, bbox=box))
        return lines


# prefix -> (pattern for the rest of the label, material kind); longest prefixes first
_SECTIONS = [
    ("HSS", r"\d+(\.\d+)?X\d+(\.\d+)?(X\d+(\.\d+)?)?", "HSS"),
    ("WT", r"\d+(\.\d+)?[X*]\d+(\.\d+)?", "Other"),
    ("HP", r"\d+X\d+(\.\d+)?", "H"),
    ("MC", r"\d+(X\d+(\.\d+)?)?", "C"),
    ("W", r"\d+X\d+(\.\d+)?", "H"),
    ("S", r"\d+X\d+(\.\d+)?", "H"),
    ("L", r"\d+(\.\d+)?X\d+(\.\d+)?X\d+(\.\d+)?", "L"),
    ("C", r"\d+(X\d+(\.\d+)?)?", "C"),
]
_CONFUSIONS = str.maketrans({"O": "0", "S": "5", "I": "1"})


def match_section(raw: str) -> dict[str, str] | None:
    s = re.sub(r"\s+", "", raw.upper())
    for prefix, rest_pattern, kind in _SECTIONS:
        if s.startswith(prefix):
            rest = s[len(prefix) :].translate(_CONFUSIONS)  # fix O/0, S/5, I/1 after the prefix
            if re.fullmatch(rest_pattern, rest):
                return {"normalized_label": prefix + rest, "material_kind": kind}
    return None


class RuleBasedLabeler:
    """Returns the same JSON shape as the real LLM. `fail_calls` makes chosen calls fail (0-based)."""

    def __init__(self, fail_calls: set[int] | None = None):
        self.fail_calls = fail_calls or set()
        self.calls = 0

    def extract(self, items: Sequence[dict[str, str]]) -> dict[str, Any]:
        call, self.calls = self.calls, self.calls + 1
        if call in self.fail_calls:
            raise LlmCallError("HTTP 500 (simulated failure)")
        entries = []
        for item in items:
            m = match_section(item["raw_text"])
            if m:
                entries.append({"str_id": item["id"], **m})
        return {"entries": entries}
