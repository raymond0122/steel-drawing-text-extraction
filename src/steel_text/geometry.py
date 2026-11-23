"""Bounding-box helpers. All boxes are (x0, y0, x1, y1) in pixels."""

from __future__ import annotations

import math
from typing import Any

BBox = tuple[int, int, int, int]


def poly_to_bbox(poly: Any) -> BBox | None:
    """Convert an Azure polygon to the smallest enclosing box.

    Azure returns either a flat list [x1, y1, x2, y2, ...] or a list of
    {"x": .., "y": ..} points, depending on the API version.
    """
    xs: list[float] = []
    ys: list[float] = []
    if isinstance(poly, list) and poly:
        if isinstance(poly[0], (int, float)):
            xs = [float(v) for v in poly[0::2]]
            ys = [float(v) for v in poly[1::2]]
        elif isinstance(poly[0], dict):
            xs = [float(p["x"]) for p in poly]
            ys = [float(p["y"]) for p in poly]
    if not xs or not ys:
        return None
    return (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))


def area(b: BBox) -> int:
    x0, y0, x1, y1 = b
    return max(0, x1 - x0) * max(0, y1 - y0)


def center(b: BBox) -> tuple[float, float]:
    x0, y0, x1, y1 = b
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def center_distance(a: BBox, b: BBox) -> float:
    (ax, ay), (bx, by) = center(a), center(b)
    return math.hypot(ax - bx, ay - by)


def intersection_area(a: BBox, b: BBox) -> int:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0, w) * max(0, h)


def intersection_box(a: BBox, b: BBox) -> BBox | None:
    """The area two boxes share, or None when they don't overlap."""
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


def region_around(a: BBox, b: BBox, pad: int, width: int, height: int) -> BBox:
    """The box around both a and b, grown by `pad` on every side and kept inside a width x height page."""
    return (
        max(0, min(a[0], b[0]) - pad),
        max(0, min(a[1], b[1]) - pad),
        min(width, max(a[2], b[2]) + pad),
        min(height, max(a[3], b[3]) + pad),
    )


def iou(a: BBox, b: BBox) -> float:
    inter = intersection_area(a, b)
    if inter == 0:
        return 0.0
    union = area(a) + area(b) - inter
    return inter / union if union > 0 else 0.0


def overlap_ratio_min(a: BBox, b: BBox) -> float:
    """Intersection divided by the smaller box's area.

    A small fragment fully inside a long text box scores 1.0 here, even though
    its IoU with that box is low. That is why this metric exists.
    """
    inter = intersection_area(a, b)
    if inter <= 0:
        return 0.0
    return inter / float(max(1, min(area(a), area(b))))


def contains(outer: BBox, inner: BBox) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]
