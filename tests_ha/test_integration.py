"""Home Assistant side: config flow (pair / wrong code / reauth) and entities, with the BLE link mocked."""
from __future__ import annotations

import asyncio
import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.bluetooth import BluetoothServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.resmed_airsense.const import (
    CONF_CLIENT_ID,
    CONF_MASTER_PAIR_KEY,
    CONF_SERIAL,
    DOMAIN,
    EVENT_THERAPY_STOPPED,
)
from custom_components.resmed_airsense.resmed_air_ble import AirSenseError, AuthError, Credentials
from pbwrite import cloud_session, soundcheck_run, summary_day

ADDR = "AA:BB:CC:DD:EE:FF"
INFO = BluetoothServiceInfo(
    name="ResMed 678901",
    address=ADDR,
    rssi=-60,
    manufacturer_data={},
    service_data={},
    service_uuids=["0000fd56-0000-1000-8000-00805f9b34fb"],
    source="local",
)
CREDS = Credentials("C0FFEE123456", bytes(range(32)))
DAY = 1_790_161_200_000
GET = {
    "SerialNumber": "12345678901",
    "FGState": "Standby",
    "IdentificationProfiles": {
        "Product": {"SerialNumber": "12345678901", "ProductCode": "39483", "ProductName": "AirSense 11 AutoSet"},
        "Software": {"ApplicationIdentifier": "SW04600.17.8.6.0"},
        "Hardware": {"HardwareIdentifier": "(90)R390-7692"},
    },
    "SettingProfiles": {
        "Attributes": {"AppliedDateTime": "2026-09-12T05:32:35.363Z"},
        "ActiveProfiles": {"TherapyProfile": "AutoSetProfile"},
        "TherapyProfiles": {"AutoSetProfile": {"TherapyMode": "AutoSet", "MaxPressure": 20.0, "MinPressure": 10.0,
                                               "StartPressure": 4.0}},
        "FeatureProfiles": {
            "EprFeature": {"EprEnable": "Off", "EprType": "FullTime", "EprPressure": 3},
            "ClimateFeature": {"ClimateControl": "Auto", "HumidifierLevel": 6, "HeatedTubeTemperature": 27.0},
            "ReminderFeature": {"ReminderMask": {"Enable": "Off", "StartDateTime": "2000-01-01T00:00:00.000Z",
                                                 "Period": "P1M"}},
        },
    },
    "MachineMetrics": {"TherapyRunMeter": "PT1151642S", "LastTherapyUseDateTime": "2026-09-26T05:19:25.000Z",
                       "LastMachineServiceDateTime": "2000-01-01T00:00:00.000Z"},
}
GET["CellularModule"] = {"ConfigurationProfiles": {"DataMode": "ACTIVE", "ServiceHost": "secret.host",
                                                   "PeriodicBrokerContactPeriod": "PT604800S"}}
VERSION = {"BluetoothModule": {"IdentificationProfiles": {"Software": {"ApplicationIdentifier": "ST318.2.13"}}}}


@pytest.fixture(autouse=True)
def auto_enable(enable_custom_integrations):
    yield


