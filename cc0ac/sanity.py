"""
Sanity checks: a reading no real weather could produce is held back, not believed.

Sensors fail in ways that look like weather. A dying one, pushed by heat or
cold, reports far too high or too low for a while; a glitch somewhere between
the sensor and the server once showed 200 mph of wind for five minutes in the
middle of the night. AcuRite passes these on. Here each measured field of each
reading is checked as it arrives, and one that fails is held back:

- outside what any station on earth reads (LIMITS);
- a jump from the last believed value faster than weather moves (STEADY: a
  little at once, so much a minute for the first ten minutes, then so much an
  hour). Once held, a field stays held until it reads near the last believed
  value again, or enough time has passed for weather to have got there;
- an outdoor temperature far from the middle of the other outdoor sensors'
  ([sanity] reference_f), when there are at least two others to ask (the
  online weather counts as one; with only one other, there is no telling
  which of the two is wrong);
- an outdoor reading that has not changed at all for hours, as a frozen sensor
  does ([sanity] stuck_hours).

A held-back value is kept, not deleted: it is taken out of the reading's fields
and put in reading.suspect with the reason, the database keeps it in its
`suspect` column, and the raw logs keep what the sensor sent. Since it is not in
the fields, nothing counts it: not the records, the daily summaries, the alerts,
the uploads or MQTT. The dashboard shows the last believed value and says what
was held back; the charts show held-back values faded; the status page counts
them. Dew point, feels-like, heat index and wind chill worked out from a held
value are held with it.
"""

from __future__ import annotations

import configparser
import logging
import statistics
from typing import Callable

from cc0ac import units

log = logging.getLogger("acurite")

# What no station reads: (lowest, highest), in the stored units.
LIMITS = {
    "temp_f": (-80, 150), "humidity_pct": (0, 100), "pressure_inhg": (25, 32.5),
    "wind_mph": (0, 120), "wind_avg_mph": (0, 120), "wind_gust_mph": (0, 160),
    "wind_dir_deg": (0, 360), "wind_gust_dir_deg": (0, 360),
    "rain_hour_in": (0, 15), "rain_day_in": (0, 50), "uv_index": (0, 20), "light_lux": (0, 250000),
    "last_strike_mi": (0, 40),
}
# How fast weather moves: (at once, a minute for the first ten minutes, an hour after).
# Rain is only checked going up: it falls back to nothing at midnight.
STEADY = {
    "temp_f": (4, 1.0, 15), "humidity_pct": (15, 3, 40), "pressure_inhg": (0.04, 0.01, 0.15),
    "rain_hour_in": (0.5, 0.2, 6), "rain_day_in": (0.5, 0.2, 6),
}
# Outdoor fields that a working sensor never leaves exactly alone for hours
# (humidity sits at 100% through fog and rain, so it isn't one).
STIRS = ("temp_f",)
DERIVED = ("dew_point_f", "heat_index_f", "wind_chill_f", "feels_like_f")
WORKED_FROM = ("temp_f", "humidity_pct", "wind_mph")
KIND = {"temp_f": "temperature", "pressure_inhg": "pressure", "rain_hour_in": "rain", "rain_day_in": "rain",
        "wind_mph": "wind", "wind_avg_mph": "wind", "wind_gust_mph": "wind", "last_strike_mi": "distance"}
SUFFIX = {"humidity_pct": "%", "wind_dir_deg": "°", "wind_gust_dir_deg": "°", "uv_index": " UV", "light_lux": " lux"}
NAMES = {"temp_f": "temperature", "humidity_pct": "humidity", "pressure_inhg": "pressure", "wind_mph": "wind",
         "wind_avg_mph": "average wind", "wind_gust_mph": "gust", "wind_dir_deg": "wind direction",
         "wind_gust_dir_deg": "gust direction", "rain_hour_in": "rain this hour", "rain_day_in": "rain today",
         "uv_index": "UV", "light_lux": "light", "last_strike_mi": "strike distance"}


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def allowed(name: str, minutes: float) -> float:
    """How far weather can move a field in that many minutes."""
    at_once, a_minute, an_hour = STEADY[name]
    return at_once + a_minute * min(minutes, 10) + an_hour * max(0.0, minutes - 10) / 60


