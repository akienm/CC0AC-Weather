"""The National Weather Service forecast, nearest airport observation and alerts (or Open-Meteo), written to forecast.json / forecast.js."""

from __future__ import annotations

import configparser
import json
import logging
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cc0ac import iolog
from cc0ac.config import station_settings
from cc0ac.fsutil import write_atomic
from cc0ac.sources import online

log = logging.getLogger("acurite")


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


def _value(q: dict | None, scale: float = 1.0, places: int = 0) -> float | None:
    """A Weather Service quantity ({"value": ..., "unitCode": ...}) → a plain number."""
    v = (q or {}).get("value")
    return None if v is None else round(v * scale, places) if places else round(v * scale)


def _miles_between(lat1: Any, lon1: Any, lat2: Any, lon2: Any) -> float:
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
    station = station_settings(cfg)
    if not station:
        log.warning("Forecast is on but [station] latitude/longitude are not set; forecast off")
        return
    lat, lon = station["latitude"], station["longitude"]
    contact = cfg.get("forecast", "contact", fallback=DEFAULT_CONTACT).strip() or DEFAULT_CONTACT
    if online.choose(cfg, "forecast", lat, lon) == "open-meteo":
        run_open_meteo_forecast(lat, lon, contact, write_path)
        return
    out: dict[str, Any] = {"source": "National Weather Service", "errors": {}}
    due = {"point": 0.0, "forecast": 0.0, "observation": 0.0, "alerts": 0.0}
    point: dict | None = None
    stations: list | None = None

    def fetch(part: str, every: float, work: Callable[[], None]) -> bool:
        if time.time() < due[part]:
            return False
        try:
            work()
            out["errors"].pop(part, None)
            due[part] = time.time() + every
            iolog.report("weather-service", True, f"{part} fetched")
        except Exception as exc:
            # Loud in the log, and named on the page; the last good copy stays.
            log.warning("Weather Service %s fetch failed: %s", part, exc)
            iolog.report("weather-service", False, f"{part}: {exc}")
            out["errors"][part] = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}: {exc}"
            due[part] = time.time() + 300
        return True

    while True:
        def get_point() -> None:
            nonlocal point, stations
            point = _nws_get(f"{NWS_API}/points/{lat:.4f},{lon:.4f}", contact)["properties"]
            stations = _nws_get(point["observationStations"], contact)["features"]
        changed = fetch("point", POINT_EVERY_S, get_point)
        if point:
            def get_forecast() -> None:
                assert point is not None   # fetched first
                out["hourly"] = _hourly(_nws_get(point["forecastHourly"], contact))
                out["daily"] = _daily(_nws_get(point["forecast"], contact))
            def get_observation() -> None:
                assert stations is not None   # fetched first
                nearest = stations[0]
                doc = _nws_get(f"{NWS_API}/stations/{nearest['properties']['stationIdentifier']}/observations/latest", contact)
                out["observation"] = _observation(nearest, doc, lat, lon)
            def get_alerts() -> None:
                out["alerts"] = _alerts(_nws_get(f"{NWS_API}/alerts/active?point={lat:.4f},{lon:.4f}", contact))
            changed |= fetch("forecast", FORECAST_EVERY_S, get_forecast)
            changed |= fetch("observation", OBSERVATION_EVERY_S, get_observation)
            changed |= fetch("alerts", ALERTS_EVERY_S, get_alerts)
        if changed:
            out["written"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            body = json.dumps(out, indent=2)
            write_atomic(write_path / "forecast.json", body)
            write_atomic(write_path / "forecast.js", f"window.ACURITE_FORECAST = {body};\n")
        time.sleep(60)


def run_open_meteo_forecast(lat: float, lon: float, contact: str, write_path: Path) -> None:
    """Outside the US ([forecast] source = auto there, or open-meteo): the hourly and
    7-day cards from Open-Meteo. It has no alerts and no airport sky."""
    out: dict[str, Any] = {"source": "Open-Meteo", "errors": {}}
    while True:
        try:
            out.update(online.open_meteo_forecast(lat, lon, contact))
            out["errors"].pop("forecast", None)
            wait = FORECAST_EVERY_S
            iolog.report("open-meteo", True, "forecast fetched")
        except Exception as exc:
            log.warning("Open-Meteo forecast fetch failed: %s", exc)
            iolog.report("open-meteo", False, f"forecast: {exc}")
            out["errors"]["forecast"] = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}: {exc}"
            wait = 300
        out["written"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        body = json.dumps(out, indent=2)
        write_atomic(write_path / "forecast.json", body)
        write_atomic(write_path / "forecast.js", f"window.ACURITE_FORECAST = {body};\n")
        time.sleep(wait)
