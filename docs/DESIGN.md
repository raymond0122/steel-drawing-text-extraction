# Design notes

The details behind the [README](../README.md): demo outputs, thresholds, retry rules, the web UI, and what this version changes.

## Demo outputs

```bash
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
| `03b_dedup_closeups.png` | each dedup decision, before and after |
| `04_labels.png`, `labels.json` | steel labels found by the LLM step, with kind and box |
| `labels.xlsx` | the same labels as an Excel list: one `Profile` column, one row per label found |
| `dedup_log.txt` | why each chunk was dropped |

## Tiling (`steel_text/tiling.py`)

Large-format drawings rendered at high DPI can exceed 10,000 px on the long side. Sent as a single image, small text gets missed. The tile size depends on the page size:

| Longest side | Tiling |
|---|---|
| up to 4,000 px | no tiling |
| up to 8,000 px | 3,000 px tiles |
| larger | 3,500 px tiles |

Tiles always overlap by 300 px, so text cut at one tile's edge is complete in its neighbour. If the grid would exceed `max_tiles`, the tile size grows by 15% until it fits.

## Dedup (`steel_text/dedup.py`)

Two chunks are compared only if they are **near**. Near means any one of these holds:

- IoU (intersection / union) ≥ 0.5
- overlap ratio (intersection / smaller box area) ≥ 0.8
- the box centres are ≤ 80 px apart

The overlap ratio is there for fragments, which IoU misses. Say `"SEE NOTE 6"` is read in a 300 × 50 px box and the cut-off `"SEE"` in a 90 × 50 px box inside it:

- IoU = 4,500 / 15,000 = 0.3, below 0.5, so IoU alone would treat them as unrelated.
- Overlap ratio = 4,500 / 4,500 = 1.0, because the smaller box is entirely covered.

For near pairs, the following rules apply in order:

1. Boxes almost identical (IoU ≥ 0.9): duplicate.
2. Same text: duplicate.
3. One box contains the other, or overlap ≥ 0.8: drop the fragment, or keep the better reading.
4. One text is a substring of the other: keep the longer one.
5. They share a number and one is a short phrase (`"TE 6"` vs `"SEE NOTE 6"`): keep the longer one.

The better reading is the one with higher confidence, then longer text, then the larger box. Text is processed longest first, and a second pass removes leftover fragments. Characters like `*` are preserved, so `WT155*79` never becomes `WT15579`.

## Retry and backoff (`steel_text/azure_ocr.py`)

Azure Document Intelligence is asynchronous: you POST the image, receive a 202 with an `Operation-Location` URL, and poll that URL. Both the POST and the polling GETs retry on 429, on 500/502/503/504 and on network errors, with up to 5 attempts:

- **On 429:** wait for `Retry-After` if the server sends it.
- **Otherwise:** exponential backoff of 1, 2, 4, 8 s, capped at 30 s.
- **Other errors on the POST** (for example 401): fail immediately, because retrying won't help.

## LLM label filter (`steel_text/llm_filter.py`)

- Uses the OpenAI Responses API with a **strict JSON schema**. The model returns only `{str_id, normalized_label, material_kind}`.
- Boxes and raw text are joined back from the OCR results by ID. The model cannot invent a location, and unknown IDs are dropped.
- Text is sent in batches. A failed batch (HTTP error, timeout, unparseable reply) is logged and skipped, so one bad request doesn't lose the page. The page fails only if every batch fails.

## Settings (`steel_text/settings.py`)

Every value the app reads from outside is a field of one `Settings` class (pydantic-settings). Environment variables win over `.env`, which is read from the working directory, and empty values are ignored so an empty line in `.env` cannot blank out a variable set in the shell.

Settings are passed in rather than read where they are used: `make_clients` takes a `Settings`, and the FastAPI apps get one through a dependency. The tests override that dependency with a `Settings` built in the test, so they never touch the real `.env`. The web UI loads a fresh `Settings` on every request (keys added to `.env` while it runs take effect without a restart), while the auth API loads it once per process, since the JWT signing key must not change while the server runs.

## Auth (`steel_text/auth/`)

- Passwords are hashed with PBKDF2-SHA256 (passlib) and stored in SQLite.
- Login returns an HS256 access token (60 min) and a refresh token (7 days).
- Each token carries a `type` claim, so a refresh token can't be used as an access token.
- Login gives the same error for an unknown user and a wrong password.
- `/me` is protected by a FastAPI dependency that requires a valid access token.
- Without `JWT_SECRET_KEY`, a random signing key is used and a warning is logged, so tokens stop working on restart.

## Offline mode

The demo and the web UI run offline by default, using two stand-ins in `steel_text/mock_clients.py`:

- **`SimulatedOcr`** knows where the text is on the synthetic drawing. It mimics what OCR returns at tile edges: partial strings with clipped boxes and a pixel or two of jitter.
- **`RuleBasedLabeler`** mimics the LLM's output format with simple patterns, including common OCR confusions (`O`→`0`, `S`→`5`).

They exist only so the demo and tests run without API keys. The real clients are `AzureReadClient` and `OpenAIResponsesClient`.

Both the demo and the web UI pick their clients and run the stages through `steel_text/pipeline.py`, so the two always run the same flow. Requests are paced only in live mode: 0.2 s between OCR tiles and 0.1 s between LLM batches, to stay under the services' rate limits.

## Web UI (`steel_text/web/`)

- **Pipeline tab:** pick offline or live mode, the LLM batch size and the tile limit, then run. Four steps can be inspected on the page:
  - **Tiles:** the grid, and how many lines each tile read.
  - **Raw OCR:** every line from every tile, with the ones dedup will drop marked.
  - **Dedup:** each decision with its reason and a before/after close-up.
  - **Labels:** the labels found, which batches succeeded or were skipped, and a check against the ground truth. **Download Excel** saves them as `labels.xlsx`.
- **Viewing the drawing:** scroll to zoom, drag to pan, and click a row to jump to it.
- **Auth API tab:** register, log in, call `/me` with the access token, the refresh token, or no token, and refresh. Each request and response is shown, with the decoded token claims.

The server is `steel_text/web/server.py`. `POST /api/run` returns every intermediate result as JSON, with the response models in `steel_text/web/schemas.py`, and `steel_text/web/index.html` draws it with no external libraries. The auth API is mounted under `/auth-api`. Live mode needs the same `.env` as `steel-demo --live`. It is the only mode that accepts an uploaded page image.

Every run is timed per tile and per LLM batch. In live mode, each tile also shows its HTTP requests, rate-limited or failed responses, and time spent waiting on polling and backoff. The same numbers are printed in the server's terminal.

In live mode, the page shows which keys it found in `.env`, by name only, never the values. The server reads `.env` again before every live run, so keys added while it is running work without a restart.

## Changes in this version

Rewritten from scratch, with these additions and fixes:

- **Automated tests** for the tiling, dedup, OCR client, LLM filter, auth and web stages.
- **LLM network errors and timeouts are skipped per batch,** like HTTP errors and unparseable replies, so one slow request doesn't fail the whole stage.
- **`/me` is a protected route,** behind the access-token dependency.
- **The auth API returns standard HTTP status codes,** and passwords must be at least 8 characters.
- **No pointless final wait:** the retry loop doesn't wait after the last failed attempt.
- **A web UI** that shows each step on the drawing.
- **The demo's default batch size is 10,** so several batches are visible.

The Excel file has only the `Profile` column. It is a label list, not a bill of materials: lengths and quantities would need member geometry and label-to-member linking, which are not in this repository, and a label count is not a member count either, since one label can point at several members.
