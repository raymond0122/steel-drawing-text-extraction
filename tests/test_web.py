import pytest
from fastapi.testclient import TestClient

from steel_text.auth.main import app as auth_app
from steel_text.auth.main import get_settings as auth_settings
from steel_text.auth.security import create_token
from steel_text.settings import Settings, load_settings
from steel_text.web import server
from steel_text.web.server import app

# Live runs check tokens against the auth API's settings: a test key here, never the real .env.
AUTH_SETTINGS = Settings(_env_file=None, jwt_secret_key="test-secret-at-least-32-bytes-long")


@pytest.fixture(scope="module")
def client():
    for a in (app, auth_app):
        a.dependency_overrides[auth_settings] = lambda: AUTH_SETTINGS
    yield TestClient(app)
    for a in (app, auth_app):
        a.dependency_overrides.pop(auth_settings, None)


def bearer(kind="access"):
    return {"Authorization": f"Bearer {create_token('tester', kind, AUTH_SETTINGS.jwt_secret_key)}"}


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
    assert len(d["timings_ms"]["llm_batches"]) == len(d["llm"]["batches"])
    assert [i for b in d["llm"]["batches"] for i in b["ids"]] == [c["id"] for c in d["merged"]]  # each line once
    assert "3,500 px tiles" in d["tiling"]["reason"]


def test_excel_download_lists_the_labels_found(client):
    import base64
    import io

    from openpyxl import load_workbook

    d = client.post("/api/run").json()
    header, b64 = d["labels_xlsx"].split(",", 1)
    assert header.startswith("data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    rows = [r[0] for r in load_workbook(io.BytesIO(base64.b64decode(b64))).worksheets[0].iter_rows(values_only=True)]
    assert rows == ["Profile"] + sorted(label["label"] for label in d["labels"])


def test_a_tile_that_fails_is_returned_as_not_read(client):
    import base64
    import io

    from openpyxl import load_workbook

    d = client.post("/api/run", params={"simulate_ocr_failure": True}).json()
    assert [f["tile"] for f in d["tiling"]["not_read"]] == [1]
    assert d["tiling"]["not_read"][0]["box"] == d["tiling"]["tiles"][1]
    assert "L102X76X7.9" in d["truth_check"]["missing"]  # printed where only tile 1 reads it
    excel = load_workbook(io.BytesIO(base64.b64decode(d["labels_xlsx"].split(",", 1)[1])))
    assert excel.sheetnames == ["Labels (incomplete)", "Not read"]


def test_simulated_llm_failure_is_reported_per_batch_and_in_the_excel_file(client):
    import base64
    import io

    from openpyxl import load_workbook

    d = client.post("/api/run", params={"simulate_llm_failure": True}).json()
    assert d["llm"]["ok"]
    assert [b for b, batch in enumerate(d["llm"]["batches"]) if batch["error"]] == [1]
    excel = load_workbook(io.BytesIO(base64.b64decode(d["labels_xlsx"].split(",", 1)[1])))
    assert excel.sheetnames == ["Labels (incomplete)", "Not checked"]
    not_checked = [row[0] for row in excel["Not checked"].iter_rows(min_row=2, values_only=True)]
    assert not_checked == d["llm"]["batches"][1]["ids"]


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


@pytest.mark.parametrize("name, media_type", [("app.js", "text/javascript"), ("style.css", "text/css")])
def test_the_page_script_and_styles_are_served_with_their_types(client, name, media_type):
    assert f'"/static/{name}"' in client.get("/").text
    r = client.get(f"/static/{name}")
    assert r.status_code == 200 and r.headers["content-type"].startswith(media_type)
    assert r.headers["cache-control"] == "no-cache"


def test_the_values_the_page_shows_and_recommends_are_the_servers_defaults(client):
    from steel_text.llm_filter import DEFAULT_BATCH_SIZE
    from steel_text.pipeline import OFFLINE_BATCH_SIZE
    from steel_text.tiling import DEFAULT_MAX_TILES, OVERLAP

    script = client.get("/static/app.js").text
    assert f"const BATCH_SIZE_LIVE = '{DEFAULT_BATCH_SIZE}', BATCH_SIZE_OFFLINE = '{OFFLINE_BATCH_SIZE}';" in script
    page = client.get("/").text
    assert f'id="max-tiles" value="{DEFAULT_MAX_TILES}"' in page and f"Recommended: {DEFAULT_MAX_TILES}<" in page
    assert f'id="overlap" value="{OVERLAP}"' in page and f"Recommended: {OVERLAP}<" in page


def test_the_overlap_chosen_on_the_page_is_used(client):
    d = client.post("/api/run", params={"overlap": 500}).json()
    assert d["tiling"]["overlap"] == 500 and "overlap by 500 px" in d["tiling"]["reason"]
    assert client.post("/api/run", params={"overlap": 5000}).status_code == 422  # over the 1,000 px cap


@pytest.mark.parametrize("name", ["index.html", "server.py", "..%2Fserver.py"])
def test_no_other_file_is_served(client, name):
    assert client.get(f"/static/{name}").status_code == 404


def test_live_run_needs_a_login(client):
    r = client.post("/api/run", params={"live": True})
    assert r.status_code == 401 and "needs a login" in r.json()["detail"]


def test_live_run_does_not_take_a_refresh_token(client):
    r = client.post("/api/run", params={"live": True}, headers=bearer("refresh"))
    assert r.status_code == 401 and "Wrong token type" in r.json()["detail"]


def test_live_mode_without_credentials_is_a_400(client, monkeypatch):
    for name in ("AZURE_DOCINT_ENDPOINT", "AZURE_DOCINT_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(app.dependency_overrides, server.live_settings, lambda: Settings(_env_file=None))
    r = client.post("/api/run", params={"live": True}, headers=bearer())
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
