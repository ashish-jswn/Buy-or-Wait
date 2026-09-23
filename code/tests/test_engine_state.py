"""Unit tests for engine.state, against five users read end to end by hand.

Those five were read event by event, so their histories are known ground truth: what
recurs, which rows must be excluded, and which amounts need an image. Runs with no API
key — nothing here touches a model.
"""

from datetime import date
from decimal import Decimal

import pytest

from common.currency import MissingRateError, convert
from data.loader import load_sample_dataset
from data.records import Event
from engine.state import (
    average_variable_daily_spend,
    build_cash_state,
    detect_recurring,
    moves_cash,
    project_recurring,
    resolve_conflicts,
    signed_home_amount,
)

REFERENCE_USERS = ("user_16", "user_19", "user_20", "user_21", "user_25")


@pytest.fixture(scope="module")
def dataset():
    """The sample dataset, loaded once for the module."""
    return load_sample_dataset()


def state_for(dataset, user_id: str):
    """Reconstruct one reference user's state at their request date."""
    request = next(item for item in dataset.requests if item.user_id == user_id)
    profile = dataset.profiles[user_id]
    return build_cash_state(profile, dataset.events_for(user_id), request.request_date, dataset.rates)


def make_event(**overrides) -> Event:
    """Build an Event with sensible defaults, overriding named fields."""
    defaults = dict(
        event_id="event_test",
        user_id="user_test",
        event_type="expense",
        description="Test expense",
        category="rent",
        direction="debit",
        amount=Decimal("100"),
        currency="INR",
        event_date=date(2024, 1, 1),
        settlement_date=date(2024, 1, 1),
        status="settled",
        linked_event_id=None,
        flexibility="fixed",
        minimum_allowed_amount=None,
    )
    defaults.update(overrides)
    return Event(**defaults)


# --------------------------------------------------------------------------
# D4 — status handling
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["cancelled", "failed", "unrealized"])
def test_ignored_statuses_never_move_cash(status: str) -> None:
    """Cancelled, failed and unrealized rows contribute nothing, either direction."""
    assert moves_cash(make_event(status=status, direction="debit")) is False
    assert moves_cash(make_event(status=status, direction="credit")) is False


def test_pending_debit_is_reserved_but_pending_credit_is_not() -> None:
    """The asymmetry at the heart of D4: reserve money going out, ignore money promised."""
    assert moves_cash(make_event(status="pending", direction="debit")) is True
    assert moves_cash(make_event(status="pending", direction="credit")) is False


def test_scheduled_and_settled_rows_move_cash() -> None:
    """A confirmed future salary counts on its settlement date."""
    assert moves_cash(make_event(status="scheduled", direction="credit")) is True
    assert moves_cash(make_event(status="settled", direction="debit")) is True


def test_non_cash_valuation_never_moves_cash() -> None:
    """An investment valuation is not cash however it is flagged."""
    assert moves_cash(make_event(direction="non_cash", status="settled")) is False


def test_user_20_excludes_its_pending_refund(dataset) -> None:
    """user_20's pending 8,640 refund (event_1785) must not be counted as cash."""
    state = state_for(dataset, "user_20")
    assert "event_1785" in state.excluded_event_ids
    assert all(flow.source_event_id != "event_1785" for flow in state.known_flows)


def test_user_21_excludes_its_unrealized_valuation(dataset) -> None:
    """user_21's portfolio valuation (event_1856) is non-cash and unrealized."""
    state = state_for(dataset, "user_21")
    assert "event_1856" in state.excluded_event_ids


def test_user_21_keeps_its_pending_debit_and_scheduled_salary(dataset) -> None:
    """The pending fuel charge is reserved; the scheduled salary is counted."""
    state = state_for(dataset, "user_21")
    flow_ids = {flow.source_event_id for flow in state.known_flows}
    assert "event_1857" in flow_ids  # pending debit, reserved
    assert "event_1858" in flow_ids  # scheduled salary, counted


def test_user_25_excludes_its_failed_debit(dataset) -> None:
    """user_25's failed utilities debit (event_2287) is ignored."""
    state = state_for(dataset, "user_25")
    assert "event_2287" in state.excluded_event_ids


# --------------------------------------------------------------------------
# D2 — currency conversion
# --------------------------------------------------------------------------


def test_same_currency_conversion_is_a_no_op() -> None:
    """No rate lookup happens when the currencies already match."""
    assert convert(Decimal("100"), "INR", "INR", date(2024, 1, 1), {}) == Decimal("100")


def test_conversion_uses_the_exact_settlement_date_row(dataset) -> None:
    """user_25's USD 1800 salary converts at the settlement-date USD->IDR rate."""
    event = make_event(
        user_id="user_25",
        category="salary",
        direction="credit",
        amount=Decimal("1800"),
        currency="USD",
        settlement_date=date(2024, 3, 15),
        status="scheduled",
    )
    amount = signed_home_amount(event, dataset.profiles["user_25"], dataset.rates)
    assert amount == Decimal("1800") * dataset.rates[(date(2024, 3, 15), "USD", "IDR")]
    assert amount > Decimal("28000000")


