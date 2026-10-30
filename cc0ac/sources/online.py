"""
The internet's weather as a source: readings with no station at all.

For a house without weather gear, or a spare phone or tablet on the wall, this
source fetches outdoor conditions for [station] latitude/longitude and records
them like any sensor's:

  nws         the US National Weather Service's nearest observing station
              (api.weather.gov; free, no key, US only). A real measurement,
              usually at an airport a few miles away, reported about hourly.
  open-meteo  Open-Meteo's current conditions for the exact spot (open-meteo.com;
              free, no key, anywhere). A weather model's estimate, updated every
              15 minutes, not a measurement.
  auto        the Weather Service where it covers the location, else Open-Meteo.

One sensor appears on the dashboard: "nws-<station>" or "open-meteo", named
after where the numbers come from (a name under [sensors] replaces it). Each
fetch is kept whole as the reading's raw text.

open_meteo_forecast() is here too: the forecast thread uses it for the hourly and
7-day cards where the Weather Service has no forecast.
"""

from __future__ import annotations

import configparser
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from cc0ac.sources import Emit, Reading, derive

log = logging.getLogger("acurite")

NAME = "online"
NWS_API = "https://api.weather.gov"
OPEN_METEO_API = "https://api.open-meteo.com/v1/forecast"
DEFAULT_CONTACT = "github.com/akienm/CC0AC-Weather"
NWS_EVERY_S = 1200          # airports report hourly, some more often
OPEN_METEO_EVERY_S = 900    # Open-Meteo's current conditions are 15-minute steps
RETRY_S = 300
STALE_OBSERVATION_S = 3 * 3600   # an older one means the station has stopped; try the next
STATIONS_TRIED = 6     # small airports often report no temperature at all
HOURS_KEPT = 48
DAILY_PERIODS_KEPT = 14   # 7 days and nights, as the Weather Service gives
CARDINALS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
             "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def enabled(cfg: configparser.ConfigParser) -> bool:
    return cfg.getboolean("online", "enabled", fallback=False)


class NotCovered(Exception):
    """The Weather Service has no grid square here (outside the US)."""


def _get(url: str, contact: str, accept: str = "application/geo+json") -> str:
    req = urllib.request.Request(url, headers={"User-Agent": f"CC0AC-Weather ({contact})", "Accept": accept})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def _contact(cfg: configparser.ConfigParser) -> str:
    return cfg.get("forecast", "contact", fallback=DEFAULT_CONTACT).strip() or DEFAULT_CONTACT


def _where(cfg: configparser.ConfigParser) -> tuple[float, float] | None:
    try:
        return cfg.getfloat("station", "latitude"), cfg.getfloat("station", "longitude")
    except (configparser.Error, ValueError):
        return None


def nws_point(lat: float, lon: float, contact: str) -> dict:
    """The Weather Service's /points entry for a location; NotCovered outside the US."""
    try:
        return json.loads(_get(f"{NWS_API}/points/{lat:.4f},{lon:.4f}", contact))["properties"]
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise NotCovered(f"api.weather.gov has no forecast point at {lat:.4f},{lon:.4f}") from exc
        raise


def choose(cfg: configparser.ConfigParser, section: str, lat: float, lon: float) -> str:
    """[section] source → "nws" or "open-meteo". auto asks the Weather Service once;
    only its "not here" (a 404) means Open-Meteo, so a network hiccup is retried."""
    source = cfg.get(section, "source", fallback="auto").strip().lower() or "auto"
    if source in ("nws", "open-meteo"):
        return source
    if source != "auto":
        log.warning("[%s] source = %s is not nws, open-meteo or auto; using auto", section, source)
    while True:
        try:
            nws_point(lat, lon, _contact(cfg))
            return "nws"
        except NotCovered:
            return "open-meteo"
        except Exception as exc:
            log.warning("[%s] source = auto: the Weather Service didn't answer (%s); asking again in %d s",
                        section, exc, RETRY_S)
            time.sleep(RETRY_S)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _q(props: dict, key: str) -> Any:
    """A Weather Service quantity → (value, unit), or (None, None)."""
    q = props.get(key) or {}
    return q.get("value"), str(q.get("unitCode") or "").split(":")[-1]


