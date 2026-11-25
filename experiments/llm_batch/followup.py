"""Follow-up of the LLM batch experiment: the prompt, the batch size again, and a few models.

Run from the repository root, so .env is found:

    python experiments/llm_batch/followup.py score
    python experiments/llm_batch/followup.py run 1-5 gpt-4o/25/section/asis_350 gpt-4o/10/section/asis_350

A condition is MODEL/BATCH_SIZE/PROMPT/INPUT:
  PROMPT  before   the app's prompt until 2026-11-24
          section  the app's prompt since, which asks for the section designation alone
  INPUT   asis_350 the test sheet's merged OCR lines at a 350 px tile overlap (what the app sends now)
          asis_300 the same at 300 px (experiment 1's lines), on which gpt-4o missed a batch's labels
Runs go round the conditions, one run each, so every condition sees the same time of day. Each
run is saved to results/followup/ and never sent again.
"""

from __future__ import annotations

import collections
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
TILING = HERE.parent / "tiling"
RESULTS = HERE / "results" / "followup"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(TILING))

from analyze import inter, overlapping  # noqa: E402
from batch_sizes import RecordingSession  # noqa: E402

from steel_text import llm_filter  # noqa: E402
from steel_text.llm_filter import OpenAIResponsesClient, filter_labels  # noqa: E402
from steel_text.ocr_types import OcrChunk  # noqa: E402
from steel_text.settings import load_settings  # noqa: E402

INPUTS = {
    "asis_350": TILING / "results" / "overlap_asis_350.json",
    "asis_300": TILING / "results" / "tiled_grayscale.json",
}
SECTION_PROMPT = llm_filter.build_prompt
SECTION_LINE = (
    "  - normalized_label: the section designation only: fix OCR mistakes, uppercase, remove spaces, and drop "
    "anything after the section such as stud counts, cambers or notes "
    "(e.g., 'CS0' -> 'C50', 'C 50' -> 'C50', 'W10x12 (6)' -> 'W10X12'); keep '*' as is"
)
BEFORE_LINE = (
    "  - normalized_label: fix OCR mistakes, uppercase, remove spaces "
    "(e.g., 'CS0' -> 'C50', 'C 50' -> 'C50'); keep '*' as is"
)


def before_prompt(items: Any) -> str:
    return SECTION_PROMPT(items).replace(SECTION_LINE, BEFORE_LINE)


assert SECTION_LINE in SECTION_PROMPT([]), "the app's prompt changed: update SECTION_LINE"
PROMPTS = {"before": before_prompt, "section": SECTION_PROMPT}


def chunks(source: str) -> list[OcrChunk]:
    lines = json.loads(INPUTS[source].read_text(encoding="utf-8"))["lines"]
    return [OcrChunk(c["text"], tuple(c["bbox"])) for c in lines]


def run(first: int, last: int, conditions: list[str]) -> None:
    settings = load_settings()
    RESULTS.mkdir(parents=True, exist_ok=True)
    for n in range(first, last + 1):
        for cond in conditions:
            model, size, prompt, source = cond.split("/")
            out = RESULTS / f"{model}__{size}__{prompt}__{source}__run{n}.json"
            if out.exists():
                continue
            session = RecordingSession()
            client = OpenAIResponsesClient(settings.openai_api_key, model=model, session=session)
            llm_filter.build_prompt = PROMPTS[prompt]
            try:
                t0 = time.perf_counter()
                res = filter_labels(chunks(source), client, batch_size=int(size), request_sleep_s=0.1)
                secs = time.perf_counter() - t0
            finally:
                llm_filter.build_prompt = SECTION_PROMPT
            record = {
                "model": model,
                "batch_size": int(size),
                "prompt": prompt,
                "input": source,
                "run": n,
                "seconds": round(secs, 1),
                "requests": len(session.usage),
                "failed_batches": res.failed_batches,
                "input_tokens": sum(u.get("input_tokens", 0) for u in session.usage),
                "output_tokens": sum(u.get("output_tokens", 0) for u in session.usage),
                "entries": [{"id": e.str_id, "label": e.normalized_label, "raw": e.raw_text} for e in res.entries],
            }
            out.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"{cond} run {n}: {secs:.0f} s, {len(res.entries)} labels", flush=True)


def summary() -> dict[tuple[str, str, int, str], list[dict[str, Any]]]:
    """Every saved run, scored against the 82 hand-checked beam labels, in run order.

    found: the line at the label's place came back with its section, e.g. W24X55;
    extra_text: with its section and more, e.g. W10X12(6); wrong: with another label;
    missed: with none.
    """
    answer_key = json.loads((TILING / "ground_truth.json").read_text(encoding="utf-8"))
    at = {}
    for source in INPUTS:
        lines = [{"id": str(i), "text": c.text, "bbox": list(c.bbox)} for i, c in enumerate(chunks(source))]
        at[source] = [max(overlapping(g, lines), key=lambda c: inter(g["bbox"], c["bbox"]))["id"] for g in answer_key]
    table: dict[tuple[str, str, int, str], list[dict[str, Any]]] = collections.defaultdict(list)
    runs = [json.loads(f.read_text(encoding="utf-8")) for f in RESULTS.glob("*__run*.json")]
    for r in sorted(runs, key=lambda r: r["run"]):
        got = {e["id"]: e["label"] for e in r["entries"]}
        want = [(at[r["input"]][i], g["section"].upper()) for i, g in enumerate(answer_key)]
        table[(r["input"], r["model"], r["batch_size"], r["prompt"])].append(
            {
                "run": r["run"],
                "found": sum(got.get(line) == s for line, s in want),
                "extra_text": sum(line in got and got[line] != s and got[line].startswith(s) for line, s in want),
                "wrong": sum(line in got and not got[line].startswith(s) for line, s in want),
                "missed": sum(line not in got for line, _ in want),
                "seconds": r["seconds"],
                "input_tokens": r["input_tokens"],
                "failed_batches": len(r["failed_batches"]),
            }
        )
    return dict(sorted(table.items()))


def score() -> None:
    print("input / model / batch / prompt                runs | found (of 82), run order | +text wrong missed | time")
    for (source, model, size, prompt), runs in summary().items():
        found = ", ".join(str(r["found"]) for r in runs)
        name = f"{source} / {model} / {size} / {prompt}"
        print(
            f"{name:46} {len(runs):3}  | {found:30} | {sum(r['extra_text'] for r in runs):5} "
            f"{sum(r['wrong'] for r in runs):5} {sum(r['missed'] for r in runs):6} | "
            f"{statistics.mean(r['seconds'] for r in runs):3.0f} s"
        )


if __name__ == "__main__":
    if sys.argv[1:2] == ["run"]:
        first, last = (int(n) for n in sys.argv[2].split("-"))
        run(first, last, sys.argv[3:])
    score()
