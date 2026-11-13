"""A deliberately tiny YAML reader for the policy-parity test.

``pyyaml`` cannot be installed here (``uv`` missing), and the policy envelope
must still be checked against the Python mirror. Rather than skip the check,
this parses the small, flat subset ``guardrails/policy.yaml`` actually uses:

* nested mappings, by indentation
* ``key: value`` scalars (int, bool, ISO date, quoted/unquoted string)
* block sequences (``- item``)
* inline empty list (``[]``)
* ``#`` comments and blank lines

It is test-only scaffolding and intentionally lives under ``tests/``: it is not
a YAML implementation and must not be imported by ``src/``. When the shared
harness lands, ``PolicyStore`` + real ``pyyaml`` replace both this and the
Python mirror it checks.
"""

from __future__ import annotations

from datetime import date
from typing import Any


def _scalar(raw: str) -> Any:
    text = raw.strip()
    if text == "[]":
        return []
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text in ("true", "false"):
        return text == "true"
    if text in ("null", "~", ""):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    parts = text.split("-")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    return text


def _strip_comment(line: str) -> str:
    """Remove a trailing ``#`` comment that is not inside quotes."""
    out, quote = [], None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out).rstrip()


def parse(text: str) -> dict[str, Any]:
    """Parse the supported subset into nested dicts/lists."""
    root: dict[str, Any] = {}
    # Each frame: (indent, container, parent_container, key_in_parent)
    stack: list[tuple[int, Any, Any, str | None]] = [(-1, root, None, None)]

    for raw_line in text.splitlines():
        line = _strip_comment(raw_line)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        _, container, parent, key_in_parent = stack[-1]

        if body.startswith("- "):
            # The placeholder dict pushed for this key is really a list.
            if isinstance(container, dict) and not container and parent is not None:
                container = []
                parent[key_in_parent] = container
                stack[-1] = (stack[-1][0], container, parent, key_in_parent)
            container.append(_scalar(body[2:]))
            continue

        key, sep, value = body.partition(":")
        if not sep:
            raise ValueError(f"unsupported YAML line: {raw_line!r}")
        key = key.strip()

        if value.strip() == "":
            child: dict[str, Any] = {}
            container[key] = child
            stack.append((indent, child, container, key))
        else:
            container[key] = _scalar(value)

    return root
