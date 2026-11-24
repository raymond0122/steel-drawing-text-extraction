"""Experiment 1: score the saved OCR results. Makes no network calls.

    python experiments/tiling/analyze.py

Writes ground_truth.json and summary.json next to this file and prints the tables.
"""

from __future__ import annotations

import collections
import json
import re
import statistics
from pathlib import Path

from steel_text.dedup import DedupConfig, DedupDecision, merge_tile_results
from steel_text.ocr_types import OcrChunk
from steel_text.tiling import Tile

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
CONDITIONS = ["tiled", "whole_10000", "whole_8000", "whole_6000", "whole_4000", "whole_3000"]
LABEL_GLYPH_PX = 31  # height of the beam-label letters at full resolution, measured on the image

# The framing plan itself (between grids 1-7 and A-D), in full-resolution pixels.
# Keeps the legend, take-off table and details out of the beam-label count.
PLAN = (1050, 1000, 9750, 4750)
SECTION = re.compile(r"W\s?(\d{1,2})\s?[xX×]\s?(\d{1,3})")
# Member counts from the sheet's own "MEMBER TAKE-OFF" table (beams only).
TAKE_OFF = {
    "W24x55": 8,
    "W21x50": 2,
    "W21x44": 12,
    "W18x40": 2,
    "W18x35": 4,
    "W16x31": 4,
    "W16x26": 28,
    "W12x19": 2,
    "W10x12": 20,
}
INCH_MARKS = "\"'*”"

Line = dict


def load(name: str) -> dict:
    return json.loads((RESULTS / f"{name}.json").read_text(encoding="utf-8"))


def norm(s: str) -> str:
    """Only what is not a reading error: the multiplication sign for x, and spacing."""
    return re.sub(r"\s+", "", s.replace("×", "x"))


