"""Response bodies of the web UI's API. index.html reads exactly these fields."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from ..dedup import DedupConfig
from ..geometry import BBox
from ..sample_drawing import TruthCheck


class PageInfo(BaseModel):
    width: int
    height: int
    preview: str  # PNG data URL, downscaled for the browser


class NotRead(BaseModel):
    """A tile OCR could not read: any text in its area is missing from every step."""

    tile: int
    box: BBox
    error: str


class Tiling(BaseModel):
    tile_size: int
    overlap: int
    reason: str  # why this tile size, in one sentence
    tiles: list[BBox]
    not_read: list[NotRead]


class Chunk(BaseModel):
    text: str
    bbox: BBox
    tile: int


class MergedChunk(Chunk):
    id: str  # matches the LLM's str_id


class DedupDecisionOut(BaseModel):
    dropped: Chunk
    kept: Chunk
    reason: str
    region: BBox
    crop: str  # PNG data URL of the region, full resolution


class DedupInfo(BaseModel):
    config: DedupConfig
    decisions: list[DedupDecisionOut]


class LlmBatch(BaseModel):
    ids: list[str]  # the merged lines sent in this batch
    error: str | None  # why it failed and was skipped; None if it succeeded


class LlmInfo(BaseModel):
    ok: bool
    batches: list[LlmBatch]


class LabelOut(BaseModel):
    str_id: str
    label: str
    kind: str
    bbox: BBox
    raw_text: str


class Timings(BaseModel):
    """Milliseconds. Retries and rate limiting show up in the server's log, not here."""

    ocr: int
    llm: int
    tiles: list[int]  # one entry per tile, in tile order
    llm_batches: list[int]  # one entry per batch, in batch order


class RunResponse(BaseModel):
    mode: Literal["live", "offline"]
    source: Literal["upload", "synthetic"]
    page: PageInfo
    tiling: Tiling
    raw: list[Chunk]
    merged: list[MergedChunk]
    dedup: DedupInfo
    llm: LlmInfo
    labels: list[LabelOut]
    truth_check: TruthCheck | None  # only for the synthetic drawing
    labels_xlsx: str  # the label sheet as a data URL
    timings_ms: Timings


class LiveStatus(BaseModel):
    """Which live-mode settings are filled in. Never the values themselves."""

    env_file: bool
    azure_endpoint: bool
    azure_key: bool
    openai_key: bool
    openai_model: str
