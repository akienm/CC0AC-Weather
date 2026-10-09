#!/usr/bin/env python3
"""
acurite-capture.py — AcuRite weather sensor capture daemon.

Two sources, either or both:
  * the AcuRite Access hub: an HTTPS listener the hub reports to, which records
    each reading and relays it on to AcuRite unchanged ([hub], sources/acurite_access.py);
  * rtl_433 with a USB radio dongle, decoding the sensors off the air
    ([capture], sources/rtl_433.py).
Each source in sources/ turns what it hears into Readings (sources/__init__.py);
readings land in <write_path>/current.json, current.js, history.csv and the
database, and go on to each output switched on in outputs/ (such as Weather
Underground for a station heard by radio).
With [forecast] on, the National Weather Service forecast, nearest airport
observation and active alerts land in <write_path>/forecast.json and forecast.js.

Requirements: Python 3 only, for the hub. For a radio dongle:
    sudo apt install rtl-433 rtl-sdr
    sudo usermod -aG plugdev $USER    # dongle access without root; log in again after

Setup:
    cp config.ini.example ~/.cc0ac-weather/config.ini
    python3 acurite-capture.py --discover   # with a dongle: find your sensor IDs
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
import ipaddress
import json
import logging
import os
import queue
import re
import shutil
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sources import FIELDS, OUTDOOR_TYPES, Reading
from sources import acurite_access

log = logging.getLogger("acurite")

DEFAULT_CONFIG = Path.home() / ".cc0ac-weather" / "config.ini"

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
    """{sensor_id: display name} from [sensors], in display order: the sensors
    listed in [sensor_order] by their numbers, then the rest by name."""
    if not cfg.has_section("sensors"):
        return {}
    names = dict(cfg.items("sensors"))
    ordered = []
    if cfg.has_section("sensor_order"):
        slots = sorted(cfg.items("sensor_order"),
                       key=lambda kv: (0, int(kv[0]), "") if kv[0].isdigit() else (1, 0, kv[0]))
        for _, sid in slots:
            sid = sid.strip().lower()
            if sid not in names:
                log.warning("[sensor_order] lists %s, which isn't in [sensors]; left out", sid)
            elif sid not in ordered:
                ordered.append(sid)
    rest = sorted((s for s in names if s not in ordered), key=lambda s: names[s].lower())
    return {s: names[s] for s in ordered + rest}


def display_order(sensors: dict, sensor_id: str, name: str) -> tuple:
    """Sort key: sensors in sensor_map's order, then any others by name."""
    position = list(sensors).index(sensor_id) if sensor_id in sensors else len(sensors)
    return position, name.lower()



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


def _write_snapshot(write_path: Path, sensors: dict, page: dict | None, written: str, hub: str) -> None:
    """current.json / current.js from _current; the caller holds _current_lock."""
    snapshot = {"written": written, "hub": hub,
                **{k: v for k, v in (page or {}).items() if v},
                **{k: v for k, v in _current_page.items() if v is not None},
                "sensors": sorted(_current.values(),
                                  key=lambda e: display_order(sensors, e["sensor_id"], e["name"]))}
    body = json.dumps(snapshot, indent=1)
    _write_atomic(write_path / "current.json", body)
    # current.js lets the dashboard load the data with a <script> tag, which
    # works from a file:// URL and from any static host without CORS.
    _write_atomic(write_path / "current.js", f"window.ACURITE_CURRENT = {body};\n")


def refresh_snapshot(write_path: Path, sensors: dict, page: dict | None) -> None:
    """At start, carry the last snapshot's sensors over and write it again with
    the settings just read. A change in config.ini (buttons, names, sensor
    order, title) then shows at once rather than at the next reading, and the
    first reading after a restart doesn't leave the other sensors off the page."""
    try:
        old = json.loads((write_path / "current.json").read_text())
    except (OSError, ValueError):
        return
    with _current_lock:
        for entry in old.get("sensors", []):
            sid = entry.get("sensor_id") if isinstance(entry, dict) else None
            if sid and isinstance(entry.get("fields"), dict):
                entry["name"] = sensors.get(sid, entry.get("name") or f"{entry.get('type', 'unknown')} {sid}")
                _current.setdefault(sid, entry)
        if old.get("pressure_change_3h") is not None:
            _current_page.setdefault("pressure_change_3h", old["pressure_change_3h"])
        if _current:
            _write_snapshot(write_path, sensors, page, old.get("written", ""), old.get("hub", ""))


