"""Checks for 1.3.14: degree days and evapotranspiration on the dashboard.
python3 -m unittest tests/test_degree_days_et0.py

The sun's energy is checked against FAO Irrigation Paper 56's example 8 (20°S,
3 September: 32.2 MJ/m²/day); the evapotranspiration against the same day
worked by hand from equation 52. Sensor 00001234 is made up.
"""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import climate      # noqa: E402

DAYS = [{"day": "2026-06-30", "temp_max": 50, "temp_min": 40},    # last heating season: left out
        {"day": "2026-07-01", "temp_max": 60, "temp_min": 50},    # 10 heating
        {"day": "2026-10-01", "temp_max": 90, "temp_min": 70},    # 15 cooling
        {"day": "2026-10-02"},                                    # no temperature: counts for nothing
        {"day": "2026-10-24", "temp_max": 55, "temp_min": 45}]    # today so far: 15 heating


class Sun(unittest.TestCase):
    def test_extraterrestrial_matches_fao_example_8(self):
        self.assertAlmostEqual(climate.extraterrestrial(-20, "2015-09-03"), 32.2, places=1)

    def test_polar_night_has_no_sun_and_no_error(self):
        self.assertAlmostEqual(climate.extraterrestrial(80, "2026-12-21"), 0, places=3)

    def test_et0_by_hargreaves(self):
        # 30 °C and 18 °C at 40°N on July 15: Ra 40.80, so
        # 0.0023 × 41.8 × √12 × 0.408 × 40.80 = 5.544 mm.
        self.assertAlmostEqual(climate.et0(86, 64.4, 40, "2026-07-15") * 25.4, 5.544, places=2)
        self.assertIsNone(climate.et0(86, 64.4, None, "2026-07-15"))
        self.assertIsNone(climate.et0(None, 64.4, 40, "2026-07-15"))


class DegreeDays(unittest.TestCase):
    def test_seasons_start_july_and_january_and_swap_south(self):
        self.assertEqual(climate.season_start("2026-10-24", True, 45), "2026-07-01")
        self.assertEqual(climate.season_start("2026-03-01", True, 45), "2025-07-01")
        self.assertEqual(climate.season_start("2026-10-24", False, 45), "2026-01-01")
        self.assertEqual(climate.season_start("2026-10-24", True, -35), "2026-01-01")
        self.assertEqual(climate.season_start("2026-03-01", False, -35), "2025-07-01")

    def test_today_month_and_season(self):
        got = climate.degree_days_and_et0(DAYS, "2026-10-24", 40, (65, 65))
        self.assertEqual(got["heating"], {"today": 15.0, "month": 15.0, "season": 25.0, "season_from": "2026-07-01"})
        self.assertEqual(got["cooling"], {"today": 0.0, "month": 15.0, "season": 15.0, "season_from": "2026-01-01"})
        self.assertEqual(got["et0"]["month"], round(sum(climate.et0(d["temp_max"], d["temp_min"], 40, d["day"])
                                                        for d in DAYS[2::2]), 2))

    def test_bases_move_the_count(self):
        got = climate.degree_days_and_et0(DAYS, "2026-10-24", 40, (60, 75))
        self.assertEqual(got["heating"]["today"], 10.0)
        self.assertEqual(got["cooling"]["month"], 5.0)

    def test_noaa_summaries_count_the_same_way(self):
        import noaa
        self.assertIs(noaa.degree_days, climate.degree_days)


class ClimateJson(unittest.TestCase):
    def test_build_carries_them_per_sensor(self):
        doc = climate.build([{"id": "00001234", "name": "Yard", "days": DAYS}], "2026-10-24", None, 40, (65, 65))
        s = doc["sensors"][0]
        self.assertEqual(s["heating"]["season"], 25.0)
        self.assertEqual(s["cooling"]["season"], 15.0)
        self.assertIsNotNone(s["et0"]["today"])

    def test_no_latitude_no_et0_but_degree_days(self):
        s = climate.build([{"id": "00001234", "name": "Yard", "days": DAYS}], "2026-10-24", None)["sensors"][0]
        self.assertEqual(s["et0"], {"today": None, "month": None})
        self.assertEqual(s["heating"]["today"], 15.0)

    def test_normals_off_still_uses_the_latitude(self):
        """[climate] normals = false fetches nothing, and the evapotranspiration still has its latitude."""
        class Done(Exception):
            pass
        fetched, real_normals, real_sleep = [], climate.normals, climate.time.sleep
        climate.normals = lambda *a: fetched.append(a)
        climate.time.sleep = lambda s: (_ for _ in ()).throw(Done())
        try:
            with tempfile.TemporaryDirectory() as tmp:
                today = climate.date.today().isoformat()
                days = [{"day": today, "temp_max": 80, "temp_min": 60}]
                with self.assertRaises(Done):
                    climate.run(40, -105, Path(tmp), lambda: [{"id": "00001234", "name": "Yard", "days": days}],
                                False, (65, 65))
                doc = climate.json.loads((Path(tmp) / "climate.json").read_text())
        finally:
            climate.normals, climate.time.sleep = real_normals, real_sleep
        self.assertEqual(fetched, [])
        self.assertIsNotNone(doc["sensors"][0]["et0"]["today"])


class Dashboard(unittest.TestCase):
    def test_climate_card_shows_the_three_rows(self):
        page = (HERE / "weather.html").read_text()
        for label in ("degree days", "season", "${ET0} today", "${ET0} this month", "Evapotranspiration"):
            self.assertIn(label, page)


if __name__ == "__main__":
    unittest.main()
