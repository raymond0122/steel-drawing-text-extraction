"""The slice of an HTTP client that the Azure and OpenAI clients use, and the retry they share.

requests.Session satisfies HttpSession. So do the scripted fakes in the tests,
which is how the clients are tested without a network.

Both clients retry 429 (rate limited), transient 5xx errors and network
exceptions. A Retry-After header is honoured, up to 60 s; otherwise the wait
doubles each time (1, 2, 4, 8 ... seconds, capped at 30). Anything else is
returned at once, because retrying won't fix it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, Protocol

import requests

logger = logging.getLogger(__name__)

RETRY_STATUS = (429, 500, 502, 503, 504)  # rate limited, or a server error that may pass
DEFAULT_MAX_ATTEMPTS = 5
MAX_BACKOFF_S = 30.0
MAX_RETRY_AFTER_S = 60.0  # a longer Retry-After (a quota window) isn't worth holding a request for


class HttpResponse(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    @property
    def text(self) -> str: ...

    def json(self) -> Any: ...


class HttpSession(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        data: bytes | None = None,
        json: Any = None,
        timeout: float | None = None,
    ) -> HttpResponse: ...


def is_retry_status(resp: HttpResponse) -> bool:
    return resp.status_code in RETRY_STATUS


def retry_wait(resp: HttpResponse | None, attempt: int) -> float:
    """The server's Retry-After (in seconds, up to 60) if it sent one, otherwise exponential backoff."""
    retry_after = resp.headers.get("Retry-After", "").strip() if resp is not None else ""
    if retry_after.isdigit():
        return min(MAX_RETRY_AFTER_S, float(retry_after))
    return min(MAX_BACKOFF_S, 2.0**attempt)


def send_with_retry(
    session: HttpSession,
    method: str,
    url: str,
    *,
    max_attempts: int,
    sleep: Callable[[float], None],
    should_retry: Callable[[HttpResponse], bool] = is_retry_status,
    headers: Mapping[str, str] | None = None,
    data: bytes | None = None,
    json: Any = None,
    timeout: float | None = None,
) -> HttpResponse | None:
    """Send one request, retrying network errors and the responses `should_retry` picks.

    Returns the last response, which may still be an error status, or None if
    every attempt raised a network exception. `headers` to `timeout` go to session.request.
    """
    resp: HttpResponse | None = None
    for attempt in range(max_attempts):
        try:
            resp = session.request(method, url, headers=headers, data=data, json=json, timeout=timeout)
        except requests.RequestException as e:
            resp = None
            reason = f"network error: {e}"
        else:
            if not should_retry(resp):
                return resp
            reason = f"HTTP {resp.status_code}"

        if attempt == max_attempts - 1:
            logger.warning("%s %s, giving up after %d attempts", method, reason, max_attempts)
            break
        wait = retry_wait(resp, attempt)
        logger.warning("%s %s, retry %d/%d in %.1fs", method, reason, attempt + 1, max_attempts - 1, wait)
        sleep(wait)
    return resp
