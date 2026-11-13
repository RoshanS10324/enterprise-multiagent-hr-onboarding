"""Onboarding budget: amount parsing, currency rules, the template's rate card
kept apart from the employee's selected expenses, and the deterministic
calculation. Unit tests - no network, no database."""

from __future__ import annotations

from decimal import Decimal

import pytest

from sub_agent.documents.budget import (
    AmountProvenance,
    BudgetCategory,
    BudgetStatus,
    InvalidCurrency,
    RowText,
    build_budget,
    budget_summary,
    calculate_budget,
    find_amounts,
    normalise_currency,
    parse_reference_rates,
)

RATE_CARD = [
    ("1 YR – INSIDE COUNTRY: AED 4700/-", "t.r8.c1"),
    ("1 YR – OUTSIDE COUNTRY: AED 4200/-", "t.r8.c1"),
    ("2 YRS – INSIDE COUNTRY: AED 5100/-", "t.r8.c1"),
    ("2 YRS – OUTSIDE COUNTRY: AED 4800/- NON SPONSORED ID CARD: AED 1100/-", "t.r8.c1"),
    ("VISIT VISA : AED 380/-", "t.r9.c1"),
    ("DUMMY RETURN TICKET: AED 100/-", "t.r9.c1"),
    ("BASIC: AED 1000/-", "t.r11.c1"),
    ("WN: AED 3500/- + 1000/- FOR MATERNITY RN: AED 4500/- + 1900/- FOR MATERNITY", "t.r11.c1"),
]

COMPLETE_ROWS = {
    "employment.visa": RowText(text="Selected: 2 years - outside country\nVisa fee: AED 4,800", source="r8"),
    "employment.visit_visa_entry_permit": RowText(text="Not applicable", source="r9"),
    "insurance.plan": RowText(text="WN", source="r11"),
    "insurance.employee_insurance": RowText(text="Category: WN\nPremium: AED 3,500", source="r14"),
    "insurance.family_insurance": RowText(text="Coverage: spouse\nPremium: AED 3,500\nMaternity cover: AED 1,000",
                                          source="r15"),
    "benefits.annual_employee_ticket": RowText(text="Sector: COK - DXB\nTravel dates: 2027-01-15\nTicket cost: AED 1,250",
                                               source="r12"),
    "benefits.annual_family_ticket": RowText(text="Ticket cost: AED 2,300", source="r13"),
    "benefits.hotel": RowText(text="Duration: 7 nights\nBudget: AED 450 per night\nLocation: Al Barsha, Dubai",
                              source="r16"),
    "offer.other_promises": RowText(text="Relocation allowance (one-time): AED 2,000\n"
                                         "Transport allowance: AED 500 per month", source="r17"),
}


def _budget(rows=None, *, years=2, where="OUTSIDE_COUNTRY", nights=7, **kw):
    return build_budget(rows if rows is not None else COMPLETE_ROWS, RATE_CARD, visa_duration_years=years,
                        visa_location_category=where, hotel_nights=nights, **kw)


# ------------------------------------------------------------------ amounts and currencies


@pytest.mark.parametrize("text,amount,currency", [
    ("AED 5,100/-", "5100.00", "AED"), ("AED5100", "5100.00", "AED"), ("USD 1,250.50", "1250.50", "USD"),
    ("5,100 AED", "5100.00", "AED"), ("Dhs 450", "450.00", "AED"), ("Selected 5,100 AED", "5100.00", "AED"),
])
def test_amounts_are_read_with_their_currency_and_original_text(text, amount, currency):
    [found] = find_amounts(text)
    assert found.money.amount == Decimal(amount) and found.money.currency == currency
    assert found.money.original_text  # the written form is kept


@pytest.mark.parametrize("text", ["7 days", "2 YRS - INSIDE COUNTRY", "for 7 nights", "spouse and 1 child"])
def test_ordinary_numbers_are_not_amounts(text):
    assert find_amounts(text) == []


def test_an_unknown_currency_code_is_an_error_not_a_guess():
    [found] = find_amounts("XYZ 500")
    assert found.money is None and found.error == "unsupported currency"
    with pytest.raises(InvalidCurrency):
        normalise_currency("XYZ")
    assert normalise_currency(" aed ") == "AED" and normalise_currency("Dhs") == "AED"


# ------------------------------------------------------------------ reference rate card


