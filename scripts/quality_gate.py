#!/usr/bin/env python3
"""Quality gate script for the netatmo custom component.

Runs linting and tests following Home Assistant conventions.
Outputs structured, parseable results so an AI agent can fix issues.

Usage:
    python scripts/quality_gate.py              # Run everything
    python scripts/quality_gate.py lint         # Lint only
    python scripts/quality_gate.py test         # Tests only
    python scripts/quality_gate.py lint --fix   # Lint with auto-fix
    python scripts/quality_gate.py test -k X    # Pass args to pytest
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPONENT_DIR = PROJECT_ROOT / "custom_components" / "netatmo"
TESTS_DIR = PROJECT_ROOT / "tests"

# ANSI colors for terminal output
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def run(cmd: list[str], label: str, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
    print(f"\n{CYAN}{BOLD}{'=' * 60}{RESET}")
    print(f"{CYAN}{BOLD}  {label}{RESET}")
    print(f"{CYAN}{BOLD}{'=' * 60}{RESET}")
    print(f"  $ {' '.join(cmd)}\n")

    result = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        capture_output=capture,
        text=True,
    )

    output = result.stdout or ""
    errors = result.stderr or ""

    if output:
        print(output)
    if errors:
        # Filter noisy HA debug logs from test stderr
        for line in errors.splitlines():
            if not line.startswith(("DEBUG:", "INFO:", "WARNING:aiohttp")):
                print(line, file=sys.stderr)

    return result


def section_header(title: str) -> None:
    """Print a section header."""
    print(f"\n{BOLD}{'#' * 60}")
    print(f"  {title}")
    print(f"{'#' * 60}{RESET}")


def lint(extra_args: list[str]) -> int:
    """Run ruff linter on component and tests."""
    section_header("LINT: ruff check (HA rules)")

    fix_flag = "--fix" if "--fix" in extra_args else ""
    targets = [str(COMPONENT_DIR), str(TESTS_DIR)]

    cmd = [sys.executable, "-m", "ruff", "check"]
    if fix_flag:
        cmd.append("--fix")
    cmd += [
        "--output-format=concise",
        *targets,
    ]

    result = run(cmd, "Ruff lint (component + tests)")

    if result.returncode == 0:
        print(f"\n{GREEN}{BOLD}  LINT PASSED{RESET}")
    else:
        print(f"\n{RED}{BOLD}  LINT FAILED{RESET}")
        print(f"\n{YELLOW}To auto-fix:{RESET} python scripts/quality_gate.py lint --fix")

    return result.returncode


def lint_format_check() -> int:
    """Check formatting with ruff format (dry-run)."""
    section_header("FORMAT: ruff format --check")

    cmd = [
        sys.executable, "-m", "ruff", "format", "--check",
        str(COMPONENT_DIR), str(TESTS_DIR),
    ]

    result = run(cmd, "Ruff format check")

    if result.returncode == 0:
        print(f"\n{GREEN}{BOLD}  FORMAT OK{RESET}")
    else:
        print(f"\n{RED}{BOLD}  FORMAT ISSUES{RESET}")
        print(f"\n{YELLOW}To auto-fix:{RESET} ruff format custom_components/netatmo tests")

    return result.returncode


def test(extra_args: list[str]) -> int:
    """Run pytest on the netatmo test suite."""
    section_header("TEST: pytest")

    # Filter out our script flags
    pytest_args = [a for a in extra_args if a != "--fix"]

    cmd = [
        sys.executable, "-m", "pytest",
        str(TESTS_DIR / "components" / "netatmo"),
        "--timeout=30",
        "-q",
        "--tb=short",
        *pytest_args,
    ]

    result = run(cmd, "Pytest (netatmo tests)", capture=False)

    if result.returncode == 0:
        print(f"\n{GREEN}{BOLD}  TESTS PASSED{RESET}")
    else:
        print(f"\n{RED}{BOLD}  TESTS FAILED{RESET}")
        print(f"\n{YELLOW}To see details:{RESET} python scripts/quality_gate.py test -v")
        print(f"{YELLOW}To update snapshots:{RESET} python scripts/quality_gate.py test --snapshot-update")

    return result.returncode


def summary(results: dict[str, int]) -> int:
    """Print final summary and return exit code."""
    section_header("QUALITY GATE SUMMARY")

    all_passed = True
    for name, rc in results.items():
        status = f"{GREEN}PASS{RESET}" if rc == 0 else f"{RED}FAIL{RESET}"
        if rc != 0:
            all_passed = False
        print(f"  {status}  {name}")

    if all_passed:
        print(f"\n{GREEN}{BOLD}  ALL CHECKS PASSED{RESET}\n")
        return 0

    # Structured output for agents: list what needs fixing
    print(f"\n{YELLOW}{BOLD}--- AGENT ACTION ITEMS ---{RESET}")
    if results.get("lint", 0) != 0:
        print("- Run `python scripts/quality_gate.py lint --fix` to auto-fix lint issues")
        print("- Remaining lint issues need manual fixes (see ruff output above)")
    if results.get("format", 0) != 0:
        print("- Run `ruff format custom_components/netatmo tests` to fix formatting")
    if results.get("test", 0) != 0:
        print("- Run `python scripts/quality_gate.py test -v` to see detailed test failures")
        print("- Snapshot mismatches: `python scripts/quality_gate.py test --snapshot-update`")
    print()

    return 1


def main() -> int:
    """Run quality gate checks."""
    args = sys.argv[1:]

    # Parse mode
    mode = "all"
    extra_args: list[str] = []
    if args and args[0] in ("lint", "test"):
        mode = args[0]
        extra_args = args[1:]
    else:
        extra_args = args

    start = time.monotonic()
    results: dict[str, int] = {}

    if mode in ("all", "lint"):
        results["lint"] = lint(extra_args)
        results["format"] = lint_format_check()

    if mode in ("all", "test"):
        results["test"] = test(extra_args)

    elapsed = time.monotonic() - start
    print(f"\n  Total time: {elapsed:.1f}s")

    return summary(results)


if __name__ == "__main__":
    raise SystemExit(main())
