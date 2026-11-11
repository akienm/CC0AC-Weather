"""Checks for air quality: python3 -m unittest tests/test_air.py

These run on answers shaped like Open-Meteo's, so they need no network. The live
service was checked by hand on 2026-11-11 for Silver City, NM (US AQI 24, pollen
empty: Open-Meteo's pollen is Europe only) and London (pollen 0.0 out of season).
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cc0ac import air   # noqa: E402


def answer(pollen=None, **current) -> dict:
    """An Open-Meteo answer: Silver City's of 2026-11-11 13:00 unless changed."""
    cur = {"time": "2026-11-11T13:00", "interval": 3600, "us_aqi": 24, "us_aqi_pm2_5": 9,
           "us_aqi_pm10": 2, "us_aqi_ozone": 24, "us_aqi_nitrogen_dioxide": 0,
           "pm2_5": 1.4, "pm10": 1.9, "ozone": 108.0, "nitrogen_dioxide": 0.2, "dust": 0.0, "uv_index": 4.35}
    cur.update({k: (pollen or {}).get(k) for k in air.POLLEN})
    cur.update(current)
    return {"timezone": "America/Denver",
            "current_units": {"pm2_5": "μg/m³", "ozone": "μg/m³", "grass_pollen": "grains/m³"},
            "current": cur,
            "hourly": {"time": ["2026-11-11T00:00", "2026-11-11T14:00", "2026-11-12T09:00", "2026-11-12T15:00"],
                       "us_aqi": [20, 31, None, 57]}}


class Bands(unittest.TestCase):
    def test_the_epa_edges(self):
        self.assertEqual(air.band(0)["name"], "Good")
        self.assertEqual(air.band(50)["name"], "Good")
        self.assertEqual(air.band(51)["name"], "Moderate")
        self.assertEqual(air.band(150)["name"], "Unhealthy for sensitive groups")
        self.assertEqual(air.band(151)["name"], "Unhealthy")
        self.assertEqual(air.band(301)["name"], "Hazardous")
        self.assertIsNone(air.band(None))


class Parsing(unittest.TestCase):
    def test_the_index_its_band_and_what_drives_it(self):
        a = air.parse(answer())
        self.assertEqual(a["aqi"], 24)
        self.assertEqual(a["band"]["name"], "Good")
        self.assertEqual(a["main_pollutant"], "Ozone")
        self.assertEqual(a["pollutants"]["pm2_5"], {"value": 1.4, "unit": "μg/m³"})
        self.assertEqual(a["uv_index"], 4.35)

    def test_smoke_shows_as_pm25(self):
        a = air.parse(answer(us_aqi=162, us_aqi_pm2_5=162, pm2_5=76.0))
        self.assertEqual(a["main_pollutant"], "PM2.5")
        self.assertEqual(a["band"]["name"], "Unhealthy")

    def test_highest_of_each_day_skips_gaps(self):
        days = air.parse(answer())["days"]
        self.assertEqual([(d["day"], d["max_aqi"]) for d in days], [("2026-11-11", 31), ("2026-11-12", 57)])
        self.assertEqual(days[1]["band"]["name"], "Moderate")

    def test_no_pollen_where_it_is_not_covered(self):
        self.assertEqual(air.parse(answer())["pollen"], [])

    def test_pollen_where_it_is_covered_even_at_zero(self):
        pollen = air.parse(answer(pollen={"grass_pollen": 12.0, "birch_pollen": 0.0}))["pollen"]
        self.assertEqual([(p["name"], p["value"]) for p in pollen], [("Birch", 0.0), ("Grass", 12.0)])
        self.assertEqual(pollen[1]["unit"], "grains/m³")

    def test_a_missing_index_is_none_not_an_error(self):
        a = air.parse({"current": {"time": "2026-11-11T13:00"}})
        self.assertIsNone(a["aqi"])
        self.assertIsNone(a["band"])
        self.assertIsNone(a["main_pollutant"])
        self.assertEqual(a["days"], [])

    def test_the_request_asks_for_everything_parse_reads(self):
        u = air.url(32.77, -108.28)
        for k in air.POLLUTANTS + list(air.POLLEN) + ["hourly=us_aqi", "latitude=32.7700"]:
            self.assertIn(k, u)


class Stop(Exception):
    pass


class Running(unittest.TestCase):
    def run_twice(self, results):
        """run() with fetch answering `results` in turn; returns air.json after each pass."""
        seen = []
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)

            def after_pass(_wait):
                seen.append(json.loads((path / "air.json").read_text()))
                js = (path / "air.js").read_text()
                self.assertTrue(js.startswith("window.ACURITE_AIR = "))
                if len(seen) == len(results):
                    raise Stop
            with mock.patch.object(air, "fetch", side_effect=results), \
                 mock.patch.object(air.time, "sleep", side_effect=after_pass), \
                 mock.patch.object(air.iolog, "report"):
                with self.assertRaises(Stop):
                    air.run(32.77, -108.28, "test", path)
        return seen

    def test_a_failed_fetch_keeps_the_last_good_copy_and_names_the_error(self):
        good = air.parse(answer())
        first, second = self.run_twice([good, OSError("no route to host")])
        self.assertEqual(first["aqi"], 24)
        self.assertEqual(first["errors"], {})
        self.assertEqual(second["aqi"], 24)
        self.assertEqual(second["updated"], first["updated"])
        self.assertIn("no route to host", second["errors"]["air"])

    def test_the_next_good_fetch_clears_the_error(self):
        good = air.parse(answer())
        _, _, third = self.run_twice([OSError("down"), good, good])
        self.assertEqual(third["errors"], {})
        self.assertEqual(third["aqi"], 24)


if __name__ == "__main__":
    unittest.main()
