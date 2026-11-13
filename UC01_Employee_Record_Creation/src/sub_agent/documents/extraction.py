"""Deterministic extraction of the 2026 onboarding form (template TEMPLATE_ID).

The form is one table: column 0 is the field label, the next columns hold
either text the TEMPLATE prints (price lists, the documents checklist,
notes) or the value HR fills in. After the table comes a "Roles and
Responsibilities" section of bullet paragraphs.

How a value is found, for every row:

1. The row is matched to a field by its label (ROW_SPECS), never by
   position, so an added or reordered row does not shift every value.
2. Every line in the non-label cells is compared with the template's own
   printed text (TEMPLATE_TEXT). Those lines are set aside: price lines are
   kept as `reference_charges` (company information, never a salary), the
   rest are dropped. What remains is what HR wrote.
3. One remaining value -> parsed per field. Two different values in two
   cells -> UNCERTAIN (a person decides; nothing is picked silently).
   Nothing -> MISSING. Unparseable -> INVALID.

No model is used here: every sensitive field (salary, personal details,
document status) is read locally. Nothing is inferred - an empty cell stays
MISSING even when another field could suggest a value.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Callable, Optional

from pydantic import SecretStr

from .budget import BUDGET_ROWS, RowText, build_budget
from .docx_reader import DocxContent, Table
from .models import (
    DocumentStatus,
    ExtractedField,
    ExtractionMetadata,
    FieldStatus,
    OnboardingDocumentExtraction,
    ReferenceCharge,
    RolesAndResponsibilities,
)


def _norm(text: str) -> str:
    """Lower-case, unify quotes/dashes, collapse everything else to spaces."""
    text = text.replace("’", "'").replace("–", "-").replace("—", "-").lower()
    return re.sub(r"[^a-z0-9&%']+", " ", text).strip()


# Matches the item immediately following a checked-box symbol (☒ ☑) so we can
# determine which option was selected in checkbox rows like
# "☒ Outside country  ☐ Inside country" without normalising away the symbols.
_CHECKED_BOX = re.compile(r"[☒☑✓]\s*([^☒☑✓☐✗✕]+?)(?=[☒☑✓☐✗✕]|$)")


# Lines the template itself prints (normalised). Never treated as a value.
TEMPLATE_TEXT = frozenset(_norm(t) for t in (
    "ACME/GLOBAL",
    "1 YR - INSIDE COUNTRY: AED 4700/-", "1 YR - OUTSIDE COUNTRY: AED 4200/-",
    "2 YRS - INSIDE COUNTRY: AED 5100/-", "2 YRS - OUTSIDE COUNTRY: AED 4800/- NON SPONSORED ID CARD: AED 1100/-",
    "VISIT VISA : AED 380/-", "DUMMY RETURN TICKET: AED 100/-",
    "DOCS REQUIRED:", "PASSPORT (INCLUDING SCANNED PASSPORT COVER)", "VISA COPY / CANCELLATION PAPER",
    "VISA COPY", "CANCELLATION PAPER",
    "EMIRATES ID", "SIGNED OFFER LETTER", "CLEAR PHOTO AS PER ICA REQUIREMENTS", "ATTESTED DEGREE CERTIFICATE",
    "FATHER'S NAME/MOTHER'S NAME/MARITAL STATUS/MOBILE NUMBER",
    "BASIC: AED 1000/-", "WN: AED 3500/- + 1000/- FOR MATERNITY RN: AED 4500/- + 1900/- FOR MATERNITY",
    "NOTE: INDICATIVE PREMIUM DEPENDS ON PRE EXISTING CONDITION AND AGE",
    "NOTE: 10% TO 15% INCREASE ON PREMIUM EVERY YEAR",
    "SECTORS AND TRAVEL DATES", "CENSUS TO BE FILLED ALONG WITH REQUIRED", "DOCUMENTS",
    "BUDGET & AREA OR LOCATION ALSO TO BE MENTIONED",
))
# The header cell's entity text is printed by the template, but it IS the
# issuing entity, so that row keeps it (see _issuing_entity).
_ENTITY_TEXT = _norm("ACME/GLOBAL")
_ENTITY_TEXT_ALT = _norm("ACME / GLOBAL")

# (label regex on the normalised label, field key). First match wins, so
# the more specific "insurance for ..." rows come before plain "insurance".
ROW_SPECS: tuple[tuple[str, str], ...] = (
    (r"^offer letter$", "offer.issuing_entity"),
    # Employee name — exact ACME/GLOBAL label first, then common alternatives (prefix ok)
    (r"^name of employee$|^employee.{0,10}full.{0,10}name|^employee name|^full name$|^name$|^candidate name|^applicant name", "employee.employee_name"),
    # Reporting manager — "Reporting manager", "Reporting to", "Reports to", "Line manager" etc.
    (r"^reporting|^reports to|^line manager|^direct manager|^supervisor$|^manager name", "employee.reporting_to"),
    # Designation / job title — "Position / designation", "Job title", "Designation" etc.
    (r"designation|^job title|^position$|^role$|^title$|^post$|^job position", "employee.designation"),
    (r"^r ?& ?r$|^roles and responsibilities|^responsibilities$|^key responsibilities", "roles.summary"),
    # Salary — "Final annual salary / package", "Final salary", "Salary", "CTC" etc.
    (r"^final.{0,20}salary|^annual salary|^monthly salary|^gross salary|^total salary|^net salary|^offered salary|^basic salary|^ctc$|^compensation$|^salary.{0,20}package|^total compensation|^monthly ctc|^salary$", "employee.final_salary"),
    # Joining date — "Date of joining (DD/MM/YYYY)", "DOJ", "Joining date", "Start date" etc.
    (r"^doj$|^date of joining|^joining date|^start date$|^date of commencement|^commencement date|^effective date$|^date of employment|^employment start date", "employee.joining_date"),
    # Probation
    (r"^probation|^trial period$", "employee.probation_months"),
    (r"^1 year or 2 years visa$|^visa.*duration|^visa.*type|^visa.*permit.*route|^visa.*route", "employment.visa"),
    (r"^visa - visit \+ entry permit$|^visa visit entry permit$|^visa - visit|^visit.*entry permit|^visit visa.*entry", "employment.visit_visa_entry_permit"),
    (r"^direct entry permit$", "employment.direct_entry_permit"),
    (r"^insurance for employee|^employee insurance|^medical insurance|^health insurance", "insurance.employee_insurance"),
    (r"^insurance for family|^family insurance|^dependent insurance", "insurance.family_insurance"),
    (r"^insurance$", "insurance.plan"),
    # Ticket — both "Annual ticket - employee" and "Employee annual ticket" orderings
    (r"^annual ticket.{0,5}employee|^annual ticket employee|^employee.{0,10}annual ticket|^employee.{0,5}ticket|^flight ticket|^air ticket|^annual flight", "benefits.annual_employee_ticket"),
    (r"^annual ticket.{0,5}family|^annual ticket family|^family.{0,10}annual ticket|^family.{0,5}ticket(?!.*employee)|^family flight", "benefits.annual_family_ticket"),
    # Hotel — combined row (original form) or split rows (multi-section forms)
    (r"^hotel stay|^hotel.*required|^hotel.*temporary|^temporary stay|^accommodation$|^housing$", "benefits.hotel"),
    (r"^duration.*days$|^number of days$|^duration number|^hotel.*days|^nights?$|^stay.*days$|^days.*stay$", "benefits.hotel_days_only"),
    (r"^budget.*area|^budget and area|^hotel budget|^accommodation budget|^budget.*location", "benefits.hotel_budget_only"),
    # Visa location comes as a separate "Application location" row in some forms
    (r"^application location$|^application.*location$|^sponsorship.*location$|^processing.*location$", "employment.visa_location"),
    (r"^any other promises|^other benefits|^additional benefits|^special terms|^remarks$|^notes$", "offer.other_promises"),
    # Individual document rows — appear in 3-column checklists (Document | Status | Remarks).
    # More specific patterns first; first match wins.
    (r"^passport cover$|^scanned passport cover$|^passport cover.*page$", "documents.passport_cover"),
    (r"^passport$|^passport.{1,50}cover|^passport.{1,30}scanned", "documents.passport_and_cover"),
    (r"^visa copy.{0,20}cancellation|^cancellation paper$|^visa copy$", "documents.visa_copy_and_cancel"),
    (r"^emirates id$|^eid$|^emirates id card$", "documents.emirates_id"),
    (r"^signed offer letter$", "documents.signed_offer_letter"),
    (r"^(clear )?photo(graph)?$|^photo.{0,10}ica|^clear photo(graph)?", "documents.photograph"),
    (r"^(attested )?degree certificate$", "documents.degree_certificate"),
    # Personal detail rows — appear in 3-column checklists or as standalone rows.
    (r"^father.{0,10}name.{0,5}mother.{0,10}name", "personal.father_and_mother"),
    (r"^father.{0,15}name$", "personal.father_name"),
    (r"^mother.{0,15}name$", "personal.mother_name"),
    (r"^marital status$", "personal.marital_status"),
    (r"^mobile.{0,10}number$|^mobile$|^phone.{0,10}number$|^contact.{0,10}number$", "personal.mobile_number"),
)
# Two matching labels are enough — any structured HR form will score at least this.
# Unrelated documents (letters, specs) have no table rows matching HR labels and score 0.
_MIN_LABELS_FOR_TEMPLATE = 2

#: Rows the 2026 template does NOT print, read from ANY table when a document
#: adds them (e.g. a separate employee-details table). Never required.
SUPPLEMENTARY_SPECS: tuple[tuple[str, str], ...] = (
    (r"^(employee )?e ?mail( address| id)?$|^email of employee$|^personal email$|^email$|^e-mail$", "employee_email"),
    (r"^department|^dept$|^division$|^business unit$|^unit$|^team$", "department"),
    (r"^nationality$|^citizenship$|^country of citizenship$", "nationality"),
    (r"^work location|^place of work$|^location$|^office location|^work place$|^site$|^base location$|^employment location|^employment.*location|^work.*country", "work_location"),
    (r"^grade$|^job grade$|^band$|^level$|^pay grade$|^salary grade$", "grade"),
    (r"^employment duration$|^contract duration$|^duration of employment$|^contract period$|^employment period$|^tenure$", "employment_duration"),
)
_EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_SYNTHETIC_MARK = re.compile(r"\bsynthetic test (data|document)\b", re.I)
#: The template rows whose HR text feeds the budget (documents/budget.py).
_BUDGET_ROW_KEYS = frozenset(k for k, *_ in BUDGET_ROWS)

_DOCUMENT_KEYS = {
    "passport": "passport", "passport cover": "passport_cover", "scanned passport cover": "passport_cover",
    "visa copy": "visa_copy", "cancellation paper": "cancellation_paper", "emirates id": "emirates_id",
    "signed offer letter": "signed_offer_letter", "photo": "photograph", "photograph": "photograph",
    "clear photo": "photograph", "degree certificate": "degree_certificate",
    "attested degree certificate": "degree_certificate",
}
_PERSONAL_KEYS = {
    "father's name": "father_name", "fathers name": "father_name", "father name": "father_name",
    "mother's name": "mother_name", "mothers name": "mother_name", "mother name": "mother_name",
    "marital status": "marital_status", "mobile number": "mobile_number", "mobile": "mobile_number",
}
_SENSITIVE_PERSONAL = {"father_name", "mother_name", "mobile_number"}
_RECEIVED = {"received", "yes", "submitted", "attached", "provided", "done"}
_PENDING = {"pending", "no", "awaiting", "not received", "missing", "to be submitted"}
_MARITAL = {"single": "SINGLE", "married": "MARRIED", "divorced": "DIVORCED", "widowed": "WIDOWED"}
_RR_HEADING = re.compile(r"roles?\s*(and|&)\s*responsibilit", re.I)
_EXAMPLE_MARK = re.compile(r"\b(e\.?\s?g\.?|example|sample)\b", re.I)


# --------------------------------------------------------------------- parsers


def _present(value: Any, source: str) -> ExtractedField:
    return ExtractedField(status=FieldStatus.PRESENT, value=value, source=source)


def _invalid(source: str, note: str) -> ExtractedField:
    return ExtractedField(status=FieldStatus.INVALID, source=source, note=note)


def _text(raw: str, source: str) -> ExtractedField:
    return _present(raw.strip(), source)


def _salary(raw: str, source: str) -> ExtractedField:
    if not re.search(r"\d", raw):
        return _invalid(source, "no amount found")
    return _present(SecretStr(raw.strip()), source)


_DATE_FORMATS = ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%d-%b-%Y", "%d-%B-%Y", "%B %d, %Y", "%b %d, %Y")


def _joining_date(raw: str, source: str) -> ExtractedField:
    text = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return _present(datetime.strptime(text, fmt).date(), source)
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", text)
    if m:
        first, second, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            parsed = date(year, second, first)  # UAE convention: day first
        except ValueError:
            return _invalid(source, "not a valid calendar date")
        if first <= 12 and second <= 12 and first != second:
            # 05/06/2026 is 5 June or 6 May: never guess silently.
            return ExtractedField(status=FieldStatus.UNCERTAIN, value=parsed, source=source,
                                  note="ambiguous day/month order; read as day first")
        return _present(parsed, source)
    return _invalid(source, "unrecognised date format")


def _probation(raw: str, source: str) -> ExtractedField:
    words = {"three": 3, "six": 6}
    m = re.search(r"\b(\d+)\b", raw) or None
    months = int(m.group(1)) if m else next((v for k, v in words.items() if k in raw.lower()), None)
    if months is None:
        return _invalid(source, "no number of months found")
    if months not in (3, 6):
        return _invalid(source, "probation must be 3 or 6 months")
    return _present(months, source)


def _int_field(raw: str, source: str) -> ExtractedField:
    m = re.search(r"\b(\d+)\b", raw)
    return _present(int(m.group(1)), source) if m else _invalid(source, "no number found")


# --------------------------------------------------------------------- table rows


def _label(row: list[list[str]]) -> str:
    return _norm(" ".join(row[0])) if row else ""


def _split_cells(row: list[list[str]], table_index: int, row_index: int):
    """(values, template_lines) for one row: values = [(text, source)] per
    non-label cell that still has text after removing template lines."""
    values: list[tuple[str, str]] = []
    template_lines: list[tuple[str, str]] = []
    for c, cell in enumerate(row[1:], start=1):
        source = f"table[{table_index}].row[{row_index}].cell[{c}]"
        kept = []
        for line in (part for paragraph in cell for part in paragraph.splitlines()):
            if not line.strip():
                continue
            if _norm(line) in TEMPLATE_TEXT:
                template_lines.append((line.strip(), source))
            else:
                kept.append(line.strip())
        if kept:
            values.append(("\n".join(kept), source))
    return values, template_lines


def _single_value(values: list[tuple[str, str]], row_source: str) -> tuple[Optional[str], str, Optional[ExtractedField]]:
    """(text, source, conflict_field). Two cells that disagree -> UNCERTAIN.
    `source` is the value's cell, or the row when there is no single value."""
    distinct: dict[str, tuple[str, str]] = {}
    for text, source in values:
        distinct.setdefault(_norm(text), (text, source))
    if not distinct:
        return None, row_source, None
    if len(distinct) > 1:
        return None, row_source, ExtractedField(status=FieldStatus.UNCERTAIN, source=row_source,
                                                note=f"{len(distinct)} different values in one row")
    text, source = next(iter(distinct.values()))
    return text, source, None


