"""Diagnostics download: everything the coordinator knows, with keys and identifiers redacted."""
from __future__ import annotations

import base64
import dataclasses
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import AirSenseConfigEntry
from .const import CONF_CLIENT_ID, CONF_MASTER_PAIR_KEY, CONF_SERIAL
from .resmed_air_ble.summary import _fields

TO_REDACT = {
    CONF_MASTER_PAIR_KEY,
    CONF_CLIENT_ID,
    CONF_SERIAL,
    "SerialNumber",
    "UniversalIdentifier",
    "HardwareIdentifier",
    "IMEI",
    "ServiceHost",
}


def _protobuf_json_envelope(raw: bytes) -> dict[str, Any]:
    """Describe top-level protobuf wire fields without claiming their application schema."""
    fields: list[dict[str, Any]] = []
    try:
        for number, value in _fields(raw):
            if isinstance(value, int):
                fields.append({"field": number, "kind": "varint", "value": value})
            else:
                fields.append(
                    {
                        "field": number,
                        "kind": "bytes",
                        "length": len(value),
                        "hex_prefix": value[:24].hex(),
                    }
                )
    except (IndexError, ValueError) as err:
        return {
            "format": "protobuf-wire-fields",
            "parsed": False,
            "error": str(err),
            "fields": fields,
        }
    return {"format": "protobuf-wire-fields", "parsed": True, "fields": fields}




async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: AirSenseConfigEntry) -> dict[str, Any]:
    c = entry.runtime_data
    d = c.data
    night = dataclasses.asdict(d.last_night) if d.last_night else None
    if night:
        night.pop("raw", None)
    capture = None
    if c.therapy_one_minute_raw is not None:
        capture = {
            "address": "TherapyOneMinutePeriodic",
            "requested_from": c.therapy_one_minute_start.isoformat() if c.therapy_one_minute_start else None,
            "raw_length": len(c.therapy_one_minute_raw),
            "raw_base64": base64.b64encode(c.therapy_one_minute_raw).decode("ascii"),
            "envelope": _protobuf_json_envelope(c.therapy_one_minute_raw),
        }
    # Live pull of every APK spool name (one at a time, byte-capped); errors stay per-spool.
    all_spools = await c.async_capture_all_spools()
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "connected": c.connected,
        "events": d.events,
        "live": d.live,
        "settings": d.settings,
        "metrics": d.metrics,
        "identity": async_redact_data(d.identity, TO_REDACT),
        "version": async_redact_data(d.version, TO_REDACT),
        "cellular": async_redact_data(d.cellular, TO_REDACT),
        "last_night": night,
        # raw field numbers of every stored day, to help decode the fields that are still unknown
        "days_raw": [{str(k): v for k, v in day.raw.items() if isinstance(v, int)} for day in d.days],
        "therapy_one_minute_capture": capture,
        "all_spools_capture": all_spools,
    }
