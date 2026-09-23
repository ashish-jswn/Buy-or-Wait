"""Unit tests for engine.income: description-class income stream detection.

Real-data tests use sample users whose income history is recorded in DATASET_FACTS F1-F5.
Runs with no API key — nothing here touches a model.
"""

import csv
from collections import Counter
from datetime import date
from decimal import Decimal

import pytest

from config import (
    EVENTS_PATH,
    INCOME_CLASS_BY_DESCRIPTION,
    INCOME_CLASS_IRREGULAR,
    INCOME_CLASS_MERGE,
    INCOME_CLASS_ONE_OFF,
    INCOME_CLASS_STABLE,
    INCOME_CLASS_TERMINAL,
    INCOME_IRREGULAR_POOL_LABEL,
)
from data.loader import load_sample_dataset
from data.records import Event
from engine.income import classify_income, detect_income_streams
from engine.state import build_cash_state, project_recurring


@pytest.fixture(scope="module")
def dataset():
    """The sample dataset, loaded once for the module."""
    return load_sample_dataset()


def state_for(dataset, user_id: str):
    """Reconstruct one sample user's state at their request date."""
    request = next(item for item in dataset.requests if item.user_id == user_id)
    return build_cash_state(
        dataset.profiles[user_id], dataset.events_for(user_id), request.request_date, dataset.rates
    )


def income_series(state):
    """Only the credit series in a state."""
    return [series for series in state.recurring if series.direction == "credit"]


def credit(description: str, on: date, amount: str = "1000", **overrides) -> Event:
    """A settled credit on `on` with the given description."""
    fields = dict(
        event_id=f"e_{description[:6]}_{on.isoformat()}",
        user_id="user_test",
        event_type="income",
        description=description,
        category="salary",
        direction="credit",
        amount=Decimal(amount),
        currency="INR",
        event_date=on,
        settlement_date=on,
        status="settled",
        linked_event_id=None,
        flexibility="fixed",
        minimum_allowed_amount=None,
    )
    fields.update(overrides)
    return Event(**fields)


# --------------------------------------------------------------------------
# the class map
# --------------------------------------------------------------------------


def test_every_credit_description_is_classified_or_safely_defaulted() -> None:
    """F1: 38 of 39 credit descriptions are mapped; the single-row one defaults to never-projected."""
    with open(EVENTS_PATH, encoding="utf-8") as handle:
        descriptions = {row["description"] for row in csv.DictReader(handle) if row["direction"] == "credit"}
    assert len(descriptions) == 39
    assert set(INCOME_CLASS_BY_DESCRIPTION) <= descriptions
    assert descriptions - set(INCOME_CLASS_BY_DESCRIPTION) == {"August 2019 net salary"}
    assert classify_income("August 2019 net salary") == INCOME_CLASS_ONE_OFF


def test_class_map_sizes() -> None:
    """8 stable, 1 merge, 6 terminal, 8 one-off, 15 irregular."""
    assert Counter(INCOME_CLASS_BY_DESCRIPTION.values()) == {
        INCOME_CLASS_STABLE: 8,
        INCOME_CLASS_MERGE: 1,
        INCOME_CLASS_TERMINAL: 6,
        INCOME_CLASS_ONE_OFF: 8,
        INCOME_CLASS_IRREGULAR: 15,
    }


def test_an_unknown_description_is_never_projected() -> None:
    """Income we cannot place is not counted."""
    assert classify_income("Mystery windfall") == INCOME_CLASS_ONE_OFF
    events = [credit("Mystery windfall", date(2024, month, 15)) for month in range(1, 6)]
    assert detect_income_streams(events, date(2024, 6, 1)) == []


# --------------------------------------------------------------------------
# synthetic behaviour per class
# --------------------------------------------------------------------------


def test_a_terminal_row_closes_the_stream_before_it() -> None:
    """Payroll x4 then Final employer payroll: nothing is projected (user_05's shape)."""
    events = [credit("Payroll credit", date(2025, month, 15)) for month in range(6, 10)]
    events.append(credit("Final employer payroll", date(2025, 10, 15)))
    assert detect_income_streams(events, date(2025, 11, 6)) == []


def test_a_stream_starting_after_the_terminal_row_stays_open() -> None:
    """Previous employer payroll x4 then New employer payroll x1: the new job projects."""
    events = [credit("Previous employer payroll", date(2024, month, 15)) for month in range(1, 5)]
    events.append(credit("New employer payroll", date(2024, 5, 15), amount="2000"))
    (series,) = detect_income_streams(events, date(2024, 6, 5))
    assert series.key[2] == "New employer payroll"
    assert series.amount == Decimal("2000")
    assert series.day_of_month == 15


