#!/bin/bash
# Sync netatmo tests from a local HA core checkout to this repo.
# Usage: ./import_tests.sh [path-to-ha-core]
#   Defaults to ../core if no path given.

set -e

HA_CORE="${1:-../core}"

if [ ! -d "${HA_CORE}/tests/components/netatmo" ]; then
    echo "Error: HA core netatmo test directory not found at ${HA_CORE}/tests/components/netatmo"
    echo "Usage: $0 [path-to-ha-core]"
    exit 1
fi

DEST="tests/components/netatmo"

echo "Syncing netatmo tests from ${HA_CORE}..."

# Remove old test files (keep directory structure)
rm -rf "${DEST}"

# Copy all netatmo test files verbatim
cp -r "${HA_CORE}/tests/components/netatmo" "${DEST}"

# Copy cloud test helper (needed by test_init.py)
mkdir -p tests/components/cloud
cp "${HA_CORE}/tests/components/cloud/__init__.py" tests/components/cloud/__init__.py

echo "Done. Copied:"
echo "  - $(find ${DEST} -name '*.py' | wc -l | tr -d ' ') Python files"
echo "  - $(find ${DEST}/fixtures -type f 2>/dev/null | wc -l | tr -d ' ') fixture files"
echo "  - $(find ${DEST}/snapshots -type f 2>/dev/null | wc -l | tr -d ' ') snapshot files"
echo ""
echo "Run 'scripts/test' to verify."
