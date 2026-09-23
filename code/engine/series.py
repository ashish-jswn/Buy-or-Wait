"""The recurring-series record shared by expense and income detection.

Lives in its own module so ``engine.state`` (expenses) and ``engine.income`` (income) can
both build it without importing each other.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class RecurringSeries:
    """A detected repeating series, with the cadence to project it forward.

    ``amount`` is unsigned and in the user's home currency; ``direction`` gives the sign.
    """

    key: tuple[str, str, str]
    category: str
    direction: str
    amount: Decimal
    last_seen: date
    period_days: int
    day_of_month: Optional[int]
    occurrences: int
    latest_event_id: str

    @property
    def is_monthly(self) -> bool:
        """True when the series repeats on a day-of-month cadence."""
        return self.day_of_month is not None
