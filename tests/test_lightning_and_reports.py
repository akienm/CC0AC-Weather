"""1.4.4: chart ranges to the minute, the lightning chart's strikes, far strikes left out
([lightning] ignore_beyond_mi), and the day / week / month reports ([reports]).
python3 -m unittest tests/test_lightning_and_reports.py"""
import configparser
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from cc0ac import reports, store, web   # noqa: E402
from cc0ac.recorder import Recorder   # noqa: E402


def local(y, mo, d, h=12, mi=0):
    """A local time as the store keeps it: UTC, e.g. 2026-11-06T20:00:00Z."""
    return store.epoch_to_iso(datetime(y, mo, d, h, mi).timestamp())


class Station:
    """A database of made-up readings in a temporary folder."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "weather.db"
        self.con = store.open_db(self.db)
        self.n = 0

    def add(self, at, sid="atlas1", kind="Atlas", **fields):
        self.n += 1
        row = {"received_utc": at, "sensor_id": sid, "type": kind, "query": f"q{self.n}", **fields}
        self.con.execute(f"INSERT INTO readings ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                         list(row.values()))
        self.con.commit()

    def cfg(self, **sections):
        cfg = configparser.ConfigParser()
        cfg.read_dict({"device": {"write_path": str(self.root)}, "station": {"name": "Test Station"}, **sections})
        return cfg

    def close(self):
        self.con.close()
        self.tmp.cleanup()


class RangeToTheMinute(unittest.TestCase):
    def setUp(self):
        self.st = Station()
        self.st.add(local(2026, 11, 1, 0, 30), temp_f=40.0)

    def tearDown(self):
        self.st.close()

    def bounds(self, **q):
        h = store.history(self.st.db, {}, q)
        return h["from"], h["to"]

    def test_a_date_and_time_is_that_minute(self):
        lo, hi = self.bounds(**{"from": "2026-11-05T08:15", "to": "2026-11-05T17:30"})
        self.assertEqual(lo, datetime(2026, 11, 5, 8, 15).timestamp())
        self.assertEqual(hi, datetime(2026, 11, 5, 17, 30).timestamp())

    def test_a_date_alone_is_the_whole_day(self):
        lo, hi = self.bounds(**{"from": "2026-11-05", "to": "2026-11-05"})
        self.assertEqual(lo, datetime(2026, 11, 5).timestamp())
        self.assertEqual(hi, datetime(2026, 11, 6).timestamp())

    def test_seconds_from_a_browser_are_taken(self):
        lo, _ = self.bounds(**{"from": "2026-11-05T08:15:00", "to": "2026-11-05T09:00"})
        self.assertEqual(lo, datetime(2026, 11, 5, 8, 15).timestamp())

    def test_nonsense_and_backwards_are_refused(self):
        with self.assertRaises(ValueError):
            self.bounds(**{"from": "yesterday"})
        with self.assertRaises(ValueError):
            self.bounds(**{"from": "2026-11-05T10:00", "to": "2026-11-05T09:00"})

    def test_a_short_range_gets_fine_buckets(self):
        h = store.history(self.st.db, {}, {"from": "2026-11-05T08:00", "to": "2026-11-05T10:00"})
        self.assertEqual(h["step"], 300)


class Strikes(unittest.TestCase):
    """The lightning chart's dots, from the sensor's running count."""

    def setUp(self):
        self.st = Station()
        s = self.st
        s.add(local(2026, 11, 6, 14, 0), strike_count=10, last_strike_ts="a", last_strike_mi=12)
        s.add(local(2026, 11, 6, 14, 5), strike_count=10, last_strike_ts="a", last_strike_mi=12)   # nothing new
        s.add(local(2026, 11, 6, 14, 10), strike_count=13, last_strike_ts="b", last_strike_mi=8)   # 3 strikes
        s.add(local(2026, 11, 6, 14, 15), strike_count=13, last_strike_ts="c", last_strike_mi=21)  # a new time: 1
        s.add(local(2026, 11, 6, 14, 20), strike_count=2, last_strike_ts="d", last_strike_mi=5)    # restarted: 2

    def tearDown(self):
        self.st.close()

    def strikes(self, beyond=None, lo=(2026, 11, 6, 14, 1)):
        return store.strikes(self.st.con, datetime(*lo).timestamp(), datetime(2026, 11, 7).timestamp(), beyond)

    def test_each_rise_is_a_dot_with_its_distance(self):
        got = self.strikes()
        self.assertEqual(got["n"], [3, 1, 2])
        self.assertEqual(got["mi"], [8, 21, 5])
        self.assertEqual(got["sensors"], 1)

    def test_the_reading_before_the_period_is_read_so_the_first_counts(self):
        got = self.strikes(lo=(2026, 11, 6, 14, 6))   # starts after the 14:05 reading
        self.assertEqual(got["n"], [3, 1, 2])

    def test_strikes_past_the_limit_are_left_out(self):
        got = self.strikes(beyond=20)
        self.assertEqual(got["mi"], [8, 5])
        self.assertEqual(got["n"], [3, 2])

    def test_history_carries_them_and_the_limit(self):
        h = store.history(self.st.db, {}, {"from": "2026-11-06T14:01", "to": "2026-11-06T15:00"}, 20)
        self.assertEqual(h["strikes"]["n"], [3, 2])
        self.assertEqual(h["strikes"]["ignore_beyond_mi"], 20)

    def test_no_lightning_sensor_says_so(self):
        got = store.strikes(self.st.con, datetime(2026, 10, 1).timestamp(), datetime(2026, 10, 2).timestamp())
        self.assertEqual((got["n"], got["sensors"]), ([], 0))


class FarStrikesNotCounted(unittest.TestCase):
    def test_the_count_for_today_skips_a_far_strike(self):
        rec = Recorder(ignore_beyond_mi=10)
        rec._count_strikes("s", 5, "2026-11-06", "a", 4)
        self.assertEqual(rec._count_strikes("s", 7, "2026-11-06", "b", 15), 0)    # 15 mi: left out
        self.assertFalse(rec._strikes["s"]["rose"])
        self.assertEqual(rec._count_strikes("s", 8, "2026-11-06", "c", 6), 1)     # only the near one
        self.assertEqual(rec._strikes["s"]["kept"], {"last_strike_mi": 6, "last_strike_ts": "c"})

    def test_without_a_limit_every_strike_counts(self):
        rec = Recorder()
        rec._count_strikes("s", 5, "2026-11-06", "a", 4)
        self.assertEqual(rec._count_strikes("s", 7, "2026-11-06", "b", 15), 2)

    def test_the_card_keeps_the_last_counted_strike(self):
        st = Station()
        try:
            rec = Recorder(ignore_beyond_mi=10)
            from cc0ac.sources import Reading
            def hear(at, count, ts, mi):
                return rec.record_reading(Reading(source="test", received_utc=at, sensor_id="s", type="Atlas", raw=f"r{at}",
                                                  fields={"strike_count": count, "last_strike_ts": ts, "last_strike_mi": mi}),
                                          st.root, {}, None, st.db)
            hear("2026-11-06T20:00:00Z", 5, "a", 4)
            hear("2026-11-06T20:01:00Z", 6, "b", 3)
            entry = hear("2026-11-06T20:02:00Z", 9, "c", 18)
            self.assertEqual(entry["fields"]["last_strike_mi"], 3)
            self.assertEqual(entry["fields"]["last_strike_ts"], "b")
            self.assertEqual(entry["fields"]["strikes_today"], 1)
        finally:
            rec.close()
            st.close()


class LightningAlertThroughABurst(unittest.TestCase):
    """A sensor sends only its latest strike's distance. In a storm of close and far strikes
    mixed, a far one used to end the alert a minute after a close one ("No lightning within
    10 mi for 30 minutes"), and the next close one started it again: alerts inside the
    bursts, as in Anthony's 11-06 storm. One alert per storm, one all-clear."""

    def setUp(self):
        from cc0ac import alerts
        self.alerts = alerts
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sent, self._send = [], alerts.send
        alerts.send = lambda c, title, message: self.sent.append((title, message)) or []
        self.c = configparser.ConfigParser()
        self.c.read_dict({"alerts": {"enabled": "true", "weather": "lightning"}, "station": {"name": "Home"}})

    def tearDown(self):
        self.alerts.send = self._send
        self.tmp.cleanup()

    def storm(self, strikes, minutes):
        """strikes: {minute: miles}; the station checks once a minute."""
        t0 = datetime(2026, 11, 6, 6, 0).timestamp()
        count = 256
        for m in range(minutes):
            latest = max((s for s in strikes if s <= m), default=None)
            fields = {}
            if latest is not None:
                count = 256 + sum(1 for s in strikes if s <= m)
                fields = {"last_strike_mi": strikes[latest], "strikes_today": count - 256,
                          "last_strike_heard": store.epoch_to_iso(t0 + latest * 60)}
            (self.root / "current.json").write_text(json.dumps(
                {"sensors": [{"sensor_id": "6045", "name": "Lightning", "fields": fields}]}))
            self.alerts.check_weather(self.c, self.root, t0 + m * 60, self.root / "state.json")
        return [title for title, _ in self.sent]

    def test_far_strikes_between_close_ones_do_not_end_it(self):
        said = self.storm({0: 4, 2: 18, 5: 6, 7: 22, 9: 3, 12: 25}, 60)
        self.assertEqual(said, ["Home: lightning", "Home: lightning over"])
        self.assertIn("for 30 minutes", self.sent[1][1])

    def test_the_all_clear_counts_from_the_last_close_strike(self):
        self.storm({0: 4, 9: 3, 12: 25}, 39)
        self.assertEqual(len(self.sent), 1)           # 9 + 30: still within the window
        self.storm({0: 4, 9: 3, 12: 25}, 41)
        self.assertEqual(len(self.sent), 2)

    def test_only_far_strikes_never_alert(self):
        self.assertEqual(self.storm({0: 18, 3: 25}, 20), [])

    def test_a_close_strike_after_the_all_clear_is_a_new_storm(self):
        self.assertEqual(self.storm({0: 4, 50: 5}, 90),
                         ["Home: lightning", "Home: lightning over", "Home: lightning", "Home: lightning over"])


class LastStrikeShowsWhen(unittest.TestCase):
    """The card's last strike says when, then how far (Anthony, 2026-11-10). Checked in a
    browser against his snapshot: the hub's 2026-11-06T12:45:34 (UTC, no zone) showed as
    11/6/26 7:45 AM in New York and 1:45 PM in Berlin."""
    def test_the_row_carries_the_time(self):
        page = (HERE / "weather.html").read_text()
        self.assertIn("strikeTime(f.last_strike_ts) + Units.text('distance', f.last_strike_mi)", page)
        self.assertIn("s + 'Z'", page)                     # the hub's time is UTC without a zone
        self.assertIn("new Date(Number(s) * 1000)", page)  # a console's is epoch seconds


class Periods(unittest.TestCase):
    today = date(2026, 11, 7)   # a Saturday

    def test_a_day_is_yesterday_by_default(self):
        self.assertEqual(reports.period_days("day", None, self.today), (date(2026, 11, 6), date(2026, 11, 6)))

    def test_a_week_runs_sunday_to_saturday(self):
        self.assertEqual(reports.period_days("week", None, self.today), (date(2026, 10, 25), date(2026, 10, 31)))
        self.assertEqual(reports.period_days("week", "2026-11-04", self.today), (date(2026, 11, 1), date(2026, 11, 7)))

    def test_a_month_is_last_month_by_default(self):
        self.assertEqual(reports.period_days("month", None, self.today), (date(2026, 10, 1), date(2026, 10, 31)))
        self.assertEqual(reports.period_days("month", "2026-02", self.today), (date(2026, 2, 1), date(2026, 2, 28)))

    def test_nonsense_is_refused(self):
        with self.assertRaises(ValueError):
            reports.period_days("fortnight", None, self.today)
        with self.assertRaises(ValueError):
            reports.period_days("day", "11/06/2026", self.today)


class Reports(unittest.TestCase):
    def setUp(self):
        self.st = Station()
        s = self.st
        for h, t, rain in ((6, 41.0, 0.0), (12, 55.0, 0.1), (15, 69.0, 0.3)):
            s.add(local(2026, 11, 6, h), temp_f=t, humidity_pct=80 - h, rain_day_in=rain, wind_mph=4.0,
                  wind_gust_mph=h + 1.0, pressure_inhg=30.0 + h / 100)
        s.add(local(2026, 11, 6, 15, 30), strike_count=1, last_strike_ts="a", last_strike_mi=9)
        s.add(local(2026, 11, 6, 15, 40), strike_count=4, last_strike_ts="b", last_strike_mi=6)
        s.add(local(2026, 11, 6, 9), sid="room1", kind="tower", temp_f=68.0, humidity_pct=40)
        s.add(local(2025, 11, 6, 12), temp_f=50.0, rain_day_in=0.5)   # a year before
        store.roll_up(s.con, datetime(2026, 11, 7, 8).timestamp())
        (s.root / "normals.json").write_text(json.dumps({
            "source": "NOAA 1991-2020 normals", "station": "TEST AP", "miles": 4,
            "days": {"11-05": {"high": 58.0, "low": 41.0, "rain_mtd": 0.5},
                     "11-06": {"high": 57.0, "low": 40.0, "rain_mtd": 0.7}}}))
        self.now = datetime(2026, 11, 7, 8).timestamp()

    def tearDown(self):
        self.st.close()

    def report(self, **q):
        return reports.report(self.st.cfg(), self.st.db, {"atlas1": "Back yard", "room1": "Den"}, self.st.root,
                              {"period": "day", **q}, self.now)

    def test_a_day_lists_lows_averages_and_highs(self):
        text = self.report()["text"]
        self.assertIn("Test Station: Friday, November 6, 2026", text)
        temp = next(line for line in text.splitlines() if "Temperature" in line and "Back" not in line)
        self.assertEqual(temp.split(), ["Temperature", "41.0°F", "55.0°F", "69.0°F"])
        self.assertIn("Rain         0.30 in", text)
        self.assertIn("Den (tower)", text)

    def test_lightning_is_counted_with_the_closest(self):
        self.assertIn("Lightning    3 strikes, the closest 6 mi at Fri 15:40", self.report()["text"])

    def test_it_compares_with_the_normals_and_earlier_years(self):
        text = self.report()["text"]
        self.assertIn("Normal (NOAA 1991-2020 normals, TEST AP (4 mi))", text)
        self.assertIn("average high 57.0°F (this time +12.0°F)", text)
        self.assertIn("rain 0.20 in (this time +0.10 in)", text)
        self.assertIn("Earlier years here (2025; 1 days): average 50.0°F", text)

    def test_a_period_not_over_says_so_far(self):
        got = self.report(period="week", date="2026-11-06")
        self.assertTrue(got["so_far"])
        self.assertIn("so far", got["title"])

    def test_a_day_without_readings_says_so(self):
        self.assertIn("No readings were stored", self.report(date="2026-09-01")["text"])


class ReportEmails(unittest.TestCase):
    def setUp(self):
        self.st = Station()
        self.st.add(local(2026, 11, 6), temp_f=50.0)
        self.sent = []
        self.real = reports.alerts._email
        reports.alerts._email = lambda cfg, to, title, text, html=None: self.sent.append((to, title))
        self.state = self.st.root / "alert-state.json"

    def tearDown(self):
        reports.alerts._email = self.real
        self.st.close()

    def check(self, at, **reps):
        cfg = self.st.cfg(reports={"send": "daily, monthly", **reps}, alerts={"email_to": "me@example.com"})
        return reports.check(cfg, self.st.db, {}, self.st.root, datetime(*at).timestamp(), self.state)

    def test_each_report_goes_once_after_the_hour(self):
        self.assertEqual(self.check((2026, 11, 7, 6, 0)), "reports: not yet the hour")
        self.assertEqual(self.check((2026, 11, 7, 7, 5)), "reports: daily sent, monthly sent")
        self.assertEqual(self.check((2026, 11, 7, 7, 10)), "reports: nothing due")
        self.assertEqual(self.check((2026, 11, 8, 7, 0)), "reports: daily sent")
        self.assertEqual([t for _, t in self.sent], ["Test Station: Friday, November 6, 2026", "Test Station: October 2026",
                                                     "Test Station: Saturday, November 7, 2026"])
        self.assertEqual(json.loads(self.state.read_text())["reports"], {"day": "2026-11-07", "month": "2026-10-01"})

    def test_its_own_address_and_hour(self):
        self.check((2026, 11, 7, 9, 0), email_to="reports@example.com", hour="9")
        self.assertEqual({to for to, _ in self.sent}, {"reports@example.com"})

    def test_off_unless_asked(self):
        cfg = self.st.cfg(alerts={"email_to": "me@example.com"})
        self.assertEqual(reports.check(cfg, self.st.db, {}, self.st.root, state_path=self.state), "reports off")


class ReportPage(unittest.TestCase):
    def setUp(self):
        self.st = Station()
        self.st.add(local(2026, 11, 6), temp_f=50.0, strike_count=1, last_strike_mi=30)
        self.st.add(local(2026, 11, 6, 12, 5), temp_f=50.0, strike_count=2, last_strike_mi=30)
        cfg = self.st.cfg(lightning={"ignore_beyond_mi": "20"})
        handler = web._make_web_handler(self.st.root, self.st.db, {}, cfg=cfg, mcp=False)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.st.close()

    def get(self, path):
        with urllib.request.urlopen(self.url + path, timeout=10) as r:
            return r.read().decode(), r.headers["Content-Type"]

    def test_the_report_as_text_and_json(self):
        text, ctype = self.get("/report?period=day&date=2026-11-06")
        self.assertTrue(ctype.startswith("text/plain"))
        self.assertIn("Friday, November 6, 2026", text)
        self.assertEqual(json.loads(self.get("/report.json?period=month&date=2026-11")[0])["first"], "2026-11-01")

    def test_a_bad_period_is_a_400(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get("/report?period=fortnight")
        self.assertEqual(e.exception.code, 400)

    def test_the_charts_leave_out_far_strikes(self):
        h = json.loads(self.get("/history.json?from=2026-11-06T11:00&to=2026-11-06T13:00")[0])
        self.assertEqual((h["strikes"]["n"], h["strikes"]["ignore_beyond_mi"]), ([], 20.0))


if __name__ == "__main__":
    unittest.main()
