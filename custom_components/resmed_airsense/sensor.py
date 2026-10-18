"""Everything the machine exposes: live state/stream, last night's summary, settings, run meters, versions."""
from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    REVOLUTIONS_PER_MINUTE,
    PERCENTAGE,
    EntityCategory,
    UnitOfPressure,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AirSenseConfigEntry
from .coordinator import AirSenseData
from .entity import AirSenseEntity
from .resmed_air_ble import FG_STATE_NAMES

CMH2O = "cmH₂O"
PER_HOUR = "events/h"
BREATHS = "breaths/min"
LPM = UnitOfVolumeFlowRate.LITERS_PER_MINUTE
NEVER = "2000-01-01T00:00:00"


def _get(d: Any, *path: str) -> Any:
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def _feature(feature: str, key: str) -> Callable[[AirSenseData], Any]:
    return lambda d: _get(d.settings, "FeatureProfiles", feature, key)


def _active_profile(d: AirSenseData) -> dict:
    name = _get(d.settings, "ActiveProfiles", "TherapyProfile")
    return _get(d.settings, "TherapyProfiles", name) or {} if name else {}


def _duration_hours(value: str | None) -> float | None:
    """ISO 8601 duration (PT1151642S, P1DT2H3M4S) -> hours."""
    if not value:
        return None
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?", value)
    if not m:
        return None
    d, h, mi, s = (float(x) if x else 0.0 for x in m.groups())
    return round(d * 24 + h + mi / 60 + s / 3600, 1)


def _timestamp(value: str | None) -> dt.datetime | None:
    if not value or value.startswith(NEVER):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _night(fn: Callable[[Any], Any]) -> Callable[[AirSenseData], Any]:
    return lambda d: fn(d.last_night) if d.last_night else None


def _lpm(stats: str, key: str) -> Callable[[AirSenseData], Any]:
    """Summary leak is in L/s; shown in L/min like the machine/myAir."""
    return _night(lambda n: round(v * 60, 1) if (v := getattr(n, stats).get(key)) is not None else None)


def _live(key: str, scale: float = 1.0) -> Callable[[AirSenseData], Any]:
    return lambda d: round(v * scale, 2) if (v := d.live.get(key)) is not None else None


@dataclass(frozen=True, kw_only=True)
class AirSenseSensorDescription(SensorEntityDescription):
    value_fn: Callable[[AirSenseData], Any]
    attrs_fn: Callable[[AirSenseData], dict[str, Any] | None] | None = None
    live: bool = False  # only meaningful while connected


def _stat(key: str, name: str, stats: str, pct: str, unit: str | None, scale_fn=None, **kw) -> AirSenseSensorDescription:
    fn = scale_fn or _night(lambda n: getattr(n, stats).get(pct))
    return AirSenseSensorDescription(
        key=key, translation_key=name, value_fn=fn, native_unit_of_measurement=unit,
        state_class=SensorStateClass.MEASUREMENT, **kw,
    )


D = AirSenseSensorDescription
DIAG = EntityCategory.DIAGNOSTIC

