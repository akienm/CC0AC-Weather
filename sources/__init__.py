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

from dataclasses import dataclass, field

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
