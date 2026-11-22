"""Local web UI: run the pipeline from the browser and inspect every step.

    uvicorn steel_text.web.server:app --reload      then open http://127.0.0.1:8000

POST /api/run runs the same pipeline as the demo (steel_text/pipeline.py) and
returns every intermediate result as JSON. The page in index.html draws them.
The auth API is mounted under /auth-api so the same page can exercise it.
"""

from __future__ import annotations

import base64
import io
import logging
import time
from collections.abc import Callable, Iterable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

import requests
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from PIL import Image

from ..auth.main import app as auth_app
from ..azure_ocr import AzureOcrError
from ..dedup import DedupConfig
from ..geometry import BBox
from ..http import HttpResponse, HttpSession
from ..label_sheet import XLSX_MIME, write_label_sheet
from ..llm_filter import LabelExtractor
from ..ocr_types import OcrChunk, OcrLine, TileOcrClient
from ..pipeline import MissingCredentialsError, check_truth, make_clients, run_pipeline
from ..sample_drawing import GroundTruthText, make_sample_drawing
from ..settings import ENV_FILE, Settings, is_filled, load_settings
from ..tiling import Tile
from .schemas import (
    Chunk,
    DedupDecisionOut,
    DedupInfo,
    FailedBatch,
    LabelOut,
    LiveStatus,
    LlmInfo,
    MergedChunk,
    PageInfo,
    RunResponse,
    TileTiming,
    Tiling,
    Timings,
)

Image.MAX_IMAGE_PIXELS = None  # large drawings exceed PIL's default safety limit

# Same format as the demo. Prints per-tile progress and timings in the uvicorn terminal.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

PREVIEW_MAX_SIDE = 4000  # the browser gets a downscaled page; close-ups are cropped at full resolution
CLOSEUP_PAD = 120

app = FastAPI(title="mypart demo UI")
app.mount("/auth-api", auth_app)


def live_settings() -> Settings:
    """Read .env again on every request, so keys added while the server runs work without a restart."""
    return load_settings()


SettingsDep = Annotated[Settings, Depends(live_settings)]


@lru_cache(maxsize=1)
def _sample() -> tuple[Image.Image, list[GroundTruthText]]:
    return make_sample_drawing()


