"""Experiment 5 with Azure and the LLM: a 300 px against a 350 px tile overlap, end to end.

Run from the repository root, so .env is found:

    python experiments/tiling/run_overlap.py

  asis           the sheet as it is
  shift1400      moved 1,400 px right on a wider page: 300 px cuts two W24x55 labels (experiment 4)
  shift1020      moved 1,020 px right: 300 px cuts two W18x35 labels

Each page is read at both overlaps (3,500 px tiles), and the merged lines go twice through the
LLM label filter (25 lines per request). OCR runs are saved as results/overlap_<page>_<overlap>.json,
LLM runs as results/overlap_llm_<page>_<overlap>_run<n>.json; a saved run is never sent again.
The 300 px runs of the first two pages are results/tiled_grayscale.json and layout_shift1400_app.json.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

import requests
from analyze import alnum, inter, norm, overlapping, read_as, score_labels, short_side
from layout_geometry import held_whole
from PIL import Image

from steel_text.azure_ocr import AzureReadClient
from steel_text.llm_filter import OpenAIResponsesClient, filter_labels
from steel_text.ocr_stage import run_tiled_ocr
from steel_text.ocr_types import OcrChunk
from steel_text.page_image import open_page
from steel_text.settings import load_settings
from steel_text.tiling import TilingChoice, make_tiles

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
PAGES = {"asis": 0, "shift1400": 1_400, "shift1020": 1_020}  # how far the drawing is moved right
OVERLAPS = (300, 350)
SAVED = {("asis", 300): "tiled_grayscale.json", ("shift1400", 300): "layout_shift1400_app.json"}
LLM_RUNS, BATCH_SIZE = 2, 25


def page_for(dx: int) -> Image.Image:
    page = open_page(HERE / "steel_framing_plan_S-201_400dpi.png")
    if not dx:
        return page
    wide = Image.new(page.mode, (page.width + dx, page.height), 255)
    wide.paste(page, (dx, 0))
    return wide


def ocr(name: str, overlap: int) -> dict:
    out = RESULTS / SAVED.get((name, overlap), f"overlap_{name}_{overlap}.json")
    if not out.exists():
        page = page_for(PAGES[name])
        t0 = time.perf_counter()
        tiling = TilingChoice(3500, overlap, f"3,500 px tiles, {overlap} px overlap")
        r = run_tiled_ocr(page, AzureReadClient.from_settings(load_settings()), tiling=tiling, request_sleep_s=0.2)
        record = {
            "name": out.stem,
            "seconds": round(time.perf_counter() - t0, 1),
            "tiles": [list(t.box) for t in r.tiles],
            "raw": [asdict(c) for c in r.raw],
            "lines": [asdict(c) for c in r.merged],
        }
        out.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{out.name}: {len(r.tiles)} tiles", flush=True)
    return json.loads(out.read_text(encoding="utf-8"))


def llm(name: str, overlap: int, lines: list[dict], n: int) -> dict[str, str]:
    out = RESULTS / f"overlap_llm_{name}_{overlap}_run{n}.json"
    if not out.exists():
        settings = load_settings()
        client = OpenAIResponsesClient(settings.openai_api_key, model=settings.openai_model, session=requests.Session())
        chunks = [OcrChunk(c["text"], tuple(c["bbox"])) for c in lines]
        res = filter_labels(chunks, client, batch_size=BATCH_SIZE, request_sleep_s=0.1)
        record = {
            "model": settings.openai_model,
            "batch_size": BATCH_SIZE,
            "failed_batches": res.failed_batches,
            "entries": [{"id": e.str_id, "label": e.normalized_label, "raw": e.raw_text} for e in res.entries],
        }
        out.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{out.name}: {len(res.entries)} labels", flush=True)
    return {e["id"]: e["label"] for e in json.loads(out.read_text(encoding="utf-8"))["entries"]}


def moved(items: list[dict], dx: int) -> list[dict]:
    return [{**it, "bbox": [it["bbox"][0] + dx, it["bbox"][1], it["bbox"][2] + dx, it["bbox"][3]]} for it in items]


def main() -> None:
    answer_key = json.loads((HERE / "ground_truth.json").read_text(encoding="utf-8"))
    tiled = json.loads((RESULTS / "tiled.json").read_text(encoding="utf-8"))
    small = [
        ln
        for ln in tiled["lines"]
        if short_side(ln["bbox"]) < 34 and len(alnum(ln["text"])) >= 3 and ln["text"] != "111"
    ]
    print("page       overlap tiles | OCR: beam labels exact | smallest | labels cut: read as -> LLM label | LLM found")
    for name, dx in PAGES.items():
        for overlap in OVERLAPS:
            record = ocr(name, overlap)
            lines = moved([{"id": str(i), **ln} for i, ln in enumerate(record["lines"])], -dx)  # sheet coordinates
            s = score_labels(answer_key, lines)["counts"]
            smallest = sum(1 for ln in small if (t := read_as(ln, lines)) is not None and norm(t) == norm(ln["text"]))
            labelled = [llm(name, overlap, record["lines"], n) for n in range(1, LLM_RUNS + 1)]
            at = [max(overlapping(g, lines), key=lambda c: inter(g["bbox"], c["bbox"]))["id"] for g in answer_key]
            found = [sum(got.get(at[i]) == g["section"].upper() for i, g in enumerate(answer_key)) for got in labelled]
            tiles = make_tiles(14_400 + dx, 9_600, tile_size=3500, overlap=overlap)
            cut = [i for i, g in enumerate(answer_key) if not held_whole(moved([g], dx)[0]["bbox"], tiles)]
            reads = [f"{lines[int(at[i])]['text']!r} -> {[got.get(at[i], '-') for got in labelled]}" for i in cut]
            print(
                f"{name:10} {overlap:5} {len(record['tiles']):6} |  {s['exact']:3}/82  |  {smallest}/{len(small)}  |  "
                f"{reads or 'none'}  |  {found}"
            )


if __name__ == "__main__":
    main()
