"""
Summaries of a day, a week or a month ([reports]), by email and on the web.

    http://THIS_BOX:12345/report?period=day|week|month&date=2026-11-06

For every sensor, the lowest, average and highest of each thing it measures,
the rain and the lightning, and, for an outdoor sensor, how that compares with
what is usual here:

  * the normals (climate.py: NOAA's 1991-2020 normals, or Open-Meteo's
    averages for the spot), when [station] latitude and longitude are set;
  * the same days in the station's own earlier years, once it has some.

A week runs Sunday to Saturday, as the rain totals do. Without a date: the last
whole day, week or month. A period that hasn't ended yet says "so far".

The emails ride the alert check (cc0ac-weather-check.timer, every 5 minutes):
after [reports] hour on the day a period ends, the report for it goes out once,
through [alerts]' mail server. alert-state.json remembers which went out.
"""

from __future__ import annotations

import configparser
import json
import logging
import smtplib
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from cc0ac import alerts, climate, units
from cc0ac.config import Query, SensorNames, page_title, strike_limit
from cc0ac.store import _combine, day_bounds, iso_to_epoch, strikes, summaries

log = logging.getLogger("acurite")

PERIODS = {"day": "daily", "week": "weekly", "month": "monthly"}
DEFAULT_HOUR = 7

# What a report lists: a label, how its values are shown, and the daily columns
# (store.DAILY) its low, average and high come from.
ROWS = [
    ("Temperature", "temperature", ("temp_min", "temp_avg", "temp_max")),
    ("Feels like", "temperature", ("feels_like_min", None, "feels_like_max")),
    ("Dew point", "temperature", (None, "dew_point_avg", None)),
    ("Humidity", "%", ("humidity_min", "humidity_avg", "humidity_max")),
    ("Pressure", "pressure", ("pressure_min", "pressure_avg", "pressure_max")),
    ("Wind", "wind", (None, "wind_avg", "wind_gust_max")),
    ("UV index", "uv", (None, None, "uv_max")),
    ("Light", "lux", (None, None, "light_max")),
]


def period_days(period: str, when: str | None, today: date) -> tuple[date, date]:
    """The first and last day of the period that holds `when` (YYYY-MM-DD, or
    YYYY-MM for a month); without one, the last whole period before today."""
    if period not in PERIODS:
        raise ValueError(f"period must be one of {', '.join(PERIODS)}")
    try:
        if when and period == "month" and len(when.strip()) == 7:
            at = datetime.strptime(when.strip(), "%Y-%m").date()
        elif when:
            at = datetime.strptime(when.strip(), "%Y-%m-%d").date()
        else:
            at = None
    except ValueError:
        raise ValueError(f"date {when!r} is not YYYY-MM-DD" + (" or YYYY-MM" if period == "month" else ""))
    if period == "day":
        d = at or today - timedelta(days=1)
        return d, d
    if period == "week":
        this_week = today - timedelta(days=today.isoweekday() % 7)
        d = at or this_week - timedelta(days=7)
        start = d - timedelta(days=d.isoweekday() % 7)
        return start, start + timedelta(days=6)
    first = (at or (today.replace(day=1) - timedelta(days=1))).replace(day=1)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, nxt - timedelta(days=1)


def _days(first: date, last: date) -> list[str]:
    return [(first + timedelta(days=n)).isoformat() for n in range((last - first).days + 1)]


def _show(kind: str, value: float | None, shown_in: dict, change: bool = False) -> str:
    if value is None:
        return ""
    if kind in units.KINDS:
        return units.show(kind, value, shown_in, change=change)
    places = 0 if kind in ("%", "lux") else 1
    text = f"{value:+.{places}f}" if change else f"{value:.{places}f}"
    return text + {"%": "%", "lux": " lux"}.get(kind, "")


def _normal(kept: dict | None, days: list[str]) -> dict | None:
    """The normal high and low averaged over the days, and the normal rain for them
    (the normals give rain as the month so far, so a day's is its step up)."""
    found = [(d, climate.normal_for(kept, d)) for d in days]
    found = [(d, n) for d, n in found if n]
    if not found:
        return None
    def avg(key: str) -> float | None:
        v = [n[key] for _, n in found if isinstance(n.get(key), (int, float))]
        return sum(v) / len(v) if v else None
    rain = 0.0
    for d, n in found:
        today = n.get("rain_mtd")
        if not isinstance(today, (int, float)):
            continue
        before = None if d.endswith("-01") else climate.normal_for(kept, (date.fromisoformat(d) - timedelta(days=1)).isoformat())
        rain += max(0.0, today - ((before or {}).get("rain_mtd") or 0))
    first = found[0][1]
    where = f", {first['station']} ({first['miles']:.0f} mi)" if first.get("station") else ""
    return {"high": avg("high"), "low": avg("low"), "rain": rain, "source": first["source"] + where}


