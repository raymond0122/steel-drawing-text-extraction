"""Pick steel section labels out of OCR text with an LLM, and fix OCR mistakes.

Design points:
  * Structured outputs (strict JSON schema): the reply always parses, with no extra fields.
  * The model only returns an ID and a label. Boxes and raw text are joined back
    from the OCR results by ID, so the model cannot invent a location. IDs that
    were not in that batch, and repeats of an ID already labelled, are dropped.
  * Text is sent in batches. A failed batch is logged and skipped, so one bad
    request does not lose the whole page.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests

from .geometry import BBox
from .http import HttpSession
from .ocr_types import OcrChunk
from .settings import Settings

logger = logging.getLogger(__name__)

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
        model: str = "gpt-4o",
        base_url: str = "https://api.openai.com/v1",
        timeout_s: float = 60.0,
        session: HttpSession | None = None,
    ) -> None:
        if not api_key:
            raise LlmCallError("OPENAI_API_KEY is required")
        self.api_key = api_key
        self.model = model
        self.url = f"{base_url.rstrip('/')}/responses"
        self.timeout_s = timeout_s
        self.session: HttpSession = session or requests.Session()

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> OpenAIResponsesClient:
        return cls(
            settings.openai_api_key.strip(),
            model=settings.openai_model.strip() or "gpt-4o",
            base_url=settings.openai_base_url.strip() or "https://api.openai.com/v1",
            **kwargs,
        )

    def extract(self, items: Sequence[dict[str, str]]) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "input": build_prompt(items),
            "text": {"format": {"type": "json_schema", "name": "label_extract", "schema": SCHEMA, "strict": True}},
        }
        try:
            r = self.session.request(
                "POST",
                self.url,
                json=payload,
                timeout=self.timeout_s,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except requests.RequestException as e:
            raise LlmCallError(f"network error: {e}") from e

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


@dataclass
class LabelEntry:
    str_id: str
    normalized_label: str
    material_kind: str
    bbox: BBox
    raw_text: str


@dataclass
class FilterResult:
    ok: bool
    entries: list[LabelEntry]
    batches: int
    failed_batches: list[tuple[int, str]] = field(default_factory=list)


def normalize_label(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").strip().upper())


def filter_labels(
    chunks: Sequence[OcrChunk],
    extractor: LabelExtractor,
    *,
    batch_size: int = 50,
    request_sleep_s: float = 0.1,
    sleep: Callable[[float], None] = time.sleep,
) -> FilterResult:
    by_id = {str(i): c for i, c in enumerate(chunks)}
    ids = list(by_id)
    entries: list[LabelEntry] = []
    failed: list[tuple[int, str]] = []
    labelled: set[str] = set()
    n_batches = 0

    for b_idx, start in enumerate(range(0, len(ids), batch_size)):
        n_batches += 1
        if b_idx > 0 and request_sleep_s > 0:
            sleep(request_sleep_s)
        batch_ids = ids[start : start + batch_size]
        items = [{"id": sid, "raw_text": by_id[sid].text} for sid in batch_ids]

        try:
            parsed = extractor.extract(items)
        except LlmCallError as e:
            # Skip this batch and keep going: one bad request should not lose the page.
            failed.append((b_idx, str(e)))
            logger.warning("batch %d skipped: %s", b_idx, e)
            continue

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

    ok = not (n_batches > 0 and len(failed) == n_batches)
    logger.info("LLM filter: %d batches, %d failed, %d labels", n_batches, len(failed), len(entries))
    return FilterResult(ok=ok, entries=entries, batches=n_batches, failed_batches=failed)