@pytest.fixture
def mock_ble():
    """Patches BLE lookup/connection in both flow and coordinator; yields the fake AirSenseClient."""
    client = MagicMock()
    client.start = AsyncMock()
    client.stop = AsyncMock()
    client.pair = AsyncMock(return_value=CREDS)
    client.open_session = AsyncMock()
    client.subscribe = AsyncMock(return_value={})
    client.get = AsyncMock(side_effect=lambda *names: {n: GET[n] for n in names if n in GET})
    client.call = AsyncMock(side_effect=lambda method, *a, **k: VERSION if method == "GetVersion" else {})
    spools = {
        "Summary": summary_day(DAY, 313, [(DAY + 51_480_000, 38), (DAY + 53_880_000, 275)]),
        "CellularActivityEvents": cloud_session(DAY + 60_000_000),
        "SoundcheckVector": soundcheck_run(DAY),
        # Opaque TOMP payload for the diagnostics capture path (layout proven from live bytes later).
        "TherapyOneMinutePeriodic": b"\x0a\x04\x08\x01\x10\x02",
    }

    def _spool(address, since, idle_timeout=5.0):
        if address not in spools:
            raise KeyError(address)
        return spools[address]

    async def _spool_read(address, since, idle_timeout=5.0, max_bytes=None):
        from custom_components.resmed_airsense.resmed_air_ble import SpoolRead

        if address == "SurveyEvents":
            return SpoolRead(data=b"", status="ERROR_DATA_UNAVAILABLE", fragments=1)
        if address == "DiagnosticExceptionEvents":
            raise AirSenseError(f"spool {address}: stalled after 0 fragment(s)")
        raw = spools.get(address, b"\x08\x01")
        if max_bytes is not None and len(raw) > max_bytes:
            return SpoolRead(
                data=raw[:max_bytes],
                status="SPOOL_COMPLETE_NO_MORE_DATA",
                fragments=1,
                truncated=True,
            )
        return SpoolRead(
            data=raw,
            status="SPOOL_COMPLETE_NO_MORE_DATA",
            fragments=1 if raw else 0,
            spool_hash="deadbeef" if raw else None,
        )

    client.spool = AsyncMock(side_effect=_spool)
    client.spool_read = AsyncMock(side_effect=_spool_read)
    ble = MagicMock(disconnect=AsyncMock())
    with (
        patch("custom_components.resmed_airsense.config_flow.bluetooth.async_ble_device_from_address",
              return_value=MagicMock()),
        patch("custom_components.resmed_airsense.coordinator.bluetooth.async_ble_device_from_address",
              return_value=MagicMock()),
        patch("custom_components.resmed_airsense.coordinator.bluetooth.async_register_callback",
              return_value=lambda: None),
        patch("custom_components.resmed_airsense.config_flow.establish_connection", AsyncMock(return_value=ble)),
        patch("custom_components.resmed_airsense.coordinator.establish_connection", AsyncMock(return_value=ble)),
        patch("custom_components.resmed_airsense.config_flow.AirSenseClient", return_value=client),
        patch("custom_components.resmed_airsense.coordinator.AirSenseClient") as coord_cls,
    ):
        coord_cls.side_effect = lambda _ble, on_event=None, on_notification=None: (
            setattr(client, "on_event", on_event), setattr(client, "on_notification", on_notification), client)[2]
        yield client


async def _start_bluetooth_flow(hass: HomeAssistant):
    with patch("homeassistant.components.bluetooth.async_setup", return_value=True), patch(
        "homeassistant.components.bluetooth_adapters.async_setup", return_value=True
    ):
        return await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=INFO
        )


async def test_pairing_flow(hass: HomeAssistant, mock_ble) -> None:
    result = await _start_bluetooth_flow(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "connect"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "code"
    with patch("custom_components.resmed_airsense.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"passkey": " 1234 "})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "AirSense 12345678901"
    assert result["data"] == {
        CONF_ADDRESS: ADDR,
        CONF_CLIENT_ID: "C0FFEE123456",
        CONF_MASTER_PAIR_KEY: CREDS.master_pair_key.hex().upper(),
        CONF_SERIAL: "12345678901",
    }
    mock_ble.pair.assert_awaited_once_with("1234")


async def test_wrong_code_then_retry(hass: HomeAssistant, mock_ble) -> None:
    mock_ble.pair.side_effect = [AuthError("bad"), CREDS]
    result = await _start_bluetooth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"passkey": "0000"})
    assert result["step_id"] == "code" and result["errors"] == {"base": "invalid_code"}
    with patch("custom_components.resmed_airsense.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"passkey": "1234"})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_cannot_connect(hass: HomeAssistant, mock_ble) -> None:
    result = await _start_bluetooth_flow(hass)
    with patch("custom_components.resmed_airsense.config_flow.bluetooth.async_ble_device_from_address",
               return_value=None):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "connect" and result["errors"] == {"base": "cannot_connect"}


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDR,
        title="AirSense 12345678901",
        data={CONF_ADDRESS: ADDR, CONF_CLIENT_ID: "OLD", CONF_MASTER_PAIR_KEY: "00" * 32, CONF_SERIAL: "1"},
    )
    entry.add_to_hass(hass)
    return entry