def _png_data_url(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _preview(page: Image.Image) -> str:
    scale = min(1.0, PREVIEW_MAX_SIDE / max(page.size))
    if scale < 1.0:
        # BOX (area average) is enough for a preview and about 3x faster than LANCZOS on a 9000 px page
        page = page.resize((int(page.width * scale), int(page.height * scale)), Image.Resampling.BOX)
    return _png_data_url(page)


def _xlsx_data_url(labels: Iterable[str]) -> str:
    """The label sheet as a data URL, so the page can offer it as a download without a second request."""
    buf = io.BytesIO()
    write_label_sheet(labels, buf)
    return f"data:{XLSX_MIME};base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _region(a: BBox, b: BBox, page: Image.Image) -> BBox:
    """Both boxes plus padding, clamped to the page."""
    return (
        max(0, min(a[0], b[0]) - CLOSEUP_PAD),
        max(0, min(a[1], b[1]) - CLOSEUP_PAD),
        min(page.width, max(a[2], b[2]) + CLOSEUP_PAD),
        min(page.height, max(a[3], b[3]) + CLOSEUP_PAD),
    )


def _chunk(c: OcrChunk) -> Chunk:
    return Chunk(text=c.text, bbox=c.bbox, tile=c.tile_index)


class _OcrTimer:
    """Wraps a tile OCR client and records where each tile's time went.

    The Azure client also gets this object as its HTTP session and its sleep
    function, so requests, rate-limited or failed responses, and polling waits
    are counted per tile. The offline simulator only gets the per-tile total.
    """

    def __init__(self, *, http: HttpSession | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self.client: TileOcrClient | None = None
        self.tiles: list[TileTiming] = []
        self._current: TileTiming | None = None
        self._http: HttpSession = http or requests.Session()
        self._sleep = sleep

    def read_tile(self, tile_img: Image.Image, tile: Tile) -> list[OcrLine]:
        if self.client is None:
            raise RuntimeError("_OcrTimer.client is not set")
        self._current = cur = TileTiming()
        start = time.perf_counter()
        try:
            return self.client.read_tile(tile_img, tile)
        finally:
            cur.ms = round((time.perf_counter() - start) * 1000)
            self.tiles.append(cur)
            self._current = None
            logger.info(
                "tile %d: %.1f s (%d requests, %d rate-limited or failed, %.1f s waiting)",
                len(self.tiles) - 1,
                cur.ms / 1000,
                cur.requests,
                cur.errors,
                cur.waiting_ms / 1000,
            )

    def sleep(self, seconds: float) -> None:
        """Sleep function for AzureReadClient: polling intervals and retry backoff."""
        if self._current is not None:
            self._current.waiting_ms += round(seconds * 1000)
        self._sleep(seconds)

    def request(self, method: str, url: str, **kwargs: Any) -> HttpResponse:
        """Session interface for AzureReadClient."""
        cur = self._current
        if cur is not None:
            cur.requests += 1
        try:
            resp = self._http.request(method, url, **kwargs)
        except requests.RequestException:
            if cur is not None:
                cur.errors += 1
            raise
        if cur is not None and (resp.status_code == 429 or resp.status_code >= 500):
            cur.errors += 1
        return resp


class _LlmTimer:
    """Wraps the label extractor and records how long each batch took, failed ones included."""

    def __init__(self, extractor: LabelExtractor) -> None:
        self.extractor = extractor
        self.batches_ms: list[int] = []

    def extract(self, items: Sequence[dict[str, str]]) -> dict[str, Any]:
        start = time.perf_counter()
        try:
            return self.extractor.extract(items)
        finally:
            self.batches_ms.append(round((time.perf_counter() - start) * 1000))
            logger.info("LLM batch %d: %.1f s", len(self.batches_ms) - 1, self.batches_ms[-1] / 1000)


def _clients(
    live: bool,
    settings: Settings,
    simulate_llm_failure: bool,
    truth: list[GroundTruthText] | None,
    timer: _OcrTimer,
) -> tuple[TileOcrClient, LabelExtractor]:
    try:
        # The timer stands in for the Azure client's HTTP session and sleep, to count requests and waits per tile.
        return make_clients(
            live,
            truth,
            settings=settings,
            simulate_llm_failure=simulate_llm_failure,
            azure_kwargs={"session": timer, "sleep": timer.sleep},
        )
    except MissingCredentialsError as e:
        hint = "Fill in the keys in .env." if ENV_FILE.exists() else "Copy .env.example to .env and fill in the keys."
        raise HTTPException(400, f"Live mode needs credentials ({e}). {hint}") from e


def _run(
    image_bytes: bytes,
    live: bool,
    settings: Settings,
    simulate_llm_failure: bool,
    batch_size: int,
    max_tiles: int,
) -> RunResponse:
    truth: list[GroundTruthText] | None
    if image_bytes:
        if not live:
            raise HTTPException(
                400, "Uploading an image needs live mode: the offline simulator only knows the synthetic drawing."
            )
        try:
            page = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        except OSError as e:
            raise HTTPException(400, f"Could not read the image: {e}") from e
        truth = None
    else:
        page, truth = _sample()

    ocr_timer = _OcrTimer()
    ocr_timer.client, extractor = _clients(live, settings, simulate_llm_failure, truth, ocr_timer)
    llm_timer = _LlmTimer(extractor)
    try:
        outcome = run_pipeline(page, ocr_timer, llm_timer, live=live, max_tiles=max_tiles, batch_size=batch_size)
    except AzureOcrError as e:  # a tile still failing after its retries (live mode only)
        raise HTTPException(502, f"OCR failed: {e}") from e
    ocr, result = outcome.ocr, outcome.labels
    logger.info(
        "%s run: OCR %.1f s over %d tiles, LLM %.1f s over %d batches",
        "live" if live else "offline",
        outcome.ocr_seconds,
        len(ocr.tiles),
        outcome.llm_seconds,
        result.batches,
    )

    decisions = []
    for d in ocr.dedup_log:
        region = _region(d.dropped.bbox, d.kept.bbox, page)
        decisions.append(
            DedupDecisionOut(
                dropped=_chunk(d.dropped),
                kept=_chunk(d.kept),
                reason=d.reason,
                region=region,
                crop=_png_data_url(page.crop(region)),
            )
        )

    return RunResponse(
        mode="live" if live else "offline",
        source="upload" if image_bytes else "synthetic",
        page=PageInfo(width=page.width, height=page.height, preview=_preview(page)),
        tiling=Tiling(tile_size=ocr.tile_size, overlap=ocr.overlap, tiles=[t.box for t in ocr.tiles]),
        raw=[_chunk(c) for c in ocr.raw],
        merged=[MergedChunk(id=str(i), **_chunk(c).model_dump()) for i, c in enumerate(ocr.merged)],
        dedup=DedupInfo(config=DedupConfig(), decisions=decisions),
        llm=LlmInfo(
            ok=result.ok,
            batch_size=batch_size,
            batches=result.batches,
            failed=[FailedBatch(batch=b, error=err) for b, err in result.failed_batches],
        ),
        labels=[
            LabelOut(str_id=e.str_id, label=e.normalized_label, kind=e.material_kind, bbox=e.bbox, raw_text=e.raw_text)
            for e in result.entries
        ],
        truth_check=check_truth(truth, result.entries) if truth is not None else None,
        labels_xlsx=_xlsx_data_url(e.normalized_label for e in result.entries),
        timings_ms=Timings(
            ocr=round(outcome.ocr_seconds * 1000),
            llm=round(outcome.llm_seconds * 1000),
            tiles=ocr_timer.tiles,
            llm_batches=llm_timer.batches_ms,
        ),
    )


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    # no-cache: the browser re-checks the page on every load, so edits show up without a hard refresh
    return FileResponse(Path(__file__).with_name("index.html"), headers={"Cache-Control": "no-cache"})


@app.get("/api/live-status")
def live_status(settings: SettingsDep) -> LiveStatus:
    return LiveStatus(
        env_file=ENV_FILE.exists(),
        azure_endpoint=is_filled(settings.azure_docint_endpoint),
        azure_key=is_filled(settings.azure_docint_key),
        openai_key=is_filled(settings.openai_api_key),
        openai_model=settings.openai_model.strip() or "gpt-4o",
    )


@app.post("/api/run")
async def run(
    request: Request,
    settings: SettingsDep,
    live: bool = False,
    simulate_llm_failure: bool = False,
    batch_size: int = Query(10, ge=1, le=200),
    max_tiles: int = Query(20, ge=1, le=100),
) -> RunResponse:
    """Body: optional raw image bytes (live mode only). Without one, the synthetic drawing is used."""
    image_bytes = await request.body()
    # Live calls block for seconds, so keep them off the event loop.
    return await run_in_threadpool(_run, image_bytes, live, settings, simulate_llm_failure, batch_size, max_tiles)