def _earlier_years(mine: list[dict], days: list[str]) -> dict | None:
    """The same days of the year in the station's earlier years, averaged."""
    by_day = {r["day"]: r for r in mine}
    years: dict[int, list[dict]] = {}
    for d in days:
        at = date.fromisoformat(d)
        for back in range(1, 100):
            try:
                then = at.replace(year=at.year - back).isoformat()
            except ValueError:   # February 29
                continue
            if then < (mine[0]["day"] if mine else then):
                break
            if then in by_day:
                years.setdefault(at.year - back, []).append(by_day[then])
    if not years:
        return None
    rows = [r for rs in years.values() for r in rs]
    def avg(col: str) -> float | None:
        v = [r[col] for r in rows if isinstance(r.get(col), (int, float))]
        return sum(v) / len(v) if v else None
    rain = [sum(r["rain_in"] or 0 for r in rs) for rs in years.values()]
    return {"years": sorted(years), "days": len(rows), "temp_avg": avg("temp_avg"),
            "high": avg("temp_max"), "low": avg("temp_min"), "rain": sum(rain) / len(rain)}


def _sensor_block(entry: dict, days: list[str], first: date, last: date, kept_normals: dict | None,
                  con: sqlite3.Connection, beyond: float | None, shown_in: dict) -> list[str] | None:
    mine = [r for r in entry["days"] if r["day"] in set(days)]
    if not mine:
        return None
    combined = {c: _combine(c, [r.get(c) for r in mine]) for c in mine[0] if c != "day"}
    lines = [f"{entry['name']} ({entry['type']}): {len(mine)} of {len(days)} days, {combined.get('readings') or 0} readings",
             f"  {'':<12} {'low':>10} {'average':>10} {'high':>10}"]
    for label, kind, cols in ROWS:
        values = [combined.get(c) if c else None for c in cols]
        if any(v is not None for v in values):
            lines.append(f"  {label:<12} " + " ".join(f"{_show(kind, v, shown_in):>10}" for v in values))
    if entry["outdoor"] and combined.get("rain_in") is not None:
        lines.append(f"  {'Rain':<12} {_show('rain', combined['rain_in'], shown_in)}")
    lo = iso_to_epoch(day_bounds(first.isoformat())[0])
    hi = iso_to_epoch(day_bounds(last.isoformat())[1])
    seen = strikes(con, lo, hi, beyond, entry["id"])
    if seen["sensors"]:
        total = sum(seen["n"])
        near = [(mi, t) for mi, t in zip(seen["mi"], seen["t"]) if mi is not None]
        text = f"{total} strike{'s' if total != 1 else ''}"
        if near:
            mi, t = min(near)
            text += f", the closest {_show('distance', mi, shown_in)} at {datetime.fromtimestamp(t).strftime('%a %H:%M')}"
        if beyond is not None:
            text += f" (farther than {_show('distance', beyond, shown_in)} not counted)"
        lines.append(f"  {'Lightning':<12} {text}")
    if not entry["outdoor"]:
        return lines
    highs = [r["temp_max"] for r in mine if r.get("temp_max") is not None]
    lows = [r["temp_min"] for r in mine if r.get("temp_min") is not None]
    mine_high = sum(highs) / len(highs) if highs else None
    mine_low = sum(lows) / len(lows) if lows else None
    lines.append("  Compared with what is usual here:")
    normal = _normal(kept_normals, [r["day"] for r in mine])
    if normal:
        def vs(own: float | None, usual: float | None, kind: str) -> str:
            if own is None or usual is None:
                return _show(kind, usual, shown_in) or "?"
            return f"{_show(kind, usual, shown_in)} (this time {_show(kind, own - usual, shown_in, change=True)})"
        lines.append(f"    Normal ({normal['source']}), for the {len(mine)} days with readings:")
        lines.append(f"      average high {vs(mine_high, normal['high'], 'temperature')}, "
                     f"average low {vs(mine_low, normal['low'], 'temperature')}, "
                     f"rain {vs(combined.get('rain_in'), normal['rain'], 'rain')}")
    else:
        lines.append("    No normals yet: set [station] latitude and longitude to have them.")
    earlier = _earlier_years(entry["days"], days)
    if earlier:
        years = ", ".join(str(y) for y in earlier["years"])
        lines.append(f"    Earlier years here ({years}; {earlier['days']} days): "
                     f"average {_show('temperature', earlier['temp_avg'], shown_in)}, "
                     f"high {_show('temperature', earlier['high'], shown_in)}, "
                     f"low {_show('temperature', earlier['low'], shown_in)}, "
                     f"rain {_show('rain', earlier['rain'], shown_in)}")
    else:
        lines.append("    No earlier years recorded here yet to compare with.")
    return lines


