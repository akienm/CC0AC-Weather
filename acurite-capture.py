#!/usr/bin/env python3
"""
acurite-capture.py — AcuRite weather sensor capture daemon.

Two sources, either or both:
  * the AcuRite Access hub: an HTTPS listener the hub reports to, which records
    each reading and relays it on to AcuRite unchanged ([hub], sources/acurite_access.py);
  * rtl_433 with a USB SDR, decoding the sensors off the air ([capture]).
Each source in sources/ turns what it hears into Readings (sources/__init__.py);
readings land in <write_path>/current.json, current.js, history.csv and the database.
With [forecast] on, the National Weather Service forecast, nearest airport
observation and active alerts land in <write_path>/forecast.json and forecast.js.

Requirements:
    sudo apt install rtl-sdr
    sudo apt install rtl-433          # Ubuntu 22.04+
    # or build from source: https://github.com/merbanan/rtl_433

    Add your user to the plugdev group for non-root SDR access:
    sudo usermod -aG plugdev $USER    # log out and back in after

Setup:
    cp config.ini.example ~/.cc0ac-weather/config.ini
    python3 acurite-capture.py --discover   # find your sensor IDs
    # edit config.ini — add sensor IDs under [sensors]
    python3 acurite-capture.py              # run daemon

Usage:
    python3 acurite-capture.py [--config PATH] [--discover] [--discover-time N] [-v]
"""

from __future__ import annotations

import argparse
import configparser
import csv
import importlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sources import FIELDS, Reading
from sources import acurite_access

log = logging.getLogger("acurite")

DEFAULT_CONFIG = Path.home() / ".cc0ac-weather" / "config.ini"
DEFAULT_PROTOCOLS = ["40", "78", "112", "191"]
WU_URL = "https://weatherstation.wunderground.com/weatherstation/updateweatherstation.php"

CSV_FIELDS = [
    "timestamp", "sensor_id", "sensor_name", "model",
    "temp_f", "humidity_pct", "wind_mph", "wind_dir_deg",
    "wind_gust_mph", "rain_in", "pressure_inhg", "dew_point_f",
    "uv_index", "battery_ok",
]


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if not path.exists():
        log.error("Config not found: %s", path)
        log.error("Copy config.ini.example to %s and edit it.", path)
        sys.exit(1)
    cfg.read(path)
    return cfg


def sensor_map(cfg: configparser.ConfigParser) -> dict[str, str]:
    """Return {sensor_id_str: display_name} from [sensors] section."""
    if not cfg.has_section("sensors"):
        return {}
    return {k: v for k, v in cfg.items("sensors")}


def rtl433_cmd(cfg: configparser.ConfigParser) -> list[str]:
    protocols = DEFAULT_PROTOCOLS
    if cfg.has_option("capture", "protocols"):
        protocols = [p.strip() for p in cfg.get("capture", "protocols").split(",")]
    device = cfg.get("capture", "device_index", fallback="0")
    cmd = ["rtl_433", "-d", device, "-F", "json"]
    for p in protocols:
        cmd += ["-R", p]
    return cmd


# ── Packet parsing ────────────────────────────────────────────────────────────

def parse_packet(raw: dict) -> dict | None:
    """Extract standardised fields from an rtl_433 JSON packet.

    Returns None for non-AcuRite packets.
    rtl_433 field names vary across versions and sensor models — handle all known variants.
    """
    model = raw.get("model", "")
    if "acurite" not in model.lower():
        return None

    sensor_id = str(raw.get("id", raw.get("sensor_id", ""))).strip()
    if not sensor_id:
        return None

    # Temperature — prefer Fahrenheit; convert Celsius if that's what we got
    temp_f = raw.get("temperature_F", raw.get("temperature_f"))
    if temp_f is None:
        temp_c = raw.get("temperature_C", raw.get("temperature_c"))
        if temp_c is not None:
            temp_f = round(float(temp_c) * 9 / 5 + 32, 1)

    # Wind speed — prefer mph; convert km/h if needed
    wind_mph = raw.get("wind_avg_mi_h", raw.get("wind_speed_mph", raw.get("wind_avg_mph")))
    if wind_mph is None:
        wind_kph = raw.get("wind_avg_km_h", raw.get("wind_speed_kph"))
        if wind_kph is not None:
            wind_mph = round(float(wind_kph) * 0.621371, 1)

    # Wind direction — degrees
    wind_dir = raw.get("wind_dir_deg", raw.get("wind_direction_deg", raw.get("wind_dir")))

    # Rain — rtl_433 reports cumulative mm; convert to inches
    rain_in = None
    rain_mm = raw.get("rain_mm", raw.get("rain_in_raw"))
    if rain_mm is not None:
        rain_in = round(float(rain_mm) / 25.4, 3)
    elif raw.get("rain_in") is not None:
        rain_in = raw["rain_in"]

    return {
        "sensor_id": sensor_id,
        "model": model,
        "temp_f": temp_f,
        "humidity_pct": raw.get("humidity"),
        "wind_mph": wind_mph,
        "wind_dir_deg": wind_dir,
        "rain_in": rain_in,
        "battery_ok": raw.get("battery_ok", raw.get("battery")),
    }


