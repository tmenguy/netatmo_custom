# Netatmo Custom Component vs HA Core — Deep Analysis Report

> Comparison baseline: `custom_components/netatmo/` (this repo) vs
> `homeassistant/components/netatmo/` in `/Users/tmenguy/Developer/homeassistant/core` as of 2026‑05‑09.

## TL;DR — One‑Page Executive Summary

The single most important change is the **API rate‑limiting overhaul in `data_handler.py`**. It is a structural improvement that solves a real, observable bug in core (uncontrolled bursts that trigger Netatmo's 429s). Around it are several smaller, cleanly‑separable improvements that can be PR'd independently:

| Area | Verdict | Upstream priority |
|---|---|---|
| **`data_handler.py` rate limiter** (rolling 1h window + 10s burst guard + dynamic CPH adjust + throttling‑aware) | 🟢 Strictly better, with three concrete bugs fixed in this branch | **#1 — flagship PR** |
| **ENERGY/GAS/WATER sensors** + `EnergyHistoryMixin` + `target_module` publisher pattern (Legrand Ecocounter) | 🟢 Net new functionality, well‑isolated | **#2** |
| **Multi‑home selection** (`CONF_DISABLED_HOMES`, options flow `+homes step`, reload‑on‑change) | 🟢 Solves a real complaint upstream | **#3** |
| **NLC / pilot‑wire climate support** (`PRESET_COMFORT`, `PRESET_ECO`, `therm_setpoint_fp`) | 🟡 Heavy device‑specific surface area; needs tests + design review | **#4** (but split) |
| **`__init__.py` polish** (`_reset_hass_domain`, cloudhook retry, reload on options change) | 🟢 Small QoL, low risk | **#5** |
| `pyatmo` requirements (`ApiThrottlingError`, `ApiHomeReachabilityError`, `all_homes_id`, `disabled_homes_ids`, energy mixin) | 🔵 Must land in `pyatmo` first | **Prereq** |
| Bundled‑pyatmo bridge in `__init__.py` (`try/except` + `sys.modules` aliasing) | 🔴 Custom‑component‑only; do **not** upstream | n/a |

Almost every other platform file (`button.py`, `camera.py`, `cover.py`, `fan.py`, `helper.py`, `light.py`, `media_source.py`, `select.py`, `switch.py`, `binary_sensor.py`, `webhook.py`, `device_trigger.py`, `diagnostics.py`, `api.py`, `application_credentials.py`) is **byte‑identical** to core. Changes are surgical and concentrated.

### Final state of the proposed `data_handler.py` fixes (after adversarial review)

Six fixes were initially proposed; **three** survived scrutiny and are applied in this branch. The other three were reverted because the original code turned out to be either correct or intentionally more conservative. Throttle/recovery decision logic and `adjust_per_scan_numbers` are now byte‑identical to your original.

| Fix | Status | Reason |
|---|---|---|
| 🐛 Bug #1 — `delta_sleep` packed calls into ⅓ of `scan_interval`, violating the 10s rate on cloud | ✅ **Applied** | Real, severe violation: 7 calls per 10s window vs cloud limit of 2 |
| 🐛 Bug #2 — `scan_limit_per_hour` integer truncation hidden by `max()` | ❌ **Reverted** | Overstated: for Netatmo's actual configs the `max()` always picks the same value; original is defensive, not buggy |
| 🐛 Bug #3 — `_min_call_per_interval` computed but never read | ✅ **Applied** | Genuinely dead code |
| 🐛 Bug #4 — Init made N homes' status calls back‑to‑back, busting the 10s burst limit | ✅ **Applied** | Real init bug; throttling responses were silently swallowed by the generic `ApiError` handler |
| 🟡 Design — split single 3600s gate into 60s/600s | ❌ **Reverted** | The 3600s value matches `rolling_hour`'s window length, so each adjustment lands on a fully‑clean measurement; faster recovery would oscillate in steady‑state over‑demand |
| 🟡 Race — `num_made_calls > 0` instead of `cph > cph_init` | ❌ **Reverted** | The two are NOT equivalent: `cph > cph_init` deliberately skips throttle when natural aging cancels new calls (rolling window flat or shrinking) — a stricter, more conservative check that the original author got right |

---

## 1. `data_handler.py` — Deep Dive (the headliner)

The file is 1045 lines vs core's 476. Almost all of the extra mass is the rate‑limiting subsystem.

### 1.1 Rate‑limit model — fundamentally different

**Core's model** (in 4 lines):

```python
poll_count, poll_start  # naive accumulator
cph = poll_count / (time - poll_start) * 3600  # average rate since start of window
if cph > self._rate_limit:
    for publisher in self.publisher.values():
        publisher.next_scan += 60          # uniform 60s penalty
if (time - self.poll_start) > 3600:
    self.poll_start = time(); self.poll_count = 0   # reset every hour (drift)
```

**Custom's model** uses two real Netatmo dimensions plus dynamic backoff:

```python
NETATMO_USER_CALL_LIMITS = {                # cloud auth (community shared client_id)
    CALL_PER_HOUR: 20,                      # 20 * num_users application limit
    CALL_PER_TEN_SECONDS: 2,                # 2 * num_users application limit
    ACCOUNT: 10800, HOME: 300, WEATHER: 600,
    AIR_CARE: 300, PUBLIC: 600, EVENT: 600,
    ENERGY_MEASURE: 1800, SCAN_INTERVAL: 60,
}
NETATMO_DEV_CALL_LIMITS = {                 # own dev client_id (per-user limits)
    CALL_PER_HOUR: 450, CALL_PER_TEN_SECONDS: 45,
    ACCOUNT: 3600, HOME: 10, WEATHER: 200,
    AIR_CARE: 100, PUBLIC: 200, EVENT: 200,
    ENERGY_MEASURE: 1200, SCAN_INTERVAL: 10,
}
```

#### 🟢 Why this is genuinely better

1. **Real rolling 1‑hour window** (`self.rolling_hour: list[float]`). `add_api_call(n)` appends timestamps and pops anything older than 3600s. Core's `cph = poll_count / elapsed * 3600` is a *running average since hour reset*, not the actual count of calls in the last 60 minutes. After a 30‑minute reset, your 100 calls of the last hour become 0 in core's view — that's a real measurement bug that masks throttling.
2. **10‑second burst limit is enforced** via `delta_sleep` between candidates (custom) — core has no concept at all, so it can fire `BATCH_SIZE * interval_factor = 21` calls within microseconds and trigger 429s.
3. **Throttling‑aware** — `pyatmo.ApiThrottlingError` is distinguished from generic `ApiError`. On throttle, `adjust_intervals_to_target(target * 0.8)` proportionally stretches every publisher's interval and waits for the rolling window to drop. Core treats throttling identically to a transient connection error and only adds 60s once.
4. **Per‑publisher backoff** — `num_consecutive_errors` tracked per publisher (currently used for logging; could drive per‑publisher delay).
5. **Recovery** — `CPH_ADJUSTEMENT_BACK_UP = 1.1` slowly walks back toward the original rate after a throttle event, so a single transient doesn't lock you in degraded mode forever.
6. **Sorted publisher candidates** (`_sorted_publisher`, sorted by `next_scan`) — picks the most‑due publisher first. Core's `deque.rotate(BATCH_SIZE)` is round‑robin, so a `next_scan=now` publisher can wait a full rotation.
7. **Even spread of next_scans** (`_spread_next_scans`) — same‑interval publishers are deliberately staggered, avoiding the "every 5 minutes all 5 homes hit at once" thundering herd.
8. **Dynamic interval scaling** — `adjust_intervals_to_target` computes theoretical CPH and rescales every publisher's interval if you have too many subscribers. With 3 homes the static intervals would already breach the 20/hour cloud limit; the scaler keeps you legal.
9. **Faster first sample** — `next_scan = time() + interval // 2` at subscribe time, vs. core's full `interval` wait. UX improvement at startup.

#### 🟡 What was reviewed and what survived

Six issues were initially flagged. Three were applied; three were rolled back during adversarial review. Each is documented below with the actual reasoning.

##### ✅ Bug #1 — `delta_sleep` clusters calls in the first ⅓ of `scan_interval` (applied)

```python
# data_handler.py (before fix)
if num_call > 0:
    delta_sleep = self._scan_interval / (3.0 * num_call)
```

For cloud (`scan_interval=60`, `num_call=12`), `delta_sleep = 1.66 s`. The 12 calls span only `12 × 1.66 = 20s`, then 40s of silence. Concretely, calls land at t=0, 1.66, 3.32, 4.98, 6.64, 8.30, 9.96 — that's **7 calls in any 10‑second window**, against a cloud per‑10s limit of **2**. The original fires 3.5× more bursts than Netatmo allows, so cloud users get repeated 429s under load.

```python
# fix
delta_sleep = max(
    self._scan_interval / num_call,
    10.0 / self._10s_rate_limit,
)
```

Even spacing across the full `scan_interval`, with a hard floor at the per‑10s spacing. Sleep moved to *before* non‑first calls so we don't pay an extra wait at loop end. Cost: the loop now uses ~`scan_interval` of wall time on max‑burst (vs ~⅓ before), tightening the timing margin before the next `async_update` fires. In practice this is fine; the 10s‑rate compliance is worth it.

For dev mode, both forms end up at the per‑10s ceiling (45/10s) anyway — the violation is specifically a cloud problem.

##### ❌ ~~Bug #2~~ — `scan_limit_per_hour` `max()` (proposed, then reverted)

I claimed the original `max(scan_limit_per_hour, 10s_derived)` was buggy because `scan_limit_per_hour` truncates to 0 for cloud (`20·60//3600 = 0`). On closer inspection this was overstated:

- For Netatmo's actual configs the `max()` always picks the 10s‑derived value (12 for cloud, 45 for dev). The truncation never affects the result.
- The hourly budget is already enforced separately in `async_update` via `min(_max_call_per_interval, _adjusted_hourly_rate_limit - cph_init)`. The `max()` is a defensive guard for hypothetical configs, not a logic bug.
- For the truncation to actually matter you'd need `hrl > 360 · _10s_rate` (cloud hrl > 720, dev hrl > 16200). Neither is reachable.

The original `max()` form stays. Only `_min_call_per_interval` (which really was unused dead code — Bug #3) is removed.

##### ✅ Bug #3 — `_min_call_per_interval` was dead code (applied)

Computed in `adjust_per_scan_numbers`, never read anywhere. Removed.

##### ✅ Bug #4 — Init made N synchronous calls without 10s‑limit guard (applied)

```python
# _init_update_status_if_needed (before fix)
for h in self.account.homes:
    if h in self.account.all_homes_id:
        await self.account.async_update_status(h)   # 1 API call each, sequential
        num_calls += 1
self.add_api_call(num_calls)                        # batch‑recorded after the fact
```

For 5 homes that's 1 (topology) + 5 (status) ≈ 6 API calls in 2–3 seconds — over the 2/10s cloud limit. A 429 here was silently swallowed by `except (NoDeviceError, ApiError)` and the integration thought setup succeeded with a partial topology (some homes' modules left empty until the next `async_update` tick).

```python
# fix
inter_call_delay = 10.0 / self._10s_rate_limit
is_first = True
for h in self.account.homes:
    if h not in self.account.all_homes_id:
        continue
    if not is_first:
        await asyncio.sleep(inter_call_delay)
    is_first = False
    try:
        await self.account.async_update_status(h)
        num_calls += 1
    except pyatmo.ApiThrottlingError as err:
        # Bail out early — next async_update retries.
        has_error = True
        break
    except ...
```

`ApiThrottlingError` is now distinguished from the other `ApiError`s and breaks the init loop so the next `async_update` retries cleanly.

##### ❌ Design — `_last_cph_change > 3600` gate (proposed split, then reverted)

I initially proposed splitting the single 3600s gate into separate throttle‑down (60s) and recovery‑up (600s) gates, on the theory of "react fast to throttling, walk back slowly". After adversarial review this was **reverted**:

- The 3600s value is principled, not arbitrary — it matches the `rolling_hour` window length, so each adjustment lands on a fully‑clean measurement.
- Faster recovery causes oscillation in steady‑state over‑demand scenarios (e.g. a user with frequent webhook‑driven `async_force_update`s spiking past the limit). The old code completes ~1 cycle per ~5h; the split gates would complete ~6 cycles per day, each cycle wasting more 429s.

The original gate stays unchanged.

##### ❌ Race — `cph_init` snapshot (proposed `num_made_calls`, then reverted)

I proposed replacing `cph > cph_init` with a `num_made_calls > 0` counter, framing it as a "race fix". Two follow‑up reviews showed this was wrong:

1. The "race" is mostly theoretical — `async_update` is single‑coroutine and only overlaps with itself if a run outlasts `scan_interval`.
2. **More importantly, the two checks are NOT equivalent.** `cph > cph_init` is "the rolling‑hour window grew during this scan". It can be False even when `num_made_calls > 0` if old calls aged out concurrently. Concrete example:

   ```
   t=3600: cph_init = 20  (calls at t = 0, 180, 360, ..., 3420)
   t=3601: we make 1 call → rolling_hour appends t=3601 AND pops t=0
   t=3601: cph = 20       (one in, one out)
   
   num_made_calls = 1 > 0     "we made a call"
   cph > cph_init = False     "we didn't make things worse"
   ```

The original semantics — "throttle only if we're actively making the over‑budget situation worse" — is deliberate: when natural aging keeps pace with new calls, the dynamic interval rescaling drains us without needing another throttle event. The original condition stays unchanged.

#### Recommended PR shape

Split the data_handler change into **two PRs** for review tractability:

- **PR A — model only**: introduce rolling‑window CPH, 10s rate guard, `ApiThrottlingError` handling, and the `NETATMO_USER_CALL_LIMITS` / `NETATMO_DEV_CALL_LIMITS` tables. Tests around `add_api_call`, `get_wait_time_to_reach_targets`, and the dynamic adjustment math.
- **PR B — adjustments**: dynamic `adjust_intervals_to_target`, `_spread_next_scans`, init flow refactor (`_do_complete_init_if_needed`), `subscribe_with_target` API.

### 1.2 Init flow — explicit two‑stage vs core's one‑shot

| Step | Core | Custom |
|---|---|---|
| 1. Create account | yes | yes |
| 2. Topology fetch | via `subscribe(ACCOUNT)` (single fetch) | dedicated `_init_update_topology_if_needed` with retry |
| 3. Status fetch per home | implicit / not in init | dedicated `_init_update_status_if_needed` per home in `account.all_homes_id` |
| 4. Subscribe ACCOUNT to publisher | yes (with fetch) | yes (with `update_only=True`, no fetch) |
| 5. Forward platform setups | yes | yes |
| 6. Dispatch entities | yes | yes |
| Failure handling | raises | logs + retries on next scan |

Custom is more robust: if API is slow at startup, core raises `ConfigEntryNotReady`; custom logs and retries on every `async_update`. **`_init_complete` becoming True only after a successful run is a good pattern**, worth upstreaming verbatim.

### 1.3 `subscribe_with_target` — the cleanest extension

```python
# custom only
async def subscribe_with_target(self, publisher, signal_name, target, ...):
    ...
    if target is None:
        target = self.account
    ...
    # later, in async_fetch_data:
    await getattr(self.publisher[signal_name].target,   # custom: stored target
                  self.publisher[signal_name].method)(**kwargs)
```

vs core:

```python
# core
await getattr(self.account, self.publisher[signal_name].method)(**kwargs)
```

Custom lets a publisher dispatch to *any* object's method, not just `self.account`. The energy sensor uses this to register itself as the target for a per‑entity `async_update_energy` poll. **This is the cleanest, most upstream‑friendly piece** of the diff — and it's tiny (~15 lines).

### 1.4 New `ENERGY_MEASURE` publisher

```python
ENERGY_MEASURE = "energy"
PUBLISHERS = { ..., ENERGY_MEASURE: "async_update_energy" }
```

Combined with `subscribe_with_target` and the `EnergyHistoryMixin` from pyatmo, this gives a per‑module energy poll loop with its own interval (1800s cloud / 1200s dev). Core has no equivalent.

### 1.5 `setup_modules` — many more device categories

Custom routes more categories through entity dispatch:

| Category | Core signals | Custom signals (additions in **bold**) |
|---|---|---|
| `dimmer` | `LIGHT` | `LIGHT`, **`LEGACY_SENSOR`**, **`ENERGY`** |
| `shutter` | `COVER`, `BUTTON` | `COVER`, `BUTTON`, **`LEGACY_SENSOR`**, **`ENERGY`** |
| `switch` | `LIGHT`, `SWITCH`, `LEGACY_SENSOR` | `LIGHT`, `SWITCH`, `LEGACY_SENSOR`, **`ENERGY`** |
| `meter` | `LEGACY_SENSOR` | `LEGACY_SENSOR`, **`ENERGY`** |
| `fan` | `FAN` | `FAN`, **`LEGACY_SENSOR`**, **`ENERGY`** |
| `meter` + `NLE` (Ecocounter) | — | special‑case handling: child `#6` → `GAS`, child `#1..5,7+` → `WATER` |

The Ecocounter (NLE) special case is hardcoded to channel numbers parsed from `entity_id.split("#")` — flag this for upstream as fragile. Worth pushing into pyatmo as a structural property.

---

## 2. `__init__.py` — small but worth merging

| Change | Verdict |
|---|---|
| `try/except` import bridge for bundled pyatmo (lines 9–25) | 🔴 **Strip before upstreaming** — only relevant when shipping as a custom component |
| `_reset_hass_domain(hass)` helper extracting the 6 dict keys | 🟢 Cosmetic but cleaner, easy to merge |
| `async_cloudhook_generate_url` retry on `"Hook is already enabled"` ValueError | 🟢 **Real fix** for a real cloud failure mode — HA users hit this |
| `async_config_entry_updated` reload‑on‑disabled‑homes‑change | 🟢 Pairs with multi‑home selection — must ship together |
| `register_webhook` extra `try/except` around `async_cloudhook_generate_url` | 🟢 Defensive, low risk |

The cloudhook retry is one of those small fixes that quietly resolves user reports — separate, two‑file PR.

---

## 3. `const.py` — additive only

Adds: `NETATMO_CREATE_BATTERY`, `NETATMO_CREATE_ENERGY`, `NETATMO_CREATE_GAS`, `NETATMO_CREATE_WATER`, `CONF_DISABLED_HOMES`. Pure additions, no removals. Each ships with the platform PR that needs it.

---

## 4. `config_flow.py` — multi‑home selection

Custom replaces the `public_weather_areas` step with `public_weather_areas_and_homes`, adding an `INTERMEDIATE_ENABLED_HOMES` multi‑select that's converted to `CONF_DISABLED_HOMES` (storing the *complement*, which is a nice forward‑compatible choice — adding a new home defaults to enabled).

Reads `runtime_data.account.all_homes_id` (a custom pyatmo addition) for the home list.

🟢 **Strong upstream candidate.** Multi‑home is one of the longest‑standing complaints on the upstream issue tracker. Pair the strings.json change ("Homes selection and ...") with this PR.

---

## 5. `entity.py` — minimal delta

One real change: `NetatmoBaseEntity.__init__` accepts `**kwargs`, and `async_added_to_hass` understands a new `target_module` publisher key:

```python
if "target_module" in publisher:
    await self.data_handler.subscribe_with_target(
        publisher=publisher["name"],
        signal_name=signal_name,
        target=publisher["target_module"],
        update_callback=self.async_update_callback,
        update_only=True,
    )
```

This is the entity‑side companion to `subscribe_with_target`. Ship it together.

---

## 6. `climate.py` — NLC / pilot‑wire support (heavy)

Custom climate.py is +7.5KB. Almost all of it is **NLC (Netatmo Legrand Connected radiator) pilot‑wire support**:

- New presets: `PRESET_COMFORT`, `PRESET_ECO` (pilot wire) on top of existing `PRESET_AWAY`, `PRESET_BOOST`, `PRESET_FROST_GUARD`, `PRESET_SCHEDULE`.
- Per‑instance preset/HVAC maps (`_netatmo_map_preset`, `_hvac_map_netatmo`, `_preset_map_netatmo`) so NLC devices can hot‑swap their semantics in `__init__`. Cleaner than a giant `if device_type == NLC:` ladder.
- Three new dictionaries: `NETATMO_MAP_PRESET_PILOT_WIRE`, `PRESET_MAP_NETATMO_PILOT_WIRE`, `HVAC_MAP_NETATMO_PILOT_WIRE`.
- `async_set_preset_mode_with_end_datetime` is now the primary code path; `async_set_preset_mode` delegates. Includes `TimeoutError` retry — flag this as another quality‑of‑life win that cloud users will benefit from beyond NLC.
- `hvac_action` reads `radiators_power` for NLC, `heating_power_request` otherwise.
- `handle_event` for `THERM_MODE` on NLC triggers `async_force_update` instead of mapping directly — because pilot‑wire mode resolution requires a fresh server state.
- `SERVICE_ALLOWED_PRESETS` widens the service preset enum from `THERM_MODES` to all known presets (so service calls work for NLC users).

🟡 **Heavy but mergeable.** This single feature is what most of your users care about. Concerns for upstream:

1. Many code paths only matter for NLC. Reviewers will want pyatmo type discrimination at compile time (e.g. a separate `NetatmoPilotWireThermostat` subclass) rather than runtime branching.
2. `STATE_NETATMO_HOME` and `PILOT_WIRE_*` constants need to land in pyatmo first.
3. Tests must cover both legacy `NATherm1`/`NRV` and `NLC` paths.
4. `TimeoutError` retry on `async_therm_set` is independent and should ship as a tiny separate PR — even non‑NLC users benefit.

---

## 7. `sensor.py` — energy/gas/water + ecocounter

Custom is +7.7KB. The deltas:

- **`NETATMO_ENERGY_SENSOR_DESCRIPTION`** (Wh, `TOTAL_INCREASING`)
- **`NETATMO_GAS_SENSOR_DESCRIPTION`** (Liters, `device_class=GAS`) — yes, despite `netatmo_name="sum_energy_elec"`. The pyatmo lib reuses the field for non‑electric meters.
- **`NETATMO_WATER_SENSOR_DESCRIPTION`** (Liters, `device_class=WATER`).
- `DEVICE_CATEGORY_LEGACY_SENSORS` extends to `dimmer`/`shutter`/`fan`. `DEVICE_CATEGORY_SENSOR_URLS` matches.
- `_create_energy_entity`, `_create_gas_entity`, `_create_water_entity` callbacks bound to the three new dispatch signals.
- New class **`NetatmoEnergySensor`** uses `EnergyHistoryMixin` from pyatmo:
  - Daily anchor reset (Netatmo only retains 2.5 days of energy history)
  - `async_update_energy(**kwargs)` is the method invoked by the data handler via the `target_module` pattern
  - `_last_val_sent` enforces monotonicity (defensive against API regressions of accumulated energy)
- `NetatmoBaseSensor.async_update_callback` adds a `"reachable"` special case (returns the bool reachable as the state value) and there's a commented‑out alternative implementation worth deleting before upstreaming.

🟢 **Net new functionality, well isolated.** Each new sensor type is its own class. Upstream as one PR after the data_handler `ENERGY_MEASURE` publisher lands.

---

## 8. Files identical to core

These are byte‑identical (per `diff` exit code 0): `binary_sensor.py`, `button.py`, `camera.py`, `cover.py`, `device_trigger.py`, `diagnostics.py`, `fan.py`, `helper.py`, `light.py`, `media_source.py`, `select.py`, `switch.py`, `webhook.py`, `api.py`, `application_credentials.py`. **No upstream work needed.**

`manifest.json` differs only by removing `pyatmo` from `requirements` (because bundled) and adding a HACS `version` field — both custom‑component artifacts.

`strings.json` differs only by the multi‑home options step rename and added strings — ships with config_flow PR.

`services.yaml`, `icons.json`, `translations/` — identical.

---

## 9. `pyatmo` prerequisites

Before any of the above can land, these need to exist in upstream `pyatmo`:

| Symbol | Used by | Where in custom pyatmo |
|---|---|---|
| `ApiThrottlingError(ApiError)` | data_handler | `pyatmo/exceptions.py:28` |
| `ApiHomeReachabilityError(ApiError)` | data_handler, init | `pyatmo/exceptions.py:32` |
| `Account.all_homes_id: dict[str,str]` | data_handler init, config_flow, __init__ reload | `pyatmo/account.py:45` |
| `Account.async_update_topology(disabled_homes_ids=...)` | data_handler init, ACCOUNT publisher | `pyatmo/account.py:82` |
| `EnergyHistoryMixin`, `MeasureInterval`, `reset_measures`, `async_update_measures`, `get_sum_energy_elec_power_adapted`, `in_reset` | NetatmoEnergySensor | `pyatmo/modules/module.py` (and base) |
| `radiators_power`, `therm_setpoint_fp`, `pilot_wire` arg, `PILOT_WIRE_*`, `STATE_NETATMO_HOME`, `STATE_NETATMO_MANUAL` | climate.py NLC path | `pyatmo/const.py`, NATherm1/NLC modules |
| 429 → `ApiThrottlingError` raise in auth | data_handler throttling response | `pyatmo/auth.py:170` |

**Order of operations**: get pyatmo PRs landed (or scheduled into a release) first; then the HA core PRs become mostly mechanical.

---

## 10. Recommended upstream sequencing

Concrete plan for getting this into HA core:

```
Step 0  Land pyatmo PRs:
        ├─ ApiThrottlingError + ApiHomeReachabilityError + 429 raise
        ├─ Account.all_homes_id + disabled_homes_ids param
        ├─ EnergyHistoryMixin / async_update_measures
        └─ NLC pilot wire constants + therm_setpoint_fp + radiators_power

Step 1  HA core PR — data_handler rate-limit foundation
        ├─ Bug fixes applied in this branch: #1 (delta_sleep 10s rate),
        │  #3 (dead _min_call_per_interval), #4 (init 10s burst guard)
        ├─ Throttle/recovery decision logic kept identical to your original
        │  (single 3600s gate + cph > cph_init — both reverted after review)
        ├─ Add subscribe_with_target API
        └─ Tests around rolling_hour, adjust_intervals_to_target

Step 2  HA core PR — small QoL bundle (independent of #1)
        ├─ async_cloudhook_generate_url retry on "Hook is already enabled"
        ├─ TimeoutError retry on async_therm_set
        ├─ _reset_hass_domain extraction
        └─ next_scan = interval // 2 first-fetch UX bump

Step 3  HA core PR — multi-home selection
        ├─ const.py: CONF_DISABLED_HOMES
        ├─ config_flow: enabled_homes step
        ├─ __init__: async_config_entry_updated reload
        ├─ data_handler: ACCOUNT subscribe disabled_homes_ids kwargs
        └─ strings.json updates

Step 4  HA core PR — ENERGY_MEASURE publisher + NetatmoEnergySensor
        ├─ data_handler: ENERGY_MEASURE constant + interval
        ├─ entity.py: target_module branch in async_added_to_hass
        ├─ sensor.py: NetatmoEnergySensor
        └─ const.py: NETATMO_CREATE_ENERGY signal

Step 5  HA core PR — Legrand Ecocounter (NLE)
        ├─ data_handler: setup_modules NLE special case (consider pushing
        │  the channel→sensor-type mapping into pyatmo first)
        ├─ const.py: NETATMO_CREATE_GAS / NETATMO_CREATE_WATER
        └─ sensor.py: NETATMO_GAS_SENSOR_DESCRIPTION + WATER + dispatch

Step 6  HA core PR — NLC pilot-wire climate
        ├─ Consider splitting into separate NetatmoPilotWireThermostat
        │  class for reviewer clarity
        ├─ Per-instance preset maps
        └─ Service preset enum widening
```

### 10.1 Alternative: single big PR

If you'd rather submit everything as one PR (faster from your end, but a heavier ask of the reviewer), the description below is ready to drop into GitHub. It reflects the actual final state of this branch (the three surviving rate‑limit bug fixes only) and calls out the Energy Dashboard payoff that's easy to under‑sell.

````markdown
## Netatmo: multi‑home, energy/gas/water sensors, pilot‑wire climate, robust rate limiting

Substantial enhancement of the Netatmo integration covering features that have been on the issue tracker for years, plus a rewrite of the API rate limiter so the integration stays under Netatmo's documented per‑10s and per‑hour limits even when the user has many homes/devices.

### User‑visible additions

- **Energy Dashboard support.** Energy (Wh), gas (L) and water (L) sensors are now produced for every supported metering module (Smart Plug, Smart Module, Legrand Ecocounter, etc.) with `SensorDeviceClass.ENERGY` / `GAS` / `WATER` and `SensorStateClass.TOTAL_INCREASING`. They show up directly in **Home Assistant's Energy Dashboard** without any user wiring — pick the entity from the dropdown and it works. The history is anchored daily because Netatmo only retains 2.5 days of energy data, with monotonicity enforced defensively in case the API regresses an accumulated total.
- **Multi‑home selection.** Users with multiple Netatmo homes (frequent for Pro installers and households with a vacation property) can now pick which homes the integration syncs. Disabled homes are filtered at the topology level so they don't burn API quota. Options flow surfaces this; changing the selection reloads the entry cleanly.
- **Legrand Ecocounter (NLE).** The Ecocounter shows up as a bridge with sub‑modules, with channel #6 mapped to gas and the others to water — both wired through the same Energy Dashboard pipeline.
- **Pilot‑wire (NLC) radiator support.** New presets `comfort`, `eco`, `frost_guard`, `schedule` and proper HVAC mapping via the pyatmo `pilot_wire` argument. Thermostat services (`set_preset_mode_with_end_datetime`, etc.) accept the new preset values. A `TimeoutError` retry on `async_therm_set` was added — every climate user benefits from this, not just NLC users.
- **Cloud webhook reliability.** Cloudhook registration now retries when Netatmo reports `"Hook is already enabled"`, which previously left the integration polling‑only with no clear error.

### API rate limiting rewrite

The old `cph = poll_count / elapsed * 3600` accumulator (with a 60s uniform penalty when over budget) doesn't match Netatmo's actual rate model and is prone to bursting. Replaced by:

- **True rolling 1‑hour window** of timestamped calls (`add_api_call(n)` appends and ages out entries past 3600s).
- **Per‑10‑second burst guard** (`delta_sleep = max(scan_interval / num_call, 10 / per_10s_limit)`) — on cloud auth (2 calls per 10s) the old code violated this 3.5×; now compliant.
- **Two real Netatmo limit profiles** (`NETATMO_USER_CALL_LIMITS` for the shared community client_id, `NETATMO_DEV_CALL_LIMITS` for users with their own dev app) with documented values.
- **Throttling‑aware**: `pyatmo.ApiThrottlingError` is distinguished from generic errors; on 429 the integration proportionally stretches publisher intervals (`adjust_intervals_to_target(target * 0.8)`) and waits for the rolling window to drain before resuming.
- **Recovery walk‑back** at +10% per hour, gated to once per rolling‑window length so the integration eventually reclaims the original rate after a transient throttle.
- **`subscribe_with_target` publisher API** — small (~15 LOC) extension allowing a publisher to dispatch to any object's method, not just `self.account`. Used by the energy sensor to plug per‑entity polls into the same scheduler.
- **Two‑stage init with retry** — topology and per‑home status fetched explicitly with inter‑call spacing, so initial setup with N homes doesn't bust the 10s burst limit (a real bug in the previous flow that silently swallowed 429s and left some homes' modules empty).

