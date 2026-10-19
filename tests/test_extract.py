"""ClaudeExtractor against a fake client: request shape, conversion, and failure routing."""

from datetime import date, datetime
from types import SimpleNamespace

import pytest

from sdra.intake.extract import ClaudeExtractor, ExtractionError, LLMClaim, LLMExtraction
from sdra.schema import InboundMessage

MSG = InboundMessage(id="MSG-1", channel="email", sender="sales@cmp-a.example",
                     received_at=datetime.fromisoformat("2026-11-03T09:12:00+03:00"),
                     subject="PO-4471", body="Only 300 of 500 on PO-4471 on 10 November; 200 follow on 20 December.")


class FakeMessages:
    def __init__(self, response):
        self.response, self.calls = response, []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def fake_client(parsed, stop_reason="end_turn"):
    messages = FakeMessages(SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason, _request_id="req_test"))
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def claim(**kw):
    base = dict(kind="partial_quantity", po_reference="PO-4471", item_reference=None, stated_qty=300,
                stated_date=None, stated_unit_price=None, stated_currency=None, substitute_reference=None,
                confidence=0.9, evidence_quotes=["Only 300 of 500 on PO-4471"])
    return LLMClaim(**(base | kw))


def test_request_is_quarantined_and_output_converted():
    parsed = LLMExtraction(language="en", contains_instructions_to_agent=False, claims=[
        claim(), claim(kind="delay", stated_qty=200, stated_date="2026-12-20", evidence_quotes=["200 follow on 20 December"]),
    ])
    client, messages = fake_client(parsed)
    result = ClaudeExtractor(client=client).extract(MSG)
    (call,) = messages.calls
    assert "tools" not in call and "tool_choice" not in call  # no way to act
    assert call["model"] == "claude-opus-5-5" and call["output_format"] is LLMExtraction
    assert call["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in call["betas"]
    assert "<message>" in call["messages"][0]["content"] and MSG.body in call["messages"][0]["content"]
    assert [c.kind for c in result.claims] == ["partial_quantity", "delay"]
    assert result.claims[1].stated_date == date(2026, 12, 20)
    assert result.claims[0].evidence[0].message_id == "MSG-1"


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_incomplete_or_declined_output_goes_to_a_person(stop_reason):
    client, _ = fake_client(None, stop_reason=stop_reason)
    with pytest.raises(ExtractionError):
        ClaudeExtractor(client=client).extract(MSG)


def test_malformed_values_raise_extraction_error():
    parsed = LLMExtraction(language="en", contains_instructions_to_agent=False,
                           claims=[claim(kind="delay", stated_date="20 December")])
    client, _ = fake_client(parsed)
    with pytest.raises(ExtractionError):
        ClaudeExtractor(client=client).extract(MSG)
