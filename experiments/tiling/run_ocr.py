"""Experiment 1: send one drawing to Azure OCR in several ways and save the raw results.

    tiled        the project's own path: overlapping tiles at full resolution, then dedup
    whole_N      the whole page shrunk so its long side is N pixels, sent as one image

Run from the repository root, so .env is found:

    python experiments/tiling/run_ocr.py

Each result is saved to results/<name>.json. A condition whose file already exists
is skipped, so re-running never pays for the same request twice.
"""

from __future__ import annotations

import io
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from PIL import Image

from steel_text.azure_ocr import AzureOcrError, AzureReadClient
from steel_text.ocr_stage import run_tiled_ocr
from steel_text.settings import load_settings
from steel_text.tiling import Tile

Image.MAX_IMAGE_PIXELS = None

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
DRAWING = HERE / "steel_framing_plan_S-201_400dpi.png"
WHOLE_LONG_SIDES = [14400, 10000, 8000, 6000, 4000, 3000]


def main() -> int:
    RESULTS.mkdir(exist_ok=True)
    page = Image.open(DRAWING).convert("RGB")
    width, height = page.size
    print(f"drawing {width}x{height}")
    client = AzureReadClient.from_settings(load_settings())

    # --- tiled, full resolution (what the project does) ---------------------------------
    out = RESULTS / "tiled.json"
    if out.exists():
        print("tiled: cached")
    else:
        t0 = time.perf_counter()
        r = run_tiled_ocr(page, client)
        secs = time.perf_counter() - t0
        out.write_text(
            json.dumps(
                {
                    "name": "tiled",
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
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"tiled: {len(r.tiles)} requests, {len(r.raw)} raw -> {len(r.merged)} merged lines, {secs:.0f}s")

    # --- whole page, shrunk to a given long side ------------------------------------------
    for long_side in WHOLE_LONG_SIDES:
        name = f"whole_{long_side}"
        out = RESULTS / f"{name}.json"
        if out.exists():
            print(f"{name}: cached")
            continue
        scale = long_side / max(width, height)
        if scale < 1.0:
            img = page.resize((round(width * scale), round(height * scale)), Image.LANCZOS)
        else:
            img = page
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        mb = len(buf.getvalue()) / 1e6
        record: dict[str, object] = {
            "name": name,
            "requests": 1,
            "image_size": list(img.size),
            "png_mb": round(mb, 2),
            "scale": scale,
        }
        t0 = time.perf_counter()
        try:
            lines = client.read_tile(img, Tile(0, 0, img.size[0], img.size[1]))
        except AzureOcrError as e:
            record["error"] = str(e)[:500]
            record["lines"] = []
            print(f"{name}: {img.size[0]}x{img.size[1]} {mb:.1f} MB -> REJECTED: {str(e)[:160]}")
        else:
            # Boxes come back in the shrunk image's pixels; map them to the original page.
            record["lines"] = [{"text": ln.text, "bbox": [round(v / scale) for v in ln.bbox]} for ln in lines]
            print(f"{name}: {img.size[0]}x{img.size[1]} {mb:.1f} MB -> {len(lines)} lines")
        record["seconds"] = round(time.perf_counter() - t0, 1)
        out.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
