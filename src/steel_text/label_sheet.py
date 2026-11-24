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
from openpyxl.styles import Font

from .geometry import BBox

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def write_label_sheet(
    labels: Iterable[str],
    target: str | Path | BinaryIO,
    *,
    not_read: Sequence[tuple[int, BBox, str]] = (),
) -> None:
    """One row per label found, so a label printed twice appears twice. Sorted so repeats sit together.

    `not_read` lists the tiles OCR could not read (tile index, box, error). Then the
    label sheet is titled "Labels (incomplete)" and a "Not read" sheet gives each
    missing area, so the file can't pass for a complete list.
    """
    wb = Workbook()
    ws = wb.worksheets[0]  # a new workbook has exactly one sheet
    ws.title = "Labels (incomplete)" if not_read else "Labels"
    ws.append(["Profile"])
    ws["A1"].font = Font(bold=True)
    for row, label in enumerate(sorted(labels), start=2):
        cell = ws.cell(row=row, column=1, value=label)
        cell.data_type = "s"  # always text: a label starting with "=" must not become a formula
    ws.column_dimensions["A"].width = 22
    ws.freeze_panes = "A2"  # keep the header visible while scrolling

    if not_read:
        gaps = wb.create_sheet("Not read")
        gaps.append(["Tile", "x0", "y0", "x1", "y1", "Error"])
        for cell in gaps[1]:
            cell.font = Font(bold=True)
        for row, (tile, box, error) in enumerate(not_read, start=2):
            for col, value in enumerate((tile, *box), start=1):
                gaps.cell(row=row, column=col, value=value)
            gaps.cell(row=row, column=6, value=error).data_type = "s"
        gaps.column_dimensions["F"].width = 80
    wb.save(target)
