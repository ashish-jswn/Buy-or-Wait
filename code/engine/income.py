"""Income stream detection: group credits by description, classify, then project.

Pure functions: data in, series out. No file reads, no model calls, no printing.

Why description and not category: every income row is category ``salary`` (bar six
``windfall`` prize rows), so a category key merges an ended payroll into an ongoing one.
``user_05`` is the proof — ``Payroll credit`` x4 then ``Final employer payroll`` x1; keyed
on category it looks like five months of steady pay (DATASET_FACTS F1-F3).

Each description maps to one class (``config.INCOME_CLASS_BY_DESCRIPTION``):

* STABLE    — one stream per description, projected monthly.
* MERGE     — ``Next confirmed salary``: folded into the user's largest open stable stream
              before projection, so the confirmed row and the stream are one series.
* TERMINAL  — never projected, and closes every income stream last seen on or before it.
* ONE_OFF   — never projected and never part of a stream.
* IRREGULAR — all such rows for a user pooled into one stream and projected.

Unknown descriptions take ``INCOME_CLASS_UNKNOWN_DEFAULT`` (never projected): income we
cannot place is not counted, the financially safer reading.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Iterable, Optional

from common.currency import convert
from common.dates import days_between
from config import (
    DIRECTION_CREDIT,
    INCOME_CLASS_BY_DESCRIPTION,
    INCOME_CLASS_IRREGULAR,
    INCOME_CLASS_MERGE,
    INCOME_CLASS_STABLE,
    INCOME_CLASS_TERMINAL,
    INCOME_CLASS_UNKNOWN_DEFAULT,
    INCOME_IRREGULAR_MIN_OCCURRENCES,
    INCOME_IRREGULAR_POOL_LABEL,
    INCOME_STABLE_MIN_OCCURRENCES,
    INCOME_STALE_DAYS,
    RECURRENCE_CADENCE_TOLERANCE_DAYS,
    RECURRENCE_MONTHLY_DAYS,
)
from data.records import Event, Profile
from engine.series import RecurringSeries

Rates = dict[tuple[date, str, str], Decimal]


def classify_income(description: str) -> str:
    """The income class for a credit description; unknown ones get the safe default."""
    return INCOME_CLASS_BY_DESCRIPTION.get(description, INCOME_CLASS_UNKNOWN_DEFAULT)


def unclassified_income_descriptions(events: Iterable[Event]) -> set[str]:
    """Credit descriptions absent from the class map — for flagging, never for guessing."""
    return {
        event.description
        for event in events
        if event.is_credit and event.description not in INCOME_CLASS_BY_DESCRIPTION
    }


def _home_amount(event: Event, profile: Optional[Profile], rates: Optional[Rates]) -> Decimal:
    """The event's amount in home currency, converted at its own cash date (D2)."""
    assert event.amount is not None
    if profile is None or rates is None or event.currency == profile.home_currency:
        return event.amount
    return convert(event.amount, event.currency, profile.home_currency, event.cash_date, rates)


def _decimal_median(values: list[Decimal]) -> Decimal:
    """Median of a non-empty list of Decimals, kept exact."""
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _closed_by_terminal(rows: list[Event], terminal_end: Optional[date]) -> bool:
    """True when a terminal row lands on or after this stream's last occurrence."""
    return terminal_end is not None and rows[-1].cash_date <= terminal_end


def _merge_target(streams: dict[str, list[Event]]) -> Optional[str]:
    """The largest open stable stream: most rows, then most recently seen."""
    if not streams:
        return None
    return max(streams, key=lambda name: (len(streams[name]), streams[name][-1].cash_date))


def _monthly_series(
    key: tuple[str, str, str],
    rows: list[Event],
    profile: Optional[Profile],
    rates: Optional[Rates],
) -> RecurringSeries:
    """A stable stream projected monthly from its latest occurrence.

    Amount and day-of-month both come from the latest row, not the most common one: every
    stable stream whose amount changes does so once, as a step (DATASET_FACTS F4), and the
    eight users whose payday moved did so in their latest month (F5).
    """
    latest = rows[-1]
    return RecurringSeries(
        key=key,
        category=latest.category,
        direction=DIRECTION_CREDIT,
        amount=_home_amount(latest, profile, rates),
        last_seen=latest.cash_date,
        period_days=RECURRENCE_MONTHLY_DAYS,
        day_of_month=latest.cash_date.day,
        occurrences=len(rows),
        latest_event_id=latest.event_id,
    )


