"""Checks for display units (units.py, units.js) and calibration (calibration.py).
python3 -m unittest tests/test_units_calibration.py

These need no network or radio; every file goes in a folder of its own that is
thrown away afterwards.
"""
import configparser
import json
import re
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
from cc0ac import alerts        # noqa: E402
from cc0ac import calibration   # noqa: E402
from cc0ac import units         # noqa: E402

from cc0ac import capture

from cc0ac import status

from cc0ac import store

from cc0ac import web

US = units.SYSTEMS["us"]
METRIC = units.SYSTEMS["metric"]


def config(**sections) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict(sections)
    return cfg


class Chosen(unittest.TestCase):
    def test_none_set_is_us(self):
        self.assertEqual(units.chosen(config()), US)

    def test_a_system_and_one_kind_changed(self):
        self.assertEqual(units.chosen(config(units={"system": "uk", "wind": "knots"})),
                         {**units.SYSTEMS["uk"], "wind": "knots"})

    def test_what_it_does_not_know_is_logged_and_left(self):
        with self.assertLogs("acurite", "WARNING") as said:
            got = units.chosen(config(units={"system": "imperial", "rain": "cubits", "temperature": "C"}))
        self.assertEqual(got, {**US, "temperature": "c"})
        self.assertEqual(len(said.records), 2)


class Show(unittest.TestCase):
    def test_us_is_as_stored(self):
        self.assertEqual(units.show("temperature", 34, US), "34.0°F")
        self.assertEqual(units.show("pressure", 30.123, US), "30.12 inHg")
        self.assertEqual(units.show("distance", 6, US), "6 mi")

    def test_metric(self):
        self.assertEqual(units.show("temperature", 32, METRIC), "0.0°C")
        self.assertEqual(units.show("temperature", 212, METRIC, 0), "100°C")
        self.assertEqual(units.show("wind", 10, METRIC), "16 km/h")
        self.assertEqual(units.show("pressure", 29.92, METRIC), "1013.2 hPa")
        self.assertEqual(units.show("rain", 1, METRIC), "25.4 mm")
        self.assertEqual(units.show("distance", 10, METRIC), "16 km")

    def test_a_change_takes_no_offset_and_is_signed(self):
        self.assertEqual(units.show("temperature", 9, METRIC, change=True), "+5.0°C")
        self.assertEqual(units.show("pressure", -0.06, US, change=True), "-0.06 inHg")

    def test_the_page_script_has_the_same_table(self):
        js = (HERE / "units.js").read_text()
        for kind, choices in units.KINDS.items():
            line = re.search(rf"^\s*{kind}: \{{(.*?)\}},$", js, re.M | re.S).group(1)
            found = {m[0]: (eval(m[1]), eval(m[2]), m[3], int(m[4])) for m in   # noqa: S307 (numbers only)
                     re.findall(r'(\w+): \[([\d./ ]+), ([-\d./* ]+), "([^"]*)", (\d)\]', line)}
            self.assertEqual(set(found), set(choices), kind)
            for choice, (factor, offset, label, places) in choices.items():
                self.assertAlmostEqual(found[choice][0], factor, 9, (kind, choice))
                self.assertAlmostEqual(found[choice][1], offset, 9, (kind, choice))
                self.assertEqual(found[choice][2:], (label, places), (kind, choice))


class Parse(unittest.TestCase):
    def test_offsets_and_factors(self):
        found, problems = calibration.parse("temp_f -1.5, humidity_pct +3, wind_mph x1.1")
        self.assertEqual(problems, [])
        self.assertEqual(found, {"temp_f": ("+", -1.5), "humidity_pct": ("+", 3.0), "wind_mph": ("x", 1.1)})

    def test_named_in_another_unit(self):
        found, _ = calibration.parse("temp_c -1, pressure_hpa +2, wind_kmh x1.2, rain_day_mm x0.9")
        self.assertAlmostEqual(found["temp_f"][1], -1.8)
        self.assertAlmostEqual(found["pressure_inhg"][1], 2 / 33.8639)
        self.assertEqual(found["wind_mph"], ("x", 1.2))      # a factor is the same in any unit
        self.assertEqual(found["rain_day_in"], ("x", 0.9))

    def test_what_cannot_be_read_is_named(self):
        found, problems = calibration.parse("rain_day_in +0.1, dew_point_f -1, temp_f about 2, uv_index x2")
        self.assertEqual(found, {"uv_index": ("x", 2.0)})
        self.assertEqual(len(problems), 3)
        self.assertIn("only be multiplied", problems[0])
        self.assertIn("isn't a field a sensor measures", problems[1])

    def test_rules_are_by_sensor_and_logged(self):
        cfg = config(calibration={"00001111": "temp_f -2", "00001234": "nonsense"})
        with self.assertLogs("acurite") as said:
            got = calibration.rules(cfg)
        self.assertEqual(got, {"00001111": {"temp_f": ("+", -2.0)}})
        self.assertTrue(any("left out" in r.getMessage() for r in said.records))


