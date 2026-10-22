"""
Upload to Weathercloud ([weathercloud]): weathercloud.net.

Add a device at https://app.weathercloud.net and copy its Weathercloud ID and
key. A free account takes an upload every 10 minutes. Weathercloud's own
format: metric, most values times ten, and the answer is a status number in
the body ("200" is accepted). Checked 2026-10-21: the endpoint answers a wrong
key with "401".
"""

from __future__ import annotations

import configparser
import logging

from outputs._station import VERSION, Station, get, needs, number
from sources import Reading

log = logging.getLogger("acurite")

NAME = "weathercloud"
URL = "https://api.weathercloud.net/v01/set"
station = Station("weathercloud", every_minutes=10)

ANSWERS = {"200": "OK", "400": "bad request", "401": "wrong ID or key",
           "429": "too often for this account", "500": "Weathercloud's error"}


def _c(f): return (f - 32) * 5 / 9                 # °F → °C
def _ms(mph): return mph * 0.44704                # mph → m/s
def _hpa(inhg): return inhg * 33.8639             # inHg → hPa
def _mm(inches): return inches * 25.4             # in → mm


# Reading field → Weathercloud name, conversion to its units (before × 10).
WC_FIELDS = {"temp_f": ("temp", _c), "dew_point_f": ("dew", _c), "wind_chill_f": ("chill", _c),
             "heat_index_f": ("heat", _c), "wind_mph": ("wspd", _ms), "wind_avg_mph": ("wspdavg", _ms),
             "wind_gust_mph": ("wspdhi", _ms), "pressure_inhg": ("bar", _hpa),
             "rain_day_in": ("rain", _mm), "rain_hour_in": ("rainrate", _mm), "uv_index": ("uvi", float)}


def enabled(cfg: configparser.ConfigParser) -> bool:
    return needs(cfg, "weathercloud", "weathercloud_id", "key")


def params(cfg: configparser.ConfigParser, fields: dict, when) -> dict:
    out = {"wid": cfg.get("weathercloud", "weathercloud_id").strip(),
           "key": cfg.get("weathercloud", "key").strip(), "ver": VERSION}
    # No time sent: sources disagree on its format, and Weathercloud stamps
    # an upload with its arrival, which is within seconds of the reading.
    for field, (name, convert) in WC_FIELDS.items():
        v = number(fields, field)
        if v is not None:
            out[name] = str(int(round(convert(v) * 10)))
    for field, name in (("humidity_pct", "hum"), ("wind_dir_deg", "wdir")):
        v = number(fields, field)
        if v is not None:
            out[name] = str(int(round(v)) % 360 if name == "wdir" else int(round(v)))
    return out


def send(cfg: configparser.ConfigParser, reading: Reading) -> None:
    due = station.take(cfg, reading)
    if due is None:
        return
    status, body = get(URL, params(cfg, *due))
    if status == 200 and body == "200":
        log.info("Weathercloud upload OK")
    else:
        log.warning("Weathercloud upload failed (%s): %s", status, ANSWERS.get(body, body[:200]))
