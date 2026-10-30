"""Checks for 1.3's parts: weather alerts, records and normals, Zambretti, MCP and
--import. python3 -m unittest tests/test_1_3.py

These need no network. On 2026-10-21 the normals were fetched live by hand
(NOAA's station 1.2 mi from a Denver test point; Open-Meteo for London).
"""
import argparse
import configparser
import importlib.util
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from cc0ac import alerts       # noqa: E402
from cc0ac import climate      # noqa: E402
from cc0ac import importer     # noqa: E402
from cc0ac import mcp_server   # noqa: E402
from cc0ac import zambretti    # noqa: E402

from cc0ac import capture

NOW = datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc).timestamp()


def cfg(**sections) -> configparser.ConfigParser:
    c = configparser.ConfigParser()
    for name, keys in sections.items():
        c[name] = keys
    return c


def sensor(sid, temp=None, feels=None, type="Atlas", minutes_ago=2):
    when = datetime.fromtimestamp(NOW - minutes_ago * 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fields = {k: v for k, v in (("temp_f", temp), ("feels_like_f", feels)) if v is not None}
    return {"sensor_id": sid, "name": f"S{sid}", "type": type, "updated": when, "fields": fields}


class WeatherAlerts(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.state = self.root / "state.json"
        self.sent = []
        self._send = alerts.send
        alerts.send = lambda c, title, message: self.sent.append((title, message)) or []
        self.c = cfg(alerts={"enabled": "true", "weather": "frost, heat, storm"}, station={"name": "Home"})

    def tearDown(self):
        alerts.send = self._send
        self.dir.cleanup()

    def look(self, *sensors, change=None):
        (self.root / "current.json").write_text(json.dumps({"sensors": list(sensors), "pressure_change_3h": change}))
        return alerts.check_weather(self.c, self.root, NOW, self.state)

    def test_frost_once_then_over_past_the_margin(self):
        self.assertEqual(self.look(sensor("1", 34.0)), "weather: frost alerted")
        self.assertEqual(self.sent[-1], ("Home: frost risk", "Frost risk: 34.0°F at S1."))
        self.assertEqual(self.look(sensor("1", 33.0)), "weather: nothing new")
        self.assertEqual(self.look(sensor("1", 36.5)), "weather: nothing new")   # within 2° of 35: still on
        self.assertEqual(self.look(sensor("1", 37.5)), "weather: frost over")
        self.assertEqual(self.sent[-1], ("Home: frost over", "The frost risk is over."))
        self.assertEqual(len(self.sent), 2)

    def test_heat_and_storm(self):
        self.assertEqual(self.look(sensor("1", 100, feels=108), change=-0.07),
                         "weather: heat alerted, storm alerted")
        self.assertIn("feels like 108°F", self.sent[0][1])
        self.assertIn("fallen 0.07 inHg", self.sent[1][1])
        self.assertEqual(self.look(sensor("1", 95, feels=103), change=-0.04), "weather: nothing new")
        self.assertEqual(self.look(sensor("1", 90, feels=100), change=0.0), "weather: heat over, storm over")

    def test_station_sensor_beats_internet_and_stale_is_ignored(self):
        self.look(sensor("open-meteo", 20.0, type="outdoor"), sensor("1", 50.0))
        self.assertEqual(self.sent, [])
        self.look(sensor("open-meteo", 20.0, type="outdoor"), sensor("1", 30.0, minutes_ago=120))
        self.assertEqual(self.sent[-1][1], "Frost risk: 20.0°F at Sopen-meteo.")

    def test_off_unless_asked(self):
        self.c["alerts"]["weather"] = ""
        self.assertEqual(self.look(sensor("1", 10.0)), "weather alerts off")


def day(d, **kw):
    return {"day": d, **kw}


class Records(unittest.TestCase):
    DAYS = [day("2025-07-04", temp_max=99.0, temp_min=60.0, rain_in=0.0, wind_gust_max=30.0),
            day("2026-01-10", temp_max=20.0, temp_min=-5.0, rain_in=None),
            day("2026-10-02", temp_max=80.0, temp_min=40.0, rain_in=1.25),
            day("2026-10-05", temp_max=80.0, temp_min=45.0, rain_in=0.5),
            day("2026-10-21", temp_max=70.0, temp_min=38.0, rain_in=0.0)]

    def test_periods_and_first_day_wins_a_tie(self):
        r = climate.records(self.DAYS, "2026-10-21")
        self.assertEqual(r["today"]["hottest"], {"value": 70.0, "day": "2026-10-21"})
        self.assertEqual(r["month"]["hottest"], {"value": 80.0, "day": "2026-10-02"})
        self.assertEqual(r["month"]["coldest"]["value"], 38.0)
        self.assertEqual(r["year"]["coldest"], {"value": -5.0, "day": "2026-01-10"})
        self.assertEqual(r["all"]["hottest"]["day"], "2025-07-04")
        self.assertEqual(r["all"]["wettest_day"]["value"], 1.25)
        self.assertNotIn("highest_gust", r["year"])                 # missing stays missing
        self.assertEqual(climate.month_rain(self.DAYS, "2026-10-21"), 1.75)

    def test_build_with_normals(self):
        kept = {"source": "NOAA", "station": "X", "miles": 3, "days": {"10-21": {"high": 66.0, "low": 36.0, "rain_mtd": 0.6}}}
        doc = climate.build([{"id": "1", "name": "Yard", "days": self.DAYS}], "2026-10-21", kept)
        self.assertEqual((doc["normal"]["high"], doc["normal"]["station"]), (66.0, "X"))
        self.assertEqual((doc["sensors"][0]["high"], doc["sensors"][0]["since"]), (70.0, "2025-07-04"))
        self.assertEqual(climate.normal_for(kept, "2028-02-29"), None)   # no 02-28 either here

    def test_nearest_noaa_station_with_daily_normals(self):
        search = {"results": [
            {"location": {"coordinates": [-105.0, 40.0]}, "stations": [{"id": "FAR", "name": "Far", "dataTypes": [{"id": "DLY-TMAX-NORMAL"}]}]},
            {"location": {"coordinates": [-105.24, 40.01]}, "stations": [{"id": "NEAR-NO", "dataTypes": [{"id": "MLY-TAVG-NORMAL"}]}]},
            {"location": {"coordinates": [-105.2, 40.05]}, "stations": [{"id": "NEAR", "name": "Near", "dataTypes": [{"id": "DLY-TMAX-NORMAL"}]}]},
            {"location": {}}]}
        sid, name, mi = climate.nearest_noaa(40.0, -105.25, search)
        self.assertEqual((sid, name), ("NEAR", "Near"))
        self.assertLess(mi, 5)
        self.assertIsNone(climate.nearest_noaa(10.0, 10.0, search))

    def test_noaa_rows_and_computed_averages(self):
        days = climate.noaa_days([{"DATE": "01-01", "DLY-TMAX-NORMAL": "47.8", "DLY-TMIN-NORMAL": "-9999"}])
        self.assertEqual(days["01-01"], {"high": 47.8, "low": None, "rain_mtd": None})
        daily = {"time": ["1991-03-01", "1991-03-02", "1992-03-01", "1992-03-02"],
                 "temperature_2m_max": [50, 52, 54, None], "temperature_2m_min": [30, 31, 32, 33],
                 "precipitation_sum": [0.1, 0.2, 0.3, None]}
        out = climate.computed_days(daily)
        self.assertEqual(out["03-01"], {"high": 52.0, "low": 31.0, "rain_mtd": 0.2})
        self.assertEqual(out["03-02"], {"high": 52.0, "low": 32.0, "rain_mtd": 0.3})

    def test_kept_normals_are_reused_and_an_old_copy_beats_none(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "normals.json"
            p.write_text(json.dumps({"lat": 1.0, "lon": 2.0, "fetched": NOW - 86400, "days": {}, "source": "s",
                                     "station": None, "miles": 0}))
            fetch = climate.fetch_normals
            climate.fetch_normals = lambda lat, lon: (_ for _ in ()).throw(OSError("offline"))
            try:
                self.assertEqual(climate.normals(1.0, 2.0, p, NOW)["source"], "s")
                self.assertEqual(climate.normals(1.0, 2.0, p, NOW + 400 * 86400)["source"], "s")
            finally:
                climate.fetch_normals = fetch


class Zambretti(unittest.TestCase):
    def test_outlooks(self):
        self.assertIsNone(zambretti.outlook(30.0, None, 6))
        self.assertEqual(zambretti.outlook(30.6, 0.0, 1), "Settled fine")              # high and steady
        self.assertEqual(zambretti.outlook(29.0, -0.1, 1), "Stormy, much rain")       # low and falling
        rising = zambretti.outlook(29.9, 0.08, 1)
        falling = zambretti.outlook(29.9, -0.08, 1)
        self.assertLess(zambretti.OUTLOOKS.index(rising), zambretti.OUTLOOKS.index(falling))
        # A north wind is fairer than a south one; south of the equator it's the other way round.
        north = zambretti.OUTLOOKS.index(zambretti.outlook(29.9, -0.08, 1, 0))
        south = zambretti.OUTLOOKS.index(zambretti.outlook(29.9, -0.08, 1, 180))
        self.assertLess(north, south)
        self.assertEqual(zambretti.outlook(29.9, -0.08, 1, 0), zambretti.outlook(29.9, -0.08, 7, 180, north=False))
        self.assertTrue(zambretti.outlook(31.5, 0.0, 1).startswith("Exceptional weather"))


class Mcp(unittest.TestCase):
    def setUp(self):
        files = {"current.json": {"written": "w", "pressure_change_3h": -0.02, "zambretti": "Fine weather",
                                  "sensors": [{"sensor_id": "1", "name": "Yard", "type": "Atlas", "updated": "u",
                                               "fields": {"temp_f": 50.0}},
                                              {"sensor_id": "2", "name": "Kitchen", "type": "tower", "fields": {}}]},
                 "forecast.json": {"source": "NWS", "hourly": {"periods": list(range(48))}}}
        hist = {"period": "today", "t": [100, 400], "step": 300,
                "outdoor": {"temp_f": [50.0, None], "humidity_pct": [40, None]}, "rooms": []}
        summ = {"today": "2026-10-21", "sensors": [{"id": "1", "name": "Yard", "outdoor": True,
                                                    "days": [{"day": "2026-10-21"}], "weeks": [], "months": [], "years": []}]}
        self.calls = []

        def history(q):
            self.calls.append(q)
            return hist
        self.server = mcp_server.Server(mcp_server.Station(files.get, history, lambda q: summ), "1.3.0/1")

    def rpc(self, method, params=None, mid=1):
        msg = {"jsonrpc": "2.0", "method": method, **({"id": mid} if mid is not None else {}),
               **({"params": params} if params is not None else {})}
        return self.server.handle(json.dumps(msg).encode())

    def call(self, name, **args):
        code, reply = self.rpc("tools/call", {"name": name, "arguments": args})
        self.assertEqual(code, 200)
        return reply["result"]

    def test_handshake_and_tool_list(self):
        code, r = self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                          "clientInfo": {"name": "t", "version": "0"}})
        self.assertEqual((code, r["result"]["protocolVersion"]), (200, "2025-03-26"))
        self.assertEqual(r["result"]["serverInfo"]["version"], "1.3.0/1")
        self.assertEqual(self.rpc("initialize", {"protocolVersion": "1999-01-01"})[1]["result"]["protocolVersion"],
                         mcp_server.PROTOCOLS[0])
        self.assertEqual(self.rpc("notifications/initialized", mid=None), (202, None))
        tools = self.rpc("tools/list")[1]["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, set(mcp_server.TOOLS))
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in tools))
        self.assertEqual(self.rpc("nope")[1]["error"]["code"], -32601)
        self.assertEqual(self.server.handle(b"{")[0], 400)
        self.assertEqual(self.server.handle(b"[]")[0], 400)

    def test_tools(self):
        cur = self.call("current_conditions")["structuredContent"]
        self.assertEqual((cur["outlook"], cur["sensors"][0]["outdoor"], cur["sensors"][1]["outdoor"]),
                         ("Fine weather", True, False))
        self.assertEqual(len(self.call("forecast")["structuredContent"]["hourly"]["periods"]), 24)
        h = self.call("history", start="2026-10-01", fields=["temp_f"])["structuredContent"]
        self.assertEqual(self.calls[-1], {"from": "2026-10-01", "to": "2026-10-01"})
        self.assertEqual(h["outdoor"], [{"time": 100, "temp_f": 50.0}])
        s = self.call("daily_summary", sensor="yard", by="days")["structuredContent"]
        self.assertEqual(s["sensors"][0]["days"], [{"day": "2026-10-21"}])

    def test_tool_errors_are_answers(self):
        self.assertTrue(self.call("records_and_normals")["isError"])          # no climate.json
        self.assertTrue(self.call("history", bogus=1)["isError"])
        self.assertTrue(self.call("daily_summary", sensor="attic")["isError"])
        self.assertTrue(self.call("rm_rf")["isError"])


