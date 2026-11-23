import pytest
import requests

from steel_text.azure_ocr import AzureOcrError, AzureReadClient

SUCCEEDED = {
    "status": "succeeded",
    "analyzeResult": {
        "pages": [
            {
                "lines": [
                    {"content": "W310X39", "polygon": [10, 20, 110, 20, 110, 60, 10, 60]},
                    {"content": "", "polygon": [0, 0, 1, 0, 1, 1, 0, 1]},
                ]
            }
        ]
    },
}


class FakeResponse:
    def __init__(self, status, headers=None, body=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class FakeSession:
    """Plays back a scripted list of responses (or exceptions) per HTTP method."""

    def __init__(self, post, get=()):
        self.script = {"POST": list(post), "GET": list(get)}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append(method)
        item = self.script[method].pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(session, sleeps):
    return AzureReadClient("https://example.test", "key", session=session, sleep=sleeps.append)


ACCEPTED = FakeResponse(202, {"Operation-Location": "https://example.test/op/1"})


def test_429_honours_retry_after_then_succeeds():
    sleeps: list[float] = []
    session = FakeSession(
        post=[FakeResponse(429, {"Retry-After": "3"}), ACCEPTED],
        get=[FakeResponse(200, body={"status": "running"}), FakeResponse(200, body=SUCCEEDED)],
    )
    result = make_client(session, sleeps).analyze(b"png")
    assert result["status"] == "succeeded"
    assert sleeps == [3.0, 2.0]  # Retry-After, then one poll interval


def test_long_retry_after_is_capped():
    sleeps: list[float] = []
    session = FakeSession(
        post=[FakeResponse(429, {"Retry-After": "3600"}), ACCEPTED], get=[FakeResponse(200, body=SUCCEEDED)]
    )
    make_client(session, sleeps).analyze(b"png")
    assert sleeps == [60.0]


def test_retry_after_is_honoured_on_503_too():
    sleeps: list[float] = []
    session = FakeSession(
        post=[FakeResponse(503, {"Retry-After": "5"}), ACCEPTED], get=[FakeResponse(200, body=SUCCEEDED)]
    )
    make_client(session, sleeps).analyze(b"png")
    assert sleeps == [5.0]


def test_transient_errors_back_off_exponentially_and_give_up():
    sleeps: list[float] = []
    session = FakeSession(post=[FakeResponse(503)] * 5)
    with pytest.raises(AzureOcrError, match="HTTP 503"):
        make_client(session, sleeps).analyze(b"png")
    assert sleeps == [1.0, 2.0, 4.0, 8.0]  # waits between the 5 attempts
    assert session.calls == ["POST"] * 5


def test_network_errors_are_retried():
    sleeps: list[float] = []
    session = FakeSession(
        post=[requests.ConnectionError("reset"), requests.Timeout("slow"), ACCEPTED],
        get=[FakeResponse(200, body=SUCCEEDED)],
    )
    assert make_client(session, sleeps).analyze(b"png")["status"] == "succeeded"
    assert sleeps == [1.0, 2.0]


def test_client_error_fails_immediately_without_retry():
    sleeps: list[float] = []
    session = FakeSession(post=[FakeResponse(401, body="bad key")])
    with pytest.raises(AzureOcrError, match="HTTP 401"):
        make_client(session, sleeps).analyze(b"png")
    assert sleeps == [] and session.calls == ["POST"]


def test_failed_analysis_raises():
    session = FakeSession(post=[ACCEPTED], get=[FakeResponse(200, body={"status": "failed"})])
    with pytest.raises(AzureOcrError, match="failed"):
        make_client(session, []).analyze(b"png")


def test_polling_times_out():
    now = [0.0]

    def fake_sleep(s):
        now[0] += s

    session = FakeSession(post=[ACCEPTED], get=[FakeResponse(200, body={"status": "running"})] * 200)
    client = AzureReadClient(
        "https://example.test", "key", session=session, sleep=fake_sleep, clock=lambda: now[0], poll_timeout_s=10
    )
    with pytest.raises(AzureOcrError, match="timed out"):
        client.analyze(b"png")


def test_client_error_while_polling_fails_at_once():
    sleeps: list[float] = []
    session = FakeSession(post=[ACCEPTED], get=[FakeResponse(404, body="operation not found")])
    with pytest.raises(AzureOcrError, match="HTTP 404"):
        make_client(session, sleeps).analyze(b"png")
    assert session.calls == ["POST", "GET"] and sleeps == []  # no 5-minute wait for the poll timeout


def test_network_errors_on_every_poll_attempt_fail_the_tile():
    sleeps: list[float] = []
    session = FakeSession(post=[ACCEPTED], get=[requests.ConnectionError("reset")] * 5)
    with pytest.raises(AzureOcrError, match="network errors"):
        make_client(session, sleeps).analyze(b"png")
    assert sleeps == [1.0, 2.0, 4.0, 8.0]


def test_read_tile_parses_lines_and_skips_empty_text():
    from PIL import Image

    from steel_text.tiling import Tile

    session = FakeSession(post=[ACCEPTED], get=[FakeResponse(200, body=SUCCEEDED)])
    lines = make_client(session, []).read_tile(Image.new("RGB", (200, 100), "white"), Tile(0, 0, 200, 100))
    assert [(line.text, line.bbox, line.conf) for line in lines] == [("W310X39", (10, 20, 110, 60), -1.0)]
