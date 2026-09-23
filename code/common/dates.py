"""Date helpers shared across the engine. Pure functions, no I/O."""

from collections import Counter
from datetime import date, timedelta
from typing import Iterable, Optional


def days_between(start: date, end: date) -> int:
    """Whole days from `start` to `end`; negative when `end` precedes `start`."""
    return (end - start).days


def add_days(start: date, days: int) -> date:
    """`start` shifted by `days`."""
    return start + timedelta(days=days)


def date_range(start: date, days: int) -> list[date]:
    """`days` + 1 consecutive dates beginning at `start`, inclusive of both ends."""
    return [start + timedelta(days=offset) for offset in range(days + 1)]


def median(values: Iterable[float]) -> Optional[float]:
    """Median of `values`, or None when empty."""
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def intervals(dates: Iterable[date]) -> list[int]:
    """Gaps in days between consecutive sorted dates."""
    ordered = sorted(dates)
    return [days_between(a, b) for a, b in zip(ordered, ordered[1:])]


def dominant_day_of_month(dates: Iterable[date]) -> Optional[int]:
    """The most common day-of-month across `dates`, or None when empty.

    Used to place a monthly series in future months (DATASET_FACTS D3: payday is
    per-user, not the 15th).
    """
    days = Counter(value.day for value in dates)
    if not days:
        return None
    return days.most_common(1)[0][0]


def on_day_of_month(year: int, month: int, day: int) -> date:
    """Build a date, clamping `day` to the last valid day of that month."""
    if month == 12:
        next_month_start = date(year + 1, 1, 1)
    else:
        next_month_start = date(year, month + 1, 1)
    last_day = (next_month_start - timedelta(days=1)).day
    return date(year, month, min(day, last_day))


def monthly_occurrences(after: date, until: date, day_of_month: int) -> list[date]:
    """Every date strictly after `after` and on or before `until` on `day_of_month`."""
    results: list[date] = []
    year, month = after.year, after.month
    while True:
        candidate = on_day_of_month(year, month, day_of_month)
        if candidate > until:
            break
        if candidate > after:
            results.append(candidate)
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return results


def periodic_occurrences(last_seen: date, after: date, until: date, period_days: int) -> list[date]:
    """Every date on a fixed `period_days` cadence from `last_seen`, within the window."""
    if period_days <= 0:
        return []
    results: list[date] = []
    candidate = last_seen + timedelta(days=period_days)
    while candidate <= until:
        if candidate > after:
            results.append(candidate)
        candidate += timedelta(days=period_days)
    return results
