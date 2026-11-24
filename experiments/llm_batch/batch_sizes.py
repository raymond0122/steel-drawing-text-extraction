"""LLM batch size: the test sheet's OCR lines through the real label filter, at several batch sizes.

Run from the repository root, so .env is found:

    python experiments/llm_batch/batch_sizes.py score            # score the saved runs, no network
    python experiments/llm_batch/batch_sizes.py run 10,25 1-5    # sizes 10 and 25, runs 1 to 5: calls OpenAI

The input is the tiling experiment's 3,500 px OCR (experiments/tiling/results/tiled.json),
merged with the current dedup: the 464 lines the app sends for that sheet. Each run is saved
to results/batch_<size>_run<n>.json, and a saved run is never sent again.
"""

from __future__ import annotations

import collections
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import requests

HERE = Path(__file__).resolve().parent
TILING = HERE.parent / "tiling"
RESULTS = HERE / "results"
sys.path.insert(0, str(TILING))

from analyze import inter, overlapping  # noqa: E402

from steel_text.dedup import merge_tile_results  # noqa: E402
from steel_text.llm_filter import OpenAIResponsesClient, filter_labels  # noqa: E402
from steel_text.ocr_types import OcrChunk  # noqa: E402
from steel_text.settings import load_settings  # noqa: E402
from steel_text.tiling import Tile  # noqa: E402

REQUEST_SLEEP_S = 0.1  # the same pacing as the app's live mode


class RecordingSession:
    """A requests.Session that also keeps each reply's token usage."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.usage: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        r = self.session.request(method, url, **kwargs)
        try:
            self.usage.append(r.json().get("usage") or {})
        except ValueError:
            self.usage.append({})
        return r


def merged_lines() -> list[OcrChunk]:
    saved = json.loads((TILING / "results" / "tiled.json").read_text(encoding="utf-8"))
    raw = [OcrChunk(c["text"], tuple(c["bbox"]), c["tile_index"]) for c in saved["raw"]]
    return merge_tile_results(raw, [Tile(*t) for t in saved["tiles"]])


def run(sizes: list[int], runs: range) -> None:
    chunks = merged_lines()
    settings = load_settings()
    RESULTS.mkdir(exist_ok=True)
    for n in runs:
        for size in sizes:
            out = RESULTS / f"batch_{size}_run{n}.json"
            if out.exists():
                print(f"{out.name}: cached", flush=True)
                continue
            session = RecordingSession()
            client = OpenAIResponsesClient(
                settings.openai_api_key, model=settings.openai_model, base_url=settings.openai_base_url, session=session
            )
            t0 = time.perf_counter()
            res = filter_labels(chunks, client, batch_size=size, request_sleep_s=REQUEST_SLEEP_S)
            secs = time.perf_counter() - t0
            record = {
                "batch_size": size,
                "run": n,
                "model": settings.openai_model,
                "lines": len(chunks),
                "batches": res.batches,
                "requests": len(session.usage),
                "seconds": round(secs, 1),
                "batch_ms": res.batch_ms,
                "failed_batches": res.failed_batches,
                "input_tokens": sum(u.get("input_tokens", 0) for u in session.usage),
                "output_tokens": sum(u.get("output_tokens", 0) for u in session.usage),
                "entries": [
                    {"id": e.str_id, "label": e.normalized_label, "kind": e.material_kind, "raw": e.raw_text}
                    for e in res.entries
                ],
            }
            out.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"{out.name}: {res.batches} batches, {secs:.0f} s, {len(res.entries)} labels", flush=True)


def summary() -> dict[int, list[dict[str, Any]]]:
    """Every saved run, scored against the 82 hand-checked beam labels, by batch size in run order.

    A beam label is found when the line read at its place comes back with its section,
    e.g. W24X55 for 'W24x55 (36) c=11/4"'; wrong when that line comes back with another
    label; missed when it comes back with none.
    """
    lines = [{"id": str(i), "text": c.text, "bbox": list(c.bbox)} for i, c in enumerate(merged_lines())]
    answer_key = json.loads((TILING / "ground_truth.json").read_text(encoding="utf-8"))
    at_label = [max(overlapping(g, lines), key=lambda c: inter(g["bbox"], c["bbox"]))["id"] for g in answer_key]
    beam_lines = set(at_label)

    table: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)
    runs = [json.loads(f.read_text(encoding="utf-8")) for f in RESULTS.glob("batch_*_run*.json")]
    for r in sorted(runs, key=lambda r: (r["batch_size"], r["run"])):
        got = {e["id"]: e["label"] for e in r["entries"]}
        want = [(at_label[i], g["section"].upper()) for i, g in enumerate(answer_key)]
        table[r["batch_size"]].append(
            {
                "run": r["run"],
                "found": sum(got.get(line) == section for line, section in want),
                "wrong": sum(line in got and got[line] != section for line, section in want),
                "missed": sum(line not in got for line, _ in want),
                "other_labels": sum(line not in beam_lines for line in got),
                "requests": r["requests"],
                "seconds": r["seconds"],
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
                "failed_batches": len(r["failed_batches"]),
            }
        )
    return dict(table)


def score() -> None:
    print("size  requests | beam labels found (of 82), per run | mean | wrong missed | time  input tok  output tok")
    for size, runs in summary().items():
        found = [r["found"] for r in runs]
        mean = {k: statistics.mean(r[k] for r in runs) for k in ("seconds", "input_tokens", "output_tokens")}
        print(
            f"{size:4}  {runs[0]['requests']:8} | {', '.join(map(str, found)):34} | {statistics.mean(found):4.1f} | "
            f"{sum(r['wrong'] for r in runs):5} {sum(r['missed'] for r in runs):6} | "
            f"{mean['seconds']:3.0f} s {mean['input_tokens']:9.0f}  {mean['output_tokens']:10.0f}"
        )


if __name__ == "__main__":
    if sys.argv[1:2] == ["run"]:
        sizes = [int(s) for s in sys.argv[2].split(",")]
        first, last = (int(n) for n in sys.argv[3].split("-"))
        run(sizes, range(first, last + 1))
    score()
