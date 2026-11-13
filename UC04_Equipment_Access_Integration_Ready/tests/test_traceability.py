"""Guards the traceability claims: every UC-04 test id and in-scope edge case must be referenced by a real test."""

import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SOURCE = "\n".join(p.read_text("utf-8") for p in TESTS.glob("test_*.py") if p.name != "test_traceability.py").lower()

IN_SCOPE_EC = [10, 11, 12, 13, 19, 20, 22, 23, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 42, 44, 51, 52]


def test_every_tc_id_is_referenced_by_a_test():
    missing = [n for n in range(1, 40) if not re.search(rf"tc[-_]?{n:02d}(?!\d)", SOURCE)]
    assert missing == [], f"no test references TC-{missing}"


def test_every_in_scope_edge_case_is_referenced_by_a_test():
    missing = [n for n in IN_SCOPE_EC if not re.search(rf"ec[-_]?{n:02d}(?!\d)", SOURCE)]
    assert missing == [], f"no test references EC-{missing}"
