# LLM batch size

How many OCR lines the label filter should send to the model per request. Measured with OpenAI `gpt-4o` on 2026-11-24.

## Setup

- **Input:** the test sheet's OCR from the [tiling experiment](../tiling/README.md) (`results/tiled.json`), merged with the current dedup: the 464 lines the app sends for that sheet.
- **The app's own label filter** (`steel_text/llm_filter.py`): the same prompt, strict JSON schema and 0.1 s pause between requests.
- **Batch sizes** 10 (the default until now), 25, 50, 100 and 200. The model's replies vary from run to run, so each size ran five times, 200 twice.
- **Scored against the 82 hand-checked beam labels** of the tiling experiment. A label is found when the line read at its place comes back with its section, such as `W24X55` for `W24x55 (36) c=11/4"`; wrong when it comes back with another label; missed when it comes back with none.

## Results

| Lines per request | Requests | Beam labels found (of 82), runs 1 to 5 | Mean | Time (mean) | Input tokens |
|---|---|---|---|---|---|
| 10 | 47 | 72, 76, 82, 76, 76 | 76.4 | 63 s | 23,221 |
| **25** | 19 | **82, 82, 82, 82, 82** | **82.0** | 36 s | 16,137 |
| 50 | 10 | 75, 75, 75, 82, 75 | 76.4 | 24 s | 13,860 |
| 100 | 5 | 55, 82, 75, 82, 66 | 72.0 | 20 s | 12,595 |
| 200 | 3 | 13, 18 | 15.5 | 11 s | 12,089 |

What this shows:

- **25 found every beam label in every run,** in 43% less time than 10, with 30% fewer input tokens: the prompt's instructions are sent once per request.
- **Smaller batches did not read better.** 10, the default until now, missed up to 10 labels in a run.
- **Larger batches missed labels again.** 50 missed 7 in four of its five runs, 100 found from 55 to 82, and at 200 the model answered for few lines.
- **A miss is a line the model returned nothing for.** No reply named an ID outside its batch or repeated one, so the app dropped nothing. The 5 labels counted as wrong, all in one run at 10, came back with their stud count, such as `W10X12(6)`.
- **In one run at 10, one request came back with no text.** The app skips such a batch without retrying it, and marks its lines as not checked.

## Choice

In live mode the app now sends 25 lines per request. Offline, the synthetic drawing's 21 lines still go 10 per request, so the demo shows several batches and one of them can be made to fail; the rule-based stand-in gives the same labels at any batch size.

## Limits

- **One sheet, one model** (`gpt-4o` as of 2026-11-24), five runs per size. Another model, prompt or drawing may move the best size.
- **Only the 82 beam labels are scored.** The model also labelled 37 to 51 other lines per run, a mix of real labels (`HSS7X7X1/2`, `L4X4X1/4`, the columns' `W12X79`) and lines that are not labels (the sheet number `S-201` as `S201`, the brace tag `BF-2` as `BF2`). There is no answer key for them.
- **Why smaller batches miss labels was not measured.**

## Re-running

From the repository root, with the package installed (`pip install -e ".[dev]"`):

```bash
python experiments/llm_batch/batch_sizes.py score            # score the saved runs, no network
python experiments/llm_batch/batch_sizes.py run 10,25 1-5    # calls OpenAI, needs .env; saved runs are skipped
```

`results/batch_<size>_run<n>.json` holds each run: the labels returned, the time per batch, the token counts and any failed batch. `tests/test_experiment.py` scores the saved runs on every test run, so a change to the package that breaks this script fails the test suite.
