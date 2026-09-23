"""Check code/formatting.py against every amount in dataset/sample_requests.csv.

The rules in formatting.py were derived from these 25 rows, so the file is the
specification: if a rule ever stops reproducing a gold amount exactly, that is a
real contract change, not a test to relax. Runs with no API key.
"""

import csv
from pathlib import Path

import pytest

from decimal import Decimal

from common.formatting import format_plan_amount, format_safe_amount, parse_amount, round_money

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLD_PATH = REPO_ROOT / "dataset" / "sample_requests.csv"

NO_PLAN_LITERAL = "none"


def load_gold_rows() -> list[dict[str, str]]:
    """Read the labelled sample requests as a list of dicts."""
    with GOLD_PATH.open(newline="", encoding="utf-8-sig") as handle:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(handle)]


def plan_amount_cases() -> list[tuple[str, str]]:
    """Every (request_id, amount-as-written) pair across all gold payment_plan entries."""
    cases: list[tuple[str, str]] = []
    for row in load_gold_rows():
        plan = row["payment_plan"]
        if plan.lower() == NO_PLAN_LITERAL:
            continue
        for entry in plan.split("|"):
            _, _, amount = entry.strip().partition(":")
            cases.append((row["request_id"], amount))
    return cases


def safe_amount_cases() -> list[tuple[str, str, str]]:
    """Every (request_id, column, amount-as-written) for the strip-zeros columns."""
    cases: list[tuple[str, str, str]] = []
    for row in load_gold_rows():
        for column in ("amount_safe_to_pay", "requested_amount"):
            if row[column]:
                cases.append((row["request_id"], column, row[column]))
    return cases


@pytest.mark.parametrize("request_id,written", plan_amount_cases())
def test_format_plan_amount_reproduces_every_gold_plan_amount(request_id: str, written: str) -> None:
    """Re-formatting a parsed gold plan amount returns the identical string."""
    assert format_plan_amount(float(written)) == written, request_id


@pytest.mark.parametrize("request_id,column,written", safe_amount_cases())
def test_format_safe_amount_reproduces_every_gold_safe_amount(
    request_id: str, column: str, written: str
) -> None:
    """Re-formatting a parsed gold safe/requested amount returns the identical string."""
    assert format_safe_amount(float(written)) == written, f"{request_id}.{column}"


def test_every_gold_row_is_covered() -> None:
    """Guard against the parametrised cases silently going empty."""
    rows = load_gold_rows()
    assert len(rows) == 25
    # 18 rows carry a plan: 6 full_payment + 6 wait at one entry each, 1 partial_payment
    # at two, and 5 installments at three each = 29 amounts. The other 7 rows are 'none'.
    assert len(plan_amount_cases()) == 29
    assert len(safe_amount_cases()) == 50


@pytest.mark.parametrize(
    "value,expected",
    [
        (25256, "25256"),
        (25256.0, "25256"),
        ("25256", "25256"),
        (620.4, "620.40"),
        (620.40, "620.40"),
        ("620.4", "620.40"),
        (15952906.67, "15952906.67"),
        (0, "0"),
        (0.0, "0"),
        (996.6, "996.60"),
        (0.005, "0.01"),
    ],
)
def test_format_plan_amount_shapes(value: object, expected: str) -> None:
    """Whole numbers render bare; everything else renders with exactly two decimals."""
    assert format_plan_amount(value) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value,expected",
    [
        (603.3, "603.3"),
        (603.30, "603.3"),
        ("603.30", "603.3"),
        (433.4, "433.4"),
        (25256, "25256"),
        (25256.00, "25256"),
        (0, "0"),
        (0.0, "0"),
        (17229139.2, "17229139.2"),
        (83.05, "83.05"),
        (0.005, "0.01"),
    ],
)
def test_format_safe_amount_shapes(value: object, expected: str) -> None:
    """Trailing zeros and a trailing dot are stripped after rounding to 2dp."""
    assert format_safe_amount(value) == expected  # type: ignore[arg-type]


def test_rounding_is_half_up_not_bankers() -> None:
    """0.125 rounds to 0.13, not 0.12 — Decimal half-up, not float/banker's."""
    assert str(round_money("0.125")) == "0.13"
    assert str(round_money("0.135")) == "0.14"


@pytest.mark.parametrize("bad", ["", "   ", "abc", "1.2.3"])
def test_unparseable_amounts_raise(bad: str) -> None:
    """A blank or non-numeric amount is an error, never a silent zero."""
    with pytest.raises(ValueError):
        format_plan_amount(bad)
    with pytest.raises(ValueError):
        format_safe_amount(bad)


