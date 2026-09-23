"""Typed records for the nine dataset CSVs.

Deliberately dumb: field access, conversions, and predicates that read straight off a
single row. Anything that reasons across rows belongs in ``engine``.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from config import (
    DIRECTION_CREDIT,
    DIRECTION_DEBIT,
    FLEXIBILITY_FIXED,
    REDUCIBLE_FLEXIBILITIES,
    STOPPABLE_FLEXIBILITIES,
)


@dataclass(frozen=True)
class Profile:
    """One user's financial profile."""

    user_id: str
    home_currency: str
    current_available_balance: Decimal
    minimum_balance_to_keep: Decimal
    financial_priorities: tuple[str, ...]
    protected_categories: tuple[str, ...]
    categories_willing_to_reduce: tuple[str, ...]
    categories_willing_to_stop: tuple[str, ...]
    payment_methods: tuple[str, ...]
    max_installment_months: Optional[int]

    def accepts(self, method: str) -> bool:
        """True when the user will consider this payment method."""
        return method in self.payment_methods

    def may_reduce(self, category: str) -> bool:
        """True when the user permits reducing spending in this category."""
        return category in self.categories_willing_to_reduce

    def may_stop(self, category: str) -> bool:
        """True when the user permits stopping spending in this category."""
        return category in self.categories_willing_to_stop


@dataclass(frozen=True)
class Event:
    """One financial event. ``amount`` is None when the row's amount is blank."""

    event_id: str
    user_id: str
    event_type: str
    description: str
    category: str
    direction: str
    amount: Optional[Decimal]
    currency: str
    event_date: date
    settlement_date: Optional[date]
    status: str
    linked_event_id: Optional[str]
    flexibility: str
    minimum_allowed_amount: Optional[Decimal]

    @property
    def cash_date(self) -> date:
        """The date this event moves cash: its settlement date, else its event date."""
        return self.settlement_date or self.event_date

    @property
    def is_debit(self) -> bool:
        """True when this event takes money out."""
        return self.direction == DIRECTION_DEBIT

    @property
    def is_credit(self) -> bool:
        """True when this event puts money in."""
        return self.direction == DIRECTION_CREDIT

    @property
    def has_blank_amount(self) -> bool:
        """True when the amount must be recovered from a linked image."""
        return self.amount is None

    @property
    def is_flexible(self) -> bool:
        """True when this event may be targeted by a spending change at all."""
        return self.flexibility != FLEXIBILITY_FIXED

    @property
    def can_reduce(self) -> bool:
        """True when flexibility permits reduce_to on this event."""
        return self.flexibility in REDUCIBLE_FLEXIBILITIES

    @property
    def can_stop(self) -> bool:
        """True when flexibility permits stop on this event."""
        return self.flexibility in STOPPABLE_FLEXIBILITIES

    @property
    def series_key(self) -> tuple[str, str, str]:
        """Identity of the recurring series this event belongs to.

        (user, category, description) — the grouping that reproduced all four gold
        spending-change targets (DATASET_FACTS A2).
        """
        return (self.user_id, self.category, self.description)


@dataclass(frozen=True)
class Request:
    """One request to answer. Gold answer columns are populated only for samples."""

    request_id: str
    user_id: str
    request_date: date
    request_type: str
    requested_amount: Decimal
    desired_completion_date: date
    allows_partial_payment: bool
    request_text: str
    gold: Optional[dict[str, str]] = None


@dataclass(frozen=True)
class PaymentOption:
    """One seller/provider payment option for a request."""

    payment_option_id: str
    request_id: str
    payment_method: str
    payment_amount: Decimal
    number_of_payments: int
    first_payment_date: date
    payment_frequency_days: Optional[int]
    financing_fee: Decimal
    total_payable_amount: Decimal

    def schedule(self) -> list[tuple[date, Decimal]]:
        """Expand this option into its (date, amount) payments, in order."""
        from datetime import timedelta

        step = self.payment_frequency_days or 0
        return [
            (self.first_payment_date + timedelta(days=step * index), self.payment_amount)
            for index in range(self.number_of_payments)
        ]


@dataclass(frozen=True)
class Message:
    """One message. ``request_id``/``related_event_id`` are often absent."""

    message_id: str
    user_id: str
    request_id: Optional[str]
    related_event_id: Optional[str]
    sent_at: str
    source_type: str
    message_text: str


@dataclass(frozen=True)
class MessageFact:
    """One financial fact the model read from a message. Nothing here is trusted as cash
    until ``engine`` decides how the intent applies."""

    intent: str
    amount: Optional[Decimal]
    currency: Optional[str]
    secondary_amount: Optional[Decimal]
    effective_date: Optional[date]
    percent: Optional[Decimal]


@dataclass(frozen=True)
class MessageReading:
    """The structured reading of one message.

    ``source`` is ``model`` (read by the primary model), ``rule_injection`` (discarded by
    the injection pre-filter before any call) or ``failed`` (every attempt failed; no
    facts, not cached).
    """

    message_id: str
    user_id: str
    language: Optional[str]
    amounts_mentioned: tuple[tuple[Decimal, str], ...]
    is_instruction_attempt: bool
    instruction_phrase: Optional[str]
    facts: tuple[MessageFact, ...]
    source: str
    model: Optional[str] = None
    error: str = ""


@dataclass(frozen=True)
class ImageReading:
    """The structured reading of one receipt/bill image for a blank-amount event.

    ``source`` is ``model`` or ``failed``. Which amount the engine uses is decided by
    ``extraction.images.resolve_amount``, not stored here.
    """

    event_id: str
    image_id: str
    language: Optional[str]
    document_currency: Optional[str]
    amounts_seen: tuple[tuple[str, Decimal], ...]
    chosen_amount: Optional[Decimal]
    chosen_label: Optional[str]
    confidence: Optional[str]
    is_instruction_attempt: bool
    source: str
    model: Optional[str] = None
    error: str = ""


@dataclass(frozen=True)
class ImageRef:
    """One image, linking a blank-amount event to a PNG on disk."""

    image_id: str
    user_id: str
    request_id: Optional[str]
    related_event_id: Optional[str]

    def path(self, images_dir) -> "object":
        """Resolve this image to ``<images_dir>/<image_id>.png``."""
        return images_dir / f"{self.image_id}.png"


@dataclass(frozen=True)
class ExchangeRate:
    """One dated, directional conversion rate."""

    rate_date: date
    from_currency: str
    to_currency: str
    rate: Decimal


@dataclass
class Dataset:
    """Everything loaded, grouped for single-user access."""

    profiles: dict[str, Profile] = field(default_factory=dict)
    events_by_user: dict[str, list[Event]] = field(default_factory=dict)
    requests: list[Request] = field(default_factory=list)
    options_by_request: dict[str, list[PaymentOption]] = field(default_factory=dict)
    messages_by_user: dict[str, list[Message]] = field(default_factory=dict)
    images_by_event: dict[str, ImageRef] = field(default_factory=dict)
    rates: dict[tuple[date, str, str], Decimal] = field(default_factory=dict)

    def request_by_id(self, request_id: str) -> Request:
        """Look up one request, raising KeyError when absent."""
        for request in self.requests:
            if request.request_id == request_id:
                return request
        raise KeyError(f"no such request: {request_id}")

    def events_for(self, user_id: str) -> list[Event]:
        """Every event for one user, or an empty list."""
        return self.events_by_user.get(user_id, [])

    def messages_for(self, user_id: str) -> list[Message]:
        """Every message for one user, or an empty list."""
        return self.messages_by_user.get(user_id, [])
