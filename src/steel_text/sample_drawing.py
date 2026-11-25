"""Generate a synthetic steel framing drawing for demos, and check labels against its ground truth.

Everything here is made up. Some texts are placed on purpose where the
default 9000x6000 tile grid will cut or duplicate them:

  tiles (3500 px, 350 px overlap):  x: [0,3500] [3150,6650] [5500,9000]
                                    y: [0,3500] [2500,6000]

  "SEE NOTE 6"      crosses x=3500 -> left tile sees only "SEE"          (fragment)
  "WT155*79"        crosses x=6650 -> middle tile sees only "WT1"        (fragment)
  "HSS152X152X9.5"  inside the x overlap band -> read by two tiles       (duplicate)
  "W310X39", "2400" inside the y overlap band -> read by two tiles       (duplicate)
  "TYP." twice, far apart                     -> must NOT be merged
  "C50" printed, OCR reads "CS0"; "W310X39" printed, OCR reads "W31OX39" (LLM fixes)
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageDraw

from .fonts import ImageFont, load_font
from .geometry import BBox, intersection_area
from .llm_filter import LabelEntry

WIDTH, HEIGHT = 9000, 6000


@dataclass
class GroundTruthText:
    text: str  # what is printed
    ocr_text: str  # what the simulated OCR reads (may contain OCR mistakes)
    bbox: BBox
    expected_label: str | None  # normalized steel label, or None if not a label


# (printed text, OCR reading or None for same, x, y, font size, expected label)
TEXTS: list[tuple[str, str | None, int, int, int, str | None]] = [
    ("SEE NOTE 6", None, 3300, 900, 80, None),
    ("HSS152X152X9.5", None, 5650, 1500, 80, "HSS152X152X9.5"),
    ("WT155*79", None, 6500, 2000, 80, "WT155*79"),
    ("L102X76X7.9", None, 3800, 2000, 80, "L102X76X7.9"),
    ("L64X64X6.4", None, 7200, 1800, 80, "L64X64X6.4"),
    ("W200X27", None, 1200, 1700, 80, "W200X27"),
    ("W310X39", "W31OX39", 1500, 2900, 80, "W310X39"),
    ("W250X33", None, 4000, 4300, 80, "W250X33"),
    ("C50", "CS0", 2000, 4600, 80, "C50"),
    ("1200", None, 2200, 1500, 70, None),
    ("2400", None, 5000, 3000, 70, None),
    ("TYP.", None, 2600, 2200, 70, None),
    ("TYP.", None, 7600, 4300, 70, None),
    ("EL. +3500", None, 3800, 5500, 70, None),
    ("GENERAL NOTES", None, 300, 4950, 70, None),
    ("4. ALL BOLTS A325 U.N.O.", None, 300, 5100, 60, None),
    ("6. SEE ARCH DWGS FOR DIMENSIONS", None, 300, 5230, 60, None),
    ("SAMPLE FRAMING PLAN", None, 6850, 4980, 70, None),
    ("SYNTHETIC DEMO DATA - NOT A REAL PROJECT", None, 6850, 5110, 50, None),
    ("DWG NO. DEMO-001", None, 6850, 5240, 60, None),
    ("SCALE 1:50", None, 6850, 5370, 60, None),
]

BEAM_YS = (1150, 2650, 4150)
COLUMN_XS = (1000, 4500, 8000)


def _text_box(
    draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font: ImageFont, anchor: str | None = None
) -> BBox:
    """Pillow measures text in floats; boxes are whole pixels everywhere else."""
    x0, y0, x1, y1 = draw.textbbox(xy, text, font=font, anchor=anchor)
    return (int(x0), int(y0), int(x1), int(y1))


def _arrow(draw: ImageDraw.ImageDraw, x: int, y0: int, y1: int) -> None:
    draw.line([(x, y0), (x, y1)], fill="black", width=5)
    d = 1 if y1 > y0 else -1
    draw.polygon([(x, y1), (x - 18, y1 - d * 45), (x + 18, y1 - d * 45)], fill="black")


def make_sample_drawing() -> tuple[Image.Image, list[GroundTruthText]]:
    img = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(img)

    draw.rectangle([100, 100, WIDTH - 100, HEIGHT - 100], outline="black", width=8)
    draw.rectangle([6750, 4900, 8850, 5850], outline="black", width=6)  # title block

    for x, name in zip(COLUMN_XS, "ABC", strict=True):  # grid lines with one-letter bubbles
        draw.line([(x, 420), (x, 4700)], fill=(150, 150, 150), width=4)
        draw.ellipse([x - 70, 200, x + 70, 340], outline="black", width=5)
        draw.text((x, 270), name, fill="black", font=load_font(80), anchor="mm")
    for y in BEAM_YS:  # beams
        draw.rectangle([1000, y - 14, 8000, y + 14], fill=(60, 60, 60))
    for x in COLUMN_XS:  # columns
        for y in BEAM_YS:
            draw.rectangle([x - 40, y - 40, x + 40, y + 40], fill="black")
    for y, (xa, xb) in ((1640, (1000, 4500)), (3140, (4500, 8000))):  # dimension lines
        draw.line([(xa, y), (xb, y)], fill="black", width=3)
        for x in (xa, xb):
            draw.line([(x - 25, y + 25), (x + 25, y - 25)], fill="black", width=4)

    truth: list[GroundTruthText] = []
    for text, ocr, x, y, size, label in TEXTS:
        font = load_font(size)
        draw.text((x, y), text, fill="black", font=font)
        x0, y0, x1, y1 = _text_box(draw, (x, y), text, font)
        truth.append(GroundTruthText(text=text, ocr_text=ocr or text, bbox=(x0, y0, x1, y1), expected_label=label))
        if label:  # leader arrow from the label to the nearest beam
            cx = (x0 + x1) // 2
            beam = min(BEAM_YS, key=lambda b: abs(b - (y0 + y1) / 2))
            if beam < y0:
                _arrow(draw, cx, y0 - 15, beam + 30)
            else:
                _arrow(draw, cx, y1 + 15, beam - 30)

    for x, name in zip(COLUMN_XS, "ABC", strict=True):  # one-letter grid marks are text too (dropped by min_chars)
        b = _text_box(draw, (x, 270), name, load_font(80), anchor="mm")
        truth.append(GroundTruthText(text=name, ocr_text=name, bbox=b, expected_label=None))
    return img, truth


def save_sample(out_dir: Path) -> tuple[Path, Path]:
    """Write the synthetic drawing and its ground truth (as JSON) to out_dir.

    Returns (image path, ground-truth path).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    img, truth = make_sample_drawing()
    img_path, truth_path = out_dir / "00_sample_drawing.png", out_dir / "ground_truth.json"
    img.save(img_path)
    truth_path.write_text(json.dumps([asdict(t) for t in truth], indent=2), encoding="utf-8")
    return img_path, truth_path