def _parse_row(values, row_source: str, parser: Callable[[str, str], ExtractedField]) -> ExtractedField:
    text, source, conflict = _single_value(values, row_source)
    if conflict is not None:
        return conflict
    if text is None:
        return ExtractedField(status=FieldStatus.MISSING, source=row_source)
    return parser(text, source)


def _keyed_lines(text: str) -> tuple[dict[str, str], list[str]]:
    """'Key: value' lines -> {normalised key: value}; other lines returned apart."""
    keyed: dict[str, str] = {}
    other: list[str] = []
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and value.strip():
            keyed[_norm(key)] = value.strip()
        elif line.strip():
            other.append(line.strip())
    return keyed, other


# --------------------------------------------------------------------- extraction


def _all_form_tables(content: DocxContent) -> tuple[list[Table], int]:
    """All tables that contain at least one ROW_SPECS label, sorted best first.
    Sorting by hit count ensures the main form table (most labels) is always
    processed before secondary tables (e.g. document checklists or summary
    tables that happen to share a label). seen_keys then prevents the secondary
    tables from overwriting values already set by the main table."""
    scored: list[tuple[int, Table]] = []
    for table in content.tables:
        hits = sum(1 for row in table.rows if any(re.search(p, _label(row)) for p, _ in ROW_SPECS))
        if hits > 0:
            scored.append((hits, table))
    scored.sort(key=lambda x: x[0], reverse=True)
    total_hits = sum(h for h, _ in scored)
    return [t for _, t in scored], total_hits