class Apply(unittest.TestCase):
    def test_no_rules_is_the_same_reading(self):
        fields = {"temp_f": 50}
        self.assertIs(calibration.apply(fields, None), fields)

    def test_limits(self):
        rules, _ = calibration.parse("wind_dir_deg +30, humidity_pct +5, wind_mph -3")
        out = calibration.apply({"wind_dir_deg": 350, "humidity_pct": 98, "wind_mph": 1}, rules)
        self.assertEqual((out["wind_dir_deg"], out["humidity_pct"], out["wind_mph"]), (20, 100, 0))

    def test_what_is_worked_out_follows_the_correction(self):
        sent = {"temp_f": 40.0, "humidity_pct": 50, "wind_mph": 10, "dew_point_f": 99, "wind_chill_f": 99,
                "feels_like_f": 99, "battery": "normal"}
        rules, _ = calibration.parse("temp_f -10")
        out = calibration.apply(sent, rules)
        self.assertEqual(out["temp_f"], 30)
        self.assertLess(out["dew_point_f"], 30)
        self.assertLess(out["wind_chill_f"], 30)
        self.assertEqual(out["feels_like_f"], out["wind_chill_f"])
        self.assertEqual(out["battery"], "normal")
        self.assertEqual(sent["temp_f"], 40.0)          # what the sensor sent is untouched

    def test_a_message_without_the_temperature_keeps_what_came(self):
        rules, _ = calibration.parse("wind_mph x2")
        out = calibration.apply({"wind_mph": 5, "feels_like_f": 20}, rules)
        self.assertEqual(out, {"wind_mph": 10, "feels_like_f": 20})


class Imported(unittest.TestCase):
    def test_raw_logs_load_corrected(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / "hub-raw").mkdir()
            (home / "hub-raw" / "2026-10-20.log").write_text(
                "2026-10-20T12:00:00Z\tPOST /weatherstation/updateweatherstation?"
                "id=24C86E000000&sensor=00001111&mt=tower&tempf=50&humidity=40\n")
            cfg = config(device={"write_path": str(home)}, hub={"raw_dir": str(home / "hub-raw")}, storage={"database": str(home / "w.db")},
                         calibration={"00001111": "temp_f -1.5"})
            capture.import_raw(cfg)
            con = sqlite3.connect(home / "w.db")
            t, raw = con.execute("SELECT temp_f, query FROM readings").fetchone()
            con.close()
        self.assertEqual(t, 48.5)
        self.assertIn("tempf=50", raw)


class Alerts(unittest.TestCase):
    def snapshot(self):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"pressure_change_3h": -0.1, "sensors": [
            {"name": "Yard", "type": "Atlas", "sensor_id": "1", "updated": now,
             "fields": {"temp_f": 30, "feels_like_f": 110, "last_strike_mi": 5, "last_strike_heard": now}}]}

    def test_messages_follow_units(self):
        cfg = config(alerts={"weather": "frost, heat, lightning, storm"}, units={"system": "metric"})
        said = alerts.weather_conditions(cfg, self.snapshot(), datetime.now(timezone.utc).timestamp())
        self.assertEqual(said["frost"], "Frost risk: -1.1°C at Yard.")
        self.assertEqual(said["heat"], "Dangerous heat: feels like 43°C at Yard.")
        self.assertEqual(said["lightning"], "Lightning: a strike 8 km away, heard at Yard.")
        self.assertIn("fallen 3.4 hPa in 3 hours", said["storm"])
        self.assertEqual(alerts.WEATHER_OVER["lightning"].format(
            lightning_distance=units.show("distance", 10, units.chosen(cfg)), lightning_minutes=30),
            "No lightning within 16 km for 30 minutes.")


class Served(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        home = Path(self.dir.name)
        (home / "out").mkdir()
        (home / "out" / "current.json").write_text(json.dumps({"sensors": []}))
        cfg = config(device={"write_path": str(home / "out")}, storage={"database": str(home / "w.db")},
                     units={"system": "metric"})
        store.open_db(home / "w.db").close()
        handler = web._make_web_handler(home / "out", home / "w.db", {}, cfg=cfg)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.dir.cleanup()

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.httpd.server_port}{path}", timeout=10) as r:
            return r.read()

    def test_the_script_and_the_choice_reach_the_page(self):
        self.assertIn(b"const Units", self.get("/units.js"))
        self.assertIn("/units.js", web.OUTSIDE_PATHS)
        self.assertEqual(json.loads(self.get("/history.json?hours=1"))["units"], METRIC)

    def test_the_pages_load_it(self):
        for page in ("weather.html", "charts.html"):
            text = (HERE / page).read_text()
            self.assertLess(text.index('src="units.js"'), text.index("Units.use("), page)


class StatusShowsIt(unittest.TestCase):
    def test_the_sensor_carries_its_line(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / "out").mkdir()
            (home / "out" / "current.json").write_text(json.dumps({"sensors": [
                {"sensor_id": "00001111", "name": "Trailer", "type": "tower", "fields": {}}]}))
            cfg = config(device={"write_path": str(home / "out")}, storage={"database": str(home / "w.db")}, hub={"raw_dir": str(home / "hub-raw")},
                         calibration={"00001111": "temp_f -1.5"})
            store.open_db(home / "w.db").close()
            saved = status.CONFIG_DIR
            status.CONFIG_DIR = home
            try:
                s = status.system_status(cfg, home / "out")
            finally:
                status.CONFIG_DIR = saved
        self.assertEqual(s["sensors"][0]["calibration"], "temp_f -1.5")
        self.assertIn("calibrated:", (HERE / "status.html").read_text())


if __name__ == "__main__":
    unittest.main()
