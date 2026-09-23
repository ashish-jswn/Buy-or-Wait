"""Plan generation and ranking: which way of paying to recommend, and the resulting status.

Pure functions: a request, profile, payment options, forecast and safety assessment in; a
Decision out. No I/O, no model calls.

Candidates, each built only when the user accepts the method and every payment keeps the
projected balance at or above the floor:

* full payment today  — when today's safe amount covers the request (``affordable_now``)
* installments        — each supplied option within ``max_installment_months``
* partial payment     — today's safe amount now, the rest on the earliest full-payment date
* wait                — one full payment on the earliest full-payment date (``affordable_later``)

Author decision (2026-09-13): a plan whose last payment falls after
``desired_completion_date`` is not eligible at all. Among eligible plans the order is the
problem statement's: no spending changes, lowest total paid, earliest start, fewest
payments, lowest ``payment_option_id``. With nothing eligible the request is
``not_affordable``. Spending changes arrive in a later part; the ranking key already
accounts for them.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable, Optional

from common.dates import days_between
from common.formatting import round_money
from config import (
    METHOD_FULL_PAYMENT,
    METHOD_INSTALLMENTS,
    METHOD_NOT_RECOMMENDED,
    METHOD_PARTIAL_PAYMENT,
    METHOD_WAIT,
    MONEY_TOLERANCE,
    STATUS_AFFORDABLE_LATER,
    STATUS_AFFORDABLE_NOW,
    STATUS_AFFORDABLE_WITH_PLAN,
    STATUS_NOT_AFFORDABLE,
)
from data.records import PaymentOption, Profile, Request
from engine.forecast import Forecast, SafetyAssessment

Payment = tuple[date, Decimal]
ZERO = Decimal("0")


@dataclass(frozen=True)
class PlanCandidate:
    """One way of paying, already checked safe."""

    method: str
    payments: tuple[Payment, ...]
    total_paid: Decimal
    payment_option_id: str = ""
    spending_changes: tuple[str, ...] = ()

    @property
    def first_date(self) -> date:
        return self.payments[0][0]

    @property
    def last_date(self) -> date:
        return self.payments[-1][0]

    @property
    def number_of_payments(self) -> int:
        return len(self.payments)


@dataclass(frozen=True)
class Decision:
    """Everything the output row needs, plus the candidates considered (for audit)."""

    amount_safe_to_pay: Decimal
    affordability_status: str
    recommended_payment_method: str
    payments: tuple[Payment, ...]
    earliest_date_for_full_payment: Optional[date]
    chosen: Optional[PlanCandidate]
    candidates: tuple[PlanCandidate, ...]


def schedule_is_safe(forecast: Forecast, payments: Iterable[Payment], last_offset: Optional[int] = None) -> bool:
    """True when every payment lands in the window and the balance never drops below the
    floor on any checked day, after all payments made so far. Allows MONEY_TOLERANCE for
    cent rounding of a partial first payment. `last_offset` limits the checked days
    (config PLAN_CHECK_WINDOW); None checks the whole window."""
    paid_on = [ZERO] * (forecast.horizon_days + 1)
    for on, amount in payments:
        offset = days_between(forecast.start, on)
        if offset < 0 or offset > forecast.horizon_days:
            return False
        paid_on[offset] += amount
    tolerance = Decimal(str(MONEY_TOLERANCE))
    paid = ZERO
    end = forecast.horizon_days if last_offset is None else max(0, min(last_offset, forecast.horizon_days))
    for offset in range(end + 1):
        paid += paid_on[offset]
        if forecast.balance_on(offset) - paid < forecast.floor - tolerance:
            return False
    return True


def full_payment_candidate(request: Request, profile: Profile, assessment: SafetyAssessment) -> Optional[PlanCandidate]:
    """Pay everything today, when accepted and today's safe amount covers it."""
    if not profile.accepts(METHOD_FULL_PAYMENT) or assessment.amount_safe_to_pay < request.requested_amount:
        return None
    return PlanCandidate(METHOD_FULL_PAYMENT, ((request.request_date, request.requested_amount),), request.requested_amount)


