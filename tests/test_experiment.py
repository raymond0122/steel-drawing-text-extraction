"""The experiments import the package, so their scoring must keep working with the current code."""

import importlib
import json
from pathlib import Path

EXPERIMENT = Path(__file__).resolve().parents[1] / "experiments" / "tiling"
LLM_BATCH = EXPERIMENT.parent / "llm_batch"


def test_tiling_experiment_still_scores_its_saved_results(monkeypatch):
    monkeypatch.syspath_prepend(str(EXPERIMENT))
    analyze = importlib.import_module("analyze")
    sizes = importlib.import_module("analyze_tile_sizes")
    answer_key = json.loads((EXPERIMENT / "ground_truth.json").read_text(encoding="utf-8"))

    lines, removed, same_tile = sizes.merge(sizes.load_size(3500))
    assert (len(lines), removed, same_tile) == (464, 232, 0)  # the numbers docs/DESIGN.md quotes
    assert analyze.score_labels(answer_key, lines)["counts"]["exact"] == 81


def test_llm_batch_experiment_still_scores_its_saved_runs(monkeypatch):
    monkeypatch.syspath_prepend(str(LLM_BATCH))
    batch_sizes = importlib.import_module("batch_sizes")

    found = {size: [r["found"] for r in runs] for size, runs in batch_sizes.summary().items()}
    assert found[25] == [82] * 5  # the numbers experiments/llm_batch/README.md quotes
    assert found[10] == [72, 76, 82, 76, 76]
