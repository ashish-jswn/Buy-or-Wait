"""Tests for engine.evidence: every message intent that moves cash, on synthetic states.
No API key, no model."""

from datetime import date
from decimal import Decimal

from data.records import MessageFact, MessageReading, Profile
from engine.evidence import apply_message_facts
from engine.series import RecurringSeries
from engine.state import CashState

AS_OF = date(2025, 5, 3)
PROFILE = Profile(
    user_id="user_t",
    home_currency="IDR",
    current_available_balance=Decimal("1000"),
    minimum_balance_to_keep=Decimal("100"),
    financial_priorities=(),
    protected_categories=(),
    categories_willing_to_reduce=(),
    categories_willing_to_stop=(),
    payment_methods=("full_payment",),
    max_installment_months=None,
)
RATES = {(date(2025, 5, 15), "USD", "IDR"): Decimal("15000")}


def salary(name="Payroll credit", amount="1000", day=15, last=date(2025, 4, 15), occurrences=5) -> RecurringSeries:
    return RecurringSeries((PROFILE.user_id, "salary", name), "salary", "credit", Decimal(amount), last, 30, day, occurrences, f"ev_{name}")


def pool() -> RecurringSeries:
    return RecurringSeries((PROFILE.user_id, "salary", "irregular income pool"), "salary", "credit", Decimal("300"), date(2025, 4, 28), 14, None, 8, "ev_pool")


def rent() -> RecurringSeries:
    return RecurringSeries((PROFILE.user_id, "rent", "Monthly rent"), "rent", "debit", Decimal("500"), date(2025, 5, 2), 30, 2, 6, "ev_rent")


def state(*series) -> CashState:
    return CashState(PROFILE.user_id, AS_OF, Decimal("1000"), Decimal("100"), recurring=list(series))


def reading(*facts, instruction=False) -> MessageReading:
    return MessageReading("message_t", PROFILE.user_id, "en", (), instruction, None, tuple(facts), "model")


def fact(intent, amount=None, currency="IDR", secondary=None, on=None, percent=None) -> MessageFact:
    to_decimal = lambda value: None if value is None else Decimal(str(value))
    return MessageFact(intent, to_decimal(amount), currency, to_decimal(secondary), on, to_decimal(percent))


def income_flows(result):
    return [(flow.on, flow.amount) for flow in result.state.known_flows if flow.amount > 0]


def income_series(result):
    return [item for item in result.state.recurring if item.direction == "credit"]


def test_salary_increase_applies_from_its_date_only() -> None:
    result = apply_message_facts(state(salary()), [reading(fact("salary_increase", 1200, on=date(2025, 6, 15)))], PROFILE, RATES)
    assert income_flows(result) == [(date(2025, 5, 15), Decimal("1000")), (date(2025, 6, 15), Decimal("1200")), (date(2025, 7, 15), Decimal("1200"))]
    assert income_series(result) == []


def test_first_salary_with_no_stream_starts_a_monthly_salary_on_its_date() -> None:
    result = apply_message_facts(state(), [reading(fact("first_salary", 900, on=date(2025, 5, 20)))], PROFILE, RATES)
    (series,) = income_series(result)
    assert series.day_of_month == 20 and series.amount == Decimal("900")


def test_fx_salary_is_converted_at_its_date_and_reanchors_payday() -> None:
    result = apply_message_facts(state(salary(day=14)), [reading(fact("fx_salary_confirmed", 2, currency="USD", on=date(2025, 5, 15)))], PROFILE, RATES)
    assert income_flows(result)[0] == (date(2025, 5, 15), Decimal("30000"))


def test_fx_salary_with_no_rate_is_skipped_not_raised() -> None:
    result = apply_message_facts(state(salary()), [reading(fact("fx_salary_confirmed", 2, currency="USD", on=date(2025, 6, 15)))], PROFILE, RATES)
    assert result.applied == () and income_series(result)


def test_payday_moved_reanchors_the_day_of_month() -> None:
    result = apply_message_facts(state(salary()), [reading(fact("payday_moved", on=date(2025, 5, 23)))], PROFILE, RATES)
    assert income_series(result)[0].day_of_month == 23


