"""Populate a COPY of the original 2026 onboarding form with SYNTHETIC data.

    python scripts/synthetic_onboarding_form.py generate --original PATH.docx
    python scripts/synthetic_onboarding_form.py generate --from-storage      # read-only download of the original

The original form ("Onboarding form - 2026") is a blank template: the
label column and the printed rate card are filled, the HR value column is
empty. This script never writes to the original. It reads its bytes and
writes NEW files:

    data/mock/documents/SYNTHETIC_onboarding_form_2026_populated.docx
    data/mock/documents/SYNTHETIC_onboarding_form_2026_populated.pdf

What changes in the copy, and nothing else:

* a banner paragraph at the top: "SYNTHETIC TEST DATA - NOT A REAL EMPLOYEE";
* the EMPTY value column (third cell) of each template row is filled with
  fictional values - the label column, the printed rate card and every other
  part (styles, fonts, numbering) are kept byte for byte;
* a second, clearly labelled table with the employee details the template
  has no row for (email, department, nationality, work location, grade,
  employment duration);
* the "Roles and Responsibilities (eg given below)" heading loses its
  "(eg given below)" marker, so the listed duties are this synthetic
  employee's confirmed duties rather than the template's example.

Every person, number and address is fictional (.invalid email domain,
SYNTHETIC-* placeholders for parents' names and the mobile number). Sample
SELECTIONS (what HR chose for this employee) are written in the value column;
the template's REFERENCE rates stay where the template prints them.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape, unescape

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
OUT_DIR = ROOT / "data" / "mock" / "documents"
OUT_STEM = "SYNTHETIC_onboarding_form_2026_populated"
#: The original's object in the shared bucket (read-only here).
ORIGINAL_BUCKET = "Doc_Collection"
ORIGINAL_OBJECT = "CLIENT-TEST-A/Onboarding form - 2026 (1).docx"
MAIN_PART = "word/document.xml"

BANNER = ("SYNTHETIC TEST DATA - NOT A REAL EMPLOYEE. Populated copy of 'Onboarding form - 2026' "
          "generated for UC-01 testing; every value is fictional.")

#: Fictional employee. Keyed by the template's row label (normalised);
#: each value is the list of lines HR writes in the value column.
SYNTHETIC_EMPLOYEE_EMAIL = "omar.synthetic@example.invalid"
VALUES: dict[str, list[str]] = {
    "name of employee": ["Omar Synthetic"],
    "reporting to": ["SYN Head of Data Governance"],
    "designation": ["Synthetic Data Governance Engineer"],
    "r&r": ["Leads the synthetic data governance programme (see Roles and Responsibilities)"],
    "final salary": ["SYNTHETIC: AED 22,000 per month (all inclusive)"],
    "doj": ["2027-01-17"],
    "probation period (3 or 6 months)": ["6 months"],
    "1 year or 2 years visa": ["Selected: 2 years - outside country", "Visa fee: AED 4,800"],
    "visa - visit + entry permit": ["Not applicable"],
    "direct entry permit": [
        "Direct entry permit: Required - employer sponsored",
        "Passport: RECEIVED", "Passport cover: RECEIVED", "Visa copy: PENDING",
        "Cancellation paper: PENDING", "Emirates ID: PENDING", "Signed offer letter: RECEIVED",
        "Photograph: RECEIVED", "Degree certificate: RECEIVED",
        "Father's name: SYNTHETIC-FATHER-NAME-31", "Mother's name: SYNTHETIC-MOTHER-NAME-31",
        "Marital status: Married", "Mobile number: SYNTHETIC-MOBILE-31",
    ],
    "insurance": ["WN"],
    "annual ticket -employee": ["Sector: Kochi (COK) - Dubai (DXB)", "Travel dates: 2027-01-15",
                                "Ticket cost: AED 1,250"],
    "annual ticket- family": ["Sector: Kochi (COK) - Dubai (DXB)", "Travel dates: 2027-02-10",
                              "Travellers: spouse and one child", "Ticket cost: AED 2,300"],
    "insurance for employee": ["Category: WN", "Premium: AED 3,500", "Census: submitted"],
    "insurance for family": ["Coverage: spouse", "Premium: AED 3,500", "Maternity cover: AED 1,000"],
    "hotel stay - how many days (only applicable for staff outside uae)": [
        "Duration: 7 nights", "Budget: AED 450 per night", "Location: Al Barsha, Dubai"],
    "any other promises made to the candidate": [
        "Relocation allowance (one-time): AED 2,000", "Transport allowance: AED 500 per month",
        "Laptop provided on day one"],
}
SUPPLEMENTARY_TITLE = "SYNTHETIC TEST DATA - supplementary employee details (not part of the 2026 template)"
SUPPLEMENTARY: list[tuple[str, str]] = [
    ("Employee Email", SYNTHETIC_EMPLOYEE_EMAIL),
    ("Department", "Data & Analytics"),
    ("Nationality", "Indian"),
    ("Work Location", "SYN Dubai Office"),
    ("Grade", "L5"),
    ("Employment Duration", "Permanent (unlimited contract)"),
]


def _norm_label(text: str) -> str:
    text = text.replace("’", "'").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip().lower()


_TEXT = re.compile(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", re.S)
_PARA = re.compile(r"<w:p(?:\s[^>]*)?/>|<w:p(?:\s[^>]*)?>.*?</w:p>", re.S)
_PPR = re.compile(r"<w:pPr>.*?</w:pPr>", re.S)


def _cell_text(cell_xml: str) -> str:
    return " ".join(p for p in (unescape("".join(_TEXT.findall(m.group(0)))) for m in _PARA.finditer(cell_xml)) if p)


def _paragraph(text: str, ppr: str = "") -> str:
    return f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def _fill_cell(cell_xml: str, lines: list[str]) -> str:
    """Replace the (empty) paragraphs of a value cell with one paragraph per
    line, reusing the cell's own paragraph properties."""
    if _cell_text(cell_xml).strip():
        raise ValueError("refusing to overwrite a value cell that is not empty")
    first = _PARA.search(cell_xml)
    ppr_match = _PPR.search(first.group(0)) if first else None
    ppr = ppr_match.group(0) if ppr_match else ""
    body = "".join(_paragraph(line, ppr) for line in lines)
    head_end = cell_xml.find("</w:tcPr>")
    head = cell_xml[: head_end + len("</w:tcPr>")] if head_end >= 0 else "<w:tc>"
    return head + body + "</w:tc>"


