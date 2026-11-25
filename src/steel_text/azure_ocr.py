"""Azure Document Intelligence "read" client with retry and backoff.

The API is asynchronous:
  1. POST the image   -> 202 Accepted + Operation-Location header
  2. GET that URL     -> poll until status is "succeeded" or "failed"

Both steps retry on 429 (rate limited), transient 5xx errors and network
exceptions, with the rules in steel_text/http.py. Any other 4xx fails at once,
while submitting or polling, because retrying won't fix it.
"""

from __future__ import annotations

import io
import logging
import time
from collections.abc import Callable
from typing import Any

import requests
from PIL import Image

from .geometry import poly_to_bbox
from .http import DEFAULT_MAX_ATTEMPTS, HttpResponse, HttpSession, is_retry_status, send_with_retry
from .ocr_types import OcrLine, TileReadError
from .settings import Settings
from .tiling import Tile

logger = logging.getLogger(__name__)


class AzureOcrError(TileReadError):
    pass


class AzureReadClient:
    def __init__(
        self,
        endpoint: str,
        key: str,
        *,
        model_id: str = "prebuilt-read",
        api_version: str = "2024-11-30",
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        post_timeout_s: float = 60.0,
        get_timeout_s: float = 30.0,
        poll_interval_s: float = 2.0,
        poll_timeout_s: float = 300.0,
        session: HttpSession | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not endpoint or not key:
            raise AzureOcrError("Azure endpoint and key are required")
        self.url = (
            f"{endpoint.rstrip('/')}/documentintelligence/documentModels/{model_id}:analyze?api-version={api_version}"
        )
        self.key = key
        self.max_attempts = max_attempts
        self.post_timeout_s = post_timeout_s
        self.get_timeout_s = get_timeout_s
        self.poll_interval_s = poll_interval_s
        self.poll_timeout_s = poll_timeout_s
        self.session: HttpSession = session or requests.Session()
        self.sleep = sleep
        self.clock = clock

    @classmethod
    def from_settings(cls, settings: Settings) -> AzureReadClient:
        return cls(settings.azure_docint_endpoint, settings.azure_docint_key)

    # -- public ---------------------------------------------------------------

    def read_tile(self, tile_img: Image.Image, tile: Tile) -> list[OcrLine]:
        buf = io.BytesIO()
        tile_img.save(buf, format="PNG")
        result = self.analyze(buf.getvalue())

        lines: list[OcrLine] = []
        for page in result.get("analyzeResult", {}).get("pages", []):
            for item in page.get("lines", []):
                text = (item.get("content") or "").strip()
                bbox = poly_to_bbox(item.get("polygon", []))
                if not text or bbox is None:
                    continue
                lines.append(OcrLine(text=text, bbox=bbox))  # lines carry no confidence score, only words do
        return lines

    def analyze(self, image_bytes: bytes) -> dict[str, Any]:
        poll_url = self._submit(image_bytes)
        return self._poll(poll_url)

    # -- internals --------------------------------------------------------------

    def _send_with_retry(self, method: str, url: str, **kwargs: Any) -> HttpResponse | None:
        return send_with_retry(self.session, method, url, max_attempts=self.max_attempts, sleep=self.sleep, **kwargs)

    def _submit(self, image_bytes: bytes) -> str:
        resp = self._send_with_retry(
            "POST",
            self.url,
            headers={"Ocp-Apim-Subscription-Key": self.key, "Content-Type": "application/octet-stream"},
            data=image_bytes,
            timeout=self.post_timeout_s,
        )
        if resp is None:
            raise AzureOcrError(f"POST failed after {self.max_attempts} attempts (network errors)")
        if resp.status_code != 202:
            # 4xx (bad key, bad image...) fails immediately: retrying will not help.
            raise AzureOcrError(f"POST failed: HTTP {resp.status_code} {resp.text[:300]}")
        poll_url = resp.headers.get("Operation-Location")
        if not poll_url:
            raise AzureOcrError("202 response without Operation-Location header")
        return poll_url

    def _poll(self, poll_url: str) -> dict[str, Any]:
        start = self.clock()
        while True:
            resp = self._send_with_retry(
                "GET",
                poll_url,
                headers={"Ocp-Apim-Subscription-Key": self.key},
                timeout=self.get_timeout_s,
            )
            if resp is None:
                raise AzureOcrError(f"GET poll failed after {self.max_attempts} attempts (network errors)")
            result = _poll_result(resp)
            if result is not None:
                return result
            # Checked after each request too, since its retries can take a while.
            if self.clock() - start + self.poll_interval_s > self.poll_timeout_s:
                raise AzureOcrError(f"polling timed out after {self.poll_timeout_s:.0f}s")
            self.sleep(self.poll_interval_s)


def _poll_result(resp: HttpResponse) -> dict[str, Any] | None:
    """The analysis result once it succeeded, or None while it is still running.

    A reply that polling again won't turn into a result fails now, not at the poll
    timeout: a 4xx (bad key, unknown operation...), a failed or cancelled analysis,
    or a body that isn't a JSON object with a known status. A 429 or 5xx that
    outlasted the retries is polled again.
    """
    if is_retry_status(resp):
        logger.warning("GET poll still returned HTTP %d after retries, will keep polling", resp.status_code)
        return None
    if resp.status_code != 200:
        raise AzureOcrError(f"GET poll failed: HTTP {resp.status_code} {resp.text[:300]}")
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise AzureOcrError(f"unexpected poll reply: {resp.text[:300]}")
    status = str(body.get("status", "")).lower()
    if status == "succeeded":
        return body
    if status in ("notstarted", "running"):
        return None
    if status in ("failed", "canceled"):
        raise AzureOcrError(f"analysis {status}: {str(body)[:300]}")
    raise AzureOcrError(f"unexpected poll reply: {resp.text[:300]}")
