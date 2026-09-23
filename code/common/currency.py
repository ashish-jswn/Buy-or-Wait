"""Currency conversion. Exact-match lookup only — see DATASET_FACTS D2."""

from datetime import date
from decimal import Decimal


class MissingRateError(LookupError):
    """Raised when no exact (settlement_date, from, to) rate row exists.

    DATASET_FACTS D2: all 140 foreign-currency events in this dataset have an exact
    match, so this never fires on the supplied data. It is the canary if that stops
    being true — never soften it into a nearest-date or inverse-pair fallback.
    """


def convert(
    amount: Decimal,
    from_currency: str,
    to_currency: str,
    on: date,
    rates: dict[tuple[date, str, str], Decimal],
) -> Decimal:
    """Convert `amount` into `to_currency` using the rate for exactly that date.

    Returns the amount unchanged when the currencies already match. Raises
    MissingRateError when no exact row exists — no nearest-prior-date fallback and
    no inverting the reverse pair.
    """
    if from_currency == to_currency:
        return amount
    rate = rates.get((on, from_currency, to_currency))
    if rate is None:
        raise MissingRateError(
            f"no exchange rate for {from_currency}->{to_currency} on {on.isoformat()}"
        )
    return amount * rate