def _cells(row_xml: str) -> list[re.Match[str]]:
    return list(re.finditer(r"<w:tc>.*?</w:tc>", row_xml, re.S))


def _simple_table(rows: list[tuple[str, str]], title: str) -> str:
    border = ''.join(f'<w:{s} w:val="single" w:sz="4" w:space="0" w:color="000000"/>'
                     for s in ("top", "left", "bottom", "right", "insideH", "insideV"))
    out = [f'<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/><w:tblBorders>{border}</w:tblBorders></w:tblPr>'
           '<w:tblGrid><w:gridCol w:w="3600"/><w:gridCol w:w="5400"/></w:tblGrid>']
    for label, value in [(title, "")] + rows:
        out.append(f"<w:tr><w:tc>{_paragraph(label)}</w:tc><w:tc>{_paragraph(value) if value else '<w:p/>'}</w:tc></w:tr>")
    return "".join(out) + "</w:tbl>"


def populate_document_xml(xml: str, values: dict[str, list[str]] | None = None,
                          supplementary: list[tuple[str, str]] | None = None) -> str:
    """The original document.xml with the synthetic values filled in.
    `values`/`supplementary` default to the module's synthetic employee; tests
    pass variants (a value removed, an invalid currency) built the same way."""
    values = VALUES if values is None else values
    supplementary = SUPPLEMENTARY if supplementary is None else supplementary
    tables = list(re.finditer(r"<w:tbl>.*?</w:tbl>", xml, re.S))
    if len(tables) != 1:
        raise ValueError(f"expected the template's single table, found {len(tables)}")
    table = tables[0].group(0)
    filled_labels: set[str] = set()

    def fill_row(m: re.Match[str]) -> str:
        row = m.group(0)
        cells = _cells(row)
        if len(cells) != 3:
            return row
        label = _norm_label(_cell_text(cells[0].group(0)))
        lines = values.get(label)
        if not lines:
            return row
        filled_labels.add(label)
        value = cells[2]
        return row[: value.start()] + _fill_cell(value.group(0), lines) + row[value.end():]

    new_table = re.sub(r"<w:tr[ >].*?</w:tr>", fill_row, table, flags=re.S)
    missing = {k for k, v in values.items() if v} - filled_labels
    if missing:
        raise ValueError(f"template rows not found for: {sorted(missing)}")
    extra = _paragraph("") + _simple_table(supplementary, SUPPLEMENTARY_TITLE) if supplementary else ""
    xml = xml[: tables[0].start()] + new_table + extra + xml[tables[0].end():]

    # Banner as the first body paragraph.
    body = xml.find("<w:body>")
    if body < 0:
        raise ValueError("no document body")
    xml = xml[: body + len("<w:body>")] + _paragraph(BANNER) + xml[body + len("<w:body>"):]

    # "Roles and Responsibilities (eg given below)" -> confirmed duties.
    def heading(m: re.Match[str]) -> str:
        para = m.group(0)
        if "Roles and Responsibilities" not in "".join(_TEXT.findall(para)):
            return para
        open_tag = re.match(r"<w:p(?:\s[^>]*)?>", para)
        ppr = _PPR.search(para)
        return (open_tag.group(0) if open_tag else "<w:p>") + (ppr.group(0) if ppr else "") + \
            '<w:r><w:t xml:space="preserve">Roles and Responsibilities</w:t></w:r></w:p>'

    return _PARA.sub(heading, xml)


