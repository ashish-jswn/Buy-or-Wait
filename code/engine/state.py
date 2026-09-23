"""Reconstruct one user's cash position as of request_date.

Pure functions: data in, result out. No file reads, no model calls, no printing, so
every piece is unit-testable with no API key present.

What this module decides:

* which events move cash, and when (DATASET_FACTS D4)
* what those amounts are in the user's home currency (DATASET_FACTS D2)
* which expense series recur, and on what cadence (DATASET_FACTS D3)
* which of two conflicting records wins (problem_statement.md conflict order)

Income streams are detected separately, by description class, in ``engine.income``.

What it deliberately does not decide: how much is safe to pay. That is ``forecast``.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Optional

from common.currency import convert
from common.dates import (
    days_between,
    dominant_day_of_month,
    median,
    monthly_occurrences,
    periodic_occurrences,
)
from config import (
    RECURRING_AMOUNT_LATEST,
    RECURRING_EXPENSE_AMOUNT,
    FORECAST_HORIZON_DAYS,
    STATUS_SETTLED,
    VARIABLE_FLOW_KIND,
    VARIABLE_MODEL_CATEGORY_CADENCE,
    VARIABLE_SPEND_MODEL,
    DIRECTION_NON_CASH,
    IGNORED_STATUSES,
    RECURRENCE_CADENCE_TOLERANCE_DAYS,
    RECURRENCE_MIN_OCCURRENCES,
    RECURRENCE_MONTHLY_DAYS,
    RECURRENCE_WEEKLY_DAYS,
    STATUS_PENDING,
    VARIABLE_ESSENTIAL_CATEGORIES,
    VARIABLE_SPEND_CONSERVATISM,
    VARIABLE_SPEND_LOOKBACK_DAYS,
)
from data.records import Event, Profile
from engine.income import detect_income_streams
from engine.series import RecurringSeries

# Conflict-resolution ranks, lowest wins (problem_statement.md "When records conflict").
CONFLICT_RANK_AMENDED = 0
CONFLICT_RANK_NEWER_SAME_SOURCE = 1
CONFLICT_RANK_SETTLED = 2
CONFLICT_RANK_SAFER = 3


@dataclass(frozen=True)
class CashFlow:
    """One dated cash movement in the user's home currency.

    ``amount`` is signed: positive is money in, negative is money out.
    """

    on: date
    amount: Decimal
    category: str
    source_event_id: Optional[str]
    kind: str

    @property
    def is_outflow(self) -> bool:
        """True when this movement reduces the balance."""
        return self.amount < 0


@dataclass
class CashState:
    """One user's reconstructed position at request_date."""

    user_id: str
    as_of: date
    opening_balance: Decimal
    minimum_balance: Decimal
    known_flows: list[CashFlow] = field(default_factory=list)
    recurring: list[RecurringSeries] = field(default_factory=list)
    variable_daily_spend: dict[str, Decimal] = field(default_factory=dict)
    variable_flows: list[CashFlow] = field(default_factory=list)
    unresolved_amount_events: list[str] = field(default_factory=list)
    excluded_event_ids: set[str] = field(default_factory=set)


def moves_cash(event: Event) -> bool:
    """True when an event should ever contribute to the cash forecast.

    Excludes cancelled, failed and unrealized rows, and non-cash valuations
    (DATASET_FACTS D4). Pending *credits* are excluded here too: money that has not
    landed is not cash, while pending debits must still be reserved.
    """
    if event.status in IGNORED_STATUSES:
        return False
    if event.direction == DIRECTION_NON_CASH:
        return False
    if event.status == STATUS_PENDING and event.is_credit:
        return False
    return True


def signed_home_amount(
    event: Event, profile: Profile, rates: dict[tuple[date, str, str], Decimal]
) -> Optional[Decimal]:
    """Convert an event to a signed amount in the user's home currency.

    Returns None when the event's amount is blank and still unresolved. Raises
    MissingRateError when a conversion has no exact rate row.
    """
    if event.amount is None:
        return None
    converted = convert(
        event.amount, event.currency, profile.home_currency, event.cash_date, rates
    )
    return converted if event.is_credit else -converted


def resolve_conflicts(events: Iterable[Event]) -> tuple[list[Event], set[str]]:
    """Drop events superseded by another record, in the spec's priority order.

    A later row that links back to an earlier one via ``linked_event_id`` amends or
    replaces it, so the earlier row is dropped when the later one supersedes it in
    the same direction (an amendment), and kept when the later one is a distinct
    movement such as a refund of a purchase.

    Returns (surviving events, dropped event ids).
    """
    by_id = {event.event_id: event for event in events}
    dropped: set[str] = set()
    for event in by_id.values():
        parent_id = event.linked_event_id
        parent = by_id.get(parent_id) if parent_id else None
        if parent is None:
            continue
        if event.direction != parent.direction or event.category != parent.category:
            continue
        # A pending debit of exactly the same amount as the settled charge it links to is a
        # duplicate authorization of money already gone, not a second charge: drop the
        # pending copy (research spec lifecycle table; decided by structure, not description).
        if (
            event.status == STATUS_PENDING
            and parent.status == STATUS_SETTLED
            and event.amount is not None
            and event.amount == parent.amount
        ):
            dropped.add(event.event_id)
            continue
        # Otherwise the later record restates the earlier one.
        if event.status not in IGNORED_STATUSES:
            dropped.add(parent.event_id)
    return [event for event in by_id.values() if event.event_id not in dropped], dropped


