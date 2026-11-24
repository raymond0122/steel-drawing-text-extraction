"""Experiment 3: how much the dedup thresholds matter. Makes no network calls.

    python experiments/tiling/dedup_thresholds.py

Merges the saved raw OCR of every tile size (experiment 2) again, with one dedup
threshold changed at a time, and scores each result against the answer key.
Writes dedup_thresholds_summary.json next to this file.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from analyze import HERE, alnum, load, norm, read_as, score_labels, short_side
from analyze_tile_sizes import SIZES, load_size

from steel_text.dedup import DedupConfig, merge_tile_results
from steel_text.ocr_types import OcrChunk
from steel_text.tiling import Tile

SETTINGS = [("iou", v) for v in (0.3, 0.5, 0.7, 0.9)] + [("overlap_min", v) for v in (0.5, 0.6, 0.7, 0.8, 0.9, 0.95)]


def kept_lines(run: dict, cfg: DedupConfig) -> list[dict]:
    raw = [OcrChunk(text=c["text"], bbox=tuple(c["bbox"]), tile_index=c["tile_index"]) for c in run["raw"]]
    merged = merge_tile_results(raw, [Tile(*b) for b in run["tiles"]], cfg=cfg)
    return [{"text": c.text, "bbox": list(c.bbox)} for c in merged]


def main() -> None:
    answer_key = json.loads((HERE / "ground_truth.json").read_text(encoding="utf-8"))
    reference = load("tiled")["lines"]
    small = [
        ln for ln in reference if short_side(ln["bbox"]) < 34 and len(alnum(ln["text"])) >= 3 and ln["text"] != "111"
    ]

    def scores(lines: list[dict]) -> tuple[int, int]:
        exact = score_labels(answer_key, lines)["counts"].get("exact", 0)
        smallest = sum(1 for ln in small if (t := read_as(ln, lines)) is not None and norm(t) == norm(ln["text"]))
        return exact, smallest

    runs = {size: load_size(size) for size in SIZES}
    default = DedupConfig()
    base = {size: kept_lines(run, default) for size, run in runs.items()}
    base_scores = {size: scores(lines) for size, lines in base.items()}

    print(f"Defaults: {asdict(default)}. Lines kept, as a change from the defaults, per tile size.")
    print("Scores: beam labels read exactly and smallest lines read exactly, against the defaults.\n")
    print(f"{'setting':<18}" + "".join(f"{s:>7}" for s in SIZES) + "   scores")
    rows = []
    for field, value in SETTINGS:
        cfg = DedupConfig(**{**asdict(default), field: value})
        change, score_changes = {}, []
        for size, run in runs.items():
            lines = kept_lines(run, cfg)
            change[size] = len(lines) - len(base[size])
            if scores(lines) != base_scores[size]:
                score_changes.append(f"{size}: {base_scores[size]} -> {scores(lines)}")
        print(
            f"{field + ' ' + str(value):<18}"
            + "".join(f"{change[s]:>+7}" for s in SIZES)
            + f"   {'; '.join(score_changes) or 'unchanged on every size'}"
        )
        rows.append({"field": field, "value": value, "kept_change": change, "score_changes": score_changes})
    (HERE / "dedup_thresholds_summary.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
