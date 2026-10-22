"""
Bring in the weather you recorded before this station: --import.

    python3 acurite-capture.py --import ~/weewx.sdb
    python3 acurite-capture.py --import old-station.csv [--metric]
    python3 acurite-capture.py --import wu --wu-station KXXYYY12 --wu-api-key KEY --from 2024-01-01

  * A WeeWX database (the .sdb file; SQLite only): the archive table, in
    whichever units it was kept.
  * A CSV file: this program's own history.csv (each row keeps its sensor),
    or any table with a time column and columns named like temperature,
    humidity, wind, gust, direction, pressure, rain. Values may carry units
    ("70 °F"); they're read as °F, mph, inHg and inches unless --metric
    (°C, km/h, hPa, mm).
  * Weather Underground: your own station's history, one day per request. It
    needs an API key, which WU gives to station owners (wunderground.com →
    My Profile → API Keys); the upload password is not it.

Everything lands as readings of one outdoor sensor: --sensor, or else the
first outdoor sensor this station has heard. Only readings older than that
sensor's first real reading are kept, so an import never overwrites or mixes
with what the station measured itself. A value outside what weather can be
(a 300 °F afternoon, 140 % humidity) is left out, and a value that is missing
stays missing; nothing is filled with zero. Running an import again adds
nothing twice. The daily summaries, records and charts are rebuilt after.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

# What weather can be, per field: outside it the value is a sensor fault, a
# unit mix-up or a placeholder (WeeWX and WU both have used -9999).
PLAUSIBLE = {
    "temp_f": (-80, 140), "humidity_pct": (1, 100), "dew_point_f": (-90, 95),
    "heat_index_f": (-80, 170), "wind_chill_f": (-120, 140), "feels_like_f": (-120, 170),
    "wind_mph": (0, 200), "wind_gust_mph": (0, 250), "wind_dir_deg": (0, 360),
    "pressure_inhg": (25.5, 32.5), "rain_hour_in": (0, 20), "rain_day_in": (0, 60),
    "uv_index": (0, 20),
}


def clean(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if v is None or k not in PLAUSIBLE:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        lo, hi = PLAUSIBLE[k]
        if lo <= v <= hi:
            out[k] = round(v, 2)
    return out


# ── Units, once, on the way in ────────────────────────────────────────────────

def c_to_f(c):
    return None if c is None else c * 9 / 5 + 32


def scaled(v, k):
    return None if v is None else v * k


KMH, MS, HPA, MM, CM = 0.621371, 2.236936, 0.0295300, 1 / 25.4, 1 / 2.54


# ── WeeWX ─────────────────────────────────────────────────────────────────────

# usUnits → (temperature, speed, pressure, rain) to °F, mph, inHg, in
WEEWX_UNITS = {1: (None, 1, 1, 1), 16: (c_to_f, KMH, HPA, CM), 17: (c_to_f, MS, HPA, MM)}


def rows_weewx(path: Path) -> Iterator[dict]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(archive)")}
        if not cols:
            raise ValueError(f"{path} has no archive table; is it a WeeWX database?")
        want = [c for c in ("dateTime", "usUnits", "outTemp", "outHumidity", "dewpoint", "heatindex",
                            "windchill", "windSpeed", "windGust", "windDir", "barometer", "rain", "UV")
                if c in cols]
        day, day_rain, last_hour = None, 0.0, []
        for r in con.execute(f"SELECT {', '.join(want)} FROM archive ORDER BY dateTime"):
            r = dict(zip(want, r))
            t_conv, speed, press, rain_k = WEEWX_UNITS.get(r.get("usUnits"), WEEWX_UNITS[1])
            temp = (lambda v: v) if t_conv is None else t_conv
            epoch = r["dateTime"]
            # WeeWX stores the rain that fell in each archive interval; the hub
            # reports the day's running total and the last hour's, so do as it does.
            local_day = datetime.fromtimestamp(epoch).date()
            if local_day != day:
                day, day_rain = local_day, 0.0
            rain = scaled(r.get("rain"), rain_k)
            fields = {"temp_f": temp(r.get("outTemp")) if r.get("outTemp") is not None else None,
                      "humidity_pct": r.get("outHumidity"),
                      "dew_point_f": temp(r["dewpoint"]) if r.get("dewpoint") is not None else None,
                      "heat_index_f": temp(r["heatindex"]) if r.get("heatindex") is not None else None,
                      "wind_chill_f": temp(r["windchill"]) if r.get("windchill") is not None else None,
                      "wind_mph": scaled(r.get("windSpeed"), speed),
                      "wind_gust_mph": scaled(r.get("windGust"), speed),
                      "wind_dir_deg": r.get("windDir"),
                      "pressure_inhg": scaled(r.get("barometer"), press),
                      "uv_index": r.get("UV")}
            if rain is not None and rain >= 0:
                day_rain += rain
                last_hour = [(t, v) for t, v in last_hour if epoch - t < 3600] + [(epoch, rain)]
                fields["rain_day_in"] = day_rain
                fields["rain_hour_in"] = sum(v for _, v in last_hour)
            yield {"epoch": epoch, "fields": fields}
    finally:
        con.close()


# ── CSV ───────────────────────────────────────────────────────────────────────

# A column's name, lowercased with everything but letters dropped → our field.
CSV_NAMES = {
    "temp_f": ("temperature", "temp", "tempf", "outtemp", "tempavg", "outsidetemperature", "outdoortemperature"),
    "humidity_pct": ("humidity", "hum", "outhumidity", "humidityavg", "relativehumidity", "outdoorhumidity"),
    "dew_point_f": ("dewpoint", "dewptf", "dewptavg"),
    "wind_mph": ("windspeed", "speed", "windspeedmph", "windspeedavg", "windavg"),
    "wind_gust_mph": ("gust", "windgust", "windgustmph", "windgusthigh", "gustspeed"),
    "wind_dir_deg": ("winddir", "winddirection", "direction", "winddiravg", "dir",
                     "wind"),    # Weather Underground's table export: "Wind" is the direction
    "pressure_inhg": ("pressure", "baromin", "barometer", "baro", "relativepressure", "sealevelpressure",
                      "pressureavg", "barometricpressure"),
    "rain_day_in": ("dailyrainin", "dailyrain", "precipaccum", "precipitationaccumulation", "raintoday",
                    "precipaccumulation", "precipaccumin", "daily", "precipitation", "rainday", "dailyrainfall",
                    "preciptotal"),
    "rain_hour_in": ("precipratein", "preciprate", "rainrate", "rainin", "hourlyrain", "hourlyrainin"),
    "uv_index": ("uv", "uvindex", "uvhigh"),
}
TIME_NAMES = ("timestamp", "time", "datetime", "date", "obstimeutc", "obstimelocal", "dateutc", "utc",
              "localtime", "datetimeutc", "timeutc")
CARDINALS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _key(name: str) -> str:
    # "Temperature (°F)" → "temperature": the units in brackets aren't part of the name.
    return re.sub(r"[^a-z]", "", re.sub(r"\(.*?\)|\[.*?\]", "", name.lower()))


def parse_time(text: str, utc: bool) -> float | None:
    text = text.strip()
    if not text:
        return None
    if NUMBER.fullmatch(text) and float(text) > 1e8:          # epoch seconds
        return float(text)
    iso = text.replace("Z", "+00:00").replace("/", "-")
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        t = None
        for fmt in ("%m-%d-%Y %I:%M %p", "%m-%d-%Y %I:%M:%S %p", "%m-%d-%Y %H:%M", "%m-%d-%Y %H:%M:%S",
                    "%Y-%m-%d %I:%M %p", "%Y-%m-%d %I:%M:%S %p", "%d-%m-%Y %H:%M"):
            try:
                t = datetime.strptime(iso, fmt)
                break
            except ValueError:
                pass
        if t is None:
            return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc) if utc else t.astimezone()
    return t.timestamp()


def _value(text: str, field: str):
    text = (text or "").strip()
    if field == "wind_dir_deg" and text.upper() in CARDINALS:
        return CARDINALS.index(text.upper()) * 22.5
    m = NUMBER.search(text.replace(",", "."))
    return float(m.group()) if m else None


def rows_csv(path: Path, metric: bool = False) -> Iterator[dict]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        heads = reader.fieldnames or []
        keys = {h: _key(h) for h in heads}
        own = {"timestamp", "sensor_id", "type"} <= set(heads)        # this program's history.csv
        if own:
            columns = {h: h for h in heads if h in PLAUSIBLE}
            time_col = "timestamp"
        else:
            columns = {}
            for field, names in CSV_NAMES.items():
                h = next((h for n in names for h in heads if keys[h] == n and h not in columns), None)
                if h:
                    columns[h] = field
            time_col = next((h for n in TIME_NAMES for h in heads if keys[h] == n), None)
        if time_col is None:
            raise ValueError(f"no time column in {path.name}; looked for {', '.join(TIME_NAMES)}")
        if not columns:
            raise ValueError(f"no weather columns recognised in {path.name}: {', '.join(heads)}")
        utc = own or "utc" in keys[time_col]
        for r in reader:
            epoch = parse_time(r.get(time_col) or "", utc)
            if epoch is None:
                continue
            fields = {field: _value(r.get(h), field) for h, field in columns.items()}
            if metric and not own:
                fields = {k: (c_to_f(v) if k.endswith("_f") else scaled(v, KMH) if k.endswith("_mph")
                              else scaled(v, HPA) if k == "pressure_inhg"
                              else scaled(v, MM) if k.endswith("_in") else v) for k, v in fields.items()}
            yield {"epoch": epoch, "fields": fields,
                   **({"sensor": r["sensor_id"], "type": r["type"]} if own else {})}


# ── Weather Underground ───────────────────────────────────────────────────────

WU_HISTORY = "https://api.weather.com/v2/pws/history/all"


def wu_day(station: str, key: str, day: date) -> list[dict]:
    q = urllib.parse.urlencode({"stationId": station, "format": "json", "units": "e",
                                "date": day.strftime("%Y%m%d"), "apiKey": key})
    req = urllib.request.Request(f"{WU_HISTORY}?{q}", headers={"Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read()
    return (json.loads(body).get("observations") or []) if body.strip() else []   # no data: an empty 204


def wu_fields(obs: dict) -> dict:
    imp = obs.get("imperial") or {}
    lo, hi = imp.get("pressureMin"), imp.get("pressureMax")
    return {"temp_f": imp.get("tempAvg"), "humidity_pct": obs.get("humidityAvg"),
            "dew_point_f": imp.get("dewptAvg"), "wind_mph": imp.get("windspeedAvg"),
            "wind_gust_mph": imp.get("windgustHigh"), "wind_dir_deg": obs.get("winddirAvg"),
            "pressure_inhg": (lo + hi) / 2 if lo is not None and hi is not None else lo if hi is None else hi,
            "rain_day_in": imp.get("precipTotal"), "rain_hour_in": imp.get("precipRate"),
            "uv_index": obs.get("uvHigh")}


def rows_wu(station: str, key: str, start: date, end: date, say=print) -> Iterator[dict]:
    day = start
    while day <= end:
        try:
            observations = wu_day(station, key, day)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise ValueError("Weather Underground refused the API key (it's the key from My Profile → "
                                 "API Keys, not the station's upload password)") from e
            raise
        say(f"  {day}: {len(observations)} observations")
        for obs in observations:
            try:
                epoch = datetime.fromisoformat(obs["obsTimeUtc"].replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError, AttributeError):
                continue
            yield {"epoch": epoch, "fields": wu_fields(obs)}
        day += timedelta(days=1)
        time.sleep(1)    # WU allows 30 calls a minute