def load_truth(path: Path) -> list[GroundTruthText]:
    """Read a ground-truth file written by save_sample()."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return [GroundTruthText(d["text"], d["ocr_text"], tuple(d["bbox"]), d["expected_label"]) for d in data]


@dataclass
class TruthCheck:
    expected: list[str]
    missing: list[str]
    unexpected: list[str]

    @property
    def found(self) -> int:
        """How many of the expected labels were found."""
        return len(self.expected) - len(self.missing)


def check_truth(truth: Iterable[GroundTruthText], entries: Iterable[LabelEntry]) -> TruthCheck:
    """Compare the labels found with the expected labels, by text and by place.

    An expected label is found only by a label with the same text whose box overlaps
    its own, and each label found counts once. So a label printed twice must be found
    twice, and one read at the wrong place is missing where it is printed. The labels
    left over are unexpected, which is how a missed duplicate shows up.
    """
    expected = [(label, t.bbox) for t in truth if (label := t.expected_label)]
    left = list(entries)
    missing: list[str] = []
    for label, box in expected:
        match = next(
            (i for i, e in enumerate(left) if e.normalized_label == label and intersection_area(e.bbox, box) > 0),
            None,
        )
        if match is None:
            missing.append(label)
        else:
            del left[match]
    return TruthCheck(
        expected=sorted(label for label, _ in expected),
        missing=sorted(missing),
        unexpected=sorted(e.normalized_label for e in left),
    )
