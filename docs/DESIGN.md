# Design notes

The details behind the [README](../README.md): demo outputs, thresholds, retry rules, the web UI, and what this version changes.

## Demo outputs

```bash
steel-demo --simulate-ocr-failure              # offline, but one OCR tile fails on purpose
steel-demo --simulate-llm-failure              # offline, but one LLM batch fails on purpose
uvicorn steel_text.auth.main:app --reload      # auth API on its own, then open http://127.0.0.1:8000/docs
```

The demo writes these files to `out/`:

| File | What it shows |
|---|---|
| `00_sample_drawing.png` | the synthetic input drawing |
| `01_tile_grid.png` | how the page was tiled (overlap bands are darker) |
| `02_raw_ocr.png` | every OCR line from every tile, before merging |
| `03_merged_ocr.png` | after dedup |
| `03b_dedup_closeups.png` | the first 40 dedup decisions, before and after (every one is in `dedup_log.txt`) |
| `04_labels.png`, `labels.json` | steel labels found by the LLM step, with kind and box |
| `labels.xlsx` | the same labels as an Excel list: one `Profile` column, one row per label found |
| `dedup_log.txt` | why each chunk was dropped |
| `not_read.json` | only when a tile could not be read: each such tile, its area and the error |
| `not_checked.json` | only when an LLM batch failed: each line it held, which may be a label, with its box and the error |

When a tile could not be read, its area is hatched red on `01` to `04`. When either of the last two files is written, the demo exits with code 1: the outputs are there, but incomplete.

## Tiling (`steel_text/tiling.py`)

Large-format drawings rendered at high DPI can exceed 10,000 px on the long side, the largest image Azure accepts, and shrinking a page to fit makes its small text unreadable. So a page whose longest side is over 3,500 px is cut into 3,500 px tiles at full resolution, and a smaller page is sent whole.

