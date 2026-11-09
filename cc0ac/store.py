"""The database: storing readings, the daily summaries, and the history and summary queries behind the charts."""

from __future__ import annotations

import configparser
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cc0ac import noaa, sanity
from cc0ac.config import Query, SensorNames, display_order
from cc0ac.sources import FIELDS, OUTDOOR_TYPES, Reading

log = logging.getLogger("acurite")


#
# Every source (see sources/) hands its readings to record_reading(), which
# folds each one into current.json / current.js, appends it to history.csv and
# stores it in the database. Nothing here knows which source a reading came from.

HISTORY_FIELDS = ["timestamp", "sensor_id", "sensor_name", "type"] + sorted(FIELDS)


def iso_to_epoch(iso: str) -> float:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


#
# One row per reading, kept for good: the reading's fields as columns for
# querying and charting, plus exactly what the source heard (for the hub, its
# whole query string) so nothing it sends is ever lost, even fields we don't map
# yet. history.csv stays as a plain-text copy. A reading the hub sends twice (it
# resends whatever AcuRite refused) is heard the same both times, so it is
# stored once.

DB_COLUMNS = sorted(FIELDS)


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
        query TEXT NOT NULL UNIQUE,  -- the hub's query string, exactly as sent
        suspect TEXT                 -- fields held back by sanity.py, as JSON: name, value and why
    )""")
    if "suspect" not in {r[1] for r in con.execute("PRAGMA table_info(readings)")}:
        con.execute("ALTER TABLE readings ADD COLUMN suspect TEXT")   # a database from before 1.3.10
    con.execute("CREATE INDEX IF NOT EXISTS readings_by_sensor ON readings (sensor_id, received_utc)")
    con.execute("CREATE INDEX IF NOT EXISTS readings_by_time ON readings (received_utc)")
    open_daily(con)
    return con


def store_reading(con: sqlite3.Connection, reading: Reading) -> bool:
    """Insert one reading; False if that exact reading was already stored."""
    row = {"received_utc": reading.received_utc, "hub_utc": reading.sensor_utc,
           "hub_id": reading.receiver, "sensor_id": reading.sensor_id, "type": reading.type,
           **reading.fields, "query": reading.raw,
           "suspect": json.dumps(reading.suspect) if reading.suspect else None}
    cur = con.execute(f"INSERT OR IGNORE INTO readings ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                      list(row.values()))
    con.commit()
    return cur.rowcount == 1


def believed_before(db_path: Path, sensor_id: str) -> dict:
    """{field: (value, epoch)}: a sensor's last stored value of each field sanity.py
    watches for jumps, from the last three hours, so a restart picks up where it was."""
    out: dict = {}
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return out
    try:
        since = epoch_to_iso(time.time() - 3 * 3600)
        for name in sanity.STEADY:
            row = con.execute(f"""SELECT received_utc, {name} FROM readings WHERE sensor_id = ?
                AND received_utc >= ? AND {name} IS NOT NULL ORDER BY received_utc DESC LIMIT 1""",
                              [sensor_id, since]).fetchone()
            if row:
                out[name] = (float(row[1]), iso_to_epoch(row[0]))
    except sqlite3.Error as exc:
        log.warning("Could not read the last readings of %s: %s", sensor_id, exc)
    finally:
        con.close()
    return out


#
# /history.json?period=today|week|month|year|all, or ?from=...&to=... where each is
# a date (YYYY-MM-DD: from its midnight, to the end of that day) or a date and a
# time (YYYY-MM-DDTHH:MM: that minute exactly), in the station's local time.
# Readings are grouped into time buckets sized so any period comes back as a few
# hundred points: every reading for today, daily averages for a year. Days and
# weeks follow the station's local time, as the hub's daily rain does.
#
# Rain: the hub sends rainin, a rolling total for the last 60 minutes, and
# dailyrainin, the total since local midnight. A day's rain is its highest
# dailyrainin; shorter spans use how much dailyrainin grew within them.

HISTORY_STEPS = [300, 900, 1800, 3600, 2 * 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 7 * 86400]

HISTORY_POINTS = 600   # about how many buckets a chart gets at most


def _when(text: str) -> tuple[float, bool]:
    """A local date or date and time → (epoch, whether it was a date alone)."""
    text = text.strip()
    for fmt, whole_day in (("%Y-%m-%d", True), ("%Y-%m-%dT%H:%M", False), ("%Y-%m-%dT%H:%M:%S", False),
                           ("%Y-%m-%d %H:%M", False)):
        try:
            return datetime.strptime(text, fmt).astimezone().timestamp(), whole_day
        except ValueError:
            pass
    raise ValueError(f"{text!r} is not YYYY-MM-DD or YYYY-MM-DDTHH:MM")


def _history_bounds(q: Query, first: float, now: datetime) -> tuple[float, float, str]:
    """(from, to) in epoch seconds for the request, and the period it names."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    period = q.get("period", "today")
    if "from" in q:
        lo, _ = _when(q["from"])
        hi, whole_day = _when(q.get("to", q["from"]))
        hi += 86400 if whole_day else 0
        if hi <= lo:
            raise ValueError("to must come after from")
        return lo, min(hi, now.timestamp()), "range"
    days = {"24h": 1, "week": 7, "month": 30, "year": 365}
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
    if lo is not None and hi is not None:
        where, args = "AND received_utc >= ? AND received_utc < ?", [epoch_to_iso(lo), epoch_to_iso(hi)]
    return con.execute(f"""SELECT date(received_utc, 'localtime') AS d, max(rain_day_in)
        FROM readings WHERE type IN {OUTDOOR} AND rain_day_in IS NOT NULL {where}
        GROUP BY d ORDER BY d""", args).fetchall()


