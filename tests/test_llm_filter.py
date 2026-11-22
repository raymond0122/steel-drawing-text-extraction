import json
from typing import Any

import pytest
import requests

from steel_text.llm_filter import LlmCallError, OpenAIResponsesClient, filter_labels
from steel_text.mock_clients import RuleBasedLabeler, match_section
from steel_text.ocr_types import OcrChunk

CHUNKS = [
    OcrChunk("CS0", (0, 0, 100, 50)),
    OcrChunk("SEE NOTE 6", (200, 0, 500, 50)),
    OcrChunk("W31OX39", (0, 100, 200, 150)),
    OcrChunk("1200", (300, 100, 400, 150)),
    OcrChunk("WT155*79", (0, 200, 220, 250)),
]


class ScriptedExtractor:
    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def extract(self, items):
        self.seen.append([i["id"] for i in items])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_labels_are_joined_back_to_ocr_boxes_by_id():
    ex = ScriptedExtractor(
        [
            {
                "entries": [
                    {"str_id": "0", "normalized_label": "c 50", "material_kind": "C"},
                    {"str_id": "99", "normalized_label": "W999X1", "material_kind": "H"},  # unknown ID: dropped
                    {"str_id": "4", "normalized_label": "WT155*79", "material_kind": "Weird"},  # unknown kind -> Other
                ]
            }
        ]
    )
    res = filter_labels(CHUNKS, ex, batch_size=50, sleep=lambda s: None)
    assert res.ok
    assert [(e.normalized_label, e.material_kind, e.bbox, e.raw_text) for e in res.entries] == [
        ("C50", "C", (0, 0, 100, 50), "CS0"),
        ("WT155*79", "Other", (0, 200, 220, 250), "WT155*79"),
    ]


def test_failed_batch_is_skipped_and_the_rest_still_processed():
    ex = ScriptedExtractor(
        [
            {"entries": [{"str_id": "0", "normalized_label": "C50", "material_kind": "C"}]},
            LlmCallError("HTTP 500"),
            {"entries": [{"str_id": "4", "normalized_label": "WT155*79", "material_kind": "Other"}]},
        ]
    )
    res = filter_labels(CHUNKS, ex, batch_size=2, sleep=lambda s: None)
    assert ex.seen == [["0", "1"], ["2", "3"], ["4"]]
    assert res.ok and res.batches == 3
    assert res.failed_batches == [(1, "HTTP 500")]
    assert [e.normalized_label for e in res.entries] == ["C50", "WT155*79"]


def test_ids_from_another_batch_and_repeated_ids_are_dropped():
    ex = ScriptedExtractor(
        [
            {"entries": []},
            {
                "entries": [
                    {"str_id": "0", "normalized_label": "C50", "material_kind": "C"},  # sent in batch 0, not this one
                    {"str_id": "2", "normalized_label": "W310X39", "material_kind": "H"},
                    {"str_id": "2", "normalized_label": "W310X39", "material_kind": "H"},  # repeat
                ]
            },
            {"entries": []},
        ]
    )
    res = filter_labels(CHUNKS, ex, batch_size=2, sleep=lambda s: None)
    assert [(e.str_id, e.normalized_label) for e in res.entries] == [("2", "W310X39")]


def test_all_batches_failing_marks_the_page_failed():
    ex = ScriptedExtractor([LlmCallError("HTTP 500")] * 3)
    res = filter_labels(CHUNKS, ex, batch_size=2, sleep=lambda s: None)
    assert not res.ok and len(res.failed_batches) == 3


class FakeSession:
    def __init__(self, outcome):
        self.outcome = outcome
        self.payload: dict[str, Any] = {}

    def request(self, method, url, **kwargs):
        assert method == "POST"
        self.payload = kwargs["json"]
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self.headers = {"x-request-id": "req_1"}
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


def responses_body(obj):
    return {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(obj)}]}]}


def test_openai_client_sends_strict_schema_and_parses_reply():
    fake = FakeSession(FakeResponse(200, responses_body({"entries": []})))
    client = OpenAIResponsesClient("sk-test", session=fake)
    assert client.extract([{"id": "0", "raw_text": "CS0"}]) == {"entries": []}
    fmt = fake.payload["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True
    assert '"raw_text": "CS0"' in fake.payload["input"]


@pytest.mark.parametrize(
    "outcome, message",
    [
        (requests.Timeout("read timed out"), "network error"),
        (FakeResponse(429, {"error": "rate limited"}), "HTTP 429"),
        (FakeResponse(200, {"output": []}), "unparseable"),
    ],
)
def test_openai_client_turns_every_failure_into_llm_call_error(outcome, message):
    client = OpenAIResponsesClient("sk-test", session=FakeSession(outcome))
    with pytest.raises(LlmCallError, match=message):
        client.extract([{"id": "0", "raw_text": "CS0"}])


def test_mock_labeler_fixes_common_ocr_confusions():
    assert match_section("CS0") == {"normalized_label": "C50", "material_kind": "C"}
    assert match_section("W31OX39") == {"normalized_label": "W310X39", "material_kind": "H"}
    assert match_section("WT155*79") == {"normalized_label": "WT155*79", "material_kind": "Other"}
    assert match_section("SEE NOTE 6") is None
    assert match_section("SCALE 1:50") is None


def test_mock_labeler_can_simulate_a_failed_call():
    labeler = RuleBasedLabeler(fail_calls={0})
    with pytest.raises(LlmCallError):
        labeler.extract([{"id": "0", "raw_text": "C50"}])
    assert labeler.extract([{"id": "0", "raw_text": "C50"}])["entries"][0]["normalized_label"] == "C50"
