"""Local web UI: run the pipeline from the browser and inspect every step.

    uvicorn steel_text.web.server:app --reload      then open http://127.0.0.1:8000

POST /api/run runs the same pipeline as the demo (steel_text/pipeline.py) and
returns every intermediate result as JSON. The page (index.html, with app.js
and style.css) draws them.
The auth API is mounted under /auth-api: live runs spend API credit, so they
need an access token from it. Offline runs need none.
"""

from __future__ import annotations

import base64
import io
import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials
from PIL import Image

from ..auth.main import app as auth_app
from ..auth.main import bearer, current_user
from ..auth.main import get_settings as auth_settings
from ..dedup import DedupDecision
from ..geometry import BBox, region_around
from ..label_sheet import XLSX_MIME, write_label_sheet
from ..llm_filter import MAX_BATCH_SIZE, NotChecked
from ..ocr_types import OcrChunk, TileReadError
from ..page_image import PageTooLargeError, open_page, shrink
from ..pipeline import MissingCredentialsError, PipelineResult, make_clients, run_pipeline
from ..sample_drawing import GroundTruthText, check_truth, make_sample_drawing
from ..settings import ENV_FILE, Settings, is_filled, load_settings
from ..tiling import DEFAULT_MAX_TILES, MAX_OVERLAP, OVERLAP, TILE_LIMIT_CAP, TileLimitError
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

logger = logging.getLogger(__name__)

PREVIEW_MAX_SIDE = 4000  # the browser gets a downscaled page; close-ups are cropped at full resolution
MAX_UPLOAD_BYTES = 100_000_000  # far more than a page needs: the 14,400 x 9,600 px test sheet is 1.5 MB as PNG
CLOSEUP_PAD = 120


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # When the server starts, not on import, so importing this module (the tests do) changes no
    # logging. Same format as the demo: per-tile progress, retries and timings in the uvicorn terminal.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    yield


app = FastAPI(title="Steel text extraction: demo UI", lifespan=lifespan)
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
            "Live mode spends API credit, so it needs a login: use Log in at the top right.",
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


def _xlsx_data_url(
    labels: Iterable[str], not_read: Sequence[tuple[int, BBox, str]], not_checked: Sequence[NotChecked]
) -> str:
    """The label sheet as a data URL, so the page can offer it as a download without a second request."""
    buf = io.BytesIO()
    write_label_sheet(labels, buf, not_read=not_read, not_checked=not_checked)
    return f"data:{XLSX_MIME};base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _chunk(c: OcrChunk) -> Chunk:
    return Chunk(text=c.text, bbox=c.bbox, tile=c.tile_index)


def _run(
    image_bytes: bytes,
    live: bool,
    settings: Settings,
    simulate_ocr_failure: bool,
    simulate_llm_failure: bool,
    batch_size: int | None,
    max_tiles: int,
    overlap: int,
) -> RunResponse:
    """Load the page, pick the clients, run the pipeline, and answer with every step's result.

    Each way a run can fail becomes an HTTP error here; a single failed tile or
    batch is not one of them, since it is part of the result.
    """
    page, truth = _load_page(image_bytes, live)
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

    try:
        outcome = run_pipeline(
            page, ocr_client, labeler, live=live, max_tiles=max_tiles, overlap=overlap, batch_size=batch_size
        )
    except TileLimitError as e:
        raise HTTPException(400, f"Too few tiles allowed: {e}") from e
    except TileReadError as e:  # no tile could be read at all; a single failed tile is in the result
        raise HTTPException(502, f"OCR failed: {e}") from e
    logger.info(
        "%s run: OCR %.1f s over %d tiles (%d not read), LLM %.1f s over %d batches (%d skipped)",
        "live" if live else "offline",
        outcome.ocr_seconds,
        len(outcome.ocr.tiles),
        len(outcome.ocr.failed_tiles),
        outcome.llm_seconds,
        outcome.labels.batches,
        len(outcome.labels.failed_batches),
    )
    return _response(page, truth, outcome, live=live, uploaded=bool(image_bytes))


def _load_page(image_bytes: bytes, live: bool) -> tuple[Image.Image, list[GroundTruthText] | None]:
    """The uploaded page, which needs live mode, with no ground truth; or else the synthetic drawing and its own."""
    if not image_bytes:
        return _sample()
    if not live:
        raise HTTPException(
            400, "Uploading an image needs live mode: the offline simulator only knows the synthetic drawing."
        )
    try:
        return open_page(io.BytesIO(image_bytes)), None
    except PageTooLargeError as e:
        raise HTTPException(413, f"Image too large: {e}") from e
    except OSError as e:
        raise HTTPException(400, f"Could not read the image: {e}") from e


