"""Conversions between Home Assistant volume and the Nubert volume byte."""

from __future__ import annotations

from math import ceil, floor

from . import protocol as p
from .const import DEFAULT_MAX_VOLUME_DB, DEFAULT_MIN_VOLUME_DB


def supported_db_range(model: str | None) -> tuple[int, int]:
    raw_max, offset, _ = p.volume_range(model)
    return -offset, raw_max - offset


def default_db_range(model: str | None) -> tuple[int, int]:
    low, high = supported_db_range(model)
    minimum = max(low, DEFAULT_MIN_VOLUME_DB)
    maximum = min(high, DEFAULT_MAX_VOLUME_DB)
    return (minimum, maximum) if minimum < maximum else (low, high)


def valid_settings(
    minimum: float, maximum: float, gamma: float, model: str | None
) -> bool:
    low, high = supported_db_range(model)
    return low <= minimum < maximum <= high and 0.5 <= gamma <= 3.0


def level_to_raw(
    level: float, minimum: float, maximum: float, gamma: float, model: str | None
) -> int:
    raw_max, offset, _ = p.volume_range(model)
    level = max(0.0, min(1.0, level))
    raw = round(minimum + (maximum - minimum) * level**gamma + offset)
    return max(
        0,
        min(raw_max, max(ceil(minimum + offset), min(floor(maximum + offset), raw))),
    )


def raw_to_level(
    raw: int, minimum: float, maximum: float, gamma: float, model: str | None
) -> float:
    raw_max, offset, _ = p.volume_range(model)
    db = max(0, min(raw_max, raw)) - offset
    fraction = max(0.0, min(1.0, (db - minimum) / (maximum - minimum)))
    return fraction ** (1 / gamma)