SENSORS: tuple[AirSenseSensorDescription, ...] = (
    # ---- live -----------------------------------------------------------------------------
    # ENUM so the history/timeline card can render standby/therapy/mask-fit as a state track.
    # Every canonical native/wire name, including firmware-only states such as Backup.
    D(
        key="FGState",
        translation_key="fg_state",
        device_class=SensorDeviceClass.ENUM,
        options=list(FG_STATE_NAMES),
        value_fn=lambda d: d.events.get("FGState"),
        live=True,
    ),
    D(key="TestDriveState", translation_key="test_drive_state", value_fn=lambda d: d.events.get("TestDriveState"),
      live=True, entity_category=DIAG),
    D(key="RecoverableError", translation_key="recoverable_error", value_fn=lambda d: d.events.get("RecoverableError"),
      live=True, entity_category=DIAG),
    D(key="SystemError", translation_key="system_error", value_fn=lambda d: d.events.get("SystemError"),
      live=True, entity_category=DIAG),
    D(key="live_mask_pressure", translation_key="live_mask_pressure", value_fn=_live("MaskPressure-100hz"),
      native_unit_of_measurement=CMH2O, state_class=SensorStateClass.MEASUREMENT, live=True, suggested_display_precision=1),
    D(key="live_inspiratory_pressure", translation_key="live_inspiratory_pressure",
      value_fn=_live("InspiratoryPressure-50hz"), native_unit_of_measurement=CMH2O,
      state_class=SensorStateClass.MEASUREMENT, live=True, suggested_display_precision=1),
    D(key="live_set_pressure", translation_key="live_set_pressure", value_fn=_live("SetPressureWithoutCAD"),
      native_unit_of_measurement=CMH2O, state_class=SensorStateClass.MEASUREMENT, live=True, suggested_display_precision=1),
    D(key="live_leak", translation_key="live_leak", value_fn=_live("Leak-50hz", 60), native_unit_of_measurement=LPM,
      state_class=SensorStateClass.MEASUREMENT, live=True, suggested_display_precision=1),
    D(key="live_ramp_remaining", translation_key="live_ramp_remaining", value_fn=_live("RemainingRampTime"),
      native_unit_of_measurement=UnitOfTime.MINUTES, live=True),
    D(key="live_expiratory_pressure", translation_key="live_expiratory_pressure", value_fn=_live("ExpiratoryPressure"),
      native_unit_of_measurement=CMH2O, state_class=SensorStateClass.MEASUREMENT, live=True, suggested_display_precision=1),
    D(key="live_epr_pressure", translation_key="live_epr_pressure", value_fn=_live("EprPressure"),
      native_unit_of_measurement=CMH2O, state_class=SensorStateClass.MEASUREMENT, live=True, suggested_display_precision=1,
      entity_registry_enabled_default=False),
    D(key="live_resp_rate", translation_key="live_resp_rate", value_fn=_live("RespiratoryRate"),
      native_unit_of_measurement=BREATHS, state_class=SensorStateClass.MEASUREMENT, live=True, suggested_display_precision=1),
    D(key="live_tidal_volume", translation_key="live_tidal_volume", value_fn=_live("TidalVolume", 1000),
      native_unit_of_measurement=UnitOfVolume.MILLILITERS, state_class=SensorStateClass.MEASUREMENT, live=True,
      suggested_display_precision=0),  # stream is in L (validated against the daily summary)
    D(key="live_minute_vent", translation_key="live_minute_vent", value_fn=_live("MinuteVentilation"),
      native_unit_of_measurement=LPM, state_class=SensorStateClass.MEASUREMENT, live=True, suggested_display_precision=1),
    D(key="live_flow_limitation", translation_key="live_flow_limitation", value_fn=_live("FlowLimitation"),
      state_class=SensorStateClass.MEASUREMENT, live=True, suggested_display_precision=2),  # 0..1 index
    D(key="live_spo2", translation_key="live_spo2", value_fn=_live("SpO2"), native_unit_of_measurement=PERCENTAGE,
      state_class=SensorStateClass.MEASUREMENT, live=True, entity_registry_enabled_default=False),
    D(key="live_heart_rate", translation_key="live_heart_rate", value_fn=_live("HeartRate"), native_unit_of_measurement="bpm",
      state_class=SensorStateClass.MEASUREMENT, live=True, entity_registry_enabled_default=False),
    D(key="live_motor_speed", translation_key="live_motor_speed", value_fn=_live("MotorSpeed"),
      native_unit_of_measurement=REVOLUTIONS_PER_MINUTE, state_class=SensorStateClass.MEASUREMENT, live=True,
      entity_category=DIAG, suggested_display_precision=0),
    # heated-tube, SpO2/heart-rate and EPR streams are unverified (no heated tube / oximeter / EPR on the test machine)
    D(key="live_heated_tube_power", translation_key="live_heated_tube_power", value_fn=_live("HeatedTubePower"),
      state_class=SensorStateClass.MEASUREMENT, live=True, entity_category=DIAG, entity_registry_enabled_default=False),
    D(key="live_heated_tube_temperature", translation_key="live_heated_tube_temperature",
      value_fn=_live("HeatedTubeTemperature"), state_class=SensorStateClass.MEASUREMENT, live=True, entity_category=DIAG,
      entity_registry_enabled_default=False),
    D(key="humidifier_connected", translation_key="humidifier_connected", live=True,
      value_fn=lambda d: d.events.get("HumidifierConnected")),
    D(key="tube_connected", translation_key="tube_connected", live=True, value_fn=lambda d: d.events.get("TubeConnected")),
    D(key="leak_alert", translation_key="leak_alert", live=True, value_fn=lambda d: d.events.get("Leak"),
      entity_category=DIAG),
    D(key="last_cloud_upload", translation_key="last_cloud_upload", device_class=SensorDeviceClass.TIMESTAMP,
      value_fn=lambda d: (d.last_upload.end or d.last_upload.start) if d.last_upload else None,
      attrs_fn=lambda d: {"successful": d.last_upload.ok, "http_statuses": d.last_upload.http_statuses,
                          "items_sent": d.last_upload.items_sent} if d.last_upload else None),
    D(key="cloud_upload_status", translation_key="cloud_upload_status", entity_category=DIAG,
      value_fn=lambda d: ("ok" if d.last_upload.ok else "failed") if d.last_upload else None),
    D(key="last_soundcheck", translation_key="last_soundcheck", device_class=SensorDeviceClass.TIMESTAMP,
      entity_category=DIAG, value_fn=lambda d: d.last_soundcheck),
    # ---- cellular (4G modem used for AirView/myAir uploads) ---------------------------------------------
    D(key="cellular_data_mode", translation_key="cellular_data_mode", entity_category=DIAG,
      entity_registry_enabled_default=False, value_fn=lambda d: _get(d.cellular, "ConfigurationProfiles", "DataMode")),
    D(key="cellular_apn", translation_key="cellular_apn", entity_category=DIAG, entity_registry_enabled_default=False,
      value_fn=lambda d: _get(d.cellular, "IdentificationProfiles", "CellularProfile", "Network",
                              "GlobalSystemForMobiles", "AccessPointName")),
    D(key="cellular_contact_period", translation_key="cellular_contact_period", entity_category=DIAG,
      entity_registry_enabled_default=False, native_unit_of_measurement=UnitOfTime.HOURS,
      device_class=SensorDeviceClass.DURATION,
      value_fn=lambda d: _duration_hours(_get(d.cellular, "ConfigurationProfiles", "PeriodicBrokerContactPeriod"))),
    # ---- last night (daily summary) ---------------------------------------------------------------
    D(key="night_date", translation_key="night_date", device_class=SensorDeviceClass.TIMESTAMP,
      value_fn=_night(lambda n: n.sessions[0].start if n.sessions else n.day_start),
      attrs_fn=lambda d: {"sessions": [{"start": s.start.isoformat(), "minutes": s.minutes} for s in d.last_night.sessions]}
      if d.last_night else None),
    D(key="usage", translation_key="usage", value_fn=_night(lambda n: n.usage_hours), device_class=SensorDeviceClass.DURATION,
      native_unit_of_measurement=UnitOfTime.HOURS, suggested_display_precision=1),
    D(key="mask_on_count", translation_key="mask_on_count", value_fn=_night(lambda n: n.mask_on_count)),
    D(key="ahi", translation_key="ahi", value_fn=_night(lambda n: n.ahi), native_unit_of_measurement=PER_HOUR,
      state_class=SensorStateClass.MEASUREMENT),
    D(key="ai", translation_key="ai", value_fn=_night(lambda n: n.ai), native_unit_of_measurement=PER_HOUR,
      state_class=SensorStateClass.MEASUREMENT),
    D(key="hi", translation_key="hi", value_fn=_night(lambda n: n.hi), native_unit_of_measurement=PER_HOUR,
      state_class=SensorStateClass.MEASUREMENT),
    D(key="oai", translation_key="oai", value_fn=_night(lambda n: n.oai), native_unit_of_measurement=PER_HOUR,
      state_class=SensorStateClass.MEASUREMENT),
    D(key="cai", translation_key="cai", value_fn=_night(lambda n: n.cai), native_unit_of_measurement=PER_HOUR,
      state_class=SensorStateClass.MEASUREMENT),
    D(key="uai", translation_key="uai", value_fn=_night(lambda n: n.uai), native_unit_of_measurement=PER_HOUR,
      state_class=SensorStateClass.MEASUREMENT),
    D(key="rin", translation_key="rin", value_fn=_night(lambda n: n.rin), native_unit_of_measurement=PER_HOUR,
      state_class=SensorStateClass.MEASUREMENT),
    D(key="csr", translation_key="csr", value_fn=_night(lambda n: n.csr_minutes), device_class=SensorDeviceClass.DURATION,
      native_unit_of_measurement=UnitOfTime.MINUTES, state_class=SensorStateClass.MEASUREMENT,
      entity_registry_enabled_default=False),
    _stat("leak_median", "leak_median", "leak_lps", "median", LPM, _lpm("leak_lps", "median")),
    _stat("leak_p70", "leak_p70", "leak_lps", "p70", LPM, _lpm("leak_lps", "p70"), entity_registry_enabled_default=False),
    _stat("leak_p95", "leak_p95", "leak_lps", "p95", LPM, _lpm("leak_lps", "p95")),
    _stat("leak_max", "leak_max", "leak_lps", "max", LPM, _lpm("leak_lps", "max")),
    _stat("pressure_median", "pressure_median", "mask_pressure", "median", CMH2O),
    _stat("pressure_p95", "pressure_p95", "mask_pressure", "p95", CMH2O),
    _stat("pressure_max", "pressure_max", "mask_pressure", "max", CMH2O),
    _stat("target_pressure_median", "target_pressure_median", "target_pressure", "median", CMH2O),
    _stat("target_pressure_p95", "target_pressure_p95", "target_pressure", "p95", CMH2O),
    _stat("target_pressure_max", "target_pressure_max", "target_pressure", "max", CMH2O,
          entity_registry_enabled_default=False),
    _stat("target_epap_median", "target_epap_median", "target_epap", "median", CMH2O,
          entity_registry_enabled_default=False),
    _stat("target_epap_p95", "target_epap_p95", "target_epap", "p95", CMH2O, entity_registry_enabled_default=False),
    _stat("resp_rate_median", "resp_rate_median", "resp_rate", "median", BREATHS),
    _stat("resp_rate_p95", "resp_rate_p95", "resp_rate", "p95", BREATHS, entity_registry_enabled_default=False),
    _stat("resp_rate_max", "resp_rate_max", "resp_rate", "max", BREATHS, entity_registry_enabled_default=False),
    _stat("tidal_volume_median", "tidal_volume_median", "tidal_volume_l", "median", UnitOfVolume.MILLILITERS,
          _night(lambda n: round(v * 1000) if (v := n.tidal_volume_l.get("median")) is not None else None)),
    _stat("tidal_volume_p95", "tidal_volume_p95", "tidal_volume_l", "p95", UnitOfVolume.MILLILITERS,
          _night(lambda n: round(v * 1000) if (v := n.tidal_volume_l.get("p95")) is not None else None),
          entity_registry_enabled_default=False),
    _stat("minute_vent_median", "minute_vent_median", "minute_vent", "median", LPM),
    _stat("minute_vent_p95", "minute_vent_p95", "minute_vent", "p95", LPM, entity_registry_enabled_default=False),
    _stat("blower_pressure_p5", "blower_pressure_p5", "blower_pressure", "p5", CMH2O, entity_registry_enabled_default=False),
    _stat("blower_pressure_p95", "blower_pressure_p95", "blower_pressure", "p95", CMH2O,
          entity_registry_enabled_default=False),
    _stat("flow_p5", "flow_p5", "flow_lps", "p5", LPM, _lpm("flow_lps", "p5"), entity_registry_enabled_default=False),
    _stat("flow_p95", "flow_p95", "flow_lps", "p95", LPM, _lpm("flow_lps", "p95"), entity_registry_enabled_default=False),
    D(key="blower_flow_median", translation_key="blower_flow_median", native_unit_of_measurement=LPM,
      value_fn=_night(lambda n: round(n.blower_flow_lps * 60, 1) if n.blower_flow_lps is not None else None),
      state_class=SensorStateClass.MEASUREMENT, entity_registry_enabled_default=False),
    D(key="ambient_humidity", translation_key="ambient_humidity", native_unit_of_measurement="mg/L",
      value_fn=_night(lambda n: n.ambient_humidity), state_class=SensorStateClass.MEASUREMENT),
    D(key="humidifier_temperature", translation_key="humidifier_temperature",
      value_fn=_night(lambda n: n.humidifier_temp), native_unit_of_measurement=UnitOfTemperature.CELSIUS,
      device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT),
    D(key="humidifier_power", translation_key="humidifier_power", value_fn=_night(lambda n: n.humidifier_power),
      native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT),
    D(key="heated_tube_temperature", translation_key="heated_tube_temperature",
      value_fn=_night(lambda n: n.heated_tube_temp), native_unit_of_measurement=UnitOfTemperature.CELSIUS,
      device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT,
      entity_registry_enabled_default=False),
    D(key="heated_tube_power", translation_key="heated_tube_power", value_fn=_night(lambda n: n.heated_tube_power),
      native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
      entity_registry_enabled_default=False),
    # ---- therapy settings ---------------------------------------------------------------------
    D(key="therapy_mode", translation_key="therapy_mode", value_fn=lambda d: _active_profile(d).get("TherapyMode")),
    D(key="min_pressure", translation_key="min_pressure", value_fn=lambda d: _active_profile(d).get("MinPressure"),
      native_unit_of_measurement=CMH2O),
    D(key="max_pressure", translation_key="max_pressure", value_fn=lambda d: _active_profile(d).get("MaxPressure"),
      native_unit_of_measurement=CMH2O),
    D(key="set_pressure", translation_key="set_pressure", value_fn=lambda d: _active_profile(d).get("SetPressure"),
      native_unit_of_measurement=CMH2O),
    D(key="start_pressure", translation_key="start_pressure", value_fn=lambda d: _active_profile(d).get("StartPressure"),
      native_unit_of_measurement=CMH2O),
    D(key="epr", translation_key="epr", value_fn=_feature("EprFeature", "EprEnable")),
    D(key="epr_level", translation_key="epr_level", value_fn=_feature("EprFeature", "EprPressure")),
    D(key="epr_type", translation_key="epr_type", value_fn=_feature("EprFeature", "EprType"), entity_category=DIAG),
    D(key="ramp", translation_key="ramp", value_fn=_feature("AutoRampFeature", "RampEnable")),
    D(key="ramp_time", translation_key="ramp_time", value_fn=_feature("AutoRampFeature", "RampTime"),
      native_unit_of_measurement=UnitOfTime.MINUTES),
    D(key="autoset_comfort", translation_key="autoset_comfort", value_fn=_feature("ComfortFeature", "AutoSetComfort")),
    D(key="smart_start", translation_key="smart_start", value_fn=_feature("SmartStartStopFeature", "SmartStart")),
    D(key="smart_stop", translation_key="smart_stop", value_fn=_feature("SmartStartStopFeature", "SmartStop")),
    D(key="mask_type", translation_key="mask_type", value_fn=_feature("CircuitFeature", "MaskType")),
    D(key="tube_type", translation_key="tube_type", value_fn=_feature("CircuitFeature", "TubeType")),
    D(key="antibacterial_filter", translation_key="antibacterial_filter",
      value_fn=_feature("CircuitFeature", "AntiBacterialFilter"), entity_category=DIAG),
    D(key="climate_control", translation_key="climate_control", value_fn=_feature("ClimateFeature", "ClimateControl")),
    D(key="humidifier", translation_key="humidifier", value_fn=_feature("ClimateFeature", "HumidifierSettingEnable")),
    D(key="humidity_level", translation_key="humidity_level", value_fn=_feature("ClimateFeature", "HumidifierLevel")),
    D(key="heated_tube", translation_key="heated_tube", value_fn=_feature("ClimateFeature", "HeatedTubeSettingEnable")),
    D(key="tube_temperature", translation_key="tube_temperature",
      value_fn=_feature("ClimateFeature", "HeatedTubeTemperature"), native_unit_of_measurement=UnitOfTemperature.CELSIUS,
      device_class=SensorDeviceClass.TEMPERATURE),
    D(key="mask_sense", translation_key="mask_sense", value_fn=_feature("MaskSenseFeature", "MaskSenseToggle"),
      entity_category=DIAG),
    D(key="soundcheck", translation_key="soundcheck", value_fn=_feature("DeviceHealthFeature", "SoundcheckRunFrequency"),
      entity_category=DIAG),
    D(key="language", translation_key="language", value_fn=_feature("LanguageFeature", "Language"), entity_category=DIAG),
    D(key="temperature_unit", translation_key="temperature_unit", value_fn=_feature("TemperatureFeature", "TemperatureUnit"),
      entity_category=DIAG),
    D(key="timezone", translation_key="timezone", value_fn=_feature("TimeZoneFeature", "TimeZoneOffset"),
      entity_category=DIAG),
    D(key="settings_changed", translation_key="settings_changed", device_class=SensorDeviceClass.TIMESTAMP,
      value_fn=lambda d: _timestamp(_get(d.settings, "Attributes", "AppliedDateTime")), entity_category=DIAG),
    *(
        D(key=f"reminder_{part.lower()}", translation_key=f"reminder_{part.lower()}", entity_category=DIAG,
          value_fn=(lambda p: lambda d: _get(d.settings, "FeatureProfiles", "ReminderFeature", f"Reminder{p}", "Enable"))(part),
          attrs_fn=(lambda p: lambda d: {
              "start": _get(d.settings, "FeatureProfiles", "ReminderFeature", f"Reminder{p}", "StartDateTime"),
              "period": _get(d.settings, "FeatureProfiles", "ReminderFeature", f"Reminder{p}", "Period"),
          })(part))
        for part in ("Mask", "Tubing", "Filter", "Humidifier")
    ),
    # ---- run meters ---------------------------------------------------------------------------
    D(key="therapy_hours", translation_key="therapy_hours", value_fn=lambda d: _duration_hours(d.metrics.get("TherapyRunMeter")),
      native_unit_of_measurement=UnitOfTime.HOURS, device_class=SensorDeviceClass.DURATION,
      state_class=SensorStateClass.TOTAL_INCREASING),
    D(key="motor_hours", translation_key="motor_hours", value_fn=lambda d: _duration_hours(d.metrics.get("MotorRunMeter")),
      native_unit_of_measurement=UnitOfTime.HOURS, device_class=SensorDeviceClass.DURATION,
      state_class=SensorStateClass.TOTAL_INCREASING, entity_category=DIAG),
    D(key="machine_hours", translation_key="machine_hours", value_fn=lambda d: _duration_hours(d.metrics.get("MachineRunMeter")),
      native_unit_of_measurement=UnitOfTime.HOURS, device_class=SensorDeviceClass.DURATION,
      state_class=SensorStateClass.TOTAL_INCREASING, entity_category=DIAG),
    D(key="motor_hours_since_service", translation_key="motor_hours_since_service",
      value_fn=lambda d: _duration_hours(d.metrics.get("MotorRunSinceLastServiceMeter")),
      native_unit_of_measurement=UnitOfTime.HOURS, device_class=SensorDeviceClass.DURATION, entity_category=DIAG),
    D(key="last_therapy", translation_key="last_therapy", device_class=SensorDeviceClass.TIMESTAMP,
      value_fn=lambda d: _timestamp(d.metrics.get("LastTherapyUseDateTime"))),
    D(key="last_service", translation_key="last_service", device_class=SensorDeviceClass.TIMESTAMP,
      value_fn=lambda d: _timestamp(d.metrics.get("LastMachineServiceDateTime")), entity_category=DIAG),
    D(key="last_data_erase", translation_key="last_data_erase", device_class=SensorDeviceClass.TIMESTAMP,
      value_fn=lambda d: _timestamp(d.metrics.get("LastEraseDataDateTime")), entity_category=DIAG,
      entity_registry_enabled_default=False),
    # ---- versions ------------------------------------------------------------------------------
    D(key="bluetooth_firmware", translation_key="bluetooth_firmware", entity_category=DIAG,
      value_fn=lambda d: _get(d.version, "BluetoothModule", "IdentificationProfiles", "Software", "ApplicationIdentifier")),
    D(key="cellular_modem", translation_key="cellular_modem", entity_category=DIAG, entity_registry_enabled_default=False,
      value_fn=lambda d: _get(d.version, "CellularModule", "IdentificationProfiles", "CellularProfile", "Equipment",
                              "Software", "ApplicationIdentifier")),
    D(key="bootloader", translation_key="bootloader", entity_category=DIAG, entity_registry_enabled_default=False,
      value_fn=lambda d: _get(d.identity, "Software", "BootloaderIdentifier")),
    D(key="configuration", translation_key="configuration", entity_category=DIAG, entity_registry_enabled_default=False,
      value_fn=lambda d: _get(d.identity, "Software", "ConfigurationIdentifier")),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: AirSenseConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(AirSenseSensor(entry.runtime_data, d) for d in SENSORS)


class AirSenseSensor(AirSenseEntity, SensorEntity):
    """Live values need the link; stored values (settings, last night, meters) stay valid while disconnected."""

    entity_description: AirSenseSensorDescription

    @property
    def available(self) -> bool:
        if self.entity_description.live and not self.coordinator.connected:
            return False
        return self.native_value is not None

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator.data) if fn else None
