"""Number formatting and parsing — the amount contract for the whole solution.

Writing. Two output shapes appear in the gold data, and they are not the same:

* ``payment_plan`` entries: a whole number renders bare, anything else with
  exactly two decimals — ``25256``, ``620.40``, ``15952906.67``.
* ``amount_safe_to_pay`` (and ``requested_amount``): rounded to two decimals,
  then trailing zeros and any trailing dot stripped — ``603.3``, ``433.4``,
  ``25256``.

Both were derived from all 25 rows of ``dataset/sample_requests.csv`` with no
exceptions (79/79 amounts); ``tests/test_formatting.py`` re-checks that against
every amount in the file.

Reading. ``parse_amount`` is the single entry point for turning free text into a
Decimal, and it must survive three grouping conventions seen in this project:

* Indian — ``1,00,000.00``. The vision probe returned exactly this for
  ``image_02.png``; a digits-only strip reads it as 10,000,000.
* Western — ``100,000.00``.
* European — ``1.302,40`` and ``43.339.000`` (the latter appears verbatim in
  ``request_43``'s Indonesian ``request_text``).

Everything that reads or writes a number goes through this module, so the
conventions never get mixed up at a call site.
"""

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Union

Number = Union[str, int, float, Decimal]

# Money is carried to two decimals everywhere in this dataset.
MONEY_QUANTUM = Decimal("0.01")
# Half-up, not banker's rounding: 0.125 -> 0.13, matching how the gold amounts read.
MONEY_ROUNDING = ROUND_HALF_UP

# The two characters that act as either a decimal point or a grouping separator.
SEPARATORS = (".", ",")
# A single separator followed by this many digits reads as grouping, not a decimal
# point: no amount in this dataset carries three decimal places.
GROUPING_TRAILING_DIGITS = 3
# Leading group is 1-3 digits; later groups are 3 (Western) or 2 (Indian).
LEADING_GROUP_SIZES = (1, 2, 3)
FOLLOWING_GROUP_SIZES = (2, 3)

# The numeric core of a string, ignoring any currency code, symbol or stray text.
NUMERIC_CORE_PATTERN = re.compile(r"[-+]?\d[\d., \s]*\d|[-+]?\d")


def _split_sign(text: str) -> tuple[int, str]:
    """Peel a leading sign off the numeric core, returning (sign, rest)."""
    if text.startswith("-"):
        return -1, text[1:]
    if text.startswith("+"):
        return 1, text[1:]
    return 1, text


def _is_grouping_leader(text: str) -> bool:
    """True when `text` could be the first group of a grouped integer.

    One to three digits with no leading zero. The leading-zero rule is what
    keeps "0.125" a genuine three-decimal number rather than the grouped 125.
    """
    return (
        text.isdigit()
        and len(text) in LEADING_GROUP_SIZES
        and not (len(text) > 1 and text.startswith("0"))
        and text != "0"
    )


def _choose_decimal_separator(core: str) -> str:
    """Decide which of '.' and ',' is the decimal point in `core`, or '' if neither.

    When both appear, the rightmost one is the decimal point and the other is
    grouping. When only one appears more than once it can only be grouping.
    When one appears exactly once it is a decimal point, unless exactly three
    digits follow it *and* what precedes it could start a grouped integer — that
    combination reads as grouping, because no amount here has three decimals.
    """
    present = [separator for separator in SEPARATORS if separator in core]
    if not present:
        return ""
    if len(present) == 2:
        return max(present, key=core.rfind)
    separator = present[0]
    if core.count(separator) > 1:
        return ""
    index = core.rfind(separator)
    trailing = len(core) - index - 1
    looks_grouped = trailing == GROUPING_TRAILING_DIGITS and _is_grouping_leader(core[:index])
    return "" if looks_grouped else separator


def _strip_grouping(digits: str, separator: str) -> str:
    """Remove a grouping separator, rejecting implausible group sizes.

    Accepts Western (3-digit groups) and Indian (2-digit groups) shapes; raises
    ValueError on anything else, so "1.2.3" is an error rather than 123.
    """
    if not separator:
        if not digits.isdigit():
            raise ValueError(f"not a number: {digits!r}")
        return digits
    groups = digits.split(separator)
    if len(groups) < 2 or not all(group.isdigit() for group in groups):
        raise ValueError(f"not a number: {digits!r}")
    if len(groups[0]) not in LEADING_GROUP_SIZES:
        raise ValueError(f"implausible digit grouping: {digits!r}")
    if any(len(group) not in FOLLOWING_GROUP_SIZES for group in groups[1:]):
        raise ValueError(f"implausible digit grouping: {digits!r}")
    return "".join(groups)


def parse_amount(text: Number) -> Decimal:
    """Parse an amount out of free text into a Decimal.

    Handles Indian ("1,00,000.00"), Western ("100,000.00") and European
    ("1.302,40", "43.339.000") grouping, a leading or trailing currency code or
    symbol, and a leading sign. Raises ValueError when nothing parses or the
    digit grouping is implausible.
    """
    if isinstance(text, Decimal):
        return text
    if isinstance(text, bool):  # bool is an int subclass; never a money value
        raise ValueError(f"not a number: {text!r}")
    if isinstance(text, float):
        return Decimal(repr(text))
    if isinstance(text, int):
        return Decimal(text)

    match = NUMERIC_CORE_PATTERN.search(str(text))
    if match is None:
        raise ValueError(f"not a number: {text!r}")
    sign, core = _split_sign(match.group(0))
    core = re.sub(r"[\s ]", "", core)
    if not core:
        raise ValueError(f"not a number: {text!r}")

    decimal_separator = _choose_decimal_separator(core)
    if decimal_separator:
        whole, _, fraction = core.rpartition(decimal_separator)
        if not fraction.isdigit():
            raise ValueError(f"not a number: {text!r}")
        grouping = next((s for s in SEPARATORS if s != decimal_separator and s in whole), "")
        whole = _strip_grouping(whole, grouping) if whole else "0"
        candidate = f"{whole}.{fraction}"
    else:
        grouping = next((s for s in SEPARATORS if s in core), "")
        candidate = _strip_grouping(core, grouping)

    try:
        return Decimal(candidate) * sign
    except InvalidOperation as error:
        raise ValueError(f"not a number: {text!r}") from error


def to_decimal(value: Number) -> Decimal:
    """Convert a string/int/float/Decimal amount to Decimal.

    Delegates string handling to :func:`parse_amount` so there is exactly one
    parsing path in the codebase. Floats go through ``repr`` so 620.4 becomes
    Decimal("620.4") rather than the full binary expansion.
    """
    return parse_amount(value)


def round_money(value: Number) -> Decimal:
    """Round an amount to two decimals, half-up."""
    return to_decimal(value).quantize(MONEY_QUANTUM, rounding=MONEY_ROUNDING)


def format_plan_amount(value: Number) -> str:
    """Render an amount for a ``payment_plan`` entry.

    A whole number renders with no decimal point; anything else renders with
    exactly two decimals. Returns e.g. "25256", "620.40", "15952906.67".
    """
    rounded = round_money(value)
    if rounded == rounded.to_integral_value():
        return str(rounded.to_integral_value())
    return f"{rounded:f}"


def format_safe_amount(value: Number) -> str:
    """Render an amount for ``amount_safe_to_pay`` and ``requested_amount``.

    Rounds to two decimals, then strips trailing zeros and any trailing dot.
    Returns e.g. "603.3", "433.4", "25256".
    """
    rounded = round_money(value)
    text = f"{rounded:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
