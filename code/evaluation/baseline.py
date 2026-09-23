"""Rule-only floor baseline for Buy or Wait?.

Predicts the most conservative possible answer for every request - nothing is
affordable, nothing is recommended, no plan, no spending changes. It reads no
financial state and makes no model call.

Its only purpose is to establish the scoring floor and to prove the evaluation
harness works end to end. It is not a candidate solution.

Usage:
    python code/evaluation/baseline.py --requests dataset/sample_requests.csv --out <csv>
"""

import argparse
import csv
import sys
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REQUESTS_PATH = REPO_ROOT / "dataset" / "sample_requests.csv"
DEFAULT_OUTPUT_PATH = REPO_ROOT / "code" / "evaluation" / "baseline_predictions.csv"

ID_COLUMN = "request_id"

OUTPUT_COLUMNS = (
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
)

BASELINE_AMOUNT_SAFE_TO_PAY = "0"
BASELINE_AFFORDABILITY_STATUS = "not_affordable"
BASELINE_PAYMENT_METHOD = "not_recommended"
BASELINE_PAYMENT_PLAN = "none"
BASELINE_EARLIEST_DATE = ""
BASELINE_SPENDING_CHANGES = "none"
BASELINE_EXPLANATION = (
    "Floor baseline: no financial state was reconstructed, so no payment is "
    "treated as safe and nothing is recommended."
)


def read_request_ids(path: Path) -> list[str]:
    """Read request_id values from a requests CSV, in file order.

    Blank ids are skipped; duplicates are preserved as-is so the scorer can
    report them.
    """
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [
            (row.get(ID_COLUMN) or "").strip()
            for row in csv.DictReader(handle)
            if (row.get(ID_COLUMN) or "").strip()
        ]


def build_baseline_row(request_id: str) -> dict[str, str]:
    """Build the constant floor prediction for one request_id."""
    return {
        "request_id": request_id,
        "amount_safe_to_pay": BASELINE_AMOUNT_SAFE_TO_PAY,
        "affordability_status": BASELINE_AFFORDABILITY_STATUS,
        "recommended_payment_method": BASELINE_PAYMENT_METHOD,
        "payment_plan": BASELINE_PAYMENT_PLAN,
        "earliest_date_for_full_payment": BASELINE_EARLIEST_DATE,
        "spending_changes_needed": BASELINE_SPENDING_CHANGES,
        "decision_explanation": BASELINE_EXPLANATION,
    }


def build_baseline_rows(request_ids: list[str]) -> list[dict[str, str]]:
    """Build one floor prediction row per request_id, preserving order."""
    return [build_baseline_row(request_id) for request_id in request_ids]


def write_predictions(rows: list[dict[str, str]], path: Path) -> None:
    """Write prediction rows to `path` in the required column order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(OUTPUT_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)


def parse_arguments(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments for the baseline."""
    parser = argparse.ArgumentParser(
        description="Emit the all-not_affordable floor baseline for Buy or Wait?.",
    )
    parser.add_argument(
        "--requests",
        default=str(DEFAULT_REQUESTS_PATH),
        help="requests CSV to read ids from (default: dataset/sample_requests.csv)",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUTPUT_PATH),
        help="where to write the predictions CSV",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """Write the floor baseline predictions. Returns 0 on success, 1 on error."""
    arguments = parse_arguments(argv)
    requests_path = Path(arguments.requests).resolve()
    output_path = Path(arguments.out).resolve()

    if not requests_path.is_file():
        print(f"ERROR: requests file not found: {requests_path}")
        return 1

    rows = build_baseline_rows(read_request_ids(requests_path))
    write_predictions(rows, output_path)
    print(f"Wrote {len(rows)} baseline rows to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
