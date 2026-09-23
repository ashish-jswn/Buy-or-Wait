"""Deterministic checks on every output row before it is written, and the safe fallback.

A row that fails any check is replaced by a ``not_affordable`` fallback row and counted
(author decision, 2026-09-13): a submission always exists and failures stay visible.
The fallback itself is checked, so it can never introduce a new violation.

Checks (problem_statement.md output schema and decision rules; DATASET_FACTS A1-A5, B1-B3, C1-C5):
column order; amount bounds; valid (status, method) pair; earliest-date rules; plan
format, chronology, sums, deadline and method acceptance; installments reproduce a
supplied option exactly; spending changes ≤ 3, distinct, permitted and non-fixed, with
reduce_to at the event's minimum_allowed_amount; non-empty explanation.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping, Optional

from common.formatting import format_plan_amount
from config import (
    MAX_SPENDING_CHANGES,
    METHOD_FULL_PAYMENT,
    METHOD_INSTALLMENTS,
    METHOD_NOT_RECOMMENDED,
    METHOD_PARTIAL_PAYMENT,
    METHOD_WAIT,
    MONEY_TOLERANCE,
    OUTPUT_COLUMNS,
    PLAN_NONE,
    SPENDING_CHANGES_NONE,
    STATUS_AFFORDABLE_LATER,
    STATUS_AFFORDABLE_NOW,
    STATUS_AFFORDABLE_WITH_PLAN,
    STATUS_NOT_AFFORDABLE,
)
from data.records import Event, PaymentOption, Profile, Request

VALID_PAIRS = frozenset(
    {
        (STATUS_AFFORDABLE_NOW, METHOD_FULL_PAYMENT),
        (STATUS_AFFORDABLE_WITH_PLAN, METHOD_FULL_PAYMENT),
        (STATUS_AFFORDABLE_WITH_PLAN, METHOD_INSTALLMENTS),
        (STATUS_AFFORDABLE_WITH_PLAN, METHOD_PARTIAL_PAYMENT),
        (STATUS_AFFORDABLE_LATER, METHOD_WAIT),
        (STATUS_NOT_AFFORDABLE, METHOD_NOT_RECOMMENDED),
    }
)
PLAN_ENTRY = re.compile(r"^(\d{4}-\d{2}-\d{2}):(\d+(?:\.\d{1,2})?)$")
TOLERANCE = Decimal(str(MONEY_TOLERANCE))


def _decimal(text: str) -> Optional[Decimal]:
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _iso(text: str) -> Optional[date]:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _parse_plan(plan: str) -> Optional[list[tuple[date, Decimal]]]:
    entries = []
    for part in plan.split("|"):
        match = PLAN_ENTRY.match(part)
        on = _iso(match.group(1)) if match else None
        if on is None:
            return None
        entries.append((on, Decimal(match.group(2))))
    return entries


def _check_plan(row, request, profile, options, amount, earliest, problems: list[str]) -> None:
    method, plan = row["recommended_payment_method"], row["payment_plan"]
    if method == METHOD_NOT_RECOMMENDED:
        if plan != PLAN_NONE:
            problems.append("not_recommended must have payment_plan none")
        return
    entries = _parse_plan(plan) if plan != PLAN_NONE else None
    if not entries:
        problems.append(f"payment_plan malformed or missing: {plan!r}")
        return
    dates = [on for on, _ in entries]
    if dates != sorted(dates) or len(set(dates)) != len(dates):
        problems.append("payment_plan dates not strictly chronological")
    if dates[0] < request.request_date:
        problems.append("payment_plan starts before request_date")
    total = sum((value for _, value in entries), Decimal("0"))
    accepted = METHOD_FULL_PAYMENT if method == METHOD_WAIT else method
    if not profile.accepts(accepted):
        problems.append(f"user does not accept {accepted}")

    if method in (METHOD_FULL_PAYMENT, METHOD_WAIT, METHOD_PARTIAL_PAYMENT):
        if abs(total - request.requested_amount) > TOLERANCE:
            problems.append(f"{method} plan sums to {total}, not {request.requested_amount}")
        if dates[-1] > request.desired_completion_date:
            problems.append("plan completes after desired_completion_date")
    if method == METHOD_FULL_PAYMENT and (len(entries) != 1 or dates[0] != request.request_date):
        problems.append("full_payment must be one payment on request_date")
    if method == METHOD_WAIT and (len(entries) != 1 or dates[0] != earliest):
        problems.append("wait must be one payment on earliest_date_for_full_payment")
    if method == METHOD_PARTIAL_PAYMENT:
        if not request.allows_partial_payment:
            problems.append("partial_payment on a request that does not allow it")
        if len(entries) != 2 or dates[0] != request.request_date or dates[1] != earliest:
            problems.append("partial_payment must pay on request_date and earliest_date_for_full_payment")
        elif amount is not None and format_plan_amount(entries[0][1]) != format_plan_amount(amount):
            problems.append("partial_payment first payment must equal amount_safe_to_pay")
    if method == METHOD_INSTALLMENTS:
        cap = profile.max_installment_months
        match = any(
            option.payment_method == METHOD_INSTALLMENTS
            and option.number_of_payments == len(entries)
            and (cap is None or option.number_of_payments <= cap)
            and [(on, format_plan_amount(value)) for on, value in option.schedule()]
            == [(on, format_plan_amount(value)) for on, value in entries]
            for option in options
        )
        if not match:
            problems.append("installments plan does not reproduce a supplied option within max_installment_months")


def _check_changes(row, request, profile, events_by_id, problems: list[str]) -> None:
    value = row["spending_changes_needed"]
    if value == SPENDING_CHANGES_NONE:
        return
    if (row["affordability_status"], row["recommended_payment_method"]) != (STATUS_AFFORDABLE_WITH_PLAN, METHOD_FULL_PAYMENT):
        problems.append("spending changes only belong on affordable_with_plan / full_payment")
    tokens = value.split("|")
    if len(tokens) > MAX_SPENDING_CHANGES:
        problems.append("more than 3 spending changes")
    seen: set[str] = set()
    for token in tokens:
        parts = token.split(":")
        verb, event_id = (parts[0], parts[1]) if len(parts) >= 2 else ("", "")
        event: Optional[Event] = events_by_id.get(event_id)
        if event is None or event.user_id != request.user_id:
            problems.append(f"{token}: unknown event for this user")
            continue
        if event_id in seen:
            problems.append(f"{token}: event targeted twice")
        seen.add(event_id)
        if not event.is_flexible or event.category in profile.protected_categories:
            problems.append(f"{token}: event is fixed or protected")
        if verb == "stop" and len(parts) == 2:
            if not (event.can_stop and profile.may_stop(event.category)):
                problems.append(f"{token}: stop not permitted")
        elif verb == "reduce_to" and len(parts) == 3:
            if not (event.can_reduce and profile.may_reduce(event.category)):
                problems.append(f"{token}: reduce not permitted")
            new_amount = _decimal(parts[2])
            if new_amount is None or event.minimum_allowed_amount is None or new_amount != event.minimum_allowed_amount:
                problems.append(f"{token}: reduce_to must use minimum_allowed_amount")
        else:
            problems.append(f"{token}: malformed action")


def check_row(
    row: Mapping[str, str],
    request: Request,
    profile: Profile,
    options: Iterable[PaymentOption],
    events_by_id: Mapping[str, Event],
) -> list[str]:
    """Every contract violation in one output row; empty when the row is valid."""
    problems: list[str] = []
    if tuple(row) != OUTPUT_COLUMNS:
        problems.append("columns missing or out of order")
        return problems
    if row["request_id"] != request.request_id:
        problems.append("request_id mismatch")

    amount = _decimal(row["amount_safe_to_pay"])
    if amount is None or not Decimal("0") <= amount <= request.requested_amount:
        problems.append(f"amount_safe_to_pay out of bounds: {row['amount_safe_to_pay']!r}")

    status, method = row["affordability_status"], row["recommended_payment_method"]
    if (status, method) not in VALID_PAIRS:
        problems.append(f"invalid status/method pair: {status}/{method}")

    earliest: Optional[date] = None
    if row["earliest_date_for_full_payment"]:
        earliest = _iso(row["earliest_date_for_full_payment"])
        if earliest is None:
            problems.append("earliest_date_for_full_payment is not a date")
    if status == STATUS_AFFORDABLE_NOW and earliest != request.request_date:
        problems.append("affordable_now requires earliest_date_for_full_payment == request_date")
    if status == STATUS_NOT_AFFORDABLE and (earliest is not None or row["spending_changes_needed"] != SPENDING_CHANGES_NONE):
        problems.append("not_affordable requires a blank earliest date and no spending changes")

    _check_plan(row, request, profile, list(options), amount, earliest, problems)
    _check_changes(row, request, profile, events_by_id, problems)
    if not row["decision_explanation"].strip():
        problems.append("decision_explanation is empty")
    return problems


def fallback_row(request: Request, amount_safe: str, explanation: str) -> dict[str, str]:
    """The safe replacement for a row that failed its checks: do not proceed."""
    return {
        "request_id": request.request_id,
        "amount_safe_to_pay": amount_safe,
        "affordability_status": STATUS_NOT_AFFORDABLE,
        "recommended_payment_method": METHOD_NOT_RECOMMENDED,
        "payment_plan": PLAN_NONE,
        "earliest_date_for_full_payment": "",
        "spending_changes_needed": SPENDING_CHANGES_NONE,
        "decision_explanation": explanation,
    }