def test_missing_rate_raises_rather_than_falling_back(dataset) -> None:
    """D2: exact match or raise. No nearest-prior-date, no inverting the reverse pair."""
    with pytest.raises(MissingRateError):
        convert(Decimal("100"), "USD", "IDR", date(2024, 3, 16), dataset.rates)
    # The reverse pair exists on a date the forward pair does not; it must not be used.
    with pytest.raises(MissingRateError):
        convert(Decimal("100"), "ZAR", "EUR", date(2023, 10, 15), dataset.rates)


def test_every_reference_user_state_builds_without_a_rate_miss(dataset) -> None:
    """Building all five states exercises real conversions and must not raise."""
    for user_id in REFERENCE_USERS:
        assert state_for(dataset, user_id).user_id == user_id


# --------------------------------------------------------------------------
# D3 — recurring detection
# --------------------------------------------------------------------------


EXPECTED_SERIES = {
    "user_16": {"rent", "utilities", "streaming", "debt_repayment", "cloud_storage", "shopping", "salary"},
    "user_19": {"rent", "utilities", "healthcare", "debt_repayment", "cloud_storage", "shopping", "family_support", "salary"},
    "user_21": {"rent", "utilities", "streaming", "cloud_storage", "shopping", "salary"},
}


@pytest.mark.parametrize("user_id,expected", sorted(EXPECTED_SERIES.items()))
def test_recurring_detection_finds_exactly_the_known_series(
    dataset, user_id: str, expected: set
) -> None:
    """The series found match the histories read event by event by hand."""
    found = {series.category for series in state_for(dataset, user_id).recurring}
    assert found == expected


def test_variable_categories_are_never_scheduled_series(dataset) -> None:
    """Groceries, transport and dining are frequent but not fixed line items (D3)."""
    for user_id in REFERENCE_USERS:
        state = state_for(dataset, user_id)
        categories = {series.category for series in state.recurring}
        assert categories.isdisjoint({"groceries", "transport", "dining"})
        assert state.variable_daily_spend or state.variable_flows, f"{user_id} has no variable spend estimate"


def test_user_16_recurring_amounts_and_cadences(dataset) -> None:
    """user_16's fixed items repeat to the cent on known days of the month."""
    by_category = {s.category: s for s in state_for(dataset, "user_16").recurring}
    assert by_category["rent"].amount == Decimal("57100")
    assert by_category["rent"].day_of_month == 1
    assert by_category["debt_repayment"].amount == Decimal("17750")
    assert by_category["debt_repayment"].day_of_month == 10
    assert by_category["streaming"].amount == Decimal("3510")
    assert by_category["streaming"].day_of_month == 8
    assert by_category["salary"].amount == Decimal("173000")
    assert by_category["salary"].day_of_month == 15
    assert by_category["salary"].direction == "credit"


def test_salary_cadence_is_detected_per_user_not_assumed(dataset) -> None:
    """D3: payday is whatever the history says. Detected, never hardcoded."""
    for user_id in REFERENCE_USERS:
        salary = next(
            (s for s in state_for(dataset, user_id).recurring if s.category == "salary"), None
        )
        assert salary is not None, f"{user_id} salary series not detected"
        assert salary.is_monthly
        assert salary.day_of_month is not None


def test_foreign_currency_salary_is_converted_into_the_series(dataset) -> None:
    """user_25 is paid USD 1800; the series must carry IDR, not 1800."""
    salary = next(s for s in state_for(dataset, "user_25").recurring if s.category == "salary")
    assert salary.amount > Decimal("28000000")


def test_recurring_expense_amount_follows_the_configured_rule(monkeypatch) -> None:
    """max: the largest amount seen; latest: the most recent amount."""
    import engine.state as state_module

    events = [
        make_event(event_id=f"e{i}", amount=Decimal(value), event_date=date(2024, i, 6), settlement_date=date(2024, i, 6))
        for i, value in ((1, "120"), (2, "150"), (3, "110"))
    ]
    monkeypatch.setattr(state_module, "RECURRING_EXPENSE_AMOUNT", "max")
    (series,) = detect_recurring(events, date(2024, 3, 20))
    assert series.amount == Decimal("150")
    monkeypatch.setattr(state_module, "RECURRING_EXPENSE_AMOUNT", "latest")
    (series,) = detect_recurring(events, date(2024, 3, 20))
    assert series.amount == Decimal("110")


def test_a_series_below_the_occurrence_threshold_is_not_recurring() -> None:
    """Two occurrences are not a pattern."""
    events = [
        make_event(event_id="e1", event_date=date(2024, 1, 1), settlement_date=date(2024, 1, 1)),
        make_event(event_id="e2", event_date=date(2024, 2, 1), settlement_date=date(2024, 2, 1)),
    ]
    assert detect_recurring(events, date(2024, 3, 1)) == []


