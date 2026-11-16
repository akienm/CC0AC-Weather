"""Air quality and pollen history: python3 -m unittest tests/test_air_history.py

What air.py keeps for the charts (air_hours, pollen_days), how far back it fetches,
and what /history.json hands the charts. On answers shaped like the live ones,
measured 2026-11-15 for Silver City, NM: Open-Meteo answered past_days=92 with
2232 hours and no gaps, and pollen.com's historic page 29 days of index, no plants.
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cc0ac import air, store   # noqa: E402

NOW = datetime(2026, 11, 15, 19, 30, tzinfo=timezone.utc).timestamp()


def hourly(times, **series):
    return {"hourly": {"time": times, **series}}


def epoch(iso):
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp())


class Parsing(unittest.TestCase):
    def test_hours_still_to_come_are_left_out(self):
        rows = air.parse_history(hourly(["2026-11-15T18:00", "2026-11-15T19:00", "2026-11-15T20:00"],
                                        us_aqi=[40, 43, 45], pm2_5=[2.0, 2.2, 2.5]), NOW)
        self.assertEqual([r["hour_utc"] for r in rows], [epoch("2026-11-15T18:00"), epoch("2026-11-15T19:00")])
        self.assertEqual(rows[1]["aqi"], 43)
        self.assertEqual(rows[1]["pm2_5"], 2.2)
        self.assertIsNone(rows[1]["dust"])   # a series not in the answer is empty, not an error

    def test_an_hour_with_nothing_in_it_is_not_a_row(self):
        rows = air.parse_history(hourly(["2026-11-15T18:00"], us_aqi=[None], pm2_5=[None]), NOW)
        self.assertEqual(rows, [])

    def test_the_request_asks_for_every_stored_series_in_gmt(self):
        u = air.history_url(32.789, -108.271, 500)
        self.assertIn("timezone=GMT", u)
        self.assertIn("past_days=92", u)        # capped at what Open-Meteo answers
        for k in air.HISTORY:
            self.assertIn(k, u)

    def test_pollen_com_history_is_a_day_each_without_plants(self):
        doc = {"Location": {"periods": [{"Period": "2026-10-18T05:30:02", "Index": 4.6},
                                        {"Period": "2026-10-19T05:30:02", "Index": None},
                                        {"Period": "2026-10-20T05:30:02", "Index": 3.1}]}}
        self.assertEqual(air.parse_pollen_com_history(doc),
                         [{"day": "2026-10-18", "index": 4.6, "types": []},
                          {"day": "2026-10-20", "index": 3.1, "types": []}])


class HowFarBack(unittest.TestCase):
    def test_a_first_start_backfills_everything(self):
        self.assertEqual(air.days_to_fetch(None, NOW), air.BACKFILL_DAYS)

    def test_an_hourly_round_asks_for_a_day(self):
        self.assertEqual(air.days_to_fetch(int(NOW - 3600), NOW), 1)

    def test_after_time_off_it_reaches_back_to_the_last_hour(self):
        self.assertEqual(air.days_to_fetch(int(NOW - 10.5 * 86400), NOW), 11)
        self.assertEqual(air.days_to_fetch(int(NOW - 400 * 86400), NOW), air.BACKFILL_DAYS)


class Storing(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        store.open_air(self.con)

    def tearDown(self):
        self.con.close()

    def test_tables_beside_readings_and_opening_twice_is_harmless(self):
        store.open_air(self.con)
        names = {r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(names, {"air_hours", "pollen_days"})

    def test_a_later_answer_for_an_hour_replaces_the_earlier(self):
        h = epoch("2026-11-15T18:00")
        store.store_air_hours(self.con, [{"hour_utc": h, "aqi": 40}])
        store.store_air_hours(self.con, [{"hour_utc": h, "aqi": 41}, {"hour_utc": h + 3600, "aqi": 42}])
        self.assertEqual(self.con.execute("SELECT aqi FROM air_hours ORDER BY hour_utc").fetchall(), [(41,), (42,)])
        self.assertEqual(store.last_air_hour(self.con), h + 3600)

    def test_a_backfill_never_overwrites_a_day_that_named_its_plants(self):
        store.store_pollen_days(self.con, "pollen.com", 12, [{"day": "2026-11-15", "index": 3.6,
                                                              "types": [{"name": "Ragweed"}, {"name": "Juniper"}]}])
        store.store_pollen_days(self.con, "pollen.com", 12, [{"day": "2026-11-14", "index": 3.0, "types": []},
                                                             {"day": "2026-11-15", "index": 9.9, "types": []}],
                                replace=False)
        rows = self.con.execute("SELECT day, idx, types FROM pollen_days ORDER BY day").fetchall()
        self.assertEqual(rows[0], ("2026-11-14", 3.0, "[]"))
        self.assertEqual(rows[1][1], 3.6)
        self.assertEqual([t["name"] for t in json.loads(rows[1][2])], ["Ragweed", "Juniper"])

    def test_a_newer_forecast_for_a_day_replaces_the_older(self):
        store.store_pollen_days(self.con, "pollen.com", 12, [{"day": "2026-11-16", "index": 3.0, "types": []}])
        store.store_pollen_days(self.con, "pollen.com", 12, [{"day": "2026-11-16", "index": 4.0,
                                                              "types": [{"name": "Juniper"}]}])
        self.assertEqual(self.con.execute("SELECT idx FROM pollen_days").fetchall(), [(4.0,)])


class Stop(Exception):
    pass


class Running(unittest.TestCase):
    """run() with a database: the first round backfills, the next asks for a day."""

    def test_backfill_then_hourly_and_pollen_kept(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            db = path / "weather.db"
            asked = []

            def history(lat, lon, contact, past, now):
                asked.append(past)
                return [{"hour_utc": int(now // 3600 * 3600) - 3600 * i, "aqi": 30 + i} for i in range(3)]
            forecast = {"source": "pollen.com", "scale": 12,
                        "days": [{"day": "2026-11-15", "index": 3.6, "types": [{"name": "Ragweed"}]}]}
            passes = []

            def after_pass(_wait):
                passes.append(1)
                if len(passes) == 2:
                    raise Stop
            with mock.patch.object(air, "fetch", return_value=air.parse({"current": {"us_aqi": 30}})), \
                 mock.patch.object(air, "fetch_history", side_effect=history), \
                 mock.patch.object(air, "fetch_pollen_com", return_value=forecast), \
                 mock.patch.object(air, "fetch_pollen_com_history",
                                   return_value=[{"day": "2026-11-01", "index": 5.0, "types": []}]) as backfill, \
                 mock.patch.object(air.time, "sleep", side_effect=after_pass), \
                 mock.patch.object(air.iolog, "report"):
                with self.assertRaises(Stop):
                    air.run(32.77, -108.28, "test", path, zip_code="88061", db_path=db)
            self.assertEqual(asked, [air.BACKFILL_DAYS, 1])
            self.assertEqual(backfill.call_count, 1)        # once a day, not every hour
            con = sqlite3.connect(db)
            self.assertEqual(con.execute("SELECT count(*) FROM air_hours").fetchone()[0], 3)
            self.assertEqual(con.execute("SELECT day FROM pollen_days ORDER BY day").fetchall(),
                             [("2026-11-01",), ("2026-11-15",)])
            con.close()

    def test_a_history_failure_never_stops_air_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            with mock.patch.object(air, "fetch", return_value=air.parse({"current": {"us_aqi": 30}})), \
                 mock.patch.object(air, "fetch_history", side_effect=OSError("timed out")), \
                 mock.patch.object(air.time, "sleep", side_effect=Stop), \
                 mock.patch.object(air.iolog, "report") as logged:
                with self.assertRaises(Stop):
                    air.run(32.77, -108.28, "test", path, db_path=path / "weather.db")
            self.assertEqual(json.loads((path / "air.json").read_text())["aqi"], 30)
            self.assertIn("air history: timed out", str(logged.call_args_list))

    def test_without_a_database_nothing_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            with mock.patch.object(air, "fetch", return_value=air.parse({"current": {"us_aqi": 30}})), \
                 mock.patch.object(air, "fetch_history") as history, \
                 mock.patch.object(air.time, "sleep", side_effect=Stop), \
                 mock.patch.object(air.iolog, "report"):
                with self.assertRaises(Stop):
                    air.run(32.77, -108.28, "test", path)
            history.assert_not_called()
            self.assertFalse((path / "weather.db").exists())


class Charts(unittest.TestCase):
    """What /history.json carries for the air and pollen charts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "weather.db"
        con = store.open_db(self.db)
        start = datetime(2026, 11, 5, 12, 0).astimezone().timestamp()
        con.execute("INSERT INTO readings (received_utc, sensor_id, type, query, temp_f) VALUES (?, 'a', 'Atlas', 'q', 50)",
                    [store.epoch_to_iso(start)])
        con.commit()
        self.start = start
        self.con = con

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_a_database_from_before_the_air_tables_still_charts(self):
        h = store.history(self.db, {}, {"from": "2026-11-05", "to": "2026-11-06"})
        self.assertTrue(all(v is None for v in h["air"]["aqi"]))
        self.assertEqual(len(h["air"]["aqi"]), len(h["t"]))
        self.assertEqual(h["pollen"], [])

    def test_the_hours_land_in_the_charts_buckets_and_the_days_come_with_them(self):
        store.open_air(self.con)
        hour = int(self.start // 3600 * 3600)
        store.store_air_hours(self.con, [{"hour_utc": hour + 3600 * i, "aqi": 20 + i, "pm2_5": 1.0 + i}
                                         for i in range(6)])
        store.store_pollen_days(self.con, "pollen.com", 12, [
            {"day": "2026-11-05", "index": 3.6, "types": [{"name": "Juniper"}]},
            {"day": "2026-11-20", "index": 1.0, "types": []}])
        h = store.history(self.db, {}, {"from": "2026-11-05", "to": "2026-11-06"})
        got = [v for v in h["air"]["aqi"] if v is not None]
        self.assertTrue(got)
        self.assertEqual(max(v for v in h["air"]["aqi_max"] if v is not None), 25)
        self.assertLessEqual(len(got), 6)
        self.assertEqual([p["day"] for p in h["pollen"]], ["2026-11-05"])   # the 20th is outside the range
        self.assertEqual(h["pollen"][0]["types"], [{"name": "Juniper"}])
        self.assertEqual(h["pollen"][0]["scale"], 12)

    def test_all_time_reaches_back_to_the_first_air_hour(self):
        store.open_air(self.con)
        early = int(self.start) - 40 * 86400       # backfilled from before the station's first reading
        store.store_air_hours(self.con, [{"hour_utc": early // 3600 * 3600, "aqi": 80}])
        h = store.history(self.db, {}, {"period": "all"})
        self.assertLessEqual(h["from"], early)
        self.assertEqual(max(v for v in h["air"]["aqi"] if v is not None), 80)


if __name__ == "__main__":
    unittest.main()
