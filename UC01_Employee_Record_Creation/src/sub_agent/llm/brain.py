"""Loads the agent's persona and task prompts from `brain/`.

Same shape as `policy.py`: a source-tree-relative default, overridable
by an environment variable for deployments where that path does not
hold. Fails closed -- a missing directory, a malformed task file, or a
task that declares a forbidden input raises rather than silently
falling back to a built-in prompt.
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

from .base import LlmAuthority, LlmError, LlmTaskSpec
from .guard import check_task_spec

DEFAULT_BRAIN_PATH = Path(__file__).resolve().parents[3] / "brain"
# Overridable for deployments where the source-tree-relative default
# doesn't hold. Empty/unset means "use DEFAULT_BRAIN_PATH".
_ENV_BRAIN_PATH = "UC01_BRAIN_PATH"

REQUIRED_TASK_KEYS = {
    "task",
    "version",
    "purpose",
    "authority",
    "inputs_allowed",
    "output_values",
    "prompt",
}


def brain_path() -> Path:
    override = os.environ.get(_ENV_BRAIN_PATH, "").strip()
    return Path(override) if override else DEFAULT_BRAIN_PATH


def load_persona(root: Optional[Path] = None) -> str:
    """The agent's persona text.

    NOT currently prepended to anything: `guard.build_prompt` assembles
    a prompt from the task spec and its declared inputs only, so no
    persona text reaches a model today. It is loaded and kept under test
    so the statement of what the agent is stays reviewable, and so a
    real client can adopt it deliberately. A client that does prepend it
    must remember the input guard does not inspect persona text.
    """
    path = (root or brain_path()) / "persona.md"
    if not path.is_file():
        raise LlmError(f"brain persona not found at {path}")
    return path.read_text(encoding="utf-8")


def load_task(name: str, root: Optional[Path] = None) -> LlmTaskSpec:
    path = (root or brain_path()) / "tasks" / f"{name}.yaml"
    if not path.is_file():
        raise LlmError(f"no brain task {name!r} at {path}")

    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    missing = REQUIRED_TASK_KEYS - set(document)
    if missing:
        raise LlmError(f"brain task {name!r} is missing keys: {sorted(missing)}")
    if document["task"] != name:
        raise LlmError(f"brain task {name!r} declares task {document['task']!r}")

    try:
        spec = LlmTaskSpec(
            task=document["task"],
            version=document["version"],
            purpose=document["purpose"],
            authority=LlmAuthority(document["authority"]),
            inputs_allowed=frozenset(document["inputs_allowed"]),
            output_values=frozenset(document["output_values"]),
            prompt=document["prompt"],
        )
    except ValueError as exc:
        raise LlmError(f"brain task {name!r} is invalid: {exc}") from exc

    # A task that declares salary/bank/loan as an input never loads.
    check_task_spec(spec)
    return spec


def load_all_tasks(root: Optional[Path] = None) -> dict[str, LlmTaskSpec]:
    tasks_dir = (root or brain_path()) / "tasks"
    if not tasks_dir.is_dir():
        raise LlmError(f"brain tasks directory not found at {tasks_dir}")
    return {p.stem: load_task(p.stem, root) for p in sorted(tasks_dir.glob("*.yaml"))}


@lru_cache(maxsize=1)
def cached_tasks() -> dict[str, LlmTaskSpec]:
    """Tasks for the default brain path, loaded once per process.

    Call `cached_tasks.cache_clear()` in a test that edits brain/.
    """
    return load_all_tasks()
