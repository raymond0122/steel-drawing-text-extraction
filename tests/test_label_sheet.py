import io

from openpyxl import load_workbook

from steel_text.label_sheet import write_label_sheet


def read_column(buf):
    ws = load_workbook(buf).worksheets[0]
    return [row[0] for row in ws.iter_rows(values_only=True)], ws.max_column


def test_one_profile_column_with_one_row_per_label():
    buf = io.BytesIO()
    write_label_sheet(["W310X39", "C50", "WT155*79", "W310X39"], buf)
    column, n_columns = read_column(buf)
    assert n_columns == 1
    assert column == ["Profile", "C50", "W310X39", "W310X39", "WT155*79"]  # repeats kept, sorted together


def test_label_starting_with_equals_is_text_not_a_formula():
    buf = io.BytesIO()
    write_label_sheet(["=1+1"], buf)
    cell = load_workbook(buf).worksheets[0]["A2"]
    assert cell.value == "=1+1" and cell.data_type == "s"


def test_no_labels_gives_just_the_header():
    buf = io.BytesIO()
    write_label_sheet([], buf)
    assert read_column(buf)[0] == ["Profile"]


def test_areas_not_read_mark_the_file_incomplete():
    buf = io.BytesIO()
    write_label_sheet(["C50"], buf, not_read=[(1, (3200, 0, 6700, 3500), "HTTP 500")])
    wb = load_workbook(buf)
    assert wb.sheetnames == ["Labels (incomplete)", "Not read"]
    assert list(wb["Not read"].iter_rows(values_only=True)) == [
        ("Tile", "x0", "y0", "x1", "y1", "Error"),
        (1, 3200, 0, 6700, 3500, "HTTP 500"),
    ]


def test_lines_the_llm_never_checked_mark_the_file_incomplete():
    buf = io.BytesIO()
    not_checked = [
        ("4", "W31OX39", (0, 100, 200, 150), "HTTP 500"),
        ("5", "=SUM(A1)", (0, 200, 90, 250), "HTTP 500"),
    ]
    write_label_sheet(["C50"], buf, not_checked=not_checked)
    wb = load_workbook(buf)
    assert wb.sheetnames == ["Labels (incomplete)", "Not checked"]
    sheet = wb["Not checked"]
    assert list(sheet.iter_rows(values_only=True)) == [
        ("Line", "Text", "x0", "y0", "x1", "y1", "Error"),
        ("4", "W31OX39", 0, 100, 200, 150, "HTTP 500"),
        ("5", "=SUM(A1)", 0, 200, 90, 250, "HTTP 500"),
    ]
    assert sheet["B3"].data_type == "s"  # OCR text starting with "=" stays text, not a formula


def test_control_characters_are_removed_so_the_file_can_still_be_written():
    buf = io.BytesIO()
    write_label_sheet(["W10\x00X12"], buf, not_checked=[("4", "SEE\x1bNOTE", (0, 0, 10, 10), "bad\x07 reply")])
    wb = load_workbook(buf)
    assert wb.worksheets[0]["A2"].value == "W10X12"
    assert list(wb["Not checked"].iter_rows(min_row=2, values_only=True)) == [
        ("4", "SEENOTE", 0, 0, 10, 10, "bad reply")
    ]
