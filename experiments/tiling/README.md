# Tiling experiment

Three questions, measured on one drawing with Azure Document Intelligence `prebuilt-read` (API version 2024-11-30):

1. **Is tiling needed at all,** or does the whole page, shrunk to fit, read just as well? (run 2026-11-15)
2. **How large should the tiles be?** (run 2026-11-23)
3. **How much do the dedup thresholds matter?** (the OCR saved by experiment 2, merged again; no new requests)

The drawing is [`steel_framing_plan_S-201_400dpi.png`](steel_framing_plan_S-201_400dpi.png): a made-up 36 × 24 in steel framing plan rendered at 400 dpi, 14,400 × 9,600 px. It belongs to no real project.

## Answer key

- **82 beam labels** on the plan, such as `W16x26 (22) c=3/4"`: a section name, a stud count and, on 56 of them, a camber. They were taken from the full-resolution reading and checked two ways: every label crop by eye ([`check/`](check/)), and the count of each section against the sheet's own MEMBER TAKE-OFF table. The eye check corrected one reading, a lowercase `c=` read as `C=`.
- **The 23 smallest lines** on the sheet, such as `S-201` inside the section bubbles, `BF-1` and the title-block field names. Their letters are about three quarters of a label's height.

For each label, the line read at the same place is scored on its section, stud count, camber, and whether the whole line matches exactly. `x` against `×`, and spacing, don't count as errors.

## Experiment 1: tiles, or the whole page shrunk

The page was sent once as the app sends it (3,500 px tiles at full resolution, 300 px overlap) and once each as a single image shrunk to a longest side of 14,400 to 3,000 px.

| Sent as | Image | Label letter height | Requests | Time |
|---|---|---|---|---|
| Tiles, full resolution | 14,400 × 9,600 | 31 px | 15 | 123 s |
| Whole, 14,400 px | 14,400 × 9,600 | 31 px | 1 | rejected by Azure |
| Whole, 10,000 px | 10,000 × 6,667 | 21.5 px | 1 | 16 s |
| Whole, 8,000 px | 8,000 × 5,333 | 17.2 px | 1 | 14 s |
| Whole, 6,000 px | 6,000 × 4,000 | 12.9 px | 1 | 8 s |
| Whole, 4,000 px | 4,000 × 2,667 | 8.6 px | 1 | 6 s |
| Whole, 3,000 px | 3,000 × 2,000 | 6.5 px | 1 | 7 s |

Azure refused the full-size page with `HTTP 400 InvalidContentDimensions: The input image dimensions are out of range.` Its documented 10,000 px limit is enforced. The tiled run's time includes waits for rate limiting (HTTP 429).

**Beam labels (82):**

| Sent as | Section | Studs | Camber (of 56) | Whole line exact |
|---|---|---|---|---|
| Tiles | 82 | 82 | 56 | **81** |
| Whole, 10,000 px | 82 | 82 | 53 | 48 |
| Whole, 8,000 px | 81 | 82 | 55 | 54 |
| Whole, 6,000 px | 82 | 82 | 56 | 66 |
| Whole, 4,000 px | 81 | 82 | 41 | 63 |
| Whole, 3,000 px | 81 | 82 | 9 | 32 |

**Smallest lines (23):**

| Sent as | Letter height | Read exactly | Nothing read |
|---|---|---|---|
| Tiles | 23 px | **23** | 0 |
| Whole, 10,000 px | 16 px | 19 | 3 |
| Whole, 8,000 px | 13 px | 18 | 3 |
| Whole, 6,000 px | 10 px | 17 | 2 |
| Whole, 4,000 px | 6.5 px | 13 | 1 |
| Whole, 3,000 px | 5 px | 9 | 6 |

What this shows:

