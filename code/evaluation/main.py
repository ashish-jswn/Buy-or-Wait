"""Prediction scorer for the Buy or Wait? challenge.

Measures a predictions CSV against the labelled examples in
``dataset/sample_requests.csv`` and prints one row per graded check plus a
per-row diff table for every mismatch.

This is a measuring tool, not a gate: it reports problems (missing rows, extra
rows, absent columns, unparseable values) and always exits 0.

Usage:
    python code/evaluation/main.py --predictions <csv> --gold dataset/sample_requests.csv
"""

import argparse
import csv
import re
import sys
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.formatting import format_plan_amount  # noqa: E402 - needs the path above

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GOLD_PATH = REPO_ROOT / "dataset" / "sample_requests.csv"
DEFAULT_EVENTS_PATH = REPO_ROOT / "dataset" / "financial_events.csv"

ID_COLUMN = "request_id"
REQUESTED_AMOUNT_COLUMN = "requested_amount"

GRADED_COLUMNS = (
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
)

# Relative slack for the "within 1%" amount check.
AMOUNT_TOLERANCE_FRACTION = 0.01
# Absolute slack when comparing money sums, i.e. one cent.
PLAN_SUM_TOLERANCE = 0.01
# Relative slack standing in for float equality on an "exact" amount match.
EXACT_AMOUNT_EPSILON = 1e-9

NO_PLAN_LITERAL = "none"
NO_SPENDING_CHANGE_LITERAL = "none"

# Methods whose payment_plan entries must add up to requested_amount. An
# installment plan follows a supplied payment option (problem_statement.md), so its
# total legitimately differs from requested_amount and is not sum-checked.
SUM_CHECKED_METHODS = frozenset({"full_payment", "partial_payment", "wait"})
# Methods that must carry no plan at all.
EMPTY_PLAN_METHODS = frozenset({"not_recommended"})

NON_FIXED_FLEXIBILITIES = frozenset({"reducible", "stoppable", "reducible_or_stoppable"})

PLAN_ENTRY_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2}):(-?\d+(?:\.\d+)?)$")
STOP_ACTION_PATTERN = re.compile(r"^stop:(\S+)$")
REDUCE_ACTION_PATTERN = re.compile(r"^reduce_to:([^:]+):(-?\d+(?:\.\d+)?)$")

DIFF_VALUE_WIDTH = 46
BLANK_DISPLAY = "<blank>"

# (key, column, human-readable check name) in report order.
CHECK_SPECS = (
    ("amount_exact", "amount_safe_to_pay", "exact match"),
    ("amount_tolerance", "amount_safe_to_pay", "within 1%"),
    ("status_exact", "affordability_status", "exact match"),
    ("method_exact", "recommended_payment_method", "exact match"),
    ("plan_exact", "payment_plan", "exact string match"),
    ("plan_normalized", "payment_plan", "normalized match"),
    ("plan_format", "payment_plan", "valid format"),
    ("plan_sum", "payment_plan", "sums to requested_amount"),
    ("date_exact", "earliest_date_for_full_payment", "exact match (blank counts)"),
    ("changes_exact", "spending_changes_needed", "exact match"),
    ("changes_non_fixed", "spending_changes_needed", "targets only non-fixed events"),
    ("explanation_exact", "decision_explanation", "exact match"),
    ("joint_exact", "ALL 7 COLUMNS", "amount within 1% + 6 exact"),
)

# The six columns that must match exactly for the joint check; amount uses the 1% band.
JOINT_EXACT_COLUMNS = (
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
)


@dataclass
class CheckResult:
    """One measured check: how many applicable rows passed."""

    column: str
    check: str
    passed: int = 0
    applicable: int = 0

    @property
    def percentage(self) -> Optional[float]:
        """Pass rate over applicable rows, or None when nothing applied."""
        if self.applicable == 0:
            return None
        return 100.0 * self.passed / self.applicable


@dataclass
class RowDiff:
    """A single mismatch between a gold row and the matching prediction row."""

    request_id: str
    column: str
    gold_value: str
    predicted_value: str


