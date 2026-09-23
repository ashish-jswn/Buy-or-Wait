"""Tests for engine.plans and output.rows on synthetic forecasts. No API key, no data files."""

from datetime import date, timedelta
from decimal import Decimal

from data.records import PaymentOption, Profile, Request
from engine.forecast import (
    Forecast,
    SafetyAssessment,
    amount_safe_to_pay,
    earliest_full_payment_date,
    headroom,
)
from engine.plans import PlanCandidate, decide, rank_key, schedule_is_safe
from output.rows import build_output_row, format_payment_plan

START = date(2025, 1, 1)


def day(offset: int) -> date:
    return START + timedelta(days=offset)


def forecast(opening=1000, floor=100, flows=None, horizon=90) -> Forecast:
    daily = [Decimal("0")] * (horizon + 1)
    for offset, amount in (flows or {}).items():
        daily[offset] += Decimal(str(amount))
    cumulative, running = [], Decimal("0")
    for value in daily:
        running += value
        cumulative.append(running)
    return Forecast(START, horizon, Decimal(str(opening)), Decimal(str(floor)), daily, cumulative)


def assessment(fc: Forecast, requested) -> SafetyAssessment:
    requested = Decimal(str(requested))
    return SafetyAssessment(
        amount_safe_to_pay(fc, requested), earliest_full_payment_date(fc, requested), headroom(fc), fc.worst_dip(), fc.trough_balance(), fc.floor
    )


def request(amount, deadline_offset=30, partial=False) -> Request:
    return Request("request_t", "user_t", START, "purchase", Decimal(str(amount)), day(deadline_offset), partial, "")


def profile(*methods, max_months=None) -> Profile:
    return Profile("user_t", "EUR", Decimal("0"), Decimal("0"), (), (), (), (), tuple(methods), max_months)


def option(option_id, count, amount, first_offset, every=30, total=None) -> PaymentOption:
    amount = Decimal(str(amount))
    total = Decimal(str(total)) if total is not None else amount * count
    return PaymentOption(option_id, "request_t", "installments", amount, count, day(first_offset), every, total - amount * count, total)


def run(req, prof, fc, options=()):
    return decide(req, prof, options, fc, assessment(fc, req.requested_amount))


# --------------------------------------------------------------------------
# safety of a schedule
# --------------------------------------------------------------------------


def test_schedule_is_safe_checks_every_day_after_each_payment() -> None:
    fc = forecast(opening=500, floor=100, flows={14: 1000})
    assert schedule_is_safe(fc, [(day(0), Decimal("400"))])
    assert not schedule_is_safe(fc, [(day(0), Decimal("401"))])
    assert schedule_is_safe(fc, [(day(0), Decimal("400")), (day(14), Decimal("1000"))])


def test_a_limited_check_window_ignores_dips_after_its_end() -> None:
    """PLAN_CHECK_WINDOW=deadline: a dip after the last checked day does not fail the plan."""
    fc = forecast(opening=1000, floor=100, flows={40: -500})
    payments = [(day(0), Decimal("600"))]
    assert not schedule_is_safe(fc, payments)
    assert schedule_is_safe(fc, payments, last_offset=30)


def test_a_payment_outside_the_window_is_never_safe() -> None:
    assert not schedule_is_safe(forecast(), [(day(91), Decimal("1"))])


# --------------------------------------------------------------------------
# each method
# --------------------------------------------------------------------------


def test_full_payment_today_when_safe_and_accepted() -> None:
    decision = run(request(500), profile("full_payment"), forecast())
    assert (decision.affordability_status, decision.recommended_payment_method) == ("affordable_now", "full_payment")
    assert decision.payments == ((START, Decimal("500")),)
    assert decision.earliest_date_for_full_payment == START


def test_installments_when_full_is_safe_but_not_accepted() -> None:
    """request_12's shape: earliest date is today, recommendation is installments."""
    decision = run(request(500, 90), profile("installments", max_months=6), forecast(), [option("payment_option_1", 3, 170, 3)])
    assert decision.recommended_payment_method == "installments"
    assert decision.earliest_date_for_full_payment == START


def test_installment_option_above_the_month_cap_is_excluded() -> None:
    decision = run(request(500, 400), profile("installments", max_months=6), forecast(horizon=400), [option("payment_option_1", 12, 45, 3)])
    assert decision.affordability_status == "not_affordable"


def test_installment_option_finishing_after_the_deadline_is_excluded() -> None:
    decision = run(request(500, 30), profile("installments", max_months=6), forecast(), [option("payment_option_1", 3, 170, 3)])
    assert decision.affordability_status == "not_affordable"
    assert decision.earliest_date_for_full_payment is None


