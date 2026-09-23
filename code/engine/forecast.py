"""The 90-day safety check. Pure functions: a CashState in, numbers out.

Definitions, both computed *before* any optional spending change — which is what lets
``earliest_date_for_full_payment`` sit after ``request_date`` on a row whose plan pays
in full today using cuts:

    cumulative(t) = sum of projected net flows over (request_date, request_date + t]
    worst_dip     = min(0, min over t in 0..H of cumulative(t))
    headroom      = balance - minimum_balance - buffer + worst_dip
    amount_safe_to_pay = clamp(headroom, 0, requested_amount)

``earliest_date_for_full_payment`` is the first date d in the window at which paying
the full requested amount leaves the projected balance at or above the minimum for
every remaining day of the window.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from common.dates import add_days, date_range, days_between
from config import (
    FORECAST_HORIZON_DAYS,
    MINIMUM_BALANCE_BUFFER,
    VARIABLE_ESSENTIAL_CATEGORIES,
)
from engine.state import CashState, project_recurring

ZERO = Decimal("0")


@dataclass
class Forecast:
    """Projected daily net flow and running balance over the horizon."""

    start: date
    horizon_days: int
    opening_balance: Decimal
    floor: Decimal
    daily_net: list[Decimal] = field(default_factory=list)
    cumulative: list[Decimal] = field(default_factory=list)

    @property
    def dates(self) -> list[date]:
        """One date per point in the projection, inclusive of both ends."""
        return date_range(self.start, self.horizon_days)

    def balance_on(self, offset: int) -> Decimal:
        """Projected balance at day `offset`, before any request payment."""
        return self.opening_balance + self.cumulative[offset]

    def worst_dip(self) -> Decimal:
        """The deepest cumulative shortfall over the window, never positive."""
        return min(ZERO, min(self.cumulative))

    def worst_dip_from(self, offset: int) -> Decimal:
        """The deepest cumulative shortfall from `offset` onward, never positive."""
        return min(self.cumulative[offset:])

    def trough_balance(self) -> Decimal:
        """The lowest projected balance over the window, before any payment."""
        return self.opening_balance + min(self.cumulative)


def build_forecast(
    state: CashState, horizon_days: int = FORECAST_HORIZON_DAYS
) -> Forecast:
    """Project daily net cash flow for `horizon_days` from the state's as-of date.

    Combines three sources: dated known flows already in the state, recurring series
    projected onto their cadence, and variable essential categories spread as a flat
    daily rate. Index 0 is the request date; a recurring item due that day counts,
    because it has not been paid yet.
    """
    start = state.as_of
    end = add_days(start, horizon_days)
    daily = [ZERO] * (horizon_days + 1)

    for flow in state.known_flows:
        offset = days_between(start, flow.on)
        if 0 <= offset <= horizon_days:
            daily[offset] += flow.amount

    # A scheduled or pending row already states an occurrence the series would also
    # project. Counting both would double it, so a projected date is dropped when a
    # known flow of the same category and direction already lands there.
    known_slots = {
        (days_between(start, flow.on), flow.category, flow.amount > ZERO)
        for flow in state.known_flows
    }
    for series in state.recurring:
        is_credit = series.direction == "credit"
        sign = Decimal(1) if is_credit else Decimal(-1)
        for occurrence in project_recurring(series, start, end):
            offset = days_between(start, occurrence)
            if not 0 <= offset <= horizon_days:
                continue
            if (offset, series.category, is_credit) in known_slots:
                continue
            daily[offset] += sign * series.amount

    # Variable purchases projected on their category cadence (DATASET_FACTS I1), when used.
    for flow in state.variable_flows:
        offset = days_between(start, flow.on)
        if 0 <= offset <= horizon_days:
            daily[offset] += flow.amount

    total_variable = sum(state.variable_daily_spend.values(), ZERO)
    if total_variable > ZERO:
        for offset in range(1, horizon_days + 1):
            daily[offset] -= total_variable

    cumulative: list[Decimal] = []
    running = ZERO
    for value in daily:
        running += value
        cumulative.append(running)

    return Forecast(
        start=start,
        horizon_days=horizon_days,
        opening_balance=state.opening_balance,
        floor=state.minimum_balance + Decimal(str(MINIMUM_BALANCE_BUFFER)),
        daily_net=daily,
        cumulative=cumulative,
    )


def headroom(forecast: Forecast) -> Decimal:
    """The largest payment today that keeps every projected day at or above the floor.

    May be negative when the user is already projected to breach the minimum without
    paying anything at all.
    """
    return forecast.opening_balance - forecast.floor + forecast.worst_dip()


def amount_safe_to_pay(forecast: Forecast, requested_amount: Decimal) -> Decimal:
    """Headroom clamped to [0, requested_amount], per the spec's bounds."""
    return max(ZERO, min(headroom(forecast), requested_amount))


