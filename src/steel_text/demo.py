"""End-to-end demo of the text-extraction layer.

    steel-demo                           # offline: synthetic drawing + simulated OCR/LLM
    steel-demo --simulate-ocr-failure    # offline, and one OCR tile fails on purpose
    steel-demo --simulate-llm-failure    # offline, and one LLM batch fails on purpose
    steel-demo --live                    # real Azure OCR + OpenAI (needs .env)
    steel-demo --live --image page.png   # real services on your own image

`python -m steel_text.demo` is the same command. Images and JSON are written to ./out.

Exit code: 0 when the whole page was read and checked; 1 when the outputs were
written but are incomplete, because an OCR tile was not read or an LLM batch was
not checked; 2 when there is nothing to write.
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
from .llm_filter import DEFAULT_BATCH_SIZE, FilterResult
from .ocr_stage import TiledOcrResult
from .ocr_types import TileReadError
from .overlay import Preview, draw_boxes, draw_dedup_closeups, draw_not_read, draw_tile_grid
from .page_image import PageTooLargeError, open_page
from .pipeline import OFFLINE_BATCH_SIZE, MissingCredentialsError, make_clients, run_pipeline
from .sample_drawing import GroundTruthText, check_truth, load_truth, save_sample
from .settings import load_settings
from .tiling import DEFAULT_MAX_TILES, OVERLAP, TileLimitError

RAW_OCR_COLOR = (220, 38, 38)  # red: every OCR line, before dedup
MERGED_OCR_COLOR = (22, 140, 60)  # green: OCR lines after dedup
LABEL_COLOR = (230, 110, 0)  # orange: steel labels from the LLM filter


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="call real Azure OCR and OpenAI")
    ap.add_argument("--image", type=Path, help="page image to process (live mode only)")
    ap.add_argument("--out", type=Path, default=Path("out"))
    ap.add_argument(
        "--batch-size",
        type=int,
        help=f"OCR lines per LLM request (default: {DEFAULT_BATCH_SIZE} live, {OFFLINE_BATCH_SIZE} offline)",
    )
    ap.add_argument("--max-tiles", type=int, default=DEFAULT_MAX_TILES)
    ap.add_argument(
        "--overlap",
        type=int,
        default=OVERLAP,
        help=f"px by which tiles overlap (default {OVERLAP}: longer than the longest label at 400 dpi)",
    )
    ap.add_argument("--simulate-ocr-failure", action="store_true", help="offline only: make OCR tile 1 fail")
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
        return open_page(image), None
    img_path, truth_path = save_sample(out)
    print(f"Synthetic drawing: {img_path}")
    return open_page(img_path), load_truth(truth_path)


def write_gaps(path: Path, gaps: list[dict[str, object]]) -> None:
    """Write the parts of the page that are missing from the results, or remove the file when there are none."""
    if gaps:
        path.write_text(json.dumps(gaps, indent=2), encoding="utf-8")
    else:
        path.unlink(missing_ok=True)  # don't leave a previous run's gaps next to a complete result


def save_step(img: Image.Image, preview: Preview, ocr: TiledOcrResult, path: Path) -> None:
    """Save a step image drawn on the preview, with the tiles OCR could not read hatched."""
    not_read = [preview.box(box) for _, box, _ in ocr.not_read]
    (draw_not_read(img, not_read) if not_read else img).save(path)


def write_ocr_outputs(page: Image.Image, preview: Preview, ocr: TiledOcrResult, out: Path) -> None:
    """Tile grid, OCR boxes before and after dedup, dedup close-ups, the dedup log, and the areas not read.

    The step images are drawn on the preview; the close-ups are cut from the full page.
    """
    tiles = [preview.box(t.box) for t in ocr.tiles]
    save_step(draw_tile_grid(preview.image, tiles), preview, ocr, out / "01_tile_grid.png")
    raw_boxes = ((preview.box(c.bbox), f"{c.text}  [t{c.tile_index}]") for c in ocr.raw)
    save_step(draw_boxes(preview.image, raw_boxes, RAW_OCR_COLOR), preview, ocr, out / "02_raw_ocr.png")
    merged_boxes = ((preview.box(c.bbox), c.text) for c in ocr.merged)
    save_step(draw_boxes(preview.image, merged_boxes, MERGED_OCR_COLOR), preview, ocr, out / "03_merged_ocr.png")
    if ocr.dedup_log:
        draw_dedup_closeups(page, ocr.raw, ocr.merged, ocr.dedup_log).save(out / "03b_dedup_closeups.png")
    (out / "dedup_log.txt").write_text("".join(f"{d}\n" for d in ocr.dedup_log), encoding="utf-8")
    write_gaps(out / "not_read.json", [{"tile": i, "box": list(box), "error": error} for i, box, error in ocr.not_read])


def write_label_outputs(preview: Preview, ocr: TiledOcrResult, result: FilterResult, out: Path) -> None:
    """Labels drawn on the page, as JSON, and as an Excel list, plus the lines the LLM never checked."""
    label_boxes = ((preview.box(e.bbox), f"{e.normalized_label} ({e.material_kind})") for e in result.entries)
    save_step(draw_boxes(preview.image, label_boxes, LABEL_COLOR, font_size=18), preview, ocr, out / "04_labels.png")
    labels = [asdict(e) for e in result.entries]
    (out / "labels.json").write_text(json.dumps(labels, indent=2), encoding="utf-8")
    write_label_sheet(
        (e.normalized_label for e in result.entries),
        out / "labels.xlsx",
        not_read=ocr.not_read,
        not_checked=result.not_checked,
    )
    write_gaps(out / "not_checked.json", [n._asdict() for n in result.not_checked])


def print_summary(ocr: TiledOcrResult, result: FilterResult, truth: list[GroundTruthText] | None) -> None:
    print(f"\nTiles: {len(ocr.tiles)}. {ocr.tiling.reason}")
    for i, box, error in ocr.not_read:
        print(f"  ! tile {i} {box} NOT READ ({error}). Text in that area is missing from the results.")
    print(f"OCR lines: {len(ocr.raw)} raw -> {len(ocr.merged)} after dedup")
    for line in ocr.dedup_log:
        print(f"  - {line}")

    print(f"\nLLM batches: {result.batches}, failed: {len(result.failed_batches)}")
    for b, err in result.failed_batches:
        print(f"  - batch {b} skipped: {err}")
    if result.not_checked:
        print(
            f"  ! {len(result.not_checked)} lines were never checked and may hold labels: "
            "listed in not_checked.json and on the 'Not checked' sheet of labels.xlsx."
        )
    print(f"Steel labels found: {len(result.entries)}")
    for e in result.entries:
        corrected = e.normalized_label != e.raw_text.replace(" ", "").upper()
        note = f"   (OCR read '{e.raw_text}')" if corrected else ""
        print(f"  - {e.normalized_label:<16} {e.material_kind:<6}{note}")

    if truth is None:
        return
    if ocr.failed_tiles or result.failed_batches:
        print("\nCheck against ground truth skipped: part of the page was not read or not checked.")
    else:
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

    try:
        page, truth = load_page(args.image, out)
    except PageTooLargeError as e:
        print(f"Page image too large: {e}")
        return 2
    except OSError as e:  # a missing or unreadable --image
        print(f"Could not load the page: {e}")
        return 2
    settings = load_settings() if args.live else None
    try:
        ocr_client, labeler = make_clients(
            args.live,
            truth,
            settings=settings,
            simulate_ocr_failure=args.simulate_ocr_failure,
            simulate_llm_failure=args.simulate_llm_failure,
        )
    except MissingCredentialsError as e:
        print(f"Live mode needs credentials ({e}). Copy .env.example to .env and fill in the keys.")
        return 2
    mode = "LIVE (Azure Document Intelligence + OpenAI)" if args.live else "OFFLINE (simulated OCR and LLM)"
    print(f"Mode: {mode}\n")

    try:
        outcome = run_pipeline(
            page,
            ocr_client,
            labeler,
            live=args.live,
            max_tiles=args.max_tiles,
            overlap=args.overlap,
            batch_size=args.batch_size,
        )
    except TileLimitError as e:
        print(f"Too few tiles allowed: {e}. Raise --max-tiles.")
        return 2
    except TileReadError as e:  # no tile could be read at all
        print(f"OCR failed: {e}")
        return 2
    preview = Preview.of(page)
    write_ocr_outputs(page, preview, outcome.ocr, out)
    write_label_outputs(preview, outcome.ocr, outcome.labels, out)

    print_summary(outcome.ocr, outcome.labels, truth)
    print(f"\nImages and JSON written to {out.resolve()}")
    if outcome.ocr.failed_tiles or outcome.labels.failed_batches:
        print("INCOMPLETE: part of the page was not read or not checked (see above).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
