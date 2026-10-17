"""Publish multi-night Summary spool values as Home Assistant long-term statistics.

The Summary spool already returns one record per therapy day (noon→noon). Last-night
entities only surface the most recent used day; this module feeds every device-held day
into the recorder so a statistics-graph card can chart every numeric Summary field
(usage, AHI family, leak, pressures, ventilation, humidifier, …) across nights. A night
with no therapy usage is imported as a 0.0 usage point so no held night is dropped.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .resmed_air_ble.summary import DailySummary

_LOGGER = logging.getLogger(__name__)

# statistic_id object portion must be a slug: [a-z0-9_]+
_SLUG_RE = re.compile(r"[^a-z0-9_]+")


def _slug(value: str) -> str:
    return _SLUG_RE.sub("_", value.lower()).strip("_") or "device"


def _hour_start(when: datetime) -> datetime:
    """Recorder external statistics must start on an hour boundary (UTC)."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    else:
        when = when.astimezone(UTC)
    return when.replace(minute=0, second=0, microsecond=0)


def _metadata(statistic_id: str, name: str, unit: str | None, *, has_mean: bool) -> dict[str, Any]:
    """Build StatisticMetaData compatible with HA 2025.1 (has_mean) and 2026.x (mean_type)."""
    meta: dict[str, Any] = {
        "has_mean": has_mean,
        "has_sum": False,
        "name": name,
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": unit,
    }
    try:
        from homeassistant.components.recorder.models import StatisticMeanType

        meta["mean_type"] = StatisticMeanType.ARITHMETIC if has_mean else StatisticMeanType.NONE
        meta["unit_class"] = None
    except Exception:  # older HA without mean_type
        pass
    return meta


Extractor = Callable[[DailySummary], float | None]


def _dict_stat(attr: str, key: str, scale: float = 1.0) -> Extractor:
    """Extractor for one percentile key of a DailySummary dict attribute."""

    def extract(day: DailySummary) -> float | None:
        stats: dict[str, float] = getattr(day, attr)
        if key not in stats:
            return None
        return round(stats[key] * scale, 2)

    return extract


def _scalar(attr: str, scale: float = 1.0) -> Extractor:
    """Extractor for a scalar DailySummary attribute."""

    def extract(day: DailySummary) -> float | None:
        value = getattr(day, attr)
        if value is None:
            return None
        return round(value * scale, 2)

    return extract


