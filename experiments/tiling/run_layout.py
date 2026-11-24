"""Experiment 4, with Azure: the app's tile layout against evenly spread tiles.

Run from the repository root, so .env is found:

    python experiments/tiling/run_layout.py

  layout_asis_even        the sheet as it is, evenly spread tiles (the app's layout is results/tiled.json)
  layout_shift1400_app    the sheet moved 1,400 px right on a wider page: the app's layout cuts two beam labels
  layout_shift1400_even   the same page, evenly spread tiles: no label is cut

Each condition is saved to results/<name>.json and never sent again. Both pages are grayscale,
as the app sends this sheet; that reads the same as RGB (results/tiled_grayscale.json).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from analyze import alnum, inter, norm, overlapping, read_as, score_labels, short_side
from layout_geometry import even_tiles, held_whole, texts
from PIL import Image

from steel_text import ocr_stage
from steel_text.azure_ocr import AzureReadClient
from steel_text.dedup import merge_tile_results
from steel_text.ocr_types import OcrChunk
from steel_text.page_image import open_page
from steel_text.settings import load_settings
from steel_text.tiling import Tile, make_tiles

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
SHIFT = 1_400


def even_layout(width: int, height: int, *, tile_size: int, overlap: int) -> list[Tile]:
    return even_tiles(width, height, tile_size=tile_size, overlap=overlap)


def run(name: str, page: Image.Image, layout) -> dict:
    out = RESULTS / f"{name}.json"
    if out.exists():
        return json.loads(out.read_text(encoding="utf-8"))
    ocr_stage.make_tiles = layout  # run_tiled_ocr cuts the page with this layout
    try:
        t0 = time.perf_counter()
        r = ocr_stage.run_tiled_ocr(page, AzureReadClient.from_settings(load_settings()), request_sleep_s=0.2)
        secs = time.perf_counter() - t0
    finally:
        ocr_stage.make_tiles = make_tiles
    record = {
        "name": name,
        "seconds": round(secs, 1),
        "page": list(page.size),
        "tiles": [list(t.box) for t in r.tiles],
        "failed": r.failed_tiles,
        "raw": [asdict(c) for c in r.raw],
        "lines": [asdict(c) for c in r.merged],
    }
    out.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{name}: {len(r.tiles)} tiles, {secs:.0f} s", flush=True)
    return record


def moved(lines: list[dict], dx: int) -> list[dict]:
    return [{**ln, "bbox": [ln["bbox"][0] + dx, ln["bbox"][1], ln["bbox"][2] + dx, ln["bbox"][3]]} for ln in lines]


def read_as_one_line(ref: list[int], lines: list[dict]) -> bool:
    """Some line overlaps the reference line and spans 95% of its width."""
    width = ref[2] - ref[0]
    return any(
        inter(ref, ln["bbox"]) and min(ref[2], ln["bbox"][2]) - max(ref[0], ln["bbox"][0]) >= 0.95 * width
        for ln in lines
    )


def main() -> None:
    answer_key = json.loads((HERE / "ground_truth.json").read_text(encoding="utf-8"))
    tiled = json.loads((RESULTS / "tiled.json").read_text(encoding="utf-8"))
    raw = [OcrChunk(c["text"], tuple(c["bbox"]), c["tile_index"]) for c in tiled["raw"]]
    app_lines = [asdict(c) for c in merge_tile_results(raw, [Tile(*t) for t in tiled["tiles"]])]
    small = [
        ln
        for ln in tiled["lines"]
        if short_side(ln["bbox"]) < 34 and len(alnum(ln["text"])) >= 3 and ln["text"] != "111"
    ]

    page = open_page(HERE / "steel_framing_plan_S-201_400dpi.png")
    wide = Image.new(page.mode, (page.width + SHIFT, page.height), 255)
    wide.paste(page, (SHIFT, 0))
    conditions = {  # name -> (merged lines in the sheet's own coordinates, tiles)
        "as it is, app's layout": (app_lines, len(tiled["tiles"])),
    }
    for name, img, layout, dx in (
        ("layout_asis_even", page, even_layout, 0),
        ("layout_shift1400_app", wide, make_tiles, SHIFT),
        ("layout_shift1400_even", wide, even_layout, SHIFT),
    ):
        r = run(name, img, layout)
        conditions[name] = (moved(r["lines"], -dx), len(r["tiles"]))

    print("\ncondition               tiles | beam labels exact  section  camber | smallest lines exact")
    for name, (lines, n) in conditions.items():
        s = score_labels(answer_key, lines)["counts"]
        smallest = sum(1 for ln in small if (t := read_as(ln, lines)) is not None and norm(t) == norm(ln["text"]))
        print(
            f"{name:23} {n:5} |  {s['exact']:3}/82   {s['section']:3}/82   {s['camber']:2}/{s['camber_total']}"
            f"  |  {smallest}/{len(small)}"
        )

    app_moved = make_tiles(page.width + SHIFT, page.height, tile_size=3500, overlap=300)
    cut_labels = [g for g in answer_key if not held_whole(moved([g], SHIFT)[0]["bbox"], app_moved)]
    print("\nThe beam labels the app's layout cuts on the moved page, as each condition read them:")
    for name, (lines, _) in conditions.items():
        reads = [" + ".join(c["text"] for c in overlapping(g, lines)) or "(nothing)" for g in cut_labels]
        print(f"  {name:23} {reads}")

    _, text_lines = texts()
    app_tiles, even = [Tile(*t) for t in tiled["tiles"]], even_tiles(page.width, page.height)
    newly_cut = [b for b in text_lines if held_whole(b, app_tiles) and not held_whole(b, even)]
    print(f"\nThe {len(newly_cut)} text lines that only even spreading cuts on the sheet as it is, read as one line:")
    for name in ("as it is, app's layout", "layout_asis_even"):
        print(f"  {name:23} {sum(read_as_one_line(b, conditions[name][0]) for b in newly_cut)}/{len(newly_cut)}")


if __name__ == "__main__":
    main()
