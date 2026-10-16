# APK-confirmed AirSense gaps: spool resume, FGState decode, multi-night statistics

## Summary

Implements three gaps that are confirmed by the myAir 5.4.1.0.681 APK reverse-engineering report (`PROTOCOL.md` from the APK RE workspace) and/or by this integration’s existing wire-verified Summary path. Smallest diff; no control/write methods; does not change pairing.

## Changes

### 1. Spool resume on `SPOOL_COMPLETE_MORE_DATA_PENDING`

**Why:** Proxy drops and multi-page spools were only handled by “retry the whole read from the original `fromDateTime`”. The APK’s fragment record carries resume metadata; without it, remaining pages are dropped when the first page completes with more data pending.

**What:** `AirSenseClient.spool` now:

- Completes one `StartSpool` + `PullSpoolFragments` cycle (`_spool_once`).
- On `SPOOL_COMPLETE_MORE_DATA_PENDING`, re-issues `StartSpool` using `nextSpoolAddress.<Name>.fromDateTime` (APK `ccg` / `i36`).
- Stops on `SPOOL_COMPLETE_NO_MORE_DATA`, `ERROR_DATA_UNAVAILABLE`, missing/null next address, or duplicate `spoolHash` (de-dupe).
- Keeps the existing seq-gap fail-fast + coordinator `SPOOL_ATTEMPTS` retry for lost notifications.

**APK evidence:** `defpackage/ccg.java` (`SpoolAddress` fragment: `seq`, `status`, `spoolHash`, `nextSpoolAddress`), status enum `ccg$c` (`SPOOL_INCOMPLETE`, `SPOOL_COMPLETE_MORE_DATA_PENDING`, `SPOOL_COMPLETE_NO_MORE_DATA`, `ERROR_DATA_UNAVAILABLE`), resume logic in `defpackage/i36.java` (`FgDirectSyncSharedRepository`).

**Verified by:** constructed unit fixtures in `tests/test_resmed_air_ble.py` (two-page resume + null-`fromDateTime` stop). **Needs live-device proof** on a real AirSense 11 (multi-page Summary / long spool through an ESPHome proxy).

### 2. FGState decode (standby / therapy / mask-fit timeline)

**Why:** The machine state sensor already existed as a free-form string. APK nameMappings define a stable 0–9 code table; ENUM + normalization makes standby/therapy/mask-fit usable as a Home Assistant state timeline.

**What:**

- `FG_STATE_BY_CODE` / `normalize_fg_state()` from libfiglib nameMappings shortName `ZRM` / globalName `FGState`:  
  `0 Reset, 1 Standby, 2 ResetCompliance, 3 Therapy, 4 MaskFit, 5 TestMode, 6 SystemError, 7 Upgrade, 8 UpgradePreparation, 9 Setup`.
- Coordinator normalizes `FGState` EventNotification and Get values (int, numeric string, or APK string).
- `sensor.*_state` is `SensorDeviceClass.ENUM` with options = APK names + wire strings `TestDrive` / `Running`.
- `TestMode` included in therapy-running set (blower diagnostic mode).

**APK evidence:** figlib `nameMappings` JSON (`FGState` / `ZRM`); companion model strings in `defpackage/fng.java` (`@ghf("Standby")`, `Therapy`, `MaskFit`, `TestMode`, …).

**Verified by:** constructed unit fixtures for code→name mapping; existing HA entity test still asserts string `Therapy`/`Standby`. **Needs live-device proof** that EventNotification values on this firmware are strings and/or ints as assumed, and that ENUM history renders cleanly.

### 3. Per-night Summary → HA long-term statistics

**Why:** The Summary spool already returns one protobuf record per therapy day (`SUMMARY_DAYS=14`); entities only expose `last_night`. Sleep dashboards need multi-night graphs (usage, AHI, leak 95, pressure 95).

**What:** After a successful Summary pull, import external statistics via `async_add_external_statistics`:

| statistic_id | source field |
|---|---|
| `resmed_airsense:<serial>_usage` | `usage_hours` |
| `resmed_airsense:<serial>_ahi` | `ahi` |
| `resmed_airsense:<serial>_leak_p95` | `leak_lps.p95` × 60 (L/min) |
| `resmed_airsense:<serial>_pressure_p95` | `target_pressure.p95` (cmH₂O) |

One hour-aligned sample per used night (at `day_end`). Metadata is compatible with HA 2025.1 (`has_mean`) and 2026.x (`mean_type` / `unit_class` when present). Module is named `summary_statistics.py` so it does not shadow stdlib `statistics` when the protocol package path is on `sys.path`.

**Evidence class:** wire-verified Summary decoder already matched against STR.edf (existing integration §7); multi-day list path already existed (`parse_summary_spool` / `data.days`). No new wire decode.

**Verified by:** constructed multi-day Summary fixtures → row builder unit test. **Needs live-device proof** that recorder accepts the import on a paired HA instance and that a statistics-graph card shows multiple nights after a real spool pull.

## Out of scope (intentionally)

- `TherapyOneMinutePeriodic` half-float layout (APK model known; byte layout needs a live capture).
- Control methods (`EnterTherapy`, `Set`, …).
- `HumidifierPower` semantics (absent from APK).
- Opening the upstream PR before live pairing proves the branch on the real machine.

## Test plan

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q tests
python -m pytest -q tests_ha -p no:cacheprovider
```

- Protocol: spool resume (positive + null-next negative), FGState normalize, multi-night stats rows, existing seq-gap fail-fast (negative control).
- HA: existing entity/event/spool-retry suites unchanged.

## Live-device checklist (before upstream PR)

1. Complete pairing (4-digit screen code) on the AirSense 11.
2. Confirm `sensor.*_state` tracks Standby → Therapy → Standby across a session (timeline).
3. Pull Summary across several nights; confirm statistics-graph sources for `resmed_airsense:<serial>_usage|_ahi|_leak_p95|_pressure_p95`.
4. If possible, force a multi-page spool / proxy path and confirm resume does not truncate Summary.