class Watch:
    """The checks, and what they remember: per sensor and field, the last value
    believed and when, what is held now, and how long a value has stood still.

    recall(sensor_id) → {field: (value, epoch)}: the last believed values from
    before this program started, asked once per sensor, so a restart doesn't
    believe whatever comes first."""

    def __init__(self, cfg: configparser.ConfigParser, recall: Callable[[str], dict] | None = None):
        self.on = cfg.getboolean("sanity", "enabled", fallback=True)
        self.reference_f = cfg.getfloat("sanity", "reference_f", fallback=20)
        self.stuck_s = cfg.getfloat("sanity", "stuck_hours", fallback=4) * 3600
        self.shown_in = units.chosen(cfg)
        self.recall = recall
        self.believed: dict[str, dict[str, tuple[float, float]]] = {}
        self.held: dict[str, dict[str, int]] = {}       # sensor → field → readings held in a row
        self.still: dict[str, dict[str, tuple[float, float]]] = {}   # sensor → field → (value, since)

    def _show(self, name: str, v: float) -> str:
        if name in KIND:
            return units.show(KIND[name], v, self.shown_in)
        return f"{v:g}{SUFFIX.get(name, '')}"

    def _why(self, sensor_id: str, name: str, v: float, at: float, outdoor: bool,
             reference: float | None) -> str | None:
        lo, hi = LIMITS.get(name, (None, None))
        if lo is not None and not lo <= v <= hi:
            return f"{self._show(name, v)} is past anything a station reads"
        last = self.believed.get(sensor_id, {}).get(name)
        if name in STEADY and last is not None:
            was, then = last
            minutes = max(0.0, (at - then) / 60)
            jump = v - was
            if (jump if name.startswith("rain_") else abs(jump)) > allowed(name, minutes):
                return f"jumped from {self._show(name, was)} to {self._show(name, v)} in {minutes:.0f} min"
        if name == "temp_f" and outdoor and reference is not None and abs(v - reference) > self.reference_f:
            return f"{self._show(name, v)}, while the other outdoor sensors read {self._show(name, reference)}"
        if name in STIRS and outdoor:
            value, since = self.still.setdefault(sensor_id, {}).get(name, (None, at))
            if value != v:
                self.still[sensor_id][name] = (v, at)
            elif at - since >= self.stuck_s:
                return f"unchanged at {self._show(name, v)} for {(at - since) / 3600:.0f} h"
        return None

    def check(self, sensor_id: str, fields: dict, at: float, outdoor: bool = False,
              reference: float | None = None) -> tuple[dict, dict]:
        """(the fields believed, {field: {"value", "why"}} held back) for one reading.
        reference: the other outdoor sensors' temperature now, for an outdoor one."""
        if not self.on:
            return fields, {}
        asked, sensor_id = sensor_id, sensor_id.lower()
        if sensor_id not in self.believed:
            self.believed[sensor_id] = dict(self.recall(asked)) if self.recall else {}
        believed, held = dict(fields), {}
        counts = self.held.setdefault(sensor_id, {})
        for name in LIMITS:
            v = fields.get(name)
            if not _number(v):
                continue
            why = self._why(sensor_id, name, float(v), at, outdoor, reference)
            if why:
                held[name] = {"value": v, "why": why}
                del believed[name]
                if not counts.get(name):
                    log.warning("Held back from %s: %s %s", asked, NAMES[name], why)
                counts[name] = counts.get(name, 0) + 1
            else:
                self.believed[sensor_id][name] = (float(v), at)
                if counts.get(name):
                    log.info("%s %s believed again after %d reading(s) held back",
                             asked, NAMES[name], counts[name])
                counts[name] = 0
        if set(held) & set(WORKED_FROM):
            for name in DERIVED:
                if name in believed:
                    held[name] = {"value": believed.pop(name), "why": "worked out from a reading held back"}
        return believed, held


def reference(entries: list[dict], sensor_id: str, now: float, fresh_s: float = 900) -> float | None:
    """The middle of the other outdoor sensors' believed temperatures, heard within
    fresh_s; entries are current.json's sensors with "epoch" added."""
    temps = [e["fields"]["temp_f"] for e in entries
             if e.get("sensor_id", "").lower() != sensor_id.lower() and e.get("outdoor")
             and now - e.get("epoch", 0) <= fresh_s and _number((e.get("fields") or {}).get("temp_f"))
             and "temp_f" not in (e.get("suspect") or {})]
    return statistics.median(temps) if len(temps) >= 2 else None