def extract_onboarding_form(
    content: DocxContent, *, document_name: str, sha256: str, size_bytes: int
) -> OnboardingDocumentExtraction:
    tables, hits = _all_form_tables(content)
    out = OnboardingDocumentExtraction(metadata=ExtractionMetadata(
        source_document_name=document_name, source_sha256=sha256, source_size_bytes=size_bytes,
        template_recognised=hits >= _MIN_LABELS_FOR_TEMPLATE))

    budget_rows: dict[str, RowText] = {}
    seen_keys: set[str] = set()  # first table to claim a key wins; later tables skip it
    for table in tables:
        _extract_table(table, out, budget_rows, seen_keys)
    _extract_supplementary(content, out)
    out.roles = _extract_roles(content, out.roles.summary)
    out.metadata.synthetic_document = _is_synthetic(content)
    out.budget = _budget(out, budget_rows)
    _finalise_metadata(out)
    return out


def _budget(out: OnboardingDocumentExtraction, rows: dict[str, RowText]):
    emp, hotel = out.employment, out.benefits.hotel_stay_days
    return build_budget(
        rows, [(c.text, c.source) for c in out.reference_charges],
        visa_duration_years=emp.visa_duration_years.value if emp.visa_duration_years.usable else None,
        visa_location_category=emp.visa_location_category.value if emp.visa_location_category.usable else None,
        hotel_nights=hotel.value if hotel.usable else None)


