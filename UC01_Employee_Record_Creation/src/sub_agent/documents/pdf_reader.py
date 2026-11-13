"""Read a text-based .pdf of the onboarding form into the same DocxContent
shape the .docx reader produces, so ONE extractor (extraction.py) and one
validation path serve both formats.

The form is a table: label column on the left, then the template's printed
text and the value HR fills in. A PDF has no table structure, only
positioned text, so rows are rebuilt from positions:

* pypdf's layout mode renders each page as text lines that keep the
  horizontal positions; a run of 2+ spaces separates two columns;
* the left-most column start is the label column; text there is label
  text, everything to the right is row content;
* a line whose label text matches a form label (extraction.ROW_SPECS)
  starts a new row; any other line continues the current row (wrapped
  labels and multi-line cells);
* the "Roles and Responsibilities" heading with nothing beside it ends the
  table; the lines after it are paragraphs, like the .docx section;
* a line of the template's printed text that the PDF wrapped is joined
  again, so it is still recognised as template text (never as a value).

Each rebuilt row is [label lines] + [all other lines]: extraction.py drops
the template's printed lines from any cell and reads what remains, exactly
as for .docx.

Untrusted input: size and page limits apply before parsing, encrypted PDFs
are refused, nothing is executed (pypdf runs no JavaScript and follows no
link). A scanned PDF has no text layer and is rejected - no text is
recognised from images, so nothing is guessed. `DocumentReadError` carries a code and
a short reason, never document text.

Requires the optional dependency ``pypdf`` (``pip install .[pdf]``); without
it a PDF is rejected with PDF_SUPPORT_UNAVAILABLE, and .docx still works.
"""

from __future__ import annotations

import io
import re
from typing import Any

from .docx_reader import MAX_FILE_BYTES, DocumentReadError, DocxContent, Paragraph, Table

PDF_MIME = "application/pdf"
MAX_PAGES = 20
#: Text starting within this many characters of the left-most column is label text.
_LABEL_COLUMN_TOLERANCE = 1
#: Text starts within this many characters of each other belong to one table column.
_COLUMN_GAP = 6
#: A wrapped template line is rejoined from at most this many PDF lines.
_MAX_WRAP = 4
_BULLET = re.compile("^[•‣◦⁃∙*-]\\s*")
_SEGMENT = re.compile(r"\S+(?: \S+)*")  # text separated by 2+ spaces = separate columns
_ITEM = re.compile(r"^[A-Za-z][A-Za-z' ]{1,40}:\s")  # "Passport: RECEIVED", "Mobile number: ..."


def _lines(page: Any) -> list[list[tuple[int, str]]]:
    """[(column, text)] per visual line, top to bottom, left to right."""
    if page.get("/Contents") is None:
        return []  # nothing drawn on the page (layout mode cannot render an empty page)
    text = page.extract_text(extraction_mode="layout") or ""
    return [[(m.start(), m.group()) for m in _SEGMENT.finditer(line)] for line in text.splitlines() if line.strip()]


def _row_label(text: str) -> bool:
    from .extraction import ROW_SPECS, SUPPLEMENTARY_SPECS, _norm  # the extractor's own label rules

    label = _norm(text)
    return any(re.search(pattern, label) or re.match(pattern.rstrip("$"), label)
               for pattern, _ in ROW_SPECS) or any(re.search(pattern, label) for pattern, _ in SUPPLEMENTARY_SPECS)


def _rejoin_template_lines(lines: list[str]) -> list[str]:
    """Join consecutive lines that together are one line the template prints."""
    from .extraction import TEMPLATE_TEXT, _norm

    out, i = [], 0
    while i < len(lines):
        for j in range(min(len(lines), i + _MAX_WRAP), i + 1, -1):
            joined = " ".join(lines[i:j])
            if _norm(joined) in TEMPLATE_TEXT:
                out.append(joined)
                i = j
                break
        else:
            out.append(lines[i])
            i += 1
    return out