# --------------------------------------------------------------------------
# parse_amount — the reading half of the contract
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        # Indian grouping. gpt-5 returned exactly this for image_02.png, and a
        # digits-only strip reads it as 10,000,000 instead of 100,000.
        ("1,00,000.00", "100000.00"),
        ("1,00,000", "100000"),
        ("12,34,567.89", "1234567.89"),
        ("1,00,00,000", "10000000"),
        # Western grouping.
        ("100,000.00", "100000.00"),
        ("100,000", "100000"),
        ("1,234,567.89", "1234567.89"),
        ("25,256", "25256"),
        # European: dot grouping, comma decimal. "43.339.000" is verbatim from
        # request_43's Indonesian request_text.
        ("43.339.000", "43339000"),
        ("1.302,40", "1302.40"),
        ("620,40", "620.40"),
        ("15.952.906,67", "15952906.67"),
        # Plain, no grouping at all.
        ("25256", "25256"),
        ("620.40", "620.40"),
        ("620.4", "620.4"),
        ("0", "0"),
        ("0.01", "0.01"),
    ],
)
def test_parse_amount_grouping_conventions(text: str, expected: str) -> None:
    """Indian, Western, European and plain amounts all parse to the same value."""
    assert parse_amount(text) == Decimal(expected)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("IDR 42750000", "42750000"),
        ("EUR 1,574.40", "1574.40"),
        ("INR 1,00,000.00", "100000.00"),
        ("ZAR 25 256", "25256"),
        ("  620.40  ", "620.40"),
        ("Balance Due: 1,00,000.00", "100000.00"),
        ("$1,234.56", "1234.56"),
        ("1234.56 USD", "1234.56"),
    ],
)
def test_parse_amount_ignores_currency_and_surrounding_text(text: str, expected: str) -> None:
    """A currency code, symbol, label or stray whitespace does not defeat parsing."""
    assert parse_amount(text) == Decimal(expected)


@pytest.mark.parametrize(
    "text,expected", [("-53.00", "-53.00"), ("-1,00,000.00", "-100000.00"), ("+620,40", "620.40")]
)
def test_parse_amount_handles_sign(text: str, expected: str) -> None:
    """A leading sign is preserved."""
    assert parse_amount(text) == Decimal(expected)


@pytest.mark.parametrize(
    "text,expected,why",
    [
        ("1,234", "1234", "three trailing digits read as grouping, not a decimal"),
        ("1,23", "1.23", "two trailing digits read as a decimal separator"),
        ("1,2", "1.2", "one trailing digit reads as a decimal separator"),
        ("1.234", "1234", "same rule applies to a dot"),
        ("1.23", "1.23", "same rule applies to a dot"),
        ("12,3456", "12.3456", "four trailing digits is no grouping convention, so decimal"),
        ("1234,567", "1234.567", "a four-digit leading group is no grouping convention either"),
        # A leading zero is never produced by digit grouping, so these stay decimals
        # even with three trailing digits. Without this, round_money("0.125") -> 125.
        ("0.125", "0.125", "leading zero rules out grouping"),
        ("0,125", "0.125", "leading zero rules out grouping, comma form"),
        ("0.135", "0.135", "leading zero rules out grouping"),
    ],
)
def test_parse_amount_single_separator_tie_break(text: str, expected: str, why: str) -> None:
    """One separator is ambiguous; three trailing digits means grouping, else decimal.

    No amount in this dataset carries three decimal places, which is what makes
    the tie-break safe for dataset text. The leading-zero carve-out protects
    genuine sub-1 values produced internally, which routinely have three or more.
    Documented here because it is a real judgement call.
    """
    assert parse_amount(text) == Decimal(expected), why


@pytest.mark.parametrize(
    "value,expected",
    [(25256, "25256"), (620.4, "620.4"), (Decimal("620.40"), "620.40"), (0, "0")],
)
def test_parse_amount_passes_through_numeric_types(value: object, expected: str) -> None:
    """Ints, floats and Decimals convert without going near the text path."""
    assert parse_amount(value) == Decimal(expected)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "abc", "1.2.3", "1,2,3", "none", "-", ".", ",", "1,2345,678", "1,00,0000", True],
)
def test_parse_amount_rejects_junk_and_implausible_grouping(bad: object) -> None:
    """Nothing parseable, or a grouping pattern no convention produces, is an error."""
    with pytest.raises(ValueError):
        parse_amount(bad)  # type: ignore[arg-type]


def test_parse_amount_feeds_the_formatters() -> None:
    """The reading and writing halves compose: parse then render round-trips."""
    assert format_plan_amount(parse_amount("1,00,000.00")) == "100000"
    assert format_plan_amount(parse_amount("1.302,40")) == "1302.40"
    assert format_safe_amount(parse_amount("1,00,000.00")) == "100000"
    assert format_safe_amount(parse_amount("620,40")) == "620.4"
