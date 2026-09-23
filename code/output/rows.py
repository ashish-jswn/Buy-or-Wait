"""Turn a Decision into one output.csv row, and write rows. Formatting only — no decisions."""

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from common.formatting import format_plan_amount, format_safe_amount
from config import OUTPUT_COLUMNS, PLAN_NONE


def write_output_csv(rows: Iterable[dict[str, str]], path: Path) -> None:
    """Write rows with exactly the required output header, in the given order, UTF-8."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(OUTPUT_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def format_payment_plan(payments: Iterable[tuple[date, Decimal]]) -> str:
    """Chronological ``YYYY-MM-DD:amount`` entries joined by ``|``, or ``none`` (C1, C2)."""
    entries = [f"{on.isoformat()}:{format_plan_amount(amount)}" for on, amount in sorted(payments)]
    return "|".join(entries) if entries else PLAN_NONE


def build_output_row(request_id: str, decision, spending_changes: str, explanation: str) -> dict[str, str]:
    """One row in the required output column order."""
    earliest = decision.earliest_date_for_full_payment
    return {
        "request_id": request_id,
        "amount_safe_to_pay": format_safe_amount(decision.amount_safe_to_pay),
        "affordability_status": decision.affordability_status,
        "recommended_payment_method": decision.recommended_payment_method,
        "payment_plan": format_payment_plan(decision.payments),
        "earliest_date_for_full_payment": earliest.isoformat() if earliest else "",
        "spending_changes_needed": spending_changes,
        "decision_explanation": explanation,
    }
