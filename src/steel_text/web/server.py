"""Local web UI: run the pipeline from the browser and inspect every step.

    uvicorn steel_text.web.server:app --reload      then open http://127.0.0.1:8000

POST /api/run runs the same pipeline as the demo (steel_text/pipeline.py) and
returns every intermediate result as JSON. The page in index.html draws them.
The auth API is mounted under /auth-api: live runs spend API credit, so they
need an access token from it. Offline runs need none.
"""

from __future__ import annotations

import base64
import io
import logging
import time
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials
from PIL import Image

from ..auth.main import app as auth_app
from ..auth.main import bearer, current_user
from ..auth.main import get_settings as auth_settings
from ..dedup import DedupConfig
from ..geometry import BBox, region_around
from ..label_sheet import XLSX_MIME, write_label_sheet
from ..llm_filter import DEFAULT_BATCH_SIZE, LabelExtractor, NotChecked
from ..ocr_types import OcrChunk, OcrLine, TileOcrClient, TileReadError
from ..page_image import PageTooLargeError, open_page
from ..pipeline import MissingCredentialsError, make_clients, run_pipeline
from ..sample_drawing import GroundTruthText, check_truth, make_sample_drawing
from ..settings import ENV_FILE, Settings, is_filled, load_settings
from ..tiling import DEFAULT_MAX_TILES, Tile, TileLimitError
from .schemas import (
    Chunk,
    DedupDecisionOut,
    DedupInfo,
    LabelOut,
    LiveStatus,
    LlmBatch,
    LlmInfo,
    MergedChunk,
    NotRead,
    PageInfo,
    RunResponse,
    Tiling,
    Timings,
)

# Same format as the demo. Prints per-tile progress, retries and timings in the uvicorn terminal.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

PREVIEW_MAX_SIDE = 4000  # the browser gets a downscaled page; close-ups are cropped at full resolution
CLOSEUP_PAD = 120

app = FastAPI(title="Steel text extraction: demo UI")
app.mount("/auth-api", auth_app)


def live_settings() -> Settings:
    """Read .env again on every request, so keys added while the server runs work without a restart."""
    return load_settings()


SettingsDep = Annotated[Settings, Depends(live_settings)]


