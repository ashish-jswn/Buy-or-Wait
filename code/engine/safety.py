"""Safety check for critical cases: a recommendation that only just holds is re-tested.

Pure functions, no I/O, no model calls. Author decisions (2026-09-13):

* a plan is *near the floor* when its lowest projected balance, after every plan payment
  (and any chosen spending changes), is within NEAR_FLOOR_FRACTION × minimum_balance_to_keep
  of the floor;
* such a plan is re-tested with variable spending scaled by STRESS_VARIABLE_MULTIPLIER;
* if the stressed run no longer recommends the same plan, the stressed decision replaces
  it automatically — it can only move toward caution, never toward a more permissive
  status — and a trace records why.

Not stressed (measured, left to the author): income raised by a message. 69 of 149 full-set
approvals depend on it, and the problem statement says confirmed salary counts on its date.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Mapping, Optional

from common.dates import days_between
from config import (
    METHOD_PARTIAL_PAYMENT,
    NEAR_FLOOR_FRACTION,
    STATUS_AFFORDABLE_LATER,
    STATUS_AFFORDABLE_NOW,
    STATUS_AFFORDABLE_WITH_PLAN,
    STATUS_NOT_AFFORDABLE,
    STRESS_VARIABLE_MULTIPLIER,
)
from data.records import Dataset, MessageReading, Request
from engine.forecast import Forecast
import config as safety_config
from config import MESSAGE_INCOME_STRESS_ALL, MESSAGE_INCOME_STRESS_NEAR_FLOOR
from engine.pipeline import (
    plan_check_last_offset,
    decide_scenario,
    full_payment_with_required_change,
    has_income_raising_facts,
    without_income_raising_facts,
)
from engine.plans import Decision

# Higher is more permissive. The safety check may only move a decision down this scale.
PERMISSIVENESS = {
    STATUS_NOT_AFFORDABLE: 0,
    STATUS_AFFORDABLE_LATER: 1,
    STATUS_AFFORDABLE_WITH_PLAN: 2,
    STATUS_AFFORDABLE_NOW: 3,
}
FLAG_NEAR_FLOOR = "near_floor"
FLAG_MARGINAL_PAY_NOW = "marginal_pay_now"
FLAG_MESSAGE_INCOME = "message_raised_income"
OUTCOME_MESSAGE_INCOME = "downgraded_without_message_income"
OUTCOME_NOT_CHECKED = "not_checked"
OUTCOME_HELD = "held_under_stress"
OUTCOME_DOWNGRADED = "downgraded"
OUTCOME_REQUIRED_CHANGE = "required_spending_change"


@dataclass(frozen=True)
class SafetyTrace:
    """Why a row was or was not changed by the safety check. Never written to output.csv."""

    request_id: str
    flags: tuple[str, ...]
    margin: Optional[Decimal]
    threshold: Decimal
    outcome: str
    before: tuple[str, str, str]
    after: tuple[str, str, str]


def plan_margin(forecast: Forecast, payments: Iterable[tuple], last_offset: Optional[int] = None) -> Decimal:
    """Lowest (balance − plan payments so far − floor) over the checked days. Negative means unsafe."""
    paid_on = [Decimal("0")] * (forecast.horizon_days + 1)
    for on, amount in payments:
        offset = days_between(forecast.start, on)
        if 0 <= offset <= forecast.horizon_days:
            paid_on[offset] += amount
    paid, lowest = Decimal("0"), None
    end = forecast.horizon_days if last_offset is None else max(0, min(last_offset, forecast.horizon_days))
    for offset in range(end + 1):
        paid += paid_on[offset]
        margin = forecast.balance_on(offset) - paid - forecast.floor
        lowest = margin if lowest is None or margin < lowest else lowest
    return lowest


def _signature(decision: Decision) -> tuple:
    """What counts as 'the same plan'."""
    changes = decision.chosen.spending_changes if decision.chosen is not None else ()
    return (decision.affordability_status, decision.recommended_payment_method, decision.payments, changes)


def _summary(decision: Decision) -> tuple[str, str, str]:
    changes = decision.chosen.spending_changes if decision.chosen is not None else ()
    plan = "|".join(f"{on}:{amount}" for on, amount in decision.payments) or "none"
    return (decision.affordability_status, decision.recommended_payment_method, plan + (" +" + "|".join(changes) if changes else ""))


def safe_decide_request(
    request: Request,
    dataset: Dataset,
    readings: Optional[Mapping[str, MessageReading]] = None,
    image_amounts: Optional[Mapping[str, Decimal]] = None,
) -> tuple[Decision, SafetyTrace]:
    """Decide, then re-test a near-floor recommendation under stress; return the safer one."""
    profile = dataset.profiles[request.user_id]
    threshold = profile.minimum_balance_to_keep * Decimal(str(NEAR_FLOOR_FRACTION))
    base = decide_scenario(request, dataset, readings, image_amounts)
    decision = base.decision
    if decision.affordability_status == STATUS_NOT_AFFORDABLE:
        return decision, SafetyTrace(request.request_id, (), None, threshold, OUTCOME_NOT_CHECKED, _summary(decision), _summary(decision))

    # A partial-payment plan pays exactly the safe amount today, so its margin is ~0 by
    # construction, not by risk; stressing it only lowers the first payment (measured:
    # request_19 moved away from gold). It is left as decided (C2, adopted 2026-09-13).
    if decision.recommended_payment_method == METHOD_PARTIAL_PAYMENT:
        return decision, SafetyTrace(request.request_id, (), None, threshold, OUTCOME_NOT_CHECKED, _summary(decision), _summary(decision))

    margin = plan_margin(base.plan_forecast, decision.payments, plan_check_last_offset(request))

    # Message-raised income stress (author decision). Re-decide without the facts that
    # raised or created income; a changed, not-more-permissive decision replaces the plan.
    scope = safety_config.MESSAGE_INCOME_STRESS_SCOPE
    in_scope = scope == MESSAGE_INCOME_STRESS_ALL or (scope == MESSAGE_INCOME_STRESS_NEAR_FLOOR and margin < threshold)
    if in_scope and has_income_raising_facts(request, dataset, readings):
        without = decide_scenario(request, dataset, without_income_raising_facts(readings), image_amounts).decision
        if _signature(without) != _signature(decision) and PERMISSIVENESS[without.affordability_status] <= PERMISSIVENESS[decision.affordability_status]:
            return without, SafetyTrace(
                request.request_id, (FLAG_MESSAGE_INCOME,), margin, threshold, OUTCOME_MESSAGE_INCOME,
                _summary(decision), _summary(without),
            )

    if margin >= threshold:
        return decision, SafetyTrace(request.request_id, (), margin, threshold, OUTCOME_NOT_CHECKED, _summary(decision), _summary(decision))

    stressed = decide_scenario(
        request, dataset, readings, image_amounts, variable_multiplier=Decimal(str(STRESS_VARIABLE_MULTIPLIER))
    ).decision
    flags = (FLAG_NEAR_FLOOR,)
    if _signature(stressed) != _signature(decision) and PERMISSIVENESS[stressed.affordability_status] <= PERMISSIVENESS[decision.affordability_status]:
        return stressed, SafetyTrace(request.request_id, flags, margin, threshold, OUTCOME_DOWNGRADED, _summary(decision), _summary(stressed))

    # Marginal pay-now rule (author decision): a full payment today that only just clears
    # the floor becomes full payment plus the smallest permitted spending change that
    # clears it by the threshold — the shape gold uses on request_06 and request_21.
    if decision.affordability_status == STATUS_AFFORDABLE_NOW:
        with_change = full_payment_with_required_change(request, dataset, readings, image_amounts, threshold)
        if with_change is not None:
            return with_change, SafetyTrace(
                request.request_id, flags + (FLAG_MARGINAL_PAY_NOW,), margin, threshold, OUTCOME_REQUIRED_CHANGE,
                _summary(decision), _summary(with_change),
            )
    return decision, SafetyTrace(request.request_id, flags, margin, threshold, OUTCOME_HELD, _summary(decision), _summary(decision))
