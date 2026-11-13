"""Deterministic privacy and untrusted-text checks (GR-06, GR-10, GR-18, BR-15).

Scans never return the matched text, only a label, so a scan result cannot itself leak the value.
"""

import re
from typing import Any

SENSITIVE_PATTERNS: dict[str, re.Pattern] = {
    "pay_or_compensation": re.compile(r"\b(salary|salaries|compensation|payslip|pay slip|wage|wages)\b", re.I),
    "loan_or_bank": re.compile(
        r"\b(loan (?:amount|account|balance|with|from|details)|mortgage|iban|swift|account number|"
        r"bank (?:account|name|details|statement|transfer))\b", re.I),
    "medical_or_disability": re.compile(r"\b(medical|diagnos\w*|disabilit\w*|illness|pregnan\w*|surgery)\b", re.I),
    "criminal_record": re.compile(r"\b(criminal|convict\w*|police clearance)\b", re.I),
    "money_amount": re.compile(r"\b(?:aed|usd|eur|gbp|sar)\s?\d[\d,.]*|\$\s?\d", re.I),
    "email_address": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "phone_number": re.compile(r"(?<!\w)(?:\+|00)\d[\d\s-]{7,}"),
    "national_id": re.compile(r"\b784[- ]?\d{4}[- ]?\d{7}[- ]?\d\b"),
    "passport_number": re.compile(r"\bpassport\b\s*(?:no\.?|number|#)?\s*[:\-]?\s*[A-Z0-9]{6,9}\b", re.I),
    "secret": re.compile(r"\b(api[_-]?key|secret|password|passwd|bearer)\b\s*[:=]|sk-[A-Za-z0-9]{10,}|"
                         r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.I),
}

INJECTION_PATTERNS: dict[str, re.Pattern] = {
    "ignore_instructions": re.compile(
        r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|all|earlier|system)\b.{0,20}"
        r"\b(instructions?|prompts?|rules?)", re.I | re.S),
    "role_hijack": re.compile(r"\b(you are now|act as|pretend to be|new instructions?)\b", re.I),
    "approval_claim": re.compile(r"\b(approve|approved|grant|activate)\b.{0,30}\b(this|it|request|access)\b", re.I | re.S),
}

REDACTED = "[REDACTED: HR item, see HR]"


class PrivacyViolation(RuntimeError):
    """Raised, fail closed, when an output or audit record would carry sensitive content."""


def scan_text(text: str) -> list[str]:
    return sorted(label for label, pattern in SENSITIVE_PATTERNS.items() if pattern.search(text))


def scan_object(obj: Any, path: str = "$") -> list[tuple[str, str]]:
    """Walk a JSON-like object (keys and string values). Returns (path, label) pairs."""
    hits: list[tuple[str, str]] = []
    if isinstance(obj, str):
        hits += [(path, label) for label in scan_text(obj)]
    elif isinstance(obj, dict):
        for key, value in obj.items():
            hits += [(f"{path}.<key>", label) for label in scan_text(str(key))]
            hits += scan_object(value, f"{path}.{key}")
    elif isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            hits += scan_object(value, f"{path}[{i}]")
    return hits


def assert_clean(obj: Any, where: str) -> None:
    hits = scan_object(obj)
    if hits:
        raise PrivacyViolation(f"{where}: sensitive content detected ({sorted({label for _, label in hits})})")


def sanitize_free_text(text: str | None, max_len: int = 200) -> str | None:
    """Make externally supplied free text safe to show a colleague: no control characters, bounded, and replaced
    entirely by a neutral 'HR item' marker if it looks sensitive (GR-06)."""
    if text is None:
        return None
    cleaned = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", text)).strip()
    if scan_text(cleaned):
        return REDACTED
    return cleaned[:max_len]


def detect_injection(text: str | None) -> list[str]:
    """Names of injection patterns found in untrusted text (GR-10). The text itself is never acted on."""
    if not text:
        return []
    return sorted(label for label, pattern in INJECTION_PATTERNS.items() if pattern.search(text))