def record_reading(reading: Reading, write_path: Path, sensors: dict, page: dict | None = None,
                   db_path: Path | None = None) -> dict:
    """Fold one reading into current.json / current.js, append history.csv,
    and store it in the database at db_path. Returns the sensor's current entry.

    page: settings the dashboard reads from the data file (it cannot read
    config.ini), e.g. {"lower_buttons": [{"label": ..., "url": ...}]}; empty values are left out."""
    global _pressure_seeded, _db, _rolled_through
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
        _write_snapshot(write_path, sensors, page, now, reading.receiver or "")
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
                # The first reading of a new day sums up the days just finished.
                day = _local_day(_epoch(now))
                if day != _rolled_through:
                    n = roll_up(_db, _epoch(now))
                    if n:
                        log.info("Daily summaries: %d day(s) summed up", n)
                    _rolled_through = day
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
_rolled_through: str | None = None      # local day daily summaries were last brought up to


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
    open_daily(con)
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
    days = roll_up(con, time.time(), rebuild=True)
    total = con.execute("SELECT count(*) FROM readings").fetchone()[0]
    print(f"{days} days summed up; ", end="")
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


# For SQL: the outdoor sensor types, as ('Atlas', 'Iris', '5N1').
OUTDOOR = "(" + ", ".join(f"'{t}'" for t in OUTDOOR_TYPES) + ")"