class McpOverHttp(unittest.TestCase):
    """/mcp in the real web server: answers here, refuses another site's page."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        (root / "current.json").write_text(json.dumps({"written": "w", "sensors": []}))
        handler = capture._make_web_handler(root, root / "none.db", {})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/mcp"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.dir.cleanup()

    def post(self, body, **headers):
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Accept": "application/json, text/event-stream", **headers})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""

    def test_post_get_and_origin(self):
        code, body = self.post({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                "params": {"name": "current_conditions", "arguments": {}}})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["result"]["structuredContent"]["written"], "w")
        self.assertEqual(self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})[0], 202)
        self.assertEqual(self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                   Origin="https://evil.example")[0], 403)
        self.assertEqual(self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, Origin="http://localhost:3000")[0], 200)
        try:
            urllib.request.urlopen(self.url, timeout=10)
            self.fail("GET should be refused")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 405)


class Import(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_clean_drops_the_impossible_and_keeps_missing_missing(self):
        self.assertEqual(importer.clean({"temp_f": -9999, "humidity_pct": 140, "wind_mph": None,
                                         "pressure_inhg": "30.1", "rain_day_in": 0}),
                         {"pressure_inhg": 30.1, "rain_day_in": 0.0})

    def weewx(self, units, rows):
        p = self.root / "weewx.sdb"
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE archive (dateTime INTEGER, usUnits INTEGER, outTemp REAL, outHumidity REAL, "
                    "windSpeed REAL, windGust REAL, windDir REAL, barometer REAL, rain REAL)")
        con.executemany("INSERT INTO archive VALUES (?,?,?,?,?,?,?,?,?)", [(t, units, *r) for t, r in rows])
        con.commit()
        con.close()
        return p

    def test_weewx_metric_and_rain_running_totals(self):
        t0 = int(datetime(2024, 5, 1, 10, 0).timestamp())   # local times, as WeeWX's day boundaries are
        p = self.weewx(17, [(t0, (20.0, 50, 10.0, 15.0, 90, 1013.25, 2.54)),
                            (t0 + 1800, (21.0, 49, 0.0, None, None, 1013.25, 2.54)),
                            (t0 + 5400, (None, None, None, None, None, None, 0.0)),
                            (t0 + 86400, (10.0, 60, 1.0, 2.0, 180, 1000.0, 0.0))])
        rows = list(importer.rows_weewx(p))
        f = rows[0]["fields"]
        self.assertEqual(round(f["temp_f"], 1), 68.0)
        self.assertEqual(round(f["wind_mph"], 2), 22.37)                 # 10 m/s
        self.assertEqual(round(f["pressure_inhg"], 2), 29.92)
        self.assertEqual(round(rows[1]["fields"]["rain_day_in"], 2), 0.2)
        self.assertEqual(round(rows[1]["fields"]["rain_hour_in"], 2), 0.2)
        self.assertEqual(rows[2]["fields"]["rain_hour_in"], 0.0)   # a record's rain fell before its time
        self.assertIsNone(rows[2]["fields"]["temp_f"])
        self.assertEqual(rows[3]["fields"]["rain_day_in"], 0.0)            # a new day starts at zero

    def test_generic_csv_with_units_in_cells(self):
        p = self.root / "wu.csv"
        p.write_text("Date,Time,Temperature,Humidity,Wind,Speed,Gust,Pressure,Precip. Accum.\n"
                     "x,2024-05-01 10:00,70.2 °F,59 %,SSW,8 mph,11 mph,30.01 in,0.12 in\n"
                     "x,bad time,1,1,N,1,1,1,1\n")
        rows = list(importer.rows_csv(p))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fields"], {"temp_f": 70.2, "humidity_pct": 59.0, "wind_dir_deg": 202.5,
                                             "wind_mph": 8.0, "wind_gust_mph": 11.0, "pressure_inhg": 30.01,
                                             "rain_day_in": 0.12})

    def test_import_keeps_only_what_came_before_and_never_twice(self):
        db = self.root / "weather.db"
        con = capture.open_db(db)
        capture.store_reading(con, capture.Reading(source="acurite-access", sensor_id="00001234", type="Atlas",
                                                   received_utc="2024-05-02T00:00:00Z", raw="live-1",
                                                   fields={"temp_f": 60.0}))
        con.close()
        p = self.root / "old.csv"
        p.write_text("timeUTC,temperature,humidity\n2024-05-01T12:00:00Z,50,40\n"
                     "2024-05-01T13:00:00Z,51,41\n2024-05-02T06:00:00Z,99,10\n")
        c = cfg(device={"write_path": str(self.root)}, storage={"database": str(db)})
        args = argparse.Namespace(import_from=str(p), sensor=None, metric=False, wu_station=None,
                                  wu_api_key=None, date_from=None, date_to=None)
        capture.import_history(c, args)
        capture.import_history(c, args)
        con = sqlite3.connect(db)
        rows = con.execute("SELECT sensor_id, type, temp_f, dew_point_f FROM readings "
                           "WHERE query LIKE 'import:%' ORDER BY received_utc").fetchall()
        self.assertEqual([r[:3] for r in rows], [("00001234", "Atlas", 50.0), ("00001234", "Atlas", 51.0)])
        self.assertIsNotNone(rows[0][3])                                   # derived, as live readings are
        self.assertGreaterEqual(con.execute("SELECT count(*) FROM daily").fetchone()[0], 1)

    def test_wu_fields(self):
        f = importer.wu_fields({"humidityAvg": 50, "winddirAvg": 270, "uvHigh": 3,
                                "imperial": {"tempAvg": 61.2, "pressureMin": 30.0, "pressureMax": 30.1,
                                             "precipTotal": 0.05, "windgustHigh": 12}})
        self.assertEqual((f["temp_f"], round(f["pressure_inhg"], 2), f["rain_day_in"], f["wind_mph"]),
                         (61.2, 30.05, 0.05, None))



class PressureSeed(unittest.TestCase):
    def test_a_long_tail_still_reaches_three_hours_back(self):
        # Many sensors: far more than 256 KB written in the last 3 hours.
        with tempfile.TemporaryDirectory() as d:
            hist = Path(d) / "history.csv"
            now = NOW
            with hist.open("w", encoding="utf-8") as f:
                f.write(",".join(capture.HISTORY_FIELDS) + "\n")
                at = {k: i for i, k in enumerate(capture.HISTORY_FIELDS)}
                for n in range(4 * 3600):              # a row a second for 4 hours
                    t = now - 4 * 3600 + n
                    row = [""] * len(capture.HISTORY_FIELDS)
                    row[at["timestamp"]] = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    row[at["sensor_id"]], row[at["type"]] = "00001234", "tower"
                    row[at["pressure_inhg"]] = "30.10" if t < now - 2.5 * 3600 else "30.00"
                    f.write(",".join(row) + "\n")
            self.assertGreater(hist.stat().st_size, 512 * 1024)
            capture._pressure.clear()
            capture._seed_pressure(hist, now)
            self.assertEqual(capture._pressure_change(now, 30.0), -0.1)

if __name__ == "__main__":
    unittest.main()
