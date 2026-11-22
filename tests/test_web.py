import pytest
from fastapi.testclient import TestClient

from steel_text.settings import Settings, load_settings
from steel_text.web import server
from steel_text.web.server import app


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def test_offline_run_returns_every_step(client):
    r = client.post("/api/run")
    assert r.status_code == 200
    d = r.json()
    assert len(d["tiling"]["tiles"]) == 6
    assert len(d["raw"]) - len(d["merged"]) == len(d["dedup"]["decisions"])  # each decision drops one line
    assert all(x["crop"].startswith("data:image/png;base64,") for x in d["dedup"]["decisions"])
    assert d["truth_check"]["missing"] == [] and d["truth_check"]["unexpected"] == []
    merged_ids = {c["id"] for c in d["merged"]}
    assert all(label["str_id"] in merged_ids for label in d["labels"])
    assert len(d["timings_ms"]["tiles"]) == len(d["tiling"]["tiles"])
    assert len(d["timings_ms"]["llm_batches"]) == d["llm"]["batches"]


def test_excel_download_lists_the_labels_found(client):
    import base64
    import io

    from openpyxl import load_workbook

    d = client.post("/api/run").json()
    header, b64 = d["labels_xlsx"].split(",", 1)
    assert header.startswith("data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    rows = [r[0] for r in load_workbook(io.BytesIO(base64.b64decode(b64))).worksheets[0].iter_rows(values_only=True)]
    assert rows == ["Profile"] + sorted(label["label"] for label in d["labels"])


def test_simulated_llm_failure_is_reported_per_batch(client):
    d = client.post("/api/run", params={"simulate_llm_failure": True}).json()
    assert d["llm"]["ok"]
    assert [f["batch"] for f in d["llm"]["failed"]] == [1]


class FakeResponse:
    def __init__(self, status, headers=None, body=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class ScriptedHttp:
    def __init__(self, responses):
        self.responses = list(responses)

    def request(self, method, url, **kwargs):
        return self.responses.pop(0)


def test_ocr_timer_records_requests_errors_and_waits_per_tile():
    from PIL import Image

    from steel_text.azure_ocr import AzureReadClient
    from steel_text.tiling import Tile

    timer = server._OcrTimer(
        sleep=lambda s: None,
        http=ScriptedHttp(
            [
                FakeResponse(429, {"Retry-After": "3"}),  # POST rate-limited
                FakeResponse(202, {"Operation-Location": "https://example.test/op/1"}),
                FakeResponse(200, body={"status": "running"}),  # first poll
                FakeResponse(200, body={"status": "succeeded", "analyzeResult": {"pages": []}}),
            ]
        ),
    )
    timer.client = AzureReadClient("https://example.test", "key", session=timer, sleep=timer.sleep)
    timer.read_tile(Image.new("RGB", (10, 10), "white"), Tile(0, 0, 10, 10))

    [t] = timer.tiles
    assert (t.requests, t.errors, t.waiting_ms) == (4, 1, 5000)  # Retry-After 3 s + one 2 s poll


def test_image_upload_needs_live_mode(client):
    assert client.post("/api/run", content=b"not an image").status_code == 400


def test_keys_added_to_env_file_are_picked_up_without_a_restart(client, tmp_path, monkeypatch):
    env = tmp_path / ".env"
    monkeypatch.setitem(app.dependency_overrides, server.live_settings, lambda: load_settings(env))
    for name in ("AZURE_DOCINT_ENDPOINT", "AZURE_DOCINT_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)  # only the file under test may supply them

    env.write_text("AZURE_DOCINT_ENDPOINT=https://<your-resource>.cognitiveservices.azure.com\nAZURE_DOCINT_KEY=\n")
    assert not any(client.get("/api/live-status").json()[k] for k in ("azure_endpoint", "azure_key", "openai_key"))

    env.write_text("AZURE_DOCINT_ENDPOINT=https://real.example.test\nAZURE_DOCINT_KEY=secret-azure-key\n")
    r = client.get("/api/live-status")
    assert r.json()["azure_endpoint"] and r.json()["azure_key"] and not r.json()["openai_key"]
    assert "secret-azure-key" not in r.text  # names only, never values


def test_page_and_auth_api_are_served(client):
    page = client.get("/")
    assert "<title>Steel Text Extraction</title>" in page.text
    assert page.headers["cache-control"] == "no-cache"
    assert client.get("/auth-api/me").status_code == 401


def test_live_mode_without_credentials_is_a_400(client, monkeypatch):
    for name in ("AZURE_DOCINT_ENDPOINT", "AZURE_DOCINT_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(app.dependency_overrides, server.live_settings, lambda: Settings(_env_file=None))
    r = client.post("/api/run", params={"live": True})
    assert r.status_code == 400 and "Live mode needs credentials" in r.json()["detail"]


def test_ocr_failure_after_retries_is_a_502(client, monkeypatch):
    from steel_text.azure_ocr import AzureOcrError
    from steel_text.mock_clients import RuleBasedLabeler

    class FailingOcr:
        def read_tile(self, tile_img, tile):
            raise AzureOcrError("POST failed: HTTP 503")

    monkeypatch.setattr(server, "make_clients", lambda *args, **kwargs: (FailingOcr(), RuleBasedLabeler()))
    r = client.post("/api/run")
    assert r.status_code == 502 and "OCR failed" in r.json()["detail"]
