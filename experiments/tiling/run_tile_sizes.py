"""Experiment 2: the same drawing, tiled at several tile sizes. Saves the raw OCR of each.

Every tile is cut from the page at full resolution, so the letters stay the same
size (beam labels about 31 px tall) and only the image sent per request changes.
The overlap stays at 300 px. 3,500 px is experiment 1's results/tiled.json.

Run from the repository root, so .env is found:

    python experiments/tiling/run_tile_sizes.py

Each result is saved to results/tile_<size>.json. A size whose file already exists
is skipped, so re-running never pays for the same requests twice.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import asdict
from pathlib import Path

from steel_text.azure_ocr import AzureOcrError, AzureReadClient
from steel_text.ocr_stage import run_tiled_ocr
from steel_text.page_image import open_page
from steel_text.settings import load_settings
from steel_text.tiling import OVERLAP, TilingChoice

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
DRAWING = HERE / "steel_framing_plan_S-201_400dpi.png"
SIZES = [10000, 8000, 6000, 4500, 3000, 2500, 2000]  # fewest requests first
REQUEST_SLEEP_S = 0.2  # the same pacing as the app's live mode


class RetryCounter(logging.Handler):
    """Counts the client's retry warnings, so waiting time can be told apart from reading time."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.retries = 0
        self.rate_limited = 0

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "retry" in msg:
            self.retries += 1
            self.rate_limited += "HTTP 429" in msg


def main() -> int:
    RESULTS.mkdir(exist_ok=True)
    page = open_page(DRAWING)
    print(f"drawing {page.width}x{page.height}", flush=True)
    client = AzureReadClient.from_settings(load_settings())
    counter = RetryCounter()
    logging.getLogger("steel_text.azure_ocr").addHandler(counter)

    for size in SIZES:
        out = RESULTS / f"tile_{size}.json"
        if out.exists():
            print(f"tile_{size}: cached", flush=True)
            continue
        counter.retries = counter.rate_limited = 0
        record: dict[str, object] = {"name": f"tile_{size}", "tile_size": size, "overlap": OVERLAP}
        t0 = time.perf_counter()
        try:
            r = run_tiled_ocr(
                page,
                client,
                tiling=TilingChoice(size, OVERLAP, f"experiment: {size} px tiles"),
                request_sleep_s=REQUEST_SLEEP_S,
            )
        except AzureOcrError as e:
            record["error"] = str(e)[:500]
            print(f"tile_{size}: FAILED: {str(e)[:200]}", flush=True)
        else:
            record.update(
                requests=len(r.tiles),
                raw_lines=len(r.raw),
                tiles=[list(t.box) for t in r.tiles],
                raw=[asdict(c) for c in r.raw],  # page coordinates, before dedup
            )
            print(
                f"tile_{size}: {len(r.tiles)} requests, {len(r.raw)} raw lines, "
                f"{time.perf_counter() - t0:.0f}s, {counter.rate_limited} rate-limit retries",
                flush=True,
            )
        record["seconds"] = round(time.perf_counter() - t0, 1)
        record["retries"] = counter.retries
        record["rate_limited"] = counter.rate_limited
        out.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
