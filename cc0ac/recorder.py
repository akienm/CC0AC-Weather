"""Recording: folds each reading into current.json / current.js, history.csv and the database,
and keeps what the dashboard shows between readings (reception, strikes, pressure trend).
Every source hands its readings to record_reading(); nothing here knows which source a reading came from."""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from cc0ac import diagnostics, sanity, upkeep, zambretti
from cc0ac.config import SensorNames, display_order
from cc0ac.fsutil import write_atomic
from cc0ac.sources import OUTDOOR_TYPES, Reading
from cc0ac.store import (
    HISTORY_FIELDS,
    day_bounds,
    epoch_to_day,
    iso_to_epoch,
    open_db,
    roll_up,
    store_reading,
)

log = logging.getLogger("acurite")


_current: dict[str, dict] = {}

current_lock = threading.Lock()

# Pressure trend: the barometer is the hub's, so every sensor's reading carries
# the same value. Forecasters read the change over 3 hours; we keep a little
# more than that, seeded from history.csv so a restart doesn't blank the trend.
PRESSURE_TREND_S = 3 * 3600

_pressure: deque = deque()   # (epoch seconds, inHg), guarded by current_lock

_pressure_seeded = False

_current_page: dict = {}     # derived values for the page, e.g. pressure_change_3h

# Reception: when each receiver heard each sensor over the last hour, so the page
# can show how many of the readings a sensor sent actually arrived. A sensor sends
# at a steady interval of its own (an AcuRite tower about every 16 s, the hub
# uploads every 5 min), so the interval is read from the gaps themselves: the
# shortest common gap, refined over the gaps that are a few intervals long.
HEARD_WINDOW_S = 3600

RECEIVERS = {"rtl_433": "radio", "acurite-access": "hub", "console": "console", "online": "online"}

_heard: dict[str, dict[str, dict]] = {}   # sensor_id → receiver → {"at": deque, "snr": [..]}; current_lock


def _receiver_of(source: str, receiver: str | None = None) -> str:
    if source in RECEIVERS:
        return RECEIVERS[source]
    # A stored row knows only the receiver: "rtl_433 dongle 0", "api.weather.gov" or a hub's id.
    r = receiver or ""
    return "radio" if r.startswith("rtl_433") else "online" if r.startswith("api.") else "hub"


def _note_heard(sensor_id: str, who: str, epoch: float, snr: float | None = None) -> None:
    h = _heard.setdefault(sensor_id, {}).setdefault(who, {"at": deque(), "snr": deque(), "since": epoch})
    h["at"].append(epoch)
    if snr is not None:
        h["snr"].append((epoch, snr))


