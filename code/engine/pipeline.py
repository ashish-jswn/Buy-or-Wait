"""One request, end to end, as a pure function over already-loaded data.

image amounts → state → message facts → forecast → plans → (spending changes when nothing
else works). No file reads, no model calls: message readings and image amounts are passed
in, already extracted. ``decide_scenario`` also accepts a variable-spend multiplier so the
safety check (``engine.safety``) can re-run the same logic under stress.
"""

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Mapping, Optional

import config as pipeline_config
from common.dates import days_between
from config import (
    INCOME_RAISING_INTENTS,
    METHOD_FULL_PAYMENT,
    PLAN_CHECK_WINDOW_DEADLINE,
    SPENDING_CHANGES_NONE,
    STATUS_AFFORDABLE_WITH_PLAN,
    STATUS_NOT_AFFORDABLE,
)
from data.records import Dataset, Event, MessageReading, PaymentOption, Profile, Request
from engine.evidence import apply_message_facts
from engine.forecast import Forecast, SafetyAssessment, assess, build_forecast
from engine.plans import Decision, decide
from engine.spending import eligible_changes, find_spending_changes, spending_change_candidate
from engine.state import CashState, build_cash_state


class UnresolvedAmountError(RuntimeError):
    """A blank-amount event that moves cash in the forecast has no readable amount.

    Never treated as zero (author decision): the caller replaces the row and counts it.
    """


@dataclass(frozen=True)
class ScenarioResult:
    """A decision plus the forecast its chosen plan was checked against.

    ``plan_forecast`` includes the savings of any chosen spending changes, so the plan's
    true margin above the floor can be measured.
    """

    decision: Decision
    plan_forecast: Forecast


@dataclass(frozen=True)
class ScenarioInputs:
    """Everything a decision is computed from, for one request under one scenario."""

    profile: Profile
    events: list[Event]
    state: CashState
    forecast: Forecast
    assessment: SafetyAssessment
    options: list[PaymentOption]
    check_until: Optional[date]


def plan_check_until(request: Request) -> Optional[date]:
    """The last day plans and the earliest date are checked (config PLAN_CHECK_WINDOW)."""
    if pipeline_config.PLAN_CHECK_WINDOW == PLAN_CHECK_WINDOW_DEADLINE:
        return request.desired_completion_date
    return None


def plan_check_last_offset(request: Request) -> Optional[int]:
    """``plan_check_until`` as a day offset from request_date, or None for the whole window."""
    until = plan_check_until(request)
    return None if until is None else days_between(request.request_date, until)


def readings_for(request: Request, dataset: Dataset, readings: Mapping[str, MessageReading]) -> list[MessageReading]:
    """This request's user's message readings, sent on or before request_date."""
    return [
        readings[message.message_id]
        for message in dataset.messages_for(request.user_id)
        if message.message_id in readings and message.sent_at[:10] <= request.request_date.isoformat()
    ]


def without_income_raising_facts(readings: Mapping[str, MessageReading]) -> dict[str, MessageReading]:
    """The same readings with every income-raising fact removed (message-income stress)."""
    return {
        message_id: replace(reading, facts=tuple(fact for fact in reading.facts if fact.intent not in INCOME_RAISING_INTENTS))
        for message_id, reading in readings.items()
    }


def has_income_raising_facts(request: Request, dataset: Dataset, readings: Optional[Mapping[str, MessageReading]]) -> bool:
    """True when this request's user has a message fact that raises or creates income."""
    if not readings:
        return False
    return any(fact.intent in INCOME_RAISING_INTENTS for reading in readings_for(request, dataset, readings) for fact in reading.facts)


