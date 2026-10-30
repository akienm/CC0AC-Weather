"""Checks for 1.3.13: NOAA-style summaries (noaa.py, /noaa) and the Cumulus import.
python3 -m unittest tests/test_noaa_cumulus.py

The readings are made up, from made-up sensors, at local times. On 2026-10-24
a month and the year were checked by hand against three weeks of a real
station's readings, and the year's numbers against that month's.
"""
import argparse
import configparser
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from cc0ac import importer     # noqa: E402
from cc0ac import noaa         # noqa: E402

from cc0ac import capture

from cc0ac import store

from cc0ac import web

SENSOR = "00001234"


def cfg(**sections) -> configparser.ConfigParser:
    c = configparser.ConfigParser()
    for name, keys in sections.items():
        c[name] = keys
    return c


def utc(*local) -> str:
    """A local time, as received_utc is stored."""
    return datetime(*local).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Station(unittest.TestCase):
    """Two days of an outdoor sensor in May 2024, a day between them with nothing."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.db = self.root / "weather.db"
        con = store.open_db(self.db)
        n = 0
        for day, hour, temp, rain, wind, gust, heading in (
                (1, 3, 40.0, 0.0, 2.0, 4.0, 180), (1, 9, 50.0, 0.1, 4.0, 9.0, 180),
                (1, 15, 70.0, 0.3, 10.0, 21.0, 200), (1, 21, 55.0, 0.3, 0.0, 3.0, 0),
                (3, 4, 30.0, 0.0, 6.0, 8.0, 270), (3, 14, 44.0, 0.0, 6.0, 12.0, 270)):
            n += 1
            store.store_reading(con, capture.Reading(
                source="acurite-access", sensor_id=SENSOR, type="Atlas", received_utc=utc(2024, 5, day, hour, 0),
                raw=f"fake-{n}", fields={"temp_f": temp, "rain_day_in": rain, "wind_mph": wind,
                                         "wind_gust_mph": gust, "wind_dir_deg": heading}))
        con.close()

    def tearDown(self):
        self.dir.cleanup()

    def summary(self, period, units=None):
        c = cfg(units=units or {})
        return store.noaa_summary(c, self.db, {SENSOR: "Back yard"}, {"period": period})


class Month(Station):
    def test_a_month_has_every_day_and_sums_the_ones_with_readings(self):
        got = self.summary("2024-05")
        self.assertEqual((got["period"], got["sensor"]), ("2024-05", SENSOR))
        self.assertEqual(got["sensors"], [{"id": SENSOR, "name": "Back yard"}])
        lines = got["text"].splitlines()
        self.assertIn("MONTHLY CLIMATOLOGICAL SUMMARY for May 2024", lines[0])
        self.assertIn("NAME: Back yard", lines)
        day1 = next(line for line in lines if line.startswith("01 ")).split()
        # mean, high, time, low, time, heating dd, cooling dd, rain, wind, gust, time, direction
        self.assertEqual(day1, ["01", "53.8", "70.0", "15:00", "40.0", "03:00", "10.0", "0.0", "0.30",
                                "4.0", "21.0", "15:00", "SSW"])
        self.assertEqual(next(line for line in lines if line.startswith("02")), "02")   # no readings: blank
        day3 = next(line for line in lines if line.startswith("03 ")).split()
        self.assertEqual((day3[2], day3[6], day3[-1]), ("44.0", "28.0", "W"))
        self.assertIn("Days with readings: 2", lines)
        self.assertIn("Days with low <= 32.0: 1", lines)
        self.assertIn("Most rain in a day: 0.30 on day 1", lines)
        total = lines[lines.index("-" * 86, lines.index("-" * 86) + 1) + 1]       # under the second rule
        self.assertEqual(total.split()[:7], ["45.4", "70.0", "1", "30.0", "3", "38.0", "0.0"])

    def test_metric_shows_celsius_and_millimetres(self):
        text = self.summary("2024-05", {"system": "metric"})["text"]
        self.assertIn("TEMPERATURE (°C), RAIN (mm), WIND SPEED (km/h)", text)
        day1 = next(line for line in text.splitlines() if line.startswith("01 ")).split()
        self.assertEqual((day1[2], day1[6], day1[8]), ("21.1", "5.6", "7.6"))   # a degree-day change, not a temperature

    def test_a_sensor_without_readings_then_is_refused(self):
        with self.assertRaises(ValueError):
            store.noaa_summary(cfg(), self.db, {}, {"period": "2024-05", "sensor": "00009999"})
        with self.assertRaises(ValueError):
            noaa.period("May 2024")


class Year(Station):
    def test_a_year_from_the_daily_table_agrees_with_its_month(self):
        con = sqlite3.connect(self.db)
        store.roll_up(con, datetime(2024, 6, 1).timestamp())
        con.close()
        text = self.summary("2024")["text"]
        lines = text.splitlines()
        self.assertIn("ANNUAL CLIMATOLOGICAL SUMMARY for 2024", lines[0])
        may = [line.split() for line in lines if line.startswith("24  05")]
        self.assertEqual(len(may), 3)                  # temperature, rain, wind
        self.assertEqual(may[0][2:9], ["57.0", "35.0", "45.4", "38.0", "0.0", "70.0", "01"])
        self.assertEqual(may[1][2:5], ["0.30", "0.30", "01"])
        self.assertEqual(may[2][2:6], ["5.0", "21.0", "01", "SW"])
        whole = [line.split() for line in lines if line.startswith("      ") and line.strip()[:1].isdigit()]
        self.assertEqual(whole[0][5:9], ["70.0", "05-01", "30.0", "05-03"])   # the whole year names the month too
        self.assertIn("Days with readings: 2", lines)
        self.assertEqual(next(line for line in lines if line.startswith("24  01")), "24  01")


class Pages(Station):
    def setUp(self):
        super().setUp()
        handler = web._make_web_handler(self.root / "out", self.db, {SENSOR: "Back yard"}, cfg=cfg())
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.httpd.server_port}{path}", timeout=10) as r:
            return r.headers, r.read().decode()

    def test_the_page_its_numbers_and_the_download(self):
        _, page = self.get("/noaa")
        self.assertIn("/noaa.json?", page)
        _, body = self.get("/noaa.json?period=2024-05")
        self.assertIn("NAME: Back yard", json.loads(body)["text"])
        headers, text = self.get("/noaa.txt?period=2024-05")
        self.assertIn('filename="NOAA-2024-05.txt"', headers["Content-Disposition"])
        self.assertTrue(text.startswith(" " * 10))
        for path in ("/noaa", "/noaa.json", "/noaa.txt", "/noaa.html"):
            self.assertNotIn(path, web.OUTSIDE_PATHS)

    def test_a_bad_period_is_a_400(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get("/noaa.json?period=soon")
        self.assertEqual(e.exception.code, 400)


class DegreeDays(unittest.TestCase):
    def test_from_the_middle_of_high_and_low(self):
        self.assertEqual(noaa.degree_days(70, 40, 65, heating=True), 10)
        self.assertEqual(noaa.degree_days(90, 70, 65, heating=True), 0)
        self.assertEqual(noaa.degree_days(90, 70, 65, heating=False), 15)
        self.assertIsNone(noaa.degree_days(None, 40, 65, heating=True))
        self.assertEqual(noaa.direction(0, 0), "---")
        self.assertEqual(noaa.direction(-1, 0), "W")


# Cumulus MX's month log, the fields its documentation numbers 1 to 29.
LOG_US = ("24/10/24,14:00,70.2,50,50.5,5.0,9.0,200,0.0,0.10,30.01,12.3,72.0,40,6.0,70.2,71.0,3,500,0.1,10.0,"
          "70.0,600,2.0,210,0.0,0.10,70.5,75.0\n"
          "24/10/24,14:30,71.0,49,50.9,4.0,8.0,210,0.0,0.30,30.00,12.5,72.0,40,3.0,71.0,72.0,3,450,0.1,10.1,"
          "71.0,600,2.5,220,0.0,0.30,71.5,76.0\n"
          "25/10/24,00:30,50.0,80,44.0,0.0,0.0,0,0.0,0.00,30.05,12.5,70.0,42,0.0,50.0,50.0,0,0,0.0,10.1,"
          "49.0,0,2.5,0,0.0,0.00,49.0,50.0\n")


class Cumulus(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / "data").mkdir()

    def tearDown(self):
        self.dir.cleanup()

    def test_us_units_from_cumulus_ini_and_rain_by_the_hour(self):
        (self.root / "Cumulus.ini").write_text("[Station]\nWindUnit=1\nPressureUnit=2\nRainUnit=1\nTempUnit=1\n")
        (self.root / "data" / "Oct24log.txt").write_text(LOG_US)
        (self.root / "data" / "dayfile.txt").write_text("not a month log\n")
        said = []
        rows = list(importer.rows_cumulus(self.root, say=said.append))
        self.assertIn("°F, mph, inHg, in, from", said[0])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["epoch"], datetime(2024, 10, 24, 14, 0).timestamp())
        f = rows[1]["fields"]
        self.assertEqual((f["temp_f"], f["humidity_pct"], f["wind_avg_mph"], f["wind_gust_mph"], f["wind_mph"],
                          f["wind_dir_deg"], f["pressure_inhg"], f["uv_index"], f["feels_like_f"]),
                         (71.0, 49, 4.0, 8.0, 3.0, 210, 30.0, 3, 71.5))
        self.assertEqual(f["rain_day_in"], 0.3)
        self.assertAlmostEqual(f["rain_hour_in"], 0.3)       # 0.1, then 0.2 more within the hour
        self.assertEqual(rows[2]["fields"]["rain_day_in"], 0.0)   # a new day starts again

    def test_semicolons_decimal_commas_and_cumulus_defaults(self):
        log = self.root / "Mar25log.txt"
        log.write_text("05.03.25;09:15;10,0;80;6,7;5,0;10,0;90;0,0;2,5;1013,2;100;20,0;45;4,0\n"
                       "garbage line\n")
        said = []
        rows = list(importer.rows_cumulus(log, say=said.append))
        self.assertIn("°C, m/s, hPa, mm: no Cumulus.ini found", said[0])
        f = rows[0]["fields"]
        self.assertEqual(rows[0]["epoch"], datetime(2025, 3, 5, 9, 15).timestamp())
        self.assertEqual(round(f["temp_f"], 1), 50.0)
        self.assertEqual(round(f["wind_gust_mph"], 2), 22.37)          # 10 m/s
        self.assertEqual(round(f["pressure_inhg"], 2), 29.92)
        self.assertEqual(round(f["rain_day_in"], 3), 0.098)            # 2.5 mm
        self.assertIsNone(f["feels_like_f"])                          # an older log stops at field 15
        with_metric = list(importer.rows_cumulus(log, metric=True, say=said.append))
        self.assertEqual(round(with_metric[0]["fields"]["wind_gust_mph"], 2), 6.21)   # 10 km/h

    def test_import_a_cumulus_folder(self):
        (self.root / "Cumulus.ini").write_text("[Station]\nWindUnit=1\nPressureUnit=2\nRainUnit=1\nTempUnit=1\n")
        (self.root / "data" / "Oct24log.txt").write_text(LOG_US)
        db = self.root / "weather.db"
        con = store.open_db(db)
        store.store_reading(con, capture.Reading(source="acurite-access", sensor_id=SENSOR, type="Atlas",
                                                   received_utc="2026-10-01T00:00:00Z", raw="live-1",
                                                   fields={"temp_f": 60.0}))
        con.close()
        c = cfg(device={"write_path": str(self.root)}, storage={"database": str(db)})
        args = argparse.Namespace(import_from=str(self.root), sensor=None, metric=False, wu_station=None,
                                  wu_api_key=None, date_from=None, date_to=None)
        capture.import_history(c, args)
        con = sqlite3.connect(db)
        got = con.execute("SELECT sensor_id, substr(query, 1, 14), temp_f FROM readings WHERE query LIKE 'import:%' "
                          "ORDER BY received_utc").fetchall()
        con.close()
        self.assertEqual(got, [(SENSOR, "import:cumulus", 70.2), (SENSOR, "import:cumulus", 71.0),
                               (SENSOR, "import:cumulus", 50.0)])


if __name__ == "__main__":
    unittest.main()
