"""The shared flow that the demo and the web UI both run."""

import pytest

from steel_text.llm_filter import LabelEntry
from steel_text.mock_clients import RuleBasedLabeler, SimulatedOcr
from steel_text.pipeline import (
    LIVE_LLM_REQUEST_SLEEP_S,
    LIVE_OCR_REQUEST_SLEEP_S,
    MissingCredentialsError,
    check_truth,
    make_clients,
    run_pipeline,
)
from steel_text.sample_drawing import GroundTruthText, make_sample_drawing
from steel_text.settings import Settings

BOX = (0, 0, 1, 1)


@pytest.fixture(scope="module")
def drawing():
    return make_sample_drawing()


def test_offline_mode_uses_the_stand_ins(drawing):
    _, truth = drawing
    ocr_client, labeler = make_clients(False, truth, simulate_llm_failure=True)
    assert isinstance(ocr_client, SimulatedOcr) and isinstance(labeler, RuleBasedLabeler)
    assert labeler.fail_calls == {1}


def test_offline_mode_needs_the_ground_truth():
    with pytest.raises(ValueError, match="ground truth"):
        make_clients(False, None)


@pytest.fixture
def no_keys(monkeypatch):
    for name in ("AZURE_DOCINT_ENDPOINT", "AZURE_DOCINT_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_missing_azure_settings_raise_missing_credentials(no_keys):
    with pytest.raises(MissingCredentialsError, match="Azure endpoint and key"):
        make_clients(True, settings=Settings(_env_file=None))


def test_missing_openai_key_raises_missing_credentials(no_keys):
    settings = Settings(_env_file=None, azure_docint_endpoint="https://example.test", azure_docint_key="key")
    with pytest.raises(MissingCredentialsError, match="OPENAI_API_KEY"):
        make_clients(True, settings=settings)


def test_offline_run_finds_every_expected_label_without_pausing(drawing):
    page, truth = drawing
    sleeps: list[float] = []
    outcome = run_pipeline(page, *make_clients(False, truth), live=False, sleep=sleeps.append)
    check = check_truth(truth, outcome.labels.entries)
    assert sleeps == []
    assert check.missing == [] and check.unexpected == [] and check.found == len(check.expected) > 0


def test_live_mode_pauses_between_requests(drawing):
    page, truth = drawing
    sleeps: list[float] = []
    # Offline stand-ins with live pacing: only the pauses are under test here.
    outcome = run_pipeline(page, *make_clients(False, truth), live=True, batch_size=10, sleep=sleeps.append)
    n_tiles, n_batches = len(outcome.ocr.tiles), outcome.labels.batches
    assert n_tiles > 1 and n_batches > 1
    assert sleeps == [LIVE_OCR_REQUEST_SLEEP_S] * (n_tiles - 1) + [LIVE_LLM_REQUEST_SLEEP_S] * (n_batches - 1)


def test_truth_check_reports_missing_and_unexpected_labels():
    truth = [
        GroundTruthText("C50", "C50", BOX, "C50"),
        GroundTruthText("W310X39", "W310X39", BOX, "W310X39"),
        GroundTruthText("TYP.", "TYP.", BOX, None),  # not a label
    ]
    entries = [
        LabelEntry("0", "C50", "C", BOX, "CS0"),
        LabelEntry("1", "L64X64X6.4", "L", BOX, "L64X64X6.4"),
    ]
    check = check_truth(truth, entries)
    assert check.expected == ["C50", "W310X39"]
    assert check.missing == ["W310X39"] and check.unexpected == ["L64X64X6.4"] and check.found == 1