@dataclass
class ScoreReport:
    """Everything one scoring run produced."""

    checks: dict[str, CheckResult] = field(default_factory=dict)
    diffs: list[RowDiff] = field(default_factory=list)
    scored_ids: list[str] = field(default_factory=list)
    missing_ids: list[str] = field(default_factory=list)
    extra_ids: list[str] = field(default_factory=list)
    duplicate_ids: list[str] = field(default_factory=list)
    absent_columns: list[str] = field(default_factory=list)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    """Read a UTF-8 CSV into a list of dicts, with every value stripped.

    Assumes the file exists; callers check that first.
    """
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return [
            {(key or ""): (value or "").strip() for key, value in row.items()}
            for row in reader
        ]


def load_flexibility_by_event(path: Path) -> dict[str, str]:
    """Map event_id to its flexibility value, for the spending-change check.

    Returns an empty dict when the events file is absent, which downgrades the
    non-fixed-target check to "not applicable" rather than failing the run.
    """
    if not path.is_file():
        return {}
    return {
        row["event_id"]: row.get("flexibility", "")
        for row in read_csv_rows(path)
        if row.get("event_id")
    }


def index_by_request_id(
    rows: Iterable[dict[str, str]],
) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Index rows by request_id, returning the index and any duplicate ids.

    The first occurrence of a duplicated id wins.
    """
    index: dict[str, dict[str, str]] = {}
    duplicates: list[str] = []
    for row in rows:
        request_id = row.get(ID_COLUMN, "")
        if not request_id:
            continue
        if request_id in index:
            duplicates.append(request_id)
            continue
        index[request_id] = row
    return index, duplicates


def parse_amount(raw: str) -> Optional[float]:
    """Parse a money string to float, or None when it is blank/unparseable."""
    if raw is None or raw.strip() == "":
        return None
    try:
        return float(raw.strip().replace(",", ""))
    except ValueError:
        return None


def parse_iso_date(raw: str) -> Optional[date]:
    """Parse a YYYY-MM-DD string, or None when it is blank/unparseable."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def amounts_match_exactly(gold: Optional[float], predicted: Optional[float]) -> bool:
    """True when both amounts parsed and are numerically equal.

    Formatting differences (620.40 vs 620.4) are not mismatches; a value that
    failed to parse never matches.
    """
    if gold is None or predicted is None:
        return False
    scale = max(1.0, abs(gold), abs(predicted))
    return abs(gold - predicted) <= EXACT_AMOUNT_EPSILON * scale


def amounts_match_within_tolerance(
    gold: Optional[float], predicted: Optional[float], fraction: float
) -> bool:
    """True when predicted is within `fraction` of gold (exact when gold is 0)."""
    if gold is None or predicted is None:
        return False
    if gold == 0:
        return predicted == 0
    return abs(predicted - gold) <= abs(gold) * fraction


def split_plan_entries(plan: str) -> list[str]:
    """Split a payment_plan string on the pipe separator into trimmed entries."""
    return [entry.strip() for entry in plan.split("|") if entry.strip()]


def is_valid_plan_format(plan: str, method: str) -> bool:
    """True when payment_plan is well-formed for the given method.

    Well-formed means the literal 'none', or one-or-more chronological
    YYYY-MM-DD:amount entries with real dates and non-negative amounts. Methods
    in EMPTY_PLAN_METHODS must use 'none'.
    """
    normalised = plan.strip()
    if normalised.lower() == NO_PLAN_LITERAL:
        return True
    if method in EMPTY_PLAN_METHODS:
        return False
    entries = split_plan_entries(normalised)
    if not entries:
        return False
    previous_date: Optional[date] = None
    for entry in entries:
        match = PLAN_ENTRY_PATTERN.match(entry)
        if match is None:
            return False
        entry_date = parse_iso_date(match.group(1))
        entry_amount = parse_amount(match.group(2))
        if entry_date is None or entry_amount is None or entry_amount < 0:
            return False
        if previous_date is not None and entry_date < previous_date:
            return False
        previous_date = entry_date
    return True