def _response(
    page: Image.Image, truth: list[GroundTruthText] | None, outcome: PipelineResult, *, live: bool, uploaded: bool
) -> RunResponse:
    """Every intermediate result, in the shape app.js reads (see schemas.py)."""
    ocr, filtered = outcome.ocr, outcome.labels
    errors = dict(filtered.failed_batches)
    return RunResponse(
        mode="live" if live else "offline",
        source="upload" if uploaded else "synthetic",
        page=PageInfo(width=page.width, height=page.height, preview=_png_data_url(shrink(page, PREVIEW_MAX_SIDE)[0])),
        tiling=Tiling(
            tile_size=ocr.tiling.tile_size,
            overlap=ocr.tiling.overlap,
            reason=ocr.tiling.reason,
            tiles=[t.box for t in ocr.tiles],
            not_read=[NotRead(tile=i, box=box, error=error) for i, box, error in ocr.not_read],
        ),
        raw=[_chunk(c) for c in ocr.raw],
        merged=[MergedChunk(id=str(i), **_chunk(c).model_dump()) for i, c in enumerate(ocr.merged)],
        dedup=DedupInfo(config=ocr.dedup_config, decisions=[_decision(page, d) for d in ocr.dedup_log]),
        llm=LlmInfo(
            ok=filtered.ok,
            batches=[LlmBatch(ids=ids, error=errors.get(b)) for b, ids in enumerate(filtered.batch_ids)],
        ),
        labels=[
            LabelOut(
                str_id=e.str_id,
                label=e.normalized_label,
                kind=e.material_kind,
                bbox=e.bbox,
                raw_text=e.raw_text,
                corrected=e.corrected,
            )
            for e in filtered.entries
        ],
        truth_check=check_truth(truth, filtered.entries) if truth is not None else None,
        labels_xlsx=_xlsx_data_url((e.normalized_label for e in filtered.entries), ocr.not_read, filtered.not_checked),
        timings_ms=Timings(
            ocr=round(outcome.ocr_seconds * 1000),
            llm=round(outcome.llm_seconds * 1000),
            tiles=ocr.tile_ms,
            llm_batches=filtered.batch_ms,
        ),
    )


def _decision(page: Image.Image, d: DedupDecision) -> DedupDecisionOut:
    """A dedup decision, with a full-resolution crop of the area around both readings."""
    region = region_around(d.dropped.bbox, d.kept.bbox, CLOSEUP_PAD, page.width, page.height)
    return DedupDecisionOut(
        dropped=_chunk(d.dropped),
        kept=_chunk(d.kept),
        reason=d.reason,
        region=region,
        crop=_png_data_url(page.crop(region)),
    )


WEB_DIR = Path(__file__).parent
NO_CACHE = {"Cache-Control": "no-cache"}  # the browser re-checks on every load, so edits show without a hard refresh
# Named here, so no other file can be asked for, and with their types: on Windows, Python can
# guess text/plain for .js from the registry, and a browser refuses a stylesheet that isn't text/css.
STATIC_FILES = {"app.js": "text/javascript", "style.css": "text/css"}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html", headers=NO_CACHE)


@app.get("/static/{name}", include_in_schema=False)
def static_file(name: str) -> FileResponse:
    if name not in STATIC_FILES:
        raise HTTPException(404, "Not Found")
    return FileResponse(WEB_DIR / name, media_type=STATIC_FILES[name], headers=NO_CACHE)


@app.get("/api/live-status")
def live_status(settings: SettingsDep) -> LiveStatus:
    return LiveStatus(
        env_file=ENV_FILE.exists(),
        azure_endpoint=is_filled(settings.azure_docint_endpoint),
        azure_key=is_filled(settings.azure_docint_key),
        openai_key=is_filled(settings.openai_api_key),
        openai_model=settings.openai_model,
    )


async def _read_upload(request: Request) -> bytes:
    """The request body, refused with 413 as soon as it passes MAX_UPLOAD_BYTES, before the rest arrives."""
    parts: list[bytes] = []
    size = 0
    async for part in request.stream():
        size += len(part)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"Upload too large: over {MAX_UPLOAD_BYTES // 1_000_000} MB")
        parts.append(part)
    return b"".join(parts)


@app.post("/api/run", dependencies=[Depends(require_login_for_live)])
async def run(
    request: Request,
    settings: SettingsDep,
    live: bool = False,
    simulate_ocr_failure: bool = False,
    simulate_llm_failure: bool = False,
    batch_size: int | None = Query(None, ge=1, le=MAX_BATCH_SIZE),  # None: the pipeline's default for the mode
    max_tiles: int = Query(DEFAULT_MAX_TILES, ge=1, le=TILE_LIMIT_CAP),
    overlap: int = Query(OVERLAP, ge=0, le=MAX_OVERLAP),
) -> RunResponse:
    """Body: optional raw image bytes (live mode only). Without one, the synthetic drawing is used."""
    image_bytes = await _read_upload(request)
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
        overlap=overlap,
    )
