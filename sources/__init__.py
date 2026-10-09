"""
Sources: where readings come from.

Each .py file in this folder is one source: a way of hearing the sensors, such
as the AcuRite Access hub, or a radio dongle decoding them off the air. A source
turns whatever it hears into Readings and hands each one to emit(). Nothing
above a source sees what the source heard, only Readings, so a new kind of
station needs only a new file here.

A source module provides:

    NAME = "short-name"
    def enabled(cfg) -> bool      # from config.ini; is this source switched on?
    def run(cfg, emit) -> None    # runs forever in its own thread, calling emit(reading)

emit(reading) records the reading and returns the sensor's entry in
current.json (a dict with name, type and fields), which a source may log.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# Sensor types that measure outdoors; every other type is a room.
OUTDOOR_TYPES = ("Atlas", "Iris", "5N1")

# Every field a reading can carry, in fixed units. A source converts to these;
# a value it doesn't have is left out. These are also the database's columns.
FIELDS = {
    "temp_f": "temperature, °F",
    "humidity_pct": "relative humidity, %",
    "dew_point_f": "dew point, °F",
    "heat_index_f": "heat index, °F",
    "feels_like_f": "feels-like temperature, °F",
    "wind_chill_f": "wind chill, °F",
    "wind_mph": "wind speed, mph",
    "wind_avg_mph": "average wind speed, mph",
    "wind_gust_mph": "wind gust, mph",
    "wind_dir_deg": "wind direction it blows from, degrees",
    "wind_gust_dir_deg": "gust direction, degrees",
    "rain_hour_in": "rain in the last 60 minutes, inches",
    "rain_day_in": "rain since local midnight, inches",
    "pressure_inhg": "barometric pressure, inHg",
    "uv_index": "UV index",
    "light_lux": "light intensity, lux",
    "light_seconds": "seconds of measured light",
    "strike_count": "lightning strikes counted",
    "last_strike_mi": "distance to the last strike, miles",
    "last_strike_ts": "time of the last strike",
    "interference": "lightning sensor interference",
    "battery": "sensor battery: normal or low",
    "signal": "signal strength",
    "hub_battery": "hub battery",
}


@dataclass
class Reading:
    source: str                # which source heard it, e.g. "acurite-access"
    sensor_id: str
    type: str | None           # the sensor kind: Atlas, tower, ...
    received_utc: str          # when it reached us, e.g. 2026-10-08T18:17:23Z
    raw: str                   # what the source heard, exactly, so it can be decoded again
    fields: dict = field(default_factory=dict)   # FIELDS names → values
    receiver: str | None = None    # the hub or radio that heard it
    sensor_utc: str | None = None  # the time the sensor or hub gave the reading, if any


def derive(fields: dict) -> dict:
    """Fill in dew point, heat index, wind chill and feels-like from temperature,
    humidity and wind, where the source didn't send them (the hub works these out
    itself; a sensor heard by radio sends only what it measures). NWS formulas."""
    t, rh, v = fields.get("temp_f"), fields.get("humidity_pct"), fields.get("wind_mph")
    if not isinstance(t, (int, float)):
        return fields
    out = dict(fields)
    if isinstance(rh, (int, float)) and rh > 0:
        c = (t - 32) * 5 / 9
        g = math.log(rh / 100) + 17.625 * c / (243.04 + c)
        out.setdefault("dew_point_f", round(243.04 * g / (17.625 - g) * 9 / 5 + 32, 1))
        hi = 0.5 * (t + 61 + (t - 68) * 1.2 + rh * 0.094)
        if (hi + t) / 2 >= 80:
            hi = (-42.379 + 2.04901523 * t + 10.14333127 * rh - 0.22475541 * t * rh
                  - 6.83783e-3 * t * t - 5.481717e-2 * rh * rh + 1.22874e-3 * t * t * rh
                  + 8.5282e-4 * t * rh * rh - 1.99e-6 * t * t * rh * rh)
            if rh < 13 and 80 <= t <= 112:
                hi -= (13 - rh) / 4 * math.sqrt((17 - abs(t - 95)) / 17)
            elif rh > 85 and 80 <= t <= 87:
                hi += (rh - 85) / 10 * (87 - t) / 5
        out.setdefault("heat_index_f", round(hi, 1))
    chill = None
    if isinstance(v, (int, float)) and t <= 50 and v >= 3:
        chill = round(35.74 + 0.6215 * t - 35.75 * v ** 0.16 + 0.4275 * t * v ** 0.16, 1)
        out.setdefault("wind_chill_f", chill)
    if chill is not None:
        out.setdefault("feels_like_f", chill)
    elif "heat_index_f" in out and t >= 80:
        out.setdefault("feels_like_f", out["heat_index_f"])
    elif isinstance(v, (int, float)) or isinstance(rh, (int, float)):
        out.setdefault("feels_like_f", t)
    return out
