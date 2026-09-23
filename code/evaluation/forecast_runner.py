"""Produce predictions using the forecast engine alone, for scoring in isolation.

Fills only ``amount_safe_to_pay`` and ``earliest_date_for_full_payment`` from
``engine.forecast``. The remaining four columns stay at the floor-baseline values, so
the scorer measures the forecast on its own rather than the forecast plus a
half-finished decision layer.

Not the pipeline entry point — that is ``code/main.py``. Makes no model call.

Run:
    python code/evaluation/forecast_runner.py --out <csv>
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.formatting import format_safe_amount  # noqa: E402
from data.loader import load_dataset, load_sample_dataset  # noqa: E402
from config import MESSAGE_FACTS_CACHE_PATH  # noqa: E402
from engine.evidence import apply_message_facts  # noqa: E402
from engine.forecast import assess  # noqa: E402
from engine.pipeline import readings_for  # noqa: E402
from engine.state import build_cash_state  # noqa: E402
from extraction.messages import load_cache, readings_from_cache  # noqa: E402
from evaluation.baseline import (  # noqa: E402
    BASELINE_AFFORDABILITY_STATUS,
    BASELINE_EXPLANATION,
    BASELINE_PAYMENT_METHOD,
    BASELINE_PAYMENT_PLAN,
    BASELINE_SPENDING_CHANGES,
    OUTPUT_COLUMNS,
    write_predictions,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_PATH = REPO_ROOT / "code" / "evaluation" / "forecast_predictions.csv"


def build_forecast_row(request, dataset, readings=None) -> dict[str, str]:
    """Build one prediction row with the two forecast columns filled.

    `readings` maps message_id to a cached MessageReading; when given, message facts are
    applied to the state before the forecast (engine.evidence).
    """
    profile = dataset.profiles[request.user_id]
    state = build_cash_state(
        profile,
        dataset.events_for(request.user_id),
        request.request_date,
        dataset.rates,
    )
    if readings:
        state = apply_message_facts(state, readings_for(request, dataset, readings), profile, dataset.rates).state
    assessment = assess(state, request.requested_amount)
    earliest = assessment.earliest_full_payment
    return {
        "request_id": request.request_id,
        "amount_safe_to_pay": format_safe_amount(assessment.amount_safe_to_pay),
        "affordability_status": BASELINE_AFFORDABILITY_STATUS,
        "recommended_payment_method": BASELINE_PAYMENT_METHOD,
        "payment_plan": BASELINE_PAYMENT_PLAN,
        "earliest_date_for_full_payment": earliest.isoformat() if earliest else "",
        "spending_changes_needed": BASELINE_SPENDING_CHANGES,
        "decision_explanation": BASELINE_EXPLANATION,
    }


def build_forecast_rows(dataset, readings=None) -> list[dict[str, str]]:
    """Build one row per request in the dataset, in file order."""
    return [build_forecast_row(request, dataset, readings) for request in dataset.requests]


def parse_arguments(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments for the forecast runner."""
    parser = argparse.ArgumentParser(
        description="Emit forecast-only predictions (two columns) for scoring.",
    )
    parser.add_argument(
        "--out", default=str(DEFAULT_OUTPUT_PATH), help="where to write the predictions CSV"
    )
    parser.add_argument(
        "--no-messages",
        action="store_true",
        help="ignore cached message readings (for before/after comparison)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="run over dataset/requests.csv instead of the 25 labelled samples",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """Write forecast-only predictions. Returns 0 on success."""
    arguments = parse_arguments(argv)
    dataset = load_dataset() if arguments.full else load_sample_dataset()
    readings = None if arguments.no_messages else readings_from_cache(load_cache(MESSAGE_FACTS_CACHE_PATH))
    rows = build_forecast_rows(dataset, readings)
    output_path = Path(arguments.out).resolve()
    write_predictions(rows, output_path)
    print(f"Wrote {len(rows)} forecast-only rows to {output_path}")
    print(f"(columns filled: amount_safe_to_pay, earliest_date_for_full_payment; "
          f"other {len(OUTPUT_COLUMNS) - 3} at baseline)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
