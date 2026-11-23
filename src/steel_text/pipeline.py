"""The whole text-extraction flow: pick the clients, run tiled OCR, then the LLM label filter.

The demo and the web UI both go through here, so the two cannot drift apart.
Loading the page and presenting the results stay with each caller.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from PIL import Image

from .azure_ocr import AzureOcrError, AzureReadClient
from .llm_filter import FilterResult, LabelExtractor, LlmCallError, OpenAIResponsesClient, filter_labels
from .ocr_stage import TiledOcrResult, run_tiled_ocr
from .ocr_types import TileOcrClient
from .sample_drawing import GroundTruthText
from .settings import Settings, load_settings

# Pause between requests in live mode, to stay under the services' rate limits.
LIVE_OCR_REQUEST_SLEEP_S = 0.2
LIVE_LLM_REQUEST_SLEEP_S = 0.1


class MissingCredentialsError(RuntimeError):
    """Live mode was requested, but the Azure or OpenAI settings are not in the environment."""


def make_clients(
    live: bool,
    truth: Sequence[GroundTruthText] | None = None,
    *,
    settings: Settings | None = None,
    simulate_llm_failure: bool = False,
) -> tuple[TileOcrClient, LabelExtractor]:
    """Real Azure and OpenAI clients in live mode, offline stand-ins otherwise.

    Live mode takes its keys from `settings` (read from the environment and .env
    when not given). Offline mode only works on the synthetic drawing, because
    the simulated OCR reads the text positions from its ground truth.
    """
    if live:
        settings = settings if settings is not None else load_settings()
        try:
            ocr: TileOcrClient = AzureReadClient.from_settings(settings)
            return ocr, OpenAIResponsesClient.from_settings(settings)
        except (AzureOcrError, LlmCallError) as e:
            raise MissingCredentialsError(str(e)) from e
    if truth is None:
        raise ValueError("offline mode needs the synthetic drawing's ground truth")
    from .mock_clients import RuleBasedLabeler, SimulatedOcr  # demo-only stand-ins

    ocr = SimulatedOcr(truth)
    return ocr, RuleBasedLabeler(fail_calls={1} if simulate_llm_failure else None)


@dataclass
class PipelineResult:
    ocr: TiledOcrResult
    labels: FilterResult
    ocr_seconds: float
    llm_seconds: float


def run_pipeline(
    page: Image.Image,
    ocr_client: TileOcrClient,
    labeler: LabelExtractor,
    *,
    live: bool,
    max_tiles: int = 20,
    batch_size: int = 10,
    sleep: Callable[[float], None] = time.sleep,
) -> PipelineResult:
    """Tiled OCR and dedup, then the LLM label filter.

    Requests are paced only in live mode; the offline stand-ins have no rate limit.
    An OCR tile that still fails after its retries raises AzureOcrError. A failed
    LLM batch does not raise: it is recorded in the result and skipped.
    """
    t0 = time.perf_counter()
    ocr = run_tiled_ocr(
        page,
        ocr_client,
        max_tiles=max_tiles,
        request_sleep_s=LIVE_OCR_REQUEST_SLEEP_S if live else 0,
        sleep=sleep,
    )
    t1 = time.perf_counter()
    labels = filter_labels(
        ocr.merged,
        labeler,
        batch_size=batch_size,
        request_sleep_s=LIVE_LLM_REQUEST_SLEEP_S if live else 0,
        sleep=sleep,
    )
    t2 = time.perf_counter()
    return PipelineResult(ocr=ocr, labels=labels, ocr_seconds=t1 - t0, llm_seconds=t2 - t1)
