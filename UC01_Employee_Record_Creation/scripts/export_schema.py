"""Read-only inventory tool: imports every Pydantic model in
sub_agent.models plus the [Proposed] intake simulator's
request/result models, and dumps model_json_schema() for each to
docs/schema/models.json.

This script only imports and introspects existing code; it does not
write, migrate, or connect to any database.

Usage: uv run python scripts/export_schema.py
"""

import importlib
import inspect
import json
import pkgutil
from pathlib import Path

from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "docs" / "schema" / "models.json"

# Primary source: every submodule of sub_agent.models.
# Secondary source: the [Proposed] email-intake simulator, which defines
# its own request/result models outside the models package.
MODULE_ROOTS = [
    "sub_agent.models",
    "sub_agent.intake.email_intake",
]


def _iter_submodules(package_name: str) -> list[str]:
    module = importlib.import_module(package_name)
    if not hasattr(module, "__path__"):
        return [package_name]
    names = [package_name]
    for info in pkgutil.iter_modules(module.__path__, prefix=f"{package_name}."):
        names.append(info.name)
    return names


def _collect_models() -> dict[str, type[BaseModel]]:
    found: dict[str, type[BaseModel]] = {}
    for root in MODULE_ROOTS:
        for module_name in _iter_submodules(root):
            module = importlib.import_module(module_name)
            for name, obj in inspect.getmembers(module, inspect.isclass):
                if not issubclass(obj, BaseModel) or obj is BaseModel:
                    continue
                if obj.__module__ != module_name:
                    continue  # skip re-exports; attribute it to its defining module
                qualified_name = f"{module_name}.{name}"
                found[qualified_name] = obj
    return found


def main() -> None:
    models = _collect_models()
    schemas: dict[str, dict] = {}
    for qualified_name, model_cls in sorted(models.items()):
        schemas[qualified_name] = model_cls.model_json_schema()

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(schemas, indent=2, default=str) + "\n")

    print(f"Exported {len(schemas)} model schemas to {OUT_PATH.relative_to(REPO_ROOT)}")
    for name in sorted(schemas):
        print(f"  - {name}")


if __name__ == "__main__":
    main()