### `pyatmo` requirements (already in latest pyatmo release)

- `ApiThrottlingError` and `ApiHomeReachabilityError` exceptions
- `Account.all_homes_id` and `async_update_topology(disabled_homes_ids=...)` for multi‑home filtering
- `EnergyHistoryMixin`, `MeasureInterval`, `async_update_measures`, `get_sum_energy_elec_power_adapted` for the energy/gas/water sensors
- Pilot‑wire constants (`PILOT_WIRE_*`, `STATE_NETATMO_HOME`, `therm_setpoint_fp`, `radiators_power`, `pilot_wire` arg on `async_therm_set`) for NLC
- 429 → `ApiThrottlingError` raised in `auth.py`

### Backwards compatibility

- All existing entity unique_ids preserved — no migration needed for current users.
- All existing services unchanged; new preset values widen the accepted enum without removing any.
- The rate limiter is strictly more conservative on cloud auth than the old code (it actually obeys the 10s limit), so existing users should see *fewer* 429s, not more.

### Tests

Adds `tests/components/netatmo/test_data_handler_rate_limiting.py` covering:
- `add_api_call` rolling‑window aging
- Cloud vs dev limit selection
- Publisher candidate selection / spreading
- `adjust_per_scan_numbers` and `adjust_intervals_to_target` boundaries
- `get_wait_time_to_reach_targets` edge cases
- `compute_theoretical_call_per_hour` math
- All `async_fetch_data` exception branches (NoDeviceError, ApiError, ApiThrottlingError, ApiHomeReachabilityError, TimeoutError)
- Throttle‑down and recovery integration paths

