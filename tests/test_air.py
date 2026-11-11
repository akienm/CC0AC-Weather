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


# pollen.com's answer for a US ZIP, as measured live 2026-11-11 (place names generic).
POLLEN_COM = {"Type": "pollen", "ForecastDate": "2026-11-11T00:00:00-05:00",
              "Location": {"ZIP": "12345", "periods": [
                  {"Type": "Yesterday", "Index": 3.8, "Triggers": [{"Name": "Ragweed"}]},
                  {"Type": "Today", "Index": 4.2, "Triggers": [{"Name": "Ragweed"}, {"Name": "Chenopods"},
                                                                {"Name": "Sagebrush"}]},
                  {"Type": "Tomorrow", "Index": 7.5, "Triggers": [{"Name": "Ragweed"}]}]}}

# Google's Pollen API answer, in the shape its reference documents.
GOOGLE = {"regionCode": "US", "dailyInfo": [
    {"date": {"year": 2026, "month": 11, "day": 11}, "pollenTypeInfo": [
        {"code": "GRASS", "displayName": "Grass", "inSeason": False,
         "indexInfo": {"code": "UPI", "value": 1, "category": "Very Low", "color": {"green": 0.62, "blue": 0.23}}},
        {"code": "TREE", "displayName": "Tree"},
        {"code": "WEED", "displayName": "Weed", "inSeason": True,
         "indexInfo": {"code": "UPI", "value": 3, "category": "Moderate", "color": {"red": 1, "green": 1}}}]},
    {"date": {"year": 2026, "month": 11, "day": 12}, "pollenTypeInfo": [
        {"code": "GRASS", "displayName": "Grass", "indexInfo": {"code": "UPI", "value": 0, "category": "None"}}]}]}


class PollenForecasts(unittest.TestCase):
    def test_pollen_com_today_and_tomorrow_not_yesterday(self):
        f = air.parse_pollen_com(POLLEN_COM)
        self.assertEqual((f["source"], f["scale"]), ("pollen.com", 12))
        self.assertEqual([(d["day"], d["index"], d["level"]["name"]) for d in f["days"]],
                         [("2026-11-11", 4.2, "Low-medium"), ("2026-11-12", 7.5, "Medium-high")])
        self.assertEqual([t["name"] for t in f["days"][0]["types"]], ["Ragweed", "Chenopods", "Sagebrush"])

    def test_pollen_com_levels_at_their_edges(self):
        self.assertEqual(air.pollen_com_level(2.4)["name"], "Low")
        self.assertEqual(air.pollen_com_level(2.5)["name"], "Low-medium")
        self.assertEqual(air.pollen_com_level(12)["name"], "High")
        self.assertIsNone(air.pollen_com_level(None))

    def test_google_the_day_is_its_worst_type_and_unmeasured_types_drop(self):
        f = air.parse_google_pollen(GOOGLE)
        self.assertEqual((f["source"], f["scale"]), ("Google Pollen", 5))
        today, tomorrow = f["days"]
        self.assertEqual((today["day"], today["index"], today["level"]["name"]), ("2026-11-11", 3, "Moderate"))
        self.assertEqual(today["level"]["colour"], "#ffff00")
        self.assertEqual([t["name"] for t in today["types"]], ["Weed", "Grass"])
        self.assertEqual((tomorrow["index"], tomorrow["types"]), (0, []))

    def test_sources_in_order_and_only_those_set(self):
        self.assertEqual([n for n, _ in air.pollen_sources(1, 2, "k", "12345")], ["google-pollen", "pollen-com"])
        self.assertEqual([n for n, _ in air.pollen_sources(1, 2, "", "12345")], ["pollen-com"])
        self.assertEqual(air.pollen_sources(1, 2), [])

    def test_the_private_settings_are_kept_out_of_config_ini(self):
        from cc0ac import private
        self.assertIn("google_api_key", private.PRIVATE_KEYS)
        self.assertIn("pollen_zip", private.PRIVATE_KEYS)


def answer_doc():
    return answer()


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

    def run_pollen(self, google, pollen_com):
        """One pass with both pollen sources set; each answers what it is given (a forecast or an exception)."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            def answer(x):
                if isinstance(x, Exception):
                    raise x
                return x
            with mock.patch.object(air, "fetch", return_value=air.parse(answer_doc())), \
                 mock.patch.object(air, "fetch_google_pollen", side_effect=lambda *a: answer(google)), \
                 mock.patch.object(air, "fetch_pollen_com", side_effect=lambda *a: answer(pollen_com)), \
                 mock.patch.object(air.time, "sleep", side_effect=Stop), \
                 mock.patch.object(air.iolog, "report") as logged:
                with self.assertRaises(Stop):
                    air.run(32.77, -108.28, "test", path, google_key="SECRETKEY", zip_code="12345")
            return json.loads((path / "air.json").read_text()), logged

    def test_google_first(self):
        doc, _ = self.run_pollen(air.parse_google_pollen(GOOGLE), air.parse_pollen_com(POLLEN_COM))
        self.assertEqual(doc["pollen_forecast"]["source"], "Google Pollen")
        self.assertNotIn("pollen", doc["errors"])

    def test_pollen_com_when_google_fails_and_the_key_never_shows(self):
        doc, logged = self.run_pollen(OSError("403 for key=SECRETKEY"), air.parse_pollen_com(POLLEN_COM))
        self.assertEqual(doc["pollen_forecast"]["source"], "pollen.com")
        self.assertNotIn("pollen", doc["errors"])
        self.assertNotIn("SECRETKEY", str(logged.call_args_list))

    def test_both_failing_is_named_on_the_page(self):
        doc, _ = self.run_pollen(OSError("403"), ValueError("not json"))
        self.assertNotIn("pollen_forecast", doc)
        self.assertIn("google-pollen: 403", doc["errors"]["pollen"])
        self.assertIn("pollen-com: not json", doc["errors"]["pollen"])
        self.assertEqual(doc["aqi"], 24)


if __name__ == "__main__":
    unittest.main()