def require_login_for_live(
    settings: Annotated[Settings, Depends(auth_settings)],
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    live: bool = False,
) -> None:
    """Live runs spend API credit, so they need a logged-in user. Offline runs are open to anyone.

    The token is checked with the auth API's own settings, so it is signed and
    checked with the same key.
    """
    if not live:
        return
    if creds is None:
        raise HTTPException(
            401,
            "Live mode spends API credit, so it needs a login: log in on the Auth API tab first.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    current_user(settings, creds)  # 401 for an expired or invalid token, or a refresh token


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


def _xlsx_data_url(
    labels: Iterable[str], not_read: Sequence[tuple[int, BBox, str]], not_checked: Sequence[NotChecked]
) -> str:
    """The label sheet as a data URL, so the page can offer it as a download without a second request."""
    buf = io.BytesIO()
    write_label_sheet(labels, buf, not_read=not_read, not_checked=not_checked)
    return f"data:{XLSX_MIME};base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _chunk(c: OcrChunk) -> Chunk:
    return Chunk(text=c.text, bbox=c.bbox, tile=c.tile_index)


class _TimedOcr:
    """Wraps the tile OCR client and records how long each tile took, in ms."""

    def __init__(self, client: TileOcrClient) -> None:
        self.client = client
        self.ms: list[int] = []

    def read_tile(self, tile_img: Image.Image, tile: Tile) -> list[OcrLine]:
        start = time.perf_counter()
        try:
            return self.client.read_tile(tile_img, tile)
        finally:
            self.ms.append(_ms_since(start))


class _TimedLabeler:
    """Wraps the LLM label extractor and records how long each batch took, in ms, failed ones included."""

    def __init__(self, extractor: LabelExtractor) -> None:
        self.extractor = extractor
        self.ms: list[int] = []

    def extract(self, items: Sequence[dict[str, str]]) -> dict[str, Any]:
        start = time.perf_counter()
        try:
            return self.extractor.extract(items)
        finally:
            self.ms.append(_ms_since(start))


def _ms_since(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


def _run(
    image_bytes: bytes,
    live: bool,
    settings: Settings,
    simulate_ocr_failure: bool,
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
            page = open_page(io.BytesIO(image_bytes))
        except PageTooLargeError as e:
            raise HTTPException(413, f"Image too large: {e}") from e
        except OSError as e:
            raise HTTPException(400, f"Could not read the image: {e}") from e
        truth = None
    else:
        page, truth = _sample()

    try:
        ocr_client, labeler = make_clients(
            live,
            truth,
            settings=settings,
            simulate_ocr_failure=simulate_ocr_failure,
            simulate_llm_failure=simulate_llm_failure,
        )
    except MissingCredentialsError as e:
        hint = "Fill in the keys in .env." if ENV_FILE.exists() else "Copy .env.example to .env and fill in the keys."
        raise HTTPException(400, f"Live mode needs credentials ({e}). {hint}") from e

    timed_ocr, timed_labeler = _TimedOcr(ocr_client), _TimedLabeler(labeler)
    try:
        outcome = run_pipeline(page, timed_ocr, timed_labeler, live=live, max_tiles=max_tiles, batch_size=batch_size)
    except TileLimitError as e:
        raise HTTPException(400, f"Too few tiles allowed: {e}") from e
    except TileReadError as e:  # no tile could be read at all; a single failed tile is in the result
        raise HTTPException(502, f"OCR failed: {e}") from e
    ocr, result = outcome.ocr, outcome.labels
    logger.info(
        "%s run: OCR %.1f s over %d tiles (%d not read), LLM %.1f s over %d batches (%d skipped)",
        "live" if live else "offline",
        outcome.ocr_seconds,
        len(ocr.tiles),
        len(ocr.failed_tiles),
        outcome.llm_seconds,
        result.batches,
        len(result.failed_batches),
    )

    errors = dict(result.failed_batches)
    decisions = []
    for d in ocr.dedup_log:
        region = region_around(d.dropped.bbox, d.kept.bbox, CLOSEUP_PAD, page.width, page.height)
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
        tiling=Tiling(
            tile_size=ocr.tiling.tile_size,
            overlap=ocr.tiling.overlap,
            reason=ocr.tiling.reason,
            tiles=[t.box for t in ocr.tiles],
            not_read=[NotRead(tile=i, box=box, error=error) for i, box, error in ocr.not_read],
        ),
        raw=[_chunk(c) for c in ocr.raw],
        merged=[MergedChunk(id=str(i), **_chunk(c).model_dump()) for i, c in enumerate(ocr.merged)],
        dedup=DedupInfo(config=DedupConfig(), decisions=decisions),
        llm=LlmInfo(
            ok=result.ok,
            batches=[LlmBatch(ids=ids, error=errors.get(b)) for b, ids in enumerate(result.batch_ids)],
        ),
        labels=[
            LabelOut(str_id=e.str_id, label=e.normalized_label, kind=e.material_kind, bbox=e.bbox, raw_text=e.raw_text)
            for e in result.entries
        ],
        truth_check=check_truth(truth, result.entries) if truth is not None else None,
        labels_xlsx=_xlsx_data_url((e.normalized_label for e in result.entries), ocr.not_read, result.not_checked),
        timings_ms=Timings(
            ocr=round(outcome.ocr_seconds * 1000),
            llm=round(outcome.llm_seconds * 1000),
            tiles=timed_ocr.ms,
            llm_batches=timed_labeler.ms,
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
        openai_model=settings.openai_model,
    )


@app.post("/api/run", dependencies=[Depends(require_login_for_live)])
async def run(
    request: Request,
    settings: SettingsDep,
    live: bool = False,
    simulate_ocr_failure: bool = False,
    simulate_llm_failure: bool = False,
    batch_size: int = Query(DEFAULT_BATCH_SIZE, ge=1, le=200),
    max_tiles: int = Query(DEFAULT_MAX_TILES, ge=1, le=100),
) -> RunResponse:
    """Body: optional raw image bytes (live mode only). Without one, the synthetic drawing is used."""
    image_bytes = await request.body()
    # Live calls block for seconds, so keep them off the event loop.
    return await run_in_threadpool(
        _run,
        image_bytes,
        live,
        settings,
        simulate_ocr_failure=simulate_ocr_failure,
        simulate_llm_failure=simulate_llm_failure,
        batch_size=batch_size,
        max_tiles=max_tiles,
    )
