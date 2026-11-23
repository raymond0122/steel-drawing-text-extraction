"""Azure Document Intelligence "read" client with retry and backoff.

The API is asynchronous:
  1. POST the image   -> 202 Accepted + Operation-Location header
  2. GET that URL     -> poll until status is "succeeded" or "failed"

Both steps retry on 429 (rate limited), transient 5xx errors and network
exceptions. A Retry-After header is honoured, up to 60 s; otherwise the wait
doubles each time (1, 2, 4, 8 ... seconds, capped at 30). Any other 4xx fails
at once, while submitting or polling, because retrying won't fix it.
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
from .http import HttpResponse, HttpSession
from .ocr_types import OcrLine
from .settings import Settings
from .tiling import Tile

logger = logging.getLogger(__name__)

TRANSIENT_STATUS = (500, 502, 503, 504)
MAX_BACKOFF_S = 30.0
MAX_RETRY_AFTER_S = 60.0  # a longer Retry-After (a quota window) isn't worth holding a request for


class AzureOcrError(RuntimeError):
    pass


class AzureReadClient:
    def __init__(
        self,
        endpoint: str,
        key: str,
        *,
        model_id: str = "prebuilt-read",
        api_version: str = "2024-11-30",
        max_attempts: int = 5,
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
        return cls(settings.azure_docint_endpoint.strip(), settings.azure_docint_key.strip())

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
                conf = item.get("confidence")
                lines.append(OcrLine(text=text, bbox=bbox, conf=float(conf) if conf is not None else -1.0))
        return lines

    def analyze(self, image_bytes: bytes) -> dict[str, Any]:
        poll_url = self._submit(image_bytes)
        return self._poll(poll_url)

    # -- internals --------------------------------------------------------------

    def _wait_time(self, resp: HttpResponse | None, attempt: int) -> float:
        """The server's Retry-After (in seconds, up to 60) if it sent one, otherwise exponential backoff."""
        retry_after = resp.headers.get("Retry-After", "").strip() if resp is not None else ""
        if retry_after.isdigit():
            return min(MAX_RETRY_AFTER_S, float(retry_after))
        return min(MAX_BACKOFF_S, 2.0**attempt)

    def _send_with_retry(self, method: str, url: str, **kwargs: Any) -> HttpResponse | None:
        """Send one request, retrying 429 / 5xx / network errors.

        Returns the last response (which may still be an error status), or
        None if every attempt raised a network exception.
        """
        resp: HttpResponse | None = None
        for attempt in range(self.max_attempts):
            try:
                resp = self.session.request(method, url, **kwargs)
            except requests.RequestException as e:
                resp = None
                reason = f"network error: {e}"
            else:
                if resp.status_code != 429 and resp.status_code not in TRANSIENT_STATUS:
                    return resp
                reason = f"HTTP {resp.status_code}"

            if attempt == self.max_attempts - 1:
                break
            wait = self._wait_time(resp, attempt)
            logger.warning("%s %s, retry %d/%d in %.1fs", method, reason, attempt + 1, self.max_attempts - 1, wait)
            self.sleep(wait)
        return resp

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
            if resp.status_code == 200:
                try:
                    body = resp.json()
                except ValueError:
                    body = {}
                if not isinstance(body, dict):
                    body = {}
                status = str(body.get("status", "")).lower()
                if status == "succeeded":
                    return body
                if status in ("failed", "canceled"):
                    raise AzureOcrError(f"analysis {status}: {str(body)[:300]}")
            elif resp.status_code == 429 or resp.status_code in TRANSIENT_STATUS:
                logger.warning("GET poll still returned HTTP %d after retries, will keep polling", resp.status_code)
            else:
                # 4xx (bad key, unknown operation...) won't fix itself: fail now, not at the poll timeout.
                raise AzureOcrError(f"GET poll failed: HTTP {resp.status_code} {resp.text[:300]}")

            # Checked after each request too, since its retries can take a while.
            if self.clock() - start + self.poll_interval_s > self.poll_timeout_s:
                raise AzureOcrError(f"polling timed out after {self.poll_timeout_s:.0f}s")
            self.sleep(self.poll_interval_s)