def normalize_plan(plan: str) -> Optional[str]:
    """Rewrite a payment_plan with every amount in canonical plan formatting.

    Returns 'none' unchanged and None when any entry is malformed, so a
    normalized comparison never silently passes on an unparseable plan.
    """
    normalised = plan.strip()
    if not normalised:
        return None
    if normalised.lower() == NO_PLAN_LITERAL:
        return NO_PLAN_LITERAL
    entries = split_plan_entries(normalised)
    if not entries:
        return None
    rendered: list[str] = []
    for entry in entries:
        match = PLAN_ENTRY_PATTERN.match(entry)
        if match is None:
            return None
        amount = parse_amount(match.group(2))
        if amount is None or amount < 0:
            return None
        try:
            rendered.append(f"{match.group(1)}:{format_plan_amount(match.group(2))}")
        except ValueError:
            return None
    return "|".join(rendered)


def plans_match_normalized(gold_plan: str, predicted_plan: str) -> bool:
    """True when two plans agree once both are rendered in canonical formatting.

    Distinguishes a formatting difference (620.4 vs 620.40) from a real
    arithmetic or schedule difference; a malformed plan never matches.
    """
    gold_normalized = normalize_plan(gold_plan)
    predicted_normalized = normalize_plan(predicted_plan)
    if gold_normalized is None or predicted_normalized is None:
        return False
    return gold_normalized == predicted_normalized


def plan_total(plan: str) -> Optional[float]:
    """Sum a payment_plan's amounts, or None when it is 'none'/unparseable."""
    normalised = plan.strip()
    if not normalised or normalised.lower() == NO_PLAN_LITERAL:
        return None
    total = 0.0
    for entry in split_plan_entries(normalised):
        match = PLAN_ENTRY_PATTERN.match(entry)
        if match is None:
            return None
        amount = parse_amount(match.group(2))
        if amount is None:
            return None
        total += amount
    return total


def plan_sum_is_required(method: str, plan: str) -> bool:
    """True when this row's plan is expected to add up to requested_amount."""
    if plan.strip().lower() == NO_PLAN_LITERAL:
        return False
    return method in SUM_CHECKED_METHODS


def plan_sums_to_requested(plan: str, requested_amount: Optional[float]) -> bool:
    """True when the plan's entries add up to requested_amount within a cent."""
    total = plan_total(plan)
    if total is None or requested_amount is None:
        return False
    return abs(total - requested_amount) <= PLAN_SUM_TOLERANCE


def parse_spending_change_targets(value: str) -> Optional[list[str]]:
    """Extract the event ids a spending_changes_needed value targets.

    Returns [] for 'none'/blank, the list of event ids for a well-formed value,
    and None when any action is malformed.
    """
    normalised = value.strip()
    if not normalised or normalised.lower() == NO_SPENDING_CHANGE_LITERAL:
        return []
    targets: list[str] = []
    for action in [part.strip() for part in normalised.split("|") if part.strip()]:
        stop_match = STOP_ACTION_PATTERN.match(action)
        reduce_match = REDUCE_ACTION_PATTERN.match(action)
        if stop_match is not None:
            targets.append(stop_match.group(1))
        elif reduce_match is not None:
            targets.append(reduce_match.group(1))
        else:
            return None
    return targets


def targets_only_non_fixed_events(value: str, flexibility_by_event: dict[str, str]) -> bool:
    """True when every targeted event exists in the dataset and is non-fixed.

    An unknown event id counts as a failure: the prediction named something the
    dataset does not contain.
    """
    targets = parse_spending_change_targets(value)
    if targets is None:
        return False
    for event_id in targets:
        flexibility = flexibility_by_event.get(event_id)
        if flexibility is None or flexibility not in NON_FIXED_FLEXIBILITIES:
            return False
    return True


def record_check(result: CheckResult, applicable: bool, passed: bool) -> None:
    """Accumulate one row's outcome into a CheckResult, ignoring n/a rows."""
    if not applicable:
        return
    result.applicable += 1
    if passed:
        result.passed += 1


def build_checks() -> dict[str, CheckResult]:
    """Create the empty per-check accumulators, in report order."""
    return {key: CheckResult(column=column, check=check) for key, column, check in CHECK_SPECS}


