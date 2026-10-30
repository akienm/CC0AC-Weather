"""
Bring in the weather you recorded before this station: --import.

    python3 acurite-capture.py --import ~/weewx.sdb
    python3 acurite-capture.py --import old-station.csv [--metric]
    python3 acurite-capture.py --import ~/CumulusMX/data
    python3 acurite-capture.py --import wu --wu-station KXXYYY12 --wu-api-key KEY --from 2024-01-01

  * A WeeWX database (the .sdb file; SQLite only): the archive table, in
    whichever units it was kept.
  * A CSV file: this program's own history.csv (each row keeps its sensor),
    or any table with a time column and columns named like temperature,
    humidity, wind, gust, direction, pressure, rain. Values may carry units
    ("70 °F"); they're read as °F, mph, inHg and inches unless --metric
    (°C, km/h, hPa, mm).
  * Cumulus (MX or 1): its data folder, the Cumulus folder above it, or one
    month's log (Oct24log.txt). The units are read from Cumulus.ini beside
    it; without one they're Cumulus's own defaults (°C, m/s, hPa, mm), or
    with --metric km/h for the wind, as Cumulus MX 3.12 and later default to.
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

import calendar
import configparser
import csv
import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
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
    "wind_avg_mph": (0, 200), "uv_index": (0, 20),
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

def c_to_f(c: float | None) -> float | None:
    return None if c is None else c * 9 / 5 + 32


def scaled(v: float | None, k: float) -> float | None:
    return None if v is None else v * k


KMH, MS, KNOTS, HPA, MM, CM = 0.621371, 2.236936, 1.150779, 0.0295300, 1 / 25.4, 1 / 2.54


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
        day, day_rain = None, 0.0
        last_hour: list = []
        for row in con.execute(f"SELECT {', '.join(want)} FROM archive ORDER BY dateTime"):
            r = dict(zip(want, row))
            t_conv, speed, press, rain_k = WEEWX_UNITS.get(r.get("usUnits") or 1, WEEWX_UNITS[1])
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
    t: datetime | None
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
    assert t is not None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc) if utc else t.astimezone()
    return t.timestamp()


def _value(text: str, field: str) -> float | None:
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
        columns: dict[str, str]
        time_col: str | None
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
            fields = {field: _value(r.get(h) or "", field) for h, field in columns.items()}
            if metric and not own:
                fields = {k: (c_to_f(v) if k.endswith("_f") else scaled(v, KMH) if k.endswith("_mph")
                              else scaled(v, HPA) if k == "pressure_inhg"
                              else scaled(v, MM) if k.endswith("_in") else v) for k, v in fields.items()}
            yield {"epoch": epoch, "fields": fields,
                   **({"sensor": r["sensor_id"], "type": r["type"]} if own else {})}


# ── Cumulus ───────────────────────────────────────────────────────────────────

# One month's log: Oct24log.txt. The month is always named in English.
CUMULUS_LOG = re.compile(r"([A-Z][a-z]{2})(\d{2})log\.txt")
# Cumulus.ini [Station] unit numbers → the factor to ours.
CUMULUS_WIND = {0: MS, 1: 1, 2: KMH, 3: KNOTS}
CUMULUS_PRESSURE = {0: HPA, 1: HPA, 2: 1}
CUMULUS_RAIN = {0: MM, 1: 1}
CUMULUS_WIND_NAMES = {0: "m/s", 1: "mph", 2: "km/h", 3: "knots"}


def cumulus_logs(path: Path) -> list[Path]:
    """The month logs to read, oldest first, from a log, a data folder or the folder above it."""
    if path.is_file():
        return [path]
    folder = path / "data" if (path / "data").is_dir() else path
    months = {m: n for n, m in enumerate(calendar.month_abbr) if m}

    def when(f: Path) -> tuple[int, int]:
        m = CUMULUS_LOG.fullmatch(f.name)
        assert m   # only matching names are sorted
        return int(m.group(2)), months.get(m.group(1), 0)

    logs = sorted((f for f in folder.iterdir() if CUMULUS_LOG.fullmatch(f.name)), key=when)
    if not logs:
        raise ValueError(f"no Cumulus month logs (like Oct24log.txt) in {folder}")
    return logs


def cumulus_units(path: Path, metric: bool) -> tuple[dict, str]:
    """(factors, what they were taken from): Cumulus.ini beside the logs, or the defaults."""
    folder = path if path.is_dir() else path.parent
    ini = next((d / "Cumulus.ini" for d in (folder, folder.parent, folder.parent.parent)
                if (d / "Cumulus.ini").is_file()), None)
    station = {}
    if ini is not None:
        cfg = configparser.ConfigParser(interpolation=None, strict=False)
        cfg.read(ini, encoding="utf-8-sig")
        station = {k.lower(): v for k, v in (cfg["Station"].items() if cfg.has_section("Station") else [])}

    def unit(key: str, default: int) -> int:
        try:
            return int(station.get(key.lower(), default))
        except ValueError:
            return default

    wind = unit("WindUnit", 2 if metric else 0)
    pressure, rain, temp = unit("PressureUnit", 1), unit("RainUnit", 0), unit("TempUnit", 0)
    told = (f"{'°F' if temp == 1 else '°C'}, {CUMULUS_WIND_NAMES.get(wind, 'm/s')}, "
            f"{'inHg' if pressure == 2 else 'hPa'}, {'in' if rain == 1 else 'mm'}")
    return ({"f": temp == 1, "wind": CUMULUS_WIND.get(wind, MS), "pressure": CUMULUS_PRESSURE.get(pressure, HPA),
             "rain": CUMULUS_RAIN.get(rain, MM)},
            f"{told}, from {ini}" if ini else f"{told}: no Cumulus.ini found, so Cumulus's defaults")


def _cumulus_time(day: str, hm: str) -> float | None:
    """'24/10/26' (day, month, year in any separator) and '14:05', local time."""
    try:
        d, m, y = (int(n) for n in re.findall(r"\d+", day))
        h, mi = (int(n) for n in re.findall(r"\d+", hm))
        return datetime(2000 + y if y < 100 else y, m, d, h, mi).timestamp()
    except ValueError:
        return None


def rows_cumulus(path: Path, metric: bool = False, say: Callable[[str], None] = print) -> Iterator[dict]:
    """A Cumulus month log a line at a time: date, time, then the fields by place."""
    k, told = cumulus_units(path, metric)
    say(f"  Cumulus units: {told}")
    temp = (lambda v: v) if k["f"] else c_to_f
    last_rain = None
    last_hour: list = []
    for log in cumulus_logs(path):
        for line in log.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            # Fields are split by the computer's list separator: a comma, or a
            # semicolon where the decimal point is a comma.
            sep = ";" if ";" in line else ","
            f = [v.strip().replace(",", ".") for v in line.split(sep)]
            if len(f) < 11:
                continue
            epoch = _cumulus_time(f[0], f[1])
            if epoch is None:
                continue

            def at(n: int, conv: Callable[[float], float | None] | None = None) -> float | None:
                """Field n as Cumulus's documentation numbers them (1 is the date)."""
                if len(f) < n or not NUMBER.fullmatch(f[n - 1]):
                    return None
                v = float(f[n - 1])
                return conv(v) if conv else v

            # Rain since midnight (field 27) where the log has it; rain today
            # runs from Cumulus's rollover, which may be 9 am.
            rain = at(27, lambda v: v * k["rain"]) if len(f) >= 27 else None
            rain = rain if rain is not None else at(10, lambda v: v * k["rain"])
            fields = {"temp_f": at(3, temp), "humidity_pct": at(4), "dew_point_f": at(5, temp),
                      "wind_avg_mph": at(6, lambda v: v * k["wind"]),
                      "wind_gust_mph": at(7, lambda v: v * k["wind"]), "wind_dir_deg": at(8),
                      "pressure_inhg": at(11, lambda v: v * k["pressure"]),
                      "wind_mph": at(15, lambda v: v * k["wind"]),
                      "wind_chill_f": at(16, temp), "heat_index_f": at(17, temp), "uv_index": at(18),
                      "feels_like_f": at(28, temp)}
            if fields["wind_mph"] is None:
                fields["wind_mph"] = fields["wind_avg_mph"]
            if rain is not None:
                # This hour's rain: how much the day's total grew in the last hour.
                fell = rain if last_rain is None or rain < last_rain else rain - last_rain
                last_rain = rain
                last_hour = [(t, v) for t, v in last_hour if epoch - t < 3600] + [(epoch, fell)]
                fields["rain_day_in"] = rain
                fields["rain_hour_in"] = sum(v for _, v in last_hour)
            yield {"epoch": epoch, "fields": fields}


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


def rows_wu(station: str, key: str, start: date, end: date, say: Callable[[str], None] = print) -> Iterator[dict]:
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
