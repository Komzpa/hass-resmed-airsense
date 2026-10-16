"""Persistent BLE link to one AirSense: connect, open session, subscribe, poll, stream, reconnect."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store

from .const import (
    AUTH_REJECTIONS_FOR_REAUTH,
    CONF_CLIENT_ID,
    CONF_MASTER_PAIR_KEY,
    CONF_SERIAL,
    DIAGNOSTIC_SPOOL_BYTE_CAP,
    DIAGNOSTIC_SPOOL_DAYS,
    DIAGNOSTIC_SPOOL_NAMES,
    DOMAIN,
    EVENT_IDS,
    EVENT_THERAPY_STOPPED,
    KEEPALIVE_INTERVAL,
    RECONNECT_BACKOFF,
    SETTING_EVENTS,
    SPOOL_ATTEMPTS,
    SETTINGS_INTERVAL,
    STREAM_IDS,
    STREAM_REPORT_MS,
    SUMMARY_DAYS,
    SUMMARY_INTERVAL,
    SUMMARY_RETRY,
)
from .resmed_air_ble import FG_THERAPY_STATES, AirSenseClient, AirSenseError, AuthError, Credentials, normalize_fg_state
from .resmed_air_ble.logs import CloudUpload, parse_cloud_uploads, parse_soundchecks
from .resmed_air_ble.summary import DailySummary, last_night, parse_summary_spool
from .summary_statistics import async_import_summary_statistics

_LOGGER = logging.getLogger(__name__)


@dataclass
class AirSenseData:
    events: dict[str, Any] = field(default_factory=dict)  # FGState, TestDriveState, RecoverableError, SystemError
    live: dict[str, float | None] = field(default_factory=dict)  # stream values while blowing
    settings: dict[str, Any] = field(default_factory=dict)  # SettingProfiles
    metrics: dict[str, Any] = field(default_factory=dict)  # MachineMetrics
    identity: dict[str, Any] = field(default_factory=dict)  # IdentificationProfiles
    version: dict[str, Any] = field(default_factory=dict)  # GetVersion
    cellular: dict[str, Any] = field(default_factory=dict)  # Get CellularModule (APN, data mode, cloud contact)
    days: list[DailySummary] = field(default_factory=list)
    last_upload: CloudUpload | None = None  # last cloud (myAir/AirView) upload session
    last_soundcheck: dt.datetime | None = None
    last_night: DailySummary | None = None

    @property
    def therapy_running(self) -> bool:
        return self.events.get("FGState") in FG_THERAPY_STATES


class AirSenseCoordinator:
    """Owns the connection task; entities read `data` and `connected`."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS]
        self.creds = Credentials(entry.data[CONF_CLIENT_ID], bytes.fromhex(entry.data[CONF_MASTER_PAIR_KEY]))
        self.data = AirSenseData()
        self.connected = False
        self._listeners: list[Callable[[], None]] = []
        self._task: asyncio.Task | None = None
        self._seen = asyncio.Event()  # set when the device advertises
        self._wake = asyncio.Event()  # set by events that need a reaction in the session loop
        self._settings_dirty = False  # a setting changed on the machine; re-read SettingProfiles
        self._unsub_adv: CALLBACK_TYPE | None = None
        # last known non-live data, so sensors survive restarts and out-of-range periods
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self._summary_raw: str | None = None  # base64 of the last Summary spool
        self.therapy_one_minute_raw: bytes | None = None
        self.therapy_one_minute_start: dt.datetime | None = None
        self._client: AirSenseClient | None = None
        self._spool_lock = asyncio.Lock()

    # ---- lifecycle ----------------------------------------------------------------------

    async def async_start(self) -> None:
        self._unsub_adv = bluetooth.async_register_callback(
            self.hass,
            self._on_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=self.address, connectable=True),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
        self._task = self.entry.async_create_background_task(self.hass, self._run(), f"{DOMAIN} {self.address}")

    async def async_stop(self) -> None:
        if self._unsub_adv:
            self._unsub_adv()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def async_load(self) -> None:
        """Restore the last known data; call before the platforms are set up."""
        stored = await self._store.async_load() or {}
        for key in ("settings", "metrics", "identity", "version", "cellular"):
            setattr(self.data, key, stored.get(key) or {})
        if up := stored.get("last_upload"):
            self.data.last_upload = CloudUpload(
                start=dt.datetime.fromisoformat(up["start"]),
                end=dt.datetime.fromisoformat(up["end"]) if up.get("end") else None,
                http_statuses=up.get("http", []),
                items_sent=up.get("items", 0),
            )
        if sc := stored.get("last_soundcheck"):
            self.data.last_soundcheck = dt.datetime.fromisoformat(sc)
        if summary := stored.get("summary"):
            self._summary_raw = summary
            with contextlib.suppress(ValueError, IndexError):
                self._set_days(parse_summary_spool(base64.b64decode(summary)))

    def _save(self, summary: bytes | None = None) -> None:
        if summary is not None:
            self._summary_raw = base64.b64encode(summary).decode()

        def data() -> dict[str, Any]:
            return {
                "settings": self.data.settings,
                "metrics": self.data.metrics,
                "identity": self.data.identity,
                "version": self.data.version,
                "cellular": self.data.cellular,
                "last_upload": {
                    "start": u.start.isoformat(),
                    "end": u.end.isoformat() if u.end else None,
                    "http": u.http_statuses,
                    "items": u.items_sent,
                } if (u := self.data.last_upload) else None,
                "last_soundcheck": self.data.last_soundcheck.isoformat() if self.data.last_soundcheck else None,
                "summary": self._summary_raw,
            }

        self._store.async_delay_save(data, 10)

    def _set_days(self, days: list[DailySummary]) -> None:
        self.data.days = days
        self.data.last_night = last_night(days)

    @callback
    def async_add_listener(self, update: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    @callback
    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    @callback
    def _on_advertisement(self, _info: bluetooth.BluetoothServiceInfoBleak, _change: Any) -> None:
        self._seen.set()

    # ---- connection loop -------------------------------------------------------------------

    async def _run(self) -> None:
        attempt = 0
        rejected = 0  # consecutive explicit rejections
        while True:
            try:
                await self._session()
                attempt = rejected = 0
            except AuthError as err:
                rejected += 1
                if rejected >= AUTH_REJECTIONS_FOR_REAUTH:
                    # another client (e.g. the phone app) paired and took the only slot
                    _LOGGER.warning("%s: credentials rejected (%s); re-pairing needed", self.address, err)
                    self._set_connected(False)
                    self.entry.async_start_reauth(self.hass)
                    return
                _LOGGER.debug("%s: credentials rejected once (%s); retrying before asking to re-pair",
                              self.address, err)
            except (AirSenseError, BleakError, TimeoutError) as err:
                _LOGGER.debug("%s: link lost: %s", self.address, err)
            self._set_connected(False)
            self._clear_live()
            delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
            attempt += 1
            # the device is silent on BLE while idle; wake up early when it advertises again
            self._seen.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._seen.wait(), delay if attempt < 3 else 600)

    async def _session(self) -> None:
        device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
        if device is None:
            raise AirSenseError("not in range of any connectable adapter/proxy")
        lost = asyncio.Event()
        ble = await establish_connection(
            BleakClientWithServiceCache,
            device,
            self.entry.title,
            disconnected_callback=lambda _c: self.hass.loop.call_soon_threadsafe(lost.set),
            ble_device_callback=lambda: bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            or device,
        )
        client = AirSenseClient(ble, self._on_event, self._on_notification)
        streaming = False
        try:
            await client.start()
            await client.open_session(self.creds)
            await client.subscribe(EVENT_IDS)
            self._client = client
            self._set_connected(True)
            _LOGGER.debug("%s: session open", self.address)
            await self._refresh_identity(client)
            next_settings = next_summary = 0.0
            while not lost.is_set():
                now = time.monotonic()
                if self._settings_dirty:
                    self._settings_dirty = False
                    next_settings = now
                if now >= next_settings:
                    await self._refresh_settings(client)
                    next_settings = now + SETTINGS_INTERVAL
                if now >= next_summary:
                    ok = await self._refresh_summary(client)
                    await self._refresh_logs(client)
                    next_summary = now + (SUMMARY_INTERVAL if ok else SUMMARY_RETRY)
                want_stream = self.data.therapy_running
                if want_stream != streaming:
                    await self._set_stream(client, want_stream)
                    streaming = want_stream
                    if not want_stream:
                        self._clear_live()
                        # the day record is finalised when therapy stops
                        next_summary = now + 60
                        next_settings = now + 60
                self._wake.clear()
                waiters = [asyncio.ensure_future(e.wait()) for e in (lost, self._wake)]
                try:
                    done, _ = await asyncio.wait(waiters, timeout=KEEPALIVE_INTERVAL, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for w in waiters:
                        w.cancel()
                if not done:  # quiet period: keepalive also catches half-open links
                    self._update_event("FGState", (await client.get("FGState")).get("FGState"))
        finally:
            self._client = None
            _LOGGER.debug("%s: session closed (disconnected=%s)", self.address, lost.is_set())
            await client.stop()
            with contextlib.suppress(Exception):
                await ble.disconnect()

    # ---- pulls ----------------------------------------------------------------------------

    async def _refresh_identity(self, client: AirSenseClient) -> None:
        with contextlib.suppress(AirSenseError):
            self.data.version = await client.call("GetVersion", None, "2.0") or {}
        with contextlib.suppress(AirSenseError):
            self.data.identity = (await client.get("IdentificationProfiles")).get("IdentificationProfiles", {})
        with contextlib.suppress(AirSenseError):
            self.data.cellular = (await client.get("CellularModule")).get("CellularModule", {})
        product = self.data.identity.get("Product", {})
        registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(registry, self.entry.entry_id):
            registry.async_update_device(
                device.id,
                model=product.get("ProductName") or None,
                model_id=product.get("ProductCode") or None,
                serial_number=product.get("SerialNumber") or None,
                sw_version=self.data.identity.get("Software", {}).get("ApplicationIdentifier") or None,
                hw_version=self.data.identity.get("Hardware", {}).get("HardwareIdentifier") or None,
            )
        self._notify()

    async def _refresh_settings(self, client: AirSenseClient) -> None:
        with contextlib.suppress(AirSenseError):
            self.data.settings = (await client.get("SettingProfiles")).get("SettingProfiles", {})
        with contextlib.suppress(AirSenseError):
            self.data.metrics = (await client.get("MachineMetrics")).get("MachineMetrics", {})
        self._save()
        self._notify()

    async def _spool(self, client: AirSenseClient, address: str, days: int) -> bytes:
        """Reads a spool by age, retrying immediately if a proxy drops a notification."""
        since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
        return await self._spool_since(client, address, since)

    async def _spool_since(self, client: AirSenseClient, address: str, since: dt.datetime) -> bytes:
        """Reads a spool from an exact timestamp, retrying a dropped notification."""
        for attempt in range(1, SPOOL_ATTEMPTS + 1):
            try:
                return await client.spool(address, since)
            except AirSenseError as err:
                _LOGGER.debug("%s: %s spool attempt %d failed: %s", self.address, address, attempt, err)
                if attempt == SPOOL_ATTEMPTS:
                    raise
        raise AssertionError("unreachable")


    async def _refresh_summary(self, client: AirSenseClient) -> bool:
        try:
            raw = await self._spool(client, "Summary", SUMMARY_DAYS)
            days = parse_summary_spool(raw)
        except (AirSenseError, ValueError, IndexError):
            return False
        if days:
            self._set_days(days)
            await self._refresh_therapy_one_minute(client)
            self._save(raw)
            self._notify()
            await self._import_summary_statistics()
        return True

    async def _refresh_therapy_one_minute(self, client: AirSenseClient) -> None:
        """Capture the opaque per-minute spool from the earliest recent therapy session.

        last_night alone can be a short same-day session; start from the earliest session
        among used days so the prior full night (and later minutes) are in the capture.
        """
        if self.data.therapy_running:
            return
        used = [d for d in self.data.days if d.usage_minutes > 0 and d.sessions]
        if not used:
            return
        since = min(session.start for day in used for session in day.sessions)
        if self.therapy_one_minute_start == since and self.therapy_one_minute_raw is not None:
            return
        try:
            raw = await self._spool_since(client, "TherapyOneMinutePeriodic", since)
        except (AirSenseError, ValueError, IndexError) as err:
            _LOGGER.debug("%s: per-minute spool read failed: %s", self.address, err)
            return
        if raw:
            self.therapy_one_minute_start = since
            self.therapy_one_minute_raw = raw



    async def async_capture_all_spools(self) -> dict[str, Any]:
        """Pull every APK spool name once for diagnostics (one at a time, byte-capped).

        Errors and DATA_UNAVAILABLE are recorded per name and do not abort the rest.
        Normal Summary/TOMP polling is unchanged; this only runs on diagnostics download.
        """
        import hashlib

        client = self._client
        if client is None or not self.connected:
            return {
                "error": "not_connected",
                "byte_cap": DIAGNOSTIC_SPOOL_BYTE_CAP,
                "days": DIAGNOSTIC_SPOOL_DAYS,
                "spools": {},
            }
        since = dt.datetime.now(dt.UTC) - dt.timedelta(days=DIAGNOSTIC_SPOOL_DAYS)
        # Prefer earliest therapy session when known so TOMP/Summary match the night window.
        used = [d for d in self.data.days if d.usage_minutes > 0 and d.sessions]
        if used:
            session_since = min(session.start for day in used for session in day.sessions)
            if session_since < since:
                since = session_since
        out: dict[str, Any] = {}
        async with self._spool_lock:
            for name in DIAGNOSTIC_SPOOL_NAMES:
                entry: dict[str, Any] = {
                    "address": name,
                    "requested_from": since.isoformat(),
                    "byte_cap": DIAGNOSTIC_SPOOL_BYTE_CAP,
                }
                try:
                    read = await client.spool_read(name, since, max_bytes=DIAGNOSTIC_SPOOL_BYTE_CAP)
                except (AirSenseError, ValueError, IndexError, TypeError) as err:
                    entry.update(
                        {
                            "status": "ERROR",
                            "error": str(err),
                            "raw_length": 0,
                            "fragments": 0,
                            "sha256": None,
                            "raw_base64": None,
                            "truncated": False,
                            "envelope": None,
                        }
                    )
                    out[name] = entry
                    continue
                raw = read.data
                entry.update(
                    {
                        "status": read.status,
                        "raw_length": len(raw),
                        "fragments": read.fragments,
                        "sha256": hashlib.sha256(raw).hexdigest() if raw else None,
                        "raw_base64": base64.b64encode(raw).decode("ascii") if raw else None,
                        "truncated": read.truncated,
                        "spool_hash": read.spool_hash,
                        "envelope": None,
                    }
                )
                if raw:
                    # Lazy import keeps coordinator free of diagnostics helper cycles.
                    from .diagnostics import _protobuf_json_envelope

                    entry["envelope"] = _protobuf_json_envelope(raw)
                out[name] = entry
        return {
            "byte_cap": DIAGNOSTIC_SPOOL_BYTE_CAP,
            "days": DIAGNOSTIC_SPOOL_DAYS,
            "requested_from": since.isoformat(),
            "spools": out,
        }


    async def _import_summary_statistics(self) -> None:
        """Publish multi-night Summary values into HA long-term statistics graphs."""
        if not self.data.days:
            return
        serial = self.entry.data.get(CONF_SERIAL) or self.address.replace(":", "").lower()
        try:
            async_import_summary_statistics(self.hass, serial, self.data.days)
        except Exception as err:  # pragma: no cover - recorder may be absent in unit tests
            _LOGGER.debug("%s: summary statistics import skipped: %s", self.address, err)

    async def _refresh_logs(self, client: AirSenseClient) -> None:
        """Cloud upload + SoundCheck logs; best effort (a failure just keeps the previous values)."""
        try:
            if uploads := parse_cloud_uploads(await self._spool(client, "CellularActivityEvents", 7)):
                self.data.last_upload = uploads[-1]
            if checks := parse_soundchecks(await self._spool(client, "SoundcheckVector", 15)):
                self.data.last_soundcheck = checks[-1]
        except (AirSenseError, ValueError, IndexError) as err:
            _LOGGER.debug("%s: log spools failed: %s", self.address, err)
        self._save()
        self._notify()

    async def _set_stream(self, client: AirSenseClient, on: bool) -> None:
        params = {"dataIds": list(STREAM_IDS) if on else [], "sampleIntervalMs": STREAM_REPORT_MS,
                  "reportIntervalMs": STREAM_REPORT_MS}
        with contextlib.suppress(AirSenseError):
            await client.call("StartStream", params)

    # ---- pushes ----------------------------------------------------------------------------

    def _on_event(self, data_id: str, value: Any, _event: dict) -> None:
        self._update_event(data_id, value)

    def _on_notification(self, method: str, params: dict) -> None:
        if method != "StreamData":
            return
        for item in params.get("data", []):
            for key, values in item.items():
                vals = [v for v in values if v is not None]
                self.data.live[key] = round(sum(vals) / len(vals), 2) if vals else None
        self._notify()

    @callback
    def _update_event(self, data_id: str, value: Any) -> None:
        if value is None:
            return
        if data_id == "FGState":
            value = normalize_fg_state(value)
            if value is None:
                return
        prev = self.data.events.get(data_id)
        if prev == value:
            return
        self.data.events[data_id] = value
        if data_id in SETTING_EVENTS and prev is not None:
            self._settings_dirty = True
            self._wake.set()
        if data_id == "FGState":
            self._wake.set()
            if prev in FG_THERAPY_STATES and value not in FG_THERAPY_STATES:
                self.hass.bus.async_fire(EVENT_THERAPY_STOPPED, {"address": self.address, "from": prev, "to": value})
        self._notify()

    @callback
    def _clear_live(self) -> None:
        if self.data.live:
            self.data.live = {}
            self._notify()

    @callback
    def _set_connected(self, connected: bool) -> None:
        if self.connected != connected:
            self.connected = connected
            self._notify()
