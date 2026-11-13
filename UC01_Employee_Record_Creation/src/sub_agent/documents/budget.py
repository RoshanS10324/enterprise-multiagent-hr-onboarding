"""Onboarding budget: the employee's SELECTED expenses, kept apart from the
template's printed RATE CARD, and a deterministic total.

Three things, never mixed:

A. Selected expenses (`BudgetLine`). Amounts HR wrote in the value column
   for THIS employee: the visa fee, insurance premiums, tickets, the hotel,
   other agreed expenses. Only these are ever counted.
B. Reference rates (`ReferenceRate`). The price list the template itself
   prints ("1 YR - INSIDE COUNTRY: AED 4700/-", "BASIC: AED 1000/-", ...).
   Company information. A rate is NEVER counted, even when HR selected that
   category: a line whose amount was not written stays MISSING and the
   matching rate is shown beside it for information only.
C. The calculation (`calculate_budget`). Per category and per currency.
   Amounts in different currencies are never added together unless the
   caller supplies an explicit conversion rate for every foreign currency;
   no rate is ever looked up or invented.

Provenance of every line:

    EXTRACTED                an amount HR wrote for this employee
    CALCULATED               derived deterministically from extracted
                             values only (nightly rate x number of nights)
    NOT_APPLICABLE           HR wrote "Not applicable", "N/A", "None", "Nil"
    INFERRED_NOT_APPLICABLE  the template's own rule makes the row not apply
                             (hotel stay is "only applicable for staff
                             outside UAE"; visa processed inside the country)
    MISSING                  nothing written, or text but no amount
    INVALID                  an amount that cannot be used (unknown currency,
                             malformed number)
    UNCERTAIN                two different values, an amount without a
                             currency, a nightly rate without a number of nights

Status of the budget:

    COMPLETE     every required row is resolved (an amount, or not
                 applicable) and the total is in one currency
    PARTIAL      at least one counted amount, but something is missing,
                 unreadable, uncertain, or in a second currency with no rate
    UNAVAILABLE  no counted amount at all

The salary is never part of the budget; that row is not read here.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Version of these rules, persisted with every budget so a stored total can
#: always be traced to the logic that produced it.
CALCULATION_VERSION = "uc01-budget-v1"

#: ISO 4217 codes UC-01 accepts. A three-letter code outside this set is
#: INVALID (never guessed as a typo of another currency).
CURRENCIES = frozenset({
    "AED", "SAR", "QAR", "OMR", "KWD", "BHD", "JOD", "EGP", "USD", "EUR", "GBP", "CHF", "CAD", "AUD", "NZD",
    "SGD", "HKD", "JPY", "CNY", "INR", "PKR", "LKR", "NPR", "BDT", "PHP", "IDR", "MYR", "THB", "ZAR", "KES",
    "NGN", "TRY",
})
#: Written forms of the dirham that unambiguously mean AED.
_CURRENCY_ALIASES = {"DHS": "AED", "DH": "AED", "DIRHAM": "AED", "DIRHAMS": "AED"}
_CENT = Decimal("0.01")


class BudgetCategory(StrEnum):
    VISA = "VISA"
    INSURANCE = "INSURANCE"
    TRAVEL = "TRAVEL"
    ACCOMMODATION = "ACCOMMODATION"
    OTHER = "OTHER"


class AmountProvenance(StrEnum):
    EXTRACTED = "EXTRACTED"
    CALCULATED = "CALCULATED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    INFERRED_NOT_APPLICABLE = "INFERRED_NOT_APPLICABLE"
    MISSING = "MISSING"
    INVALID = "INVALID"
    UNCERTAIN = "UNCERTAIN"


#: Provenances whose amount is counted in the totals.
COUNTED = frozenset({AmountProvenance.EXTRACTED, AmountProvenance.CALCULATED})
#: Provenances that resolve a row without an amount.
RESOLVED_WITHOUT_AMOUNT = frozenset({AmountProvenance.NOT_APPLICABLE, AmountProvenance.INFERRED_NOT_APPLICABLE})


class BudgetStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class InvalidCurrency(ValueError):
    pass


def normalise_currency(code: str) -> str:
    """Upper-case ISO 4217 code, or InvalidCurrency. Aliases of the dirham
    map to AED; nothing else is mapped."""
    text = (code or "").strip().upper().rstrip(".")
    text = _CURRENCY_ALIASES.get(text, text)
    if text not in CURRENCIES:
        raise InvalidCurrency(f"unsupported currency code {text!r}")
    return text


class Money(BaseModel):
    model_config = ConfigDict(frozen=True)

    amount: Decimal = Field(ge=0)
    currency: str
    # The exact text the amount was read from, e.g. "AED 5,100/-". Kept so the
    # original value and currency can always be shown next to the number.
    original_text: Optional[str] = None

    @field_validator("currency")
    @classmethod
    def _iso(cls, v: str) -> str:
        return normalise_currency(v)

    @field_validator("amount")
    @classmethod
    def _cents(cls, v: Decimal) -> Decimal:
        return v.quantize(_CENT, rounding=ROUND_HALF_UP)


class ReferenceRate(BaseModel):
    """One price the TEMPLATE prints. Information only; never counted."""

    model_config = ConfigDict(frozen=True)

    key: str  # e.g. "visa.2y.inside", "insurance.wn.maternity"
    category: BudgetCategory
    label: str
    money: Money
    source: str
    note: Optional[str] = None


class BudgetLine(BaseModel):
    """One employee-specific expense (or the reason there is none)."""

    key: str  # e.g. "visa.fee", "insurance.employee.premium", "travel.employee.ticket"
    category: BudgetCategory
    label: str
    provenance: AmountProvenance
    money: Optional[Money] = None
    source: Optional[str] = None
    note: Optional[str] = None
    # The rate-card price for what HR selected - shown, never counted.
    reference: Optional[ReferenceRate] = None
    # Non-amount facts stated on the same row (sector, travel dates,
    # location, nights, nightly rate, plan). Never personal data.
    details: dict[str, str] = Field(default_factory=dict)

    @property
    def counted(self) -> bool:
        return self.provenance in COUNTED and self.money is not None


class ConversionApplied(BaseModel):
    base_currency: str
    # 1 unit of the key currency = rate units of base_currency, as SUPPLIED.
    rates: dict[str, Decimal]


class OnboardingBudget(BaseModel):
    lines: list[BudgetLine] = Field(default_factory=list)
    reference_rates: list[ReferenceRate] = Field(default_factory=list)
    # category -> currency -> total of counted lines
    category_totals: dict[str, dict[str, Decimal]] = Field(default_factory=dict)
    totals_by_currency: dict[str, Decimal] = Field(default_factory=dict)
    # Set only when every counted amount is in one currency (or was
    # converted with a supplied rate).
    currency: Optional[str] = None
    budget_total: Optional[Decimal] = None
    conversion: Optional[ConversionApplied] = None
    status: BudgetStatus = BudgetStatus.UNAVAILABLE
    status_reasons: list[str] = Field(default_factory=list)
    # Selected amount differs from the rate-card price for the same
    # selection. Information for HR; the selected amount is still the one used.
    reference_differences: list[str] = Field(default_factory=list)
    calculation_version: str = CALCULATION_VERSION

    def category_total(self, category: BudgetCategory) -> Optional[Decimal]:
        """The category total in the budget currency, or None when there is
        no single-currency total for it."""
        totals = self.category_totals.get(category.value, {})
        if not totals:
            return Decimal("0.00") if self.currency else None
        if self.conversion is not None:
            return _convert(totals, self.conversion)
        if set(totals) == {self.currency}:
            return totals[self.currency]  # type: ignore[index]
        return None

    def line(self, key: str) -> Optional[BudgetLine]:
        return next((x for x in self.lines if x.key == key), None)


# ------------------------------------------------------------------ parsing amounts

_NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?"
# "AED 5,100/-", "AED5100", "USD 1,250.50", "Dhs 450" (currency first) and
# "5,100 AED" (currency after). Two separate passes, so a word that is not a
# currency ("Selected 5,100 AED") cannot swallow the number the trailing
# currency belongs to.
_MONEY_PREFIX = re.compile(rf"(?P<cur>\b[A-Za-z]{{2,7}}\.?)\s*(?P<num>{_NUMBER})(?:\s*/-)?(?![\d,])")
_MONEY_SUFFIX = re.compile(rf"(?<![\w.,])(?P<num>{_NUMBER})(?:\s*/-)?\s*(?P<cur>[A-Za-z]{{2,7}})\b")
_NOT_APPLICABLE = re.compile(r"^\s*(n/?a|not applicable|none|nil|not required|no|-)\s*\.?\s*$", re.I)
_PER_NIGHT = re.compile(r"^\s*(?:/|per\s*)(night|day)\b", re.I)
_RECURRING = re.compile(r"\b(per|a|every|each)\s+(month|year|annum)\b|\bmonthly\b|\bannually\b|\bp\.?\s?m\.?\b", re.I)
_AMOUNT_WORDS = re.compile(r"\b(cost|fee|fees|premium|budget|amount|price|fare|charge|charges|rate)\b", re.I)


class _Amount(BaseModel):
    money: Optional[Money] = None
    error: Optional[str] = None  # why it is not usable
    span: tuple[int, int] = (0, 0)


def _known_currency(token: str) -> bool:
    t = token.upper().rstrip(".")
    return t in CURRENCIES or t in _CURRENCY_ALIASES


def find_amounts(text: str) -> list[_Amount]:
    """Every amount with a currency in `text`, in reading order.

    A supported code (or a dirham alias) before or after the number is an
    amount. An UPPER-CASE three-letter code written BEFORE a number that is
    not a supported currency ("XYZ 500") is an error entry - never guessed as
    a typo of another currency. Ordinary words next to numbers ("for 7",
    "7 days", "2 YRS") are not amounts."""
    found: list[_Amount] = []
    taken: list[tuple[int, int]] = []

    def overlaps(span: tuple[int, int]) -> bool:
        return any(span[0] < e and s < span[1] for s, e in taken)

    for pattern, prefix in ((_MONEY_PREFIX, True), (_MONEY_SUFFIX, False)):
        for m in pattern.finditer(text):
            cur, num, span = m.group("cur"), m.group("num"), m.span()
            unknown_code = prefix and cur.isupper() and len(cur) == 3
            if not (_known_currency(cur) or unknown_code) or overlaps(span):
                continue
            taken.append(span)
            raw = m.group(0).strip()
            try:
                code = normalise_currency(cur)  # before Money(): pydantic would wrap the error
                found.append(_Amount(money=Money(amount=Decimal(num.replace(",", "")), currency=code,
                                                 original_text=raw), span=span))
            except InvalidCurrency:
                found.append(_Amount(error="unsupported currency", span=span))
            except (InvalidOperation, ValueError):
                found.append(_Amount(error="unreadable amount", span=span))
    return sorted(found, key=lambda a: a.span)


def _has_bare_amount(text: str) -> bool:
    """A number that looks like money (with an amount word) but no currency."""
    return bool(_AMOUNT_WORDS.search(text)) and bool(re.search(r"\d", text)) and not find_amounts(text)


def is_not_applicable(text: str) -> bool:
    return bool(_NOT_APPLICABLE.match(text or ""))


# ------------------------------------------------------------------ the rate card

_RATE_CLAUSE = re.compile(
    rf"(?P<label>[A-Za-z0-9][A-Za-z0-9 .\-–]*?)\s*:\s*(?P<cur>AED|[A-Z]{{3}})\s*(?P<num>{_NUMBER})\s*/-"
    rf"(?:\s*\+\s*(?P<extra>{_NUMBER})\s*/-\s*FOR\s+MATERNITY)?",
    re.I,
)


def _norm(text: str) -> str:
    text = text.replace("–", "-").replace("—", "-").lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _rate_key(label: str) -> tuple[str, BudgetCategory]:
    n = _norm(label)
    years = re.search(r"\b([12])\s*(yr|yrs|year|years)\b", n)
    if years:
        where = "inside" if "inside" in n else "outside" if "outside" in n else "unspecified"
        return f"visa.{years.group(1)}y.{where}", BudgetCategory.VISA
    if "non sponsored" in n:
        return "visa.non_sponsored_id_card", BudgetCategory.VISA
    if "visit visa" in n:
        return "visa.visit_visa", BudgetCategory.VISA
    if "return ticket" in n:
        return "visa.dummy_return_ticket", BudgetCategory.VISA
    if n in ("basic", "wn", "rn"):
        return f"insurance.{n}", BudgetCategory.INSURANCE
    return "other." + n.replace(" ", "_"), BudgetCategory.OTHER


def parse_reference_rates(lines: list[tuple[str, str]]) -> list[ReferenceRate]:
    """(template_text, source) -> structured rates. One printed line can hold
    several clauses ("2 YRS - OUTSIDE COUNTRY: AED 4800/- NON SPONSORED ID
    CARD: AED 1100/-"). A maternity supplement printed as "+ 1000/- FOR
    MATERNITY" carries no currency of its own; it takes the currency printed
    in the same clause, and the rate says so."""
    rates: dict[str, ReferenceRate] = {}
    for text, source in lines:
        for m in _RATE_CLAUSE.finditer(text):
            label = m.group("label").strip(" -")
            key, category = _rate_key(label)
            try:
                money = Money(amount=Decimal(m.group("num").replace(",", "")), currency=m.group("cur"),
                              original_text=m.group(0).split("+")[0].strip())
            except (InvalidCurrency, InvalidOperation, ValueError):
                continue
            rates.setdefault(key, ReferenceRate(key=key, category=category, label=label, money=money, source=source))
            if m.group("extra"):
                extra = Money(amount=Decimal(m.group("extra").replace(",", "")), currency=money.currency,
                              original_text="+ " + m.group("extra") + "/- FOR MATERNITY")
                rates.setdefault(key + ".maternity", ReferenceRate(
                    key=key + ".maternity", category=category, label=label + " maternity", money=extra,
                    source=source, note="currency printed once for the clause"))
    return list(rates.values())


# ------------------------------------------------------------------ selected lines

class RowText(BaseModel):
    """What HR wrote in one budget row, as the extractor found it."""

    model_config = ConfigDict(frozen=True)

    text: Optional[str] = None
    source: Optional[str] = None
    conflict: bool = False  # two different values in two cells


#: The template rows that carry budget information: (row key, category,
#: line-key prefix, label, required for COMPLETE).
BUDGET_ROWS: tuple[tuple[str, BudgetCategory, str, str, bool], ...] = (
    ("employment.visa", BudgetCategory.VISA, "visa", "Visa (1 or 2 years)", True),
    ("employment.visit_visa_entry_permit", BudgetCategory.VISA, "visa.visit", "Visit visa + entry permit", False),
    ("insurance.plan", BudgetCategory.INSURANCE, "insurance.plan", "Insurance plan", False),
    ("insurance.employee_insurance", BudgetCategory.INSURANCE, "insurance.employee", "Insurance for employee", True),
    ("insurance.family_insurance", BudgetCategory.INSURANCE, "insurance.family", "Insurance for family", True),
    ("benefits.annual_employee_ticket", BudgetCategory.TRAVEL, "travel.employee", "Annual ticket - employee", True),
    ("benefits.annual_family_ticket", BudgetCategory.TRAVEL, "travel.family", "Annual ticket - family", True),
    ("benefits.hotel", BudgetCategory.ACCOMMODATION, "accommodation.hotel", "Hotel stay", True),
    ("offer.other_promises", BudgetCategory.OTHER, "other", "Other promises", False),
)
_DETAIL_KEYS = {
    "sector": "sector", "sectors": "sector", "route": "sector", "travel sector": "sector",
    "travel date": "travel_dates", "travel dates": "travel_dates", "dates": "travel_dates", "date": "travel_dates",
    "class": "class", "location": "location", "area": "location", "accommodation": "location",
    "nights": "nights", "days": "nights", "plan": "plan", "category": "plan", "coverage": "coverage",
    "dependants": "dependants", "dependents": "dependants", "notes": "note", "note": "note",
}
_DEFAULT_ITEM = {
    "visa": "fee", "visa.visit": "fee", "insurance.employee": "premium", "insurance.family": "premium",
    "travel.employee": "ticket", "travel.family": "ticket", "accommodation.hotel": "budget", "other": "expense",
}


def _slug(text: str) -> str:
    s = _norm(text).replace(" ", "_")
    return s[:40].strip("_") or "item"


def _split_label(line: str, amount_span: tuple[int, int]) -> str:
    """The words describing an amount: the text before it, without a trailing ':'."""
    head = line[: amount_span[0]].strip(" :-–")
    return head


def _row_lines(prefix: str, category: BudgetCategory, label: str, row: RowText,
               hotel_nights: Optional[int]) -> list[BudgetLine]:
    """Every BudgetLine HR's text in one row produces."""
    base: dict[str, Any] = dict(category=category, source=row.source)
    if row.conflict:
        return [BudgetLine(key=f"{prefix}.{_DEFAULT_ITEM.get(prefix, 'expense')}", label=label,
                           provenance=AmountProvenance.UNCERTAIN, note="different values in one row", **base)]
    if not row.text or not row.text.strip():
        return [BudgetLine(key=f"{prefix}.{_DEFAULT_ITEM.get(prefix, 'expense')}", label=label,
                           provenance=AmountProvenance.MISSING, note="nothing written", **base)]
    if is_not_applicable(row.text):
        return [BudgetLine(key=f"{prefix}.{_DEFAULT_ITEM.get(prefix, 'expense')}", label=label,
                           provenance=AmountProvenance.NOT_APPLICABLE, **base)]

    details: dict[str, str] = {}
    lines: list[BudgetLine] = []
    used: set[str] = set()

    def key_for(item_label: str) -> str:
        """Stable keys: the row's first amount is its primary item
        (visa.fee, travel.employee.ticket, accommodation.hotel.budget);
        recognised extras get a fixed name; only other promises and
        further amounts are named from their own label."""
        n_label = _norm(item_label)
        if "maternity" in n_label:
            item = "maternity"
        elif "non sponsored" in n_label:
            item = "non_sponsored_id_card"
        elif category != BudgetCategory.OTHER and f"{prefix}.{_DEFAULT_ITEM.get(prefix, 'expense')}" not in used:
            item = _DEFAULT_ITEM.get(prefix, "expense")
        else:
            item = _slug(item_label) if item_label else _DEFAULT_ITEM.get(prefix, "expense")
        key, n = f"{prefix}.{item}", 2
        while key in used:
            key, n = f"{prefix}.{item}_{n}", n + 1
        used.add(key)
        return key

    for raw_line in (x.strip() for x in row.text.splitlines()):
        if not raw_line:
            continue
        amounts = find_amounts(raw_line)
        if not amounts:
            k, sep, v = raw_line.partition(":")
            detail = _DETAIL_KEYS.get(_norm(k)) if sep else None
            if detail and v.strip():
                details[detail] = v.strip()
            elif _has_bare_amount(raw_line):
                lines.append(BudgetLine(key=key_for(k if sep else ""), label=(k if sep else label).strip(),
                                        provenance=AmountProvenance.UNCERTAIN, note="amount without a currency",
                                        **base))
            else:
                details.setdefault("text", raw_line if "text" not in details else details["text"] + "; " + raw_line)
            continue
        for a in amounts:
            item_label = _split_label(raw_line, a.span) if len(amounts) == 1 else (
                _split_label(raw_line, a.span).split("+")[-1].strip() or raw_line[a.span[1]:].strip(" :-"))
            if a.error:
                lines.append(BudgetLine(key=key_for(item_label), label=item_label or label,
                                        provenance=AmountProvenance.INVALID, note=a.error, **base))
                continue
            assert a.money is not None
            tail = raw_line[a.span[1]:]
            if not item_label:
                item_label = tail.strip(" :-()") if tail.strip() and not _PER_NIGHT.match(tail) else ""
            # Shown label: HR's own "Label:" when the amount follows one,
            # otherwise the row's label (never a fragment like "7 days,").
            keyed = raw_line[: a.span[0]].rstrip().endswith(":")
            shown = item_label if keyed and item_label else label
            if _PER_NIGHT.match(tail):
                details["nightly_rate"] = a.money.original_text or ""
                if hotel_nights is None:
                    lines.append(BudgetLine(key=key_for(item_label or "budget"), label=shown,
                                            provenance=AmountProvenance.UNCERTAIN,
                                            note="nightly rate without a number of nights", **base))
                else:
                    total = Money(amount=a.money.amount * hotel_nights, currency=a.money.currency,
                                  original_text=a.money.original_text)
                    lines.append(BudgetLine(key=key_for(item_label or "budget"), label=shown,
                                            provenance=AmountProvenance.CALCULATED, money=total,
                                            note=f"nightly rate x {hotel_nights} nights", **base))
                continue
            note = None
            prov = AmountProvenance.EXTRACTED
            if category == BudgetCategory.OTHER and _RECURRING.search(raw_line):
                # A monthly allowance is a term of employment, not a one-off
                # onboarding expense: kept, shown, not counted.
                prov, note = AmountProvenance.MISSING, "recurring amount; not a one-off onboarding expense"
                lines.append(BudgetLine(key=key_for(item_label), label=shown, provenance=prov,
                                        note=note, details={"stated_amount": a.money.original_text or ""}, **base))
                continue
            lines.append(BudgetLine(key=key_for(item_label), label=shown, provenance=prov,
                                    money=a.money, note=note, **base))

    if not lines:
        lines.append(BudgetLine(key=key_for(""), label=label, provenance=AmountProvenance.MISSING,
                                note="no amount stated", **base))
    for i, line in enumerate(lines):
        # Structured facts (sector, dates, location) describe the whole row;
        # free text is kept once, on the row's first line.
        line.details.update({k: v for k, v in details.items()
                             if k not in line.details and (k != "text" or i == 0)})
    return lines


def _plan_selection(row: RowText) -> Optional[str]:
    if not row.text or row.conflict:
        return None
    n = _norm(row.text)
    found = [p for p in ("basic", "wn", "rn") if re.search(rf"\b{p}\b", n)]
    return found[0] if len(found) == 1 else None


def _attach_reference(line: BudgetLine, rates: Mapping[str, ReferenceRate], *, visa_years: Optional[int],
                      visa_where: Optional[str], plan: Optional[str]) -> None:
    n = _norm(line.label + " " + line.key)
    key: Optional[str] = None
    if line.key.startswith("visa.visit"):
        # The row's own label always says "visit": only HR's words count here.
        n = _norm(" ".join(line.details.values()) + (" " + line.label if line.money else ""))
        key =("visa.dummy_return_ticket" if "ticket" in n else "visa.visit_visa" if "visit" in n else None)
    elif line.key.startswith("visa."):
        if "non sponsored" in n or "non_sponsored" in line.key:
            key = "visa.non_sponsored_id_card"
        elif visa_years and visa_where:
            key = f"visa.{visa_years}y.{visa_where}"
    elif line.key.startswith(("insurance.employee", "insurance.family")) and plan:
        key = f"insurance.{plan}.maternity" if "maternity" in n else f"insurance.{plan}"
    if key and key in rates:
        line.reference = rates[key]


def build_budget(
    rows: Mapping[str, RowText],
    template_lines: list[tuple[str, str]],
    *,
    visa_duration_years: Optional[int],
    visa_location_category: Optional[str],
    hotel_nights: Optional[int],
    conversion_rates: Optional[Mapping[str, Decimal]] = None,
    base_currency: Optional[str] = None,
) -> OnboardingBudget:
    """Extract the selected expenses from HR's text, attach the matching
    rate-card price to each (information only), then calculate."""
    rates = parse_reference_rates(template_lines)
    by_key = {r.key: r for r in rates}
    where = {"INSIDE_COUNTRY": "inside", "OUTSIDE_COUNTRY": "outside"}.get(visa_location_category or "")
    plan = _plan_selection(rows.get("insurance.plan", RowText()))

    lines: list[BudgetLine] = []
    for row_key, category, prefix, label, _required in BUDGET_ROWS:
        row = rows.get(row_key, RowText())
        if row_key == "insurance.plan":
            # The plan row is a SELECTION; its amount (if any) is the plan's
            # stated rate and is not counted - premiums are counted on the
            # employee/family rows, so nothing is counted twice.
            if row.text and not is_not_applicable(row.text):
                detail = {"plan": plan.upper()} if plan else {"text": row.text.strip()}
                stated = find_amounts(row.text)
                if stated and stated[0].money:
                    detail["stated_rate"] = stated[0].money.original_text or ""
                line = BudgetLine(key="insurance.plan.selection", category=category, label=label,
                                  provenance=AmountProvenance.NOT_APPLICABLE, source=row.source,
                                  note="plan selection; premiums are counted on the employee and family rows",
                                  details=detail)
                if plan and f"insurance.{plan}" in by_key:
                    line.reference = by_key[f"insurance.{plan}"]
                lines.append(line)
            continue
        if row_key == "employment.visit_visa_entry_permit" and not (row.text or "").strip() and not row.conflict:
            continue  # optional alternative to the 1/2-year visa: absent is not "missing"
        if row_key == "offer.other_promises" and not (row.text or "").strip() and not row.conflict:
            continue  # nothing promised is not a missing amount
        if (row_key == "benefits.hotel" and not (row.text or "").strip() and not row.conflict
                and visa_location_category == "INSIDE_COUNTRY"):
            lines.append(BudgetLine(key="accommodation.hotel.budget", category=category, label=label,
                                    provenance=AmountProvenance.INFERRED_NOT_APPLICABLE, source=row.source,
                                    note="template: hotel stay only applies to staff outside UAE"))
            continue
        row_lines = _row_lines(prefix, category, label, row, hotel_nights if row_key == "benefits.hotel" else None)
        for line in row_lines:
            _attach_reference(line, by_key, visa_years=visa_duration_years, visa_where=where, plan=plan)
        lines.extend(row_lines)

    budget = OnboardingBudget(lines=lines, reference_rates=rates)
    return calculate_budget(budget, conversion_rates=conversion_rates, base_currency=base_currency)


# ------------------------------------------------------------------ calculation

def _convert(totals: Mapping[str, Decimal], conversion: ConversionApplied) -> Decimal:
    total = Decimal("0")
    for cur, amount in totals.items():
        rate = Decimal("1") if cur == conversion.base_currency else conversion.rates[cur]
        total += amount * rate
    return total.quantize(_CENT, rounding=ROUND_HALF_UP)


def calculate_budget(
    budget: OnboardingBudget,
    *,
    conversion_rates: Optional[Mapping[str, Decimal]] = None,
    base_currency: Optional[str] = None,
) -> OnboardingBudget:
    """Deterministic totals over the COUNTED lines only. Returns a new budget."""
    category_totals: dict[str, dict[str, Decimal]] = {}
    by_currency: dict[str, Decimal] = {}
    for line in budget.lines:
        if not line.counted:
            continue
        assert line.money is not None
        cat = category_totals.setdefault(line.category.value, {})
        cat[line.money.currency] = cat.get(line.money.currency, Decimal("0")) + line.money.amount
        by_currency[line.money.currency] = by_currency.get(line.money.currency, Decimal("0")) + line.money.amount

    reasons: list[str] = []
    currency: Optional[str] = None
    total: Optional[Decimal] = None
    conversion: Optional[ConversionApplied] = None
    if len(by_currency) == 1:
        currency, total = next(iter(by_currency.items()))
    elif len(by_currency) > 1:
        base = normalise_currency(base_currency) if base_currency else None
        rates = {normalise_currency(k): Decimal(v) for k, v in (conversion_rates or {}).items()}
        foreign = sorted(c for c in by_currency if c != base)
        if base and all(c in rates and rates[c] > 0 for c in foreign):
            conversion = ConversionApplied(base_currency=base, rates={c: rates[c] for c in foreign})
            currency, total = base, _convert(by_currency, conversion)
        else:
            reasons.append("mixed_currencies_without_conversion_rate")

    required = {prefix for _k, _c, prefix, _l, req in BUDGET_ROWS if req}
    for line in budget.lines:
        if line.provenance in (AmountProvenance.INVALID, AmountProvenance.UNCERTAIN):
            reasons.append(f"{line.provenance.value.lower()}:{line.key}")
        elif line.provenance == AmountProvenance.MISSING and _row_prefix(line.key) in required:
            reasons.append(f"missing:{line.key}")
    for prefix in sorted(required):
        if not any(_row_prefix(x.key) == prefix for x in budget.lines):
            reasons.append(f"missing:{prefix}")

    differences = []
    for line in budget.lines:
        ref = line.reference
        if line.counted and ref is not None and line.money is not None and line.provenance == AmountProvenance.EXTRACTED:
            if ref.money.currency == line.money.currency and ref.money.amount != line.money.amount:
                differences.append(f"{line.key}:selected_differs_from_{ref.key}")

    if not by_currency:
        status = BudgetStatus.UNAVAILABLE
        reasons.insert(0, "no_counted_amount")
    elif reasons:
        status = BudgetStatus.PARTIAL
    else:
        status = BudgetStatus.COMPLETE
    for cat in category_totals.values():
        for cur in cat:
            cat[cur] = cat[cur].quantize(_CENT, rounding=ROUND_HALF_UP)
    return budget.model_copy(update=dict(
        category_totals=category_totals,
        totals_by_currency={k: v.quantize(_CENT, rounding=ROUND_HALF_UP) for k, v in by_currency.items()},
        currency=currency, budget_total=total.quantize(_CENT, rounding=ROUND_HALF_UP) if total is not None else None,
        conversion=conversion, status=status, status_reasons=sorted(dict.fromkeys(reasons)),
        reference_differences=differences))


def _row_prefix(key: str) -> str:
    """The BUDGET_ROWS prefix a line key belongs to (longest match)."""
    prefixes = sorted((p for _k, _c, p, _l, _r in BUDGET_ROWS), key=len, reverse=True)
    return next((p for p in prefixes if key == p or key.startswith(p + ".")), key)


def budget_summary(budget: OnboardingBudget) -> dict[str, object]:
    """A flat, JSON-safe view (amounts as strings, so no float rounding)."""
    def money(m: Optional[Money]) -> Optional[dict[str, str]]:
        return None if m is None else {"amount": str(m.amount), "currency": m.currency,
                                       "original_text": m.original_text or ""}

    return {
        "status": budget.status.value, "status_reasons": list(budget.status_reasons),
        "currency": budget.currency,
        "budget_total": str(budget.budget_total) if budget.budget_total is not None else None,
        "totals_by_currency": {k: str(v) for k, v in budget.totals_by_currency.items()},
        "category_totals": {c.value: (str(t) if (t := budget.category_total(c)) is not None else None)
                            for c in BudgetCategory},
        "conversion": None if budget.conversion is None else {
            "base_currency": budget.conversion.base_currency,
            "rates": {k: str(v) for k, v in budget.conversion.rates.items()}},
        "lines": [{"key": x.key, "category": x.category.value, "label": x.label, "provenance": x.provenance.value,
                   "counted": x.counted, "money": money(x.money), "source": x.source, "note": x.note,
                   "reference": None if x.reference is None else {"key": x.reference.key,
                                                                  "money": money(x.reference.money)},
                   "details": dict(x.details)} for x in budget.lines],
        "reference_rates": [{"key": r.key, "category": r.category.value, "label": r.label,
                             "money": money(r.money), "source": r.source, "note": r.note}
                            for r in budget.reference_rates],
        "reference_differences": list(budget.reference_differences),
        "calculation_version": budget.calculation_version,
    }
