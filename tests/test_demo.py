"""The steel-demo command: what it writes and its exit code when part of the page is missing."""

import json
import sys

import pytest
from openpyxl import load_workbook

from steel_text import demo


def run_demo(monkeypatch, out, *flags):
    monkeypatch.setattr(sys, "argv", ["steel-demo", "--out", str(out), *flags])
    return demo.main()


def test_a_skipped_llm_batch_marks_every_output_incomplete_until_a_clean_run(tmp_path, monkeypatch):
    assert run_demo(monkeypatch, tmp_path, "--simulate-llm-failure") == 1  # written, but incomplete

    not_checked = json.loads((tmp_path / "not_checked.json").read_text(encoding="utf-8"))
    assert not_checked and all(line["error"] == "HTTP 500 (simulated failure)" for line in not_checked)
    excel = load_workbook(tmp_path / "labels.xlsx")
    assert excel.sheetnames == ["Labels (incomplete)", "Not checked"]
    assert excel["Not checked"].max_row - 1 == len(not_checked)

    assert run_demo(monkeypatch, tmp_path) == 0
    assert not (tmp_path / "not_checked.json").exists()  # the previous run's gaps don't linger
    assert load_workbook(tmp_path / "labels.xlsx").sheetnames == ["Labels"]


@pytest.mark.parametrize(
    "flag, value",
    [("--overlap", "5000"), ("--overlap", "-5"), ("--batch-size", "0"), ("--batch-size", "ten"), ("--max-tiles", "0")],
)
def test_a_bad_setting_is_refused_before_anything_runs(monkeypatch, capsys, flag, value):
    monkeypatch.setattr(sys, "argv", ["steel-demo", flag, value])
    with pytest.raises(SystemExit) as exit_:
        demo.parse_args()
    assert exit_.value.code == 2  # nothing written, unlike 1 (written, but incomplete)
    assert f"argument {flag}" in capsys.readouterr().err