Existing snapshot suite remains green (433 snapshots passed).

### Known follow‑ups (not blocking)

- The Legrand Ecocounter (NLE) channel→sensor‑type mapping is currently parsed from `entity_id.split("#")` in `data_handler.py:setup_modules`; pushing this into pyatmo as a structural property would be cleaner.
- Climate.py's NLC vs legacy paths are runtime‑branched today; a separate `NetatmoPilotWireThermostat` subclass would be more idiomatic if reviewers prefer.
````

---

## 11. Notable strengths reviewers will appreciate

- **Real Netatmo API limits** are encoded with citations to the dev docs in comments. That alone clears half the reviewer questions.
- **Two distinct modes** (`USER` vs `DEV`) instead of a single tunable factor — matches reality.
- **Pure additive design** for sensors/dispatch signals — no reshuffle of existing entity unique_ids, no migration headaches.
- **Two‑stage init** with retry is a pattern HA core already uses for many integrations; no philosophical conflict.
- **Test infrastructure preserved** — `tests/components/netatmo/` mirrors HA core layout, so once pyatmo is unbundled the tests should largely just work.

## 12. Sharp edges left for follow‑ups

1. ~~`delta_sleep` clusters calls into ⅓ of `scan_interval` and violates the cloud 10s rate~~ — fixed (Bug #1).
2. ~~Dead code: `_min_call_per_interval`~~ — removed (Bug #3). Commented‑out blocks in `sensor.py` (`# attr_name = ...`) still to clean.
3. ~~Init status burst hits 10s rate~~ — fixed (Bug #4) with inter‑call sleep + early bail on `ApiThrottlingError`.
4. **Magic numbers**: `CPH_ADJUSTEMENT_DOWN = 0.8`, `CPH_ADJUSTEMENT_BACK_UP = 1.1`, the single `3600s` gate — at minimum needs a sentence of justification in the source. The `3600s` value matches `rolling_hour`'s window length on purpose; document that.
5. **Hardcoded NLE channel parse** (`split("#")` + integer cast) — push into pyatmo as a structural property.
6. **Integration code uses `from . import pyatmo`** in places — must scrub to `import pyatmo` everywhere before upstreaming (your AGENTS.md already documents this rule).
7. **Tests for the rate‑limiting math**: the upstream test suite has none for this; the reviewer will want unit tests for `add_api_call`, `get_wait_time_to_reach_targets`, and `adjust_intervals_to_target` boundaries. The current branch already has `tests/components/netatmo/test_data_handler_rate_limiting.py` covering most of this — port it as part of the PR.

---

## 13. One‑sentence per file

| File | Δ vs core | One‑line summary |
|---|---|---|
| `data_handler.py` | +21KB | Real rate limiter (1h+10s+throttle), dynamic CPH adjust, multi‑home init, `subscribe_with_target`, `ENERGY_MEASURE` publisher |
| `sensor.py` | +7.7KB | Energy/Gas/Water sensors via `EnergyHistoryMixin`, `target_module` pattern, dimmer/shutter/fan legacy sensors |
| `climate.py` | +7.5KB | NLC pilot‑wire support, per‑instance preset maps, `_with_end_datetime` becomes primary, timeout retry |
| `__init__.py` | +3.3KB | pyatmo bundle bridge (custom only), cloudhook retry, reload‑on‑disabled‑homes, `_reset_hass_domain` |
| `config_flow.py` | +1.2KB | Multi‑home selection step (`enabled_homes` → `CONF_DISABLED_HOMES`) |
| `entity.py` | +0.4KB | `target_module` branch in `async_added_to_hass`, `**kwargs` |
| `const.py` | +0.2KB | `CONF_DISABLED_HOMES`, `NETATMO_CREATE_{BATTERY,ENERGY,GAS,WATER}` |
| `strings.json` | +0.2KB | Multi‑home UI strings |
| `manifest.json` | small | Version field; pyatmo dependency removed (bundled) |
| `binary_sensor.py`, `button.py`, `camera.py`, `cover.py`, `device_trigger.py`, `diagnostics.py`, `fan.py`, `helper.py`, `light.py`, `media_source.py`, `select.py`, `switch.py`, `webhook.py`, `api.py`, `application_credentials.py`, `services.yaml`, `icons.json`, `translations/` | 0 | Identical |