def alnum(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", s)


def short_side(b: list[int]) -> int:
    return min(b[2] - b[0], b[3] - b[1])


def inter(a: list[int], b: list[int]) -> int:
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def area(b: list[int]) -> int:
    return max(1, (b[2] - b[0]) * (b[3] - b[1]))


def overlapping(ref: Line, lines: list[Line]) -> list[Line]:
    """Lines that cover at least 30% of the reference box, or are 30% covered by it."""
    out = []
    for ln in lines:
        i = inter(ref["bbox"], ln["bbox"])
        if i and i / min(area(ref["bbox"]), area(ln["bbox"])) >= 0.3:
            out.append(ln)
    return out


def read_as(ref: Line, lines: list[Line]) -> str | None:
    """What a condition read at the reference line's position (None if nothing is there).

    If the reference text appears inside a longer line (the OCR joined it with a
    neighbour) that counts as reading it, so line grouping is not scored as an error.
    """
    cands = overlapping(ref, lines)
    if not cands:
        return None
    want = norm(ref["text"])
    for c in cands:
        if want in norm(c["text"]):
            return ref["text"]
    vertical = (ref["bbox"][3] - ref["bbox"][1]) > (ref["bbox"][2] - ref["bbox"][0])
    cands.sort(key=lambda c: -c["bbox"][3] if vertical else c["bbox"][0])
    joined = "".join(c["text"] for c in cands)
    if want in norm(joined):
        return ref["text"]
    best = max(cands, key=lambda c: inter(ref["bbox"], c["bbox"]))
    return best["text"]


# --- beam labels -------------------------------------------------------------------


def parse_label(text: str) -> dict:
    t = text.replace("×", "x")
    m = SECTION.search(t)
    section = f"W{m.group(1)}x{m.group(2)}" if m else None
    s = re.search(r"[\(\[]\s*(\d+)\s*[\)\]]", t)
    studs = s.group(1) if s else None
    camber = None
    if "=" in t:
        camber = t.split("=", 1)[1].strip().rstrip(INCH_MARKS).strip()
    return {"section": section, "studs": studs, "camber": camber}


def build_ground_truth(tiled: list[Line]) -> list[dict]:
    """The 82 beam labels on the plan.

    Taken from the full-resolution reading, then checked by eye against a crop of
    every label (check/labels_*.png) and against the take-off table's counts. The one
    correction from that check: a lowercase "c=" that was read as "C=".
    """
    gt = []
    for ln in tiled:
        cx, cy = (ln["bbox"][0] + ln["bbox"][2]) / 2, (ln["bbox"][1] + ln["bbox"][3]) / 2
        if not (PLAN[0] <= cx <= PLAN[2] and PLAN[1] <= cy <= PLAN[3]) or not SECTION.search(ln["text"]):
            continue
        text = norm(ln["text"]).replace("C=", "c=")
        text = re.sub(r"\)c=", ") c=", text.replace("(", " (", 1))
        gt.append({"text": text, "bbox": ln["bbox"], **parse_label(text)})
    counts = collections.Counter(x["section"] for x in gt)
    assert dict(counts) == TAKE_OFF, f"label counts do not match the take-off table: {dict(counts)}"
    return gt


def score_labels(gt: list[dict], lines: list[Line]) -> dict:
    r: collections.Counter = collections.Counter()
    wrong: list[tuple[str, str]] = []
    for lab in gt:
        cands = overlapping(lab, lines)
        if not cands:
            r["missed"] += 1
            wrong.append((lab["text"], "(nothing read)"))
            continue
        best = max(cands, key=lambda c: inter(lab["bbox"], c["bbox"]))
        p = parse_label(best["text"])
        r["found"] += 1
        r["section"] += p["section"] == lab["section"]
        r["studs"] += p["studs"] == lab["studs"]
        if lab["camber"] is not None:
            r["camber_total"] += 1
            r["camber"] += p["camber"] == lab["camber"]
        exact = norm(best["text"]) == norm(lab["text"])
        r["exact"] += exact
        if not exact:
            wrong.append((lab["text"], best["text"]))
    r["total"] = len(gt)
    return {"counts": dict(r), "wrong": wrong}


# --- dedup audit ---------------------------------------------------------------------


def dedup_audit(tiled: dict) -> dict:
    raw = [OcrChunk(text=c["text"], bbox=tuple(c["bbox"]), tile_index=c["tile_index"]) for c in tiled["raw"]]
    log: list[DedupDecision] = []
    merged = merge_tile_results(raw, [Tile(*b) for b in tiled["tiles"]], cfg=DedupConfig(), log=log)
    # A tile never reads the same text twice, so two lines from one tile are two
    # different pieces of text. Removing one of them is a wrong removal.
    same_tile = [d for d in log if d.dropped.tile_index == d.kept.tile_index]
    places = {(d.dropped.text, d.dropped.bbox[0] // 60, d.dropped.bbox[1] // 60) for d in same_tile}
    return {
        "raw": len(raw),
        "merged": len(merged),
        "removed": len(log),
        "by_rule": dict(collections.Counter(d.reason.split(" (")[0] for d in log)),
        "wrong_removals": [{"text": d.dropped.text, "bbox": list(d.dropped.bbox), "rule": d.reason} for d in same_tile],
        "wrong_places": len(places),
    }


# --- main ------------------------------------------------------------------------------


def frac(a: int, b: int) -> str:
    return f"{a}/{b}" if b else "-"


def main() -> None:
    data = {n: load(n) for n in CONDITIONS}
    rejected = load("whole_14400")
    lines = {n: data[n]["lines"] for n in CONDITIONS}
    gt = build_ground_truth(lines["tiled"])
    (HERE / "ground_truth.json").write_text(json.dumps(gt, ensure_ascii=False, indent=1), encoding="utf-8")
    box_to_glyph = LABEL_GLYPH_PX / statistics.mean(short_side(x["bbox"]) for x in gt)

    print(f"whole page at full size (14400 px): {rejected.get('error', '')[:230]}\n")

    print("TABLE 1  what was sent")
    print(f"{'condition':<13}{'image':>13}{'label px':>10}{'requests':>10}{'seconds':>9}{'lines':>7}")
    summary: dict = {"conditions": {}, "box_to_glyph": round(box_to_glyph, 3)}
    for n in CONDITIONS:
        d = data[n]
        scale = d["scale"]
        size = "14400x9600" if n == "tiled" else "x".join(map(str, d["image_size"]))
        print(
            f"{n:<13}{size:>13}{LABEL_GLYPH_PX * scale:>10.1f}{d['requests']:>10}{d['seconds']:>9}{len(d['lines']):>7}"
        )
        summary["conditions"][n] = {
            "image": size,
            "label_px": round(LABEL_GLYPH_PX * scale, 1),
            "requests": d["requests"],
            "seconds": d["seconds"],
            "lines": len(d["lines"]),
        }

    with_camber = sum(1 for x in gt if x["camber"])
    print(f"\nTABLE 2  beam labels on the plan ({len(gt)} labels, {with_camber} with a camber)")
    print(f"{'condition':<13}{'found':>8}{'section':>9}{'studs':>8}{'camber':>8}{'whole line exact':>18}")
    for n in CONDITIONS:
        s = score_labels(gt, lines[n])
        c = s["counts"]
        print(
            f"{n:<13}{frac(c.get('found', 0), c['total']):>8}{frac(c.get('section', 0), c['total']):>9}"
            f"{frac(c.get('studs', 0), c['total']):>8}{frac(c.get('camber', 0), c.get('camber_total', 0)):>8}"
            f"{frac(c.get('exact', 0), c['total']):>18}"
        )
        summary["conditions"][n]["labels"] = c
        summary["conditions"][n]["label_errors"] = collections.Counter(
            f"{a} -> {b}" for a, b in s["wrong"]
        ).most_common(12)

    # Smallest text on the sheet: lines whose box is under 34 px tall at full resolution.
    small = [
        ln
        for ln in lines["tiled"]
        if short_side(ln["bbox"]) < 34 and len(alnum(ln["text"])) >= 3 and ln["text"] != "111"
    ]
    print(
        f"\nTABLE 3  smallest text on the sheet ({len(small)} lines: detail references 'S-201', 'BF-1', "
        "title-block field names)"
    )
    print(f"{'condition':<13}{'letter px':>10}{'read exactly':>14}{'nothing read':>14}")
    small_glyph = statistics.mean(short_side(ln["bbox"]) for ln in small) * box_to_glyph
    for n in CONDITIONS:
        got = [read_as(ln, lines[n]) for ln in small]
        ok = sum(1 for ln, t in zip(small, got, strict=True) if t is not None and norm(t) == norm(ln["text"]))
        none = sum(1 for t in got if t is None)
        print(f"{n:<13}{small_glyph * data[n]['scale']:>10.1f}{frac(ok, len(small)):>14}{none:>14}")
        summary["conditions"][n]["small_text"] = {
            "total": len(small),
            "exact": ok,
            "nothing": none,
            "letter_px": round(small_glyph * data[n]["scale"], 1),
        }

    # All text, by how tall the letters are once the page has been shrunk.
    ref = [ln for ln in lines["tiled"] if len(alnum(ln["text"])) >= 3]
    edges = [0, 6, 8, 10, 12, 15, 20, 1000]
    bucket: dict = collections.defaultdict(lambda: [0, 0, 0])  # total, same, nothing
    for n in CONDITIONS[1:]:
        for ln in ref:
            h = short_side(ln["bbox"]) * box_to_glyph * data[n]["scale"]
            b = next(i for i in range(len(edges) - 1) if edges[i] <= h < edges[i + 1])
            t = read_as(ln, lines[n])
            bucket[b][0] += 1
            bucket[b][1] += t is not None and norm(t) == norm(ln["text"])
            bucket[b][2] += t is None
    print(f"\nTABLE 4  all text ({len(ref)} lines), whole-page conditions pooled, by letter height after shrinking")
    print("         'same' = read the same as the full-resolution tiled run (agreement, not verified accuracy)")
    print(f"{'letter height':<16}{'lines':>7}{'same':>7}{'same %':>8}{'nothing read':>14}")
    summary["by_height"] = []
    for b in sorted(bucket):
        tot, ok, none = bucket[b]
        lab = f"{edges[b]}-{edges[b + 1]} px" if edges[b + 1] < 1000 else f"{edges[b]}+ px"
        print(f"{lab:<16}{tot:>7}{ok:>7}{100 * ok / tot:>7.0f}%{none:>14}")
        summary["by_height"].append({"range": lab, "lines": tot, "same": ok, "nothing": none})

    print("\nTABLE 4b all text, per condition")
    print(f"{'condition':<13}{'lines':>7}{'same':>7}{'same %':>8}{'nothing read':>14}")
    for n in CONDITIONS[1:]:
        got = [read_as(ln, lines[n]) for ln in ref]
        ok = sum(1 for ln, t in zip(ref, got, strict=True) if t is not None and norm(t) == norm(ln["text"]))
        none = sum(1 for t in got if t is None)
        print(f"{n:<13}{len(ref):>7}{ok:>7}{100 * ok / len(ref):>7.0f}%{none:>14}")
        summary["conditions"][n]["all_text"] = {"total": len(ref), "same": ok, "nothing": none}

    a = dedup_audit(data["tiled"])
    print(
        f"\nTABLE 5  dedup in the tiled run: {a['raw']} lines read -> {a['merged']} kept, "
        f"{a['removed']} removed  {a['by_rule']}"
    )
    print(
        "         removed although both lines came from the same tile (so they were different text): "
        f"{len(a['wrong_removals'])} removals at {a['wrong_places']} places"
    )
    for w in a["wrong_removals"]:
        print(f"           {w['text']!r:<10} at {w['bbox']}  [{w['rule']}]")
    summary["dedup"] = a
    single = sum(1 for ln in lines["whole_10000"] if len(ln["text"].strip()) < 2)
    print(
        "\nnote: the tiled path drops 1-character lines by design (min_chars=2); "
        f"whole_10000 returned {single} such lines (grid bubbles, symbols)"
    )

    print("\nmost common label misreadings")
    for n in CONDITIONS:
        errs = summary["conditions"][n]["label_errors"]
        print(f"  {n}: " + ("; ".join(f"{k} (x{v})" for k, v in errs[:5]) if errs else "none"))
    (HERE / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
