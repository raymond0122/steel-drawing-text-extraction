"""Pick steel section labels out of OCR text with an LLM, and fix OCR mistakes.

Design points:
  * Structured outputs (strict JSON schema): the reply always parses, with no extra fields.
  * The model only returns an ID and a label. Boxes and raw text are joined back
    from the OCR results by ID, so the model cannot invent a location. IDs that
    were not in that batch, and repeats of an ID already labelled, are dropped.
  * Requests are retried like Azure's (steel_text/http.py), except a 429 for a
    used-up quota, which lasts until the billing changes.
  * Text is sent in batches. A batch that still fails is logged and skipped, so
    one bad request does not lose the whole page. Its lines are kept as "not
    checked", so every output can show that they may hold labels.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, NamedTuple, Protocol

import requests

from .geometry import BBox
from .http import DEFAULT_MAX_ATTEMPTS, HttpResponse, HttpSession, is_retry_status, send_with_retry
from .ocr_types import OcrChunk
from .settings import DEFAULT_OPENAI_BASE_URL, DEFAULT_OPENAI_MODEL, Settings

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 10  # OCR lines per request
MATERIAL_KINDS: tuple[str, ...] = ("HSS", "H", "L", "C", "Other")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "str_id": {"type": "string"},
                    "normalized_label": {"type": "string"},
                    "material_kind": {"type": "string", "enum": list(MATERIAL_KINDS)},
                },
                "required": ["str_id", "normalized_label", "material_kind"],
            },
        }
    },
    "required": ["entries"],
}


def build_prompt(items: Sequence[dict[str, str]]) -> str:
    kinds = ", ".join(MATERIAL_KINDS)
    return (
        "You are extracting and classifying steel/material component labels from OCR text.\n"
        "Return ONLY JSON that matches the schema.\n\n"
        f"Available material kinds: {kinds}\n\n"
        "Task:\n"
        "- Decide whether each OCR line contains a steel component size/shape label.\n"
        "- If YES, output an entry with:\n"
        "  - str_id: copy from input 'id'\n"
        "  - normalized_label: fix OCR mistakes, uppercase, remove spaces "
        "(e.g., 'CS0' -> 'C50', 'C 50' -> 'C50'); keep '*' as is\n"
        f"  - material_kind: one of {kinds} (use 'Other' if it is a valid steel label "
        "but not in the main categories)\n"
        "- If NO (dimensions, notes, grid marks, title block text), output nothing for that line.\n\n"
        "OCR_INPUT:\n" + json.dumps(list(items), ensure_ascii=False, indent=2)
    )


class LlmCallError(RuntimeError):
    pass


class LabelExtractor(Protocol):
    """Takes [{"id", "raw_text"}, ...] and returns {"entries": [...]} matching SCHEMA."""

    def extract(self, items: Sequence[dict[str, str]]) -> dict[str, Any]: ...


class OpenAIResponsesClient:
    """Calls the OpenAI Responses API with a strict JSON schema."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_OPENAI_MODEL,
        base_url: str = DEFAULT_OPENAI_BASE_URL,
        timeout_s: float = 60.0,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        session: HttpSession | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise LlmCallError("OPENAI_API_KEY is required")
        self.api_key = api_key
        self.model = model
        self.url = f"{base_url.rstrip('/')}/responses"
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts
        self.session: HttpSession = session or requests.Session()
        self.sleep = sleep

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenAIResponsesClient:
        return cls(settings.openai_api_key, model=settings.openai_model, base_url=settings.openai_base_url)

    def extract(self, items: Sequence[dict[str, str]]) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "input": build_prompt(items),
            "text": {"format": {"type": "json_schema", "name": "label_extract", "schema": SCHEMA, "strict": True}},
        }
        r = send_with_retry(
            self.session,
            "POST",
            self.url,
            max_attempts=self.max_attempts,
            sleep=self.sleep,
            should_retry=_worth_retrying,
            json=payload,
            timeout=self.timeout_s,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        if r is None:
            raise LlmCallError(f"network error on all {self.max_attempts} attempts")

        req_id = r.headers.get("x-request-id")
        if r.status_code >= 400:
            raise LlmCallError(f"HTTP {r.status_code} (request_id={req_id}): {r.text[:300]}")
        try:
            data = r.json()
            texts = [
                c["text"]
                for out in data.get("output", [])
                for c in (out.get("content") or [])
                if c.get("type") == "output_text"
            ]
            parsed = json.loads(texts[0])
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
            raise LlmCallError(f"unparseable response (request_id={req_id}): {e}") from e
        if not isinstance(parsed, dict):
            raise LlmCallError(f"unparseable response (request_id={req_id}): not a JSON object")
        return parsed


def _worth_retrying(resp: HttpResponse) -> bool:
    """429 and 5xx, except the 429 for a used-up quota: it lasts until the billing changes."""
    if resp.status_code == 429 and _error_code(resp) == "insufficient_quota":
        return False
    return is_retry_status(resp)


def _error_code(resp: HttpResponse) -> object:
    """`error.code` from an OpenAI error body, or None."""
    try:
        body = resp.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    return error.get("code") if isinstance(error, dict) else None


@dataclass
class LabelEntry:
    str_id: str
    normalized_label: str
    material_kind: str
    bbox: BBox
    raw_text: str


class NotChecked(NamedTuple):
    """An OCR line the LLM never saw, because its batch failed. It may hold a label."""

    str_id: str
    text: str
    bbox: BBox
    error: str


@dataclass
class FilterResult:
    ok: bool
    entries: list[LabelEntry]
    batch_ids: list[list[str]]  # the line IDs sent in each batch, in order
    failed_batches: list[tuple[int, str]] = field(default_factory=list)  # (batch index, error)
    not_checked: list[NotChecked] = field(default_factory=list)  # the lines of the failed batches
    batch_ms: list[int] = field(default_factory=list)  # how long each batch took, failed batches included

    @property
    def batches(self) -> int:
        return len(self.batch_ids)


def normalize_label(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").strip().upper())


def filter_labels(
    chunks: Sequence[OcrChunk],
    extractor: LabelExtractor,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    request_sleep_s: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> FilterResult:
    """Send the OCR lines to the extractor in batches and return the steel labels it found.

    Each line gets an ID ("0", "1", ...) that the model must copy back; boxes and
    raw text are joined back by that ID. The lines of a failed batch are listed in
    `not_checked`. The result is not ok only if every batch failed.
    `request_sleep_s` is the pause between batches.
    """
    by_id = {str(i): c for i, c in enumerate(chunks)}
    ids = list(by_id)
    batches = [ids[start : start + batch_size] for start in range(0, len(ids), batch_size)]
    entries: list[LabelEntry] = []
    failed: list[tuple[int, str]] = []
    not_checked: list[NotChecked] = []
    batch_ms: list[int] = []
    labelled: set[str] = set()

    for b_idx, batch_ids in enumerate(batches):
        if b_idx > 0 and request_sleep_s > 0:
            sleep(request_sleep_s)
        items = [{"id": sid, "raw_text": by_id[sid].text} for sid in batch_ids]

        start = time.perf_counter()
        try:
            parsed = extractor.extract(items)
        except LlmCallError as e:
            # Skip this batch and keep going: one bad request should not lose the page.
            failed.append((b_idx, str(e)))
            not_checked.extend(NotChecked(sid, by_id[sid].text, by_id[sid].bbox, str(e)) for sid in batch_ids)
            logger.warning("batch %d skipped, its %d lines not checked: %s", b_idx, len(batch_ids), e)
            continue
        finally:
            batch_ms.append(round((time.perf_counter() - start) * 1000))

        in_batch = set(batch_ids)
        dropped = 0
        for item in parsed.get("entries", []) if isinstance(parsed, dict) else []:
            sid = str(item.get("str_id", "")).strip()
            label = normalize_label(str(item.get("normalized_label", "")))
            if sid not in in_batch or sid in labelled or not label:
                dropped += 1  # an ID this batch never sent (invented, or from another batch), a repeat, or no label
                continue
            labelled.add(sid)
            chunk = by_id[sid]
            kind = str(item.get("material_kind", "")).strip()
            entries.append(
                LabelEntry(
                    str_id=sid,
                    normalized_label=label,
                    material_kind=kind if kind in MATERIAL_KINDS else "Other",
                    bbox=chunk.bbox,
                    raw_text=chunk.text,
                )
            )
        if dropped:
            logger.warning("batch %d: dropped %d entries (unknown or repeated ID, or empty label)", b_idx, dropped)

    ok = not batches or len(failed) < len(batches)
    logger.info("LLM filter: %d batches, %d failed, %d labels", len(batches), len(failed), len(entries))
    return FilterResult(
        ok=ok,
        entries=entries,
        batch_ids=batches,
        failed_batches=failed,
        not_checked=not_checked,
        batch_ms=batch_ms,
    )
