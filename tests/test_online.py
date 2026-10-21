"""Checks for the internet-weather source: python3 -m unittest tests/test_online.py

These run on made-up answers shaped like the services' documentation, so they need
no network. The live services were checked by hand on 2026-10-21 for a US point
(the Weather Service) and London (Open-Meteo).
"""
import configparser
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sources import online   # noqa: E402


def cfg(**online_keys) -> configparser.ConfigParser:
    c = configparser.ConfigParser()
    c["online"] = online_keys
    return c


class Units(unittest.TestCase):
    def test_weather_service_units_become_ours(self):
        props = {"temperature": {"value": 20, "unitCode": "wmoUnit:degC"},
                 "relativeHumidity": {"value": 41.6},
                 "windSpeed": {"value": 16.09344, "unitCode": "wmoUnit:km_h-1"},
                 "windGust": {"value": 10, "unitCode": "wmoUnit:m_s-1"},
                 "windDirection": {"value": 270},
                 "seaLevelPressure": {"value": 101325},
                 "barometricPressure": {"value": 80000},
                 "precipitationLastHour": {"value": 25.4}}
        f = online.nws_fields(props)
        self.assertEqual(f["temp_f"], 68.0)
        self.assertEqual(f["humidity_pct"], 42)
        self.assertEqual(f["wind_mph"], 10.0)
        self.assertEqual(f["wind_gust_mph"], 22.4)
        self.assertEqual(f["wind_dir_deg"], 270)
        self.assertEqual(f["pressure_inhg"], 29.92)   # sea level, not the station's own
        self.assertEqual(f["rain_hour_in"], 1.0)

    def test_missing_values_are_left_out(self):
        self.assertEqual(online.nws_fields({"temperature": {"value": None, "unitCode": "wmoUnit:degC"}}), {})


class Choosing(unittest.TestCase):
    def test_a_named_source_is_used_without_asking(self):
        with mock.patch.object(online, "nws_point") as ask:
            self.assertEqual(online.choose(cfg(source="open-meteo"), "online", 0, 0), "open-meteo")
            ask.assert_not_called()

    def test_auto_uses_the_weather_service_where_it_covers(self):
        with mock.patch.object(online, "nws_point", return_value={}):
            self.assertEqual(online.choose(cfg(source="auto"), "online", 40, -105), "nws")

    def test_auto_uses_open_meteo_only_on_not_covered(self):
        with mock.patch.object(online, "nws_point", side_effect=online.NotCovered("404")):
            self.assertEqual(online.choose(cfg(), "online", 51.5, 0), "open-meteo")

    def test_auto_retries_a_hiccup_instead_of_giving_up_on_the_weather_service(self):
        answers = [OSError("timed out"), {}]
        with mock.patch.object(online, "nws_point", side_effect=answers), \
                mock.patch.object(online.time, "sleep") as slept:
            self.assertEqual(online.choose(cfg(), "online", 40, -105), "nws")
            slept.assert_called_once_with(online.RETRY_S)


def open_meteo_body(start: str, hours: int, days: int, code, pop) -> str:
    """A forecast answer at UTC+1: every hour has weather code `code(n)` and rain chance `pop(n)`."""
    t0 = datetime.fromisoformat(start)
    times = [(t0 + timedelta(hours=n)).strftime("%Y-%m-%dT%H:%M") for n in range(hours)]
    day0 = t0.date()
    dates = [(day0 + timedelta(days=n)).isoformat() for n in range(days)]
    return json.dumps({
        "utc_offset_seconds": 3600,
        "hourly": {"time": times,
                   "temperature_2m": [50.0] * hours, "relative_humidity_2m": [80] * hours,
                   "precipitation_probability": [pop(n) for n in range(hours)],
                   "weather_code": [code(n) for n in range(hours)],
                   "wind_speed_10m": [5.0 + (n % 24 == 20) * 10 for n in range(hours)],
                   "wind_direction_10m": [180] * hours,
                   "is_day": [int(6 <= (t0 + timedelta(hours=n)).hour < 18) for n in range(hours)]},
        "daily": {"time": dates, "weather_code": [65] * days,
                  "temperature_2m_max": [60.0] * days, "temperature_2m_min": [40.0] * days,
                  "precipitation_probability_max": [90] * days,
                  "wind_speed_10m_max": [20.0] * days, "wind_direction_10m_dominant": [270] * days}})


class Forecast(unittest.TestCase):
    def test_a_clear_night_reads_clear_though_the_day_had_heavy_rain(self):
        # Rain 06:00-18:00 local, clear after: the daily code (65) is the day's worst.
        body = open_meteo_body("2026-10-21T00:00", 72, 3,
                               code=lambda n: 65 if 6 <= n % 24 < 18 else 0, pop=lambda n: 90 if 6 <= n % 24 < 18 else 0)
        now = datetime(2026, 10, 21, 0, 30, tzinfo=timezone.utc)
        periods = online.forecast_from(body, now)["daily"]["periods"]
        self.assertEqual([p["name"] for p in periods[:2]], ["Today", "Tonight"])
        self.assertEqual(periods[0]["sky"], "Heavy Rain")
        self.assertEqual(periods[1]["sky"], "Clear")
        self.assertEqual(periods[1]["temp_f"], 40)

    def test_likely_rain_names_the_rain_however_few_its_hours(self):
        body = open_meteo_body("2026-10-21T00:00", 72, 3,
                               code=lambda n: 61 if n % 24 == 9 else 1, pop=lambda n: 80)
        now = datetime(2026, 10, 21, 0, 30, tzinfo=timezone.utc)
        self.assertEqual(online.forecast_from(body, now)["daily"]["periods"][0]["sky"], "Light Rain")

    def test_after_six_in_the_evening_tonight_leads(self):
        body = open_meteo_body("2026-10-21T00:00", 72, 3, code=lambda n: 0, pop=lambda n: 0)
        now = datetime(2026, 10, 21, 18, 30, tzinfo=timezone.utc)   # 19:30 local
        f = online.forecast_from(body, now)
        self.assertEqual(f["daily"]["periods"][0]["name"], "Tonight")
        self.assertTrue(all(datetime.fromisoformat(h["start"]) + timedelta(hours=1) > now
                            for h in f["hourly"]["periods"]))

    def test_night_wind_is_the_nights_own_peak(self):
        body = open_meteo_body("2026-10-21T00:00", 72, 3, code=lambda n: 0, pop=lambda n: 0)
        now = datetime(2026, 10, 21, 0, 30, tzinfo=timezone.utc)
        tonight = online.forecast_from(body, now)["daily"]["periods"][1]
        self.assertEqual((tonight["wind"], tonight["wind_from"]), ("15 mph", "S"))

    def test_hours_past_the_end_of_the_forecast_stop_it_cleanly(self):
        doc = json.loads(open_meteo_body("2026-10-21T00:00", 72, 3, code=lambda n: 0, pop=lambda n: 0))
        for key in ("temperature_2m", "weather_code"):
            doc["hourly"][key][48:] = [None] * 24
        doc["daily"]["temperature_2m_max"][2] = None
        now = datetime(2026, 10, 21, 0, 30, tzinfo=timezone.utc)
        f = online.forecast_from(json.dumps(doc), now)
        self.assertEqual(len(f["hourly"]["periods"]), 47)   # 48 with values, less the hour already over
        self.assertEqual([p["name"] for p in f["daily"]["periods"]][-1], "Thursday Night")


if __name__ == "__main__":
    unittest.main()