def _check_end(forecast: Forecast, last_offset: Optional[int]) -> int:
    """The last day checked: the whole window, or `last_offset` when a shorter check applies."""
    if last_offset is None:
        return forecast.horizon_days
    return max(0, min(last_offset, forecast.horizon_days))


def can_pay_full_on(
    forecast: Forecast, offset: int, requested_amount: Decimal, last_offset: Optional[int] = None
) -> bool:
    """True when paying the full amount at day `offset` holds the floor afterwards.

    Only days from `offset` to the check end are tested: before the payment the balance is
    whatever it would have been anyway. `last_offset` limits the check (config
    PLAN_CHECK_WINDOW); None checks the whole window.
    """
    end = _check_end(forecast, last_offset)
    if offset < 0 or offset > end:
        return False
    return (
        forecast.opening_balance + min(forecast.cumulative[offset:end + 1]) - requested_amount
        >= forecast.floor
    )


def earliest_full_payment_date(
    forecast: Forecast, requested_amount: Decimal, last_offset: Optional[int] = None
) -> Optional[date]:
    """First date within the check window at which one full payment is safe, else None.

    Computed before any optional spending change, so it reports unaided capacity.
    """
    for offset in range(_check_end(forecast, last_offset) + 1):
        if can_pay_full_on(forecast, offset, requested_amount, last_offset):
            return add_days(forecast.start, offset)
    return None


@dataclass(frozen=True)
class SafetyAssessment:
    """The two numbers the forecast exists to produce, plus the context behind them."""

    amount_safe_to_pay: Decimal
    earliest_full_payment: Optional[date]
    headroom: Decimal
    worst_dip: Decimal
    trough_balance: Decimal
    floor: Decimal

    @property
    def full_amount_safe_today(self) -> bool:
        """True when the whole request could be paid on the request date."""
        return self.earliest_full_payment is not None and self.headroom >= ZERO


def assess(
    state: CashState,
    requested_amount: Decimal,
    horizon_days: int = FORECAST_HORIZON_DAYS,
    check_until: Optional[date] = None,
) -> SafetyAssessment:
    """Run the safety check for one request against one reconstructed state.

    amount_safe_to_pay always uses the full window; the earliest full-payment date is
    checked only up to `check_until` when it is given.
    """
    forecast = build_forecast(state, horizon_days)
    last_offset = None if check_until is None else days_between(forecast.start, check_until)
    return SafetyAssessment(
        amount_safe_to_pay=amount_safe_to_pay(forecast, requested_amount),
        earliest_full_payment=earliest_full_payment_date(forecast, requested_amount, last_offset),
        headroom=headroom(forecast),
        worst_dip=forecast.worst_dip(),
        trough_balance=forecast.trough_balance(),
        floor=forecast.floor,
    )


__all__ = [
    "Forecast",
    "SafetyAssessment",
    "amount_safe_to_pay",
    "assess",
    "build_forecast",
    "can_pay_full_on",
    "earliest_full_payment_date",
    "headroom",
    "VARIABLE_ESSENTIAL_CATEGORIES",
]
