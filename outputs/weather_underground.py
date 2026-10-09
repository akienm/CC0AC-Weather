"""
Upload to Weather Underground ([weather_underground]).

Only for a station heard by radio: the Access hub uploads to Weather
Underground itself, so readings from the hub are never sent from here. Only
outdoor sensors' readings are sent; a room sensor's temperature isn't the
station's.
"""

from __future__ import annotations

import configparser
import logging
import urllib.parse
import urllib.request

from sources import OUTDOOR_TYPES, Reading

log = logging.getLogger("acurite")

NAME = "weather-underground"
WU_URL = "https://weatherstation.wunderground.com/weatherstation/updateweatherstation.php"

# Reading fields → Weather Underground's upload names, and decimal places.
WU_FIELDS = {"temp_f": ("tempf", 1), "humidity_pct": ("humidity", 0), "dew_point_f": ("dewptf", 1),
             "wind_mph": ("windspeedmph", 1), "wind_gust_mph": ("windgustmph", 1),
             "wind_dir_deg": ("winddir", 0), "rain_hour_in": ("rainin", 2),
             "rain_day_in": ("dailyrainin", 2), "pressure_inhg": ("baromin", 2), "uv_index": ("UV", 0)}


def enabled(cfg: configparser.ConfigParser) -> bool:
    if not cfg.getboolean("weather_underground", "enabled", fallback=False):
        return False
    if not (cfg.get("weather_underground", "station_id", fallback="").strip()
            and cfg.get("weather_underground", "station_key", fallback="").strip()):
        log.warning("WU upload enabled but station_id / station_key not set in config")
        return False
    return True


def params(cfg: configparser.ConfigParser, reading: Reading) -> dict | None:
    """The upload's query parameters, or None if this reading isn't sent."""
    if reading.source == "acurite-access" or reading.type not in OUTDOOR_TYPES:
        return None
    out = {"ID": cfg.get("weather_underground", "station_id").strip(),
           "PASSWORD": cfg.get("weather_underground", "station_key").strip(),
           "dateutc": "now", "action": "updateraw"}
    for field, (name, places) in WU_FIELDS.items():
        v = reading.fields.get(field)
        if isinstance(v, (int, float)):
            out[name] = str(round(float(v), places) if places else int(round(v)))
    return out if len(out) > 4 else None


def send(cfg: configparser.ConfigParser, reading: Reading) -> None:
    p = params(cfg, reading)
    if p is None:
        return
    try:
        with urllib.request.urlopen(WU_URL + "?" + urllib.parse.urlencode(p), timeout=10) as resp:
            log.info("WU upload OK: %s", resp.read().decode().strip())
    except Exception as exc:
        log.warning("WU upload failed: %s", exc)