def detect_recurring(
    events: Iterable[Event],
    as_of: date,
    profile: Optional[Profile] = None,
    rates: Optional[dict[tuple[date, str, str], Decimal]] = None,
) -> list[RecurringSeries]:
    """Find expense series that repeat on a detectable cadence before `as_of`.

    Credits are skipped: income is classified by description in ``engine.income``,
    because a gap-and-amount test cannot tell an ended payroll from an ongoing one.

    A series is (user, category, description). It must appear at least
    RECURRENCE_MIN_OCCURRENCES times with a consistent interval, and its amount must
    be stable — a fixed line item like rent or a subscription, not a frequent but
    variable category. Categories in VARIABLE_ESSENTIAL_CATEGORIES are never treated
    as scheduled series; they are forecast from an average instead
    (DATASET_FACTS D3).

    When `profile` and `rates` are given, a foreign-currency series is converted to
    the home currency **once, at its last observed date** — a date DATASET_FACTS D2
    guarantees a rate for. Future occurrences are projections, not records, so looking
    up a rate for a date no event settles on would invent data.
    """
    grouped: dict[tuple[str, str, str], list[Event]] = defaultdict(list)
    for event in events:
        if event.is_credit:
            continue
        if event.amount is None or event.cash_date > as_of:
            continue
        if event.category in VARIABLE_ESSENTIAL_CATEGORIES:
            continue
        grouped[event.series_key].append(event)

    series: list[RecurringSeries] = []
    for key, members in grouped.items():
        if len(members) < RECURRENCE_MIN_OCCURRENCES:
            continue
        members.sort(key=lambda item: item.cash_date)
        dates = [item.cash_date for item in members]
        gaps = [days_between(a, b) for a, b in zip(dates, dates[1:])]
        typical = median(gaps)
        if typical is None or typical <= 0:
            continue
        if max(abs(gap - typical) for gap in gaps) > RECURRENCE_CADENCE_TOLERANCE_DAYS:
            continue

        latest = members[-1]
        if RECURRING_EXPENSE_AMOUNT == RECURRING_AMOUNT_LATEST:
            representative = latest.amount  # the most recent amount recurs (research spec §3.1)
        else:
            representative = max(item.amount for item in members if item.amount is not None)
        if profile is not None and rates is not None and latest.currency != profile.home_currency:
            representative = convert(
                representative, latest.currency, profile.home_currency, latest.cash_date, rates
            )

        day_of_month: Optional[int] = None
        period = int(round(typical))
        if abs(typical - RECURRENCE_MONTHLY_DAYS) <= RECURRENCE_CADENCE_TOLERANCE_DAYS:
            day_of_month = dominant_day_of_month(dates)
            period = RECURRENCE_MONTHLY_DAYS
        elif abs(typical - RECURRENCE_WEEKLY_DAYS) <= 1:
            period = RECURRENCE_WEEKLY_DAYS

        series.append(
            RecurringSeries(
                key=key,
                category=latest.category,
                direction=latest.direction,
                amount=representative,
                last_seen=latest.cash_date,
                period_days=period,
                day_of_month=day_of_month,
                occurrences=len(members),
                latest_event_id=latest.event_id,
            )
        )
    return sorted(series, key=lambda item: item.key)


def average_variable_daily_spend(
    events: Iterable[Event], as_of: date
) -> dict[str, Decimal]:
    """Average daily outflow per variable-essential category, from recent history.

    Groceries, transport and dining are frequent but are not fixed line items, so
    they are projected as a smooth daily rate rather than scheduled on specific dates
    (DATASET_FACTS D3). Scaled by VARIABLE_SPEND_CONSERVATISM.
    """
    totals: dict[str, Decimal] = defaultdict(Decimal)
    earliest: dict[str, date] = {}
    for event in events:
        if event.category not in VARIABLE_ESSENTIAL_CATEGORIES:
            continue
        if event.amount is None or not event.is_debit:
            continue
        age = days_between(event.cash_date, as_of)
        if age < 0 or age > VARIABLE_SPEND_LOOKBACK_DAYS:
            continue
        totals[event.category] += event.amount
        if event.category not in earliest or event.cash_date < earliest[event.category]:
            earliest[event.category] = event.cash_date

    daily: dict[str, Decimal] = {}
    for category, total in totals.items():
        span = max(days_between(earliest[category], as_of), 1)
        daily[category] = (
            total / Decimal(span) * Decimal(str(VARIABLE_SPEND_CONSERVATISM))
        )
    return daily