def epoch_to_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rain_totals(con: sqlite3.Connection, now: datetime) -> dict:
    days = _local_days(con)
    today = now.date()
    week_start = today.toordinal() - (today.isoweekday() % 7)   # weeks start on Sunday
    def total(keep: Any) -> float:
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
            ORDER BY received_utc""", [epoch_to_iso(hi), epoch_to_iso(lo)]).fetchall()
        prev = None
        for stamp, total in rows:
            if prev is not None:
                # A fall to under half is the midnight reset: everything since is new
                # rain. A smaller dip is the hub wobbling (0.32, 0.31, 0.32): not rain
                # when it falls, and not rain again when it climbs back.
                reset = total <= prev / 2
                grew = total if reset else total - prev
                t = iso_to_epoch(stamp)
                if grew > 0 and t >= lo:
                    buckets[key(t)] = buckets.get(key(t), 0) + grew
                if total < prev and not reset:
                    continue
            prev = total
    ticks = list(range(key(lo), int(hi) + 1, step))
    return {"step": step, "t": ticks, "in": [round(buckets.get(b, 0), 2) for b in ticks]}


#
# Lightning, strike by strike. A lightning sensor sends a running count of the
# strikes it has heard and the distance to the last one. Each rise in the count
# is that many new strikes (a count smaller than the last means it started again,
# so all of it is new), and a new time of the last strike is at least one, as the
# recorder counts them for the dashboard. A rise is put at the distance the same
# reading gives: the sensor sends only the last strike's, so when several came
# between two readings they share it. A strike farther than [lightning]
# ignore_beyond_mi is left out, here and wherever strikes are counted.

def strike_rise(last: float | None, count: float, last_at: str | None, at: str | None) -> int:
    """How many new strikes a reading's running count says, after the one before it."""
    if last is None:
        return 0
    grew = int(count - last if count >= last else count)
    if at and at != last_at:
        grew = max(grew, 1)
    return grew


def ignored(mi: Any, beyond: float | None) -> bool:
    """A strike past [lightning] ignore_beyond_mi."""
    return beyond is not None and isinstance(mi, (int, float)) and mi > beyond


STRIKES_SHOWN = 5000   # the newest this many readings with strikes, at most


def strikes(con: sqlite3.Connection, lo: float, hi: float, beyond: float | None = None,
            sensor: str | None = None) -> dict:
    """Every reading in [lo, hi) that brought new strikes: {"t", "mi", "n", "sensors"}.
    sensors counts the lightning sensors heard then, so a chart can tell "no strikes"
    from "no lightning sensor". The hour before lo is read as well, so the first
    reading in the period has one to follow."""
    rows = con.execute("""SELECT sensor_id, received_utc, strike_count, last_strike_ts, last_strike_mi,
            lag(strike_count) OVER w, lag(last_strike_ts) OVER w
        FROM readings WHERE strike_count IS NOT NULL AND received_utc >= ? AND received_utc < ?
          AND (? IS NULL OR sensor_id = ?)
        WINDOW w AS (PARTITION BY sensor_id ORDER BY received_utc)
        ORDER BY received_utc""", [epoch_to_iso(lo - 3600), epoch_to_iso(hi), sensor, sensor]).fetchall()
    out: dict = {"t": [], "mi": [], "n": [], "sensors": 0}
    heard = set()
    for sid, at, count, struck, mi, last, last_struck in rows:
        t = iso_to_epoch(at)
        if t < lo:
            continue
        heard.add(sid)
        n = strike_rise(last, count, last_struck, struck)
        if n and not ignored(mi, beyond):
            out["t"].append(int(t))
            out["mi"].append(None if mi is None else round(mi, 1))
            out["n"].append(n)
    for k in ("t", "mi", "n"):
        out[k] = out[k][-STRIKES_SHOWN:]
    out["sensors"] = len(heard)
    return out