def populate_docx(original: bytes, values: dict[str, list[str]] | None = None,
                  supplementary: list[tuple[str, str]] | None = None) -> bytes:
    """A new .docx: every part of the original copied unchanged except
    word/document.xml, which gets the synthetic values."""
    src = zipfile.ZipFile(io.BytesIO(original))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == MAIN_PART:
                data = populate_document_xml(data.decode("utf-8"), values, supplementary).encode("utf-8")
            dst.writestr(info, data)
    return out.getvalue()


def build_pdf_copy(populated_docx: bytes) -> bytes:
    """A text-based PDF of the populated form: the same rows, template text
    and values, read from the populated .docx itself so the two never drift."""
    import importlib.util
    import textwrap

    from sub_agent.documents.docx_reader import read_docx

    spec = importlib.util.spec_from_file_location("mock_docs", ROOT / "scripts" / "mock_onboarding_documents.py")
    mock_docs = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mock_docs)  # type: ignore[union-attr]

    content = read_docx(populated_docx)
    widths, xs = (30, 40, 52), (30.0, 170.0, 340.0)
    pages: list[list[tuple[float, float, str]]] = [[]]
    y = 800.0

    def put_lines(cells: list[list[str]]) -> None:
        nonlocal y
        wrapped = [[w for line in cell for w in (textwrap.wrap(line, width) or [""])]
                   for cell, width in zip(cells, widths)]
        height = max((len(c) for c in wrapped), default=1) or 1
        if y - 11 * height < 40:
            pages.append([])
            y = 800.0
        for x, cell in zip(xs, wrapped):
            for i, line in enumerate(cell):
                if line:
                    pages[-1].append((x, y - 11 * i, line))
        y -= 11 * height + 4

    put_lines([[BANNER], [], []])
    for table in content.tables:
        for row in table.rows:
            cells = [list(c) for c in row] + [[]] * (3 - len(row))
            put_lines(cells[:3])
    after = False
    for p in content.paragraphs:
        if p.text.startswith("Roles and Responsibilities"):
            after = True
        if not after or not p.text.strip():
            continue
        # One text line per duty (as mock_onboarding_documents.build_pdf does):
        # a wrapped duty would read back as two duties.
        if y < 40:
            pages.append([])
            y = 800.0
        pages[-1].append((30.0, y, p.text if p.text.startswith("Roles") else "- " + p.text))
        y -= 13
    return mock_docs.render_pdf(pages)


def _download_original() -> bytes:
    """Read-only GET of the original from Supabase Storage (service role from .env)."""
    import os
    import urllib.parse

    import httpx

    for env_file in (ROOT.parent / ".env", ROOT / ".env"):
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                key, sep, value = line.strip().partition("=")
                if sep and not key.startswith("#"):
                    os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    url, key = os.environ.get("SUPABASE_URL", "").rstrip("/"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise SystemExit("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not set")
    path = urllib.parse.quote(ORIGINAL_OBJECT)
    resp = httpx.get(f"{url}/storage/v1/object/authenticated/{ORIGINAL_BUCKET}/{path}",
                     headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=60)
    resp.raise_for_status()
    return resp.content


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    gen = sub.add_parser("generate")
    src = gen.add_mutually_exclusive_group(required=True)
    src.add_argument("--original", type=Path)
    src.add_argument("--from-storage", action="store_true")
    gen.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    original = args.original.read_bytes() if args.original else _download_original()
    before = hashlib.sha256(original).hexdigest()
    populated = populate_docx(original)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    docx_path = args.out_dir / f"{OUT_STEM}.docx"
    pdf_path = args.out_dir / f"{OUT_STEM}.pdf"
    if args.original and docx_path.resolve() == args.original.resolve():
        raise SystemExit("refusing to overwrite the original")
    docx_path.write_bytes(populated)
    pdf_path.write_bytes(build_pdf_copy(populated))
    assert hashlib.sha256(original).hexdigest() == before  # the original bytes were never modified
    print(f"original sha256 {before} (unchanged, not written)")
    for p in (docx_path, pdf_path):
        print(f"wrote {p.relative_to(ROOT) if p.is_relative_to(ROOT) else p}  sha256 {hashlib.sha256(p.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