def nws_fields(props: dict) -> dict:
    """An observation's properties → reading fields (°F, mph, inHg, inches)."""
    out = {}
    for key, name in (("temperature", "temp_f"), ("dewpoint", "dew_point_f"),
                      ("heatIndex", "heat_index_f"), ("windChill", "wind_chill_f")):
        v, unit = _q(props, key)
        if v is not None:
            out[name] = round(v * 9 / 5 + 32, 1) if unit == "degC" else round(v, 1)
    v, _ = _q(props, "relativeHumidity")
    if v is not None:
        out["humidity_pct"] = round(v)
    for key, name in (("windSpeed", "wind_mph"), ("windGust", "wind_gust_mph")):
        v, unit = _q(props, key)
        if v is not None:
            out[name] = round(v / 1.609344 if unit == "km_h-1" else v * 2.236936 if unit == "m_s-1" else v, 1)
    v, _ = _q(props, "windDirection")
    if v is not None:
        out["wind_dir_deg"] = round(v)
    v, _ = _q(props, "seaLevelPressure")
    if v is None:
        v, _ = _q(props, "barometricPressure")
    if v is not None:
        out["pressure_inhg"] = round(v / 3386.389, 2)
    v, _ = _q(props, "precipitationLastHour")
    if v is not None:
        out["rain_hour_in"] = round(v / 25.4, 2)
    return out


def _nws_latest(stations: list, contact: str) -> tuple[dict, str] | None:
    """The newest usable observation from the nearest stations: (station, body)."""
    for station in stations[:STATIONS_TRIED]:
        sid = station["properties"]["stationIdentifier"]
        try:
            body = _get(f"{NWS_API}/stations/{sid}/observations/latest", contact)
        except urllib.error.HTTPError as exc:
            log.info("online: station %s has no latest observation (%s)", sid, exc)
            continue
        props = json.loads(body)["properties"]
        when = props.get("timestamp")
        fresh = when and (datetime.now(timezone.utc) - datetime.fromisoformat(when)).total_seconds() < STALE_OBSERVATION_S
        if fresh and _q(props, "temperature")[0] is not None:
            return station, body
        log.info("online: station %s's latest observation is old or has no temperature; trying the next", sid)
    return None


def nws_reading(station: dict, body: str) -> Reading:
    props, s = json.loads(body)["properties"], station["properties"]
    sid = s["stationIdentifier"]
    when = datetime.fromisoformat(props["timestamp"]).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Reading(source=NAME, sensor_id=f"nws-{sid.lower()}", type="outdoor", received_utc=_now_utc(),
                   raw=body, fields=derive(nws_fields(props)), receiver="api.weather.gov", sensor_utc=when,
                   name=f"{s.get('name') or sid} (Weather Service)")


OPEN_METEO_UNITS = {"temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch",
                    "timezone": "auto"}
OPEN_METEO_CURRENT = ("temperature_2m", "relative_humidity_2m", "dew_point_2m", "apparent_temperature",
                      "precipitation", "weather_code", "pressure_msl", "wind_speed_10m",
                      "wind_direction_10m", "wind_gusts_10m", "uv_index")


def _open_meteo(lat: float, lon: float, contact: str, **params: Any) -> str:
    query = urllib.parse.urlencode({"latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
                                    **OPEN_METEO_UNITS, **params}, safe=",")   # it won't split on %2C
    return _get(f"{OPEN_METEO_API}?{query}", contact, accept="application/json")


def _local_to_utc(local: str, offset_s: int) -> datetime:
    return (datetime.fromisoformat(local) - timedelta(seconds=offset_s)).replace(tzinfo=timezone.utc)


