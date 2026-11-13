"""Read a .docx with the standard library only (zipfile + ElementTree).

A .docx is a ZIP of XML parts. Only `word/document.xml` is read: paragraphs
(with their style, so headings are kept) and tables (row by row, cell by
cell), in body order. Nothing is executed, no macro or embedded object is
opened, and no external reference is followed.

Untrusted input, so hard limits apply before any XML is parsed:

* file size, uncompressed size and part count are capped (zip bombs);
* a DTD anywhere in the XML is refused, which rules out entity-expansion
  attacks ("billion laughs") - ElementTree never resolves external
  entities, and a well-formed .docx never needs a DTD;
* a file that is not a ZIP, or has no main document part, is rejected.

`DocumentReadError` carries a code and a short reason, never document text.
"""

from __future__ import annotations

import io
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 60 * 1024 * 1024
MAX_PARTS = 2000
MAIN_PART = "word/document.xml"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class DocumentReadError(ValueError):
    def __init__(self, code: str, reason: str) -> None:
        self.code = code
        super().__init__(f"{code}: {reason}")


@dataclass(frozen=True)
class Paragraph:
    index: int
    text: str
    style: str = ""


@dataclass(frozen=True)
class Table:
    index: int
    # rows[r][c] = the cell's text, one entry per paragraph inside the cell.
    rows: list[list[list[str]]] = field(default_factory=list)


@dataclass(frozen=True)
class DocxContent:
    paragraphs: list[Paragraph]
    tables: list[Table]


def _paragraph_text(p: ET.Element) -> str:
    parts: list[str] = []
    for node in p.iter():
        if node.tag == f"{W}t":
            parts.append(node.text or "")
        elif node.tag == f"{W}tab":
            parts.append("\t")
        elif node.tag in (f"{W}br", f"{W}cr"):
            parts.append("\n")
    return "".join(parts)


def _style(p: ET.Element) -> str:
    s = p.find(f"{W}pPr/{W}pStyle")
    return (s.get(f"{W}val") or "") if s is not None else ""


def read_docx(data: bytes, filename: str = "") -> DocxContent:
    """Parse `data` as a .docx. Raises DocumentReadError on anything unsafe."""
    if filename and not filename.lower().endswith(".docx"):
        raise DocumentReadError("UNSUPPORTED_FORMAT", "only .docx is supported")
    if len(data) == 0:
        raise DocumentReadError("EMPTY_FILE", "the file is empty")
    if len(data) > MAX_FILE_BYTES:
        raise DocumentReadError("FILE_TOO_LARGE", f"limit is {MAX_FILE_BYTES} bytes")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise DocumentReadError("NOT_A_DOCX", "not a ZIP-based Office document")

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_PARTS:
            raise DocumentReadError("TOO_MANY_PARTS", f"limit is {MAX_PARTS} parts")
        if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED_BYTES:
            raise DocumentReadError("TOO_LARGE_UNCOMPRESSED", f"limit is {MAX_UNCOMPRESSED_BYTES} bytes")
        if MAIN_PART not in archive.namelist():
            raise DocumentReadError("NOT_A_DOCX", "no word/document.xml part")
        xml = archive.read(MAIN_PART)

    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        raise DocumentReadError("UNSAFE_XML", "document XML declares a DTD")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise DocumentReadError("CORRUPT_DOCX", "document XML is not well-formed") from exc

    body = root.find(f"{W}body")
    if body is None:
        raise DocumentReadError("CORRUPT_DOCX", "document has no body")

    paragraphs: list[Paragraph] = []
    tables: list[Table] = []
    for element in body:
        if element.tag == f"{W}p":
            text = _paragraph_text(element)
            if text.strip():
                paragraphs.append(Paragraph(index=len(paragraphs), text=text.strip(), style=_style(element)))
        elif element.tag == f"{W}tbl":
            rows = [
                [[_paragraph_text(p).strip() for p in cell.iter(f"{W}p")] for cell in row.findall(f"{W}tc")]
                for row in element.findall(f"{W}tr")
            ]
            tables.append(Table(index=len(tables), rows=rows))
    return DocxContent(paragraphs=paragraphs, tables=tables)