def _local_days(con: sqlite3.Connection, lo: float | None = None, hi: float | None = None) -> list:
    """[(local date 'YYYY-MM-DD', that day's rain)] from the outdoor sensor's dailyrainin."""
    where, args = "", []
    if lo is not None:
        where, args = "AND received_utc >= ? AND received_utc < ?", [_iso(lo), _iso(hi)]
    return con.execute(f"""SELECT date(received_utc, 'localtime') AS d, max(rain_day_in)
        FROM readings WHERE type IN {OUTDOOR} AND rain_day_in IS NOT NULL {where}
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
        rows = con.execute(f"""SELECT received_utc, rain_day_in FROM readings
            WHERE type IN {OUTDOOR} AND rain_day_in IS NOT NULL AND received_utc < ?
              AND received_utc >= (SELECT coalesce(max(received_utc), '') FROM readings
                                   WHERE type IN {OUTDOOR} AND rain_day_in IS NOT NULL AND received_utc < ?)
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
            FROM readings WHERE type IN {OUTDOOR} AND received_utc >= :lo AND received_utc < :hi
            GROUP BY b""", span).fetchall()
        rooms = []
        for sid, kind in con.execute(f"""SELECT DISTINCT sensor_id, type FROM readings
                WHERE type NOT IN {OUTDOOR} AND received_utc >= ? AND received_utc < ?""", [_iso(lo), _iso(hi)]):
            temp, hum = column(con.execute(f"""SELECT {bucket} AS b, avg(temp_f), avg(humidity_pct)
                FROM readings WHERE sensor_id = :sid AND received_utc >= :lo AND received_utc < :hi
                GROUP BY b""", {**span, "sid": sid}).fetchall(), 2)
            rooms.append({"id": sid, "name": sensors.get(sid, f"{kind} {sid}"), "temp_f": temp, "humidity_pct": hum})
        rooms.sort(key=lambda r: display_order(sensors, r["id"], r["name"]))
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


# ── Daily summaries ───────────────────────────────────────────────────────────
#
# One row per sensor per local day in the `daily` table: lows, highs, averages
# and the day's rain. A day is summed up once it is over (when the first reading
# of the next day arrives); today is worked out from the readings when asked.
# Weeks (starting Sunday), months and years are worked out from the days.

DAILY = {   # column → SQL over that day's readings
    "readings": "count(*)",
    "temp_min": "min(temp_f)", "temp_max": "max(temp_f)", "temp_avg": "avg(temp_f)",
    "humidity_min": "min(humidity_pct)", "humidity_max": "max(humidity_pct)",
    "humidity_avg": "avg(humidity_pct)",
    "dew_point_avg": "avg(dew_point_f)",
    "feels_like_min": "min(feels_like_f)", "feels_like_max": "max(feels_like_f)",
    "wind_avg": "avg(wind_mph)", "wind_gust_max": "max(wind_gust_mph)",
    "rain_in": "max(rain_day_in)",
    "pressure_min": "min(pressure_inhg)", "pressure_max": "max(pressure_inhg)",
    "pressure_avg": "avg(pressure_inhg)",
    "uv_max": "max(uv_index)", "light_max": "max(light_lux)",
}
# How a longer period combines its days' values, by column-name ending.
_COMBINE = {"_min": min, "_max": max, "_avg": lambda v: sum(v) / len(v), "rain_in": sum, "readings": sum}


def _combine(name: str, values: list):
    values = [v for v in values if v is not None]
    if not values:
        return None
    how = next(f for end, f in _COMBINE.items() if name.endswith(end))
    return int(how(values)) if name == "readings" else round(how(values), 2)


def open_daily(con: sqlite3.Connection) -> None:
    con.execute(f"""CREATE TABLE IF NOT EXISTS daily (
        day TEXT NOT NULL,           -- local date, YYYY-MM-DD
        sensor_id TEXT NOT NULL,
        type TEXT,
        {", ".join(f"{c} {'INTEGER' if c == 'readings' else 'REAL'}" for c in DAILY)},
        PRIMARY KEY (day, sensor_id)
    )""")


def _local_day(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")


def _day_bounds(day: str) -> list[str]:
    """A local date's start and end as UTC times, as received_utc is stored."""
    start = datetime.strptime(day, "%Y-%m-%d")
    end = datetime.fromordinal(start.toordinal() + 1)
    return [_iso(start.astimezone().timestamp()), _iso(end.astimezone().timestamp())]


def _day_rows(con: sqlite3.Connection, day: str) -> list[dict]:
    rows = con.execute(f"""SELECT sensor_id, max(type), {", ".join(DAILY.values())} FROM readings
        WHERE received_utc >= ? AND received_utc < ? GROUP BY sensor_id""", _day_bounds(day)).fetchall()
    return [{"day": day, "sensor_id": r[0], "type": r[1],
             **{c: None if v is None else round(v, 2) for c, v in zip(DAILY, r[2:])}} for r in rows]


def roll_up(con: sqlite3.Connection, now: float, rebuild: bool = False) -> int:
    """Sum up every finished day not yet in `daily` (every day, with rebuild).
    Returns how many days were summed up."""
    open_daily(con)
    if rebuild:
        con.execute("DELETE FROM daily")
    last = con.execute("SELECT max(day) FROM daily").fetchone()[0]
    first = con.execute("SELECT min(received_utc) FROM readings").fetchone()[0]
    if first is None:
        con.commit()
        return 0
    start = (datetime.strptime(last, "%Y-%m-%d").toordinal() + 1 if last
             else datetime.strptime(_local_day(_epoch(first)), "%Y-%m-%d").toordinal())
    today = datetime.strptime(_local_day(now), "%Y-%m-%d").toordinal()
    done = 0
    for n in range(start, today):
        rows = _day_rows(con, datetime.fromordinal(n).strftime("%Y-%m-%d"))
        for r in rows:
            con.execute(f"INSERT OR REPLACE INTO daily ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})",
                        list(r.values()))
        done += bool(rows)
    con.commit()
    return done


def _period_start(day: str, period: str) -> str:
    if period == "weeks":   # weeks start on Sunday, as rain_totals has them
        d = datetime.strptime(day, "%Y-%m-%d").date()
        return datetime.fromordinal(d.toordinal() - d.isoweekday() % 7).strftime("%Y-%m-%d")
    return day[:7] if period == "months" else day[:4]


def summaries(db_path: Path, sensors: dict, q: dict) -> dict:
    """Days, weeks, months and years for every sensor. q: days=N, how many of the
    latest days to list (default 400); weeks, months and years are listed in full."""
    try:
        keep = max(int(q.get("days", 400)), 1)
    except ValueError:
        raise ValueError("days must be a number")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        today = _local_day(time.time())
        stored = []
        if con.execute("SELECT 1 FROM sqlite_master WHERE name = 'daily'").fetchone():
            stored = [dict(zip(["day", "sensor_id", "type", *DAILY], r)) for r in con.execute(
                f"SELECT day, sensor_id, type, {', '.join(DAILY)} FROM daily WHERE day < ? ORDER BY day", [today])]
        days = stored + _day_rows(con, today)
    finally:
        con.close()
    out = []
    for sid in sorted({r["sensor_id"] for r in days}):
        mine = [r for r in days if r["sensor_id"] == sid]
        kind = mine[-1]["type"]
        entry = {"id": sid, "name": sensors.get(sid, f"{kind} {sid}"), "type": kind,
                 "outdoor": kind in OUTDOOR_TYPES,
                 "days": [{k: v for k, v in r.items() if k not in ("sensor_id", "type")} for r in mine[-keep:]]}
        for period in ("weeks", "months", "years"):
            groups: dict[str, list] = {}
            for r in mine:
                groups.setdefault(_period_start(r["day"], period), []).append(r)
            entry[period] = [{"start": k, "days": len(g), **{c: _combine(c, [r[c] for r in g]) for c in DAILY}}
                             for k, g in groups.items()]
        out.append(entry)
    out.sort(key=lambda e: display_order(sensors, e["id"], e["name"]))
    return {"today": today, "sensors": out}


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


# From outside your network (anything through the router's port forward) only the
# dashboard and the charts are served, with what those two pages load; everything
# else answers 404. Your own network gets every page, the data files and /config.cgi.
OUTSIDE_PATHS = {"/", "/weather.html", "/charts.html", "/theme.css",
                 "/current.js", "/forecast.js", "/history.json"}

# /config.cgi edits config.ini in the browser: one pane, Save and Revert. It is
# answered only for addresses on your own network (and this machine); a request
# through the router's port forward comes from an outside address and gets a 404.
# There is no password yet, so anyone on your network can use it. Save checks the
# file reads as an ini file, keeps the old one as config.ini.previous, writes the
# new one and restarts the service so it takes effect.

CONFIG_MAX_BYTES = 1_000_000


def _local_client(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local


def _local_host(host: str) -> bool:
    """The address the browser asked for: an address on your network, localhost,
    or a bare or .local machine name. Another site's name pointed at this machine
    (DNS rebinding) is refused."""
    if host.startswith("["):                       # [IPv6]:port
        name = host[1:].split("]", 1)[0]
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    name = name.rstrip(".").lower()
    if not name:
        return False
    if name == "localhost" or name.endswith(".local") or "." not in name and ":" not in name:
        return True
    return _local_client(name)


def check_config(text: str) -> str | None:
    """Why this text can't be config.ini, or None if it reads cleanly."""
    cfg = configparser.ConfigParser()
    try:
        cfg.read_string(text, source="config.ini")
        for section in cfg.sections():
            cfg.items(section)          # a broken %(name)s shows up only when read
    except configparser.Error as e:
        return f"Not saved: {e}"
    return None


def save_config(config_path: Path, text: str) -> None:
    """Keep the old file as config.ini.previous, then replace it in one step,
    with the same permissions (it holds passwords)."""
    mode = config_path.stat().st_mode & 0o777 if config_path.exists() else 0o600
    if config_path.exists():
        shutil.copy2(config_path, config_path.with_name(config_path.name + ".previous"))
    tmp = config_path.with_name(config_path.name + ".saving")
    tmp.write_text(text)
    tmp.chmod(mode)
    tmp.replace(config_path)


def restart_soon(delay: float = 1.0) -> None:
    """Restart the whole program once the browser has its answer. Under systemd
    the service exits and systemd starts it again (Restart=always), which also
    ends a running rtl_433; run by hand, it starts itself over in place."""
    def restart():
        time.sleep(delay)
        for handler in logging.getLogger().handlers:
            handler.flush()
        if os.environ.get("INVOCATION_ID"):
            os._exit(0)
        os.execv(sys.executable, [sys.executable, str(Path(sys.argv[0]).resolve()), *sys.argv[1:]])
    threading.Thread(target=restart, daemon=True).start()


def _find_page(name: str, own_dir: Path | None, page_dir: Path) -> Path | None:
    """The file to serve for /<name>: yours first, then the shipped one."""
    if not PAGE_NAME.fullmatch(name) or Path(name).suffix.lower() not in PAGE_TYPES:
        return None
    if own_dir is not None and (own_dir / name).is_file():
        return own_dir / name
    return page_dir / name if name in SHIPPED_PAGES else None


def _make_web_handler(write_path: Path, db_path: Path | None = None, sensors: dict | None = None,
                      own_dir: Path | None = None, title: str = DEFAULT_TITLE,
                      config_path: Path | None = None):
    page_dir = Path(__file__).resolve().parent

    class WebHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            path = url.path
            if path not in OUTSIDE_PATHS and not _local_client(self.client_address[0]):
                self.send_error(404)
                return
            if path == "/config.cgi":
                self._config_page(dict(urllib.parse.parse_qsl(url.query)))
                return
            if path in ("/history.json", "/summaries.json"):
                self._query(history if path == "/history.json" else summaries,
                            dict(urllib.parse.parse_qsl(url.query)))
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

        def _config_allowed(self) -> bool:
            if (config_path is not None and _local_client(self.client_address[0])
                    and _local_host(self.headers.get("Host", ""))):
                return True
            self.send_error(404)
            return False

        def _config_page(self, q: dict):
            if not self._config_allowed():
                return
            if q.get("raw"):
                self._send(config_path.read_bytes(), "text/plain; charset=utf-8")
            else:
                self._send((page_dir / "config.html").read_bytes(), PAGE_TYPES[".html"])

        def do_POST(self):
            if urllib.parse.urlparse(self.path).path != "/config.cgi":
                self.send_error(404)
                return
            if not self._config_allowed():
                return
            # Only the page's own script sends this header; a form on another
            # site can't, so it can't save over your config from your browser.
            if self.headers.get("X-CC0AC-Config") != "save":
                self.send_error(403)
                return
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= CONFIG_MAX_BYTES:
                self._answer(400, "Not saved: empty, or too big for a config file.")
                return
            try:
                text = self.rfile.read(length).decode("utf-8")
            except UnicodeDecodeError:
                self._answer(400, "Not saved: the text isn't UTF-8.")
                return
            problem = check_config(text)
            if problem:
                self._answer(400, problem)
                return
            try:
                save_config(config_path, text)
            except OSError as e:
                log.error("Saving %s failed: %s", config_path, e)
                self._answer(500, f"Not saved: {e}")
                return
            log.info("config.ini saved from %s; restarting", self.client_address[0])
            self._answer(200, "Saved; restarting.")
            restart_soon()

        def _answer(self, code: int, message: str):
            body = message.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _query(self, answer, q: dict):
            if db_path is None or not db_path.exists():
                self.send_error(404, "No database yet")
                return
            try:
                data = {"title": title, **answer(db_path, sensors or {}, q)}
            except ValueError as e:
                self.send_error(400, str(e))
                return
            except sqlite3.Error as e:
                log.error("%s query failed: %s", answer.__name__, e)
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


def run_web_server(cfg: configparser.ConfigParser, write_path: Path, config_path: Path | None = None) -> None:
    # One or more ports, comma-separated: e.g. "12345, 80" when a router can only
    # forward 80 to 80 but the inside address should stay easy to remember.
    ports = [int(p) for p in cfg.get("web", "port", fallback="12345").split(",") if p.strip()]
    own = cfg.get("web", "pages", fallback="").strip()
    own_dir = Path(own).expanduser() if own else None
    if own_dir is not None and not own_dir.is_dir():
        log.warning("[web] pages folder %s not found; serving the shipped pages only", own_dir)
    handler = _make_web_handler(write_path, _db_path(cfg), sensor_map(cfg), own_dir, _title(cfg), config_path)
    servers = [ThreadingHTTPServer(("", port), handler) for port in ports]
    for port, server in zip(ports[1:], servers[1:]):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Web server on port(s) %s", ", ".join(map(str, ports)))
    servers[0].serve_forever()


# ── Daemon ────────────────────────────────────────────────────────────────────

def _plugins(folder: str) -> list:
    """Every module in a plug-in folder (sources/, outputs/) beside this script."""
    here = Path(__file__).resolve().parent / folder
    return [importlib.import_module(f"{folder}.{p.stem}")
            for p in sorted(here.glob("*.py")) if not p.name.startswith("_")]


def load_sources() -> list:
    return _plugins("sources")


def load_outputs() -> list:
    return _plugins("outputs")


def _run_output(cfg: configparser.ConfigParser, output, todo: queue.Queue) -> None:
    """Hand each reading to one output, in order; a failure loses that reading only."""
    while True:
        reading = todo.get()
        try:
            output.send(cfg, reading)
        except Exception as exc:
            log.warning("%s output failed: %s", output.NAME, exc)


def run_daemon(cfg: configparser.ConfigParser, config_path: Path | None = None) -> None:
    write_path = Path(cfg.get("device", "write_path")).expanduser()
    write_path.mkdir(parents=True, exist_ok=True)
    sensors = sensor_map(cfg)

    # The dashboard reads these settings from current.json; it cannot read config.ini.
    # The lower pane is a row of [buttons], each loading its page below; none set, no pane.
    page = {"title": _title(cfg), "lower_buttons": _buttons(cfg), "station": _station(cfg)}
    db_path = _db_path(cfg)
    refresh_snapshot(write_path, sensors, page)

    queues = []
    for output in load_outputs():
        if output.enabled(cfg):
            todo: queue.Queue = queue.Queue(maxsize=1000)
            threading.Thread(target=_run_output, args=(cfg, output, todo), daemon=True,
                             name=output.NAME).start()
            queues.append((output.NAME, todo))
            log.info("Output on: %s", output.NAME)

    def emit(reading: Reading) -> dict:
        entry = record_reading(reading, write_path, sensors, page, db_path)
        for name, todo in queues:
            try:
                todo.put_nowait(reading)
            except queue.Full:
                log.warning("%s output is behind; dropped a reading", name)
        return entry

    if cfg.getboolean("web", "enabled", fallback=False):
        threading.Thread(target=run_web_server, args=(cfg, write_path, config_path), daemon=True).start()

    if cfg.getboolean("forecast", "enabled", fallback=False):
        threading.Thread(target=run_forecast, args=(cfg, write_path), daemon=True).start()

    source_threads = []
    for source in load_sources():
        if source.enabled(cfg):
            thread = threading.Thread(target=source.run, args=(cfg, emit), daemon=True, name=source.NAME)
            thread.start()
            source_threads.append(thread)
            log.info("Source on: %s", source.NAME)
    if not source_threads:
        log.warning("No source switched on ([hub] or [capture] enabled); nothing to record")
    for thread in source_threads:
        thread.join()
    # A source reading recordings ([capture] read) finishes; let the outputs drain.
    for _, todo in queues:
        while not todo.empty():
            time.sleep(0.2)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AcuRite weather sensor capture"
    )
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG,
        help=f"Path to config.ini (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--discover", action="store_true",
        help="With a radio dongle: print every sensor heard for --discover-time seconds and exit",
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
        from sources import rtl_433
        rtl_433.discover(cfg, args.discover_time)
    else:
        run_daemon(cfg, args.config.expanduser().resolve())


if __name__ == "__main__":
    main()