def report(cfg: configparser.ConfigParser, db_path: Path, sensors: SensorNames, write_path: Path, q: Query,
           now: float | None = None) -> dict:
    """/report and the emails: {"period", "first", "last", "so_far", "title", "text"}."""
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now).date()
    period = (q.get("period") or "day").strip().lower()
    first, last = period_days(period, q.get("date"), today)
    days = _days(first, min(last, today))
    so_far = last >= today
    shown_in = units.chosen(cfg)
    beyond = strike_limit(cfg)
    try:
        kept_normals = json.loads((write_path / "normals.json").read_text())
    except (OSError, ValueError):
        kept_normals = None
    if period == "day":
        named = f"{first:%A, %B} {first.day}, {first.year}"
    elif period == "week":
        named = f"the week of {first:%B} {first.day} to {last:%B} {last.day}, {last.year}"
    else:
        named = first.strftime("%B %Y")
    title = f"{page_title(cfg)}: {named}" + (" so far" if so_far else "")
    blocks = []
    if first <= today:
        found = summaries(db_path, sensors, {"days": str((today - first).days + 1)})
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        try:
            for entry in found["sensors"]:
                block = _sensor_block(entry, days, first, last, kept_normals, con, beyond, shown_in)
                if block:
                    blocks.append("\n".join(block))
        finally:
            con.close()
    text = title + "\n" + "=" * len(title) + "\n\n"
    if so_far:
        at = datetime.fromtimestamp(now)
        text += f"So far, through {at:%a %b} {at.day} {at:%H:%M}.\n\n"
    text += "\n\n".join(blocks) if blocks else "No readings were stored in that time."
    return {"period": period, "first": first.isoformat(), "last": last.isoformat(), "so_far": so_far,
            "title": title, "text": text + "\n"}


def wanted(cfg: configparser.ConfigParser) -> list[str]:
    """The periods [reports] send names (daily, weekly, monthly → day, week, month)."""
    named = {w.strip().lower() for w in cfg.get("reports", "send", fallback="").split(",") if w.strip()}
    return [p for p, word in PERIODS.items() if word in named or p in named]


def check(cfg: configparser.ConfigParser, db_path: Path, sensors: SensorNames, write_path: Path,
          now: float | None = None, state_path: Path = alerts.STATE_FILE) -> str:
    """One look from the alert check: email each report that is due and not yet sent."""
    periods = wanted(cfg)
    if not periods:
        return "reports off"
    to = cfg.get("reports", "email_to", fallback="").strip() or cfg.get("alerts", "email_to", fallback="").strip()
    if not to:
        return "reports: no email_to in [reports] or [alerts]"
    now = time.time() if now is None else now
    try:
        hour = int(cfg.get("reports", "hour", fallback=str(DEFAULT_HOUR)).strip() or DEFAULT_HOUR)
    except ValueError:
        hour = DEFAULT_HOUR
    if datetime.fromtimestamp(now).hour < hour:
        return "reports: not yet the hour"
    if not db_path.exists():
        return "reports: no database yet"
    today = datetime.fromtimestamp(now).date()
    state = alerts._load_state(state_path)
    sent = dict(state.get("reports") or {})
    did = []
    for period in periods:
        first, _ = period_days(period, None, today)
        if sent.get(period) == first.isoformat():
            continue
        got = report(cfg, db_path, sensors, write_path, {"period": period}, now)
        try:
            alerts._email(cfg, to, got["title"], got["text"])
        except (OSError, smtplib.SMTPException, ValueError) as e:
            did.append(f"{PERIODS[period]} not sent: {e}")
            continue
        sent[period] = first.isoformat()
        did.append(f"{PERIODS[period]} sent")
    state = {**alerts._load_state(state_path), "reports": sent}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_name(state_path.name + ".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(state_path)
    return "reports: " + (", ".join(did) or "nothing due")
