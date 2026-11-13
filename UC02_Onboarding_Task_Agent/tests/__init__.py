"""Test package.

Puts ``src/`` on ``sys.path`` so the suite runs under stdlib ``unittest``
without an install step. ``pytest`` gets the same effect from
``[tool.pytest.ini_options] pythonpath`` once it is installed; this bootstrap is
needed because ``uv`` is unavailable, so the package cannot be installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
