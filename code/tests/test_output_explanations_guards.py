"""Tests for output.explanations and output.guards.

The explanation test renders every gold row's own decision through the templates and
compares with the gold text: 23 of 25 must match exactly (the two known variant wordings,
request_04 and request_09, cannot). The guard tests check that every gold row passes and
that each kind of corruption is caught. Uses the sample dataset; no API key.
"""

from datetime import date
from decimal import Decimal

import pytest

from config import OUTPUT_COLUMNS
from data.loader import load_sample_dataset
from engine.plans import Decision, PlanCandidate
from output.explanations import explain, format_long_date, format_money
from output.guards import check_row, fallback_row

VARIANT_WORDINGS = {"request_04", "request_09"}


@pytest.fixture(scope="module")
def dataset():
    return load_sample_dataset()


@pytest.fixture(scope="module")
def events_by_id(dataset):
    return {event.event_id: event for events in dataset.events_by_user.values() for event in events}


def gold_decision(request) -> Decision:
    """A Decision built from a gold row, so templates can be tested in isolation."""
    gold = request.gold
    payments = () if gold["payment_plan"] == "none" else tuple(
        (date.fromisoformat(entry.split(":")[0]), Decimal(entry.split(":")[1])) for entry in gold["payment_plan"].split("|")
    )
    changes = () if gold["spending_changes_needed"] == "none" else tuple(gold["spending_changes_needed"].split("|"))
    chosen = PlanCandidate(gold["recommended_payment_method"], payments, Decimal("0"), "", changes) if payments else None
    earliest = date.fromisoformat(gold["earliest_date_for_full_payment"]) if gold["earliest_date_for_full_payment"] else None
    return Decision(
        Decimal(gold["amount_safe_to_pay"]), gold["affordability_status"], gold["recommended_payment_method"], payments, earliest, chosen, ()
    )


def gold_row(request) -> dict[str, str]:
    return {column: request.gold[column] if column != "request_id" else request.request_id for column in OUTPUT_COLUMNS}


def test_money_and_date_formats_match_gold() -> None:
    assert format_money("EUR", Decimal("620.4")) == "EUR 620.40"
    assert format_money("ZAR", Decimal("18000")) == "ZAR 18,000"
    assert format_money("IDR", Decimal("15952906.67")) == "IDR 15,952,906.67"
    assert format_long_date(date(2025, 8, 8)) == "8 August 2025"


def test_templates_reproduce_gold_explanations(dataset, events_by_id) -> None:
    descriptions = {event_id: event.description for event_id, event in events_by_id.items()}
    mismatches = []
    for request in dataset.requests:
        text = explain(request, dataset.profiles[request.user_id], gold_decision(request), descriptions)
        if text != request.gold["decision_explanation"]:
            mismatches.append(request.request_id)
    assert set(mismatches) == VARIANT_WORDINGS


def test_every_gold_row_passes_the_guards(dataset, events_by_id) -> None:
    for request in dataset.requests:
        problems = check_row(
            gold_row(request), request, dataset.profiles[request.user_id], dataset.options_by_request[request.request_id], events_by_id
        )
        assert problems == [], (request.request_id, problems)


@pytest.mark.parametrize(
    "request_id,column,value,expected",
    [
        ("request_01", "amount_safe_to_pay", "99999999", "out of bounds"),
        ("request_01", "recommended_payment_method", "wait", "invalid status/method pair"),
        ("request_01", "earliest_date_for_full_payment", "2024-03-04", "affordable_now requires"),
        ("request_05", "payment_plan", "2025-11-06:15488", "payment_plan none"),
        ("request_19", "payment_plan", "2024-09-04:28820|2024-09-15:10000", "sums to"),
        ("request_02", "payment_plan", "2025-08-08:15952906.67|2025-09-08:15952906.67|2025-10-07:15952906.67", "reproduce a supplied option"),
        ("request_08", "payment_plan", "2025-04-16:996.60", "wait must be one payment"),
        ("request_21", "spending_changes_needed", "stop:event_1815|reduce_to:event_1816:10", "minimum_allowed_amount"),
        ("request_06", "spending_changes_needed", "stop:event_1", "unknown event"),
        ("request_01", "decision_explanation", " ", "explanation is empty"),
    ],
)
def test_guards_catch_each_corruption(dataset, events_by_id, request_id, column, value, expected) -> None:
    request = dataset.request_by_id(request_id)
    row = gold_row(request)
    row[column] = value
    problems = check_row(row, request, dataset.profiles[request.user_id], dataset.options_by_request[request_id], events_by_id)
    assert any(expected in problem for problem in problems), problems


def test_fallback_row_passes_the_guards(dataset, events_by_id) -> None:
    request = dataset.request_by_id("request_02")
    row = fallback_row(request, "17229139.2", "Do not make this payment.")
    assert check_row(row, request, dataset.profiles[request.user_id], dataset.options_by_request["request_02"], events_by_id) == []
