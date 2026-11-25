import json
from typing import Any

import pytest
import requests

from steel_text.llm_filter import (
    LabelEntry,
    LlmCallError,
    NotChecked,
    OpenAIResponsesClient,
    build_prompt,
    filter_labels,
)
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
    res = filter_labels(CHUNKS, ex, batch_size=50)
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
    res = filter_labels(CHUNKS, ex, batch_size=2)
    assert ex.seen == res.batch_ids == [["0", "1"], ["2", "3"], ["4"]]
    assert res.ok and res.batches == 3 and len(res.batch_ms) == 3  # the failed batch is timed too
    assert res.failed_batches == [(1, "HTTP 500")]
    assert [e.normalized_label for e in res.entries] == ["C50", "WT155*79"]
    # the lines of the skipped batch are kept, so the outputs can show they may hold labels
    assert res.not_checked == [
        NotChecked("2", "W31OX39", (0, 100, 200, 150), "HTTP 500"),
        NotChecked("3", "1200", (300, 100, 400, 150), "HTTP 500"),
    ]


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
    res = filter_labels(CHUNKS, ex, batch_size=2)
    assert [(e.str_id, e.normalized_label) for e in res.entries] == [("2", "W310X39")]


def test_a_batch_size_below_one_is_refused():
    with pytest.raises(ValueError, match="batch_size must be at least 1"):
        filter_labels(CHUNKS, ScriptedExtractor([]), batch_size=0)


def test_all_batches_failing_marks_the_page_failed():
    ex = ScriptedExtractor([LlmCallError("HTTP 500")] * 3)
    res = filter_labels(CHUNKS, ex, batch_size=2)
    assert not res.ok and len(res.failed_batches) == 3


class FakeSession:
    """Plays back the scripted outcomes in order: a response to return, or an exception to raise."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.payloads: list[dict[str, Any]] = []

    def request(self, method, url, **kwargs):
        assert method == "POST"
        self.payloads.append(kwargs["json"])
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self.headers = {"x-request-id": "req_1", **(headers or {})}
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


def responses_body(obj):
    return {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(obj)}]}]}


def make_openai_client(session, sleeps):
    return OpenAIResponsesClient("sk-test", session=session, sleep=sleeps.append)


ITEMS = [{"id": "0", "raw_text": "CS0"}]
REPLY = FakeResponse(200, responses_body({"entries": []}))


def test_openai_client_sends_strict_schema_and_parses_reply():
    fake = FakeSession(REPLY)
    assert make_openai_client(fake, []).extract(ITEMS) == {"entries": []}
    fmt = fake.payloads[0]["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True
    assert '"raw_text": "CS0"' in fake.payloads[0]["input"]


def test_openai_client_retries_a_rate_limit_and_honours_retry_after():
    sleeps: list[float] = []
    rate_limited = FakeResponse(429, {"error": {"code": "rate_limit_exceeded"}}, {"Retry-After": "3"})
    fake = FakeSession(rate_limited, FakeResponse(503, {"error": "overloaded"}), REPLY)
    assert make_openai_client(fake, sleeps).extract(ITEMS) == {"entries": []}
    assert sleeps == [3.0, 2.0]  # Retry-After, then the backoff after the second attempt
    assert len(fake.payloads) == 3


@pytest.mark.parametrize(
    "outcome, attempts, message",
    [
        (requests.Timeout("read timed out"), 5, "network error on all 5 attempts"),
        (FakeResponse(429, {"error": {"code": "rate_limit_exceeded"}}), 5, "HTTP 429"),
        (FakeResponse(503, {"error": "overloaded"}), 5, "HTTP 503"),
        (FakeResponse(429, {"error": {"code": "insufficient_quota"}}), 1, "HTTP 429"),  # waiting won't refill it
        (  # out of credit: same type, another code, as OpenAI sent on 2026-11-24
            FakeResponse(429, {"error": {"code": "credit_balance_exhausted", "type": "insufficient_quota"}}),
            1,
            "HTTP 429",
        ),
        (FakeResponse(401, {"error": {"code": "invalid_api_key"}}), 1, "HTTP 401"),
        (FakeResponse(200, {"output": [{"content": [{"type": "output_text", "text": "not json"}]}]}), 1, "unparseable"),
    ],
)
def test_openai_client_turns_every_failure_into_llm_call_error(outcome, attempts, message):
    sleeps: list[float] = []
    fake = FakeSession(*[outcome] * attempts)  # one more request than scripted fails the test
    with pytest.raises(LlmCallError, match=message):
        make_openai_client(fake, sleeps).extract(ITEMS)
    assert len(fake.payloads) == attempts
    assert sleeps == [1.0, 2.0, 4.0, 8.0][: attempts - 1]


@pytest.mark.parametrize(
    "label, raw, corrected",
    [
        ("C50", "CS0", True),  # a misread fixed
        ("W310X39", "W31OX39", True),
        ("W24X55", 'W24x55 (36) c=11/4"', False),  # only the section taken out of the line
        ("WT155*79", "WT155*79", False),
        ("W21X44", "W21×44 (26)", True),  # the multiplication sign written as X
    ],
)
def test_a_label_is_corrected_only_when_the_model_changed_what_ocr_read(label, raw, corrected):
    assert LabelEntry("0", label, "H", (0, 0, 1, 1), raw).corrected is corrected


def test_a_reply_with_no_text_is_asked_for_once_more():
    empty = FakeResponse(200, {"output": []})
    fake = FakeSession(empty, REPLY)
    assert make_openai_client(fake, []).extract(ITEMS) == {"entries": []}
    assert len(fake.payloads) == 2
    with pytest.raises(LlmCallError, match="no text in the reply, 2 times"):
        make_openai_client(FakeSession(empty, empty), []).extract(ITEMS)  # a third request would fail the test


def test_the_prompt_asks_for_the_section_alone():
    prompt = build_prompt([{"id": "0", "raw_text": "W10x12 (6)"}])
    assert "the section designation only" in prompt and "'W10x12 (6)' -> 'W10X12'" in prompt


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
