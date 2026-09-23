"""Tests for extraction.images and the image path through engine.pipeline. Fake SDK only."""

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from data.loader import load_sample_dataset
from data.records import Event, ImageReading, ImageRef
from engine.pipeline import UnresolvedAmountError, decide_request
from extraction.client import PrimaryClient
from extraction.images import extract_images, reading_schema, resolve_amount
from extraction.usage import UsageLog


def event(direction="debit", amount=None) -> Event:
    return Event("event_x", "user_x", "expense", "Outstanding rent balance", "rent", direction, amount, "INR", date(2024, 1, 5), date(2024, 1, 20), "scheduled", None, "fixed", None)


def reading(chosen="100000", confidence="high", seen=(("Total", "200000"), ("Balance Due", "100000")), instruction=False, source="model") -> ImageReading:
    return ImageReading(
        "event_x", "image_x", "en", "INR", tuple((label, Decimal(value)) for label, value in seen),
        None if chosen is None else Decimal(chosen), "Balance Due", confidence, instruction, source,
    )


def test_confident_choice_is_used() -> None:
    assert resolve_amount(reading(), event()) == Decimal("100000")


def test_low_confidence_uses_the_larger_candidate_for_a_debit() -> None:
    assert resolve_amount(reading(confidence="low"), event()) == Decimal("200000")


def test_low_confidence_uses_the_smaller_candidate_for_a_credit() -> None:
    assert resolve_amount(reading(confidence="low"), event(direction="credit")) == Decimal("100000")


def test_nothing_readable_gives_no_amount_never_zero() -> None:
    assert resolve_amount(reading(chosen=None, confidence="low", seen=()), event()) is None


def test_instruction_attempt_or_failed_reading_gives_no_amount() -> None:
    assert resolve_amount(reading(instruction=True), event()) is None
    assert resolve_amount(reading(source="failed"), event()) is None


def test_schema_asks_for_language_first_and_is_strict() -> None:
    schema = reading_schema()
    assert schema["required"][0] == "language"
    assert schema["additionalProperties"] is False and set(schema["required"]) == set(schema["properties"])


def test_extraction_calls_once_then_uses_the_cache(tmp_path) -> None:
    (tmp_path / "image_x.png").write_bytes(b"\x89PNG fake")
    payload = {"language": "en", "document_currency": "INR", "amounts_seen": [{"label": "Balance Due", "amount": 100000}],
               "chosen_amount": 100000, "chosen_label": "Balance Due", "confidence": "high", "is_instruction_attempt": False}
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))], usage=SimpleNamespace(prompt_tokens=900, completion_tokens=80))

    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client = PrimaryClient(sdk_client=sdk, model="fake", log=UsageLog(path=tmp_path / "u.jsonl"), sleep=lambda _: None)
    images = {"event_x": ImageRef("image_x", "user_x", None, "event_x")}
    first = extract_images(images, {"event_x": event()}, client, cache_path=tmp_path / "c.json", images_dir=tmp_path)
    assert first["event_x"].chosen_amount == Decimal("100000")
    assert calls[0]["messages"][1]["content"][1]["type"] == "image_url"
    extract_images(images, {"event_x": event()}, client, cache_path=tmp_path / "c.json", images_dir=tmp_path)
    assert len(calls) == 1


def test_pipeline_refuses_a_future_blank_amount_with_no_reading() -> None:
    """user_16's scheduled rent balance (event_1442) must never be treated as zero."""
    dataset = load_sample_dataset()
    with pytest.raises(UnresolvedAmountError):
        decide_request(dataset.request_by_id("request_16"), dataset, None, {})


def test_pipeline_uses_the_image_amount_for_a_blank_event() -> None:
    dataset = load_sample_dataset()
    decision = decide_request(dataset.request_by_id("request_16"), dataset, None, {"event_1442": Decimal("100000")})
    assert decision.affordability_status
