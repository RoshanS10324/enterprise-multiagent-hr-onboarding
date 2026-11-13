from __future__ import annotations

import re
from dataclasses import dataclass

INJECTION_PATTERNS: list[str] = [
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"you\s+are\s+now\s+",
    r"disregard\s+(all\s+)?",
    r"forget\s+(all\s+)?previous",
    r"new\s+instructions?:",
    r"system\s*:",
    r"act\s+as\s+(if\s+you\s+are|a\s+)",
    r"jailbreak",
    r"DAN\s+mode",
    r"prompt\s+injection",
    r"override\s+your\s+(instructions?|programming)",
    r"do\s+not\s+follow\s+",
    r"reveal\s+your\s+(system\s+)?prompt",
    r"print\s+your\s+(system\s+)?prompt",
]

_COMPILED = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]


@dataclass
class ScanResult:
    detected: bool
    matched_pattern: str | None


def scan_for_injection(text: str, field_name: str = "document_text") -> ScanResult:
    """Scan text for prompt-injection patterns. Returns ScanResult; does not raise."""
    if not text:
        return ScanResult(detected=False, matched_pattern=None)
    for pattern, compiled in zip(INJECTION_PATTERNS, _COMPILED):
        if compiled.search(text):
            return ScanResult(detected=True, matched_pattern=pattern)
    return ScanResult(detected=False, matched_pattern=None)