def _is_synthetic(content: DocxContent) -> bool:
    texts = [p.text for p in content.paragraphs]
    texts += [line for t in content.tables for row in t.rows for cell in row for line in cell]
    return any(_SYNTHETIC_MARK.search(t) for t in texts)


def _extract_supplementary(content: DocxContent, out: OnboardingDocumentExtraction) -> None:
    for table in content.tables:
        for r, row in enumerate(table.rows):
            label = _label(row)
            name = next((n for p, n in SUPPLEMENTARY_SPECS if re.search(p, label)), None)
            if name is None:
                continue
            row_source = f"table[{table.index}].row[{r}]"
            values, _ = _split_cells(row, table.index, r)
            parser = _email if name == "employee_email" else _text
            field = _parse_row(values, row_source, parser)
            current = getattr(out.supplementary, name)
            if (current.status != FieldStatus.MISSING and field.status != FieldStatus.MISSING
                    and _norm(str(current.value)) != _norm(str(field.value))):
                field = ExtractedField(status=FieldStatus.UNCERTAIN, source=row_source,
                                       note="stated twice with different values")
            if field.status != FieldStatus.MISSING or current.status == FieldStatus.MISSING:
                setattr(out.supplementary, name, field)
            if row_source in out.metadata.unrecognised_rows:
                out.metadata.unrecognised_rows.remove(row_source)