def open_meteo_reading(body: str) -> Reading:
    doc = json.loads(body)
    cur = doc["current"]
    names = {"temperature_2m": "temp_f", "relative_humidity_2m": "humidity_pct", "dew_point_2m": "dew_point_f",
             "apparent_temperature": "feels_like_f", "wind_speed_10m": "wind_mph",
             "wind_direction_10m": "wind_dir_deg", "wind_gusts_10m": "wind_gust_mph", "uv_index": "uv_index"}
    fields = {name: cur[key] for key, name in names.items() if cur.get(key) is not None}
    if cur.get("pressure_msl") is not None:
        fields["pressure_inhg"] = round(cur["pressure_msl"] * 0.0295300, 2)
    when = _local_to_utc(cur["time"], doc.get("utc_offset_seconds", 0)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Reading(source=NAME, sensor_id="open-meteo", type="outdoor", received_utc=_now_utc(), raw=body,
                   fields=derive(fields), receiver="api.open-meteo.com", sensor_utc=when,
                   name="Outside (Open-Meteo estimate)")


def run(cfg: configparser.ConfigParser, emit: Emit) -> None:
    where = _where(cfg)
    if not where:
        log.warning("[online] is on but [station] latitude/longitude are not set; nothing to fetch")
        return
    lat, lon = where
    contact = _contact(cfg)
    source = choose(cfg, "online", lat, lon)
    log.info("online: readings from %s", "the Weather Service" if source == "nws" else "Open-Meteo")
    last = None
    stations: list = []
    while True:
        wait = NWS_EVERY_S if source == "nws" else OPEN_METEO_EVERY_S
        try:
            if source == "nws":
                if not stations:
                    stations = json.loads(_get(nws_point(lat, lon, contact)["observationStations"], contact))["features"]
                found = _nws_latest(stations, contact)
                reading = nws_reading(*found) if found else None
                if not found:
                    log.warning("online: none of the %d nearest stations has a recent observation", STATIONS_TRIED)
            else:
                reading = open_meteo_reading(_open_meteo(lat, lon, contact, current=",".join(OPEN_METEO_CURRENT)))
            # The same observation fetched twice is recorded once.
            if reading and (reading.sensor_id, reading.sensor_utc) != last:
                emit(reading)
                last = (reading.sensor_id, reading.sensor_utc)
        except Exception as exc:
            log.warning("online: fetch failed: %s", exc)
            wait = RETRY_S
        time.sleep(wait)


# ── Open-Meteo forecast, in the Weather Service's shape for the dashboard ──────

# WMO weather codes → words the dashboard turns into emoji (weather.html skyEmoji).
WEATHER_CODES = {0: ("Sunny", "Clear"), 1: ("Mostly Sunny", "Mostly Clear"), 2: ("Partly Cloudy",) * 2,
                 3: ("Cloudy",) * 2, 45: ("Fog",) * 2, 48: ("Freezing Fog",) * 2,
                 51: ("Light Drizzle",) * 2, 53: ("Drizzle",) * 2, 55: ("Heavy Drizzle",) * 2,
                 56: ("Freezing Drizzle",) * 2, 57: ("Freezing Drizzle",) * 2,
                 61: ("Light Rain",) * 2, 63: ("Rain",) * 2, 65: ("Heavy Rain",) * 2,
                 66: ("Freezing Rain",) * 2, 67: ("Freezing Rain",) * 2,
                 71: ("Light Snow",) * 2, 73: ("Snow",) * 2, 75: ("Heavy Snow",) * 2, 77: ("Snow Grains",) * 2,
                 80: ("Rain Showers",) * 2, 81: ("Rain Showers",) * 2, 82: ("Heavy Rain Showers",) * 2,
                 85: ("Snow Showers",) * 2, 86: ("Heavy Snow Showers",) * 2,
                 95: ("Thunderstorms",) * 2, 96: ("Thunderstorms With Hail",) * 2, 99: ("Thunderstorms With Hail",) * 2}


def sky(code: Any, day: bool) -> str:
    words = WEATHER_CODES.get(code)
    return (words[0] if day else words[1]) if words else ""


def _r(v: Any) -> Any:
    """round(), but None stays None: Open-Meteo pads past its forecast's end with nulls."""
    return None if v is None else round(v)


def cardinal(deg: Any) -> str | None:
    return None if deg is None else CARDINALS[round(deg / 22.5) % 16]


def _iso(local: str, offset_s: int) -> str:
    """Open-Meteo's local time + the place's UTC offset → ISO 8601 with the offset."""
    sign = "+" if offset_s >= 0 else "-"
    h, m = divmod(abs(offset_s) // 60, 60)
    return f"{local}:00{sign}{h:02d}:{m:02d}" if len(local) == 16 else f"{local}{sign}{h:02d}:{m:02d}"


def forecast_from(body: str, now: datetime | None = None) -> dict:
    """An Open-Meteo forecast → {"hourly": ..., "daily": ...} as the dashboard reads them."""
    doc = json.loads(body)
    off = doc.get("utc_offset_seconds", 0)
    now = now or datetime.now(timezone.utc)
    h = doc["hourly"]
    hours = []
    for i, t in enumerate(h["time"]):
        if _local_to_utc(t, off) + timedelta(hours=1) <= now or h["temperature_2m"][i] is None:
            continue
        day = bool(h["is_day"][i])
        hours.append({"start": _iso(t, off), "day": day, "temp_f": _r(h["temperature_2m"][i]),
                      "pop": h["precipitation_probability"][i], "humidity_pct": h["relative_humidity_2m"][i],
                      "wind_mph": _r(h["wind_speed_10m"][i]), "wind_from": cardinal(h["wind_direction_10m"][i]),
                      "sky": sky(h["weather_code"][i], day)})
    # The daily weather code is the day's worst; a half-day's sky is its most common
    # hourly one, as the Weather Service's short forecast is the prevailing weather.
    by_hour = {t: i for i, t in enumerate(h["time"])}

    def half(date: str, first_hour: int) -> tuple[str, int | None, int | None, str | None]:
        start = datetime.fromisoformat(f"{date}T{first_hour:02d}:00")
        idx = [by_hour[k] for k in ((start + timedelta(hours=n)).strftime("%Y-%m-%dT%H:%M") for n in range(12))
               if k in by_hour and h["weather_code"][by_hour[k]] is not None]
        if not idx:
            return "", None, None, None
        codes = [h["weather_code"][i] for i in idx]
        pops = [h["precipitation_probability"][i] for i in idx if h["precipitation_probability"][i] is not None]
        pop = max(pops) if pops else None
        wet = [c for c in codes if c >= 51]   # drizzle, rain, snow, showers, storms
        if wet and (pop or 0) >= 50:          # likely rain names the rain, however few its hours
            codes = wet
        common = max(sorted(set(codes), reverse=True), key=codes.count)   # a tie goes to the wetter code
        winds = [i for i in idx if h["wind_speed_10m"][i] is not None]
        top = max(winds, key=lambda i: h["wind_speed_10m"][i]) if winds else None
        wind = _r(h["wind_speed_10m"][top]) if top is not None else None
        frm = cardinal(h["wind_direction_10m"][top]) if top is not None else None
        return sky(common, first_hour == 6), pop, wind, frm

    d = doc["daily"]
    periods = []
    for i, date in enumerate(d["time"]):
        if d["temperature_2m_max"][i] is None:
            break
        weekday = datetime.fromisoformat(date).strftime("%A")
        hi, wind, frm = _r(d["temperature_2m_max"][i]), _r(d["wind_speed_10m_max"][i]), cardinal(d["wind_direction_10m_dominant"][i])
        words, pop, _, _ = half(date, 6)
        words = words or sky(d["weather_code"][i], True)
        pop = d["precipitation_probability_max"][i] if pop is None else pop
        detail = f"{words}. High {hi}°F." + (f" Chance of precipitation {pop}%." if pop else "") \
            + f" Wind up to {wind} mph" + (f" from the {frm}." if frm else ".")
        periods.append({"name": "Today" if i == 0 else weekday, "start": _iso(f"{date}T06:00", off), "day": True,
                        "temp_f": hi, "pop": pop, "wind": f"{wind} mph", "wind_from": frm, "sky": words,
                        "detail": detail})
        if i + 1 < len(d["time"]) and d["temperature_2m_min"][i + 1] is not None:   # tonight's low is the next morning's
            lo = round(d["temperature_2m_min"][i + 1])
            words, pop, nwind, nfrm = half(date, 18)
            if not words:
                break
            periods.append({"name": "Tonight" if i == 0 else f"{weekday} Night", "start": _iso(f"{date}T18:00", off),
                            "day": False, "temp_f": lo, "pop": pop,
                            "wind": f"{nwind} mph" if nwind is not None else None, "wind_from": nfrm, "sky": words,
                            "detail": f"{words}. Low around {lo}°F." + (f" Chance of precipitation {pop}%." if pop else "")
                            + (f" Wind up to {nwind} mph" + (f" from the {nfrm}." if nfrm else ".")
                               if nwind is not None else "")})
    if periods and _local_to_utc(f"{d['time'][0]}T18:00", off) <= now:
        periods.pop(0)   # the evening: today is over, tonight leads
    issued = now.isoformat(timespec="seconds")   # Open-Meteo gives no issue time; this is when we asked
    return {"hourly": {"updated": issued, "periods": hours[:HOURS_KEPT]},
            "daily": {"updated": issued, "periods": periods[:DAILY_PERIODS_KEPT]}}


def open_meteo_forecast(lat: float, lon: float, contact: str = DEFAULT_CONTACT) -> dict:
    body = _open_meteo(lat, lon, contact, forecast_days=8, past_hours=1,
                       hourly="temperature_2m,relative_humidity_2m,precipitation_probability,weather_code,"
                              "wind_speed_10m,wind_direction_10m,is_day",
                       daily="weather_code,temperature_2m_max,temperature_2m_min,"
                             "precipitation_probability_max,wind_speed_10m_max,wind_direction_10m_dominant")
    return forecast_from(body)