def test_one_off_income_never_projects_or_joins_a_stream() -> None:
    """A bonus between paydays neither recurs nor moves the payroll's amount or day."""
    events = [credit("Payroll credit", date(2024, month, 15)) for month in range(1, 5)]
    events.append(credit("Quarterly performance bonus", date(2024, 4, 20), amount="9000"))
    (series,) = detect_income_streams(events, date(2024, 5, 1))
    assert series.key[2] == "Payroll credit"
    assert series.amount == Decimal("1000")
    assert series.day_of_month == 15


def test_next_confirmed_salary_merges_into_the_stable_stream() -> None:
    """One series, last seen on the confirmed date, so that month is not counted twice."""
    events = [credit("Payroll credit", date(2024, month, 15)) for month in range(1, 5)]
    events.append(credit("Next confirmed salary", date(2024, 5, 15), status="scheduled"))
    (series,) = detect_income_streams(events, date(2024, 5, 6))
    assert series.key[2] == "Payroll credit"
    assert series.last_seen == date(2024, 5, 15)
    assert series.occurrences == 5
    assert project_recurring(series, date(2024, 5, 6), date(2024, 8, 4))[0] == date(2024, 6, 15)


def test_stable_amount_and_payday_follow_the_latest_row() -> None:
    """A step-down in pay and a moved payday both come from the most recent row."""
    events = [credit("Payroll credit", date(2024, month, 15), amount="1441") for month in range(1, 4)]
    events.append(credit("Payroll credit", date(2024, 4, 23), amount="1037.52"))
    (series,) = detect_income_streams(events, date(2024, 5, 5))
    assert series.amount == Decimal("1037.52")
    assert series.day_of_month == 23


def test_irregular_income_needs_three_pooled_rows() -> None:
    """Two gig payouts are not a pattern; three pool into one stream."""
    two = [credit("Delivery platform payout", date(2024, 1, 5)), credit("Driver platform payout", date(2024, 1, 19))]
    assert detect_income_streams(two, date(2024, 2, 1)) == []
    three = two + [credit("Task marketplace payout", date(2024, 2, 2))]
    (series,) = detect_income_streams(three, date(2024, 2, 10))
    assert series.key[2] == INCOME_IRREGULAR_POOL_LABEL
    assert series.period_days == 14
    assert series.day_of_month is None


# --------------------------------------------------------------------------
# real sample users (DATASET_FACTS F2-F5)
# --------------------------------------------------------------------------


def test_user_05_has_no_projected_income(dataset) -> None:
    """F2: Payroll credit x4 then Final employer payroll — income has ended."""
    assert income_series(state_for(dataset, "user_05")) == []


def test_user_09_freelance_income_is_one_pooled_stream(dataset) -> None:
    """user_09: 8 distinct project descriptions, 10 rows, pooled and projected."""
    (series,) = income_series(state_for(dataset, "user_09"))
    assert series.key[2] == INCOME_IRREGULAR_POOL_LABEL
    assert series.occurrences == 10
    assert series.period_days == 15
    assert series.amount > Decimal("0")


def test_user_13_next_confirmed_salary_joins_the_primary_salary(dataset) -> None:
    """The 2024-03-15 confirmed salary is the Primary household salary's March payment."""
    by_name = {series.key[2]: series for series in income_series(state_for(dataset, "user_13"))}
    assert "Next confirmed salary" not in by_name
    primary = by_name["Primary household salary"]
    assert primary.last_seen == date(2024, 3, 15)
    assert primary.occurrences == 6


def test_user_07_payday_moved_to_the_23rd(dataset) -> None:
    """F5: latest Payroll credit landed on the 23rd; the stream is kept and anchored there."""
    (series,) = income_series(state_for(dataset, "user_07"))
    assert series.day_of_month == 23


def test_a_stream_silent_for_more_than_40_days_has_ended() -> None:
    """Author decision: last payment 45 days before the request means the stream ended."""
    events = [credit("Second household income", date(2024, month, 20)) for month in range(1, 4)]
    assert detect_income_streams(events, date(2024, 5, 4)) == []
    assert detect_income_streams(events, date(2024, 4, 25))


def test_user_13_second_household_income_is_stale(dataset) -> None:
    """user_13: last Second household income 2024-01-20, request 2024-03-07 (47 days)."""
    names = {series.key[2] for series in income_series(state_for(dataset, "user_13"))}
    assert names == {"Primary household salary"}


def test_user_06_projects_the_stepped_down_salary(dataset) -> None:
    """F4: 1441 x3 then 1037.52 x2 — the lower, latest level is what recurs."""
    (series,) = income_series(state_for(dataset, "user_06"))
    assert series.amount == Decimal("1037.52")
