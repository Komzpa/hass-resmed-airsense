"""Publish multi-night Summary spool values as Home Assistant long-term statistics.

The Summary spool already returns one record per therapy day (noon→noon). Last-night
entities only surface the most recent used day; this module feeds every used day into
the recorder so a statistics-graph card can chart usage / AHI / leak p95 / pressure p95
across nights.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
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


def build_summary_statistic_rows(
    serial: str, days: Iterable[DailySummary]
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Return (metadata, rows) jobs for used therapy nights. Constructed for unit tests too."""
    slug = _slug(serial)
    series: list[tuple[str, str, str | None, list[dict[str, Any]]]] = [
        (f"{DOMAIN}:{slug}_usage", "CPAP usage", "h", []),
        (f"{DOMAIN}:{slug}_ahi", "CPAP AHI", "events/h", []),
        (f"{DOMAIN}:{slug}_leak_p95", "CPAP leak 95th", "L/min", []),
        (f"{DOMAIN}:{slug}_pressure_p95", "CPAP pressure 95th", "cmH₂O", []),
    ]
    # One sample per night, placed on the hour of day_end (end of the noon→noon therapy day).
    # Avoid colliding two nights into the same hour by shifting left when needed.
    used_hours: set[datetime] = set()
    for day in days:
        if not day.usage_minutes:
            continue
        start = _hour_start(day.day_end - timedelta(seconds=1))
        while start in used_hours:
            start -= timedelta(hours=1)
        used_hours.add(start)
        values = (
            day.usage_hours,
            day.ahi,
            (round(day.leak_lps["p95"] * 60, 2) if "p95" in day.leak_lps else None),
            day.target_pressure.get("p95"),
        )
        for (_, _, _, rows), value in zip(series, values, strict=True):
            if value is None:
                continue
            rows.append({"start": start, "mean": float(value), "state": float(value)})
    jobs: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for statistic_id, name, unit, rows in series:
        if not rows:
            continue
        jobs.append((_metadata(statistic_id, name, unit, has_mean=True), rows))
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
