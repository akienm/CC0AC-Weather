"""
Records and normals: is today unusual?

Records are each outdoor sensor's highs and lows for today, this month, this
year and all time, worked out from the daily summaries (the `daily` table plus
today so far): hottest, coldest, highest gust, wettest day, highest and lowest
pressure, each with the day it happened.

Normals are what a day is like on average, 1991-2020, for the station's
[station] latitude and longitude:

  * In the US, NOAA's official climate normals (NCEI, free, no key): the
    nearest normals station's daily normal high and low and month-to-date
    rain. The station's name and distance are shown, so the choice is visible.
  * Elsewhere, or if no NOAA station is within about 35 miles, the same three
    worked out from Open-Meteo's historical weather (a reanalysis model, free,
    no key) for the exact spot, and labelled as that.

Normals change once a decade, so they are fetched once and kept in
normals.json beside current.json, fetched again after 180 days or when the
location changes. The daemon writes climate.json / climate.js every hour.
"""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger("acurite")

NCEI_SEARCH = "https://www.ncei.noaa.gov/access/services/search/v1/data"
NCEI_DATA = "https://www.ncei.noaa.gov/access/services/data/v1"
OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
NEAREST_BOX_DEG = 0.5          # NOAA stations looked for within this many degrees
NEAREST_MAX_MI = 35            # and used only this close
NORMALS_KEEP_S = 180 * 86400
EVERY_S = 3600
USER_AGENT = "CC0AC-Weather (github.com/akienm/CC0AC-Weather)"

# Record → (column of the daily summaries, highest or lowest wins)
RECORDS = {"hottest": ("temp_max", max), "coldest": ("temp_min", min),
           "highest_gust": ("wind_gust_max", max), "wettest_day": ("rain_in", max),
           "highest_pressure": ("pressure_max", max), "lowest_pressure": ("pressure_min", min)}
PERIODS = {"today": 10, "month": 7, "year": 4, "all": 0}   # how many characters of the day must match


def records(days: list[dict], today: str) -> dict:
    """One sensor's days (oldest first, with `day`) → period → record → {value, day}."""
    out = {}
    for period, chars in PERIODS.items():
        mine = [d for d in days if d["day"][:chars] == today[:chars]]
        found = {}
        for name, (column, best) in RECORDS.items():
            have = [(d[column], d["day"]) for d in mine if d.get(column) is not None]
            if have:
                # The first day it happened wins a tie.
                value = best(v for v, _ in have)
                found[name] = {"value": value, "day": next(day for v, day in have if v == value)}
        out[period] = found
    return out


def month_rain(days: list[dict], today: str) -> float | None:
    rain = [d["rain_in"] for d in days if d["day"][:7] == today[:7] and d.get("rain_in") is not None]
    return round(sum(rain), 2) if rain else None


# ── Normals ───────────────────────────────────────────────────────────────────

def _get_json(url: str, params: dict, timeout: int = 60):
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 3958.8 * 2 * math.asin(math.sqrt(a))


def nearest_noaa(lat: float, lon: float, search: dict) -> tuple[str, str, float] | None:
    """(station ID, name, miles) of the nearest station with daily normal highs
    in an NCEI search answer; None if none is within NEAREST_MAX_MI."""
    best = None
    for result in search.get("results") or []:
        try:
            slon, slat = result["location"]["coordinates"]
        except (KeyError, TypeError, ValueError):
            continue
        for st in result.get("stations") or []:
            if not any(t.get("id") == "DLY-TMAX-NORMAL" for t in st.get("dataTypes") or []):
                continue
            d = miles(lat, lon, slat, slon)
            if d <= NEAREST_MAX_MI and (best is None or d < best[2]):
                best = (st["id"], st.get("name") or st["id"], round(d, 1))
    return best


def _number(v) -> float | None:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if -900 < v < 20000 else None    # NCEI marks a missing value with a large negative


def noaa_days(rows: list[dict]) -> dict:
    """NCEI daily normals rows → "MM-DD" → {high, low, rain_mtd}."""
    return {r["DATE"]: {"high": _number(r.get("DLY-TMAX-NORMAL")), "low": _number(r.get("DLY-TMIN-NORMAL")),
                        "rain_mtd": _number(r.get("MTD-PRCP-NORMAL"))}
            for r in rows if isinstance(r, dict) and r.get("DATE")}


def computed_days(daily: dict) -> dict:
    """Open-Meteo's 1991-2020 daily history → "MM-DD" → {high, low, rain_mtd},
    each the average over the years. rain_mtd is the month's rain up to that
    day, averaged, as NOAA gives it."""
    sums: dict[str, list] = {}
    month_so_far = {}
    for day, hi, lo, rain in zip(daily["time"], daily["temperature_2m_max"],
                                 daily["temperature_2m_min"], daily["precipitation_sum"]):
        ym, md = day[:7], day[5:]
        if rain is not None:
            month_so_far[ym] = month_so_far.get(ym, 0.0) + rain
        s = sums.setdefault(md, [[], [], []])
        for i, v in enumerate((hi, lo, month_so_far.get(ym) if rain is not None else None)):
            if v is not None:
                s[i].append(v)
    avg = lambda v: round(sum(v) / len(v), 2) if v else None
    return {md: {"high": avg(s[0]), "low": avg(s[1]), "rain_mtd": avg(s[2])} for md, s in sorted(sums.items())}