def test_next_salary_reduced_sets_the_level_from_the_next_payroll() -> None:
    result = apply_message_facts(state(salary(amount="782.57")), [reading(fact("next_salary_reduced", "1422.85"))], PROFILE, RATES)
    assert {amount for _, amount in income_flows(result)} == {Decimal("1422.85")}


def test_temporary_pay_holds_from_the_next_payroll() -> None:
    result = apply_message_facts(state(salary(amount="1441")), [reading(fact("temporary_pay", "1037.52"))], PROFILE, RATES)
    assert {amount for _, amount in income_flows(result)} == {Decimal("1037.52")}


def test_arrears_add_to_the_next_salary_only() -> None:
    result = apply_message_facts(state(salary()), [reading(fact("salary_next_with_arrears", 1000, secondary=450))], PROFILE, RATES)
    assert [amount for _, amount in income_flows(result)] == [Decimal("1450"), Decimal("1000"), Decimal("1000")]


def test_arrears_message_without_an_amount_changes_nothing() -> None:
    """message_02: no amount stated, so no cash effect."""
    result = apply_message_facts(state(salary()), [reading(fact("salary_next_with_arrears"))], PROFILE, RATES)
    assert result.applied == () and income_series(result) == [salary()]


def test_remaining_household_salary_replaces_the_level_and_ends_other_streams() -> None:
    primary, second = salary("Primary household salary", occurrences=5), salary("Second household income", day=20, occurrences=4)
    result = apply_message_facts(state(primary, second), [reading(fact("remaining_household_salary", 1628))], PROFILE, RATES)
    assert income_series(result) == []
    assert {amount for _, amount in income_flows(result)} == {Decimal("1628")}
    assert {flow.on.day for flow in result.state.known_flows} == {15}


def test_base_salary_sets_the_level_and_stops_commission() -> None:
    result = apply_message_facts(state(salary("Base salary"), pool()), [reading(fact("base_salary_commission_pending", 3872))], PROFILE, RATES)
    assert income_series(result) == []
    assert {amount for _, amount in income_flows(result)} == {Decimal("3872")}


def test_payout_pending_stops_irregular_income_only() -> None:
    result = apply_message_facts(state(salary(), pool()), [reading(fact("payout_pending"))], PROFILE, RATES)
    assert [item.key[2] for item in income_series(result)] == ["Payroll credit"]


def test_invoice_approved_counts_only_the_invoice() -> None:
    result = apply_message_facts(state(pool()), [reading(fact("invoice_approved", 5000, on=date(2025, 5, 15)))], PROFILE, RATES)
    assert income_series(result) == []
    assert income_flows(result) == [(date(2025, 5, 15), Decimal("5000"))]


def test_employment_ended_stops_all_income() -> None:
    result = apply_message_facts(state(salary(), pool(), rent()), [reading(fact("employment_ended"))], PROFILE, RATES)
    assert income_series(result) == [] and [item.category for item in result.state.recurring] == ["rent"]


def test_rent_increase_applies_to_rent_only() -> None:
    result = apply_message_facts(state(salary(), rent()), [reading(fact("rent_increase_pct", percent=12))], PROFILE, RATES)
    rent_series = next(item for item in result.state.recurring if item.category == "rent")
    assert rent_series.amount == Decimal("560.00")
    assert income_series(result) == [salary()]


def test_no_cash_effect_intents_change_nothing() -> None:
    original = state(salary(), pool(), rent())
    facts = [fact(name) for name in ("refund_pending", "bonus_pending", "dispute_open", "internal_transfer", "investment_value_unrealized")]
    result = apply_message_facts(original, [reading(*facts)], PROFILE, RATES)
    assert result.state.recurring == sorted(original.recurring, key=lambda item: item.key)
    assert result.applied == ()
    assert len(result.skipped) == 5


def test_instruction_attempt_contributes_nothing() -> None:
    result = apply_message_facts(state(salary()), [reading(fact("salary_increase", 99999, on=date(2025, 5, 15)), instruction=True)], PROFILE, RATES)
    assert income_series(result) == [salary()] and result.applied == ()


def test_original_state_is_not_mutated() -> None:
    original = state(salary(), rent())
    apply_message_facts(original, [reading(fact("rent_increase_pct", percent=12), fact("payday_moved", on=date(2025, 5, 23)))], PROFILE, RATES)
    assert original.recurring == [salary(), rent()] and original.known_flows == []