# ── CSV ───────────────────────────────────────────────────────────────────────

def append_csv(path: Path, row: dict) -> None:
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)


# ── Weather Underground ───────────────────────────────────────────────────────

def upload_wu(cfg: configparser.ConfigParser, packet: dict) -> None:
    if not cfg.getboolean("weather_underground", "enabled", fallback=False):
        return
    station_id = cfg.get("weather_underground", "station_id", fallback="").strip()
    station_key = cfg.get("weather_underground", "station_key", fallback="").strip()
    if not station_id or not station_key:
        log.warning("WU upload enabled but station_id / station_key not set in config")
        return

    params: dict[str, str] = {
        "ID": station_id,
        "PASSWORD": station_key,
        "dateutc": "now",
        "action": "updateraw",
    }
    if packet.get("temp_f") is not None:
        params["tempf"] = str(round(float(packet["temp_f"]), 1))
    if packet.get("humidity_pct") is not None:
        params["humidity"] = str(int(packet["humidity_pct"]))
    if packet.get("wind_mph") is not None:
        params["windspeedmph"] = str(round(float(packet["wind_mph"]), 1))
    if packet.get("wind_dir_deg") is not None:
        params["winddir"] = str(int(packet["wind_dir_deg"]))
    if packet.get("rain_in") is not None:
        params["rainin"] = str(round(float(packet["rain_in"]), 3))

    url = WU_URL + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            log.info("WU upload OK: %s", resp.read().decode().strip())
    except Exception as exc:
        log.warning("WU upload failed: %s", exc)


# ── Recording ─────────────────────────────────────────────────────────────────
#
# Every source (see sources/) hands its readings to record_reading(), which
# folds each one into current.json / current.js, appends it to history.csv and
# stores it in the database. Nothing here knows which source a reading came from.

HISTORY_FIELDS = ["timestamp", "sensor_id", "sensor_name", "type"] + sorted(FIELDS)

_current: dict[str, dict] = {}
_current_lock = threading.Lock()

# Pressure trend: the barometer is the hub's, so every sensor's reading carries
# the same value. Forecasters read the change over 3 hours; we keep a little
# more than that, seeded from history.csv so a restart doesn't blank the trend.
PRESSURE_TREND_S = 3 * 3600
_pressure: deque = deque()   # (epoch seconds, inHg), guarded by _current_lock
_pressure_seeded = False
_current_page: dict = {}     # derived values for the page, e.g. pressure_change_3h


def _epoch(iso: str) -> float:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _seed_pressure(history: Path, now: float) -> None:
    """Load the last few hours of pressure from the tail of history.csv."""
    if not history.exists():
        return
    with history.open("rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 256 * 1024))
        tail = f.read().decode("utf-8", errors="replace").splitlines()[1:]
    for row in csv.DictReader(tail, fieldnames=HISTORY_FIELDS):
        try:
            t, p = _epoch(row["timestamp"]), float(row["pressure_inhg"])
        except (TypeError, ValueError):
            continue
        if now - t <= PRESSURE_TREND_S * 1.2:
            _pressure.append((t, p))


def _pressure_change(now: float, value: float) -> float | None:
    """inHg change since about 3 hours ago, or None until we have that much."""
    _pressure.append((now, value))
    while _pressure and now - _pressure[0][0] > PRESSURE_TREND_S * 1.2:
        _pressure.popleft()
    then = [p for t, p in _pressure if now - t >= PRESSURE_TREND_S * 0.9]
    return round(value - then[-1], 2) if then else None


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def record_reading(reading: Reading, write_path: Path, sensors: dict, page: dict | None = None,
                   db_path: Path | None = None) -> dict:
    """Fold one reading into current.json / current.js, append history.csv,
    and store it in the database at db_path. Returns the sensor's current entry.

    page: settings the dashboard reads from the data file (it cannot read
    config.ini), e.g. {"lower_buttons": [{"label": ..., "url": ...}]}; empty values are left out."""
    global _pressure_seeded, _db
    sensor_id = reading.sensor_id
    kind = reading.type or "unknown"
    now = reading.received_utc
    fields = reading.fields
    with _current_lock:
        if not _pressure_seeded:
            _seed_pressure(write_path / "history.csv", _epoch(now))
            _pressure_seeded = True
        if isinstance(fields.get("pressure_inhg"), (int, float)):
            _current_page["pressure_change_3h"] = _pressure_change(_epoch(now), float(fields["pressure_inhg"]))
        entry = _current.setdefault(sensor_id, {"sensor_id": sensor_id, "type": kind, "fields": {}})
        entry["name"] = sensors.get(sensor_id, entry.get("name") or f"{kind} {sensor_id}")
        entry["type"] = kind
        entry["updated"] = now
        entry["fields"].update(fields)
        snapshot = {"written": now, "hub": reading.receiver or "",
                    **{k: v for k, v in (page or {}).items() if v},
                    **{k: v for k, v in _current_page.items() if v is not None},
                    "sensors": list(_current.values())}
        body = json.dumps(snapshot, indent=1)
        _write_atomic(write_path / "current.json", body)
        # current.js lets the dashboard load the data with a <script> tag, which
        # works from a file:// URL and from any static host without CORS.
        _write_atomic(write_path / "current.js", f"window.ACURITE_CURRENT = {body};\n")
        history = write_path / "history.csv"
        exists = history.exists()
        with history.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS, extrasaction="ignore")
            if not exists:
                w.writeheader()
            w.writerow({"timestamp": now, "sensor_id": sensor_id,
                        "sensor_name": entry["name"], "type": kind, **fields})
        if db_path is not None:
            try:
                if _db is None:
                    _db = open_db(db_path)
                store_reading(_db, reading)
            except sqlite3.Error as exc:
                log.error("Store failed: %s (%s raw=%s)", exc, reading.source, reading.raw)
    return entry