def _email(raw: str, source: str) -> ExtractedField:
    text = raw.strip()
    return _present(text.lower(), source) if _EMAIL.match(text) else _invalid(source, "not an email address")


def _set(out: OnboardingDocumentExtraction, key: str, value: ExtractedField) -> None:
    section, name = key.split(".", 1)
    setattr(getattr(out, section), name, value)


def _extract_table(
    table: Table,
    out: OnboardingDocumentExtraction,
    budget_rows: dict[str, RowText],
    seen_keys: Optional[set] = None,
) -> None:
    if seen_keys is None:
        seen_keys = set()
    for r, row in enumerate(table.rows):
        label = _label(row)
        row_source = f"table[{table.index}].row[{r}]"
        key = next((k for p, k in ROW_SPECS if re.search(p, label)), None)
        if key is None:
            if label:
                out.metadata.unrecognised_rows.append(row_source)
            continue
        if key in seen_keys:
            continue
        seen_keys.add(key)
        values, template_lines = _split_cells(row, table.index, r)
        copied: list[tuple[str, str]] = []
        first_cell: dict[str, str] = {}
        for line, source in template_lines:
            if "aed" not in _norm(line):
                continue
            seen_in = first_cell.setdefault(_norm(line), source)
            if seen_in == source:
                out.reference_charges.append(ReferenceCharge(section=key, text=line, source=source))
            else:
                # The same printed price again, in a LATER cell of the row:
                # HR copied it into the value column as the selection. It
                # feeds the budget; it is not a second rate card.
                copied.append((line, source))
        if key in _BUDGET_ROW_KEYS:
            text, source, conflict = _single_value(values + copied, row_source)
            budget_rows[key] = RowText(text=text, source=source, conflict=conflict is not None)

        if key == "offer.issuing_entity":
            entity = [(t, s) for t, s in template_lines if (_norm(t) == _ENTITY_TEXT or _norm(t) == _ENTITY_TEXT_ALT)]
            out.offer.issuing_entity = _parse_row(values or entity, row_source, _text)
        elif key == "employee.final_salary":
            out.employee.final_salary = _parse_row(values, row_source, _salary)
        elif key == "employee.joining_date":
            out.employee.joining_date = _parse_row(values, row_source, _joining_date)
        elif key == "employee.probation_months":
            out.employee.probation_months = _parse_row(values, row_source, _probation)
        elif key == "employment.visa":
            _extract_visa(values, row_source, out)
        elif key == "employment.visa_location":
            _extract_visa_location(values, row_source, out)
        elif key == "employment.direct_entry_permit":
            _extract_documents_and_personal(values, row_source, out)
        elif key == "benefits.hotel":
            _extract_hotel(values, row_source, out)
        elif key == "benefits.hotel_days_only":
            text, source, conflict = _single_value(values, row_source)
            if conflict:
                out.benefits.hotel_stay_days = conflict
            elif text:
                m = re.search(r"\b(\d+)\b", text)
                if m:
                    out.benefits.hotel_stay_days = _present(int(m.group(1)), source)
        elif key == "benefits.hotel_budget_only":
            text, source, conflict = _single_value(values, row_source)
            if conflict:
                out.benefits.hotel_budget_and_location = conflict
            elif text:
                out.benefits.hotel_budget_and_location = _present(text, source)
        elif key in ("documents.passport_cover", "documents.passport_and_cover",
                     "documents.visa_copy_and_cancel", "documents.emirates_id",
                     "documents.signed_offer_letter", "documents.photograph",
                     "documents.degree_certificate"):
            _extract_doc_row(key, values, row_source, out)
        elif key in ("personal.father_and_mother", "personal.father_name", "personal.mother_name",
                     "personal.marital_status", "personal.mobile_number"):
            _extract_personal_row(key, values, row_source, out)
        else:
            _set(out, key, _parse_row(values, row_source, _text))


