# AGENTS.md

Instructions for AI coding agents (Copilot, Claude Code, Cursor, etc.) working in this repository.

## Project Overview

Custom Home Assistant integration for Netatmo devices, distributed via HACS. This is a fork/beta of the official HA Netatmo component with additional features: multi-home selection, energy entities, Legrand Ecocounter support, and improved API throttling.

The project bundles a modified copy of [pyatmo](https://github.com/tmenguy/pyatmo) (development branch) directly inside the integration, with import paths rewritten from `from pyatmo` to relative imports (`from .pyatmo`).

## Commands

```bash
scripts/setup          # Install dependencies (pip install -r requirements_dev.txt)
scripts/develop        # Start Home Assistant with the custom component in debug mode (port 8123)
scripts/lint           # Run ruff linter with auto-fix (legacy, simple)
scripts/test           # Run the test suite (pytest). Pass extra args: scripts/test -v -k test_sensor
```

### Quality Gate

The quality gate script runs linting (ruff with HA core rules) and tests, with structured output for agents.

```bash
pip install -r requirements_test.txt                        # Install test deps (once)
python scripts/quality_gate.py                              # Run everything (lint + format + tests)
python scripts/quality_gate.py lint                         # Lint only
python scripts/quality_gate.py lint --fix                   # Lint with auto-fix
python scripts/quality_gate.py test                         # Tests only
python scripts/quality_gate.py test -v -k test_sensor       # Run specific tests
python scripts/quality_gate.py test --snapshot-update        # Regenerate snapshots after changes
```

Ruff rules in `pyproject.toml` match HA core's configuration for upstream mergeability. The summary section prints **AGENT ACTION ITEMS** with exact commands to fix issues.

### Testing

Tests live at `tests/components/netatmo/` and mirror HA core's test structure for mergeability. The test infrastructure uses `pytest-homeassistant-custom-component` with shim files at `tests/common.py`, `tests/typing.py`, etc. that re-export under HA core import paths. The root `tests/conftest.py` aliases the bundled pyatmo and custom component modules into `sys.modules` so that HA core test patches resolve correctly.

### Syncing upstream code

- `build.sh` — Pulls the HA integration files from `cgtobi/home-assistant` (branch `netatmo_debug_climate`) and rewrites pyatmo imports to relative
- `import_pyatmo.sh` — Pulls pyatmo from `tmenguy/pyatmo` (branch `development`) and rewrites imports to relative
- `import_tests.sh` — Copies netatmo tests from a local HA core checkout (default: `../core`)

Both `build.sh` and `import_pyatmo.sh` use `gsed` (GNU sed on macOS) to transform import paths. After running either script, verify that all `from pyatmo` imports were correctly rewritten to relative imports (`from .pyatmo` or `from .`).

## Architecture

### Data Flow

```
Netatmo Cloud API
       │
   [pyatmo]          ← bundled library: custom_components/netatmo/pyatmo/
       │
 [data_handler.py]   ← central orchestrator: publishers, rate limiting, subscriptions
       │
 [entity platforms]  ← sensor.py, climate.py, camera.py, binary_sensor.py, etc.
       │
  Home Assistant
```

### Key Components

- **`data_handler.py`** — The core of the integration. `NetatmoDataHandler` manages all data flow. `NetatmoPublisher` implements a pub/sub model where entity platforms subscribe to data topics (ACCOUNT, HOME, WEATHER, ENERGY_MEASURE, etc.). Implements Netatmo API rate limiting with rolling buffers (20 req/hour, 2 req/10s per user) and dynamic rate adjustment.

- **`__init__.py`** — Integration setup/teardown, OAuth2 auth initialization, webhook registration, cloud subscription management.

- **`api.py`** — `AsyncConfigEntryNetatmoAuth` bridges HA's OAuth2 session with pyatmo's auth abstraction. Filters scopes for cloud vs. local auth.

- **`entity.py`** — `NetatmoBaseEntity` base class with HA dispatcher signal subscriptions for real-time updates.

- **`config_flow.py`** — OAuth2 config flow + options flow for selecting which Netatmo homes to sync.

### Bundled pyatmo Library

Located at `custom_components/netatmo/pyatmo/`. This is NOT an installed package — it's vendored source with rewritten imports.

Key structure:
- `account.py` — `AsyncAccount`: top-level Netatmo account with homes/modules
- `home.py` — `Home` object: rooms, modules, schedules, persons
- `modules/` — Device-type implementations: `netatmo.py` (cameras, thermostats, weather), `legrand.py` (ecocounter), `bticino.py`, `smarther.py`, `somfy.py`
- `modules/base_class.py` — Base `Module` class with energy/power tracking

### Platform Entities

10 HA platforms: sensor, climate, camera, binary_sensor, light, switch, cover, fan, button, select. Each platform file defines entity descriptions and maps pyatmo module data to HA entities.

## Important Patterns

- **Import rewriting**: All pyatmo imports within the integration use relative paths (`from .pyatmo`, `from . import pyatmo`). When adding new files or imports, follow this convention.
- **Publisher/subscriber**: Entities register with `data_handler.subscribe()` for specific data topics. Don't poll directly.
- **Webhook + polling**: Real-time events come via webhooks (with cloudhook support); polling is the fallback.
- **Multi-home filtering**: Disabled homes are filtered at the topology level in `data_handler.py` and `config_flow.py`.

### pyatmo Dual-Mode Compatibility

The integration must work with pyatmo as **either** a bundled directory (`custom_components/netatmo/pyatmo/`) **or** a pip-installed package. This is critical for upstream mergeability — the goal is to eventually merge this custom component into official HA core, where pyatmo is an installed dependency.

**Rules:**
- **Integration code**: All files use plain `import pyatmo` / `from pyatmo import ...`. Only `__init__.py` has the `try/except/else` bridge that registers the bundled copy into `sys.modules`.
- **Test code**: Tests must **not** assume pyatmo is bundled. Use standard patch paths like `patch("pyatmo.home.Home.method")` — never `patch("custom_components.netatmo.pyatmo.home.Home.method")`. The root `tests/conftest.py` handles module aliasing so both paths resolve to the same objects.
- **Test modifications**: Keep changes to HA core test files minimal. When importing tests from upstream, copy them verbatim. If a test needs adaptation, prefer fixing the test infrastructure (conftest.py, shims) over modifying the test file itself.
- **Never import from `custom_components.netatmo.pyatmo`** in integration code or tests — always use the top-level `pyatmo` namespace.

## Home Assistant Integration Guidelines

This integration follows the conventions of the Home Assistant core project. The rules below are derived from the HA core coding guidelines and integration quality scale.

### General Rules

- **Polling intervals are NOT user-configurable.** Never add `scan_interval`, `update_interval`, or polling frequency options to config flows or config entries.
- **Do NOT allow users to set config entry names in config flows.** Names are auto-generated or can be customized later in UI. Exception: helper integrations may allow custom names.
- **Quality Scale**: When looking for examples of HA best practices, prefer integrations with platinum or gold `quality_scale` in their `manifest.json`.
- **Git commits during review**: Do not amend, squash, or rebase commits after review has started — reviewers need to see what changed since their last review.

### Testing

- All test function parameters must have type annotations.
- Prefer concrete types (`HomeAssistant`, `MockConfigEntry`, etc.) over `Any`.
- Tests should avoid interacting with or mocking internal integration details.

### Diagnostics (`diagnostics.py`)

- Implement diagnostic data collection.
- Never expose passwords, tokens, or sensitive coordinates.

### Repairs (`repairs.py`)

- All repair issues must be actionable for end users.
- Clearly explain what is happening, why it matters, and provide exact steps to resolve.
- Severity guidelines: `CRITICAL` reserved for extreme scenarios only, `ERROR` for immediate attention, `WARNING` for future potential breakage.
- Only create issues for problems users can potentially resolve.