def fetch_normals(lat: float, lon: float) -> dict:
    """Normals for a location: NOAA's if a station is near, else computed from Open-Meteo."""
    try:
        r = NEAREST_BOX_DEG
        search = _get_json(NCEI_SEARCH, {"dataset": "normals-daily", "limit": 100,
                                         "bbox": f"{lat + r},{lon - r},{lat - r},{lon + r}"})
        near = nearest_noaa(lat, lon, search)
        if near:
            rows = _get_json(NCEI_DATA, {"dataset": "normals-daily", "stations": near[0],
                                         "dataTypes": "DLY-TMAX-NORMAL,DLY-TMIN-NORMAL,MTD-PRCP-NORMAL",
                                         "startDate": "2010-01-01", "endDate": "2010-12-31",
                                         "format": "json", "units": "standard"})
            days = noaa_days(rows)
            if days:
                return {"source": "NOAA 1991-2020 normals", "station": near[1], "miles": near[2],
                        "lat": lat, "lon": lon, "fetched": time.time(), "days": days}
    except (OSError, ValueError) as e:
        log.warning("NOAA normals: %s; trying Open-Meteo", e)
    doc = _get_json(OPEN_METEO_ARCHIVE, {"latitude": lat, "longitude": lon, "start_date": "1991-01-01",
                                         "end_date": "2020-12-31", "timezone": "auto",
                                         "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
                                         "temperature_unit": "fahrenheit", "precipitation_unit": "inch"}, 120)
    return {"source": "1991-2020 averages from Open-Meteo's historical weather", "station": None,
            "miles": 0, "lat": lat, "lon": lon, "fetched": time.time(), "days": computed_days(doc["daily"])}


def normals(lat: float, lon: float, path: Path, now: float) -> dict | None:
    """The kept normals, fetched again when old or for another place; None if unavailable."""
    try:
        kept = json.loads(path.read_text())
    except (OSError, ValueError):
        kept = None
    if kept and (kept.get("lat"), kept.get("lon")) == (lat, lon) and now - kept.get("fetched", 0) < NORMALS_KEEP_S:
        return kept
    try:
        fresh = fetch_normals(lat, lon)
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.warning("Normals unavailable: %s", e)
        return kept   # an old copy beats none
    path.write_text(json.dumps(fresh))
    log.info("Normals: %s%s", fresh["source"], f", {fresh['station']} ({fresh['miles']} mi)" if fresh["station"] else "")
    return fresh


def normal_for(kept: dict | None, day: str) -> dict | None:
    if not kept:
        return None
    md = day[5:]
    found = kept["days"].get(md) or (kept["days"].get("02-28") if md == "02-29" else None)
    return dict(found, source=kept["source"], station=kept["station"], miles=kept["miles"]) if found else None


# ── climate.json ──────────────────────────────────────────────────────────────

def build(sensors: list[dict], today: str, kept_normals: dict | None) -> dict:
    """sensors: [{id, name, days}] for the outdoor sensors (days oldest first, today last)."""
    out = {"today": today, "normal": normal_for(kept_normals, today), "sensors": []}
    for s in sensors:
        now = next((d for d in s["days"] if d["day"] == today), {})
        out["sensors"].append({"id": s["id"], "name": s["name"],
                               "high": now.get("temp_max"), "low": now.get("temp_min"),
                               "month_rain": month_rain(s["days"], today),
                               "since": s["days"][0]["day"] if s["days"] else None,
                               "records": records(s["days"], today)})
    return out


def write(write_path: Path, doc: dict) -> None:
    body = json.dumps(doc, indent=1)
    for name, text in (("climate.json", body), ("climate.js", f"window.ACURITE_CLIMATE = {body};\n")):
        tmp = write_path / (name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(write_path / name)


def run(lat: float | None, lon: float | None, write_path: Path, outdoor_days) -> None:
    """Forever: climate.json every hour. outdoor_days() → the sensors for build()."""
    while True:
        try:
            today = date.today().isoformat()
            kept = (normals(lat, lon, write_path / "normals.json", time.time())
                    if lat is not None and lon is not None else None)
            write(write_path, build(outdoor_days(), today, kept))
        except Exception:   # keep the thread; an hour later may go better
            log.exception("climate.json not written")
        time.sleep(EVERY_S - datetime.now().minute * 60 % EVERY_S + 60)   # just after the hour