def test_unsafe_installment_option_is_excluded() -> None:
    decision = run(request(450, 80), profile("installments", max_months=6), forecast(opening=300), [option("payment_option_1", 3, 150, 0)])
    assert decision.affordability_status == "not_affordable"


def test_wait_until_the_earliest_safe_date() -> None:
    decision = run(request(800), profile("full_payment"), forecast(opening=500, flows={14: 1000}))
    assert (decision.affordability_status, decision.recommended_payment_method) == ("affordable_later", "wait")
    assert decision.payments == ((day(14), Decimal("800")),)
    assert decision.earliest_date_for_full_payment == day(14)


def test_wait_past_the_deadline_is_not_affordable_with_a_blank_date() -> None:
    decision = run(request(800, 10), profile("full_payment"), forecast(opening=500, flows={14: 1000}))
    assert decision.affordability_status == "not_affordable"
    assert decision.payments == () and decision.earliest_date_for_full_payment is None


def test_partial_payment_pays_the_safe_amount_now_and_the_rest_on_the_earliest_date() -> None:
    decision = run(request(800, partial=True), profile("partial_payment"), forecast(opening=500, flows={14: 1000}))
    assert (decision.affordability_status, decision.recommended_payment_method) == ("affordable_with_plan", "partial_payment")
    assert decision.payments == ((START, Decimal("400")), (day(14), Decimal("400")))


def test_partial_payment_beats_a_costlier_installment_plan() -> None:
    """request_19's shape: both safe; partial totals the request, installments carry a fee."""
    fc = forecast(opening=500, flows={14: 1000})
    options = [option("payment_option_1", 2, 410, 14, total=820)]
    decision = run(request(800, 60, partial=True), profile("partial_payment", "installments", max_months=2), fc, options)
    assert decision.recommended_payment_method == "partial_payment"


def test_partial_payment_needs_the_request_to_allow_it() -> None:
    decision = run(request(800, partial=False), profile("partial_payment"), forecast(opening=500, flows={14: 1000}))
    assert decision.affordability_status == "not_affordable"


def test_full_payment_today_beats_waiting_and_installments() -> None:
    decision = run(request(500, 90), profile("full_payment", "installments", max_months=6), forecast(), [option("payment_option_1", 2, 250, 0)])
    assert decision.recommended_payment_method == "full_payment"


# --------------------------------------------------------------------------
# ranking order
# --------------------------------------------------------------------------


def candidate(total, first=0, count=1, option_id="", changes=()) -> PlanCandidate:
    payments = tuple((day(first + index), Decimal(str(total)) / count) for index in range(count))
    return PlanCandidate("installments", payments, Decimal(str(total)), option_id, changes)


def test_ranking_follows_the_problem_statement_order() -> None:
    deadline = day(60)
    ordered = [
        candidate(900, changes=()),
        candidate(800, changes=("stop:event_1",)),
    ]
    assert min(ordered, key=lambda item: rank_key(item, deadline)).total_paid == Decimal("900")
    assert min([candidate(820), candidate(810, first=5)], key=lambda item: rank_key(item, deadline)).total_paid == Decimal("810")
    assert min([candidate(800, first=5), candidate(800, first=2)], key=lambda item: rank_key(item, deadline)).first_date == day(2)
    assert min([candidate(800, count=3), candidate(800, count=2)], key=lambda item: rank_key(item, deadline)).number_of_payments == 2
    assert min([candidate(800, option_id="payment_option_9"), candidate(800, option_id="payment_option_2")], key=lambda item: rank_key(item, deadline)).payment_option_id == "payment_option_2"


# --------------------------------------------------------------------------
# output row
# --------------------------------------------------------------------------


def test_payment_plan_formatting_matches_gold_conventions() -> None:
    assert format_payment_plan([(date(2024, 9, 15), Decimal("10840")), (date(2024, 9, 4), Decimal("28820"))]) == "2024-09-04:28820|2024-09-15:10840"
    assert format_payment_plan([(date(2026, 1, 3), Decimal("620.4"))]) == "2026-01-03:620.40"
    assert format_payment_plan([]) == "none"


def test_not_affordable_row_has_plan_none_and_blank_date() -> None:
    decision = run(request(800, 10), profile("full_payment"), forecast(opening=500, flows={14: 1000}))
    row = build_output_row("request_t", decision, "none", "x")
    assert row["payment_plan"] == "none" and row["earliest_date_for_full_payment"] == ""
    assert list(row) == ["request_id", "amount_safe_to_pay", "affordability_status", "recommended_payment_method", "payment_plan", "earliest_date_for_full_payment", "spending_changes_needed", "decision_explanation"]