# (id suffix, display name, unit of measurement, extractor) for every numeric Summary
# field. Leak/flow rates are stored in L/s and converted to L/min to match leak_p95.
_SERIES: tuple[tuple[str, str, str | None, Extractor], ...] = (
    ("usage", "CPAP usage", "h", lambda d: d.usage_hours),
    ("ahi", "CPAP AHI", "events/h", _scalar("ahi")),
    ("ai", "CPAP apnea index", "events/h", _scalar("ai")),
    ("hi", "CPAP hypopnea index", "events/h", _scalar("hi")),
    ("oai", "CPAP obstructive apnea index", "events/h", _scalar("oai")),
    ("cai", "CPAP central apnea index", "events/h", _scalar("cai")),
    ("uai", "CPAP unclassified apnea index", "events/h", _scalar("uai")),
    ("rin", "CPAP RERA index", "events/h", _scalar("rin")),
    ("csr_minutes", "CPAP CSR duration", "min", _scalar("csr_minutes")),
    ("mask_events", "CPAP mask events", "events", _scalar("mask_events")),
    ("leak_median", "CPAP leak median", "L/min", _dict_stat("leak_lps", "median", 60.0)),
    ("leak_p70", "CPAP leak 70th", "L/min", _dict_stat("leak_lps", "p70", 60.0)),
    ("leak_p95", "CPAP leak 95th", "L/min", _dict_stat("leak_lps", "p95", 60.0)),
    ("leak_max", "CPAP leak max", "L/min", _dict_stat("leak_lps", "max", 60.0)),
    ("target_pressure_median", "CPAP target pressure median", "cmH₂O", _dict_stat("target_pressure", "median")),
    ("pressure_p95", "CPAP pressure 95th", "cmH₂O", _dict_stat("target_pressure", "p95")),
    ("target_pressure_max", "CPAP target pressure max", "cmH₂O", _dict_stat("target_pressure", "max")),
    ("target_epap_median", "CPAP target EPAP median", "cmH₂O", _dict_stat("target_epap", "median")),
    ("target_epap_p95", "CPAP target EPAP 95th", "cmH₂O", _dict_stat("target_epap", "p95")),
    ("target_epap_max", "CPAP target EPAP max", "cmH₂O", _dict_stat("target_epap", "max")),
    ("mask_pressure_median", "CPAP mask pressure median", "cmH₂O", _dict_stat("mask_pressure", "median")),
    ("mask_pressure_p95", "CPAP mask pressure 95th", "cmH₂O", _dict_stat("mask_pressure", "p95")),
    ("mask_pressure_max", "CPAP mask pressure max", "cmH₂O", _dict_stat("mask_pressure", "max")),
    ("tidal_volume_median", "CPAP tidal volume median", "L", _dict_stat("tidal_volume_l", "median")),
    ("tidal_volume_p95", "CPAP tidal volume 95th", "L", _dict_stat("tidal_volume_l", "p95")),
    ("tidal_volume_max", "CPAP tidal volume max", "L", _dict_stat("tidal_volume_l", "max")),
    ("minute_vent_median", "CPAP minute ventilation median", "L/min", _dict_stat("minute_vent", "median")),
    ("minute_vent_p95", "CPAP minute ventilation 95th", "L/min", _dict_stat("minute_vent", "p95")),
    ("minute_vent_max", "CPAP minute ventilation max", "L/min", _dict_stat("minute_vent", "max")),
    ("respiratory_rate_median", "CPAP respiratory rate median", "breaths/min", _dict_stat("resp_rate", "median")),
    ("respiratory_rate_p95", "CPAP respiratory rate 95th", "breaths/min", _dict_stat("resp_rate", "p95")),
    ("respiratory_rate_max", "CPAP respiratory rate max", "breaths/min", _dict_stat("resp_rate", "max")),
    ("blower_pressure_p5", "CPAP blower pressure 5th", "cmH₂O", _dict_stat("blower_pressure", "p5")),
    ("blower_pressure_p95", "CPAP blower pressure 95th", "cmH₂O", _dict_stat("blower_pressure", "p95")),
    ("flow_p5", "CPAP flow 5th", "L/min", _dict_stat("flow_lps", "p5", 60.0)),
    ("flow_p95", "CPAP flow 95th", "L/min", _dict_stat("flow_lps", "p95", 60.0)),
    ("blower_flow_median", "CPAP blower flow median", "L/min", _scalar("blower_flow_lps", 60.0)),
    ("ambient_humidity", "CPAP ambient humidity", "mg/L", _scalar("ambient_humidity")),
    ("humidifier_temperature", "CPAP humidifier temperature", "°C", _scalar("humidifier_temp")),
    ("heated_tube_temperature", "CPAP heated tube temperature", "°C", _scalar("heated_tube_temp")),
    ("humidifier_power", "CPAP humidifier power", "%", _scalar("humidifier_power")),
    ("heated_tube_power", "CPAP heated tube power", "%", _scalar("heated_tube_power")),
)


def build_summary_statistic_rows(
    serial: str, days: Iterable[DailySummary]
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Return (metadata, rows) jobs: one point per night for every numeric Summary field.

    Constructed for unit tests too. A device-held night without therapy usage contributes
    only its zero usage so every night the device holds appears in the usage series.
    """
    slug = _slug(serial)
    series = [(f"{DOMAIN}:{slug}_{suffix}", name, unit, extract) for suffix, name, unit, extract in _SERIES]
    usage_sid = f"{DOMAIN}:{slug}_usage"
    # One sample per night, placed on the hour of day_end (end of the noon→noon therapy day).
    # Avoid colliding two nights into the same hour by shifting left when needed.
    starts: list[tuple[DailySummary, datetime]] = []
    used_hours: set[datetime] = set()
    for day in days:
        start = _hour_start(day.day_end - timedelta(seconds=1))
        while start in used_hours:
            start -= timedelta(hours=1)
        used_hours.add(start)
        starts.append((day, start))
    rows: dict[str, list[dict[str, Any]]] = {sid: [] for sid, _, _, _ in series}
    for day, start in starts:
        for sid, _name, _unit, extract in series:
            if sid != usage_sid and not day.usage_minutes:
                continue  # a night without therapy carries only its zero usage
            value = extract(day)
            if value is None:
                continue
            rows[sid].append({"start": start, "mean": float(value), "state": float(value)})
    jobs: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for sid, name, unit, _ in series:
        if rows[sid]:
            jobs.append((_metadata(sid, name, unit, has_mean=True), rows[sid]))
    return jobs


def async_import_summary_statistics(hass: HomeAssistant, serial: str, days: list[DailySummary]) -> None:
    """Queue external long-term statistics for every used Summary day."""
    from homeassistant.components.recorder.statistics import async_add_external_statistics

    jobs = build_summary_statistic_rows(serial, days)
    if not jobs:
        return
    for metadata, rows in jobs:
        async_add_external_statistics(hass, metadata, rows)  # type: ignore[arg-type]
        _LOGGER.debug("queued %d night(s) for %s", len(rows), metadata["statistic_id"])
