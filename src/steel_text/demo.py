"""End-to-end demo of the text-extraction layer.

    steel-demo                           # offline: synthetic drawing + simulated OCR/LLM
    steel-demo --simulate-llm-failure    # offline, and one LLM batch fails on purpose
    steel-demo --live                    # real Azure OCR + OpenAI (needs .env)
    steel-demo --live --image page.png   # real services on your own image

`python -m steel_text.demo` is the same command. Images and JSON are written to ./out.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from PIL import Image

from .label_sheet import write_label_sheet
from .llm_filter import FilterResult
from .ocr_stage import TiledOcrResult
from .overlay import draw_boxes, draw_dedup_closeups, draw_tile_grid, save_preview
from .pipeline import MissingCredentialsError, check_truth, make_clients, run_pipeline
from .sample_drawing import GroundTruthText, load_truth, save_sample
from .settings import load_settings

Image.MAX_IMAGE_PIXELS = None  # large drawings exceed PIL's default safety limit

RAW_OCR_COLOR = (220, 38, 38)  # red: every OCR line, before dedup
MERGED_OCR_COLOR = (22, 140, 60)  # green: OCR lines after dedup
LABEL_COLOR = (230, 110, 0)  # orange: steel labels from the LLM filter


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="call real Azure OCR and OpenAI")
    ap.add_argument("--image", type=Path, help="page image to process (live mode only)")
    ap.add_argument("--out", type=Path, default=Path("out"))
    ap.add_argument("--batch-size", type=int, default=10, help="OCR lines per LLM request")
    ap.add_argument("--max-tiles", type=int, default=20)
    ap.add_argument("--simulate-llm-failure", action="store_true", help="offline only: make LLM batch 1 fail")
    args = ap.parse_args()
    if args.image and not args.live:
        ap.error("--image needs --live: the offline simulator only knows the synthetic drawing")
    return args


def load_page(image: Path | None, out: Path) -> tuple[Image.Image, list[GroundTruthText] | None]:
    """Return the page to process and its ground truth.

    A user-supplied image has no ground truth, so it comes back as None. Otherwise
    the synthetic drawing and its ground truth are written to `out` and read back.
    """
    if image:
        return Image.open(image).convert("RGB"), None
    img_path, truth_path = save_sample(out)
    print(f"Synthetic drawing: {img_path}")
    return Image.open(img_path).convert("RGB"), load_truth(truth_path)


def write_ocr_outputs(page: Image.Image, ocr: TiledOcrResult, out: Path) -> None:
    """Tile grid, OCR boxes before and after dedup, dedup close-ups and the dedup log."""
    save_preview(draw_tile_grid(page, ocr.tiles), out / "01_tile_grid.png")
    raw_boxes = ((c.bbox, f"{c.text}  [t{c.tile_index}]") for c in ocr.raw)
    save_preview(draw_boxes(page, raw_boxes, RAW_OCR_COLOR), out / "02_raw_ocr.png")
    merged_boxes = ((c.bbox, c.text) for c in ocr.merged)
    save_preview(draw_boxes(page, merged_boxes, MERGED_OCR_COLOR), out / "03_merged_ocr.png")
    if ocr.dedup_log:
        draw_dedup_closeups(page, ocr.raw, ocr.merged, ocr.dedup_log).save(out / "03b_dedup_closeups.png")
    (out / "dedup_log.txt").write_text("".join(f"{d}\n" for d in ocr.dedup_log), encoding="utf-8")


def write_label_outputs(page: Image.Image, result: FilterResult, out: Path) -> None:
    """Labels drawn on the page, as JSON, and as an Excel list."""
    label_boxes = ((e.bbox, f"{e.normalized_label} ({e.material_kind})") for e in result.entries)
    save_preview(draw_boxes(page, label_boxes, LABEL_COLOR, font_size=54), out / "04_labels.png")
    labels = [asdict(e) for e in result.entries]
    (out / "labels.json").write_text(json.dumps(labels, indent=2), encoding="utf-8")
    write_label_sheet((e.normalized_label for e in result.entries), out / "labels.xlsx")


def print_summary(ocr: TiledOcrResult, result: FilterResult, truth: list[GroundTruthText] | None) -> None:
    print(f"\nTiles: {len(ocr.tiles)} (tile_size={ocr.tile_size}px, overlap={ocr.overlap}px)")
    print(f"OCR lines: {len(ocr.raw)} raw -> {len(ocr.merged)} after dedup")
    for line in ocr.dedup_log:
        print(f"  - {line}")

    print(f"\nLLM batches: {result.batches}, failed: {len(result.failed_batches)}")
    for b, err in result.failed_batches:
        print(f"  - batch {b} skipped: {err}")
    print(f"Steel labels found: {len(result.entries)}")
    for e in result.entries:
        corrected = e.normalized_label != e.raw_text.replace(" ", "").upper()
        note = f"   (OCR read '{e.raw_text}')" if corrected else ""
        print(f"  - {e.normalized_label:<16} {e.material_kind:<6}{note}")

    # Skipped batches would show up as missing labels, so only compare a complete run.
    if truth is not None and not result.failed_batches:
        check = check_truth(truth, result.entries)
        print(
            f"\nCheck against ground truth: {check.found}/{len(check.expected)} labels found"
            + (f", missing {check.missing}" if check.missing else "")
            + (f", unexpected {check.unexpected}" if check.unexpected else "")
        )


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    page, truth = load_page(args.image, out)
    settings = load_settings() if args.live else None
    try:
        ocr_client, labeler = make_clients(
            args.live, truth, settings=settings, simulate_llm_failure=args.simulate_llm_failure
        )
    except MissingCredentialsError as e:
        print(f"Live mode needs credentials ({e}). Copy .env.example to .env and fill in the keys.")
        return 2
    mode = "LIVE (Azure Document Intelligence + OpenAI)" if args.live else "OFFLINE (simulated OCR and LLM)"
    print(f"Mode: {mode}\n")

    outcome = run_pipeline(
        page, ocr_client, labeler, live=args.live, max_tiles=args.max_tiles, batch_size=args.batch_size
    )
    write_ocr_outputs(page, outcome.ocr, out)
    write_label_outputs(page, outcome.labels, out)

    print_summary(outcome.ocr, outcome.labels, truth)
    print(f"\nImages and JSON written to {out.resolve()}")
    return 0 if outcome.labels.ok else 1


if __name__ == "__main__":
    sys.exit(main())
