"""The tiling experiment imports the package, so its scoring must keep working with the current code."""

import importlib
import json
from pathlib import Path

EXPERIMENT = Path(__file__).resolve().parents[1] / "experiments" / "tiling"


def test_tiling_experiment_still_scores_its_saved_results(monkeypatch):
    monkeypatch.syspath_prepend(str(EXPERIMENT))
    analyze = importlib.import_module("analyze")
    sizes = importlib.import_module("analyze_tile_sizes")
    answer_key = json.loads((EXPERIMENT / "ground_truth.json").read_text(encoding="utf-8"))

    lines, removed, same_tile = sizes.merge(sizes.load_size(3500))
    assert (len(lines), removed, same_tile) == (464, 232, 0)  # the numbers docs/DESIGN.md quotes
    assert analyze.score_labels(answer_key, lines)["counts"]["exact"] == 81