def _location_from_text(text: str, source: str) -> ExtractedField:
    """Extract INSIDE_COUNTRY / OUTSIDE_COUNTRY from raw text, checkbox-aware.
    When ☒ symbols are present we look only at the checked option; otherwise
    we scan the full normalised text."""
    checked = _CHECKED_BOX.search(text)
    scan = _norm(checked.group(1)) if checked else _norm(text)
    inside, outside = "inside" in scan, "outside" in scan
    if inside != outside:
        return _present("INSIDE_COUNTRY" if inside else "OUTSIDE_COUNTRY", source)
    return ExtractedField(
        status=FieldStatus.MISSING if not (inside or outside) else FieldStatus.UNCERTAIN,
        source=source, note=None if not (inside or outside) else "both inside and outside")


def _extract_visa(values, row_source: str, out: OnboardingDocumentExtraction) -> None:
    text, source, conflict = _single_value(values, row_source)
    if conflict is not None:
        out.employment.visa_duration_years = conflict
        out.employment.visa_location_category = conflict
        return
    if text is None:
        return  # both stay MISSING
    n = _norm(text)
    years = re.search(r"\b([12])\s*(yr|yrs|year|years)\b", n)
    out.employment.visa_duration_years = (
        _present(int(years.group(1)), source) if years else _invalid(source, "expected 1 or 2 years"))
    # Only attempt location from this row if inside/outside language is present;
    # forms that put location in a separate "Application location" row leave this MISSING.
    if "inside" in n or "outside" in n:
        out.employment.visa_location_category = _location_from_text(text, source)