def score_single_row(
    request_id: str,
    gold_row: dict[str, str],
    predicted_row: dict[str, str],
    checks: dict[str, CheckResult],
    flexibility_by_event: dict[str, str],
) -> list[RowDiff]:
    """Measure one prediction row, updating `checks` and returning its diffs."""
    diffs: list[RowDiff] = []
    requested_amount = parse_amount(gold_row.get(REQUESTED_AMOUNT_COLUMN, ""))

    def compare(key: str, column: str, comparer: Callable[[str, str], bool]) -> None:
        gold_value = gold_row.get(column, "")
        predicted_value = predicted_row.get(column, "")
        matched = comparer(gold_value, predicted_value)
        record_check(checks[key], applicable=True, passed=matched)
        if not matched:
            diffs.append(RowDiff(request_id, column, gold_value, predicted_value))

    compare(
        "amount_exact",
        "amount_safe_to_pay",
        lambda gold, predicted: amounts_match_exactly(
            parse_amount(gold), parse_amount(predicted)
        ),
    )
    record_check(
        checks["amount_tolerance"],
        applicable=True,
        passed=amounts_match_within_tolerance(
            parse_amount(gold_row.get("amount_safe_to_pay", "")),
            parse_amount(predicted_row.get("amount_safe_to_pay", "")),
            AMOUNT_TOLERANCE_FRACTION,
        ),
    )

    compare("status_exact", "affordability_status", lambda gold, pred: gold == pred)
    compare("method_exact", "recommended_payment_method", lambda gold, pred: gold == pred)
    compare("plan_exact", "payment_plan", lambda gold, pred: gold == pred)
    record_check(
        checks["plan_normalized"],
        applicable=True,
        passed=plans_match_normalized(
            gold_row.get("payment_plan", ""), predicted_row.get("payment_plan", "")
        ),
    )
    compare("date_exact", "earliest_date_for_full_payment", lambda gold, pred: gold == pred)
    compare("changes_exact", "spending_changes_needed", lambda gold, pred: gold == pred)

    predicted_plan = predicted_row.get("payment_plan", "")
    predicted_method = predicted_row.get("recommended_payment_method", "")
    record_check(
        checks["plan_format"],
        applicable=True,
        passed=is_valid_plan_format(predicted_plan, predicted_method),
    )
    record_check(
        checks["plan_sum"],
        applicable=plan_sum_is_required(predicted_method, predicted_plan),
        passed=plan_sums_to_requested(predicted_plan, requested_amount),
    )

    predicted_changes = predicted_row.get("spending_changes_needed", "")
    has_changes = parse_spending_change_targets(predicted_changes) != []
    record_check(
        checks["changes_non_fixed"],
        applicable=bool(flexibility_by_event) and has_changes,
        passed=targets_only_non_fixed_events(predicted_changes, flexibility_by_event),
    )

    compare("explanation_exact", "decision_explanation", lambda gold, pred: gold == pred)
    amount_ok = amounts_match_within_tolerance(
        parse_amount(gold_row.get("amount_safe_to_pay", "")),
        parse_amount(predicted_row.get("amount_safe_to_pay", "")),
        AMOUNT_TOLERANCE_FRACTION,
    )
    record_check(
        checks["joint_exact"],
        applicable=True,
        passed=amount_ok
        and all(gold_row.get(column, "") == predicted_row.get(column, "") for column in JOINT_EXACT_COLUMNS),
    )
    return diffs


def score_predictions(
    gold_rows: list[dict[str, str]],
    prediction_index: dict[str, dict[str, str]],
    flexibility_by_event: dict[str, str],
) -> tuple[dict[str, CheckResult], list[RowDiff], list[str]]:
    """Score every gold row that has a matching prediction row.

    Returns the accumulated checks, the mismatch diffs, and the scored ids.
    Gold rows with no prediction are skipped here and reported separately.
    """
    checks = build_checks()
    diffs: list[RowDiff] = []
    scored_ids: list[str] = []

    for gold_row in gold_rows:
        request_id = gold_row.get(ID_COLUMN, "")
        predicted_row = prediction_index.get(request_id)
        if predicted_row is None:
            continue
        scored_ids.append(request_id)
        diffs.extend(
            score_single_row(
                request_id, gold_row, predicted_row, checks, flexibility_by_event
            )
        )
    return checks, diffs, scored_ids


def find_absent_columns(prediction_rows: list[dict[str, str]]) -> list[str]:
    """List graded columns the predictions file does not contain."""
    present = set(prediction_rows[0].keys()) if prediction_rows else set()
    return [column for column in GRADED_COLUMNS if column not in present]