def seed_heard(db_path: Path | None, now: float) -> None:
    """The last hour from the store, so a restart doesn't zero the counts. Two
    hours are read: a sensor heard before the hour began is not a new one."""
    if db_path is None or not db_path.exists():
        return
    since = datetime.fromtimestamp(now - 2 * HEARD_WINDOW_S, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        rows = con.execute("SELECT sensor_id, hub_id, received_utc FROM readings WHERE received_utc >= ?"
                           " ORDER BY received_utc", (since,)).fetchall()
        con.close()
    except sqlite3.Error as exc:
        log.warning("Reception counts start empty, the store is unreadable: %s", exc)
        return
    for sid, receiver, at in rows:
        _note_heard(sid, _receiver_of("", receiver), iso_to_epoch(at))


def _interval(times: list[float]) -> float | None:
    gaps = [b - a for a, b in zip(times, times[1:]) if b - a >= 2]
    if len(gaps) < 4:
        return None
    base = sorted(gaps)[len(gaps) // 10]
    # A missed reading makes a gap of two or three intervals; those still count,
    # and over many of them the whole-second rounding of the times averages out.
    steps = [(g, round(g / base)) for g in gaps if round(g / base) <= 8]
    return sum(g for g, _ in steps) / sum(n for _, n in steps)


class Reception(TypedDict, total=False):
    """How one receiver heard one sensor over the last hour. every_s, expected and
    percent need two or more messages; snr_db needs a radio that reports it."""
    heard: int
    last: str
    every_s: float
    expected: int
    percent: int
    snr_db: float


def reception(sensor_id: str, now: float) -> dict[str, Reception]:
    """{receiver: Reception} for the last hour."""
    out: dict[str, Reception] = {}
    for who, h in _heard.get(sensor_id, {}).items():
        while h["at"] and h["at"][0] < now - HEARD_WINDOW_S:
            h["at"].popleft()
        while h["snr"] and h["snr"][0][0] < now - HEARD_WINDOW_S:
            h["snr"].popleft()
        if not h["at"]:
            continue
        times = list(h["at"])
        every = _interval(times)
        r: Reception = {"heard": len(times), "last": datetime.fromtimestamp(times[-1], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        if every:
            # A sensor first heard less than an hour ago is expected only since then,
            # and that first reading is one more than the gaps after it.
            new = h["since"] > now - HEARD_WINDOW_S
            span = now - h["since"] if new else HEARD_WINDOW_S
            r["every_s"] = round(every, 1)
            r["expected"] = max(1, round(span / every) + (1 if new else 0))
            r["percent"] = min(100, round(100 * len(times) / r["expected"]))
        if h["snr"]:
            r["snr_db"] = round(sorted(s for _, s in h["snr"])[len(h["snr"]) // 2], 1)
        out[who] = r
    return out


def _seed_pressure(history: Path, now: float) -> None:
    """Load the last few hours of pressure from the tail of history.csv."""
    if not history.exists():
        return
    # Read back until the tail reaches past the trend's window: with many sensors
    # 256 KB is only an hour or two, and the trend would be blank that long.
    size, chunk = history.stat().st_size, 256 * 1024
    while True:
        with history.open("rb") as f:
            f.seek(max(0, size - chunk))
            tail = f.read().decode("utf-8", errors="replace").splitlines()[1:]
        first = next((r["timestamp"] for r in csv.DictReader(tail[:5], fieldnames=HISTORY_FIELDS)), None)
        try:
            covered = first is not None and now - iso_to_epoch(first) > PRESSURE_TREND_S * 1.2
        except (TypeError, ValueError):
            covered = False
        if covered or chunk >= size or chunk >= 64 * 1024 * 1024:
            break
        chunk *= 4
    for row in csv.DictReader(tail, fieldnames=HISTORY_FIELDS):
        try:
            t, p = iso_to_epoch(row["timestamp"]), float(row["pressure_inhg"])
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


def _write_snapshot(write_path: Path, sensors: SensorNames, page: dict | None, written: str, hub: str) -> None:
    """current.json / current.js from _current; the caller holds current_lock."""
    now = time.time()
    for e in _current.values():
        e["reception"] = reception(e["sensor_id"], now)
    snapshot = {"written": written, "hub": hub,
                **{k: v for k, v in (page or {}).items() if v},
                **{k: v for k, v in _current_page.items() if v is not None},
                "warnings": upkeep.problems(diagnostics.BOOT_ROOT) + upkeep.low_batteries(_current.values()),
                "sensors": sorted(_current.values(),
                                  key=lambda e: display_order(sensors, e["sensor_id"], e["name"]))}
    body = json.dumps(snapshot, indent=1)
    write_atomic(write_path / "current.json", body)
    # current.js lets the dashboard load the data with a <script> tag, which
    # works from a file:// URL and from any static host without CORS.
    write_atomic(write_path / "current.js", f"window.ACURITE_CURRENT = {body};\n")


def refresh_snapshot(write_path: Path, sensors: SensorNames, page: dict | None) -> None:
    """At start, carry the last snapshot's sensors over and write it again with
    the settings just read. A change in config.ini (buttons, names, sensor
    order, title) then shows at once rather than at the next reading, and the
    first reading after a restart doesn't leave the other sensors off the page."""
    try:
        old = json.loads((write_path / "current.json").read_text())
    except (OSError, ValueError):
        return
    with current_lock:
        for entry in old.get("sensors", []):
            sid = entry.get("sensor_id") if isinstance(entry, dict) else None
            if sid and isinstance(entry.get("fields"), dict):
                entry["name"] = sensors.get(sid, entry.get("name") or f"{entry.get('type', 'unknown')} {sid}")
                _current.setdefault(sid, entry)
        if old.get("pressure_change_3h") is not None:
            _current_page.setdefault("pressure_change_3h", old["pressure_change_3h"])
        if _current:
            _write_snapshot(write_path, sensors, page, old.get("written", ""), old.get("hub", ""))


# Lightning: a sensor keeps a running count of the strikes it has heard, which
# goes on from wherever it was (and starts again when its batteries come out), so
# the number it sends is not today's strikes. The page shows the strikes counted
# since local midnight: each rise in the count, and the whole count when it is
# smaller than the last one (it started again). Seeded from the store, so a
# restart doesn't lose the day's strikes. A count the source already keeps per
# day comes out the same. A new time of the last strike counts as at least one
# strike, whatever the count did. A rise also stamps last_strike_heard, our own
# time for it, which the lightning alert reads (alerts.py).
_strikes: dict[str, dict] = {}   # sensor_id → {"day", "today", "last", "at", "rose"}, guarded by current_lock

# A guess from one strike: an Access hub went from 0 to 256 for a single strike
# (one new strike time, 2026-10-23), as if the count sat one byte up. So a rise
# that is a whole number of 256s counts as that many strikes. A second strike
# reading 512 would bear it out; 257 would say it was wrong.
COUNT_STEP = 256


def _count_strikes(sensor_id: str, count: int, day: str, struck_at: str | None = None) -> int:
    s = _strikes.setdefault(sensor_id, {"day": day, "today": 0, "last": None, "at": None})
    if s["day"] != day:
        s["day"], s["today"] = day, 0
    grew = 0
    if s["last"] is not None:
        grew = count - s["last"] if count >= s["last"] else count
        if grew and grew % COUNT_STEP == 0:
            grew //= COUNT_STEP
        if struck_at and struck_at != s["at"]:
            grew = max(grew, 1)
    s["today"] += grew
    s["last"], s["rose"] = count, grew > 0
    if struck_at:
        s["at"] = struck_at
    return s["today"]


def _seed_strikes(db_path: Path | None, sensor_id: str, now: float) -> None:
    """Today's counts for one sensor from the store, and the last one before midnight."""
    _strikes.pop(sensor_id, None)
    if db_path is None or not db_path.exists():
        return
    day = epoch_to_day(now)
    midnight = day_bounds(day)[0]
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        before = con.execute("SELECT strike_count, last_strike_ts FROM readings WHERE sensor_id = ? AND strike_count IS NOT NULL"
                             " AND received_utc < ? ORDER BY received_utc DESC LIMIT 1",
                             (sensor_id, midnight)).fetchall()
        today = con.execute("SELECT strike_count, last_strike_ts FROM readings WHERE sensor_id = ? AND strike_count IS NOT NULL"
                            " AND received_utc >= ? ORDER BY received_utc", (sensor_id, midnight)).fetchall()
        con.close()
    except sqlite3.Error as exc:
        log.warning("Strikes today start at 0, the store is unreadable: %s", exc)
        return
    if before:
        _strikes[sensor_id] = {"day": day, "today": 0, "last": before[0][0], "at": before[0][1]}
    for count, at in today:
        _count_strikes(sensor_id, count, day, at)


def record_reading(reading: Reading, write_path: Path, sensors: SensorNames, page: dict | None = None,
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
    with current_lock:
        if not _pressure_seeded:
            _seed_pressure(write_path / "history.csv", iso_to_epoch(now))
            _pressure_seeded = True
        if isinstance(fields.get("pressure_inhg"), (int, float)):
            _current_page["pressure_change_3h"] = _pressure_change(iso_to_epoch(now), float(fields["pressure_inhg"]))
        entry = _current.setdefault(sensor_id, {"sensor_id": sensor_id, "type": kind, "fields": {}})
        entry["name"] = sensors.get(sensor_id) or reading.name or entry.get("name") or f"{kind} {sensor_id}"
        entry["type"] = kind
        entry["updated"] = now
        entry["fields"].update(fields)
        # What sanity.py held back: shown beside the last believed value until a
        # reading of that field is believed again.
        held = entry.setdefault("suspect", {})
        for name in fields:
            held.pop(name, None)
        for name, h in reading.suspect.items():
            held[name] = {**h, "since": held.get(name, {}).get("since", now)}
        if not held:
            del entry["suspect"]
        if isinstance(fields.get("strike_count"), (int, float)):
            if sensor_id not in _strikes:
                _seed_strikes(db_path, sensor_id, iso_to_epoch(now))
            entry["fields"]["strikes_today"] = _count_strikes(sensor_id, fields["strike_count"], epoch_to_day(iso_to_epoch(now)),
                                                             fields.get("last_strike_ts"))
            if _strikes[sensor_id]["rose"]:
                entry["fields"]["last_strike_heard"] = now
        if reading.channel:
            entry["channel"] = reading.channel
        _note_heard(sensor_id, _receiver_of(reading.source, reading.receiver), iso_to_epoch(now), reading.snr_db)
        if isinstance(fields.get("pressure_inhg"), (int, float)):
            # Wind under 1 mph has no direction worth counting.
            calm = not (entry["fields"].get("wind_mph") or 0) >= 1
            _current_page["zambretti"] = zambretti.outlook(
                float(fields["pressure_inhg"]), _current_page.get("pressure_change_3h"),
                datetime.now().month, None if calm else entry["fields"].get("wind_dir_deg"),
                ((page or {}).get("station") or {}).get("latitude", 0) >= 0)
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
                day = epoch_to_day(iso_to_epoch(now))
                if day != _rolled_through:
                    n = roll_up(_db, iso_to_epoch(now))
                    if n:
                        log.info("Daily summaries: %d day(s) summed up", n)
                    _rolled_through = day
            except sqlite3.Error as exc:
                log.error("Store failed: %s (%s raw=%s)", exc, reading.source, reading.raw)
    return entry


_db: sqlite3.Connection | None = None   # guarded by current_lock

_rolled_through: str | None = None      # local day daily summaries were last brought up to


def reference_temp(sensor_id: str, now: float) -> float | None:
    """The other outdoor sensors' temperature now, for sanity.py to compare with."""
    with current_lock:
        entries = [{**e, "epoch": iso_to_epoch(e["updated"]), "outdoor": e.get("type") in OUTDOOR_TYPES}
                   for e in _current.values() if e.get("updated")]
    return sanity.reference(entries, sensor_id, now)