def prepare_scenario(
    request: Request,
    dataset: Dataset,
    readings: Optional[Mapping[str, MessageReading]] = None,
    image_amounts: Optional[Mapping[str, Decimal]] = None,
    variable_multiplier: Decimal = Decimal("1"),
) -> ScenarioInputs:
    """Build state, forecast and assessment for one scenario.

    `image_amounts` fills blank event amounts before anything else reads them. Raises
    UnresolvedAmountError when a blank-amount event still moves cash in the forecast.
    """
    profile = dataset.profiles[request.user_id]
    amounts = image_amounts or {}
    events = [
        replace(event, amount=amounts[event.event_id]) if event.amount is None and event.event_id in amounts else event
        for event in dataset.events_for(request.user_id)
    ]
    state = build_cash_state(profile, events, request.request_date, dataset.rates)
    if state.unresolved_amount_events:
        raise UnresolvedAmountError(f"no readable amount for {', '.join(state.unresolved_amount_events)}")
    if variable_multiplier != 1:
        state = replace(
            state,
            variable_daily_spend={key: value * variable_multiplier for key, value in state.variable_daily_spend.items()},
            variable_flows=[replace(flow, amount=flow.amount * variable_multiplier) for flow in state.variable_flows],
        )
    if readings:
        state = apply_message_facts(state, readings_for(request, dataset, readings), profile, dataset.rates).state
    check_until = plan_check_until(request)
    return ScenarioInputs(
        profile, events, state, build_forecast(state),
        assess(state, request.requested_amount, check_until=check_until),
        dataset.options_by_request.get(request.request_id, []), check_until,
    )


def decide_scenario(
    request: Request,
    dataset: Dataset,
    readings: Optional[Mapping[str, MessageReading]] = None,
    image_amounts: Optional[Mapping[str, Decimal]] = None,
    variable_multiplier: Decimal = Decimal("1"),
) -> ScenarioResult:
    """The full deterministic decision under one scenario.

    Spending changes are searched only when no plan without them is eligible and the user
    accepts full payment (DATASET_FACTS A6).
    """
    inputs = prepare_scenario(request, dataset, readings, image_amounts, variable_multiplier)
    profile, state, forecast, assessment, options = inputs.profile, inputs.state, inputs.forecast, inputs.assessment, inputs.options
    last_offset = plan_check_last_offset(request)
    decision = decide(request, profile, options, forecast, assessment, check_until=inputs.check_until)
    plan_forecast = forecast
    if decision.affordability_status == STATUS_NOT_AFFORDABLE and profile.accepts(METHOD_FULL_PAYMENT):
        combo = find_spending_changes(
            state, eligible_changes(state, inputs.events, profile, dataset.rates), request, last_offset=last_offset
        )
        if combo:
            extra = [spending_change_candidate(request, combo)]
            decision = decide(request, profile, options, forecast, assessment, extra_candidates=extra, check_until=inputs.check_until)
            if decision.chosen is not None and decision.chosen.spending_changes:
                savings = [flow for change in combo for flow in change.savings]
                plan_forecast = build_forecast(replace(state, known_flows=list(state.known_flows) + savings))
    return ScenarioResult(decision, plan_forecast)


def full_payment_with_required_change(
    request: Request,
    dataset: Dataset,
    readings: Optional[Mapping[str, MessageReading]],
    image_amounts: Optional[Mapping[str, Decimal]],
    required_margin: Decimal,
) -> Optional[Decision]:
    """Full payment today enabled by the smallest permitted change that clears the floor by
    `required_margin`; None when no such change exists.

    The earliest date and amount_safe_to_pay stay the unaided values (before changes).
    """
    inputs = prepare_scenario(request, dataset, readings, image_amounts)
    combo = find_spending_changes(
        inputs.state, eligible_changes(inputs.state, inputs.events, inputs.profile, dataset.rates), request,
        required_margin, plan_check_last_offset(request),
    )
    if not combo:
        return None
    candidate = spending_change_candidate(request, combo)
    return Decision(
        inputs.assessment.amount_safe_to_pay,
        STATUS_AFFORDABLE_WITH_PLAN,
        METHOD_FULL_PAYMENT,
        candidate.payments,
        inputs.assessment.earliest_full_payment,
        candidate,
        (candidate,),
    )


def decide_request(
    request: Request,
    dataset: Dataset,
    readings: Optional[Mapping[str, MessageReading]] = None,
    image_amounts: Optional[Mapping[str, Decimal]] = None,
) -> Decision:
    """The unstressed decision for one request (see ``decide_scenario``)."""
    return decide_scenario(request, dataset, readings, image_amounts).decision


def spending_changes_text(decision: Decision) -> str:
    """The spending_changes_needed column for a decision."""
    if decision.chosen is not None and decision.chosen.spending_changes:
        return "|".join(decision.chosen.spending_changes)
    return SPENDING_CHANGES_NONE
