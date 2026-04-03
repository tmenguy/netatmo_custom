#!/usr/bin/env bash
# Generate translations/en.json from strings.json.
# Resolves [%key:...] placeholder references.
# strings.json is the single source of truth — never edit en.json directly.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/generate_translations.py"
