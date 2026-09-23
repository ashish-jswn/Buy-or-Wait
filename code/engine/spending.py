"""Spending changes: the smallest set of permitted cuts that makes paying in full today safe.

Pure functions: state, events, profile and request in; changes out. No I/O, no model calls.

Eligibility (DATASET_FACTS A1-A5): the change names the latest event of a (category,
description) series dated before request_date; that event is not ``fixed``; its category
is not protected; ``stop`` needs a stoppable event in a category the user will stop, and
``reduce_to`` needs a reducible event in a category the user will reduce, at exactly its
``minimum_allowed_amount``. When both verbs are permitted, reduce (A4).

Author decisions (2026-09-13):

* try 1 change, then 2, then 3; within a size, pick the set that cuts the least money;
* an expense the forecast does not project as a recurring series (e.g. a 2-row dining
  series) is projected from its own average gap;
* output order: stops first, then reductions, each by event id.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from itertools import combinations
from typing import Iterable, Optional

from common.currency import convert
from common.dates import add_days, days_between
from common.formatting import format_plan_amount
from config import (
    DIRECTION_DEBIT,
    FORECAST_HORIZON_DAYS,
    IGNORED_STATUSES,
    MAX_SPENDING_CHANGES,
    METHOD_FULL_PAYMENT,
    SPENDING_CHANGE_FLOW_KIND,
)
from data.records import Event, Profile, Request
from engine.forecast import build_forecast
from engine.plans import PlanCandidate, schedule_is_safe
from engine.state import CashFlow, CashState, project_recurring

Rates = dict[tuple[date, str, str], Decimal]
VERB_STOP = "stop"
VERB_REDUCE = "reduce_to"


@dataclass(frozen=True)
class SpendingChange:
    """One permitted cut and the cash it frees inside the forecast window."""

    event_id: str
    verb: str
    new_amount: Optional[Decimal]
    savings: tuple[CashFlow, ...]

    @property
    def total_saving(self) -> Decimal:
        return sum((flow.amount for flow in self.savings), Decimal("0"))

    @property
    def action(self) -> str:
        """The spending_changes_needed token for this change."""
        if self.verb == VERB_STOP:
            return f"stop:{self.event_id}"
        return f"reduce_to:{self.event_id}:{format_plan_amount(self.new_amount)}"


def _event_number(event_id: str) -> int:
    """Numeric part of an event id, for ordering (event_989 before event_1816)."""
    digits = re.sub(r"\D", "", event_id)
    return int(digits) if digits else 0


def _home(amount: Decimal, event: Event, profile: Profile, rates: Rates) -> Decimal:
    """`amount` in the event's currency, converted to home at the event's cash date (D2)."""
    if event.currency == profile.home_currency:
        return amount
    return convert(amount, event.currency, profile.home_currency, event.cash_date, rates)


def _projected_dates(rows: list[Event], series, as_of: date, until: date) -> list[date]:
    """Future occurrences: from the recurring series when one exists, else the rows' own
    average gap (author decision)."""
    if series is not None:
        return project_recurring(series, as_of, until)
    if len(rows) < 2:
        return []
    gap = round(days_between(rows[0].cash_date, rows[-1].cash_date) / (len(rows) - 1))
    if gap <= 0:
        return []
    dates, on = [], rows[-1].cash_date + timedelta(days=gap)
    while on <= until:
        if on >= as_of:
            dates.append(on)
        on += timedelta(days=gap)
    return dates


def eligible_changes(
    state: CashState,
    events: Iterable[Event],
    profile: Profile,
    rates: Rates,
    horizon_days: int = FORECAST_HORIZON_DAYS,
) -> list[SpendingChange]:
    """Every single change the user permits, with its savings over the window."""
    as_of = state.as_of
    until = add_days(as_of, horizon_days)
    groups: dict[tuple[str, str], list[Event]] = defaultdict(list)
    for event in events:
        if not event.is_debit or event.amount is None or event.status in IGNORED_STATUSES:
            continue
        if event.cash_date >= as_of:
            continue
        groups[(event.category, event.description)].append(event)
    series_by_key = {item.key: item for item in state.recurring if item.direction == DIRECTION_DEBIT}

    changes: list[SpendingChange] = []
    for (category, description), rows in groups.items():
        rows.sort(key=lambda item: (item.cash_date, item.event_id))
        latest = rows[-1]
        if not latest.is_flexible or category in profile.protected_categories:
            continue
        can_reduce = latest.can_reduce and profile.may_reduce(category) and latest.minimum_allowed_amount is not None
        can_stop = latest.can_stop and profile.may_stop(category)
        if not (can_reduce or can_stop):
            continue
        series = series_by_key.get((profile.user_id, category, description))
        dates = _projected_dates(rows, series, as_of, until)
        if not dates:
            continue
        current = series.amount if series is not None else _home(latest.amount, latest, profile, rates)

        verb, new_amount, per_occurrence = None, None, Decimal("0")
        if can_reduce:
            per_occurrence = current - _home(latest.minimum_allowed_amount, latest, profile, rates)
            if per_occurrence > 0:
                verb, new_amount = VERB_REDUCE, latest.minimum_allowed_amount
        if verb is None and can_stop:
            verb, per_occurrence = VERB_STOP, current
        if verb is None or per_occurrence <= 0:
            continue
        savings = tuple(
            CashFlow(on, per_occurrence, f"{category}:change", latest.event_id, SPENDING_CHANGE_FLOW_KIND)
            for on in dates
        )
        changes.append(SpendingChange(latest.event_id, verb, new_amount, savings))
    return sorted(changes, key=lambda item: _event_number(item.event_id))


def _safe_with(
    state: CashState,
    changes: Iterable[SpendingChange],
    request: Request,
    required_margin: Decimal = Decimal("0"),
    last_offset: Optional[int] = None,
) -> bool:
    """True when paying the full amount today holds floor + `required_margin` once `changes` apply."""
    extra = [flow for change in changes for flow in change.savings]
    forecast = build_forecast(replace(state, known_flows=list(state.known_flows) + extra))
    if required_margin:
        forecast = replace(forecast, floor=forecast.floor + required_margin)
    return schedule_is_safe(forecast, [(request.request_date, request.requested_amount)], last_offset)


def find_spending_changes(
    state: CashState,
    changes: list[SpendingChange],
    request: Request,
    required_margin: Decimal = Decimal("0"),
    last_offset: Optional[int] = None,
) -> Optional[tuple[SpendingChange, ...]]:
    """The smallest working set; within a size, the one cutting least money, then lowest ids.

    `required_margin` asks for the plan to clear the floor by that much, not just reach it
    (used by the marginal-plan rule in ``engine.safety``). `last_offset` limits the checked
    days (config PLAN_CHECK_WINDOW).
    """
    for size in range(1, MAX_SPENDING_CHANGES + 1):
        working = [
            combo for combo in combinations(changes, size) if _safe_with(state, combo, request, required_margin, last_offset)
        ]
        if working:
            return min(
                working,
                key=lambda combo: (
                    sum((change.total_saving for change in combo), Decimal("0")),
                    sorted(_event_number(change.event_id) for change in combo),
                ),
            )
    return None


def ordered_actions(combo: Iterable[SpendingChange]) -> tuple[str, ...]:
    """Stops first, then reductions, each by event id (author decision)."""
    combo = list(combo)
    stops = sorted((c for c in combo if c.verb == VERB_STOP), key=lambda c: _event_number(c.event_id))
    reduces = sorted((c for c in combo if c.verb == VERB_REDUCE), key=lambda c: _event_number(c.event_id))
    return tuple(change.action for change in stops + reduces)


def spending_change_candidate(request: Request, combo: Iterable[SpendingChange]) -> PlanCandidate:
    """Full payment today, enabled by the given changes."""
    return PlanCandidate(
        METHOD_FULL_PAYMENT,
        ((request.request_date, request.requested_amount),),
        request.requested_amount,
        "",
        ordered_actions(combo),
    )
