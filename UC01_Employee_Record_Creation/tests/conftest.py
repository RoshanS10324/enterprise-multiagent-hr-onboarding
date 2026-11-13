import sys
from pathlib import Path

# So `import evaluation` (a plain repo-root package, not part of the installed
# sub_agent distribution) works from tests/test_evals.py
# regardless of the invoking cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