async def test_entities_and_event(hass: HomeAssistant, mock_ble) -> None:
    entry = _entry(hass)
    stopped = []
    hass.bus.async_listen(EVENT_THERAPY_STOPPED, lambda e: stopped.append(e.data))
    with patch("custom_components.resmed_airsense.coordinator.KEEPALIVE_INTERVAL", 3600):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get("binary_sensor.airsense_12345678901_connected").state == "on"
        state = lambda e: hass.states.get(f"sensor.airsense_12345678901_{e}").state  # noqa: E731
        assert state("usage") == "5.21666666666667" or float(state("usage")) == 5.22
        assert state("ahi") == "0.9" and state("mask_on_off") == "2"
        assert state("leak_95th_percentile") == "26.4"
        assert state("mask_pressure_median") == "8.0" and state("target_pressure_median") == "10.2"
        assert state("tidal_volume_median") == "480" and state("minute_ventilation_median") == "7.25"
        assert state("humidifier_temperature") == "23.2" and state("ambient_humidity") == "15.6"
        assert state("therapy_mode") == "AutoSet" and state("minimum_pressure") == "10.0"
        assert state("humidity_level") == "6" and state("epr") == "Off"
        assert state("therapy_hours") == "319.9"
        assert state("last_therapy") == "2026-09-26T05:19:25+00:00"
        assert state("last_service") == "unavailable"
        assert state("bluetooth_firmware") == "ST318.2.13"
        assert state("last_cloud_upload") == "2026-09-24T03:40:25+00:00"
        assert hass.states.get("sensor.airsense_12345678901_last_cloud_upload").attributes["successful"] is True
        assert state("cloud_upload_status") == "ok"
        assert state("last_soundcheck") == "2026-09-23T11:00:00+00:00"
        assert state("mask_replacement_reminder") == "Off"
        assert state("mask_pressure") == "unavailable"  # live stream only while blowing
        mock_ble.on_event("FGState", "Therapy", {})
        await hass.async_block_till_done()
        assert hass.states.get("binary_sensor.airsense_12345678901_therapy").state == "on"
        assert hass.states.get("sensor.airsense_12345678901_state").state == "Therapy"
        # blower running -> live stream requested; values show up
        for _ in range(10):  # let the background session loop react
            await asyncio.sleep(0)
        assert any(c.args[0] == "StartStream" and c.args[1]["dataIds"] for c in mock_ble.call.call_args_list)
        mock_ble.on_notification("StreamData", {"data": [{"MaskPressure-100hz": [10.4, 10.6]}, {"Leak-50hz": [0.1]}]})
        await hass.async_block_till_done()
        assert hass.states.get("sensor.airsense_12345678901_mask_pressure").state == "10.5"
        assert hass.states.get("sensor.airsense_12345678901_leak").state == "6.0"
        mock_ble.on_event("FGState", "Standby", {})
        await hass.async_block_till_done()
        assert hass.states.get("binary_sensor.airsense_12345678901_therapy").state == "off"
        assert stopped == [{"address": ADDR, "from": "Therapy", "to": "Standby"}]
        for _ in range(10):
            await asyncio.sleep(0)
        await hass.async_block_till_done()
        assert hass.states.get("sensor.airsense_12345678901_mask_pressure").state == "unavailable"
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_fg_state_enum_consumer_validation_and_backup_stop(hass: HomeAssistant, mock_ble) -> None:
    """A valid Backup reply must survive the real entity write, notify and unload path."""
    from custom_components.resmed_airsense.const import SUMMARY_DAYS
    from custom_components.resmed_airsense.resmed_air_ble.protocol import FG_STATE_BY_CODE, FG_STATE_NAMES
    from custom_components.resmed_airsense.sensor import AirSenseSensor

    async def subscribe(*_args):
        # The device can send its state before _session marks the link connected.
        mock_ble.on_event("FGState", "Backup", {})
        return {}

    mock_ble.subscribe.side_effect = subscribe
    mock_ble.get.side_effect = lambda *names: {
        n: "Backup" if n == "FGState" else GET[n] for n in names if n in GET
    }
    entry = _entry(hass)
    with patch("custom_components.resmed_airsense.coordinator.KEEPALIVE_INTERVAL", 3600):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        state = lambda e: hass.states.get(f"sensor.airsense_12345678901_{e}")  # noqa: E731
        assert state("state").state == "Backup"
        assert state("state").attributes["options"] == list(FG_STATE_NAMES)
        assert set(FG_STATE_BY_CODE) == set(range(10))
        assert set(FG_STATE_NAMES) == {*FG_STATE_BY_CODE.values(), "TestDrive", "Running", "Backup"}
        # Every canonical name (not just Backup) passes HA's actual enum validator.
        for name in FG_STATE_NAMES:
            mock_ble.on_event("FGState", name, {})
            assert state("state").state == name
        for code, name in FG_STATE_BY_CODE.items():
            mock_ble.on_event("FGState", code, {})
            assert state("state").state == name

        # Counterfactual old options: the same real _set_connected -> _notify ->
        # async_write_ha_state consumer must reject Backup, not quietly accept it.
        coordinator._set_connected(False)
        mock_ble.on_event("FGState", "Backup", {})
        old_options = [name for name in FG_STATE_NAMES if name != "Backup"]
        with patch.object(AirSenseSensor, "options", property(
            lambda entity: old_options if entity.entity_description.key == "FGState" else entity.entity_description.options
        )):
            with pytest.raises(ValueError, match="provides state value 'Backup'.*not in the list of options"):
                coordinator._set_connected(True)
        # Restoring the canonical options fixes that exact write without swallowing errors.
        coordinator._notify()
        coordinator._set_connected(False)
        coordinator._set_connected(True)
        assert state("state").state == "Backup"
        assert hass.states.get("binary_sensor.airsense_12345678901_therapy").state == "off"
        assert hass.states.get("binary_sensor.airsense_12345678901_connected").state == "on"

        cached = {key: state(key).state for key in (
            "last_night", "usage", "ahi", "leak_95th_percentile", "therapy_mode", "therapy_hours"
        )}
        assert float(cached["usage"]) > 0 and cached["ahi"] == "0.9"
        days, last_night, raw = coordinator.data.days, coordinator.data.last_night, coordinator._summary_raw
        assert days and last_night is not None and raw
        assert SUMMARY_DAYS == 3650
        summary_call = next(call for call in mock_ble.spool.await_args_list if call.args[0] == "Summary")
        age = dt.datetime.now(dt.UTC) - summary_call.args[1]
        assert dt.timedelta(days=3650) <= age < dt.timedelta(days=3650, minutes=1)

        await coordinator.async_stop()
        assert coordinator._task.cancelled()
        assert hass.states.get("binary_sensor.airsense_12345678901_connected").state == "off"
        assert hass.states.get("binary_sensor.airsense_12345678901_therapy").state == "unavailable"
        assert state("state").state == "unavailable"
        assert state("mask_pressure").state == "unavailable"
        assert coordinator.data.live == {}
        assert coordinator.data.days is days and coordinator.data.last_night is last_night
        assert coordinator._summary_raw == raw
        assert {key: state(key).state for key in cached} == cached
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert entry.state is config_entries.ConfigEntryState.NOT_LOADED


