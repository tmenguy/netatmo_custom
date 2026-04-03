#!/usr/bin/env python3
"""Generate translations/en.json from strings.json.

Resolves [%key:...] placeholder references (same logic as HA core's
script/translations/develop.py) so that strings.json can remain the
single source of truth with DRY references.

Usage:
    python scripts/generate_translations.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

COMPONENT = "netatmo"
COMPONENT_DIR = Path("custom_components") / COMPONENT
STRINGS_PATH = COMPONENT_DIR / "strings.json"
EN_JSON_PATH = COMPONENT_DIR / "translations" / "en.json"
HA_STRINGS_PATH = Path("custom_components") / ".." / "scripts"  / "strings_from_homeassistant.json"

# ---------------------------------------------------------------------------
# Reference substitution (extracted from HA core script/translations/util.py)
# ---------------------------------------------------------------------------


def flatten_translations(translations: dict[str, Any]) -> dict[str, str]:
    """Flatten nested dict into ``key1::key2::key3`` -> value mapping."""
    stack: list[Any] = [iter(translations.items())]
    key_stack: list[str] = []
    flat: dict[str, str] = {}
    while stack:
        for k, v in stack[-1]:
            key_stack.append(k)
            if isinstance(v, dict):
                stack.append(iter(v.items()))
                break
            if isinstance(v, str):
                flat["::".join(key_stack)] = v
                key_stack.pop()
        else:
            stack.pop()
            if key_stack:
                key_stack.pop()
    return flat


_KEY_RE = re.compile(r"\[%key:([a-z0-9_]+(?:::(?:[a-z0-9_-])+)+)%\]")


def _substitute_one(value: str, flat: dict[str, str]) -> str:
    """Resolve [%key:...%] references in a single string."""
    matches = _KEY_RE.findall(value)
    if not matches:
        return value
    result = value
    for key in matches:
        if key not in flat:
            print(f"ERROR: missing reference '{key}' in value '{value}'", file=sys.stderr)
            sys.exit(1)
        # Resolved value may itself contain references — recurse.
        resolved = _substitute_one(flat[key], flat)
        result = result.replace(f"[%key:{key}%]", resolved)
    return result


def substitute_references(
    translations: dict[str, Any], flat: dict[str, str]
) -> dict[str, Any]:
    """Recursively resolve all [%key:...%] references in a translations dict."""
    result: dict[str, Any] = {}
    for key, value in translations.items():
        if isinstance(value, dict):
            result[key] = substitute_references(value, flat)
        elif isinstance(value, str):
            result[key] = _substitute_one(value, flat)
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    if not STRINGS_PATH.exists():
        print(f"ERROR: {STRINGS_PATH} not found", file=sys.stderr)
        return 1

    strings = json.loads(STRINGS_PATH.read_text(encoding="utf-8"))
    wrapped = json.loads(HA_STRINGS_PATH.read_text(encoding="utf-8"))

    # Wrap in the same structure HA core uses so that
    # [%key:component::netatmo::...%] references resolve correctly.
    wrapped["component"] = {COMPONENT: strings}

    flat = flatten_translations(wrapped)

    resolved = substitute_references(strings, flat)

    EN_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    EN_JSON_PATH.write_text(
        json.dumps(resolved, indent=4, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"Generated {EN_JSON_PATH} from {STRINGS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
