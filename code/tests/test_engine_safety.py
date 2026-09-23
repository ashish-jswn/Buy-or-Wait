"""Tests for engine.safety: margin measurement and the one-directional downgrade."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from config import MESSAGE_FACTS_CACHE_PATH
from data.loader import load_sample_dataset
from engine.forecast import Forecast
from engine.pipeline import decide_request
from engine.safety import OUTCOME_DOWNGRADED, OUTCOME_HELD, OUTCOME_NOT_CHECKED, PERMISSIVENESS, plan_margin, safe_decide_request
from extraction.images import load_image_cache, resolve_amount
from extraction.messages import load_cache, readings_from_cache

START = date(2025, 1, 1)


def forecast(opening=1000, floor=100, flows=None, horizon=30) -> Forecast:
    daily = [Decimal("0")] * (horizon + 1)
    for offset, amount in (flows or {}).items():
        daily[offset] += Decimal(str(amount))
    cumulative, running = [], Decimal("0")
    for value in daily:
        running += value
        cumulative.append(running)
    return Forecast(START, horizon, Decimal(str(opening)), Decimal(str(floor)), daily, cumulative)


def test_plan_margin_is_the_lowest_balance_above_the_floor_after_payments() -> None:
    fc = forecast(opening=1000, floor=100, flows={10: -300})
    assert plan_margin(fc, [(START, Decimal("500"))]) == Decimal("100")
    assert plan_margin(fc, [(START + timedelta(days=20), Decimal("500"))]) == Decimal("100")
    assert plan_margin(fc, [(START, Decimal("700"))]) == Decimal("-100")


@pytest.fixture(scope="module")
def sample_inputs():
    dataset = load_sample_dataset()
    readings = readings_from_cache(load_cache(MESSAGE_FACTS_CACHE_PATH))
    events = {event.event_id: event for events in dataset.events_by_user.values() for event in events}
    amounts = {key: amount for key, reading in load_image_cache().items() if key in events and (amount := resolve_amount(reading, events[key])) is not None}
    return dataset, readings, amounts


def test_the_safety_check_never_makes_a_decision_more_permissive(sample_inputs) -> None:
    dataset, readings, amounts = sample_inputs
    for request in dataset.requests:
        base = decide_request(request, dataset, readings, amounts)
        final, trace = safe_decide_request(request, dataset, readings, amounts)
        assert PERMISSIVENESS[final.affordability_status] <= PERMISSIVENESS[base.affordability_status]
        if trace.outcome == OUTCOME_NOT_CHECKED:
            assert final == base
        if trace.outcome in (OUTCOME_HELD, OUTCOME_DOWNGRADED):
            assert trace.margin < trace.threshold


def test_marginal_pay_now_gets_a_spending_change_under_category_cadence(sample_inputs, monkeypatch) -> None:
    """request_21: pay-now clears the floor by 84.86 < 90 under the category model; gold cuts spending first."""
    import engine.state as state_module

    monkeypatch.setattr(state_module, "VARIABLE_SPEND_MODEL", "category_cadence")
    dataset, readings, amounts = sample_inputs
    final, trace = safe_decide_request(dataset.request_by_id("request_21"), dataset, readings, amounts)
    assert (final.affordability_status, final.recommended_payment_method) == ("affordable_with_plan", "full_payment")
    assert final.chosen.spending_changes and trace.outcome == "required_spending_change"


def test_message_income_stress_scope_matches_the_gold_evidence(sample_inputs, monkeypatch) -> None:
    """request_02's gold installment plan relies on a message-confirmed salary increase.

    Stressing every message-raised income ("all") wrongly refuses it; the adopted
    "near_floor" scope leaves it alone because the plan is not marginal.
    """
    import config

    dataset, readings, amounts = sample_inputs
    request = dataset.request_by_id("request_02")
    monkeypatch.setattr(config, "MESSAGE_INCOME_STRESS_SCOPE", "all")
    stressed_all, _ = safe_decide_request(request, dataset, readings, amounts)
    monkeypatch.setattr(config, "MESSAGE_INCOME_STRESS_SCOPE", "near_floor")
    near_floor, _ = safe_decide_request(request, dataset, readings, amounts)
    assert stressed_all.affordability_status == "not_affordable"
    assert (near_floor.affordability_status, near_floor.recommended_payment_method) == ("affordable_with_plan", "installments")


def test_partial_payment_plans_are_not_stressed(sample_inputs) -> None:
    """C2: a partial plan pays exactly the safe amount, so its ~0 margin is by construction."""
    dataset, readings, amounts = sample_inputs
    request = dataset.request_by_id("request_19")
    final, trace = safe_decide_request(request, dataset, readings, amounts)
    assert final.recommended_payment_method == "partial_payment"
    assert trace.outcome == OUTCOME_NOT_CHECKED and final == decide_request(request, dataset, readings, amounts)


def test_not_affordable_rows_are_never_stressed(sample_inputs) -> None:
    dataset, readings, amounts = sample_inputs
    final, trace = safe_decide_request(dataset.request_by_id("request_05"), dataset, readings, amounts)
    assert final.affordability_status == "not_affordable" and trace.outcome == OUTCOME_NOT_CHECKED
