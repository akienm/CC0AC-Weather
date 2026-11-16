"""Air quality (and pollen where it is covered), written to air.json / air.js.

Open-Meteo's air-quality service (air-quality-api.open-meteo.com; free, no key,
worldwide) for [station] latitude/longitude: the US Air Quality Index and the
pollutants behind it, UV, and the highest AQI forecast for today and tomorrow.
These are a model's estimate for the spot (CAMS), not a monitor's measurement.

Pollen comes from the same service only in Europe; elsewhere its pollen values
are empty. Two more sources fill in, each optional ([air] in config.ini):

  * google_api_key: Google's Pollen API (pollen.googleapis.com; worldwide, needs
    a Google Maps Platform key with the Pollen API switched on). Used first.
  * pollen_zip: pollen.com's forecast for a US ZIP code. Its API is the one its
    own page uses, not a published one, so it may change without notice.

Either gives a pollen forecast for today and tomorrow: one index per day, its
level and colour, and the plants behind it. Without either, the card leaves
pollen out.

One thread fetches every hour (the model's steps are hourly) and keeps the last
good copy when a fetch fails, so the page shows an old reading with its age and
the error rather than nothing.

History, for the charts (given the database): every hour's AQI and pollutants go
to the air_hours table, and each day's pollen index and plants to pollen_days
(store.py). On a first start, or after time off, it backfills: Open-Meteo answers
up to 92 days back (BACKFILL_DAYS), and pollen.com's own page keeps 30 days of
its index (without the plants). A backfilled hour is the same model's answer for
that hour, so it charts alongside the rest as one series.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from cc0ac import iolog, store
from cc0ac.fsutil import write_atomic

log = logging.getLogger("acurite")

AIR_API = "https://air-quality-api.open-meteo.com/v1/air-quality"
EVERY_S = 3600
RETRY_S = 300

POLLUTANTS = ["us_aqi", "us_aqi_pm2_5", "us_aqi_pm10", "us_aqi_ozone", "us_aqi_nitrogen_dioxide",
              "pm2_5", "pm10", "ozone", "nitrogen_dioxide", "dust", "uv_index"]
POLLEN = {"alder_pollen": "Alder", "birch_pollen": "Birch", "grass_pollen": "Grass",
          "mugwort_pollen": "Mugwort", "olive_pollen": "Olive", "ragweed_pollen": "Ragweed"}

# The EPA's bands: (highest AQI in the band, name, colour).
AQI_BANDS = [(50, "Good", "#00e400"), (100, "Moderate", "#ffff00"),
             (150, "Unhealthy for sensitive groups", "#ff7e00"), (200, "Unhealthy", "#ff0000"),
             (300, "Very unhealthy", "#8f3f97"), (10**9, "Hazardous", "#7e0023")]

# Which pollutant's sub-index is the overall AQI: the largest one.
SUB_INDEX = {"us_aqi_pm2_5": "PM2.5", "us_aqi_pm10": "PM10", "us_aqi_ozone": "Ozone",
             "us_aqi_nitrogen_dioxide": "NO₂"}


def band(aqi: float | None) -> dict | None:
    if aqi is None:
        return None
    for top, name, colour in AQI_BANDS:
        if aqi <= top:
            return {"name": name, "colour": colour}
    return None   # unreachable: the last band has no top


# The hourly series kept in air_hours: Open-Meteo's name → the column's.
HISTORY = {"us_aqi": "aqi", "pm2_5": "pm2_5", "pm10": "pm10", "ozone": "ozone",
           "nitrogen_dioxide": "nitrogen_dioxide", "dust": "dust", "uv_index": "uv_index",
           "aerosol_optical_depth": "aod"}
BACKFILL_DAYS = 92          # as far back as Open-Meteo's past_days goes
POLLEN_BACKFILL_S = 86400   # pollen.com's past 30 days, fetched again once a day


def history_url(lat: float, lon: float, past_days: int) -> str:
    q = {"latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "timezone": "GMT", "forecast_days": "1",
         "past_days": str(max(1, min(BACKFILL_DAYS, past_days))), "hourly": ",".join(HISTORY)}
    return AIR_API + "?" + urllib.parse.urlencode(q, safe=",")


def parse_history(doc: dict, now: float) -> list[dict]:
    """Open-Meteo's hourly answer (in GMT) → air_hours rows, the hours not yet come left out."""
    hourly = doc.get("hourly") or {}
    out = []
    for i, t in enumerate(hourly.get("time") or []):
        try:
            at = int(datetime.fromisoformat(t).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
        if at > now:
            break
        row = {"hour_utc": at}
        for k, col in HISTORY.items():
            vals = hourly.get(k) or []
            row[col] = vals[i] if i < len(vals) else None
        if any(row[c] is not None for c in HISTORY.values()):
            out.append(row)
    return out


def days_to_fetch(last_hour: int | None, now: float) -> int:
    """How many past days the next history fetch asks for: all of them on a first
    start, otherwise back to the last stored hour (and a day more)."""
    if last_hour is None:
        return BACKFILL_DAYS
    return max(1, min(BACKFILL_DAYS, int((now - last_hour) // 86400) + 1))


def fetch_history(lat: float, lon: float, contact: str, past_days: int, now: float) -> list[dict]:
    req = urllib.request.Request(history_url(lat, lon, past_days), headers={"User-Agent": f"CC0AC-Weather ({contact})"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return parse_history(json.loads(resp.read()), now)


def url(lat: float, lon: float) -> str:
    q = {"latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "timezone": "auto", "forecast_days": "2",
         "current": ",".join(POLLUTANTS + list(POLLEN)), "hourly": "us_aqi"}
    return AIR_API + "?" + urllib.parse.urlencode(q, safe=",")


def parse(doc: dict) -> dict:
    """Open-Meteo's answer → what air.json carries (without the bookkeeping)."""
    cur = doc.get("current") or {}
    units = doc.get("current_units") or {}
    aqi = cur.get("us_aqi")
    subs = {name: cur[k] for k, name in SUB_INDEX.items() if cur.get(k) is not None}
    main = max(subs, key=lambda n: subs[n]) if subs else None
    out: dict[str, Any] = {
        "time": cur.get("time"), "timezone": doc.get("timezone"),
        "aqi": aqi, "band": band(aqi), "main_pollutant": main,
        "pollutants": {k: {"value": cur.get(k), "unit": units.get(k, "")}
                       for k in ("pm2_5", "pm10", "ozone", "nitrogen_dioxide", "dust") if cur.get(k) is not None},
        "uv_index": cur.get("uv_index"),
        "pollen": [{"name": name, "value": cur[k], "unit": units.get(k, "grains/m³")}
                   for k, name in POLLEN.items() if cur.get(k) is not None],
    }
    hourly = doc.get("hourly") or {}
    days: dict[str, float] = {}
    for t, v in zip(hourly.get("time") or [], hourly.get("us_aqi") or []):
        if v is not None:
            days[t[:10]] = max(v, days.get(t[:10], v))
    out["days"] = [{"day": d, "max_aqi": v, "band": band(v)} for d, v in sorted(days.items())]
    return out


# ── Pollen forecasts ─────────────────────────────────────────────────────────

GOOGLE_POLLEN_API = "https://pollen.googleapis.com/v1/forecast:lookup"
POLLEN_COM_API = "https://www.pollen.com/api/forecast/current/pollen/"

# Google's Universal Pollen Index, 0-5 (its own colours come with each answer).
UPI = {0: ("None", "#9e9e9e"), 1: ("Very low", "#009e3a"), 2: ("Low", "#84cf33"), 3: ("Moderate", "#ffff00"),
       4: ("High", "#ffaa00"), 5: ("Very high", "#ff0000")}
# pollen.com's index, 0-12: (highest index in the level, level, colour), as its own page shows them.
POLLEN_COM_LEVELS = [(2.4, "Low", "#78b33c"), (4.8, "Low-medium", "#c8d22d"), (7.2, "Medium", "#f5d10e"),
                     (9.6, "Medium-high", "#f39221"), (12.0, "High", "#d8342e")]


def pollen_com_level(index: float | None) -> dict | None:
    if index is None:
        return None
    for top, name, colour in POLLEN_COM_LEVELS:
        if index <= top:
            return {"name": name, "colour": colour}
    return {"name": POLLEN_COM_LEVELS[-1][1], "colour": POLLEN_COM_LEVELS[-1][2]}


def _hex(c: dict | None) -> str | None:
    if not isinstance(c, dict):
        return None
    return "#" + "".join(f"{round(255 * float(c.get(k, 0))):02x}" for k in ("red", "green", "blue"))


def parse_google_pollen(doc: dict) -> dict:
    """Google's answer → {"source", "scale", "days": [{"day", "index", "level", "types"}]}."""
    days = []
    for d in doc.get("dailyInfo") or []:
        date = d.get("date") or {}
        try:
            day = f"{int(date['year']):04d}-{int(date['month']):02d}-{int(date['day']):02d}"
        except (KeyError, TypeError, ValueError):
            continue
        types = []
        for t in d.get("pollenTypeInfo") or []:
            info = t.get("indexInfo")
            if not info:            # out of season, or not measured here
                continue
            v = info.get("value", 0)
            name, colour = UPI.get(v, (info.get("category", ""), None))
            types.append({"name": t.get("displayName") or t.get("code", ""), "index": v,
                          "level": {"name": info.get("category") or name, "colour": _hex(info.get("color")) or colour}})
        top = max(types, key=lambda t: t["index"]) if types else None
        days.append({"day": day, "index": top["index"] if top else None, "level": top["level"] if top else None,
                     "types": [t for t in sorted(types, key=lambda t: -t["index"]) if t["index"] > 0]})
    return {"source": "Google Pollen", "scale": 5, "days": days}


def parse_pollen_com(doc: dict) -> dict:
    """pollen.com's answer → the same shape; its plants come without levels of their own."""
    loc = doc.get("Location") or {}
    try:
        first = datetime.fromisoformat(str(doc.get("ForecastDate"))).date()
    except ValueError:
        first = None
    offset = {"Today": 0, "Tomorrow": 1}
    days = []
    for p in loc.get("periods") or []:
        if p.get("Type") not in offset or first is None:
            continue                # yesterday is not a forecast
        day = date.fromordinal(first.toordinal() + offset[p["Type"]]).isoformat()
        index = p.get("Index")
        days.append({"day": day, "index": index, "level": pollen_com_level(index),
                     "types": [{"name": t.get("Name", "")} for t in p.get("Triggers") or [] if t.get("Name")]})
    return {"source": "pollen.com", "scale": 12, "days": days}


def parse_pollen_com_history(doc: dict) -> list[dict]:
    """pollen.com's past 30 days → [{"day", "index", "types": []}]: an index a day, no plants."""
    out = []
    for p in (doc.get("Location") or {}).get("periods") or []:
        day = str(p.get("Period") or "")[:10]
        if len(day) == 10 and p.get("Index") is not None:
            out.append({"day": day, "index": p["Index"], "types": []})
    return out


def fetch_pollen_com_history(zip_code: str) -> list[dict]:
    page = f"https://www.pollen.com/forecast/historic/pollen/{zip_code}"
    req = urllib.request.Request("https://www.pollen.com/api/forecast/historic/pollen/" + urllib.parse.quote(zip_code),
                                 headers={"Referer": page, "User-Agent": "Mozilla/5.0 (CC0AC-Weather)",
                                          "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return parse_pollen_com_history(json.loads(resp.read()))


def fetch_google_pollen(lat: float, lon: float, key: str) -> dict:
    q = {"key": key, "location.latitude": f"{lat:.4f}", "location.longitude": f"{lon:.4f}", "days": "2",
         "plantsDescription": "false"}
    with urllib.request.urlopen(GOOGLE_POLLEN_API + "?" + urllib.parse.urlencode(q), timeout=30) as resp:
        return parse_google_pollen(json.loads(resp.read()))


def fetch_pollen_com(zip_code: str) -> dict:
    # Its API answers only a request that seems to come from its own page.
    page = f"https://www.pollen.com/forecast/current/pollen/{zip_code}"
    req = urllib.request.Request(POLLEN_COM_API + urllib.parse.quote(zip_code),
                                 headers={"Referer": page, "User-Agent": "Mozilla/5.0 (CC0AC-Weather)",
                                          "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return parse_pollen_com(json.loads(resp.read()))


def pollen_sources(lat: float, lon: float, google_key: str = "", zip_code: str = "") -> list:
    """[(log name, fetch)] in the order tried: Google, then pollen.com."""
    out = []
    if google_key:
        out.append(("google-pollen", lambda: fetch_google_pollen(lat, lon, google_key)))
    if zip_code:
        out.append(("pollen-com", lambda: fetch_pollen_com(zip_code)))
    return out


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def fetch(lat: float, lon: float, contact: str) -> dict:
    req = urllib.request.Request(url(lat, lon), headers={"User-Agent": f"CC0AC-Weather ({contact})"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return parse(json.loads(resp.read()))


def write(write_path: Path, doc: dict) -> None:
    body = json.dumps(doc, indent=1)
    write_atomic(write_path / "air.json", body)
    write_atomic(write_path / "air.js", f"window.ACURITE_AIR = {body};\n")


def keep_history(db_path: Path | None, lat: float, lon: float, contact: str, zip_code: str,
                 pollen_due: float, now: float) -> float:
    """One round of history: the air hours missing since the last stored one, and
    (when due) pollen.com's past 30 days. Returns when pollen's backfill is next due.
    A failure is logged and named to iolog; it never stops air.json."""
    if db_path is None:
        return pollen_due
    con = sqlite3.connect(db_path, timeout=30)
    try:
        store.open_air(con)
        try:
            past = days_to_fetch(store.last_air_hour(con), now)
            n = store.store_air_hours(con, fetch_history(lat, lon, contact, past, now))
            log.info("Air history: %d hours stored (%d days asked for)", n, past)
            iolog.report("open-meteo-air-history", True, f"{n} hours")
        except Exception as exc:
            log.warning("Air history fetch failed: %s", exc)
            iolog.report("open-meteo-air-history", False, f"air history: {exc}")
        if zip_code and now >= pollen_due:
            try:
                n = store.store_pollen_days(con, "pollen.com", 12, fetch_pollen_com_history(zip_code), replace=False)
                log.info("Pollen history: %d days from pollen.com", n)
                iolog.report("pollen-com-history", True, f"{n} days")
                pollen_due = now + POLLEN_BACKFILL_S
            except Exception as exc:
                log.warning("Pollen history from pollen.com failed: %s", exc)
                iolog.report("pollen-com-history", False, f"pollen history: {exc}")
                pollen_due = now + RETRY_S
    finally:
        con.close()
    return pollen_due


def keep_pollen(db_path: Path | None, forecast: dict) -> None:
    """Each day of a pollen forecast into pollen_days, the newest answer for a day kept."""
    if db_path is None:
        return
    con = sqlite3.connect(db_path, timeout=30)
    try:
        store.open_air(con)
        store.store_pollen_days(con, forecast["source"], forecast["scale"], forecast["days"])
    finally:
        con.close()


def run(lat: float, lon: float, contact: str, write_path: Path, google_key: str = "", zip_code: str = "",
        db_path: Path | None = None) -> None:
    """Forever: air.json every hour, the last good copy of each part kept through a failed fetch,
    and (given db_path) the history the charts draw from."""
    out: dict[str, Any] = {"source": "Open-Meteo (CAMS model)", "errors": {}}
    sources = pollen_sources(lat, lon, google_key, zip_code)
    pollen_due = 0.0
    while True:
        wait = EVERY_S
        pollen_due = keep_history(db_path, lat, lon, contact, zip_code, pollen_due, time.time())
        try:
            out.update(fetch(lat, lon, contact))
            out["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            out["errors"].pop("air", None)
            iolog.report("open-meteo-air", True, "air quality fetched")
        except Exception as exc:
            # Loud in the log, named on the page; the last good copy stays.
            log.warning("Air quality fetch failed: %s", exc)
            iolog.report("open-meteo-air", False, f"air: {exc}")
            out["errors"]["air"] = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}: {exc}"
            wait = RETRY_S
        if sources:
            failed = []
            for name, get in sources:     # the first that answers
                try:
                    forecast = get()
                    if not forecast["days"]:
                        raise ValueError("no days in its answer")
                    out["pollen_forecast"] = {**forecast, "updated": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                    try:
                        keep_pollen(db_path, forecast)
                    except Exception as exc:     # the card still shows it; only the history missed a day
                        log.warning("Pollen history not stored: %s", exc)
                    iolog.report(name, True, "pollen fetched")
                    break
                except Exception as exc:
                    said = _redact(str(exc), google_key)
                    log.warning("Pollen from %s failed: %s", name, said)
                    iolog.report(name, False, f"pollen: {said}")
                    failed.append(f"{name}: {said}")
            if len(failed) < len(sources):
                out["errors"].pop("pollen", None)    # one that failed before the next answered is in the log
            else:
                out["errors"]["pollen"] = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}: " + "; ".join(failed)
                wait = RETRY_S
        out["written"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        write(write_path, out)
        time.sleep(wait)
