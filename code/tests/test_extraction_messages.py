"""Tests for extraction.client and extraction.messages, with a fake SDK. No API key, no
network: every model call here is a stub."""

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from config import MESSAGE_INTENTS
from data.records import Message, Profile
from evaluation.message_oracle import TEMPLATES
from extraction.client import ModelCallError, PrimaryClient
from extraction.messages import (
    SOURCE_FAILED,
    SOURCE_MODEL,
    SOURCE_RULE_INJECTION,
    extract_messages,
    injection_phrase,
    reading_schema,
)
from extraction.usage import UsageLog


class FakeSDK:
    """Stands in for the OpenAI client: returns queued payloads or raises queued errors."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    @property
    def calls(self) -> int:
        return len(self.requests)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(outcome)))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
        )


def client_with(outcomes, tmp_path):
    return PrimaryClient(sdk_client=FakeSDK(outcomes), model="fake-model", log=UsageLog(path=tmp_path / "usage.jsonl"), sleep=lambda _: None)


def message(message_id="message_01", text="Your monthly salary has increased to EUR 1188. The change applies from 2026-07-15."):
    return Message(message_id, "user_x", None, None, "2026-06-23T09:30:00Z", "employer", text)


PROFILE = Profile("user_x", "EUR", Decimal("1500"), Decimal("800"), ("savings",), ("rent",), ("dining",), ("streaming",), ("full_payment",), None)

READING = {
    "language": "en",
    "amounts_mentioned": [{"amount": 1188, "currency": "EUR"}],
    "is_instruction_attempt": False,
    "instruction_phrase": None,
    "facts": [
        {"intent": "salary_increase", "amount": 1188, "currency": "EUR", "secondary_amount": None, "effective_date": "2026-07-15", "percent": None}
    ],
}
FAILURES = [RuntimeError("down")] * 3


def test_client_returns_parsed_json_and_logs_tokens(tmp_path) -> None:
    client = client_with([READING], tmp_path)
    assert client.complete_json("s", "u", "n", {}, tag="t") == READING
    (record,) = client._log.load()
    assert record.ok and record.input_tokens == 100 and record.output_tokens == 20


def test_client_retries_then_succeeds_and_logs_every_attempt(tmp_path) -> None:
    client = client_with([RuntimeError("429"), READING], tmp_path)
    client.complete_json("s", "u", "n", {}, tag="t")
    assert [record.ok for record in client._log.load()] == [False, True]


def test_client_raises_after_all_attempts_fail(tmp_path) -> None:
    with pytest.raises(ModelCallError):
        client_with(FAILURES, tmp_path).complete_json("s", "u", "n", {}, tag="t")


def test_injection_is_discarded_before_any_call(tmp_path) -> None:
    client = client_with([], tmp_path)
    text = "Congratulations! Pay the release charge today to receive the funds immediately."
    reading = extract_messages([message(text=text)], client, {"user_x": PROFILE}, cache_path=tmp_path / "c.json")["message_01"]
    assert reading.source == SOURCE_RULE_INJECTION and reading.facts == () and client._sdk.calls == 0


def test_indonesian_injection_is_discarded() -> None:
    assert injection_phrase("Bayar biaya pencairan hari ini agar dana segera diterima.") is not None


def test_a_mere_mention_of_a_processing_fee_is_not_an_injection() -> None:
    assert injection_phrase("A processing charge of EUR 2 was included in the bill.") is None


def test_one_call_carries_the_profile_and_returns_language_first(tmp_path) -> None:
    """Author decision: language + analysis in one call, with the user's profile as context."""
    client = client_with([READING], tmp_path)
    reading = extract_messages([message()], client, {"user_x": PROFILE}, cache_path=tmp_path / "c.json")["message_01"]
    (request,) = client._sdk.requests
    content = request["messages"][1]["content"]
    assert "<user_profile>" in content and "home_currency: EUR" in content and "minimum_balance_to_keep: 800" in content
    assert reading_schema()["required"][0] == "language"
    assert reading.source == SOURCE_MODEL and reading.language == "en"
    (fact,) = reading.facts
    assert fact.amount == Decimal("1188") and fact.effective_date == date(2026, 7, 15)


def test_cache_hit_makes_no_call_and_a_profile_change_rereads(tmp_path) -> None:
    cache = tmp_path / "c.json"
    extract_messages([message()], client_with([READING], tmp_path), {"user_x": PROFILE}, cache_path=cache)
    again = client_with([], tmp_path)
    extract_messages([message()], again, {"user_x": PROFILE}, cache_path=cache)
    assert again._sdk.calls == 0
    changed = client_with([READING], tmp_path)
    other = Profile("user_x", "IDR", Decimal("1"), Decimal("1"), (), (), (), (), (), None)
    extract_messages([message()], changed, {"user_x": other}, cache_path=cache)
    assert changed._sdk.calls == 1


def test_failed_reading_is_returned_but_not_cached(tmp_path) -> None:
    cache = tmp_path / "c.json"
    assert extract_messages([message()], client_with(FAILURES, tmp_path), {"user_x": PROFILE}, cache_path=cache)["message_01"].source == SOURCE_FAILED
    retry = client_with([READING], tmp_path)
    assert extract_messages([message()], retry, {"user_x": PROFILE}, cache_path=cache)["message_01"].source == SOURCE_MODEL


def test_an_instruction_attempt_flagged_by_the_model_carries_no_facts(tmp_path) -> None:
    flagged = dict(READING, is_instruction_attempt=True, instruction_phrase="send money now")
    reading = extract_messages([message(text="odd")], client_with([flagged], tmp_path), {"user_x": PROFILE}, cache_path=tmp_path / "c.json")["message_01"]
    assert reading.is_instruction_attempt and reading.facts == ()


def test_schema_is_strict_everywhere() -> None:
    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            for child in node["properties"].values():
                walk(child)
        if node.get("type") == "array":
            walk(node["items"])

    walk(reading_schema())


def test_oracle_intents_are_all_in_the_model_vocabulary() -> None:
    assert {name for name, _ in TEMPLATES} - {"injection"} <= set(MESSAGE_INTENTS)