def test_an_irregular_cadence_is_not_recurring() -> None:
    """Three occurrences with wildly different gaps are not a schedule."""
    events = [
        make_event(event_id="e1", event_date=date(2024, 1, 1), settlement_date=date(2024, 1, 1)),
        make_event(event_id="e2", event_date=date(2024, 1, 9), settlement_date=date(2024, 1, 9)),
        make_event(event_id="e3", event_date=date(2024, 3, 20), settlement_date=date(2024, 3, 20)),
    ]
    assert detect_recurring(events, date(2024, 4, 1)) == []


def test_recurring_detection_ignores_events_after_the_as_of_date() -> None:
    """State is reconstructed as of request_date; later rows are not history."""
    events = [
        make_event(event_id=f"e{i}", event_date=date(2024, i, 1), settlement_date=date(2024, i, 1))
        for i in range(1, 5)
    ]
    assert detect_recurring(events, date(2024, 2, 15)) == []


# --------------------------------------------------------------------------
# projection
# --------------------------------------------------------------------------


def test_a_monthly_item_due_on_the_request_date_is_projected(dataset) -> None:
    """user_19's rent recurs on the 4th and the request is dated the 4th.

    Dropping it would overstate available cash by a whole rent payment.
    """
    state = state_for(dataset, "user_19")
    rent = next(series for series in state.recurring if series.category == "rent")
    occurrences = project_recurring(rent, date(2024, 9, 4), date(2024, 12, 3))
    assert occurrences[0] == date(2024, 9, 4)


def test_projection_never_repeats_an_already_observed_occurrence(dataset) -> None:
    """Occurrences start strictly after the last one seen in history."""
    state = state_for(dataset, "user_16")
    rent = next(series for series in state.recurring if series.category == "rent")
    occurrences = project_recurring(rent, state.as_of, date(2023, 11, 10))
    assert all(occurrence > rent.last_seen for occurrence in occurrences)


# --------------------------------------------------------------------------
# conflict resolution and blank amounts
# --------------------------------------------------------------------------


def test_a_linked_same_direction_record_supersedes_its_parent() -> None:
    """An amendment replaces the row it points at, rather than adding to it."""
    original = make_event(event_id="e1", amount=Decimal("100"))
    amendment = make_event(event_id="e2", amount=Decimal("120"), linked_event_id="e1")
    surviving, dropped = resolve_conflicts([original, amendment])
    assert dropped == {"e1"}
    assert {event.event_id for event in surviving} == {"e2"}


def test_a_pending_duplicate_of_a_settled_charge_is_dropped_not_reserved() -> None:
    """Same amount, same category, pending child of a settled parent: a duplicate authorization."""
    original = make_event(event_id="e1", category="shopping", amount=Decimal("80"), status="settled")
    duplicate = make_event(event_id="e2", category="shopping", amount=Decimal("80"), status="pending", linked_event_id="e1")
    surviving, dropped = resolve_conflicts([original, duplicate])
    assert dropped == {"e2"}
    assert {event.event_id for event in surviving} == {"e1"}


def test_a_pending_linked_charge_of_a_different_amount_still_supersedes() -> None:
    """Only an exact-amount pending copy is treated as a duplicate."""
    original = make_event(event_id="e1", category="shopping", amount=Decimal("80"), status="settled")
    amended = make_event(event_id="e2", category="shopping", amount=Decimal("95"), status="pending", linked_event_id="e1")
    _, dropped = resolve_conflicts([original, amended])
    assert dropped == {"e1"}


def test_a_linked_opposite_direction_record_is_a_separate_movement() -> None:
    """A refund of a purchase is its own flow, not a correction of the purchase."""
    purchase = make_event(event_id="e1", direction="debit", category="shopping")
    refund = make_event(
        event_id="e2", direction="credit", category="shopping", linked_event_id="e1"
    )
    surviving, dropped = resolve_conflicts([purchase, refund])
    assert dropped == set()
    assert len(surviving) == 2


def test_blank_amounts_are_reported_not_treated_as_zero(dataset) -> None:
    """A blank amount needs image extraction; silently zeroing it inflates headroom."""
    assert state_for(dataset, "user_16").unresolved_amount_events == ["event_1442"]
    assert state_for(dataset, "user_20").unresolved_amount_events == ["event_1786"]
    assert state_for(dataset, "user_21").unresolved_amount_events == []


def test_average_variable_daily_spend_is_positive_and_per_category(dataset) -> None:
    """Variable spend is a per-category daily rate drawn from recent history."""
    request = next(item for item in dataset.requests if item.user_id == "user_16")
    events = dataset.events_for("user_16")
    daily = average_variable_daily_spend(events, request.request_date)
    assert set(daily) == {"groceries", "transport", "dining"}
    assert all(value > 0 for value in daily.values())