def read_pdf(data: bytes, filename: str = "") -> DocxContent:
    """Parse `data` as a text-based .pdf. Raises DocumentReadError on anything unsafe."""
    if filename and not filename.lower().endswith(".pdf"):
        raise DocumentReadError("UNSUPPORTED_FORMAT", "only .pdf is read here")
    if len(data) == 0:
        raise DocumentReadError("EMPTY_FILE", "the file is empty")
    if len(data) > MAX_FILE_BYTES:
        raise DocumentReadError("FILE_TOO_LARGE", f"limit is {MAX_FILE_BYTES} bytes")
    if data.lstrip()[:5] != b"%PDF-":
        raise DocumentReadError("NOT_A_PDF", "no PDF header")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise DocumentReadError("PDF_SUPPORT_UNAVAILABLE", "install the optional 'pdf' extra (pypdf)") from exc
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise DocumentReadError("ENCRYPTED_PDF", "password-protected PDFs are not read")
        if len(reader.pages) > MAX_PAGES:
            raise DocumentReadError("TOO_MANY_PAGES", f"limit is {MAX_PAGES} pages")
        all_lines = [line for page in reader.pages for line in _lines(page)]
    except DocumentReadError:
        raise
    except Exception as exc:  # noqa: BLE001 - PdfReadError and others for a malformed file; never echo it
        raise DocumentReadError("CORRUPT_PDF", "the PDF could not be parsed") from exc

    if not all_lines:
        raise DocumentReadError("NO_TEXT_LAYER", "the PDF has no text (scanned image?); images are not read")
    left = min(col for line in all_lines for col, _ in line)

    from .extraction import _RR_HEADING

    paragraphs: list[Paragraph] = []
    # Each row: (label lines, [(column, text)] of everything to the right).
    rows: list[tuple[list[str], list[tuple[int, str]]]] = []
    in_table, after_table = False, False
    for line in all_lines:
        label = " ".join(t for col, t in line if col - left <= _LABEL_COLUMN_TOLERANCE)
        rest = [(col, t) for col, t in line if col - left > _LABEL_COLUMN_TOLERANCE]
        whole = " ".join(t for _, t in line)
        if after_table:
            paragraphs.append(Paragraph(index=len(paragraphs), text=_BULLET.sub("", whole).strip()))
            continue
        if in_table and not rest and _RR_HEADING.search(whole) and len(rows) >= 2:
            after_table = True  # the section heading after the table, not the R&R row
            paragraphs.append(Paragraph(index=len(paragraphs), text=whole))
            continue
        if label and _row_label(label):
            in_table = True
            rows.append(([label], list(rest)))
        elif in_table and rows:
            if label:
                rows[-1][0].append(label)  # a wrapped label
            rows[-1][1].extend(rest)
        else:
            paragraphs.append(Paragraph(index=len(paragraphs), text=whole))

    columns = _column_starts([col for _, rest in rows for col, _ in rest])
    # Right edge of each column: where the next one starts, or the widest text in the last one.
    ends = [c - 1 for c in columns[1:]] + [max((col + len(t) for _, rest in rows for col, t in rest), default=0)]
    table_rows: list[list[list[str]]] = []
    for label_lines, rest in rows:
        cells: list[list[tuple[int, str]]] = [[] for _ in columns]
        for col, text in rest:
            cells[_column_of(col, columns)].append((col, text))
        # Wrapped label lines become one label, as a .docx cell paragraph would be read.
        table_rows.append([[" ".join(label_lines)]]
                          + [_rejoin_template_lines(_unwrap(c, ends[i])) for i, c in enumerate(cells)])
    return DocxContent(paragraphs=paragraphs, tables=[Table(index=0, rows=table_rows)] if table_rows else [])


def _unwrap(cell: list[tuple[int, str]], right_edge: int) -> list[str]:
    """Join a line to the previous one only when it is a visual wrap: its first
    word would not have fitted after the previous line in this column, and it
    does not start a "Label: value" item. Any other line break is a separate
    item (e.g. one document or personal detail per line)."""
    out: list[tuple[int, str]] = []
    for col, text in cell:
        if out and not _ITEM.match(text):
            prev_col, prev = out[-1]
            first_word = text.split(" ", 1)[0]
            if prev_col + len(prev) + 1 + len(first_word) > right_edge:
                out[-1] = (prev_col, f"{prev} {text}")
                continue
        out.append((col, text))
    return [t for _, t in out]


def _column_starts(starts: list[int]) -> list[int]:
    """The table's content columns: text starts closer than _COLUMN_GAP are one column."""
    columns: list[int] = []
    for start in sorted(set(starts)):
        if not columns or start - columns[-1] > _COLUMN_GAP:
            columns.append(start)
    return columns or [0]


def _column_of(start: int, columns: list[int]) -> int:
    return max(i for i, c in enumerate(columns) if c <= start + _COLUMN_GAP) if start + _COLUMN_GAP >= columns[0] else 0


def read_document(data: bytes, filename: str) -> DocxContent:
    """The reader for the file's type: .pdf or .docx. Anything else is refused."""
    from .docx_reader import read_docx

    name = (filename or "").lower()
    if name.endswith(".pdf"):
        return read_pdf(data, filename)
    if name.endswith(".docx"):
        return read_docx(data, filename)
    raise DocumentReadError("UNSUPPORTED_FORMAT", "only .docx and .pdf are supported")


def document_source(filename: str) -> str:
    return "pdf" if (filename or "").lower().endswith(".pdf") else "docx"
