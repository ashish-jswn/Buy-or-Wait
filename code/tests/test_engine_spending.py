"""Tests for engine.spending on synthetic states and events. No API key, no data files."""

from datetime import date
from decimal import Decimal

from data.records import Event, Profile, Request
from engine.series import RecurringSeries
from engine.spending import (
    eligible_changes,
    find_spending_changes,
    ordered_actions,
    spending_change_candidate,
)
from engine.state import CashState

AS_OF = date(2025, 1, 10)
USER = "user_s"


def event(event_id, on, category="streaming", description="Streaming plan", amount="50", flexibility="stoppable", minimum=None) -> Event:
    return Event(event_id, USER, "subscription", description, category, "debit", Decimal(amount), "EUR", on, on, "settled", None, flexibility, None if minimum is None else Decimal(minimum))


def monthly(events, day=15, amount="50", category="streaming", description="Streaming plan") -> RecurringSeries:
    last = max(item.cash_date for item in events)
    return RecurringSeries((USER, category, description), category, "debit", Decimal(amount), last, 30, day, len(events), events[-1].event_id)


def profile(stop=("streaming",), reduce=(), protect=()) -> Profile:
    return Profile(USER, "EUR", Decimal("1000"), Decimal("100"), (), tuple(protect), tuple(reduce), tuple(stop), ("full_payment",), None)


def state(*series) -> CashState:
    return CashState(USER, AS_OF, Decimal("1000"), Decimal("100"), recurring=list(series))


def request(amount) -> Request:
    return Request("request_s", USER, AS_OF, "purchase", Decimal(str(amount)), date(2025, 2, 1), False, "")


STREAMING = [event("event_1", date(2024, 10, 15)), event("event_2", date(2024, 11, 15)), event("event_3", date(2024, 12, 15))]


def test_a_single_stop_that_makes_full_payment_safe_is_found() -> None:
    """880 today leaves 120; the 50 streaming charge on the 15th would breach 100."""
    st, prof = state(monthly(STREAMING)), profile()
    combo = find_spending_changes(st, eligible_changes(st, STREAMING, prof, {}), request(880))
    assert ordered_actions(combo) == ("stop:event_3",)


def test_the_change_names_the_latest_event_before_the_request() -> None:
    (change,) = eligible_changes(state(monthly(STREAMING)), STREAMING, profile(), {})
    assert change.event_id == "event_3"


def test_protected_category_is_never_changed() -> None:
    assert eligible_changes(state(monthly(STREAMING)), STREAMING, profile(protect=("streaming",)), {}) == []


def test_category_outside_the_users_stop_list_is_not_stopped() -> None:
    assert eligible_changes(state(monthly(STREAMING)), STREAMING, profile(stop=()), {}) == []


def test_fixed_event_is_never_changed() -> None:
    fixed = [event(f"event_{i}", date(2024, m, 15), flexibility="fixed") for i, m in ((1, 10), (2, 11), (3, 12))]
    assert eligible_changes(state(monthly(fixed)), fixed, profile(), {}) == []


def test_both_verbs_permitted_reduces_to_the_minimum_allowed_amount_verbatim() -> None:
    rows = [event(f"event_{i}", date(2024, m, 15), flexibility="reducible_or_stoppable", minimum="20.5") for i, m in ((1, 10), (2, 11), (3, 12))]
    (change,) = eligible_changes(state(monthly(rows)), rows, profile(stop=("streaming",), reduce=("streaming",)), {})
    assert change.action == "reduce_to:event_3:20.50"
    assert change.savings[0].amount == Decimal("29.5")


def test_smallest_set_first_and_least_money_cut_within_a_size() -> None:
    gym = [event(f"event_{i}", date(2024, m, 15), category="gym", description="Gym", amount="200") for i, m in ((11, 10), (12, 11), (13, 12))]
    st = state(monthly(STREAMING), monthly(gym, amount="200", category="gym", description="Gym"))
    prof = profile(stop=("streaming", "gym"))
    # Over the 90-day window streaming (50) and gym (200) each recur 3 times: 750 out.
    # Paying 250 today leaves 750 -> unsafe unaided (0 < 100). Stopping streaming leaves
    # 750 - 600 = 150 and stopping gym leaves 750 - 150 = 600: both work alone, and
    # streaming cuts less money (150 vs 600), so it wins.
    combo = find_spending_changes(st, eligible_changes(st, STREAMING + gym, prof, {}), request(250))
    assert ordered_actions(combo) == ("stop:event_3",)


def test_an_expense_not_projected_as_recurring_uses_its_own_average_gap() -> None:
    """request_11's shape: a 2-row reducible dining series, not in the recurring list."""
    dining = [
        event("event_21", date(2024, 12, 27), category="dining", description="Weekend dining", amount="80", flexibility="reducible", minimum="30"),
        event("event_22", date(2025, 1, 3), category="dining", description="Weekend dining", amount="80", flexibility="reducible", minimum="30"),
    ]
    (change,) = eligible_changes(state(), dining, profile(stop=(), reduce=("dining",)), {})
    assert change.action == "reduce_to:event_22:30"
    assert change.savings[0].on == date(2025, 1, 10)
    assert all(flow.amount == Decimal("50") for flow in change.savings)


def test_no_working_set_returns_none() -> None:
    st = state(monthly(STREAMING))
    assert find_spending_changes(st, eligible_changes(st, STREAMING, profile(), {}), request(990)) is None


def test_output_order_is_stops_then_reductions_by_event_id() -> None:
    """Matches gold request_21: stop:event_1815|reduce_to:event_1816:23.50."""
    stop_rows = [event("event_1815", date(2024, 12, 12), category="cloud_storage", description="Cloud")]
    reduce_rows = [event("event_1816", date(2024, 12, 9), flexibility="reducible_or_stoppable", minimum="23.5")]
    st = state(monthly(stop_rows, day=12, category="cloud_storage", description="Cloud"), monthly(reduce_rows, day=9))
    changes = eligible_changes(st, stop_rows + reduce_rows, profile(stop=("cloud_storage", "streaming"), reduce=("streaming",)), {})
    assert ordered_actions(changes) == ("stop:event_1815", "reduce_to:event_1816:23.50")
    candidate = spending_change_candidate(request(10), changes)
    assert candidate.method == "full_payment" and candidate.spending_changes == ordered_actions(changes)
