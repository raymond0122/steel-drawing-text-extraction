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

In live mode the app now sends 25 lines per request. Offline, the synthetic drawing's 21 lines still go 10 per request, so the demo shows several batches and one of them can be made to fail; the rule-based stand-in gives the same labels at any batch size. The follow-up below confirmed 25 with a changed prompt.

## Follow-up: the prompt, and the batch size again

Later the same day, gpt-4o at 25 lines per request no longer found every label on the same input: two runs of six missed the 7 labels of one batch (`W10x12`, `W12x19`), where the model answered for 2 of its 9 label lines. So the prompt and the batch size were measured again ([`followup.py`](followup.py), the runs in `results/followup/`) on two inputs: the 300 px-overlap lines used above, where the misses showed, and the 350 px-overlap lines the app sends now. The conditions ran in turn, so each saw the same time of day.

The prompt asked to "fix OCR mistakes, uppercase, remove spaces", and said nothing of the stud count and camber that follow the section on a beam label; replies sometimes kept them (`W10X12(6)` on 17 labels in one run). The app's prompt now asks for the section designation alone.

| gpt-4o, 25 lines per request | Beam labels found (of 82), in run order |
|---|---|
| 300 px lines, the prompt before | 82, 75, 82, 82, 75, 82 |
| 300 px lines, section only | 82, 82, 82, 82, 82, 82, 82, 82, 81, 82 |
| 350 px lines, the prompt before | 82 in all 10 runs |
| 350 px lines, section only | 82 in all 10 runs |

| gpt-4o, section only | 10 lines per request | 25 lines per request |
|---|---|---|
| Beam labels found, 300 px lines | 82 in all 5 runs | 82 in 9 runs of 10, 81 in one |
| Beam labels found, 350 px lines | 82 in all 5 runs | 82 in all 10 runs |
| Time (mean) | 54 s | 29 s |
| Input tokens | 24,900 | 16,800 |

- **The section-only prompt stopped the missed batch:** 9 runs of 10 found every label, against 4 of 6 before, for about 4% more input tokens. No reply kept a stud count in these runs with either prompt, so its effect on that is not measured.
- **With it, 10 and 25 lines read alike, and 25 took half the time with a third fewer tokens,** so the app keeps 25. The one run at 50 lines found 68.
- **One request at 10 lines came back with no text,** like one in the runs above. The app now asks again once before it counts the batch as failed.

Other models, with the prompt before, 25 lines and the 350 px lines, five runs each: `gpt-4.1-2025-04-14` and `gpt-5.4-2026-03-05` found all 82 every time; `gpt-5.4-mini-2026-03-17` did in four runs and kept the stud count on 17 labels in the fifth. On the 300 px lines, gpt-4.1 found all 82 in all 10 runs, with either prompt. The model is set with `OPENAI_MODEL`; these runs were a check, not a choice of model.

## Limits

- **One sheet, one model** (`gpt-4o` as of 2026-11-24), five to ten runs per condition. Another model, prompt or drawing may move the best size.
- **The same model name gave different results within a day** on the same input: 82 in five runs in the morning, 75 in two runs of six later. A dated version, such as `gpt-4o-2024-11-20`, can be set with `OPENAI_MODEL`, but replies still vary from run to run.
- **Only the 82 beam labels are scored.** The model also labelled 37 to 51 other lines per run, a mix of real labels (`HSS7X7X1/2`, `L4X4X1/4`, the columns' `W12X79`) and lines that are not labels (the sheet number `S-201` as `S201`, the brace tag `BF-2` as `BF2`). There is no answer key for them.
- **Why the model skips a batch's labels, or returns no text, was not measured.**

## Re-running

From the repository root, with the package installed (`pip install -e ".[dev]"`):

```bash
python experiments/llm_batch/batch_sizes.py score            # score the saved runs, no network
python experiments/llm_batch/batch_sizes.py run 10,25 1-5    # calls OpenAI, needs .env; saved runs are skipped
python experiments/llm_batch/followup.py score                # the follow-up, no network
python experiments/llm_batch/followup.py run 1-5 gpt-4o/25/section/asis_350   # calls OpenAI, needs .env
```

`results/batch_<size>_run<n>.json` holds each run of the first experiment, `results/followup/` each run of the follow-up: the labels returned, the time, the token counts and any failed batch. The first experiment's runs used the prompt before. `tests/test_experiment.py` scores the saved runs on every test run, so a change to the package that breaks these scripts fails the test suite, and a change to the app's prompt fails it too, until the follow-up is measured again.