def _extract_visa_location(values, row_source: str, out: OnboardingDocumentExtraction) -> None:
    """Handle a standalone "Application location" row (checkbox form style)."""
    text, source, conflict = _single_value(values, row_source)
    if conflict is not None:
        out.employment.visa_location_category = conflict
        return
    if text is None:
        return
    out.employment.visa_location_category = _location_from_text(text, source)


def _extract_documents_and_personal(values, row_source: str, out: OnboardingDocumentExtraction) -> None:
    text, source, conflict = _single_value(values, row_source)
    if conflict is not None:
        out.employment.direct_entry_permit = conflict
        return
    if text is None:
        return
    keyed, other = _keyed_lines(text)
    if other:
        out.employment.direct_entry_permit = _present(" ".join(other), source)
    for key, raw in keyed.items():
        if key in ("direct entry permit", "entry permit"):
            out.employment.direct_entry_permit = _present(raw, source)
        elif key in _DOCUMENT_KEYS:
            word = _norm(raw)
            status = (DocumentStatus.RECEIVED if word in _RECEIVED
                      else DocumentStatus.PENDING if word in _PENDING else None)
            setattr(out.documents, _DOCUMENT_KEYS[key],
                    _present(status, source) if status else
                    ExtractedField(status=FieldStatus.UNCERTAIN, source=source, note="unrecognised document status"))
        elif key in _PERSONAL_KEYS:
            name = _PERSONAL_KEYS[key]
            if name == "marital_status":
                value = _MARITAL.get(_norm(raw))
                out.personal.marital_status = (_present(value, source) if value else
                                               _invalid(source, "unrecognised marital status"))
            else:
                setattr(out.personal, name, _present(SecretStr(raw), source) if name in _SENSITIVE_PERSONAL
                        else _present(raw, source))
        else:
            out.metadata.unrecognised_rows.append(f"{source}:key")


def _extract_hotel(values, row_source: str, out: OnboardingDocumentExtraction) -> None:
    text, source, conflict = _single_value(values, row_source)
    if conflict is not None:
        out.benefits.hotel_stay_days = conflict
        out.benefits.hotel_budget_and_location = conflict
        return
    if text is None:
        return
    days = re.search(r"\b(\d+)\s*(day|days|night|nights)\b", text, re.I) or \
        re.search(r"days?\s*:\s*(\d+)", text, re.I)
    if days:
        out.benefits.hotel_stay_days = _present(int(days.group(1)), source)
    rest = re.sub(r"days?\s*:\s*\d+|\b\d+\s*(day|days|night|nights)\b", "", text, flags=re.I).strip(" ,;\n")
    if rest:
        out.benefits.hotel_budget_and_location = _present(rest, source)


def _doc_status_from_first_cell(values: list[tuple[str, str]], row_source: str) -> ExtractedField:
    """Parse document received/pending status from the first value cell only.
    Used for 3-column checklist tables (Document | Status | Remarks) where the
    second column holds the status and the third holds remarks we don't need."""
    if not values:
        return ExtractedField(status=FieldStatus.MISSING, source=row_source)
    text, source = values[0]
    checked = _CHECKED_BOX.search(text)
    word = _norm(checked.group(1) if checked else text)
    if word in _RECEIVED:
        return _present(DocumentStatus.RECEIVED, source)
    if word in _PENDING:
        return _present(DocumentStatus.PENDING, source)
    return ExtractedField(status=FieldStatus.UNCERTAIN, source=source,
                          note=f"unrecognised document status: {word!r}")


def _extract_doc_row(key: str, values: list[tuple[str, str]], row_source: str,
                     out: OnboardingDocumentExtraction) -> None:
    """Handle a standalone document-checklist row (3-column: Doc | Status | Remarks)."""
    field = _doc_status_from_first_cell(values, row_source)
    if key == "documents.passport_and_cover":
        for attr in ("passport", "passport_cover"):
            if getattr(out.documents, attr).status == FieldStatus.MISSING:
                setattr(out.documents, attr, field)
    elif key == "documents.visa_copy_and_cancel":
        for attr in ("visa_copy", "cancellation_paper"):
            if getattr(out.documents, attr).status == FieldStatus.MISSING:
                setattr(out.documents, attr, field)
    else:
        attr = key.split(".", 1)[1]
        if getattr(out.documents, attr).status == FieldStatus.MISSING:
            setattr(out.documents, attr, field)


