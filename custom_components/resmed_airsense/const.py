"""Constants for the ResMed AirSense integration."""
from __future__ import annotations

DOMAIN = "resmed_airsense"

CONF_CLIENT_ID = "client_id"
CONF_MASTER_PAIR_KEY = "master_pair_key"
CONF_PASSKEY = "passkey"
CONF_SERIAL = "serial"

# fired on the bus when FGState leaves a therapy state (AutoStop = mask removed, or end of night)
EVENT_THERAPY_STOPPED = f"{DOMAIN}_therapy_stopped"

KEEPALIVE_INTERVAL = 60  # s; Get FGState to detect half-open links
SETTINGS_INTERVAL = 900  # s; SettingProfiles + MachineMetrics
SUMMARY_INTERVAL = 3600  # s; daily summary spool (also re-read 1 min after therapy stops)
SUMMARY_RETRY = 120  # s; retry a failed/partial spool read sooner
SPOOL_ATTEMPTS = 3  # immediate retries of a spool read (lost notification on proxies)
SUMMARY_DAYS = 3650  # read back far enough to cover the device's full Summary retention
# APK SpoolAddress.kt (acg.java) — complete set for diagnostics capture
DIAGNOSTIC_SPOOL_NAMES: tuple[str, ...] = (
    "CellularActivityEvents",
    "DiagnosticExceptionEvents",
    "DiagnosticTenMinutePeriodic",
    "GUIActivityEvents",
    "MachineMetrics",
    "MemoryMetrics",
    "SettingProfilesCollection",
    "SoundcheckVector",
    "Summary",
    "SurveyEvents",
    "SystemActivityEvents",
    "SystemExceptionEvents",
    "TherapyEvents",
    "TherapyOneMinutePeriodic",
    "UsageEvents",
)
# Cap each diagnostics spool pull so a huge blob cannot blow the download.
DIAGNOSTIC_SPOOL_BYTE_CAP = 262_144  # 256 KiB
DIAGNOSTIC_SPOOL_DAYS = 30
# Proxy paths drop mid-spool under load; TOMP needs a longer gap than the default 5s.
DIAGNOSTIC_SPOOL_IDLE_TIMEOUT = 20.0


# pushed on change (the device answers valid per id, so unknown ones are harmless on other models)
EVENT_IDS = (
    "FGState", "TestDriveState", "RecoverableError", "SystemError", "Leak",
    "HumidifierConnected", "TubeConnected", "Summary-HumidifierConnected", "Summary-TubeConnected",
    "HumidifierLevel", "HeatedTubeTemperature", "RemainingRampTime", "SmartStart", "SmartStop",
    "ActiveTherapyProfile", "Language", "TimeZoneOffset",
)
# events that mean a setting changed on the machine -> re-read SettingProfiles
SETTING_EVENTS = frozenset({"HumidifierLevel", "HeatedTubeTemperature", "SmartStart", "SmartStop",
                            "ActiveTherapyProfile", "Language", "TimeZoneOffset"})

# live values streamed while the blower runs (Leak-TwoSecond is reported invalid on the AirSense 11;
# HumidifierPower is accepted but always 0, so it is left out)
STREAM_IDS = (
    "MaskPressure-100hz", "InspiratoryPressure-50hz", "ExpiratoryPressure", "EprPressure", "SetPressureWithoutCAD",
    "Leak-50hz", "RespiratoryRate", "TidalVolume", "MinuteVentilation", "FlowLimitation", "SpO2", "HeartRate",
    "MotorSpeed", "HeatedTubePower", "HeatedTubeTemperature", "RemainingRampTime",
)
STREAM_REPORT_MS = 5000
RECONNECT_BACKOFF = (5, 10, 30, 60, 120)  # s
# a real rejection repeats on every attempt; asking the user for a new screen code needs it twice in a row
AUTH_REJECTIONS_FOR_REAUTH = 2
