"""Apply message facts to a reconstructed cash state.

Pure functions: a CashState and message readings in, an adjusted CashState out. No file
reads, no model calls. The model only *read* the messages (``extraction.messages``); every
rule for what a fact does to cash lives here, in code. Evidence per intent is in
DATASET_FACTS G2.

Author decisions (2026-09-13):

* temporary_pay — the stated amount holds from the next payroll onward.
* next_salary_reduced — the stated amount (the earlier, normal level) holds from the next
  payroll onward.
* invoice_approved — projected irregular income stops; only the approved invoice counts,
  on its settlement date.
* stale income — handled in ``engine.income`` (INCOME_STALE_DAYS).

Income changes that start on a date are materialised as dated cash flows over the horizon
and the series is removed, so a level change mid-window is exact. Facts with no cash effect
(refunds pending, prize claims, bonuses, investment values, disputes, transfers with no
matching rows, FX bills) and facts missing the amount they need change nothing, and are
reported as skipped.
"""

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Optional

from common.currency import MissingRateError, convert
from common.dates import add_days
from config import (
    DIRECTION_CREDIT,
    DIRECTION_DEBIT,
    EVIDENCE_CATEGORY_SUFFIX,
    EVIDENCE_FLOW_KIND,
    FORECAST_HORIZON_DAYS,
    INCOME_IRREGULAR_POOL_LABEL,
    RECURRENCE_MONTHLY_DAYS,
    RENT_CATEGORY,
)
from data.records import MessageFact, MessageReading, Profile
from engine.series import RecurringSeries
from engine.state import CashFlow, CashState, project_recurring

Rates = dict[tuple[date, str, str], Decimal]

INCOME_ENDED_INTENTS = frozenset({"employment_ended", "seasonal_contract_ended"})
LEVEL_FROM_NEXT_PAYROLL_INTENTS = frozenset({"temporary_pay", "next_salary_reduced"})
SALARY_FROM_DATE_INTENTS = frozenset({"first_salary", "salary_resumes_with_childcare", "fx_salary_confirmed"})


@dataclass(frozen=True)
class EvidenceResult:
    """The adjusted state plus a trace of what each fact did — for explanations and audit."""

    state: CashState
    applied: tuple[str, ...]
    skipped: tuple[str, ...]


@dataclass
class _Stream:
    """A working copy of one income series while facts are applied."""

    series: RecurringSeries
    levels: list[tuple[date, Decimal]] = field(default_factory=list)
    next_amount: Optional[Decimal] = None

    @property
    def is_pool(self) -> bool:
        return self.series.key[2] == INCOME_IRREGULAR_POOL_LABEL


def _home_amount(fact: MessageFact, profile: Profile, rates: Rates, on: date) -> Optional[Decimal]:
    """The fact's amount in home currency, converted at `on` (D2). None when unusable."""
    if fact.amount is None:
        return None
    if not fact.currency or fact.currency == profile.home_currency:
        return fact.amount
    try:
        return convert(fact.amount, fact.currency, profile.home_currency, on, rates)
    except MissingRateError:
        return None


def _main_stable(streams: list[_Stream]) -> Optional[_Stream]:
    """The user's principal salary stream: most occurrences, then most recently seen."""
    stable = [stream for stream in streams if not stream.is_pool]
    return max(stable, key=lambda item: (item.series.occurrences, item.series.last_seen), default=None)


def _new_stream(profile: Profile, message_id: str, on: date, amount: Decimal) -> _Stream:
    """A monthly salary stream that starts on `on`, for a user with no stream to adjust."""
    return _Stream(
        RecurringSeries(
            key=(profile.user_id, "salary", f"message {message_id}"),
            category="salary",
            direction=DIRECTION_CREDIT,
            amount=amount,
            last_seen=on - timedelta(days=1),
            period_days=RECURRENCE_MONTHLY_DAYS,
            day_of_month=on.day,
            occurrences=0,
            latest_event_id=message_id,
        )
    )