def installment_candidates(
    request: Request, profile: Profile, options: Iterable[PaymentOption], forecast: Forecast,
    last_offset: Optional[int] = None,
) -> list[PlanCandidate]:
    """Every supplied installment option the user accepts, within the cap, by the deadline, and safe."""
    if not profile.accepts(METHOD_INSTALLMENTS) or profile.max_installment_months is None:
        return []
    candidates: list[PlanCandidate] = []
    for option in options:
        if option.payment_method != METHOD_INSTALLMENTS:
            continue
        if option.number_of_payments > profile.max_installment_months:
            continue
        schedule = tuple(option.schedule())
        if not schedule or schedule[-1][0] > request.desired_completion_date:
            continue
        if not schedule_is_safe(forecast, schedule, last_offset):
            continue
        candidates.append(
            PlanCandidate(METHOD_INSTALLMENTS, schedule, option.total_payable_amount, option.payment_option_id)
        )
    return candidates


def partial_payment_candidate(
    request: Request, profile: Profile, forecast: Forecast, assessment: SafetyAssessment,
    last_offset: Optional[int] = None,
) -> Optional[PlanCandidate]:
    """Safe amount today, remainder on the earliest full-payment date."""
    if not (request.allows_partial_payment and profile.accepts(METHOD_PARTIAL_PAYMENT)):
        return None
    earliest = assessment.earliest_full_payment
    if earliest is None or not request.request_date < earliest <= request.desired_completion_date:
        return None
    first = round_money(assessment.amount_safe_to_pay)
    if not ZERO < first < request.requested_amount:
        return None
    schedule = ((request.request_date, first), (earliest, request.requested_amount - first))
    if not schedule_is_safe(forecast, schedule, last_offset):
        return None
    return PlanCandidate(METHOD_PARTIAL_PAYMENT, schedule, request.requested_amount)


def wait_candidate(request: Request, profile: Profile, assessment: SafetyAssessment) -> Optional[PlanCandidate]:
    """One full payment later, on the earliest safe date, when that is by the deadline."""
    earliest = assessment.earliest_full_payment
    if not profile.accepts(METHOD_FULL_PAYMENT) or earliest is None:
        return None
    if not request.request_date < earliest <= request.desired_completion_date:
        return None
    return PlanCandidate(METHOD_WAIT, ((earliest, request.requested_amount),), request.requested_amount)


def rank_key(candidate: PlanCandidate, deadline: date) -> tuple:
    """The problem statement's order: by deadline, no changes, cheapest, earliest, fewest, lowest id."""
    return (
        candidate.last_date > deadline,
        bool(candidate.spending_changes),
        candidate.total_paid,
        candidate.first_date,
        candidate.number_of_payments,
        candidate.payment_option_id,
    )


def status_for(candidate: PlanCandidate) -> str:
    """affordable_now only for an unaided full payment today; wait is later; the rest need a plan."""
    if candidate.method == METHOD_WAIT:
        return STATUS_AFFORDABLE_LATER
    if candidate.method == METHOD_FULL_PAYMENT and not candidate.spending_changes:
        return STATUS_AFFORDABLE_NOW
    return STATUS_AFFORDABLE_WITH_PLAN


def decide(
    request: Request,
    profile: Profile,
    options: Iterable[PaymentOption],
    forecast: Forecast,
    assessment: SafetyAssessment,
    extra_candidates: Iterable[PlanCandidate] = (),
    check_until: Optional[date] = None,
) -> Decision:
    """Build every candidate, keep those finishing by the deadline, and pick the best.

    `extra_candidates` lets a later stage (spending changes) add plans that are ranked in
    the same order. ``earliest_date_for_full_payment`` is the request date when
    ``affordable_now``, blank when ``not_affordable``, and otherwise the unaided date.
    """
    last_offset = None if check_until is None else days_between(forecast.start, check_until)
    built = [
        full_payment_candidate(request, profile, assessment),
        *installment_candidates(request, profile, options, forecast, last_offset),
        partial_payment_candidate(request, profile, forecast, assessment, last_offset),
        wait_candidate(request, profile, assessment),
        *extra_candidates,
    ]
    candidates = tuple(candidate for candidate in built if candidate is not None)
    deadline = request.desired_completion_date
    eligible = [candidate for candidate in candidates if candidate.last_date <= deadline]
    if not eligible:
        return Decision(
            assessment.amount_safe_to_pay, STATUS_NOT_AFFORDABLE, METHOD_NOT_RECOMMENDED, (), None, None, candidates
        )
    chosen = min(eligible, key=lambda candidate: rank_key(candidate, deadline))
    status = status_for(chosen)
    earliest = request.request_date if status == STATUS_AFFORDABLE_NOW else assessment.earliest_full_payment
    return Decision(
        assessment.amount_safe_to_pay, status, chosen.method, chosen.payments, earliest, chosen, candidates
    )
