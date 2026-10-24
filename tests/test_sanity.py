"""Checks for the sanity filters (sanity.py): readings no weather could produce
are held back, kept, and counted by nothing. python3 -m unittest tests/test_sanity.py

These need no network or radio; every file goes in a folder of its own that is
thrown away afterwards.
"""
import configparser
import importlib.util
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import sanity                    # noqa: E402
from sources import acurite_access   # noqa: E402

spec = importlib.util.spec_from_file_location("acurite_capture", HERE / "acurite-capture.py")
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)

T0 = 1_790_000_000.0


def config(**sections) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict(sections)
    return cfg


def iso(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Limits(unittest.TestCase):
    def test_200_mph_in_the_night_is_held_and_the_rest_believed(self):
        believed, held = sanity.Watch(config()).check("s", {"wind_mph": 200, "temp_f": 40, "battery": "normal"}, T0)
        self.assertEqual(believed, {"temp_f": 40, "battery": "normal"})
        self.assertEqual(held["wind_mph"]["value"], 200)
        self.assertIn("past anything a station reads", held["wind_mph"]["why"])

    def test_the_reason_is_in_the_units_shown(self):
        _, held = sanity.Watch(config(units={"system": "metric"})).check("s", {"wind_gust_mph": 200}, T0)
        self.assertIn("322 km/h", held["wind_gust_mph"]["why"])

    def test_what_is_worked_out_from_it_is_held_with_it(self):
        believed, held = sanity.Watch(config()).check(
            "s", {"temp_f": 190, "humidity_pct": 50, "dew_point_f": 150, "feels_like_f": 200}, T0)
        self.assertEqual(believed, {"humidity_pct": 50})
        self.assertEqual(held["dew_point_f"]["why"], "worked out from a reading held back")

    def test_turned_off_believes_everything(self):
        fields = {"wind_mph": 200}
        self.assertEqual(sanity.Watch(config(sanity={"enabled": "no"})).check("s", fields, T0), (fields, {}))


class Jumps(unittest.TestCase):
    def test_a_dying_sensor_is_held_until_it_comes_back(self):
        w = sanity.Watch(config())
        seen = [w.check("Trailer", {"temp_f": t}, T0 + i * 60)[1] for i, t in enumerate((50, 51, 120, 118, -30, 52))]
        self.assertEqual([bool(h) for h in seen], [False, False, True, True, True, False])
        self.assertIn("jumped from 51.0°F to 120.0°F in 1 min", seen[2]["temp_f"]["why"])
        self.assertIn("jumped from 51.0°F to 118.0°F in 2 min", seen[3]["temp_f"]["why"])   # still from the last believed

    def test_weather_given_time_gets_there(self):
        w = sanity.Watch(config())
        w.check("s", {"temp_f": 50}, T0)
        self.assertEqual(w.check("s", {"temp_f": 70}, T0 + 3 * 3600)[1], {})

    def test_rain_going_back_to_nothing_at_midnight_is_believed(self):
        w = sanity.Watch(config())
        w.check("s", {"rain_day_in": 1.2}, T0)
        self.assertEqual(w.check("s", {"rain_day_in": 0}, T0 + 60)[1], {})
        self.assertIn("rain_day_in", w.check("s", {"rain_day_in": 9}, T0 + 120)[1])

    def test_logged_once_when_held_and_once_when_believed_again(self):
        w = sanity.Watch(config())
        w.check("s", {"temp_f": 50}, T0)
        with self.assertLogs("acurite", "INFO") as said:
            for i, t in enumerate((120, 121, 122, 50)):
                w.check("s", {"temp_f": t}, T0 + (i + 1) * 60)
        self.assertEqual([r.levelname for r in said.records], ["WARNING", "INFO"])
        self.assertIn("after 3 reading(s)", said.records[1].getMessage())

    def test_a_restart_remembers_the_last_believed(self):
        asked = []
        w = sanity.Watch(config(), recall=lambda sid: asked.append(sid) or {"temp_f": (50.0, T0 - 60)})
        self.assertIn("temp_f", w.check("ABC1", {"temp_f": 120}, T0)[1])
        w.check("ABC1", {"temp_f": 50}, T0 + 60)
        self.assertEqual(asked, ["ABC1"])


class Reference(unittest.TestCase):
    def entries(self, *temps, at=T0):
        return [{"sensor_id": f"o{i}", "outdoor": True, "epoch": at, "fields": {"temp_f": t}} for i, t in enumerate(temps)]

    def test_two_others_make_a_reference(self):
        self.assertEqual(sanity.reference(self.entries(40, 44), "x", T0), 42)

    def test_one_other_cannot_say_which_is_wrong(self):
        self.assertIsNone(sanity.reference(self.entries(40), "x", T0))

    def test_its_own_stale_held_and_indoor_ones_are_left_out(self):
        e = self.entries(40, 44, 41)
        e[0]["sensor_id"] = "X"
        e[1]["epoch"] = T0 - 3600
        e[2]["suspect"] = {"temp_f": {}}
        e.append({"sensor_id": "in", "outdoor": False, "epoch": T0, "fields": {"temp_f": 70}})
        self.assertIsNone(sanity.reference(e, "x", T0))

    def test_far_from_the_others_is_held(self):
        w = sanity.Watch(config(sanity={"reference_f": "15"}))
        self.assertIn("while the other outdoor sensors read 42.0°F",
                      w.check("s", {"temp_f": 60}, T0, outdoor=True, reference=42)[1]["temp_f"]["why"])
        self.assertEqual(w.check("s", {"temp_f": 56}, T0 + 3600, outdoor=True, reference=42)[1], {})


class Stuck(unittest.TestCase):
    def test_an_outdoor_temperature_frozen_for_hours(self):
        w = sanity.Watch(config(sanity={"stuck_hours": "2"}))
        held = [w.check("s", {"temp_f": 33.1}, T0 + i * 1800, outdoor=True)[1] for i in range(6)]
        self.assertEqual([bool(h) for h in held], [False] * 4 + [True] * 2)
        self.assertIn("unchanged at 33.1°F for 2 h", held[4]["temp_f"]["why"])
        self.assertEqual(w.check("s", {"temp_f": 33.2}, T0 + 6 * 1800, outdoor=True)[1], {})

    def test_a_room_may_sit_still(self):
        w = sanity.Watch(config())
        self.assertEqual([w.check("s", {"temp_f": 70}, T0 + i * 3600)[1] for i in range(6)], [{}] * 6)


class Stored(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.home = Path(self.dir.name)
        self.db = self.home / "w.db"

    def tearDown(self):
        self.dir.cleanup()

    def test_a_database_from_before_gets_the_column(self):
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE readings (received_utc TEXT, sensor_id TEXT, temp_f REAL, query TEXT NOT NULL UNIQUE)")
        con.execute("INSERT INTO readings VALUES ('2026-10-24T00:00:00Z', 's', 50, 'q')")
        con.commit()
        con.close()
        capture.open_db(self.db).close()
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT temp_f, suspect FROM readings").fetchone(), (50, None))
        con.close()

    def test_held_back_is_kept_beside_and_counted_by_nothing(self):
        (self.home / "hub-raw").mkdir()
        lines = [f"{iso(T0 + i * 60)}\tGET /weatherstation/updateweatherstation?id=X&sensor=00001111&mt=Atlas"
                 f"&tempf=50&windspeedmph={mph}&q={i}" for i, mph in enumerate((5, 200, 6))]
        (self.home / "hub-raw" / "2026-10-24.log").write_text("\n".join(lines) + "\n")
        cfg = config(device={"write_path": str(self.home)}, hub={"raw_dir": str(self.home / "hub-raw")},
                     storage={"database": str(self.db)})
        capture.import_raw(cfg)
        con = sqlite3.connect(self.db)
        rows = con.execute("SELECT wind_mph, suspect FROM readings ORDER BY received_utc").fetchall()
        top = con.execute("SELECT max(wind_mph) FROM readings").fetchone()[0]
        con.close()
        self.assertEqual([r[0] for r in rows], [5, None, 6])
        self.assertEqual(json.loads(rows[1][1])["wind_mph"]["value"], 200)
        self.assertEqual(top, 6)

    def test_the_card_says_what_was_held_until_it_is_believed_again(self):
        def rec(i, fields, suspect=None):
            r = capture.Reading(source="acurite-access", sensor_id="sx", type="Atlas", received_utc=iso(T0 + i * 60),
                                raw=f"card{i}", fields=fields, suspect=suspect or {})
            return json.loads(json.dumps(capture.record_reading(r, self.home, {}, db_path=self.db)))
        rec(0, {"temp_f": 50, "wind_mph": 5})
        e = rec(1, {"temp_f": 50}, {"wind_mph": {"value": 200, "why": "too fast"}})
        self.assertEqual(e["fields"]["wind_mph"], 5)                        # the last believed
        self.assertEqual(e["suspect"]["wind_mph"]["since"], iso(T0 + 60))
        e = rec(2, {"temp_f": 50}, {"wind_mph": {"value": 190, "why": "too fast"}})
        self.assertEqual(e["suspect"]["wind_mph"]["since"], iso(T0 + 60))  # since it first was
        e = rec(3, {"temp_f": 50, "wind_mph": 6})
        self.assertNotIn("suspect", e)

    def store(self, rows):
        con = capture.open_db(self.db)
        for i, (sid, kind, at, fields, suspect) in enumerate(rows):
            capture.store_reading(con, capture.Reading(source="t", sensor_id=sid, type=kind, received_utc=iso(at),
                                                       raw=f"s{i}", fields=fields, suspect=suspect))
        con.commit()
        con.close()

    def test_the_charts_get_held_back_values_apart(self):
        now = time.time()
        self.store([("o", "Atlas", now - 600, {"temp_f": 50}, {}),
                    ("o", "Atlas", now - 300, {"temp_f": 50}, {"wind_gust_mph": {"value": 200, "why": "x"}}),
                    ("r", "tower", now - 300, {}, {"temp_f": {"value": 120, "why": "x"}})])
        h = capture.history(self.db, {}, {"period": "24h"})
        self.assertEqual([v for v in h["held"]["wind_gust_mph"] if v is not None], [200])
        self.assertTrue(all(v is None for v in h["outdoor"]["wind_gust_mph"]))
        room = next(r for r in h["rooms"] if r["id"] == "r")
        self.assertEqual([v for v in room["held"]["temp_f"] if v is not None], [120])

    def test_the_status_page_counts_a_days_worth(self):
        now = time.time()
        self.store([("o", "Atlas", now - 600, {"temp_f": 50}, {"temp_f": {"value": 1, "why": "x"}}),
                    ("o", "Atlas", now - 300, {}, {"temp_f": {"value": 1, "why": "x"}}),
                    ("o", "Atlas", now - 2 * 86400, {}, {"temp_f": {"value": 1, "why": "x"}}),
                    ("o", "Atlas", now - 60, {"temp_f": 50}, {})])
        self.assertEqual(capture._held_back(self.db, now), {"o": 2})
        page = (HERE / "status.html").read_text()
        self.assertIn("held_back_24h", page)

    def test_a_restart_reads_the_last_believed_back(self):
        now = time.time()
        self.store([("o", "Atlas", now - 120, {"temp_f": 50, "humidity_pct": 40}, {}),
                    ("o", "Atlas", now - 60, {"humidity_pct": 41}, {"temp_f": {"value": 120, "why": "x"}}),
                    ("o", "Atlas", now - 5 * 3600, {"pressure_inhg": 30}, {})])
        got = capture._believed_before(self.db, "o")
        self.assertEqual({k: v[0] for k, v in got.items()}, {"temp_f": 50, "humidity_pct": 41})


class PassedOn(unittest.TestCase):
    def test_without(self):
        self.assertEqual(acurite_access.without("id=X&tempf=120&dewptf=99&windspeedmph=5&mt=tower", {"temp_f", "dew_point_f"}),
                         "id=X&windspeedmph=5&mt=tower")

    def relayed(self, hold_back: str) -> str:
        sent = []
        relay = acurite_access._relay
        acurite_access._relay = lambda url, path, *a: sent.append(path) or (200, "application/json", b"{}")

        def emit(r):
            r.fields, r.suspect = sanity.Watch(config()).check(r.sensor_id, r.fields, T0)
            return {"type": r.type, "name": "n", "fields": r.fields}
        with tempfile.TemporaryDirectory() as d:
            cfg = config(hub={"raw_dir": d}, sanity={"hold_back_from_acurite": hold_back})
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), acurite_access._make_hub_handler(cfg, emit))
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}/weatherstation/updateweatherstation"
                                       "?id=X&sensor=1&mt=Atlas&tempf=50&windgustmph=200", timeout=10).read()
            finally:
                httpd.shutdown()
                httpd.server_close()
                acurite_access._relay = relay
        return sent[0]

    def test_held_back_is_left_out_when_asked(self):
        self.assertEqual(self.relayed("yes"), "/weatherstation/updateweatherstation?id=X&sensor=1&mt=Atlas&tempf=50")

    def test_otherwise_acurite_gets_what_the_hub_sent(self):
        self.assertIn("windgustmph=200", self.relayed("no"))


if __name__ == "__main__":
    unittest.main()