- **Section names and stud counts survive shrinking.** Even with 6.5 px letters, 81 of 82 sections were right; the miss read `W21x44` as `V21x44`. For section names alone, this sheet didn't need tiling.
- **Fractions break between 12.9 and 8.6 px letters.** At 8.6 px `3/4` starts coming back as `1/4`, and at 6.5 px almost every camber is wrong. Azure's documentation gives about 12 px as the minimum.
- **Most whole-line errors at 10,000 and 8,000 px are the trailing inch mark,** dropped or read as `'` (29 and 23 labels). Wrong field values were only 2 or 3.
- **The whole page didn't get better as it got larger:** whole lines were best at 6,000 px and worse at 10,000 px. Experiment 2 explains why.
- **The small text is where tiling clearly wins.** The whole pages mostly lost `S-201` inside a circle, reading it as `5-201` or not at all. At 10,000 px they also dropped the last character of section names in the take-off table (`W10x12` as `W10x1`, `W21x50` as `W21x5`).
- **Single characters are dropped on purpose.** The tiled path skips lines shorter than 2 characters, so grid numbers and letters and one-digit quantities are missing from it. The whole page at 10,000 px returned 91 such lines, some of them misread symbols.

## Experiment 2: tile size

Each tile is cut at full resolution, so the letters stay 31 px tall at every size and only the image sent per request changes. The overlap stays at 300 px, and every size is merged with the current dedup. The 3,500 px row is experiment 1's tiled run.

| Tile size | Requests | Time | 429 retries | Camber (of 56) | Whole line exact (of 82) | Smallest lines (of 23) |
|---|---|---|---|---|---|---|
| 2,000 px | 54 | 395 s | 86 | 56 | 81 | 23 |
| 2,500 px | 35 | 255 s | 60 | 56 | 81 | 23 |
| 3,000 px | 24 | 171 s | 51 | 56 | 82 | 23 |
| 3,500 px | 15 | 123 s | not recorded | 56 | 81 | 23 |
| 4,500 px | 12 | 99 s | 18 | 56 | 82 | 23 |
| 6,000 px | 6 | 50 s | 7 | 54 | 80 | 22 |
| 8,000 px | 4 | 39 s | 1 | 56 | 72 | 22 |
| 10,000 px | 2 | 28 s | 0 | 55 | 78 | 21 |

What this shows:

- **Up to 4,500 px, every size read equally well.** Smaller tiles only added requests, and with them rate-limit waits.
- **From 6,000 px on, accuracy drops although the letters are the same size.** Azure reads a large image less well than a small one. This is why the whole page got worse above 6,000 px in experiment 1.
- **No beam label was cut by a tile edge at any size:** each fitted whole inside at least one tile, so the differences come from the image size. The labels are up to 342 px long, a little more than the overlap, so a label crossing an edge in an unlucky place could still be cut.
- **Dedup removed nothing from the same tile at any size.**