def build_report(
    gold_rows: list[dict[str, str]],
    prediction_rows: list[dict[str, str]],
    flexibility_by_event: dict[str, str],
) -> ScoreReport:
    """Assemble a full ScoreReport from already-loaded rows."""
    gold_index, _ = index_by_request_id(gold_rows)
    prediction_index, duplicate_ids = index_by_request_id(prediction_rows)
    checks, diffs, scored_ids = score_predictions(
        gold_rows, prediction_index, flexibility_by_event
    )
    return ScoreReport(
        checks=checks,
        diffs=diffs,
        scored_ids=scored_ids,
        missing_ids=[rid for rid in gold_index if rid not in prediction_index],
        extra_ids=[rid for rid in prediction_index if rid not in gold_index],
        duplicate_ids=duplicate_ids,
        absent_columns=find_absent_columns(prediction_rows),
    )


def truncate(value: str, width: int = DIFF_VALUE_WIDTH) -> str:
    """Shorten a value for table display, marking blanks explicitly."""
    if value == "":
        return BLANK_DISPLAY
    if len(value) <= width:
        return value
    return value[: width - 3] + "..."


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a list of string rows as a fixed-width text table."""
    if not rows:
        return "  (none)"
    widths = [len(header) for header in headers]
    for row in rows:
        for column_index, cell in enumerate(row):
            widths[column_index] = max(widths[column_index], len(cell))
    lines = ["  ".join(header.ljust(widths[i]) for i, header in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    lines.extend(
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows
    )
    return "\n".join(lines)


def render_check_table(checks: dict[str, CheckResult]) -> str:
    """Render the per-column check table."""
    rows: list[list[str]] = []
    for result in checks.values():
        if result.percentage is None:
            ratio, rate = "0/0", "n/a"
        else:
            ratio, rate = f"{result.passed}/{result.applicable}", f"{result.percentage:.2f}%"
        rows.append([result.column, result.check, ratio, rate])
    return format_table(["column", "check", "passed", "rate"], rows)


def render_diff_table(diffs: list[RowDiff]) -> str:
    """Render the per-row mismatch table."""
    rows = [
        [diff.request_id, diff.column, truncate(diff.gold_value), truncate(diff.predicted_value)]
        for diff in diffs
    ]
    return format_table(["request_id", "column", "gold", "predicted"], rows)


def print_coverage(report: ScoreReport) -> None:
    """Print row alignment between the predictions file and the gold file."""
    print("ROW COVERAGE")
    print(f"  scored rows (present in both) : {len(report.scored_ids)}")
    print(f"  missing from predictions      : {len(report.missing_ids)}")
    if report.missing_ids:
        print(f"      {', '.join(report.missing_ids)}")
    print(f"  extra rows not in gold        : {len(report.extra_ids)}")
    if report.extra_ids:
        print(f"      {', '.join(report.extra_ids)}")
    print(f"  duplicate request_ids         : {len(report.duplicate_ids)}")
    if report.duplicate_ids:
        print(f"      {', '.join(report.duplicate_ids)} (first occurrence scored)")
    if report.absent_columns:
        print(f"  graded columns absent         : {', '.join(report.absent_columns)}")
        print("      (absent columns read as blank and will fail their checks)")


def print_report(report: ScoreReport, predictions_path: Path, gold_path: Path) -> None:
    """Print the full scoring report to stdout."""
    print("=" * 96)
    print("Buy or Wait? - prediction scorer")
    print(f"predictions : {predictions_path}")
    print(f"gold        : {gold_path}")
    print("=" * 96)
    print()
    print_coverage(report)
    print()
    print("PER-COLUMN CHECKS")
    print("  amount 'exact match' is numeric equality: 620.40 and 620.4 are the same value.")
    print("  payment_plan 'exact string match' is literal; 'normalized match' re-renders both")
    print("  through format_plan_amount first, so a gap between the two rows is formatting only.")
    print(
        "  'sums to requested_amount' applies to "
        + ", ".join(sorted(SUM_CHECKED_METHODS))
        + " only;"
    )
    print("  an installments plan follows a supplied payment option, so it is not sum-checked.")
    print()
    print(render_check_table(report.checks))
    print()
    print(f"ROW-LEVEL MISMATCHES ({len(report.diffs)})")
    print(render_diff_table(report.diffs))
    print()


STATUS_ORDER = {"not_affordable": 0, "affordable_later": 1, "affordable_with_plan": 2, "affordable_now": 3}


def print_reliability(gold_rows: list[dict[str, str]], prediction_rows: list[dict[str, str]]) -> None:
    """How far off the wrong answers are, not just how many are right.

    amount_safe_to_pay error bands and worst error; amounts overstated by more than 10%
    (the direction that could let a user overspend); status distance in steps, and rows
    more permissive than gold.
    """
    predicted = {row.get(ID_COLUMN, ""): row for row in prediction_rows}
    errors, overstated, steps, permissive = [], [], [], []
    for gold in gold_rows:
        request_id = gold.get(ID_COLUMN, "")
        row = predicted.get(request_id)
        if row is None:
            continue
        gold_amount = parse_amount(gold.get("amount_safe_to_pay", ""))
        our_amount = parse_amount(row.get("amount_safe_to_pay", ""))
        if gold_amount is not None and our_amount is not None:
            error = abs(our_amount - gold_amount) / max(1.0, abs(gold_amount)) * 100
            errors.append((error, request_id))
            if our_amount > gold_amount and error > 10:
                overstated.append(f"{request_id}(+{error:.0f}%)")
        gold_rank = STATUS_ORDER.get(gold.get("affordability_status", ""))
        our_rank = STATUS_ORDER.get(row.get("affordability_status", ""))
        if gold_rank is not None and our_rank is not None:
            steps.append(abs(our_rank - gold_rank))
            if our_rank > gold_rank:
                permissive.append(request_id)
    if not errors:
        return
    values = sorted(error for error, _ in errors)
    worst_error, worst_id = max(errors)
    total = len(values)
    print("RELIABILITY (how wrong the wrong answers are)")
    print(
        "  amount_safe_to_pay error bands: "
        + " | ".join(f"<= {band}%: {sum(v <= band for v in values)}/{total}" for band in (1, 5, 10, 25))
        + f" | > 25%: {sum(v > 25 for v in values)}/{total}"
    )
    print(f"  amount error median {values[total // 2]:.1f}% | worst {worst_error:.1f}% ({worst_id})")
    print(f"  amounts overstated by more than 10% (risky): {len(overstated)} {overstated}")
    print("  status steps from gold: " + " | ".join(f"{k}: {steps.count(k)}" for k in range(4)))
    print(f"  status more permissive than gold (risky): {len(permissive)} {permissive}")
    print()


def parse_arguments(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments for the scorer."""
    parser = argparse.ArgumentParser(
        description="Score Buy or Wait? predictions against the labelled sample requests.",
    )
    parser.add_argument("--predictions", required=True, help="path to the predictions CSV")
    parser.add_argument(
        "--gold",
        default=str(DEFAULT_GOLD_PATH),
        help="path to the labelled gold CSV (default: dataset/sample_requests.csv)",
    )
    parser.add_argument(
        "--events",
        default=str(DEFAULT_EVENTS_PATH),
        help="path to financial_events.csv, used for the non-fixed-target check",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """Run the scorer. Always returns 0 - this measures, it does not gate."""
    arguments = parse_arguments(argv)
    predictions_path = Path(arguments.predictions).resolve()
    gold_path = Path(arguments.gold).resolve()
    events_path = Path(arguments.events).resolve()

    try:
        if not predictions_path.is_file():
            print(f"ERROR: predictions file not found: {predictions_path}")
            return 0
        if not gold_path.is_file():
            print(f"ERROR: gold file not found: {gold_path}")
            return 0

        flexibility_by_event = load_flexibility_by_event(events_path)
        if not flexibility_by_event:
            print(f"NOTE: {events_path} not readable; non-fixed-target check reports n/a.")

        gold_rows, prediction_rows = read_csv_rows(gold_path), read_csv_rows(predictions_path)
        report = build_report(gold_rows, prediction_rows, flexibility_by_event)
        print_report(report, predictions_path, gold_path)
        print_reliability(gold_rows, prediction_rows)
    except Exception:  # noqa: BLE001 - a measuring tool never gates the build
        print("ERROR: scoring failed; traceback follows on stderr.")
        traceback.print_exc(file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