def _apply_fact(
    fact: MessageFact,
    message_id: str,
    streams: list[_Stream],
    expenses: list[RecurringSeries],
    flows: list[CashFlow],
    profile: Profile,
    rates: Rates,
    as_of: date,
    until: date,
) -> tuple[Optional[str], list[_Stream], list[RecurringSeries]]:
    """Apply one fact. Returns (note or None when skipped, streams, expenses)."""
    intent = fact.intent
    label = f"{message_id}:{intent}"

    if intent in INCOME_ENDED_INTENTS:
        return f"{label} stopped all projected income", [], expenses

    if intent == "payout_pending":
        return f"{label} stopped projected irregular income", [s for s in streams if not s.is_pool], expenses

    if intent == "rent_increase_pct":
        if fact.percent is None:
            return None, streams, expenses
        factor = 1 + fact.percent / 100
        adjusted = [
            replace(item, amount=item.amount * factor)
            if item.category == RENT_CATEGORY and item.direction == DIRECTION_DEBIT
            else item
            for item in expenses
        ]
        return f"{label} raised rent by {fact.percent}%", streams, adjusted

    if intent == "invoice_approved":
        if fact.effective_date is None:
            return None, streams, expenses
        amount = _home_amount(fact, profile, rates, fact.effective_date)
        if amount is None:
            return None, streams, expenses
        if as_of <= fact.effective_date <= until:
            flows.append(CashFlow(fact.effective_date, amount, "invoice" + EVIDENCE_CATEGORY_SUFFIX, message_id, EVIDENCE_FLOW_KIND))
        return f"{label} counted {amount} on {fact.effective_date}, stopped projected irregular income", [s for s in streams if not s.is_pool], expenses

    main = _main_stable(streams)

    if intent == "payday_moved":
        if fact.effective_date is None or main is None:
            return None, streams, expenses
        main.series = replace(main.series, day_of_month=fact.effective_date.day, period_days=RECURRENCE_MONTHLY_DAYS)
        return f"{label} moved payday to day {fact.effective_date.day}", streams, expenses

    amount = _home_amount(fact, profile, rates, fact.effective_date or as_of)
    if amount is None:
        return None, streams, expenses

    if intent in LEVEL_FROM_NEXT_PAYROLL_INTENTS:
        if main is None:
            return None, streams, expenses
        main.levels.append((as_of, amount))
        return f"{label} set salary to {amount} from the next payroll", streams, expenses

    if intent == "base_salary_commission_pending":
        if main is None:
            return None, streams, expenses
        main.levels.append((as_of, amount))
        return f"{label} set base salary to {amount}, stopped projected commission", [s for s in streams if not s.is_pool], expenses

    if intent == "remaining_household_salary":
        if main is None:
            return None, streams, expenses
        main.levels.append((as_of, amount))
        kept = [stream for stream in streams if stream is main or stream.is_pool]
        return f"{label} set household salary to {amount}, ended other salary streams", kept, expenses

    if intent == "salary_next_with_arrears":
        if main is None:
            return None, streams, expenses
        main.levels.append((as_of, amount))
        main.next_amount = amount + (fact.secondary_amount or Decimal("0"))
        return f"{label} next salary {main.next_amount}, then {amount}", streams, expenses

    if intent == "salary_increase" or intent in SALARY_FROM_DATE_INTENTS:
        if fact.effective_date is None:
            return None, streams, expenses
        if main is None:
            return f"{label} started salary {amount} on {fact.effective_date}", streams + [_new_stream(profile, message_id, fact.effective_date, amount)], expenses
        if intent in SALARY_FROM_DATE_INTENTS and main.series.is_monthly:
            main.series = replace(main.series, day_of_month=fact.effective_date.day)
        main.levels.append((fact.effective_date, amount))
        return f"{label} salary {amount} from {fact.effective_date}", streams, expenses

    return None, streams, expenses


def _materialise(stream: _Stream, as_of: date, until: date) -> list[CashFlow]:
    """Dated flows for a stream whose amount changes, over [as_of, until]."""
    flows: list[CashFlow] = []
    for index, on in enumerate(project_recurring(stream.series, as_of, until)):
        amount = stream.series.amount
        for start, level in stream.levels:
            if on >= start:
                amount = level
        if index == 0 and stream.next_amount is not None:
            amount = stream.next_amount
        flows.append(
            CashFlow(on, amount, stream.series.category + EVIDENCE_CATEGORY_SUFFIX, stream.series.latest_event_id, EVIDENCE_FLOW_KIND)
        )
    return flows


def apply_message_facts(
    state: CashState,
    readings: Iterable[MessageReading],
    profile: Profile,
    rates: Rates,
    horizon_days: int = FORECAST_HORIZON_DAYS,
) -> EvidenceResult:
    """Return a new state with every applicable message fact applied; `state` is untouched.

    The caller passes only readings for this user sent on or before ``state.as_of``.
    Instruction attempts contribute nothing.
    """
    as_of = state.as_of
    until = add_days(as_of, horizon_days)
    streams = [_Stream(item) for item in state.recurring if item.direction == DIRECTION_CREDIT]
    expenses = [item for item in state.recurring if item.direction != DIRECTION_CREDIT]
    flows = list(state.known_flows)
    applied: list[str] = []
    skipped: list[str] = []

    for reading in readings:
        if reading.is_instruction_attempt:
            skipped.append(f"{reading.message_id}: instruction attempt, ignored")
            continue
        for fact in reading.facts:
            note, streams, expenses = _apply_fact(fact, reading.message_id, streams, expenses, flows, profile, rates, as_of, until)
            if note is None:
                skipped.append(f"{reading.message_id}:{fact.intent} no cash effect")
            else:
                applied.append(note)

    recurring = list(expenses)
    for stream in streams:
        if stream.levels or stream.next_amount is not None:
            flows.extend(_materialise(stream, as_of, until))
        else:
            recurring.append(stream.series)

    adjusted = replace(
        state,
        recurring=sorted(recurring, key=lambda item: item.key),
        known_flows=sorted(flows, key=lambda item: (item.on, item.category)),
    )
    return EvidenceResult(adjusted, tuple(applied), tuple(skipped))
