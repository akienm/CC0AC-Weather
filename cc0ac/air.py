"""Air quality (and pollen where it is covered), written to air.json / air.js.

Open-Meteo's air-quality service (air-quality-api.open-meteo.com; free, no key,
worldwide) for [station] latitude/longitude: the US Air Quality Index and the
pollutants behind it, UV, and the highest AQI forecast for today and tomorrow.
These are a model's estimate for the spot (CAMS), not a monitor's measurement.

Pollen comes from the same service only in Europe; elsewhere its pollen values
are empty and the card leaves pollen out.

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
from datetime import datetime, timezone
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


def fetch(lat: float, lon: float, contact: str) -> dict:
    req = urllib.request.Request(url(lat, lon), headers={"User-Agent": f"CC0AC-Weather ({contact})"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return parse(json.loads(resp.read()))


def write(write_path: Path, doc: dict) -> None:
    body = json.dumps(doc, indent=1)
    write_atomic(write_path / "air.json", body)
    write_atomic(write_path / "air.js", f"window.ACURITE_AIR = {body};\n")


def run(lat: float, lon: float, contact: str, write_path: Path) -> None:
    """Forever: air.json every hour, the last good copy kept through a failed fetch."""
    out: dict[str, Any] = {"source": "Open-Meteo (CAMS model)", "errors": {}}
    while True:
        try:
            out.update(fetch(lat, lon, contact))
            out["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            out["errors"].pop("air", None)
            wait = EVERY_S
            iolog.report("open-meteo-air", True, "air quality fetched")
        except Exception as exc:
            # Loud in the log, named on the page; the last good copy stays.
            log.warning("Air quality fetch failed: %s", exc)
            iolog.report("open-meteo-air", False, f"air: {exc}")
            out["errors"]["air"] = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}: {exc}"
            wait = RETRY_S
        out["written"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        write(write_path, out)
        time.sleep(wait)
