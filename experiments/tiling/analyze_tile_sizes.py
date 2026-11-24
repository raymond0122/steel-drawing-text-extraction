"""Experiment 2: score the tile-size sweep (run_tile_sizes.py). Makes no network calls.

    python experiments/tiling/analyze_tile_sizes.py

Every size is merged again from its raw lines with the package's current dedup, so all
sizes, 3,500 px (results/tiled.json) included, are scored the same way. Uses the
verified answer key from analyze.py: the 82 beam labels and the 23 smallest lines.
Writes tile_sizes_summary.json next to this file.
"""

from __future__ import annotations

import json

from analyze import HERE, alnum, frac, load, norm, read_as, score_labels, short_side

from steel_text.dedup import DedupDecision, merge_tile_results
from steel_text.ocr_types import OcrChunk
from steel_text.tiling import Tile

SIZES = [2000, 2500, 3000, 3500, 4500, 6000, 8000, 10000]


def load_size(size: int) -> dict:
    return load("tiled") if size == 3500 else load(f"tile_{size}")  # 3,500 px is the original tiled run


def merge(run: dict) -> tuple[list[dict], int, int]:
    """Merged lines with the current dedup, how many were removed, and how many of those were same-tile."""
    raw = [OcrChunk(text=c["text"], bbox=tuple(c["bbox"]), tile_index=c["tile_index"]) for c in run["raw"]]
    log: list[DedupDecision] = []
    merged = merge_tile_results(raw, [Tile(*b) for b in run["tiles"]], log=log)
    same_tile = sum(1 for d in log if d.dropped.tile_index == d.kept.tile_index)
    return [{"text": c.text, "bbox": list(c.bbox)} for c in merged], len(log), same_tile


def inside(box: list[int], tile: list[int]) -> bool:
    return tile[0] <= box[0] and tile[1] <= box[1] and box[2] <= tile[2] and box[3] <= tile[3]


def main() -> None:
    gt = json.loads((HERE / "ground_truth.json").read_text(encoding="utf-8"))
    reference = load("tiled")["lines"]
    small = [
        ln for ln in reference if short_side(ln["bbox"]) < 34 and len(alnum(ln["text"])) >= 3 and ln["text"] != "111"
    ]
    all_text = [ln for ln in reference if len(alnum(ln["text"])) >= 3]

    print(f"{len(gt)} beam labels, {len(small)} smallest lines, {len(all_text)} lines for agreement\n")
    head = (
        f"{'tile px':>8}{'requests':>9}{'seconds':>9}{'429s':>6}{'raw':>6}{'kept':>6}{'same-tile':>10}"
        f"{'not whole':>10}{'section':>9}{'camber':>8}{'exact':>7}{'smallest':>10}{'same as 3500':>14}"
    )
    print(head)
    rows = []
    merged_3500 = None
    for size in SIZES:
        try:
            run = load_size(size)
        except FileNotFoundError:
            print(f"{size:>8}  (not run)")
            continue
        if "error" in run:
            print(f"{size:>8}  FAILED: {run['error'][:100]}")
            rows.append({"tile_size": size, "error": run["error"]})
            continue
        lines, removed, same_tile = merge(run)
        if size == 3500:
            merged_3500 = lines
        # Labels that no single tile contains whole: they can only come back cut.
        not_whole = sum(1 for lab in gt if not any(inside(lab["bbox"], t) for t in run["tiles"]))
        c = score_labels(gt, lines)["counts"]
        small_ok = sum(1 for ln in small if (t := read_as(ln, lines)) is not None and norm(t) == norm(ln["text"]))
        rows.append(
            {
                "tile_size": size,
                "requests": run["requests"],
                "seconds": run["seconds"],
                "rate_limited": run.get("rate_limited"),
                "raw": len(run["raw"]),
                "kept": len(lines),
                "removed": removed,
                "same_tile_removals": same_tile,
                "labels_not_whole_in_any_tile": not_whole,
                "labels": c,
                "smallest_exact": small_ok,
                "_lines": lines,
            }
        )

    for r in rows:
        if "error" in r:
            continue
        agree = "-"
        if merged_3500 is not None:
            same = sum(
                1
                for ln in all_text
                if (a := read_as(ln, r["_lines"])) is not None
                and (b := read_as(ln, merged_3500)) is not None
                and norm(a) == norm(b)
            )
            r["same_as_3500"] = same
            agree = f"{100 * same / len(all_text):.0f}%"
        c = r["labels"]
        retries = "?" if r["rate_limited"] is None else r["rate_limited"]
        print(
            f"{r['tile_size']:>8}{r['requests']:>9}{r['seconds']:>9}{retries:>6}"
            f"{r['raw']:>6}{r['kept']:>6}{r['same_tile_removals']:>10}{r['labels_not_whole_in_any_tile']:>10}"
            f"{frac(c.get('section', 0), c['total']):>9}{frac(c.get('camber', 0), c.get('camber_total', 0)):>8}"
            f"{frac(c.get('exact', 0), c['total']):>7}{frac(r['smallest_exact'], len(small)):>10}{agree:>14}"
        )
    print(
        "\n429s: rate-limit retries during the run (their waits are in 'seconds'); '?' = not recorded."
        "\nnot whole: beam labels that no tile contains whole, so every reading of them is cut by a tile edge."
        "\nsame as 3500: lines read the same as at 3,500 px (agreement, not verified accuracy)."
    )
    for r in rows:
        r.pop("_lines", None)
    (HERE / "tile_sizes_summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
