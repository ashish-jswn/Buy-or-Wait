"""Tests for the category-cadence variable-spend model (DATASET_FACTS I1). No API key."""

from datetime import date, timedelta
from decimal import Decimal

import engine.state as state_module
from data.loader import load_sample_dataset
from data.records import Event
from engine.forecast import build_forecast
from engine.state import CashState, build_cash_state, project_variable_spend

AS_OF = date(2025, 3, 1)


def purchase(event_id, on, category="groceries", amount="60", status="settled", description="Grocery delivery") -> Event:
    return Event(event_id, "user_v", "expense", description, category, "debit", Decimal(amount), "EUR", on, on, status, None, "fixed", None)


def weekly(last=date(2025, 2, 22), count=4, category="groceries", amounts=("50", "60", "70", "60")):
    return [purchase(f"e{i}", last - timedelta(days=7 * (count - 1 - i)), category, amounts[i % len(amounts)], description=f"label {i}") for i in range(count)]


def test_category_is_projected_on_its_exact_gap_at_the_mean_amount_across_descriptions() -> None:
    flows = project_variable_spend(weekly(), AS_OF, horizon_days=21)
    assert [flow.on for flow in flows] == [date(2025, 3, 8), date(2025, 3, 15), date(2025, 3, 22)]
    assert {flow.amount for flow in flows} == {Decimal("-60")}


def test_a_purchase_due_on_the_request_date_is_not_projected() -> None:
    flows = project_variable_spend(weekly(last=date(2025, 2, 22)), date(2025, 3, 1), horizon_days=7)
    assert all(flow.on > date(2025, 3, 1) for flow in flows)


def test_only_settled_variable_debits_before_the_request_count() -> None:
    rows = weekly() + [
        purchase("rent1", date(2025, 2, 10), category="rent", amount="900"),
        purchase("pend", date(2025, 2, 27), amount="999", status="pending"),
        purchase("future", date(2025, 3, 5), amount="999"),
    ]
    flows = project_variable_spend(rows, AS_OF, horizon_days=21)
    assert {flow.category for flow in flows} == {"groceries"} and {flow.amount for flow in flows} == {Decimal("-60")}


def test_a_single_purchase_is_not_a_cadence() -> None:
    assert project_variable_spend([purchase("e1", date(2025, 2, 20))], AS_OF) == []


def test_forecast_includes_variable_flows() -> None:
    flows = project_variable_spend(weekly(), AS_OF, horizon_days=21)
    state = CashState("user_v", AS_OF, Decimal("1000"), Decimal("100"), variable_flows=flows)
    forecast = build_forecast(state, horizon_days=21)
    assert forecast.cumulative[-1] == Decimal("-180")


def test_build_cash_state_uses_the_configured_model(monkeypatch) -> None:
    dataset = load_sample_dataset()
    request = dataset.request_by_id("request_15")
    monkeypatch.setattr(state_module, "VARIABLE_SPEND_MODEL", "category_cadence")
    cadence = build_cash_state(dataset.profiles["user_15"], dataset.events_for("user_15"), request.request_date, dataset.rates)
    assert cadence.variable_flows and cadence.variable_daily_spend == {}
    assert {flow.category for flow in cadence.variable_flows} == {"groceries", "transport", "dining"}
