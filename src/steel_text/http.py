"""The slice of an HTTP client that the Azure and OpenAI clients use.

requests.Session satisfies it. So do the scripted fakes in the tests and the
web UI's per-tile request counter, which is how those run without a network.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


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
