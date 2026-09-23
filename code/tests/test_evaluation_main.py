"""One test per defect the scorer is supposed to catch.

These were an ad-hoc probe file during the scorer's first run; each injected
defect now lives here as its own case, asserting the specific check that catches
it rather than eyeballing a printed table. Runs with no API key — the scorer is
standard library only and never calls a model.
"""

import csv
from pathlib import Path

import pytest

from evaluation.main import (
    ScoreReport,
    build_report,
    is_valid_plan_format,
    main,
    normalize_plan,
    plan_sums_to_requested,
    plans_match_normalized,
    targets_only_non_fixed_events,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

PREDICTION_COLUMNS = (
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
)

# One correct gold row, reproduced exactly — the control case every defect is a
# mutation of. Taken verbatim from dataset/sample_requests.csv.
CLEAN_GOLD_ROW = {
    "request_id": "request_16",
    "user_id": "user_16",
    "request_date": "2023-08-12",
    "request_type": "housing",
    "requested_amount": "122500",
    "desired_completion_date": "2023-10-11",
    "allows_partial_payment": "true",
    "request_text": "Can I pay the rental deposit by the requested date?",
    "amount_safe_to_pay": "122500",
    "affordability_status": "affordable_now",
    "recommended_payment_method": "full_payment",
    "payment_plan": "2023-08-12:122500",
    "earliest_date_for_full_payment": "2023-08-12",
    "spending_changes_needed": "none",
    "decision_explanation": "Pay INR 122,500 today.",
}

# Real event ids from dataset/financial_events.csv, used by the flexibility checks.
FIXED_EVENT_ID = "event_01"
STOPPABLE_EVENT_ID = "event_1815"
REDUCIBLE_OR_STOPPABLE_EVENT_ID = "event_1816"
UNKNOWN_EVENT_ID = "event_99999"

FLEXIBILITY_BY_EVENT = {
    FIXED_EVENT_ID: "fixed",
    STOPPABLE_EVENT_ID: "stoppable",
    REDUCIBLE_OR_STOPPABLE_EVENT_ID: "reducible_or_stoppable",
}


def write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, str]]) -> Path:
    """Write rows to a CSV at `path` and return the path."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(rows)
    return path


def prediction_from(**overrides: str) -> dict[str, str]:
    """Build a prediction row that matches the control gold row, with overrides."""
    row = {column: CLEAN_GOLD_ROW[column] for column in PREDICTION_COLUMNS}
    row.update(overrides)
    return row


def score(predictions: list[dict[str, str]], gold: list[dict[str, str]] | None = None) -> ScoreReport:
    """Score prediction rows against the control gold row (or a supplied gold set)."""
    return build_report(
        gold or [CLEAN_GOLD_ROW],
        predictions,
        FLEXIBILITY_BY_EVENT,
    )


def rate(report: ScoreReport, key: str) -> tuple[int, int]:
    """Return (passed, applicable) for one check."""
    result = report.checks[key]
    return result.passed, result.applicable


# --------------------------------------------------------------------------
# control
# --------------------------------------------------------------------------


def test_a_perfect_prediction_passes_every_check() -> None:
    """The unmutated gold row must score clean, or every defect test below is meaningless."""
    report = score([prediction_from()])
    assert report.diffs == []
    for key, result in report.checks.items():
        assert result.passed == result.applicable, key


# --------------------------------------------------------------------------
# row coverage
# --------------------------------------------------------------------------


def test_missing_row_is_reported_not_crashed() -> None:
    """A gold row with no prediction is listed as missing and left out of the denominators."""
    report = score([])
    assert report.missing_ids == ["request_16"]
    assert report.scored_ids == []
    assert rate(report, "status_exact") == (0, 0)


def test_extra_row_is_reported_and_not_scored() -> None:
    """A prediction row with no gold row is listed as extra and never scored."""
    report = score([prediction_from(), prediction_from(request_id="request_99")])
    assert report.extra_ids == ["request_99"]
    assert report.scored_ids == ["request_16"]
    assert rate(report, "status_exact") == (1, 1)


def test_duplicate_request_id_is_reported_and_first_wins() -> None:
    """A repeated request_id is flagged; the first occurrence is the one scored."""
    report = score(
        [
            prediction_from(),
            prediction_from(affordability_status="not_affordable"),
        ]
    )
    assert report.duplicate_ids == ["request_16"]
    assert rate(report, "status_exact") == (1, 1)


def test_absent_graded_column_is_reported_and_fails_its_check() -> None:
    """A predictions file missing a graded column reports it and scores it blank."""
    row = prediction_from()
    del row["payment_plan"]
    report = build_report([CLEAN_GOLD_ROW], [row], FLEXIBILITY_BY_EVENT)
    assert "payment_plan" in report.absent_columns
    assert rate(report, "plan_exact") == (0, 1)


# --------------------------------------------------------------------------
# amount_safe_to_pay
# --------------------------------------------------------------------------


def test_sub_one_percent_amount_error_fails_exact_but_passes_tolerance() -> None:
    """603.3 vs 605.0 is 0.28% off: exact fails, within-1% passes."""
    gold = dict(CLEAN_GOLD_ROW, amount_safe_to_pay="603.3", requested_amount="620.4")
    report = score([prediction_from(amount_safe_to_pay="605.0")], gold=[gold])
    assert rate(report, "amount_exact") == (0, 1)
    assert rate(report, "amount_tolerance") == (1, 1)


def test_large_amount_error_fails_both_amount_checks() -> None:
    """An amount well outside 1% fails the tolerance check too."""
    report = score([prediction_from(amount_safe_to_pay="0")])
    assert rate(report, "amount_exact") == (0, 1)
    assert rate(report, "amount_tolerance") == (0, 1)


def test_amount_formatting_difference_is_not_a_mismatch() -> None:
    """122500.00 and 122500 are the same number, so exact match holds."""
    report = score([prediction_from(amount_safe_to_pay="122500.00")])
    assert rate(report, "amount_exact") == (1, 1)


# --------------------------------------------------------------------------
# payment_plan
# --------------------------------------------------------------------------


def test_trailing_zero_difference_fails_exact_string_but_passes_normalized() -> None:
    """620.4 vs 620.40 is formatting only — this is the gap the two rows exist to show."""
    gold = dict(CLEAN_GOLD_ROW, payment_plan="2026-01-03:620.40", requested_amount="620.4")
    report = score([prediction_from(payment_plan="2026-01-03:620.4")], gold=[gold])
    assert rate(report, "plan_exact") == (0, 1)
    assert rate(report, "plan_normalized") == (1, 1)


def test_wrong_plan_amount_fails_normalized_too() -> None:
    """A real arithmetic difference fails both plan-match rows, not just the literal one."""
    report = score([prediction_from(payment_plan="2023-08-12:122499")])
    assert rate(report, "plan_exact") == (0, 1)
    assert rate(report, "plan_normalized") == (0, 1)


def test_out_of_order_plan_dates_fail_the_format_check() -> None:
    """payment_plan entries must be chronological."""
    plan = "2026-07-04:166.61|2026-01-01:5"
    report = score([prediction_from(payment_plan=plan)])
    assert rate(report, "plan_format") == (0, 1)
    assert is_valid_plan_format(plan, "full_payment") is False


def test_not_recommended_carrying_a_plan_fails_the_format_check() -> None:
    """not_recommended must use payment_plan 'none'."""
    report = score(
        [
            prediction_from(
                recommended_payment_method="not_recommended",
                payment_plan="2020-01-01:10",
            )
        ]
    )
    assert rate(report, "plan_format") == (0, 1)


@pytest.mark.parametrize(
    "plan",
    ["2023-08-12", "2023-8-12:100", "12-08-2023:100", "2023-08-12:abc", "2023-08-12:-5", ""],
)
def test_malformed_plan_entries_fail_the_format_check(plan: str) -> None:
    """A missing amount, a loose date, a non-number, a negative, or an empty plan are all invalid."""
    assert is_valid_plan_format(plan, "full_payment") is False
    assert normalize_plan(plan) is None
    assert plans_match_normalized("2023-08-12:122500", plan) is False


def test_plan_that_does_not_sum_to_requested_amount_fails_the_sum_check() -> None:
    """A full_payment plan totalling 171.61 against a requested 166.61 is caught."""
    gold = dict(CLEAN_GOLD_ROW, requested_amount="166.61")
    report = score([prediction_from(payment_plan="2026-07-04:166.61|2026-07-05:5")], gold=[gold])
    assert rate(report, "plan_sum") == (0, 1)


def test_partial_payment_plan_must_sum_to_requested_amount() -> None:
    """The two partial payments must add up to the full requested amount."""
    gold = dict(CLEAN_GOLD_ROW, requested_amount="39660")
    good = "2024-09-04:28820|2024-09-15:10840"
    bad = "2024-09-04:28820|2024-09-15:10000"
    assert plan_sums_to_requested(good, 39660.0) is True
    assert plan_sums_to_requested(bad, 39660.0) is False
    report = score(
        [prediction_from(recommended_payment_method="partial_payment", payment_plan=bad)],
        gold=[gold],
    )
    assert rate(report, "plan_sum") == (0, 1)


def test_installment_plan_is_not_sum_checked() -> None:
    """An installments total legitimately exceeds requested_amount (it carries a financing fee)."""
    gold = dict(CLEAN_GOLD_ROW, requested_amount="46018000")
    plan = "2025-08-08:15952906.67|2025-09-07:15952906.67|2025-10-07:15952906.67"
    report = score(
        [prediction_from(recommended_payment_method="installments", payment_plan=plan)],
        gold=[gold],
    )
    assert rate(report, "plan_sum") == (0, 0)
    assert rate(report, "plan_format") == (1, 1)


def test_plan_none_is_not_sum_checked() -> None:
    """'none' is a valid plan and has nothing to add up."""
    report = score(
        [prediction_from(recommended_payment_method="not_recommended", payment_plan="none")]
    )
    assert rate(report, "plan_sum") == (0, 0)
    assert rate(report, "plan_format") == (1, 1)


# --------------------------------------------------------------------------
# earliest_date_for_full_payment
# --------------------------------------------------------------------------


def test_blank_date_is_a_real_value_to_match() -> None:
    """A blank gold date must be matched by a blank prediction, not by any date."""
    gold = dict(CLEAN_GOLD_ROW, earliest_date_for_full_payment="")
    assert rate(score([prediction_from(earliest_date_for_full_payment="")], gold=[gold]),
                "date_exact") == (1, 1)
    assert rate(score([prediction_from(earliest_date_for_full_payment="2023-08-12")], gold=[gold]),
                "date_exact") == (0, 1)


def test_blank_prediction_against_a_real_gold_date_is_a_mismatch() -> None:
    """The reverse also fails, and shows up in the diff table."""
    report = score([prediction_from(earliest_date_for_full_payment="")])
    assert rate(report, "date_exact") == (0, 1)
    assert any(d.column == "earliest_date_for_full_payment" for d in report.diffs)


# --------------------------------------------------------------------------
# spending_changes_needed
# --------------------------------------------------------------------------


def test_change_targeting_a_fixed_event_fails_the_non_fixed_check() -> None:
    """Only reducible/stoppable/reducible_or_stoppable events may be changed."""
    report = score([prediction_from(spending_changes_needed=f"stop:{FIXED_EVENT_ID}")])
    assert rate(report, "changes_non_fixed") == (0, 1)


def test_change_targeting_an_unknown_event_fails_the_non_fixed_check() -> None:
    """Naming an event id the dataset does not contain is a failure, not a pass."""
    report = score(
        [prediction_from(spending_changes_needed=f"reduce_to:{UNKNOWN_EVENT_ID}:5")]
    )
    assert rate(report, "changes_non_fixed") == (0, 1)


def test_changes_targeting_non_fixed_events_pass() -> None:
    """The two-change gold shape from request_21 passes the flexibility check."""
    value = f"stop:{STOPPABLE_EVENT_ID}|reduce_to:{REDUCIBLE_OR_STOPPABLE_EVENT_ID}:23.50"
    report = score([prediction_from(spending_changes_needed=value)])
    assert rate(report, "changes_non_fixed") == (1, 1)
    assert targets_only_non_fixed_events(value, FLEXIBILITY_BY_EVENT) is True


def test_none_is_not_flexibility_checked() -> None:
    """'none' has no target, so the non-fixed check does not apply to that row."""
    report = score([prediction_from(spending_changes_needed="none")])
    assert rate(report, "changes_non_fixed") == (0, 0)


@pytest.mark.parametrize(
    "value", ["stop", "stop:", "reduce_to:event_1815", "cancel:event_1815", "stop event_1815"]
)
def test_malformed_spending_change_actions_fail(value: str) -> None:
    """An action that is not stop:<id> or reduce_to:<id>:<amount> never passes."""
    assert targets_only_non_fixed_events(value, FLEXIBILITY_BY_EVENT) is False


def test_flexibility_check_reports_na_when_the_events_file_is_unavailable() -> None:
    """With no flexibility data the check degrades to n/a rather than failing every row."""
    report = build_report(
        [CLEAN_GOLD_ROW],
        [prediction_from(spending_changes_needed=f"stop:{FIXED_EVENT_ID}")],
        {},
    )
    assert rate(report, "changes_non_fixed") == (0, 0)


# --------------------------------------------------------------------------
# the tool never gates
# --------------------------------------------------------------------------


def test_missing_predictions_file_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """A path that does not exist is reported clearly and still exits 0."""
    exit_code = main(["--predictions", str(tmp_path / "nope.csv")])
    assert exit_code == 0
    assert "not found" in capsys.readouterr().out


def test_missing_gold_file_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Same for an unreadable gold path."""
    predictions = write_csv(tmp_path / "p.csv", PREDICTION_COLUMNS, [prediction_from()])
    exit_code = main(
        ["--predictions", str(predictions), "--gold", str(tmp_path / "nope.csv")]
    )
    assert exit_code == 0
    assert "not found" in capsys.readouterr().out


def test_end_to_end_run_against_the_real_gold_file_exits_zero(tmp_path: Path) -> None:
    """A full run over dataset/sample_requests.csv returns 0."""
    predictions = write_csv(tmp_path / "p.csv", PREDICTION_COLUMNS, [prediction_from()])
    assert main(["--predictions", str(predictions)]) == 0