async def test_session_failure_clears_live_but_preserves_summary(hass: HomeAssistant, mock_ble) -> None:
    """An unexpected session failure invalidates live consumers but still propagates."""
    entry = _entry(hass)
    with patch("custom_components.resmed_airsense.coordinator.KEEPALIVE_INTERVAL", 3600):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        cached_summary = coordinator.data.last_night
        usage = hass.states.get("sensor.airsense_12345678901_usage").state
        mock_ble.call.side_effect = RuntimeError("session failure control")
        mock_ble.on_event("FGState", "Therapy", {})
        mock_ble.on_notification("StreamData", {"data": [{"MaskPressure-100hz": [10.4, 10.6]}]})
        assert hass.states.get("binary_sensor.airsense_12345678901_therapy").state == "on"
        assert hass.states.get("sensor.airsense_12345678901_mask_pressure").state == "10.5"
        task = coordinator._task
        with pytest.raises(RuntimeError, match="session failure control"):
            await task
        assert not coordinator.connected and coordinator.data.live == {}
        assert hass.states.get("binary_sensor.airsense_12345678901_connected").state == "off"
        assert hass.states.get("binary_sensor.airsense_12345678901_therapy").state == "unavailable"
        assert hass.states.get("sensor.airsense_12345678901_state").state == "unavailable"
        assert hass.states.get("sensor.airsense_12345678901_mask_pressure").state == "unavailable"
        assert coordinator.data.last_night is cached_summary
        assert hass.states.get("sensor.airsense_12345678901_usage").state == usage
        with pytest.raises(RuntimeError, match="session failure control"):
            await coordinator.async_stop()
        # The failure has been observed above; detach the failed task only for fixture teardown.
        coordinator._task = None
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_auth_error_starts_reauth(hass: HomeAssistant, mock_ble) -> None:
    mock_ble.open_session.side_effect = AuthError("VerificationFailure")
    entry = _entry(hass)
    with patch("custom_components.resmed_airsense.coordinator.RECONNECT_BACKOFF", (0, 0, 0, 0, 0)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        for _ in range(20):
            await asyncio.sleep(0)
        await hass.async_block_till_done()
    assert mock_ble.open_session.await_count == 2  # confirmed once more before bothering the user
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows and flows[0]["context"]["source"] == config_entries.SOURCE_REAUTH
    # user re-pairs from the reauth flow
    mock_ble.open_session.side_effect = None
    result = await hass.config_entries.flow.async_configure(flows[0]["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"passkey": "1234"})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data[CONF_CLIENT_ID] == "C0FFEE123456"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_timeouts_never_start_reauth(hass: HomeAssistant, mock_ble) -> None:
    """Regression (v0.1.0): a RequestSession timeout after a proxy reboot asked the user to re-pair."""
    from custom_components.resmed_airsense.resmed_air_ble import AirSenseError

    mock_ble.open_session.side_effect = AirSenseError("RequestSession: timeout")
    entry = _entry(hass)
    # zero backoff: attempts 1-3 run back to back, then the loop waits for an advertisement
    with patch("custom_components.resmed_airsense.coordinator.RECONNECT_BACKOFF", (0, 0, 0, 0, 0)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        for _ in range(50):
            await asyncio.sleep(0)
        assert mock_ble.open_session.await_count >= 3
        assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_diagnostics_redacts_secrets(hass: HomeAssistant, mock_ble) -> None:
    from custom_components.resmed_airsense.const import DIAGNOSTIC_SPOOL_NAMES
    from custom_components.resmed_airsense.diagnostics import async_get_config_entry_diagnostics

    entry = _entry(hass)
    with patch("custom_components.resmed_airsense.coordinator.KEEPALIVE_INTERVAL", 3600):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        diag = await async_get_config_entry_diagnostics(hass, entry)
        dump = str(diag)
        assert "00" * 32 not in dump and "OLD" not in dump and "12345678901" not in dump
        assert "secret.host" not in dump and diag["cellular"]["ConfigurationProfiles"]["DataMode"] == "ACTIVE"
        assert diag["entry"]["master_pair_key"] == "**REDACTED**"
        assert diag["last_night"]["usage_minutes"] == 313
        assert diag["settings"]["ActiveProfiles"]["TherapyProfile"] == "AutoSetProfile"
        capture = diag["therapy_one_minute_capture"]
        assert capture["address"] == "TherapyOneMinutePeriodic"
        assert capture["raw_length"] == 6
        assert capture["raw_base64"]
        assert capture["envelope"]["parsed"] is True
        assert capture["requested_from"].startswith("2026-09")
        all_cap = diag["all_spools_capture"]
        assert set(all_cap["spools"]) == set(DIAGNOSTIC_SPOOL_NAMES)
        tomp = all_cap["spools"]["TherapyOneMinutePeriodic"]
        assert tomp["status"] == "SPOOL_COMPLETE_NO_MORE_DATA"
        assert tomp["raw_length"] == 6
        assert tomp["fragments"] == 1
        assert tomp["sha256"]
        assert tomp["raw_base64"]
        assert tomp["envelope"]["parsed"] is True
        survey = all_cap["spools"]["SurveyEvents"]
        assert survey["status"] == "ERROR_DATA_UNAVAILABLE"
        assert survey["raw_length"] == 0
        assert survey["raw_base64"] is None
        bad = all_cap["spools"]["DiagnosticExceptionEvents"]
        assert bad["status"] == "ERROR"
        assert "stalled" in bad["error"]
        # unavailable / error must not abort the rest
        assert all_cap["spools"]["Summary"]["raw_length"] > 0
        assert await hass.config_entries.async_unload(entry.entry_id)




async def test_restores_last_known_data_while_out_of_range(hass: HomeAssistant, mock_ble, hass_storage) -> None:
    import base64

    entry = _entry(hass)
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {
        "version": 1,
        "key": f"{DOMAIN}.{entry.entry_id}",
        "data": {
            "settings": GET["SettingProfiles"],
            "metrics": GET["MachineMetrics"],
            "identity": {},
            "version": {},
            "summary": base64.b64encode(summary_day(DAY, 313, [(DAY + 51_480_000, 313)])).decode(),
        },
    }
    with patch("custom_components.resmed_airsense.coordinator.bluetooth.async_ble_device_from_address", return_value=None):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        state = lambda e: hass.states.get(f"sensor.airsense_12345678901_{e}").state  # noqa: E731
        assert hass.states.get("binary_sensor.airsense_12345678901_connected").state == "off"
        assert state("ahi") == "0.9" and state("therapy_mode") == "AutoSet" and state("therapy_hours") == "319.9"
        assert state("state") == "unavailable"  # live values need the link
        assert hass.states.get("binary_sensor.airsense_12345678901_therapy").state == "unavailable"
        assert float(state("usage")) > 0
        assert state("last_night") != "unavailable"
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_setting_event_rereads_settings(hass: HomeAssistant, mock_ble) -> None:
    entry = _entry(hass)
    with patch("custom_components.resmed_airsense.coordinator.KEEPALIVE_INTERVAL", 3600):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        mock_ble.subscribe.assert_awaited()
        assert "HumidifierConnected" in mock_ble.subscribe.await_args.args[0]
        mock_ble.on_event("HumidifierLevel", 6, {})  # initial value: no re-read
        before = sum(1 for c in mock_ble.get.await_args_list if c.args == ("SettingProfiles",))
        mock_ble.on_event("HumidifierLevel", 4, {})  # changed on the machine
        for _ in range(10):
            await asyncio.sleep(0)
        after = sum(1 for c in mock_ble.get.await_args_list if c.args == ("SettingProfiles",))
        assert after == before + 1
        mock_ble.on_event("TubeConnected", "15mmNonHeated", {})
        await hass.async_block_till_done()
        assert hass.states.get("sensor.airsense_12345678901_tube_attached").state == "15mmNonHeated"
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_spool_read_retries_after_lost_fragment(hass: HomeAssistant, mock_ble) -> None:
    from custom_components.resmed_airsense.resmed_air_ble import AirSenseError

    calls: dict[str, int] = {}
    good = mock_ble.spool.side_effect

    def flaky(address, since):
        calls[address] = calls.get(address, 0) + 1
        if address == "CellularActivityEvents" and calls[address] < 3:
            raise AirSenseError("spool CellularActivityEvents: stalled after 1 fragment(s)")
        return good(address, since)

    mock_ble.spool.side_effect = flaky
    entry = _entry(hass)
    with patch("custom_components.resmed_airsense.coordinator.KEEPALIVE_INTERVAL", 3600):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert calls["CellularActivityEvents"] == 3
        assert hass.states.get("sensor.airsense_12345678901_cloud_upload_status").state == "ok"
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_capture_labels_firmware_refused_spool(hass: HomeAssistant, mock_ble) -> None:
    from custom_components.resmed_airsense.diagnostics import async_get_config_entry_diagnostics
    from custom_components.resmed_airsense.resmed_air_ble import RpcError

    good = mock_ble.spool_read.side_effect

    async def picky(address, since, idle_timeout=5.0, max_bytes=None):
        if address == "TherapyEvents":
            raise RpcError("StartSpool", {"code": -32602, "message": "Invalid Params"})
        if address == "UsageEvents":
            raise RpcError("StartSpool", {"code": -32000, "message": "Device busy"})
        return await good(address, since, idle_timeout=idle_timeout, max_bytes=max_bytes)

    mock_ble.spool_read.side_effect = picky
    entry = _entry(hass)
    with patch("custom_components.resmed_airsense.coordinator.KEEPALIVE_INTERVAL", 3600):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        diag = await async_get_config_entry_diagnostics(hass, entry)
        spools = diag["all_spools_capture"]["spools"]
        refused = spools["TherapyEvents"]
        assert refused["status"] == "refused"
        assert refused["error_code"] == -32602
        assert refused["error_message"] == "Invalid Params"
        other = spools["UsageEvents"]
        assert other["status"] == "ERROR"
        assert "error_code" not in other
        assert await hass.config_entries.async_unload(entry.entry_id)