def test_the_rate_card_is_parsed_into_structured_reference_rates():
    rates = {r.key: r for r in parse_reference_rates(RATE_CARD)}
    assert {k: str(r.money.amount) for k, r in rates.items()} == {
        "visa.1y.inside": "4700.00", "visa.1y.outside": "4200.00", "visa.2y.inside": "5100.00",
        "visa.2y.outside": "4800.00", "visa.non_sponsored_id_card": "1100.00", "visa.visit_visa": "380.00",
        "visa.dummy_return_ticket": "100.00", "insurance.basic": "1000.00", "insurance.wn": "3500.00",
        "insurance.wn.maternity": "1000.00", "insurance.rn": "4500.00", "insurance.rn.maternity": "1900.00"}
    assert rates["insurance.wn.maternity"].note  # currency taken from the clause, and says so


def test_reference_rates_are_never_counted_on_a_blank_form():
    budget = _budget({}, years=None, where=None, nights=None)
    assert budget.status == BudgetStatus.UNAVAILABLE and budget.budget_total is None
    assert len(budget.reference_rates) == 12 and not any(line.counted for line in budget.lines)
    assert "no_counted_amount" in budget.status_reasons


def test_a_selection_without_an_amount_stays_missing_and_shows_the_rate_for_information():
    rows = dict(COMPLETE_ROWS, **{"employment.visa": RowText(text="2 years - outside country", source="r8")})
    line = _budget(rows).line("visa.fee")
    assert line.provenance == AmountProvenance.MISSING and line.money is None and not line.counted
    assert line.reference.key == "visa.2y.outside" and line.reference.money.amount == Decimal("4800")


# ------------------------------------------------------------------ selected expenses


def test_every_category_is_extracted_from_the_employee_specific_rows():
    b = _budget()
    assert b.line("visa.fee").money.amount == Decimal("4800.00")
    assert b.line("visa.visit.fee").provenance == AmountProvenance.NOT_APPLICABLE
    assert b.line("insurance.employee.premium").money.amount == Decimal("3500.00")
    assert b.line("insurance.family.premium").money.amount == Decimal("3500.00")
    assert b.line("insurance.family.maternity").reference.key == "insurance.wn.maternity"
    travel = b.line("travel.employee.ticket")
    assert travel.money.amount == Decimal("1250.00")
    assert travel.details == {"sector": "COK - DXB", "travel_dates": "2027-01-15"}
    assert b.line("travel.family.ticket").money.amount == Decimal("2300.00")
    hotel = b.line("accommodation.hotel.budget")
    assert hotel.provenance == AmountProvenance.CALCULATED and hotel.money.amount == Decimal("3150.00")
    assert hotel.details["location"] == "Al Barsha, Dubai" and hotel.details["nightly_rate"] == "AED 450"
    assert b.line("other.relocation_allowance_one_time").money.amount == Decimal("2000.00")


def test_the_plan_row_is_a_selection_and_is_never_counted_twice():
    rows = dict(COMPLETE_ROWS, **{"insurance.plan": RowText(text="WN - AED 3,500", source="r11")})
    b = _budget(rows)
    plan = b.line("insurance.plan.selection")
    assert not plan.counted and plan.details["plan"] == "WN" and plan.details["stated_rate"] == "AED 3,500"
    assert b.category_total(BudgetCategory.INSURANCE) == Decimal("8000.00")


def test_a_recurring_allowance_is_kept_but_not_counted_as_an_onboarding_expense():
    line = _budget().line("other.transport_allowance")
    assert not line.counted and line.details["stated_amount"] == "AED 500" and "recurring" in line.note


def test_hotel_without_nights_is_uncertain_and_hotel_inside_country_is_inferred_not_applicable():
    b = _budget(nights=None)
    assert b.line("accommodation.hotel.budget").provenance == AmountProvenance.UNCERTAIN
    rows = dict(COMPLETE_ROWS, **{"benefits.hotel": RowText(text=None, source="r16")})
    inside = _budget(rows, where="INSIDE_COUNTRY")
    assert inside.line("accommodation.hotel.budget").provenance == AmountProvenance.INFERRED_NOT_APPLICABLE


