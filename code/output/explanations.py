"""decision_explanation from fixed templates. No model call (DECISIONS #6).

Wording, number and date formats are copied from the 25 gold explanations (DATASET_FACTS
C3): amounts carry thousands separators and exactly two decimals when fractional
(``EUR 620.40``), whole amounts none (``ZAR 18,000``); dates are ``8 August 2025``.

Two gold rows use variant wordings that no field predicts (request_04's "Wait until…",
request_09's "This keeps…"); the main template is used for those shapes.

The second not-affordable wording ("Although X is available today…") is used when the
request allows partial payment, the user accepts partial but not installments, and some
amount is safe today — the split that separates 7/7 gold not-affordable rows.
"""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping

from config import (
    METHOD_FULL_PAYMENT,
    METHOD_INSTALLMENTS,
    METHOD_PARTIAL_PAYMENT,
    METHOD_WAIT,
    STATUS_NOT_AFFORDABLE,
)
from data.records import Profile, Request

MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def format_money(currency: str, amount: Decimal) -> str:
    """``EUR 620.40`` / ``ZAR 18,000``: thousands separators, 2dp only when fractional."""
    value = Decimal(amount).quantize(Decimal("0.01"), ROUND_HALF_UP)
    text = f"{int(value):,}" if value == value.to_integral_value() else f"{value:,.2f}"
    return f"{currency} {text}"


def format_long_date(on: date) -> str:
    """``8 August 2025`` — no leading zero on the day."""
    return f"{on.day} {MONTH_NAMES[on.month - 1]} {on.year}"


def _lower_first(text: str) -> str:
    """Lower-case only the first character, so currency codes and acronyms survive."""
    return text[:1].lower() + text[1:]


def _join(clauses: list[str]) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    if len(clauses) == 1:
        return clauses[0]
    return ", ".join(clauses[:-1]) + " and " + clauses[-1]


def not_affordable_explanation(request: Request, profile: Profile, amount_safe: Decimal) -> str:
    """The two not-affordable wordings."""
    currency = profile.home_currency
    if (
        request.allows_partial_payment
        and profile.accepts(METHOD_PARTIAL_PAYMENT)
        and not profile.accepts(METHOD_INSTALLMENTS)
        and amount_safe > 0
    ):
        return (
            f"Do not proceed with the {format_money(currency, request.requested_amount)} request. "
            f"Although {format_money(currency, amount_safe)} is available today, the full amount "
            "cannot be completed safely within 90 days."
        )
    return (
        f"Do not make this payment by {format_long_date(request.desired_completion_date)}. "
        f"None of the available options keeps the {format_money(currency, profile.minimum_balance_to_keep)} "
        "minimum protected."
    )


def explain(request: Request, profile: Profile, decision, descriptions: Mapping[str, str]) -> str:
    """The explanation for one decision. `descriptions` maps event_id to its description."""
    currency = profile.home_currency
    minimum = format_money(currency, profile.minimum_balance_to_keep)
    requested = format_money(currency, request.requested_amount)
    method = decision.recommended_payment_method
    changes = decision.chosen.spending_changes if decision.chosen is not None else ()

    if decision.affordability_status == STATUS_NOT_AFFORDABLE or not decision.payments:
        return not_affordable_explanation(request, profile, decision.amount_safe_to_pay)

    if method == METHOD_FULL_PAYMENT and changes:
        clauses = []
        for action in changes:
            parts = action.split(":")
            description = _lower_first(descriptions.get(parts[1], parts[1]))
            if parts[0] == "stop":
                clauses.append(f"stop the {description}")
            else:
                clauses.append(f"reduce the {description} to {format_money(currency, Decimal(parts[2]))}")
        lead = _join(clauses)
        lead = lead[:1].upper() + lead[1:]
        return f"{lead}, then pay {requested} today. This leaves at least {minimum} available."

    if method == METHOD_FULL_PAYMENT:
        return f"Pay {requested} today. This leaves at least {minimum} available over the next 90 days."

    if method == METHOD_INSTALLMENTS:
        first_on, amount = decision.payments[0]
        return (
            f"Use {len(decision.payments)} installments of {format_money(currency, amount)}, "
            f"starting {format_long_date(first_on)}. This leaves at least {minimum} available."
        )

    if method == METHOD_WAIT:
        on, _ = decision.payments[0]
        return (
            f"Pay {requested} in full on {format_long_date(on)}. "
            f"Paying earlier would take the balance below the {minimum} minimum."
        )

    if method == METHOD_PARTIAL_PAYMENT:
        (_, first), (second_on, second) = decision.payments
        return (
            f"Pay {format_money(currency, first)} today and the remaining {format_money(currency, second)} "
            f"on {format_long_date(second_on)}. This completes the full request and keeps the {minimum} "
            "minimum protected."
        )

    return not_affordable_explanation(request, profile, decision.amount_safe_to_pay)
