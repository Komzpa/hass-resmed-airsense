# ResMed AirSense 11 — local Bluetooth protocol

Technical reference for what this integration implements. Reverse-engineered for personal interoperability
(reading one's own machine); it only covers **read-only** operations. Tested on an AirSense 11 AutoSet;
other "Air 11" machines (AirCurve 11) use the same stack.

The protocol library lives in [`custom_components/resmed_airsense/resmed_air_ble/`](../custom_components/resmed_airsense/resmed_air_ble)
and has no Home Assistant dependency.

## 1. GATT profile

| Item | UUID |
|---|---|
| Service | `0000fd56-0000-1000-8000-00805f9b34fb` (ResMed's SIG-assigned 16-bit UUID) |
| TX — write, app → machine | `a6220002-35f1-4b20-afae-cb089d2044aa` |
| RX — notify, machine → app | `a6220003-35f1-4b20-afae-cb089d2044aa` |
| Device Information | `0x180A` (model, firmware, serial strings) |

The machine advertises as `ResMed <last 6 digits of the serial>`. It accepts **one connection at a time** and
stops advertising while connected. Writes larger than the ATT MTU are split by the client; the machine reassembles.
No Bluetooth-level bonding is used — authentication happens in the application protocol below.

## 2. FIG framing

Every message (both directions) is a frame; notifications may carry a frame in several pieces.

```
offset  size  field
0       4     magic      CA FE BA BE  (bytes on the wire: BE BA FE CA)
4       2     type       uint16 LE — channel + flags
6       2     length     uint16 LE — payload length
8       4     pcrc       CRC-32 of the payload, LE
12      4     hcrc       CRC-32 of bytes 4..11, LE
16      n     payload    JSON text, or IV(16) || AES-256-CBC(...) when encrypted
```

| type | direction | content |
|---|---|---|
| `0x0393` | app → machine | clear (pairing, session handshake) |
| `0x0392` | machine → app | clear (heartbeats, handshake results) |
| `0x0397` | app → machine | encrypted session |
| `0x0396` | machine → app | encrypted session |

Bit `0x0004` of `type` marks encryption. Encrypted payload: `IV(16) || AES-256-CBC(key, IV, len(2, LE) || JSON || zero padding to 16)`.

## 3. JSON-RPC

`{"id": N, "jsonrpc": "<version>", "method": "<Method>", "params": ...}`. The `jsonrpc` value is the method's
version, not "2.0" for everything: pairing and session methods use `"2.0"`, `Get`/`SubscribeEvent`/`StartStream`/
`StartSpool` use `"1.0"`, `GetVersion` uses `"2.0"`. Errors: `{"error": {"code": ..., "message": ...}}`
(e.g. `-11005 VerificationFailure`, `-11201 InvalidObject` with the offending names, `-32602 Invalid Params`).
`GetVersion` returns the list of methods and versions the machine supports.

## 4. First pairing — SRP-6a with the on-screen code

The code shown on the machine's screen is the password of an SRP-6a exchange (channel `0x0393`, clear):

```
→ StartKeyExchange   {"clientPk": A}                         A = 256-byte hex
← result             {"serverPk": B, "salt": s}
→ ConfirmKeyExchange {"clientConfirmation": M1}
← result             {"clientId": "<12 hex>", "serverConfirmation": M2, "nonce": "<32-byte hex>"}
```

Parameters: RFC 5054 2048-bit group, `g = 2`, `H = SHA-256`, `PAD()` = left-pad to 256 bytes, **no identity**.

```
a random 32 bytes,       A = g^a mod N
u = H(PAD(A) | PAD(B)),  k = H(PAD(N) | PAD(g)),  x = H(salt | H(code as ASCII))
S = (B - k·g^x)^(a + u·x) mod N
K = H(PAD(S))                                          -> masterPairKey
M1 = H(H(PAD(N)) xor H(PAD(g)) | salt | PAD(A) | PAD(B) | K)
M2 = H(PAD(A) | M1 | K)
```

After `ConfirmKeyExchange` the encrypted session is already open with `sessionKey = SHA-256(K || nonce)`.
The machine keeps **one** `clientId`/`masterPairKey`; pairing again replaces the previous client.

## 5. Reconnecting

```
→ RequestSession        {"clientId": ...}               (clear)
← result                {"challenge": c, "nonce": n}
→ CheckSessionIntegrity {"response": HMAC-SHA256(K, c) as upper-case hex}
← result                {"confirmation": true}
   sessionKey = SHA-256(K || n)
```

## 6. Reading data (all encrypted, all read-only)

**Events** — `SubscribeEvent {"dataIds": [...]}`; the reply flags each id `valid`, then
`EventNotification {"dataId", "events": [{"value", "reportTime"}]}` arrives on every change.
Valid on the AirSense 11: `FGState` (Standby/Therapy/MaskFit/…), `TestDriveState`, `RecoverableError`,
`SystemError`, `Leak`, `HumidifierConnected`, `TubeConnected`, `HumidifierLevel`, `HeatedTubeTemperature`,
`RemainingRampTime`, `SmartStart`, `SmartStop`, `ActiveTherapyProfile`, `Language`, `TimeZoneOffset`, …

**Get** — `Get ["Name", ...]` returns the named objects; one unknown name fails the whole call with
`InvalidObject` listing it. Useful objects: `SettingProfiles` (active profile, pressures, EPR, ramp,
comfort, humidifier, heated tube, mask/tube type, reminders…), `MachineMetrics` (run meters, last use),
`IdentificationProfiles` (product, software, hardware), `CellularModule`, `SerialNumber`, `FGState`.

**Live stream** — `StartStream {"dataIds": [...], "sampleIntervalMs", "reportIntervalMs"}` (empty list stops),
then `StreamData {"data": [{"<id>": [values]}], "startTime", "intervalMs"}`. Values are only meaningful while the
blower runs. Units (validated against the daily summary of the same night): pressures in cmH₂O, `Leak-50hz` in
L/s, `TidalVolume` in L, `MinuteVentilation` in L/min, `RespiratoryRate` in breaths/min, `FlowLimitation` as a
0–1 index, `MotorSpeed` in rpm, `RemainingRampTime` in minutes. `HumidifierPower` is accepted but always reads 0.
Ids include `MaskPressure-100hz`, `InspiratoryPressure-50hz`, `ExpiratoryPressure`, `SetPressureWithoutCAD`,
`Leak-50hz`, `RespiratoryRate`, `TidalVolume`, `MinuteVentilation`, `FlowLimitation`, `SpO2`, `HeartRate`,
`MotorSpeed`, `HeatedTubePower`.

**Logs (spools)** — `StartSpool {"maxSpoolSize", "spoolAddress": {"<Log>": {"fromDateTime": ISO8601}}}` →
`spoolId`; `PullSpoolFragments {"spoolId", "maxFragmentSize", "maxNotifications": 0}` → notifications
`SpoolFragment {"seq", "data": base64, "status", "spoolHash"?, "nextSpoolAddress"?}` until a terminal
status. Status values (APK `ccg$c`): `SPOOL_INCOMPLETE`, `SPOOL_COMPLETE_MORE_DATA_PENDING`,
`SPOOL_COMPLETE_NO_MORE_DATA`, `ERROR_DATA_UNAVAILABLE`. Concatenate `data` by `seq` within one
`spoolId`; on `SPOOL_COMPLETE_MORE_DATA_PENDING` re-issue `StartSpool` using
`nextSpoolAddress.<Log>.fromDateTime` and append. A gap in `seq` or silence means the read must be
retried from the original `fromDateTime` (Bluetooth proxies occasionally drop a notification).

## 7. Daily summary (`Summary` spool)

One record per therapy day (noon to noon), field `2` of the stream. Every field was matched against the machine's
own SD-card summary (`STR.edf`) over 70+ nights with no mismatch; values are the STR.edf signal × 100.

| Field | STR.edf signal | Field | STR.edf signal |
|---|---|---|---|
| 2 / 3 | day start / end (ms) | 20.{2,3,4} | TgtEPAP.50/95/Max |
| 5 | Duration (minutes, not scaled) | 21.{2,3,4} | MaskPress.50/95/Max |
| 6 | sessions {1: start ms, 2: minutes} | 22.{2,3,4} | TidVol.50/95/Max (L) |
| 7 / 8 / 9 | AHI / AI / HI | 23.{2,3,4} | MinVent.50/95/Max (L/min) |
| 10 / 11 / 12 / 13 | OAI / CAI / UAI / RIN | 25.{2,3,4} | RespRate.50/95/Max |
| 14.{2,3,4,5} | Leak.50/70/95/Max (L/s) | 29 / 30 / 31 (.2) | AmbHumidity / HumTemp / HTubeTemp .50 |
| 15.{2,3,4} | TgtIPAP.50/95/Max | 32 / 33 (.2) | HumPow / HTubePow .50 |
| 16 | CSR | 36.{1,3} / 37.{1,3} | BlowPress.5/95 / Flow.5/95 |
| 38.2 | BlowFlow.50 | 39 | MaskEvents (not scaled) |

Other logs: `CellularActivityEvents` (the modem's nightly upload session with HTTP statuses),
`SoundcheckVector` (weekly circuit self-test), `TherapyOneMinutePeriodic` (per-minute channels, compressed,
not decoded yet), `SettingProfilesCollection`, `GUIActivityEvents`, `MemoryMetrics`, `MachineMetrics`.

## 8. Out of scope

Methods that change the machine (`Set`, `Enter*`, `EraseData`, upgrades, `GenerateAuthCode`, `DiscardPairKey`)
are deliberately not implemented.
