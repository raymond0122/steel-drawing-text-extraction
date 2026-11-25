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


def test_llm_followup_still_scores_its_saved_runs(monkeypatch):
    monkeypatch.syspath_prepend(str(LLM_BATCH))
    followup = importlib.import_module("followup")  # also checks that the app's prompt is the one measured

    found = {key: [r["found"] for r in runs] for key, runs in followup.summary().items()}
    assert found[("asis_300", "gpt-4o", 25, "before")] == [82, 75, 82, 82, 75, 82]
    assert found[("asis_300", "gpt-4o", 25, "section")] == [82] * 8 + [81, 82]