def _extract_personal_row(key: str, values: list[tuple[str, str]], row_source: str,
                          out: OnboardingDocumentExtraction) -> None:
    """Handle a standalone personal-detail row (may be 2-col with actual value,
    or 3-col acknowledgement row where cell[1] says 'Received').
    Uses the first value cell; leaves the field MISSING when the cell only
    acknowledges receipt (no actual value is present in the form)."""
    if not values:
        return
    text, source = values[0]
    checked = _CHECKED_BOX.search(text)
    word = _norm(checked.group(1) if checked else text)
    # Receipt-only acknowledgement — the actual value is not in the form.
    if word in _RECEIVED or word in _PENDING:
        return
    if key == "personal.father_and_mother":
        # Combined row: cannot split into individual names from a single cell.
        return
    elif key == "personal.marital_status":
        value = _MARITAL.get(word)
        if value and out.personal.marital_status.status == FieldStatus.MISSING:
            out.personal.marital_status = _present(value, source)
        elif not value and out.personal.marital_status.status == FieldStatus.MISSING:
            out.personal.marital_status = _invalid(source, f"unrecognised marital status: {word!r}")
    elif key == "personal.father_name":
        if out.personal.father_name.status == FieldStatus.MISSING:
            out.personal.father_name = _present(SecretStr(text.strip()), source)
    elif key == "personal.mother_name":
        if out.personal.mother_name.status == FieldStatus.MISSING:
            out.personal.mother_name = _present(SecretStr(text.strip()), source)
    elif key == "personal.mobile_number":
        if out.personal.mobile_number.status == FieldStatus.MISSING:
            out.personal.mobile_number = _present(SecretStr(text.strip()), source)


def _extract_roles(content: DocxContent, summary: ExtractedField) -> RolesAndResponsibilities:
    heading = next((p for p in content.paragraphs if _RR_HEADING.search(p.text)), None)
    if heading is not None:
        items: list[str] = []
        for p in content.paragraphs[heading.index + 1:]:
            if p.style.lower().startswith("heading") or _RR_HEADING.search(p.text):
                break
            items.append(p.text)
        return RolesAndResponsibilities(
            responsibilities=items,
            is_example=bool(_EXAMPLE_MARK.search(heading.text)),
            source=f"paragraph[{heading.index}]",
            summary=summary,
        )

    # Fall back: look for R&R section inside table cells (e.g. 3-column form layout
    # where "Roles & Responsibilities" is a row label and items are in adjacent cells).
    for table in content.tables:
        for r, row in enumerate(table.rows):
            label_text = " ".join(row[0]) if row else ""
            if not _RR_HEADING.search(label_text):
                continue
            # Collect all text lines from every non-label cell in this row and
            # any immediately following rows that have no label of their own.
            items = []
            source = f"table[{table.index}].row[{r}]"
            is_example = bool(_EXAMPLE_MARK.search(label_text))
            for cell in row[1:]:
                for para in cell:
                    for line in para.splitlines():
                        if line.strip():
                            items.append(line.strip())
            # Absorb continuation rows (first cell empty, not a new label)
            for row2 in table.rows[r + 1:]:
                if row2 and any(s.strip() for para in row2[0] for s in para.splitlines()):
                    break  # new labelled row — stop
                for cell in row2[1:] if len(row2) > 1 else row2:
                    for para in cell:
                        for line in para.splitlines():
                            if line.strip():
                                items.append(line.strip())
            return RolesAndResponsibilities(
                responsibilities=items,
                is_example=is_example,
                source=source,
                summary=summary,
            )

    return RolesAndResponsibilities(summary=summary)


def _finalise_metadata(out: OnboardingDocumentExtraction) -> None:
    fields = out.fields()
    md = out.metadata
    md.missing_fields = sorted(k for k, f in fields.items() if f.status == FieldStatus.MISSING)
    md.invalid_fields = sorted(k for k, f in fields.items() if f.status == FieldStatus.INVALID)
    md.uncertain_fields = sorted(k for k, f in fields.items() if f.status == FieldStatus.UNCERTAIN)
    md.completeness = round(sum(f.status == FieldStatus.PRESENT for f in fields.values()) / len(fields), 3)
