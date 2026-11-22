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
