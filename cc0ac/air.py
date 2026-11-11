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
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from cc0ac import iolog
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


def run(lat: float, lon: float, contact: str, write_path: Path, google_key: str = "", zip_code: str = "") -> None:
    """Forever: air.json every hour, the last good copy of each part kept through a failed fetch."""
    out: dict[str, Any] = {"source": "Open-Meteo (CAMS model)", "errors": {}}
    sources = pollen_sources(lat, lon, google_key, zip_code)
    while True:
        wait = EVERY_S
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