def _pooled_series(
    user_id: str,
    rows: list[Event],
    profile: Optional[Profile],
    rates: Optional[Rates],
) -> Optional[RecurringSeries]:
    """One stream for all irregular income: average cadence, median amount.

    The cadence is the mean gap over the whole span, not the median gap: freelance payouts
    alternate short and long gaps, and the median of those overstates how often money lands.
    """
    latest = rows[-1]
    span = days_between(rows[0].cash_date, latest.cash_date)
    period = round(span / (len(rows) - 1))
    if period <= 0:
        return None
    is_monthly = abs(period - RECURRENCE_MONTHLY_DAYS) <= RECURRENCE_CADENCE_TOLERANCE_DAYS
    return RecurringSeries(
        key=(user_id, latest.category, INCOME_IRREGULAR_POOL_LABEL),
        category=latest.category,
        direction=DIRECTION_CREDIT,
        amount=_decimal_median([_home_amount(row, profile, rates) for row in rows]),
        last_seen=latest.cash_date,
        period_days=RECURRENCE_MONTHLY_DAYS if is_monthly else period,
        day_of_month=latest.cash_date.day if is_monthly else None,
        occurrences=len(rows),
        latest_event_id=latest.event_id,
    )


def detect_income_streams(
    events: Iterable[Event],
    as_of: date,
    profile: Optional[Profile] = None,
    rates: Optional[Rates] = None,
) -> list[RecurringSeries]:
    """Income series to project forward from `as_of`, by description class.

    Expects cash-moving events only (pending credits already removed by the caller).
    History is every credit dated on or before `as_of`; ``Next confirmed salary`` rows
    are merged in whatever their date, because a scheduled salary is confirmed. When
    `profile` and `rates` are given, amounts are converted to home currency.
    """
    credits = sorted(
        (event for event in events if event.is_credit and event.amount is not None),
        key=lambda event: (event.cash_date, event.event_id),
    )
    stable: dict[str, list[Event]] = defaultdict(list)
    irregular: list[Event] = []
    confirmed_next: list[Event] = []
    terminal_end: Optional[date] = None

    for event in credits:
        income_class = classify_income(event.description)
        if income_class == INCOME_CLASS_MERGE:
            confirmed_next.append(event)
            continue
        if event.cash_date > as_of:
            continue
        if income_class == INCOME_CLASS_STABLE:
            stable[event.description].append(event)
        elif income_class == INCOME_CLASS_IRREGULAR:
            irregular.append(event)
        elif income_class == INCOME_CLASS_TERMINAL:
            terminal_end = event.cash_date  # credits are sorted, so this ends as the latest

    open_streams = {
        name: rows for name, rows in stable.items() if not _closed_by_terminal(rows, terminal_end)
    }
    if confirmed_next:
        target = _merge_target(open_streams)
        if target is None:
            # A confirmed salary with no stream to join starts its own (user_01).
            target = confirmed_next[0].description
            open_streams[target] = []
        open_streams[target] = sorted(
            open_streams[target] + confirmed_next,
            key=lambda event: (event.cash_date, event.event_id),
        )

    series: list[RecurringSeries] = []
    for name, rows in sorted(open_streams.items()):
        if len(rows) < INCOME_STABLE_MIN_OCCURRENCES:
            continue
        key = (rows[-1].user_id, rows[-1].category, name)
        series.append(_monthly_series(key, rows, profile, rates))

    if len(irregular) >= INCOME_IRREGULAR_MIN_OCCURRENCES and not _closed_by_terminal(
        irregular, terminal_end
    ):
        pooled = _pooled_series(irregular[-1].user_id, irregular, profile, rates)
        if pooled is not None:
            series.append(pooled)
    # A stream silent for longer than INCOME_STALE_DAYS has ended (author decision). A
    # merged Next confirmed salary puts last_seen in the future, so it is never stale.
    return [item for item in series if days_between(item.last_seen, as_of) <= INCOME_STALE_DAYS]