# The fields the charts draw that a held-back value can be shown on.
HELD_SHOWN = ("temp_f", "humidity_pct", "wind_mph", "wind_gust_mph", "pressure_inhg", "uv_index")


def for_pages(db_path: Path, sensors: SensorNames) -> sqlite3.Connection:
    """The database read-only, as the pages see it: a sensor the pages leave out
    (sensors.hidden, sensor_config.py's disposition) is not in it. TEMP views named
    readings and daily stand in front of the tables, and SQLite looks a bare name up
    in TEMP first, so every query reading for a page leaves those sensors out without
    each one having to say so."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    left_out = sorted(getattr(sensors, "hidden", ()))
    if left_out:
        ids = ", ".join("'" + sid.replace("'", "''") + "'" for sid in left_out)
        for table in ("readings", "daily"):
            if con.execute("SELECT 1 FROM main.sqlite_master WHERE type = 'table' AND name = ?", [table]).fetchone():
                con.execute(f"CREATE TEMP VIEW {table} AS SELECT * FROM main.{table} "
                            f"WHERE lower(sensor_id) NOT IN ({ids})")
    return con


def history(db_path: Path, sensors: SensorNames, q: Query, ignore_beyond_mi: float | None = None) -> dict:
    con = for_pages(db_path, sensors)
    try:
        now = datetime.now().astimezone()
        off = int((now.utcoffset() or timedelta(0)).total_seconds())
        first = con.execute("SELECT min(received_utc) FROM readings").fetchone()[0]
        first = iso_to_epoch(first) if first else now.timestamp()
        lo, hi, period = _history_bounds(q, first, now)
        lo = max(lo, first) if period != "today" else lo
        step = next((s for s in HISTORY_STEPS if (hi - lo) / s <= HISTORY_POINTS), HISTORY_STEPS[-1])
        bucket = "(CAST(strftime('%s', received_utc) AS INTEGER) + :off) / :step * :step - :off"
        span = {"off": off, "step": step, "lo": epoch_to_iso(lo), "hi": epoch_to_iso(hi)}
        t = list(range(int((lo + off) // step * step - off), int(hi) + 1, step))
        index = {b: i for i, b in enumerate(t)}

        def column(rows: Any, n: Any) -> list[list]:
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
                WHERE type NOT IN {OUTDOOR} AND received_utc >= ? AND received_utc < ?""", [epoch_to_iso(lo), epoch_to_iso(hi)]):
            temp, hum = column(con.execute(f"""SELECT {bucket} AS b, avg(temp_f), avg(humidity_pct)
                FROM readings WHERE sensor_id = :sid AND received_utc >= :lo AND received_utc < :hi
                GROUP BY b""", {**span, "sid": sid}).fetchall(), 2)
            rooms.append({"id": sid, "name": sensors.get(sid, f"{kind} {sid}"), "temp_f": temp, "humidity_pct": hum})
        rooms.sort(key=lambda r: display_order(sensors, r["id"], r["name"]))
        # What sanity.py held back, bucket by bucket, for the charts to show faded.
        held_out: dict = {}
        held_rooms: dict = {}
        for sid, kind, at, suspect in con.execute("""SELECT sensor_id, type, received_utc, suspect FROM readings
                WHERE suspect IS NOT NULL AND received_utc >= ? AND received_utc < ? ORDER BY received_utc""",
                                                  [epoch_to_iso(lo), epoch_to_iso(hi)]):
            try:
                suspect = json.loads(suspect)
            except ValueError:
                continue
            i = index.get(int((iso_to_epoch(at) + off) // step * step - off))
            into = held_out if kind in OUTDOOR_TYPES else held_rooms.setdefault(sid, {})
            for name in HELD_SHOWN:
                v = (suspect.get(name) or {}).get("value")
                if i is not None and isinstance(v, (int, float)):
                    into.setdefault(name, [None] * len(t))[i] = round(v, 2)
        for room in rooms:
            room["held"] = held_rooms.get(room["id"], {})
        return {"period": period, "from": lo, "to": hi, "step": step, "t": t,
                "outdoor": dict(zip(out_names, column(outdoor, len(out_names)))),
                "held": held_out,
                "rooms": rooms,
                "rain": _rain_buckets(con, lo, hi, step, off),
                "strikes": {**strikes(con, lo, hi, ignore_beyond_mi), "ignore_beyond_mi": ignore_beyond_mi},
                "rain_totals": _rain_totals(con, now)}
    finally:
        con.close()


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
_COMBINE: dict[str, Callable[[list], float]] = {"_min": min, "_max": max, "_avg": lambda v: sum(v) / len(v), "rain_in": sum, "readings": sum}


def _combine(name: str, values: list) -> float | None:
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


def epoch_to_day(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")


def day_bounds(day: str) -> list[str]:
    """A local date's start and end as UTC times, as received_utc is stored."""
    start = datetime.strptime(day, "%Y-%m-%d")
    end = datetime.fromordinal(start.toordinal() + 1)
    return [epoch_to_iso(start.astimezone().timestamp()), epoch_to_iso(end.astimezone().timestamp())]


def _day_rows(con: sqlite3.Connection, day: str) -> list[dict]:
    rows = con.execute(f"""SELECT sensor_id, max(type), {", ".join(DAILY.values())} FROM readings
        WHERE received_utc >= ? AND received_utc < ? GROUP BY sensor_id""", day_bounds(day)).fetchall()
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
             else datetime.strptime(epoch_to_day(iso_to_epoch(first)), "%Y-%m-%d").toordinal())
    today = datetime.strptime(epoch_to_day(now), "%Y-%m-%d").toordinal()
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


def summaries(db_path: Path, sensors: SensorNames, q: Query) -> dict:
    """Days, weeks, months and years for every sensor. q: days=N, how many of the
    latest days to list (default 400); weeks, months and years are listed in full."""
    try:
        keep = max(int(q.get("days", 400)), 1)
    except ValueError:
        raise ValueError("days must be a number")
    con = for_pages(db_path, sensors)
    try:
        today = epoch_to_day(time.time())
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


def noaa_summary(cfg: configparser.ConfigParser, db_path: Path, sensors: SensorNames, q: Query) -> dict:
    """/noaa.json and --noaa: a month's or year's NOAA-style summary (noaa.py) for
    q["sensor"], or else the outdoor sensor with the most readings in it."""
    year, month = noaa.period(q.get("period") or datetime.now().strftime("%Y-%m"))
    first = f"{year:04d}-{month or 1:02d}-01"
    last = f"{year + (month or 12) // 12:04d}-{(month or 12) % 12 + 1:02d}-01"
    con = for_pages(db_path, sensors)
    try:
        # A day either side: the rows are UTC, the period is local.
        found = con.execute(f"""SELECT sensor_id, max(type), count(*) FROM readings WHERE type IN {OUTDOOR}
            AND sensor_id NOT LIKE 'open-meteo%' AND sensor_id NOT LIKE 'nws-%'
            AND received_utc >= date(?, '-1 day') AND received_utc < date(?, '+1 day')
            GROUP BY sensor_id ORDER BY count(*) DESC""", [first, last]).fetchall()
        choices = [{"id": sid, "name": sensors.get(sid, f"{kind} {sid}")} for sid, kind, _ in found]
        wanted = (q.get("sensor") or "").strip()
        sid = next((c["id"] for c in choices if c["id"].lower() == wanted.lower()), None) if wanted else None
        if wanted and sid is None:
            raise ValueError(f"no outdoor readings from {wanted} then")
        sid = sid or (choices[0]["id"] if choices else None)
        name = next((c["name"] for c in choices if c["id"] == sid), "no outdoor sensor")
        text = noaa.report(cfg, con, sid or "", name, year, month)
    finally:
        con.close()
    return {"period": f"{year:04d}" + (f"-{month:02d}" if month else ""), "sensor": sid,
            "sensors": choices, "text": text}
