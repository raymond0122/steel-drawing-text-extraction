"""Write the steel labels found on a page to an Excel sheet with one "Profile" column.

This is a list of labels, not a bill of materials. Lengths and quantities
would need member geometry and label-to-member linking, which are not part
of this repository. A label count is not a member count either: one label
can point at several members.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO

from openpyxl import Workbook
from openpyxl.styles import Font

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def write_label_sheet(labels: Iterable[str], target: str | Path | BinaryIO) -> None:
    """One row per label found, so a label printed twice appears twice. Sorted so repeats sit together."""
    wb = Workbook()
    ws = wb.worksheets[0]  # a new workbook has exactly one sheet
    ws.title = "Labels"
    ws.append(["Profile"])
    ws["A1"].font = Font(bold=True)
    for row, label in enumerate(sorted(labels), start=2):
        cell = ws.cell(row=row, column=1, value=label)
        cell.data_type = "s"  # always text: a label starting with "=" must not become a formula
    ws.column_dimensions["A"].width = 22
    ws.freeze_panes = "A2"  # keep the header visible while scrolling
    wb.save(target)
