# ResMed AirSense — local Bluetooth for Home Assistant

[![Validate](https://github.com/maroliar/hass-resmed-airsense/actions/workflows/validate.yml/badge.svg)](https://github.com/maroliar/hass-resmed-airsense/actions/workflows/validate.yml)
[![Tests](https://github.com/maroliar/hass-resmed-airsense/actions/workflows/tests.yml/badge.svg)](https://github.com/maroliar/hass-resmed-airsense/actions/workflows/tests.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)

Custom integration (HACS) that reads a ResMed AirSense 11 CPAP directly over Bluetooth Low Energy — no cloud,
no myAir account. It exposes the machine's live state, live pressure and leak while you sleep, last night's
results (usage, AHI, leak, pressure, breathing) and every therapy and comfort setting, as regular Home Assistant
entities you can chart, keep in history and automate on.

Some things people build with it: a morning dashboard of last night's therapy, long-term AHI/leak trends,
a reminder when usage drops, climate automations around the humidifier — or a soft bedside alarm when the mask
comes off during the night (see the example below).

> ⚠️ **Not a medical device, not affiliated with ResMed.** This is an independent interoperability project that
> only *reads* data the machine already exposes to its own companion app. It never changes settings, firmware
> or therapy. Do not rely on it for anything safety-critical or for treatment decisions — that is what your
> sleep physician, the machine itself and ResMed's own tools are for.

## What you get

Everything the machine exposes to its companion app over Bluetooth, read-only:

| Group | Entities | Updated |
|---|---|---|
| **Live** | state (`Standby`/`Therapy`/`MaskFit`…), *Therapy* running (binary), recoverable/system error, test-drive state, humidifier and tube attached, leak alert | pushed instantly |
| **Live while blowing** | mask / inspiratory / expiratory / target pressure, leak, respiratory rate, tidal volume, minute ventilation, flow limitation, ramp time remaining, motor speed, humidifier and heated-tube power, SpO₂ and heart rate (with an oximeter) | every 5 s during therapy |
| **Last night** | date + session list, usage hours, mask on/off count, AHI, apnea/hypopnea index, obstructive/central/unclassified apnea index, leak (median/95th/max), pressure (median/95th/max), respiratory rate, tidal volume, minute ventilation | after each session, hourly |
| **Therapy settings** | mode, min/max/set/start pressure, EPR (on/level/type), ramp (on/time), AutoSet comfort, SmartStart/Stop, mask type, tube type, antibacterial filter, climate control, humidifier (on/level), heated tube (mode/temperature), MaskSense | every 15 min |
| **Cloud & self-test** | last upload to ResMed's cloud (myAir/AirView) with success/HTTP status, last SoundCheck, cellular data mode/APN/contact period | after each night |
| **Machine** | therapy/motor/machine run hours, last therapy, last service, settings last changed, replacement reminders (mask/tubing/filter/humidifier), language, time zone, SoundCheck, firmware versions (main, Bluetooth, bootloader, configuration, cellular modem) | every 15 min / on connect |

The device page shows model, serial number, firmware and hardware revision.
The *Therapy* binary sensor turning `on → off` means the blower stopped — the mask came off (AutoStop) or the
night ended; the event `resmed_airsense_therapy_stopped` (`address`, `from`, `to`) fires at the same moment.

Last-night values come from the machine's daily summary log, whose format is undocumented. Every decoded field
was matched against the machine's own SD-card summary (`STR.edf`) over 72 nights with zero mismatches, and uses
ResMed's names and units (AHI and apnea/hypopnea indices, leak 50/70/95/max, mask and target pressure, target EPAP,
tidal volume, minute ventilation, respiratory rate, Cheyne-Stokes, blower pressure/flow, ambient humidity,
humidifier and heated-tube temperature/power). Less common sensors are disabled by default — enable them on the
device page. Last known values are kept across restarts.

**The machine goes quiet on Bluetooth when idle** (daytime standby): the *Connected* sensor turns off and live values
become unavailable, while last night's data, settings and meters stay. It reconnects by itself as soon as the machine
wakes up (touching it or starting therapy).

**Reporting contract:** the state enum MUST expose every canonical `FG_STATE_NAMES` value from
`resmed_air_ble/protocol.py`: all APK/native ZRM names plus evidenced wire-only names, including
`Backup`. Valid states MUST reach Home Assistant unchanged; do not substitute `unknown` or suppress
entity-write errors. Session loss, cancellation or failure MUST turn *Connected* off and make
live state and *Therapy* unavailable, never infer therapy-off from a disconnected machine. Cached
Summary dates/values, settings and meters MUST remain readable; the full-history Summary read window
(`SUMMARY_DAYS = 3650`) and statistics importer MUST be preserved. Reports MUST label an older Summary
with its actual date and leave a missing current night as a gap, not zero usage or a power-based estimate.

## Important: Home Assistant *or* the phone app's Bluetooth — not both

The machine remembers **one Bluetooth pairing** (the code shown on its screen) and accepts **one connection** at a
time.

- Pairing Home Assistant replaces the myAir app's pairing, and pairing the app again replaces Home Assistant's
  (Home Assistant then shows *Re-authentication required* and asks for a new screen code). Stopping the
  integration does **not** give the app its pairing back — each side needs its own code, and the last one wins.
- **The myAir app keeps working for your nightly reports.** The machine uploads each night over its own cellular
  modem, and the app reads them from ResMed's cloud — no Bluetooth needed (verified: usage, AHI and mask-seal
  data showed up in a freshly installed, unpaired app). What you give up in the app are its Bluetooth-only
  features (e.g. the mask-fit test and changing comfort settings from the phone). If the app offers to pair
  with the machine, decline it to keep Home Assistant connected.

## Requirements

- Home Assistant with Bluetooth, and a **connectable** Bluetooth path within range of the bedroom:
  a local adapter, or an **ESPHome Bluetooth proxy with active connections enabled**:
  ```yaml
  bluetooth_proxy:
    active: true
  ```
  A passive proxy can see the machine but cannot connect to it.
- Tested on an AirSense 11 AutoSet (firmware SW04600.17.8.6.0). Other "Air 11" machines (AirCurve 11) likely work.

## Install

1. HACS → Integrations → ⋮ → Custom repositories → add this repository (category *Integration*) → install.
2. Restart Home Assistant.
3. The machine is usually discovered automatically; otherwise *Settings → Devices & services → Add integration →
   ResMed AirSense*.
4. Put the machine in Bluetooth pairing mode, press **Submit**, then type the code shown on its screen.

## Mask-off alert (blueprint)

[![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/create-link/?redirect=blueprint_import&blueprint_url=https%3A%2F%2Fgithub.com%2Fmaroliar%2Fhass-resmed-airsense%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fresmed_airsense%2Ftherapy_stopped_alert.yaml)

The blueprint runs your actions when therapy stops during the night (after a minimum time on therapy, inside a
time window) — e.g. a soft sound on the bedroom speaker. Or write it yourself:

```yaml
automation:
  - alias: "CPAP mask off at night"
    triggers:
      - trigger: state
        entity_id: binary_sensor.airsense_therapy
        from: "on"
        to: "off"
    conditions:
      - condition: time
        after: "23:00:00"
        before: "06:30:00"
    actions:
      - action: media_player.play_media
        target:
          entity_id: media_player.bedroom
        data:
          media_content_id: media-source://media_source/local/soft_chime.mp3
          media_content_type: music
```

## Troubleshooting

- **Bug reports:** attach the file from *Settings → Devices & services → ResMed AirSense → ⋮ → Download
  diagnostics* (pairing keys and serial numbers are redacted), plus a debug log.
- **Everything unavailable after pairing the phone app:** expected — the machine keeps one pairing. Home Assistant
  shows a *Re-authentication required* card; follow it with a new screen code.
- **Last-night sensors lag after a proxy hiccup:** the summary is re-read automatically (retries within seconds,
  then every 2 minutes).

## How it works

Reverse-engineered from the official app for personal interoperability: BLE GATT service `0xFD56` carrying
ResMed's "FIG" framing (CRC-32 frames) and JSON-RPC. First pairing is an SRP-6a password-authenticated key exchange
using the on-screen code; reconnections use an HMAC challenge and an AES-256-CBC session. After subscribing, the
machine pushes state changes, streams live values while it runs, and serves its internal logs (daily summary,
cloud-upload and self-test logs). Full details: [docs/PROTOCOL.md](docs/PROTOCOL.md). The protocol library
(`custom_components/resmed_airsense/resmed_air_ble/`) has no Home Assistant dependencies.

## Development

```bash
pip install bleak cryptography pytest
pytest tests                       # protocol + SRP + simulated device
pip install -r requirements_test.txt   # Linux: Home Assistant does not run on Windows
pytest tests_ha                    # config flow / entities with BLE mocked

# dev CLI (Linux + BlueZ), run from custom_components/resmed_airsense/
python -m resmed_air_ble pair    <MAC> creds.json
python -m resmed_air_ble monitor <MAC> creds.json 300
```

## License

MIT
