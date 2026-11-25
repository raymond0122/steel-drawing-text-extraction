"""Write the steel labels found on a page to an Excel sheet with one "Profile" column.

This is a list of labels, not a bill of materials. Lengths and quantities
would need member geometry and label-to-member linking, which are not part
of this repository. A label count is not a member count either: one label
can point at several members.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import BinaryIO

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font

from .geometry import BBox

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def write_label_sheet(
    labels: Iterable[str],
    target: str | Path | BinaryIO,
    *,
    not_read: Sequence[tuple[int, BBox, str]] = (),
    not_checked: Sequence[tuple[str, str, BBox, str]] = (),
) -> None:
    """One row per label found, so a label printed twice appears twice. Sorted so repeats sit together.

    `not_read` lists the tiles OCR could not read (tile index, box, error), and
    `not_checked` the OCR lines the LLM never checked because their batch failed
    (line ID, text, box, error). Either makes the label sheet "Labels (incomplete)"
    and adds a sheet listing the gap, so the file can't pass for a complete list.
    """
    wb = Workbook()
    ws = wb.worksheets[0]  # a new workbook has exactly one sheet
    ws.title = "Labels (incomplete)" if not_read or not_checked else "Labels"
    ws.append(["Profile"])
    ws["A1"].font = Font(bold=True)
    for row, label in enumerate(sorted(labels), start=2):
        cell = ws.cell(row=row, column=1, value=_text(label))
        cell.data_type = "s"  # always text: a label starting with "=" must not become a formula
    ws.column_dimensions["A"].width = 22
    ws.freeze_panes = "A2"  # keep the header visible while scrolling

    if not_read:
        tiles = [(tile, *box, error) for tile, box, error in not_read]
        _add_gap_sheet(wb, "Not read", ("Tile", "x0", "y0", "x1", "y1", "Error"), tiles, {"F": 80})
    if not_checked:
        lines = [(line, text, *box, error) for line, text, box, error in not_checked]
        _add_gap_sheet(wb, "Not checked", ("Line", "Text", "x0", "y0", "x1", "y1", "Error"), lines, {"B": 40, "G": 80})
    wb.save(target)


def _text(value: str) -> str:
    """The text without the control characters an Excel file can't hold; openpyxl refuses them.

    Tab and newline are allowed, so they stay.
    """
    return ILLEGAL_CHARACTERS_RE.sub("", value)


def _add_gap_sheet(
    wb: Workbook,
    title: str,
    header: Sequence[str],
    rows: Iterable[Sequence[str | int]],
    widths: dict[str, int],
) -> None:
    ws = wb.create_sheet(title)
    ws.append(list(header))
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for r, values in enumerate(rows, start=2):
        for c, value in enumerate(values, start=1):
            cell = ws.cell(row=r, column=c, value=_text(value) if isinstance(value, str) else value)
            if isinstance(value, str):
                cell.data_type = "s"  # OCR text or an error starting with "=" must not become a formula
    for column, width in widths.items():
        ws.column_dimensions[column].width = width