Tiles overlap by 350 px, so a text up to 350 px long that crosses a tile edge is whole in one of the two tiles ([why 350 px](#why-350-px)). If the grid would exceed `max_tiles`, the tile size grows by 15% until it fits, but never past 10,000 px. A page that would still need more tiles is refused with an error that says how many it needs.

### Why 3,500 px

A tile is cut from the page without shrinking it, so the text keeps its size and the tile size only changes how large an image each request sends. The tile size was measured on one full-size test sheet: a synthetic 36 × 24 in steel framing plan rendered at 400 dpi (14,400 × 9,600 px, beam labels about 31 px tall). The sheet, the scripts and the raw results are in [experiments/tiling](../experiments/tiling/). The sheet was sent through Azure once per tile size, with a 300 px overlap, and scored against its 82 beam labels and its 23 smallest lines, both checked by hand:

| Tile size | Requests | Time | Beam labels read exactly (of 82) | Smallest lines read exactly (of 23) |
|---|---|---|---|---|
| 2,000 px | 54 | 395 s | 81 | 23 |
| 2,500 px | 35 | 255 s | 81 | 23 |
| 3,000 px | 24 | 171 s | 82 | 23 |
| **3,500 px** | 15 | 123 s | 81 | 23 |
| 4,500 px | 12 | 99 s | 82 | 23 |
| 6,000 px | 6 | 50 s | 80 | 22 |
| 8,000 px | 4 | 39 s | 72 | 22 |
| 10,000 px | 2 | 28 s | 78 | 21 |

Times include the waits for rate limiting (HTTP 429), which grow with the number of requests.

- **Up to 4,500 px, every size read equally well.** Smaller tiles only added requests and time.
- **From 6,000 px on, accuracy dropped although the text was the same size.** Azure reads a large image less well than a small one.
- **No beam label was cut by a tile edge at any size:** each fitted whole inside at least one tile. The differences come from the image size, not from cut text.

3,500 px is inside the range that read well, with a margin below where it drops. Lowering `max_tiles` makes the tiles grow, which trades accuracy for fewer requests.

This is one clean synthetic sheet at one resolution, with each size run once. Scanned drawings, other resolutions or denser text may move the point where accuracy drops, so 3,500 px is a measured default for drawings like this one, not a rule for every drawing.

### Why 350 px

The overlap follows from geometry, not from a measurement: a text no longer than the overlap that crosses a tile edge always lies whole inside one of the two tiles, and a longer one may lie whole inside neither. So the overlap has to beat the longest text that must stay whole. On the test sheet the 82 beam labels are 309 px long at the median and 342 px at most, longer than the 300 px the tile size was measured with ([experiment 5](../experiments/tiling/README.md#experiment-5-tile-overlap)):

| Overlap | Drawing placements with a beam label no tile holds whole (of 400) | Tiles over 3,000 random page sizes |
|---|---|---|
| 300 px | 20 | (the base) |
| **350 px** | **0** | **+1.7%** |
| 450 px | 0 | +5.7% |

Read with Azure, the sheet moved 1,400 px right on a wider page lost one cut label at 300 px: `W24x55` came back as `V24x55`, and the LLM passed it on as the section `V24X55`, a wrong label in the final result. At 350 px nothing was cut or misread, and the sheet as it is read as well as at 300 px, with the same 15 tiles.

Text is longer at a higher resolution: a 342 px label at 400 dpi is about 510 px at 600 dpi. So 350 px is the default for drawings like the test sheet, and the overlap can be set per run, from 0 to 1,000 px (`steel-demo --overlap`, the web UI's **Tile overlap**). Long note lines, up to 1,920 px on the test sheet, are not protected by any overlap of reasonable cost.

## OCR per tile (`steel_text/ocr_stage.py`)

A grayscale page, like the test sheet, stays grayscale (`steel_text/page_image.py`): as RGB it would hold each pixel's one value three times. Opening the 14,400 × 9,600 px test sheet then peaks at 150 MB instead of 678 MB, and its 15 tiles send 2.2 MB instead of 4.4 MB. Azure read the grayscale tiles exactly as it read RGB ones: the same 696 lines, with the same text and boxes ([results](../experiments/tiling/results/tiled_grayscale.json)).

Each tile is cut from the page and sent to Azure on its own. For every line of text it reads, Azure returns the text and a polygon: the four corners of the area the line covers, in pixels of the tile image. It is a polygon rather than a rectangle because text can be slanted.

Two steps turn the polygon into the box the rest of the pipeline uses:

1. **Polygon to box** (`geometry.poly_to_bbox`): the smallest upright rectangle around the four corners, `(x0, y0, x1, y1)`, in whole pixels: the corners can fall between pixels, so the start is rounded down and the end up. With upright boxes, areas and overlaps are just width × height.
2. **Tile to page coordinates:** Azure measures from the tile's top-left corner, so the tile's position on the page is added. A line at `(100, 50, 400, 100)` in a tile that starts at `(1800, 0)` is at `(1900, 50, 2200, 100)` on the page. Boxes from two tiles can only be compared once both are in page coordinates.

Lines shorter than 2 characters are dropped here: single letters are grid marks, not labels. Each remaining line becomes a **chunk**: its text, its box on the page, and the tile that read it. Dedup works on these chunks.

### When a tile can't be read

A tile whose request still fails after its retries is skipped, and the other tiles are read as usual. Stopping the whole page would throw away every tile already read. Returning the rest without a word would be worse: a list of labels with one area missing looks the same as a complete one, and nobody would know which labels to look for.

So the gap is carried into every output. The web UI hatches the tile red on every step and says above each step which tile wasn't read and why. The demo hatches it on its images and writes `not_read.json`. The Excel file is titled "Labels (incomplete)" and gets a "Not read" sheet listing the area. The whole tile is marked, although the strips it shares with its neighbours were read by them.

A page where no tile can be read has nothing to show, so it fails: a 502 in the web UI, an error in the demo. `steel-demo --simulate-ocr-failure` and **Make OCR tile 1 fail** in the web UI show a failed tile without Azure.

## Dedup (`steel_text/dedup.py`)

Only chunks read by **two different tiles, inside the area both tiles cover,** are compared. One OCR pass doesn't read a text twice, so chunks from the same tile are always kept, even a label printed twice close together. Text outside the shared area can only have been read by one of the two tiles.

Such a pair is one text read twice if either of these holds:

1. **Same text, same place:** the texts match, ignoring case and extra spaces and reading `×` as `X`, and the boxes share most of their area: IoU (intersection / union, where the union is both box areas added minus the intersection) ≥ 0.5. This is a text read whole by both tiles.
2. **Overlapping box:** the overlap ratio (intersection / smaller box area) is ≥ 0.8, so the smaller reading lies (almost) inside the larger one. This is a cut-off piece inside the full reading, or the same text read differently by the two tiles.

Anything else is two different texts that happen to be close, such as `C50` printed just above `C500`, and both are kept.

The overlap ratio is there for fragments, which IoU misses. A fragment's text differs from the full reading, so rule 1 never applies to it, and rule 2 needs a measure that is high when one box lies inside another. Say `"SEE NOTE 6"` is read in a 300 × 50 px box and the cut-off `"SEE"` in a 90 × 50 px box inside it:

- IoU = 4,500 / 15,000 = 0.3, below 0.5, so IoU alone would treat them as unrelated.
- Overlap ratio = 4,500 / 4,500 = 1.0, because the smaller box is entirely covered.

IoU equals smaller box / larger box here only because the small box lies entirely inside the large one. When two boxes only partly overlap, the union is larger than either box.

Chunks are processed longest text first, then larger box first, and of two readings of one text the first is kept. So the full string is already kept when its fragments come up. Azure's text lines carry no confidence score, so there is none to compare, and dedup can't tell a misreading from the correct reading of the same label: it keeps the longer one, which is sometimes the misreading.

### Why 0.5 and 0.8

Both were checked on the full-size test sheet, by merging the saved OCR of all eight tile sizes again with one threshold changed at a time ([experiment 3](../experiments/tiling/README.md#experiment-3-dedup-thresholds)):

- **IoU 0.5** is the usual threshold for "the same object" in object detection. Here it hardly matters: two tiles reading the same text return almost the same box (median IoU 0.91), and any value from 0.3 to 0.9 changes at most two lines.
- **Overlap 0.8** means the dropped reading lies (almost) inside the kept one, so dropping it loses no text. Lower values also merge two different cuts of one long line, each holding words the other lacks, and now and then two different texts. Higher values leave duplicates and fragments behind.

Each decision in `dedup_log.txt` names the chunk that was dropped and the one kept in its place.

## Retry and backoff (`steel_text/http.py`)

Azure and OpenAI requests go through the same retry rule. Azure Document Intelligence is asynchronous: you POST the image, receive a 202 with an `Operation-Location` URL, and poll that URL. The OpenAI call is a single POST. Every request, Azure's polling GETs included, retries on 429, on 500/502/503/504 and on network errors, with up to 5 attempts:

- **When the server sends `Retry-After`** (in seconds): wait that long, but at most 60 s. A longer wait, such as a quota window, isn't worth holding a request for.
- **Otherwise:** exponential backoff of 1, 2, 4, 8 s, capped at 30 s.
- **Other 4xx errors** (for example 401, or 404 for an unknown operation): fail immediately, on the POST and while polling, because retrying won't help.
- **OpenAI's 429 for used-up quota or credit** (error type `insufficient_quota`, with the code `insufficient_quota` or `credit_balance_exhausted`): fails immediately too. Unlike a rate limit, it lasts until the billing changes, so retrying would only add 15 s of waiting to every batch.
- **Polling gives up** when every attempt of a poll request fails with a network error, or when the 5-minute poll timeout passes. The timeout is checked after each request as well, since a request's retries can take a while.

A tile that still fails after all this is skipped and marked as not read (see [When a tile can't be read](#when-a-tile-cant-be-read)). An LLM batch that still fails is skipped and its lines are marked as not checked (see [LLM label filter](#llm-label-filter-steel_textllm_filterpy)).

## LLM label filter (`steel_text/llm_filter.py`)

- Uses the OpenAI Responses API with a **strict JSON schema**. The model returns only `{str_id, normalized_label, material_kind}`.
- Boxes and raw text are joined back from the OCR results by ID. The model cannot invent a location: IDs that weren't in the batch, and repeats of an ID already labelled, are dropped.
- The prompt asks for the section designation alone, without the stud count and camber that follow it on a beam label. With the earlier prompt gpt-4o sometimes kept them (`W10X12(6)`), and on one input it skipped the labels of a whole batch in 2 runs of 6; with this one it found every label in 9 runs of 10 and missed one label in the tenth ([experiment](../experiments/llm_batch/README.md#follow-up-the-prompt-and-the-batch-size-again)).
- Text is sent in batches of 25 lines in live mode. With this prompt, 10 and 25 lines found the labels alike, and 25 took half the time and a third fewer tokens; 50 or more lines missed labels. Offline, the synthetic drawing's 21 lines go 10 at a time, so it still shows several batches.
- The model is set with `OPENAI_MODEL` (default `gpt-4o`, which every measurement above used).
- A batch that fails (an HTTP error or timeout its retries didn't fix, or an unparseable reply) is skipped, so one bad request doesn't lose the page. A reply with no text is asked for once more first. The page fails only if every batch fails.
- The lines of a skipped batch were never checked and may hold labels, so, like a tile that wasn't read, they are carried into every output. The web UI draws them dashed and says which batch failed. The demo writes `not_checked.json`. The Excel file is titled "Labels (incomplete)" and gets a "Not checked" sheet listing each line with its text and box, so a reviewer can check them by hand.

## Settings (`steel_text/settings.py`)

Every value the app reads from outside is a field of one `Settings` class (pydantic-settings). Environment variables win over `.env`, which is read from the working directory. Spaces around a value are removed, and empty values are ignored so an empty line in `.env` cannot blank out a variable set in the shell.

Settings are passed in rather than read where they are used: `make_clients` takes a `Settings`, and the FastAPI apps get one through a dependency. The tests override that dependency with a `Settings` built in the test, so they never touch the real `.env`. The web UI loads a fresh `Settings` on every request (keys added to `.env` while it runs take effect without a restart), while the auth API loads it once per process, since the JWT signing key must not change while the server runs.

## Auth (`steel_text/auth/`)

Live runs in the web UI spend Azure and OpenAI credit, so they need a valid access token from this API (`require_login_for_live` in `steel_text/web/server.py`). The token is checked with the auth API's own settings, so it is signed and checked with the same key. Offline runs need no login, and neither does `steel-demo --live`, which runs on the user's own machine with their own keys.

- Passwords are hashed with PBKDF2-SHA256 (passlib) and stored in SQLite.
- Login returns an HS256 access token (60 min) and a refresh token (7 days).
- Each token carries a `type` claim, so a refresh token can't be used as an access token.
- Login gives the same error, and takes about the same time, for an unknown user and a wrong password: for an unknown user, a dummy password check runs.
- Spaces around a username are removed before validation, so a username of only spaces is rejected.
- `/me` is protected by a FastAPI dependency that requires a valid access token.
- Without `JWT_SECRET_KEY`, a random signing key is used and a warning is logged, so tokens stop working on restart.
- Both the auth API and the web UI answer only requests addressed to `127.0.0.1` or `localhost`. Otherwise a web page elsewhere could point its own domain at this machine (DNS rebinding), then register, log in and start live runs from the visitor's browser.

## Offline mode

The demo and the web UI run offline by default, using two stand-ins in `steel_text/mock_clients.py`:

- **`SimulatedOcr`** knows where the text is on the synthetic drawing. It mimics what OCR returns at tile edges: partial strings with clipped boxes and a pixel or two of jitter.
- **`RuleBasedLabeler`** mimics the LLM's output format with simple patterns, including common OCR confusions (`O`→`0`, `S`→`5`).

They exist only so the demo and tests run without API keys. The real clients are `AzureReadClient` and `OpenAIResponsesClient`.

Both the demo and the web UI pick their clients and run the stages through `steel_text/pipeline.py`, so the two always run the same flow. Requests are paced only in live mode: 0.2 s between OCR tiles and 0.1 s between LLM batches, to stay under the services' rate limits.

## Web UI (`steel_text/web/`)

- **Pipeline tab:** pick offline or live mode, the LLM batch size, the tile limit and the tile overlap, each with its recommended value below it, then run. Offline, an OCR tile or an LLM batch can be made to fail on purpose. Four steps can be inspected on the page:
  - **Tiles:** the grid, how many lines each tile read, and which tiles could not be read. Those stay hatched red on every step.
  - **Raw OCR:** every line from every tile, with the ones dedup will drop marked.
  - **Dedup:** each decision with its reason and a before/after close-up.
  - **Labels:** the labels found, which batches succeeded or were skipped, and a check against the ground truth by text and place: a label printed twice must be found twice, and one read at the wrong place counts as missing. **Download Excel** saves them as `labels.xlsx`.
- **Viewing the drawing:** scroll to zoom, drag to pan, and click a row to jump to it.
- **Log in** (top right): register or log in, which unlocks live runs; the page then shows who is logged in. A live run refused with a 401, for example after the 60-minute access token expires, logs the page out again.
- **Auth API tab:** register, log in, call `/me` with the access token, the refresh token, or no token, and refresh. Each request and response is shown, with the decoded token claims. It uses the same login as the top bar.

The server is `steel_text/web/server.py`. `POST /api/run` returns every intermediate result as JSON, with the response models in `steel_text/web/schemas.py`, and the page draws it with no external libraries: `index.html` holds the markup, `app.js` the script and `style.css` the styles. The auth API is mounted under `/auth-api`. Live mode needs the same `.env` as `steel-demo --live`, and a login. It is the only mode that accepts an uploaded page image. An upload over 100 MB is refused with a 413 as it arrives, and images over 300 million pixels are refused with a 413 before they are decoded (an A0 sheet scanned at 400 dpi is about 250 million), and the demo applies the same limit.

Every run is timed per tile and per LLM batch, and the page shows both. Retries, rate limiting and per-tile progress are logged in the server's terminal.

In live mode, the page shows which keys it found in `.env`, by name only, never the values. The server reads `.env` again before every live run, so keys added while it is running work without a restart.

## Excel label list (`steel_text/label_sheet.py`)

The Excel file has only the `Profile` column, plus a "Not read" sheet when a tile could not be read and a "Not checked" sheet when an LLM batch failed. It is a label list, not a bill of materials: lengths and quantities would need member geometry and label-to-member linking, which are not in this repository, and a label count is not a member count either, since one label can point at several members.
