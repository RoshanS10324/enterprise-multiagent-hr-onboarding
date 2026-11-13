"""Import bootstrap for the whole repository.

Why this file exists
--------------------

The package lives under ``src/``, and ``uv`` is unavailable here, so it cannot
be installed. Putting ``src/`` on ``sys.path`` lets both ``pytest`` and stdlib
``unittest`` run the suite without an install step. ``pytest`` also gets this
from ``[tool.pytest.ini_options] pythonpath``; this file covers the rest.

Six 0-byte legacy scaffolds (``src/config.py``, ``src/contracts.py``,
``src/synthetic.py``, ``src/agent.py``, ``src/testing.py``, ``src/__init__.py``)
shadow top-level module names, so every import must stay package-qualified:
``from onboarding_task_management import config``, never ``import config``.

Entry points that can be run directly — ``agent/api.py`` and
``agent/evaluation/run_evals.py`` under
``src/onboarding_task_management/`` — repeat this bootstrap themselves,
because a script run with ``python path/to.py`` never loads this file.

**Recommendation:** deleting the six empty ``src/*.py`` placeholders would
remove the shadowing trap entirely. That needs sign-off; until then,
package-qualified imports are the rule.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def ensure_import_order() -> None:
    """Put ``src/`` on ``sys.path``, ahead of the repository root.

    Idempotent: safe to call from several entry points in one process.
    """
    src = str(ROOT / "src")
    if src in sys.path:
        sys.path.remove(src)
    sys.path.insert(0, src)


ensure_import_order()


# The environment loader (``envfile.load_environment``) is a no-op under test, so a developer's real ``.env`` / ``.env.local``
# can never leak credentials, secrets or ``UC02_STORE_MODE`` into a test run.
import os as _os  # noqa: E402

_os.environ["UC02_DISABLE_DOTENV"] = "1"