# ── Long-term store (SQLite) ──────────────────────────────────────────────────
#
# One row per reading, kept for good: the reading's fields as columns for
# querying and charting, plus exactly what the source heard (for the hub, its
# whole query string) so nothing it sends is ever lost, even fields we don't map
# yet. history.csv stays as a plain-text copy. A reading the hub sends twice (it
# resends whatever AcuRite refused) is heard the same both times, so it is
# stored once.

DB_COLUMNS = sorted(FIELDS)
_db: sqlite3.Connection | None = None   # guarded by _current_lock


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=False, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute(f"""CREATE TABLE IF NOT EXISTS readings (
        received_utc TEXT NOT NULL,  -- when it reached us, e.g. 2026-10-08T18:17:23Z
        hub_utc TEXT,                -- the hub's own dateutc for the reading
        hub_id TEXT,
        sensor_id TEXT NOT NULL,
        type TEXT,                   -- Atlas, tower, ...
        {", ".join(DB_COLUMNS)},
        query TEXT NOT NULL UNIQUE   -- the hub's query string, exactly as sent
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS readings_by_sensor ON readings (sensor_id, received_utc)")
    con.execute("CREATE INDEX IF NOT EXISTS readings_by_time ON readings (received_utc)")
    return con


def store_reading(con: sqlite3.Connection, reading: Reading) -> bool:
    """Insert one reading; False if that exact reading was already stored."""
    row = {"received_utc": reading.received_utc, "hub_utc": reading.sensor_utc,
           "hub_id": reading.receiver, "sensor_id": reading.sensor_id, "type": reading.type,
           **reading.fields, "query": reading.raw}
    cur = con.execute(f"INSERT OR IGNORE INTO readings ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                      list(row.values()))
    con.commit()
    return cur.rowcount == 1


def import_raw(cfg: configparser.ConfigParser) -> None:
    """Load every reading in the raw hub logs into the store. Safe to run again:
    readings already there are skipped."""
    con = open_db(_db_path(cfg))
    added = skipped = other = 0
    for reading in acurite_access.raw_readings(cfg):
        if reading is None:
            other += 1
        elif store_reading(con, reading):
            added += 1
        else:
            skipped += 1
    total = con.execute("SELECT count(*) FROM readings").fetchone()[0]
    print(f"{added} readings added, {skipped} already stored, {other} other requests left out; "
          f"{total} readings in {_db_path(cfg)}")


def _db_path(cfg: configparser.ConfigParser) -> Path:
    write_path = Path(cfg.get("device", "write_path")).expanduser()
    return Path(cfg.get("storage", "database", fallback=str(write_path / "weather.db"))).expanduser()


# ── History for the charts page ───────────────────────────────────────────────
#
# /history.json?period=today|week|month|year|all, or ?from=YYYY-MM-DD&to=YYYY-MM-DD.
# Readings are grouped into time buckets sized so any period comes back as a few
# hundred points: every reading for today, daily averages for a year. Days and
# weeks follow the station's local time, as the hub's daily rain does.
#
# Rain: the hub sends rainin, a rolling total for the last 60 minutes, and
# dailyrainin, the total since local midnight. A day's rain is its highest
# dailyrainin; shorter spans use how much dailyrainin grew within them.

HISTORY_STEPS = [300, 900, 1800, 3600, 2 * 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 7 * 86400]
HISTORY_POINTS = 600   # about how many buckets a chart gets at most


def _history_bounds(q: dict, first: float, now: datetime) -> tuple[float, float, str]:
    """(from, to) in epoch seconds for the request, and the period it names."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    period = q.get("period", "today")
    if "from" in q:
        lo = datetime.strptime(q["from"], "%Y-%m-%d").astimezone()
        hi = datetime.strptime(q.get("to", q["from"]), "%Y-%m-%d").astimezone().timestamp() + 86400
        return lo.timestamp(), min(hi, now.timestamp()), "range"
    days = {"week": 7, "month": 30, "year": 365}
    if period in days:
        return now.timestamp() - days[period] * 86400, now.timestamp(), period
    if period == "all":
        return first, now.timestamp(), period
    return midnight.timestamp(), now.timestamp(), "today"


def _local_days(con: sqlite3.Connection, lo: float | None = None, hi: float | None = None) -> list:
    """[(local date 'YYYY-MM-DD', that day's rain)] from the Atlas's dailyrainin."""
    where, args = "", []
    if lo is not None:
        where, args = "AND received_utc >= ? AND received_utc < ?", [_iso(lo), _iso(hi)]
    return con.execute(f"""SELECT date(received_utc, 'localtime') AS d, max(rain_day_in)
        FROM readings WHERE type = 'Atlas' AND rain_day_in IS NOT NULL {where}
        GROUP BY d ORDER BY d""", args).fetchall()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rain_totals(con: sqlite3.Connection, now: datetime) -> dict:
    days = _local_days(con)
    today = now.date()
    week_start = today.toordinal() - (today.isoweekday() % 7)   # weeks start on Sunday
    def total(keep) -> float:
        return round(sum(r for d, r in days if keep(datetime.strptime(d, "%Y-%m-%d").date())), 2)
    return {"today": total(lambda d: d == today),
            "week": total(lambda d: d.toordinal() >= week_start),
            "month": total(lambda d: (d.year, d.month) == (today.year, today.month)),
            "year": total(lambda d: d.year == today.year),
            "all": total(lambda d: True),
            "since": days[0][0] if days else None}


def _rain_buckets(con: sqlite3.Connection, lo: float, hi: float, step: int, off: int) -> dict:
    """Rain per bucket: an hour at the finest, a day once buckets reach a day."""
    step = max(step, 3600)
    buckets: dict[int, float] = {}
    def key(epoch: float) -> int:
        return int((epoch + off) // step * step - off)
    if step >= 86400:
        for d, rain in _local_days(con, lo, hi):
            day = datetime.strptime(d, "%Y-%m-%d").astimezone().timestamp()
            buckets[key(day)] = buckets.get(key(day), 0) + (rain or 0)
    else:
        rows = con.execute("""SELECT received_utc, rain_day_in FROM readings
            WHERE type = 'Atlas' AND rain_day_in IS NOT NULL AND received_utc < ?
              AND received_utc >= (SELECT coalesce(max(received_utc), '') FROM readings
                                   WHERE type = 'Atlas' AND rain_day_in IS NOT NULL AND received_utc < ?)
            ORDER BY received_utc""", [_iso(hi), _iso(lo)]).fetchall()
        prev = None
        for stamp, total in rows:
            if prev is not None:
                # A drop is the midnight reset: everything since is new rain.
                grew = total - prev if total >= prev else total
                t = _epoch(stamp)
                if grew > 0 and t >= lo:
                    buckets[key(t)] = buckets.get(key(t), 0) + grew
            prev = total
    t = list(range(key(lo), int(hi) + 1, step))
    return {"step": step, "t": t, "in": [round(buckets.get(b, 0), 2) for b in t]}


def history(db_path: Path, sensors: dict, q: dict) -> dict:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        now = datetime.now().astimezone()
        off = int(now.utcoffset().total_seconds())
        first = con.execute("SELECT min(received_utc) FROM readings").fetchone()[0]
        first = _epoch(first) if first else now.timestamp()
        lo, hi, period = _history_bounds(q, first, now)
        lo = max(lo, first) if period != "today" else lo
        step = next((s for s in HISTORY_STEPS if (hi - lo) / s <= HISTORY_POINTS), HISTORY_STEPS[-1])
        bucket = "(CAST(strftime('%s', received_utc) AS INTEGER) + :off) / :step * :step - :off"
        span = {"off": off, "step": step, "lo": _iso(lo), "hi": _iso(hi)}
        t = list(range(int((lo + off) // step * step - off), int(hi) + 1, step))
        index = {b: i for i, b in enumerate(t)}

        def column(rows, n) -> list[list]:
            cols = [[None] * len(t) for _ in range(n)]
            for b, *vals in rows:
                if b in index:
                    for c, v in zip(cols, vals):
                        c[index[b]] = None if v is None else round(v, 2)
            return cols

        out_names = ["temp_f", "temp_min", "temp_max", "dew_point_f", "feels_like_f", "humidity_pct",
                     "wind_mph", "wind_gust_mph", "wind_dir_deg", "pressure_inhg", "uv_index", "light_lux"]
        outdoor = con.execute(f"""SELECT {bucket} AS b, avg(temp_f), min(temp_f), max(temp_f),
                avg(dew_point_f), avg(feels_like_f), avg(humidity_pct), avg(wind_mph), max(wind_gust_mph),
                (degrees(atan2(avg(sin(radians(wind_dir_deg))), avg(cos(radians(wind_dir_deg))))) + 360) % 360,
                avg(pressure_inhg), max(uv_index), avg(light_lux)
            FROM readings WHERE type = 'Atlas' AND received_utc >= :lo AND received_utc < :hi
            GROUP BY b""", span).fetchall()
        rooms = []
        for sid, kind in con.execute("""SELECT DISTINCT sensor_id, type FROM readings
                WHERE type != 'Atlas' AND received_utc >= ? AND received_utc < ?""", [_iso(lo), _iso(hi)]):
            temp, hum = column(con.execute(f"""SELECT {bucket} AS b, avg(temp_f), avg(humidity_pct)
                FROM readings WHERE sensor_id = :sid AND received_utc >= :lo AND received_utc < :hi
                GROUP BY b""", {**span, "sid": sid}).fetchall(), 2)
            rooms.append({"id": sid, "name": sensors.get(sid, f"{kind} {sid}"), "temp_f": temp, "humidity_pct": hum})
        rooms.sort(key=lambda r: r["name"])
        return {"period": period, "from": lo, "to": hi, "step": step, "t": t,
                "outdoor": dict(zip(out_names, column(outdoor, len(out_names)))),
                "rooms": rooms,
                "rain": _rain_buckets(con, lo, hi, step, off),
                "rain_totals": _rain_totals(con, now)}
    finally:
        con.close()


def _station(cfg: configparser.ConfigParser) -> dict:
    """[station] latitude/longitude/elevation_ft, for the page's sun and moon.
    Empty (and the sun/moon card hidden) unless latitude and longitude are set."""
    out = {}
    for key in ("latitude", "longitude", "elevation_ft"):
        try:
            out[key] = cfg.getfloat("station", key)
        except (configparser.Error, ValueError):
            pass
    return out if "latitude" in out and "longitude" in out else {}


def _title(cfg: configparser.ConfigParser) -> str:
    """[station] name: the pages' title and heading."""
    return cfg.get("station", "name", fallback="").strip() or DEFAULT_TITLE


def _buttons(cfg: configparser.ConfigParser) -> list[dict]:
    """[buttons] button1..buttonN = Label | URL, in number order, for the lower pane.
    A third part, Target=<window name>, opens the page in that browser window or
    tab instead of the pane (Target=_blank: a new tab every time). A blank button,
    or one missing its label or URL, is left out."""
    if not cfg.has_section("buttons"):
        return []
    numbered = []
    for key, value in cfg.items("buttons"):
        if not (key.startswith("button") and key[6:].isdigit()):
            continue
        label, url, *rest = [part.strip() for part in value.split("|")] + [""]
        button = {"label": label, "url": url}
        for part in rest:
            name, eq, target = part.partition("=")
            if eq and name.strip().lower() == "target" and target.strip():
                button["target"] = target.strip()
        if label and url:
            numbered.append((int(key[6:]), button))
    return [b for _, b in sorted(numbered, key=lambda n: n[0])]


# ── Forecast, airport observation and alerts (National Weather Service) ───────
#
# api.weather.gov: free, no key, US only. It asks every caller to name itself
# in the User-Agent with a way to reach whoever runs it ([forecast] contact).
# One thread polls it and writes forecast.json / forecast.js beside
# current.json. Each part keeps its last good copy when a fetch fails, so the
# page shows an old forecast with its age rather than nothing.

NWS_API = "https://api.weather.gov"
DEFAULT_CONTACT = "github.com/akienm/CC0AC-Weather"
FORECAST_EVERY_S = 3600      # the Weather Service reissues forecasts about hourly
OBSERVATION_EVERY_S = 1200   # airports report hourly, some more often
ALERTS_EVERY_S = 600         # a warning shouldn't wait an hour
POINT_EVERY_S = 86400        # the grid square for a location rarely changes
HOURS_KEPT = 48


def _nws_get(url: str, contact: str) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": f"CC0AC-Weather ({contact})", "Accept": "application/geo+json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _mph(wind: str | None) -> int | None:
    """'5 mph' or '5 to 10 mph' → the larger number."""
    nums = [int(w) for w in str(wind or "").split() if w.isdigit()]
    return max(nums) if nums else None


def _value(q: dict | None, scale: float = 1.0, places: int = 0):
    """A Weather Service quantity ({"value": ..., "unitCode": ...}) → a plain number."""
    v = (q or {}).get("value")
    return None if v is None else round(v * scale, places) if places else round(v * scale)


def _miles_between(lat1, lon1, lat2, lon2) -> float:
    from math import asin, cos, radians, sin, sqrt
    a = sin(radians(lat2 - lat1) / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(radians(lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * asin(sqrt(a))


def _hourly(doc: dict) -> dict:
    props = doc["properties"]
    return {"updated": props.get("updateTime") or props.get("generatedAt"), "periods": [
        {"start": p["startTime"], "day": p.get("isDaytime"), "temp_f": p.get("temperature"),
         "pop": _value(p.get("probabilityOfPrecipitation")),
         "humidity_pct": _value(p.get("relativeHumidity")),
         "wind_mph": _mph(p.get("windSpeed")), "wind_from": p.get("windDirection"),
         "sky": p.get("shortForecast")}
        for p in props["periods"][:HOURS_KEPT]]}


def _daily(doc: dict) -> dict:
    props = doc["properties"]
    return {"updated": props.get("updateTime") or props.get("generatedAt"), "periods": [
        {"name": p["name"], "start": p["startTime"], "day": p.get("isDaytime"),
         "temp_f": p.get("temperature"), "pop": _value(p.get("probabilityOfPrecipitation")),
         "wind": p.get("windSpeed"), "wind_from": p.get("windDirection"),
         "sky": p.get("shortForecast"), "detail": p.get("detailedForecast")}
        for p in props["periods"]]}


def _observation(station: dict, doc: dict, lat: float, lon: float) -> dict:
    props, s = doc["properties"], station["properties"]
    s_lon, s_lat = station["geometry"]["coordinates"][:2]
    return {"station": s["stationIdentifier"], "name": s.get("name"),
            "distance_mi": round(_miles_between(lat, lon, s_lat, s_lon), 1),
            "time": props.get("timestamp"), "sky": props.get("textDescription"),
            "visibility_mi": _value(props.get("visibility"), 1 / 1609.344, 1),
            "clouds": [{"amount": c.get("amount"), "base_ft": _value(c.get("base"), 3.28084)}
                       for c in props.get("cloudLayers") or []]}


def _alerts(doc: dict) -> list:
    return [{"event": p.get("event"), "severity": p.get("severity"), "headline": p.get("headline"),
             "onset": p.get("onset"), "ends": p.get("ends") or p.get("expires"),
             "description": p.get("description"), "instruction": p.get("instruction")}
            for p in (f["properties"] for f in doc.get("features", []))]


def run_forecast(cfg: configparser.ConfigParser, write_path: Path) -> None:
    station = _station(cfg)
    if not station:
        log.warning("Forecast is on but [station] latitude/longitude are not set; forecast off")
        return
    lat, lon = station["latitude"], station["longitude"]
    contact = cfg.get("forecast", "contact", fallback=DEFAULT_CONTACT).strip() or DEFAULT_CONTACT
    out = {"source": "National Weather Service", "errors": {}}
    due = {"point": 0.0, "forecast": 0.0, "observation": 0.0, "alerts": 0.0}
    point = stations = None

    def fetch(part, every, work):
        if time.time() < due[part]:
            return False
        try:
            work()
            out["errors"].pop(part, None)
            due[part] = time.time() + every
        except Exception as exc:
            # Loud in the log, and named on the page; the last good copy stays.
            log.warning("Weather Service %s fetch failed: %s", part, exc)
            out["errors"][part] = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}: {exc}"
            due[part] = time.time() + 300
        return True

    while True:
        def get_point():
            nonlocal point, stations
            point = _nws_get(f"{NWS_API}/points/{lat:.4f},{lon:.4f}", contact)["properties"]
            stations = _nws_get(point["observationStations"], contact)["features"]
        changed = fetch("point", POINT_EVERY_S, get_point)
        if point:
            def get_forecast():
                out["hourly"] = _hourly(_nws_get(point["forecastHourly"], contact))
                out["daily"] = _daily(_nws_get(point["forecast"], contact))
            def get_observation():
                nearest = stations[0]
                doc = _nws_get(f"{NWS_API}/stations/{nearest['properties']['stationIdentifier']}/observations/latest", contact)
                out["observation"] = _observation(nearest, doc, lat, lon)
            def get_alerts():
                out["alerts"] = _alerts(_nws_get(f"{NWS_API}/alerts/active?point={lat:.4f},{lon:.4f}", contact))
            changed |= fetch("forecast", FORECAST_EVERY_S, get_forecast)
            changed |= fetch("observation", OBSERVATION_EVERY_S, get_observation)
            changed |= fetch("alerts", ALERTS_EVERY_S, get_alerts)
        if changed:
            out["written"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            body = json.dumps(out, indent=2)
            _write_atomic(write_path / "forecast.json", body)
            _write_atomic(write_path / "forecast.js", f"window.ACURITE_FORECAST = {body};\n")
        time.sleep(60)


# ── Web server ───────────────────────────────────────────────────────────────
#
# Serves the dashboard and its data, read-only, and nothing else: no directory
# listing, no other files. The pages come from beside this script (so a repo
# update shows at once); the data comes from write_path.
#
# [web] pages names a folder of your own: a page, stylesheet, script or image
# put there is served by its name, and one named like a shipped page (theme.css,
# weather.html, charts.html) replaces it. An update never touches that folder.

DEFAULT_TITLE = "CC0AC Weather"
SHIPPED_PAGES = ("weather.html", "charts.html", "theme.css")
PAGE_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "application/javascript",
              ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml",
              ".ico": "image/x-icon", ".webp": "image/webp"}
PAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
DATA_FILES = {"/current.js": "application/javascript", "/current.json": "application/json",
              "/forecast.js": "application/javascript", "/forecast.json": "application/json",
              "/history.csv": "text/csv"}


def _find_page(name: str, own_dir: Path | None, page_dir: Path) -> Path | None:
    """The file to serve for /<name>: yours first, then the shipped one."""
    if not PAGE_NAME.fullmatch(name) or Path(name).suffix.lower() not in PAGE_TYPES:
        return None
    if own_dir is not None and (own_dir / name).is_file():
        return own_dir / name
    return page_dir / name if name in SHIPPED_PAGES else None


def _make_web_handler(write_path: Path, db_path: Path | None = None, sensors: dict | None = None,
                      own_dir: Path | None = None, title: str = DEFAULT_TITLE):
    page_dir = Path(__file__).resolve().parent

    class WebHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            path = url.path
            if path == "/history.json":
                self._history(dict(urllib.parse.parse_qsl(url.query)))
                return
            if path in DATA_FILES:
                target, ctype = write_path / path.lstrip("/"), DATA_FILES[path]
            else:
                target = _find_page(path.lstrip("/") or "weather.html", own_dir, page_dir)
                if target is None:
                    self.send_error(404)
                    return
                ctype = PAGE_TYPES[target.suffix.lower()]
            try:
                body = target.read_bytes()
            except FileNotFoundError:
                self.send_error(404, "No data yet")
                return
            self._send(body, ctype)

        def _history(self, q: dict):
            if db_path is None or not db_path.exists():
                self.send_error(404, "No database yet")
                return
            try:
                data = {"title": title, **history(db_path, sensors or {}, q)}
            except ValueError as e:
                self.send_error(400, str(e))
                return
            except sqlite3.Error as e:
                log.error("history query failed: %s", e)
                self.send_error(500, "Database error")
                return
            self._send(json.dumps(data, separators=(",", ":")).encode(), "application/json")

        def _send(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

    return WebHandler


def run_web_server(cfg: configparser.ConfigParser, write_path: Path) -> None:
    # One or more ports, comma-separated: e.g. "12345, 80" when a router can only
    # forward 80 to 80 but the inside address should stay easy to remember.
    ports = [int(p) for p in cfg.get("web", "port", fallback="12345").split(",") if p.strip()]
    own = cfg.get("web", "pages", fallback="").strip()
    own_dir = Path(own).expanduser() if own else None
    if own_dir is not None and not own_dir.is_dir():
        log.warning("[web] pages folder %s not found; serving the shipped pages only", own_dir)
    handler = _make_web_handler(write_path, _db_path(cfg), sensor_map(cfg), own_dir, _title(cfg))
    servers = [ThreadingHTTPServer(("", port), handler) for port in ports]
    for port, server in zip(ports[1:], servers[1:]):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Web server on port(s) %s", ", ".join(map(str, ports)))
    servers[0].serve_forever()


# ── Discover mode ─────────────────────────────────────────────────────────────

def run_discover(cfg: configparser.ConfigParser, duration_s: int = 300) -> None:
    """Print all AcuRite sensors in range for duration_s seconds then exit."""
    cmd = rtl433_cmd(cfg)
    print(f"\n{'─'*60}")
    print(f"DISCOVER MODE — listening {duration_s}s for AcuRite sensors")
    print(f"rtl_433: {' '.join(cmd)}")
    print(f"{'─'*60}\n")

    deadline = time.monotonic() + duration_s
    seen: dict[str, dict] = {}

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
        )
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            packet = parse_packet(raw)
            if packet is None:
                continue
            sid = packet["sensor_id"]
            if sid not in seen:
                seen[sid] = packet
                print(f"  Sensor ID : {sid}")
                print(f"  Model     : {packet['model']}")
                if packet.get("temp_f") is not None:
                    print(f"  Temp      : {packet['temp_f']}°F")
                if packet.get("humidity_pct") is not None:
                    print(f"  Humidity  : {packet['humidity_pct']}%")
                if packet.get("wind_mph") is not None:
                    print(f"  Wind      : {packet['wind_mph']} mph @ {packet.get('wind_dir_deg', '?')}°")
                if packet.get("rain_in") is not None:
                    print(f"  Rain      : {packet['rain_in']}\"")
                print()
    except KeyboardInterrupt:
        print("\n(stopped early)")
    finally:
        proc.terminate()
        proc.wait()

    print(f"{'─'*60}")
    print(f"Found {len(seen)} sensor(s). Add to ~/.cc0ac-weather/config.ini under [sensors]:\n")
    for sid, p in seen.items():
        print(f"  {sid} = My {p['model']}")
    print(f"\n{'─'*60}\n")


# ── Daemon ────────────────────────────────────────────────────────────────────

def load_sources() -> list:
    """Every source module in the sources/ folder beside this script."""
    folder = Path(__file__).resolve().parent / "sources"
    return [importlib.import_module(f"sources.{p.stem}")
            for p in sorted(folder.glob("*.py")) if not p.name.startswith("_")]


def run_daemon(cfg: configparser.ConfigParser) -> None:
    write_path = Path(cfg.get("device", "write_path")).expanduser()
    write_path.mkdir(parents=True, exist_ok=True)
    csv_path = write_path / "weather.csv"

    sensors = sensor_map(cfg)
    whitelist = set(sensors.keys()) if sensors else None

    # The dashboard reads these settings from current.json; it cannot read config.ini.
    # The lower pane is a row of [buttons], each loading its page below; none set, no pane.
    page = {"title": _title(cfg), "lower_buttons": _buttons(cfg), "station": _station(cfg)}
    db_path = _db_path(cfg)

    def emit(reading: Reading) -> dict:
        return record_reading(reading, write_path, sensors, page, db_path)

    source_threads = []
    for source in load_sources():
        if source.enabled(cfg):
            thread = threading.Thread(target=source.run, args=(cfg, emit), daemon=True, name=source.NAME)
            thread.start()
            source_threads.append(thread)

    if cfg.getboolean("web", "enabled", fallback=False):
        threading.Thread(target=run_web_server, args=(cfg, write_path), daemon=True).start()

    if cfg.getboolean("forecast", "enabled", fallback=False):
        threading.Thread(target=run_forecast, args=(cfg, write_path), daemon=True).start()

    if not cfg.getboolean("capture", "enabled", fallback=False):
        log.info("Radio capture off ([capture] enabled = false) — hub relay only")
        for thread in source_threads:
            thread.join()
        return

    cmd = rtl433_cmd(cfg)
    log.info("Starting — writing to %s", csv_path)
    if whitelist:
        log.info("Sensor whitelist: %s", sorted(whitelist))
    else:
        log.info("No [sensors] configured — capturing all AcuRite sensors (consider adding IDs to suppress neighbor noise)")

    while True:
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
            )
            log.info("rtl_433 started (pid %d)", proc.pid)
            for line in proc.stdout:
                try:
                    raw = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue

                packet = parse_packet(raw)
                if packet is None:
                    continue

                sid = packet["sensor_id"]
                if whitelist and sid not in whitelist:
                    log.debug("Skipping unknown sensor %s", sid)
                    continue

                now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                row = {
                    "timestamp": now,
                    "sensor_id": sid,
                    "sensor_name": sensors.get(sid, sid),
                    "model": packet["model"],
                    "temp_f": packet.get("temp_f"),
                    "humidity_pct": packet.get("humidity_pct"),
                    "wind_mph": packet.get("wind_mph"),
                    "wind_dir_deg": packet.get("wind_dir_deg"),
                    "wind_gust_mph": packet.get("wind_gust_mph"),
                    "rain_in": packet.get("rain_in"),
                    "pressure_inhg": packet.get("pressure_inhg"),
                    "dew_point_f": packet.get("dew_point_f"),
                    "uv_index": packet.get("uv_index"),
                    "battery_ok": packet.get("battery_ok"),
                }
                append_csv(csv_path, row)
                log.info(
                    "sensor=%s name=%r temp_f=%s humidity=%s wind_mph=%s",
                    sid, sensors.get(sid, sid),
                    packet.get("temp_f"), packet.get("humidity_pct"), packet.get("wind_mph"),
                )

                threading.Thread(
                    target=upload_wu, args=(cfg, packet), daemon=True
                ).start()

            ret = proc.wait()
            log.warning("rtl_433 exited (code %d) — restarting in 10s", ret)
            time.sleep(10)

        except Exception as exc:
            log.error("Daemon error: %s — restarting in 10s", exc)
            time.sleep(10)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AcuRite weather sensor capture — writes weather.csv for cloud sync"
    )
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG,
        help=f"Path to config.ini (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--discover", action="store_true",
        help="Print all AcuRite sensors in range for --discover-time seconds and exit",
    )
    parser.add_argument(
        "--discover-time", type=int, default=300,
        metavar="SECONDS",
        help="How long to listen in discover mode (default: 300)",
    )
    parser.add_argument(
        "--import-raw", action="store_true",
        help="Load every reading in the raw hub logs into the database and exit (safe to re-run)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cfg = load_config(args.config)

    if args.import_raw:
        import_raw(cfg)
    elif args.discover:
        run_discover(cfg, args.discover_time)
    else:
        run_daemon(cfg)


if __name__ == "__main__":
    main()