def project_variable_spend(
    events: Iterable[Event], as_of: date, horizon_days: int = FORECAST_HORIZON_DAYS
) -> list[CashFlow]:
    """Projected variable purchases: one series per variable category (DATASET_FACTS I1).

    Every (user, category) variable series in the dataset repeats on one exact gap, and
    descriptions are only labels on those purchases. Each category is projected forward
    from its last settled purchase before `as_of` on its modal gap, at the mean amount of
    its history (scaled by VARIABLE_SPEND_CONSERVATISM). A purchase due exactly on `as_of`
    is treated as already reflected in the balance and not projected.
    """
    groups: dict[str, list[Event]] = defaultdict(list)
    for event in events:
        if event.category not in VARIABLE_ESSENTIAL_CATEGORIES or not event.is_debit or event.amount is None:
            continue
        if event.status != STATUS_SETTLED or event.cash_date >= as_of:
            continue
        groups[event.category].append(event)

    until = as_of + timedelta(days=horizon_days)
    conservatism = Decimal(str(VARIABLE_SPEND_CONSERVATISM))
    flows: list[CashFlow] = []
    for category, rows in sorted(groups.items()):
        rows.sort(key=lambda item: (item.cash_date, item.event_id))
        if len(rows) < 2:
            continue
        gaps = [days_between(a.cash_date, b.cash_date) for a, b in zip(rows, rows[1:])]
        gap = Counter(gaps).most_common(1)[0][0]
        if gap <= 0:
            continue
        amount = sum((row.amount for row in rows), Decimal("0")) / len(rows) * conservatism
        on = rows[-1].cash_date + timedelta(days=gap)
        while on <= until:
            if on > as_of:
                flows.append(CashFlow(on, -amount, category, None, VARIABLE_FLOW_KIND))
            on += timedelta(days=gap)
    return flows


def build_cash_state(
    profile: Profile,
    events: Iterable[Event],
    as_of: date,
    rates: dict[tuple[date, str, str], Decimal],
) -> CashState:
    """Reconstruct `profile`'s cash position as of `as_of`.

    Known future flows are every cash-moving event dated on or after `as_of`;
    recurring series (expenses from ``detect_recurring``, income from
    ``engine.income``) and variable spending cover what history implies but no row
    states. Blank-amount events are listed in ``unresolved_amount_events`` rather
    than silently treated as zero.
    """
    surviving, dropped = resolve_conflicts(events)
    state = CashState(
        user_id=profile.user_id,
        as_of=as_of,
        opening_balance=profile.current_available_balance,
        minimum_balance=profile.minimum_balance_to_keep,
        excluded_event_ids=set(dropped),
    )

    for event in sorted(surviving, key=lambda item: (item.cash_date, item.event_id)):
        if not moves_cash(event):
            state.excluded_event_ids.add(event.event_id)
            continue
        if event.cash_date < as_of:
            continue
        amount = signed_home_amount(event, profile, rates)
        if amount is None:
            state.unresolved_amount_events.append(event.event_id)
            continue
        state.known_flows.append(
            CashFlow(
                on=event.cash_date,
                amount=amount,
                category=event.category,
                source_event_id=event.event_id,
                kind=event.status,
            )
        )

    historical = [event for event in surviving if moves_cash(event)]
    state.recurring = sorted(
        detect_recurring(historical, as_of, profile, rates)
        + detect_income_streams(historical, as_of, profile, rates),
        key=lambda item: item.key,
    )
    if VARIABLE_SPEND_MODEL == VARIABLE_MODEL_CATEGORY_CADENCE:
        state.variable_flows = project_variable_spend(historical, as_of)
    else:
        state.variable_daily_spend = average_variable_daily_spend(historical, as_of)
    return state


def project_recurring(series: RecurringSeries, start: date, until: date) -> list[date]:
    """Dates this series is expected to recur on, within [`start`, `until`].

    Occurrences are generated strictly after the series' last observed date, then
    clipped to the window — so a monthly item last paid a month before `start` and due
    again *on* `start` is included. Dropping it would overstate available cash by a
    whole rent payment.

    Monthly series land on their calendar day-of-month (never last date + 30);
    everything else steps by the detected period from the last occurrence seen.
    """
    if series.is_monthly and series.day_of_month is not None:
        candidates = monthly_occurrences(series.last_seen, until, series.day_of_month)
    else:
        candidates = periodic_occurrences(
            series.last_seen, series.last_seen, until, series.period_days
        )
    return [occurrence for occurrence in candidates if start <= occurrence <= until]
