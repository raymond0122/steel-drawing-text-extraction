"""The 3,500 px tiled run again, with grayscale tiles, as the app now sends a grayscale page like this one.

Run from the repository root, so .env is found:

    python experiments/tiling/run_grayscale.py

Saves results/tiled_grayscale.json (skipped when it exists, so the requests are never paid
for twice) and compares its raw OCR with results/tiled.json, which was sent as RGB.
"""

from __future__ import annotations

import collections
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from steel_text.azure_ocr import AzureReadClient
from steel_text.ocr_stage import run_tiled_ocr
from steel_text.page_image import open_page
from steel_text.settings import load_settings

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
DRAWING = HERE / "steel_framing_plan_S-201_400dpi.png"
REQUEST_SLEEP_S = 0.2  # the same pacing as the app's live mode
OVERLAP = 300  # as in results/tiled.json, which this run is compared with


def main() -> int:
    out = RESULTS / "tiled_grayscale.json"
    if out.exists():
        print("tiled_grayscale: cached")
    else:
        page = open_page(DRAWING)
        assert page.mode == "L", f"expected a grayscale page, got {page.mode}"
        t0 = time.perf_counter()
        client = AzureReadClient.from_settings(load_settings())
        r = run_tiled_ocr(page, client, overlap=OVERLAP, request_sleep_s=REQUEST_SLEEP_S)
        secs = time.perf_counter() - t0
        record = {
            "name": "tiled_grayscale",
            "requests": len(r.tiles),
            "seconds": round(secs, 1),
            "tile_size": r.tiling.tile_size,
            "overlap": r.tiling.overlap,
            "scale": 1.0,
            "raw_lines": len(r.raw),
            "tiles": [list(t.box) for t in r.tiles],
            "raw": [asdict(c) for c in r.raw],  # page coordinates, before dedup
            "dedup_log": [str(d) for d in r.dedup_log],
            "lines": [asdict(c) for c in r.merged],  # page coordinates, after dedup
        }
        out.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")

    rgb = json.loads((RESULTS / "tiled.json").read_text(encoding="utf-8"))
    gray = json.loads(out.read_text(encoding="utf-8"))

    def key(c: dict) -> tuple:
        return (c["tile_index"], c["text"], tuple(c["bbox"]))

    a, b = collections.Counter(map(key, rgb["raw"])), collections.Counter(map(key, gray["raw"]))
    print(
        f"raw lines: RGB {sum(a.values())}, grayscale {sum(b.values())}, "
        f"the same tile, text and box: {sum((a & b).values())}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