The app uses 3,500 px tiles: inside the range that read well, with a margin below where accuracy drops. The full rule is in [docs/DESIGN.md](../../docs/DESIGN.md#tiling-steel_texttilingpy).

## Experiment 3: dedup thresholds

Dedup treats two readings from different tiles as one text when they say the same thing in the same place (IoU ≥ 0.5), or when one box covers at least 80% of the smaller one. The saved raw OCR of all eight tile sizes was merged again with one threshold changed at a time ([`dedup_thresholds.py`](dedup_thresholds.py)). Lines kept, as a change from those defaults:

| Setting | 2,000 | 2,500 | 3,000 | 3,500 | 4,500 | 6,000 | 8,000 | 10,000 |
|---|---|---|---|---|---|---|---|---|
| IoU 0.3 | 0 | 0 | 0 | 0 | 0 | 0 | −1 | 0 |
| IoU 0.7 | 0 | 0 | 0 | 0 | 0 | 0 | +2 | 0 |
| IoU 0.9 | 0 | 0 | 0 | 0 | 0 | 0 | +2 | 0 |
| Overlap 0.5 | −7 | −16 | −22 | 0 | −3 | −3 | −6 | −1 |
| Overlap 0.6 | −6 | −15 | −6 | 0 | −3 | −3 | −6 | −1 |
| Overlap 0.7 | −2 | −11 | −4 | 0 | −1 | −3 | −6 | 0 |
| Overlap 0.9 | +6 | +9 | +2 | +1 | +1 | +6 | +9 | +6 |
| Overlap 0.95 | +20 | +23 | +8 | +7 | +5 | +13 | +20 | +11 |

The beam-label and smallest-line scores stay the same for every setting, except one label at overlap 0.9 and 0.95 (below).

What this shows:

- **The IoU threshold hardly matters.** Two tiles reading the same text return almost the same box (median IoU 0.91), so any value from 0.3 to 0.9 changes at most two lines, on one size.
- **A lower overlap threshold drops text.** From 0.7 down, the extra merges are mostly two different cuts of one long note line, and dropping one loses its end: keeping `…ALL WELDS BY CERTIFIED` drops `LDS BY CERTIFIED WELDERS.`, and with it `WELDERS.`. A few merge different texts, such as `10` into `/ 1`. At 0.8 the dropped reading lies (almost) inside the kept one, so nothing is lost, with room for boxes that jitter by a few pixels.
- **A higher overlap threshold leaves duplicates and fragments behind,** 1 to 23 per size. It also makes one label on the 2,000 and 6,000 px sizes score as exact, but only because the correct reading (`W16x26 (22) c=3/4"`) is then kept next to a misreading of the same label (`W16x26 ( 22) C=3/ 11`), and the scorer picks the correct one. At 0.8 dedup keeps one of the two, the longer text or the larger box, and here that was the misreading: Azure's lines carry no confidence, so dedup can't tell which reading is right.
## Limits

- **One drawing,** cleanly rendered and synthetic. Scanned drawings have noise and skew and may break down earlier, and other resolutions or denser text may move the point where accuracy drops. The results are a measured default for drawings like this one, not a rule for every drawing.
- **Each condition ran once.** The 3,500 px tiled run was repeated and gave exactly the same output ([`results/tiled_first_run.json`](results/tiled_first_run.json)).
- **Experiments 1 and 2 sent RGB tiles.** The app now keeps a grayscale page like this one grayscale. The 3,500 px run repeated with grayscale tiles read the same 696 lines, with the same text and boxes ([`results/tiled_grayscale.json`](results/tiled_grayscale.json)).
- **The whole-page images were shrunk from the 400 dpi image.** Rendering the PDF at a lower dpi could differ slightly.
- **Only the beam labels and the smallest lines are verified.** `summary.json` also compares all 391 lines against the tiled reading, which is agreement, not accuracy.
- **OCR only.** The LLM label filter isn't part of either experiment.

## Re-running

From the repository root, with the package installed (`pip install -e ".[dev]"`):

```bash
python experiments/tiling/analyze.py              # score experiment 1 from the saved results (no network)
python experiments/tiling/analyze_tile_sizes.py   # score experiment 2 from the saved results (no network)
python experiments/tiling/dedup_thresholds.py     # experiment 3, from the same saved results (no network)

python experiments/tiling/run_ocr.py              # experiment 1: calls Azure, needs .env
python experiments/tiling/run_tile_sizes.py       # experiment 2: calls Azure, needs .env
python experiments/tiling/run_grayscale.py        # the 3,500 px run with grayscale tiles: calls Azure, needs .env
```

The `run_` scripts skip any condition whose result is already in `results/`, so they never pay for the same requests twice. Delete a result file to measure that condition again.

| File | What it holds |
|---|---|
| `run_ocr.py`, `run_tile_sizes.py`, `run_grayscale.py` | send the drawing to Azure and save the raw OCR in `results/` |
| `analyze.py`, `analyze_tile_sizes.py`, `dedup_thresholds.py` | build the answer key, score every condition, print the tables |
| `results/*.json` | the raw OCR of each condition, in page coordinates |
| `ground_truth.json` | the 82 verified beam labels |
| `summary.json`, `tile_sizes_summary.json`, `dedup_thresholds_summary.json` | the numbers in the tables above |
| `check/labels_*.png` | the label crops used for the eye check |

`tests/test_experiment.py` re-scores the saved 3,500 px run on every test run, so a change to the package that breaks these scripts fails the test suite.