@pytest.mark.parametrize("text,provenance", [
    ("Ticket cost: XYZ 1,250", AmountProvenance.INVALID),
    ("Ticket cost: 1,250", AmountProvenance.UNCERTAIN),  # an amount with no currency is never assumed AED
])
def test_unusable_amounts_are_flagged_not_counted(text, provenance):
    rows = dict(COMPLETE_ROWS, **{"benefits.annual_employee_ticket": RowText(text=text, source="r12")})
    b = _budget(rows)
    line = next(x for x in b.lines if x.key.startswith("travel.employee"))
    assert line.provenance == provenance and not line.counted
    assert b.status == BudgetStatus.PARTIAL


def test_two_different_values_in_one_row_are_uncertain():
    rows = dict(COMPLETE_ROWS, **{"benefits.annual_family_ticket": RowText(conflict=True, source="r13")})
    b = _budget(rows)
    assert b.line("travel.family.ticket").provenance == AmountProvenance.UNCERTAIN
    assert "uncertain:travel.family.ticket" in b.status_reasons


# ------------------------------------------------------------------ calculation


def test_the_complete_budget_is_totalled_per_category_and_overall():
    b = _budget()
    assert b.status == BudgetStatus.COMPLETE and b.status_reasons == []
    assert b.currency == "AED" and b.budget_total == Decimal("21500.00")
    assert {c.value: str(b.category_total(c)) for c in BudgetCategory} == {
        "VISA": "4800.00", "INSURANCE": "8000.00", "TRAVEL": "3550.00", "ACCOMMODATION": "3150.00",
        "OTHER": "2000.00"}
    assert b.reference_differences == []  # every selection equals its rate


def test_a_selected_amount_that_differs_from_the_rate_card_is_reported_and_the_selection_wins():
    rows = dict(COMPLETE_ROWS, **{"employment.visa": RowText(text="2 years outside\nVisa fee: AED 4,700", source="r8")})
    b = _budget(rows)
    assert b.line("visa.fee").money.amount == Decimal("4700.00")
    assert b.reference_differences == ["visa.fee:selected_differs_from_visa.2y.outside"]
    assert b.budget_total == Decimal("21400.00")


def test_missing_rows_make_the_budget_partial_and_are_named():
    rows = {k: v for k, v in COMPLETE_ROWS.items() if k != "benefits.annual_family_ticket"}
    b = _budget(rows)
    assert b.status == BudgetStatus.PARTIAL and "missing:travel.family.ticket" in b.status_reasons
    assert b.budget_total == Decimal("19200.00")  # what IS stated is still totalled


def test_amounts_in_two_currencies_are_never_added_without_a_rate():
    rows = dict(COMPLETE_ROWS, **{"benefits.annual_employee_ticket": RowText(text="Ticket cost: USD 340", source="r12")})
    b = _budget(rows)
    assert b.budget_total is None and b.currency is None
    assert b.totals_by_currency == {"AED": Decimal("20250.00"), "USD": Decimal("340.00")}
    assert "mixed_currencies_without_conversion_rate" in b.status_reasons and b.status == BudgetStatus.PARTIAL
    assert b.category_total(BudgetCategory.TRAVEL) is None


def test_an_explicit_supplied_rate_converts_and_is_recorded():
    rows = dict(COMPLETE_ROWS, **{"benefits.annual_employee_ticket": RowText(text="Ticket cost: USD 340", source="r12")})
    b = _budget(rows, conversion_rates={"USD": Decimal("3.6725")}, base_currency="AED")
    assert b.currency == "AED" and b.budget_total == Decimal("21498.65")  # 20250 + 340 x 3.6725
    assert b.conversion.rates == {"USD": Decimal("3.6725")} and b.status == BudgetStatus.COMPLETE


def test_a_rate_for_only_some_currencies_does_not_convert():
    rows = dict(COMPLETE_ROWS, **{"benefits.annual_employee_ticket": RowText(text="Ticket cost: USD 340", source="r12"),
                                  "benefits.annual_family_ticket": RowText(text="Ticket cost: EUR 300", source="r13")})
    b = _budget(rows, conversion_rates={"USD": Decimal("3.6725")}, base_currency="AED")
    assert b.budget_total is None and b.conversion is None


def test_recalculating_is_deterministic():
    b = _budget()
    assert calculate_budget(b) == calculate_budget(b) == b
    assert budget_summary(b) == budget_summary(_budget())


def test_the_summary_is_json_safe_and_carries_no_floats():
    import json

    text = json.dumps(budget_summary(_budget()))
    assert '"budget_total": "21500.00"' in text and '"status": "COMPLETE"' in text
