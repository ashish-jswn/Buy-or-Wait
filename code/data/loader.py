"""Load the nine dataset CSVs into typed records, grouped by user_id.

Pure I/O: takes paths, returns data, computes nothing. Every load ends with the join
validation in :func:`validate_joins`, which asserts the structural facts recorded in
DATASET_FACTS E1 and raises loudly if any of them stops holding — a silently broken
join would corrupt every downstream number.
"""

import csv
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterable, Optional, TypeVar

from common.formatting import parse_amount
from config import (
    EVENTS_PATH,
    EXCHANGE_RATES_PATH,
    IMAGES_INDEX_PATH,
    MESSAGES_PATH,
    PAYMENT_OPTIONS_PATH,
    PROFILES_PATH,
    REQUESTS_PATH,
    SAMPLE_REQUESTS_PATH,
)
from data.records import (
    Dataset,
    Event,
    ExchangeRate,
    ImageRef,
    Message,
    PaymentOption,
    Profile,
    Request,
)

T = TypeVar("T")

LIST_SEPARATOR = "|"
GOLD_COLUMNS = (
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
)


class DatasetError(RuntimeError):
    """Raised when the dataset is missing, malformed, or fails join validation."""


def read_rows(path: Path) -> list[dict[str, str]]:
    """Read a UTF-8 CSV into a list of dicts with every value stripped."""
    if not path.is_file():
        raise DatasetError(f"dataset file not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [
            {(key or ""): (value or "").strip() for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def _optional(value: str, convert: Callable[[str], T]) -> Optional[T]:
    """Convert a value, or return None when it is blank."""
    return convert(value) if value else None


def parse_date(value: str) -> date:
    """Parse a YYYY-MM-DD date, raising DatasetError on anything else."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as error:
        raise DatasetError(f"bad date: {value!r}") from error


def parse_bool(value: str) -> bool:
    """Parse the dataset's lowercase true/false."""
    return value.strip().lower() == "true"


def parse_list(value: str) -> tuple[str, ...]:
    """Split a pipe-separated list column, dropping blanks."""
    return tuple(part.strip() for part in value.split(LIST_SEPARATOR) if part.strip())


def load_profiles(path: Path = PROFILES_PATH) -> dict[str, Profile]:
    """Load financial_profiles.csv, keyed by user_id."""
    profiles: dict[str, Profile] = {}
    for row in read_rows(path):
        profiles[row["user_id"]] = Profile(
            user_id=row["user_id"],
            home_currency=row["home_currency"],
            current_available_balance=parse_amount(row["current_available_balance"]),
            minimum_balance_to_keep=parse_amount(row["minimum_balance_to_keep"]),
            financial_priorities=parse_list(row["financial_priorities"]),
            protected_categories=parse_list(row["expense_categories_to_protect"]),
            categories_willing_to_reduce=parse_list(
                row["expense_categories_user_is_willing_to_reduce"]
            ),
            categories_willing_to_stop=parse_list(
                row["expense_categories_user_is_willing_to_stop"]
            ),
            payment_methods=parse_list(row["payment_methods_user_will_consider"]),
            max_installment_months=_optional(row["max_installment_months"], int),
        )
    return profiles


def load_events(path: Path = EVENTS_PATH) -> dict[str, list[Event]]:
    """Load financial_events.csv, grouped by user_id and sorted by cash date."""
    grouped: dict[str, list[Event]] = {}
    for row in read_rows(path):
        event = Event(
            event_id=row["event_id"],
            user_id=row["user_id"],
            event_type=row["event_type"],
            description=row["description"],
            category=row["category"],
            direction=row["direction"],
            amount=_optional(row["amount"], parse_amount),
            currency=row["currency"],
            event_date=parse_date(row["event_date"]),
            settlement_date=_optional(row["settlement_date"], parse_date),
            status=row["status"],
            linked_event_id=row["linked_event_id"] or None,
            flexibility=row["flexibility"],
            minimum_allowed_amount=_optional(row["minimum_allowed_amount"], parse_amount),
        )
        grouped.setdefault(event.user_id, []).append(event)
    for events in grouped.values():
        events.sort(key=lambda item: (item.cash_date, item.event_id))
    return grouped


def load_requests(path: Path, with_gold: bool = False) -> list[Request]:
    """Load requests.csv or sample_requests.csv, in file order."""
    requests: list[Request] = []
    for row in read_rows(path):
        gold = {column: row.get(column, "") for column in GOLD_COLUMNS} if with_gold else None
        requests.append(
            Request(
                request_id=row["request_id"],
                user_id=row["user_id"],
                request_date=parse_date(row["request_date"]),
                request_type=row["request_type"],
                requested_amount=parse_amount(row["requested_amount"]),
                desired_completion_date=parse_date(row["desired_completion_date"]),
                allows_partial_payment=parse_bool(row["allows_partial_payment"]),
                request_text=row["request_text"],
                gold=gold,
            )
        )
    return requests


def load_payment_options(path: Path = PAYMENT_OPTIONS_PATH) -> dict[str, list[PaymentOption]]:
    """Load request_payment_options.csv, grouped by request_id."""
    grouped: dict[str, list[PaymentOption]] = {}
    for row in read_rows(path):
        option = PaymentOption(
            payment_option_id=row["payment_option_id"],
            request_id=row["request_id"],
            payment_method=row["payment_method"],
            payment_amount=parse_amount(row["payment_amount"]),
            number_of_payments=int(row["number_of_payments"]),
            first_payment_date=parse_date(row["first_payment_date"]),
            payment_frequency_days=_optional(row["payment_frequency_days"], int),
            financing_fee=parse_amount(row["financing_fee"]),
            total_payable_amount=parse_amount(row["total_payable_amount"]),
        )
        grouped.setdefault(option.request_id, []).append(option)
    for options in grouped.values():
        options.sort(key=lambda item: item.payment_option_id)
    return grouped


def load_messages(path: Path = MESSAGES_PATH) -> dict[str, list[Message]]:
    """Load messages.csv, grouped by user_id."""
    grouped: dict[str, list[Message]] = {}
    for row in read_rows(path):
        message = Message(
            message_id=row["message_id"],
            user_id=row["user_id"],
            request_id=row["request_id"] or None,
            related_event_id=row["related_event_id"] or None,
            sent_at=row["sent_at"],
            source_type=row["source_type"],
            message_text=row["message_text"],
        )
        grouped.setdefault(message.user_id, []).append(message)
    return grouped


def load_images(path: Path = IMAGES_INDEX_PATH) -> dict[str, ImageRef]:
    """Load images.csv, keyed by the event whose amount the image supplies."""
    images: dict[str, ImageRef] = {}
    for row in read_rows(path):
        image = ImageRef(
            image_id=row["image_id"],
            user_id=row["user_id"],
            request_id=row["request_id"] or None,
            related_event_id=row["related_event_id"] or None,
        )
        if image.related_event_id:
            images[image.related_event_id] = image
    return images


def load_exchange_rates(path: Path = EXCHANGE_RATES_PATH) -> dict[tuple[date, str, str], Decimal]:
    """Load exchange_rates.csv into an exact-match lookup table.

    Keyed by (rate_date, from_currency, to_currency). No inverse pairs are
    synthesised and no dates are interpolated — see DATASET_FACTS D2.
    """
    table: dict[tuple[date, str, str], Decimal] = {}
    for row in read_rows(path):
        rate = ExchangeRate(
            rate_date=parse_date(row["rate_date"]),
            from_currency=row["from_currency"],
            to_currency=row["to_currency"],
            rate=parse_amount(row["rate"]),
        )
        table[(rate.rate_date, rate.from_currency, rate.to_currency)] = rate.rate
    return table


def validate_joins(dataset: Dataset) -> None:
    """Assert the structural facts in DATASET_FACTS E1, raising on any failure.

    Checked: every request's user has a profile and events; request ids are unique;
    one request per user; every payment option points at a known request; every image
    points at a real event whose amount is actually blank.
    """
    problems: list[str] = []

    request_ids = [request.request_id for request in dataset.requests]
    if len(set(request_ids)) != len(request_ids):
        problems.append("duplicate request_id values in requests")

    user_ids = [request.user_id for request in dataset.requests]
    if len(set(user_ids)) != len(user_ids):
        problems.append("E1 broken: a user asks more than one question")

    for request in dataset.requests:
        if request.user_id not in dataset.profiles:
            problems.append(f"E1 broken: {request.request_id} user has no profile")
        if not dataset.events_for(request.user_id):
            problems.append(f"E1 broken: {request.request_id} user has no events")

    # Only the loaded requests are checked for options. The options file always covers
    # all 275 requests, so loading just the 25 samples leaves 250 unreferenced — that is
    # expected, not a fault. DATASET_FACTS E6: every request carries 2-4 options.
    for request in dataset.requests:
        options = dataset.options_by_request.get(request.request_id, [])
        if not options:
            problems.append(f"E6 broken: {request.request_id} has no payment options")
        elif not 2 <= len(options) <= 4:
            problems.append(
                f"E6 broken: {request.request_id} has {len(options)} payment options, expected 2-4"
            )
        elif sum(1 for option in options if option.payment_method == "full_payment") != 1:
            problems.append(
                f"B6 broken: {request.request_id} does not have exactly one full_payment option"
            )

    events_by_id = {
        event.event_id: event
        for events in dataset.events_by_user.values()
        for event in events
    }
    for event_id in dataset.images_by_event:
        event = events_by_id.get(event_id)
        if event is None:
            problems.append(f"image points at unknown event: {event_id}")
        elif not event.has_blank_amount:
            problems.append(f"image points at event with a non-blank amount: {event_id}")

    if problems:
        raise DatasetError(
            "dataset join validation failed:\n  " + "\n  ".join(problems)
        )


def load_dataset(
    requests_path: Path = REQUESTS_PATH,
    with_gold: bool = False,
    validate: bool = True,
) -> Dataset:
    """Load every dataset file and return it grouped for single-user access.

    `requests_path` selects requests.csv or sample_requests.csv; pass with_gold=True
    for the latter to keep its answer columns. Raises DatasetError when a file is
    missing or the joins in DATASET_FACTS E1 do not hold.
    """
    dataset = Dataset(
        profiles=load_profiles(),
        events_by_user=load_events(),
        requests=load_requests(requests_path, with_gold=with_gold),
        options_by_request=load_payment_options(),
        messages_by_user=load_messages(),
        images_by_event=load_images(),
        rates=load_exchange_rates(),
    )
    if validate:
        validate_joins(dataset)
    return dataset


def load_sample_dataset(validate: bool = True) -> Dataset:
    """Load the dataset with the 25 labelled sample requests and their gold answers."""
    return load_dataset(SAMPLE_REQUESTS_PATH, with_gold=True, validate=validate)
