"""Checks for the station's own day: its time zone, the 24-hour charts and
lightning strikes counted since midnight and the alert for them. python3 -m unittest tests/test_station_day.py

These need no network or radio. Each one that sets a time zone puts the
computer's back afterwards.
"""
import configparser
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "updates"))
import bootloader as bl   # noqa: E402
from cc0ac import alerts             # noqa: E402
from cc0ac import wizard             # noqa: E402

from cc0ac import capture

from cc0ac import config

from cc0ac import recorder

from cc0ac import store

ZONE = "America/Denver"


def station(**values) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["station"] = values
    return cfg


class TimeZoneKept(unittest.TestCase):
    def setUp(self):
        self.was = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"     # as a Docker container's clock is
        time.tzset()

    def tearDown(self):
        if self.was is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.was
        time.tzset()


@unittest.skipUnless((Path("/usr/share/zoneinfo") / ZONE).is_file(), "no tz database on this computer")
class StationTimeZone(TimeZoneKept):
    def test_today_starts_at_the_stations_midnight_on_a_utc_computer(self):
        config.apply_timezone(station(timezone=ZONE))
        now = datetime.now().astimezone()
        lo, hi, period = store._history_bounds({"period": "today"}, 0, now)
        start = datetime.fromtimestamp(lo, ZoneInfo(ZONE))
        self.assertEqual((period, start.hour, start.minute), ("today", 0, 0))
        self.assertEqual(start.date(), datetime.now(ZoneInfo(ZONE)).date())

    def test_the_days_rain_and_records_follow_it_too(self):
        config.apply_timezone(station(timezone=ZONE))
        # 03:00 UTC is the evening before, in Denver.
        at = datetime(2026, 10, 24, 3, 0, tzinfo=ZoneInfo("UTC")).timestamp()
        self.assertEqual(store.epoch_to_day(at), "2026-10-23")

    def test_unset_leaves_the_computers_clock(self):
        config.apply_timezone(station())
        self.assertEqual(os.environ["TZ"], "UTC")

    def test_a_zone_this_computer_lacks_is_refused_loudly(self):
        for bad in ("Mars/Olympus_Mons", "../../etc/passwd", "America/Denver; rm"):
            with self.assertLogs(capture.log, "ERROR"):
                config.apply_timezone(station(timezone=bad))
            self.assertEqual(os.environ["TZ"], "UTC")

    def test_the_boot_loader_schedules_in_it(self):
        bl.station_timezone(station(timezone=ZONE))
        self.assertEqual(os.environ["TZ"], ZONE)
        self.assertEqual(datetime.now().astimezone().tzname(), datetime.now(ZoneInfo(ZONE)).tzname())


class SetupPage(unittest.TestCase):
    TEXT = (HERE / "config.ini.example").read_text()

    def test_saved_and_shown(self):
        text = wizard.apply_setup(self.TEXT, {"station": {"name": "Home", "timezone": ZONE}})
        cfg = configparser.ConfigParser()
        cfg.read_string(text)
        self.assertEqual(cfg.get("station", "timezone"), ZONE)
        self.assertEqual(wizard.setup_state(cfg, leases=Path("/nonexistent"))["current"]["station"]["timezone"], ZONE)

    def test_not_a_zone_is_not_saved(self):
        with self.assertRaises(ValueError):
            wizard.apply_setup(self.TEXT, {"station": {"timezone": "../../etc/passwd"}})


class LastDay(unittest.TestCase):
    def test_24_hours_is_from_this_time_yesterday(self):
        now = datetime(2026, 10, 24, 15, 30).astimezone()
        lo, hi, period = store._history_bounds({"period": "24h"}, 0, now)
        self.assertEqual((period, hi - lo), ("24h", 86400))
        self.assertEqual(hi, now.timestamp())

    def test_the_charts_page_offers_it(self):
        page = (HERE / "charts.html").read_text()
        self.assertIn('data-p="24h"', page)
        self.assertIn('"24h"', page.split("includes(h)")[0].rsplit("[", 1)[1])


class Strikes(unittest.TestCase):
    """The hub sends a sensor's running count; one strike once showed as 256."""

    def setUp(self):
        self.rec = recorder.Recorder()

    def test_each_recorder_counts_on_its_own(self):
        other = recorder.Recorder()
        self.rec._count_strikes("s", 10, "2026-10-24")
        self.rec._count_strikes("s", 14, "2026-10-24")
        self.assertEqual(other._count_strikes("s", 14, "2026-10-24"), 0)   # a new one has heard nothing

    def test_the_count_already_standing_is_not_todays(self):
        self.assertEqual(self.rec._count_strikes("s", 256, "2026-10-24"), 0)
        self.assertEqual(self.rec._count_strikes("s", 257, "2026-10-24"), 1)
        self.assertEqual(self.rec._count_strikes("s", 257, "2026-10-24"), 1)

    def test_a_count_started_again_adds_its_whole_count(self):
        self.rec._count_strikes("s", 250, "2026-10-24")
        self.rec._count_strikes("s", 252, "2026-10-24")
        self.assertEqual(self.rec._count_strikes("s", 3, "2026-10-24"), 5)

    def test_midnight_starts_the_day_at_zero(self):
        self.rec._count_strikes("s", 10, "2026-10-24")
        self.rec._count_strikes("s", 14, "2026-10-24")
        self.assertEqual(self.rec._count_strikes("s", 14, "2026-10-25"), 0)
        self.assertEqual(self.rec._count_strikes("s", 15, "2026-10-25"), 1)

    def test_one_strike_that_reads_256_counts_once(self):
        # The pattern an Access hub sent on 2026-10-23: 0, then 256 with the first
        # strike time, then 256 with that same time.
        self.assertEqual(self.rec._count_strikes("s", 0, "2026-10-24"), 0)
        self.assertEqual(self.rec._count_strikes("s", 256, "2026-10-24", "2026-10-24T09:10:25"), 1)
        self.assertEqual(self.rec._count_strikes("s", 256, "2026-10-24", "2026-10-24T09:10:25"), 1)
        self.assertEqual(self.rec._count_strikes("s", 512, "2026-10-24", "2026-10-24T11:00:00"), 2)

    def test_a_new_strike_time_counts_even_when_the_count_stands(self):
        self.rec._count_strikes("s", 7, "2026-10-24", "2026-10-24T09:00:00")
        self.assertEqual(self.rec._count_strikes("s", 7, "2026-10-24", "2026-10-24T09:00:00"), 0)
        self.assertEqual(self.rec._count_strikes("s", 7, "2026-10-24", "2026-10-24T09:30:00"), 1)
        self.assertEqual(self.rec._count_strikes("s", 10, "2026-10-24", "2026-10-24T09:40:00"), 4)

    def test_a_restart_reads_the_day_back_from_the_store(self):
        now = datetime(2026, 10, 24, 15, 0).astimezone().timestamp()
        midnight = datetime(2026, 10, 24).astimezone().timestamp()
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "weather.db"
            con = store.open_db(db)
            for i, (at, count) in enumerate([(midnight - 3600, 255), (midnight + 600, 256),
                                              (midnight + 1200, 256), (now - 60, 258)]):
                con.execute("INSERT INTO readings (received_utc, sensor_id, strike_count, query) VALUES (?, ?, ?, ?)",
                            (store.epoch_to_iso(at), "s", count, f"q{i}"))
            con.commit()
            con.close()
            self.rec._seed_strikes(db, "s", now)
        self.assertEqual(self.rec._count_strikes("s", 259, store.epoch_to_day(now)), 4)

    def test_the_card_gets_strikes_today(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            for i, count in enumerate((256, 257)):
                at = (datetime.now().astimezone() + timedelta(seconds=i)).astimezone(ZoneInfo("UTC"))
                r = capture.Reading(source="acurite-access", sensor_id="s", type="Atlas",
                                    received_utc=at.strftime("%Y-%m-%dT%H:%M:%SZ"), raw=f"q{i}",
                                    fields={"strike_count": count})
                entry = self.rec.record_reading(r, out, {})
        self.assertEqual((entry["fields"]["strike_count"], entry["fields"]["strikes_today"]), (257, 1))

    def test_a_strike_is_stamped_only_when_the_count_rises(self):
        stamps = []
        with tempfile.TemporaryDirectory() as d:
            for i, count in enumerate((40, 40, 41, 41)):
                at = f"2026-10-24T20:0{i}:00Z"
                r = capture.Reading(source="acurite-access", sensor_id="s2", type="Atlas", received_utc=at,
                                    raw=f"r{i}", fields={"strike_count": count, "last_strike_mi": 6})
                stamps.append(self.rec.record_reading(r, Path(d), {})["fields"].get("last_strike_heard"))
        self.assertEqual(stamps, [None, None, "2026-10-24T20:02:00Z", "2026-10-24T20:02:00Z"])


class LightningAlert(unittest.TestCase):
    NOW = datetime(2026, 10, 24, 20, 0).astimezone().timestamp()

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.sent = []
        self._send = alerts.send
        alerts.send = lambda c, title, message: self.sent.append((title, message)) or []
        self.c = configparser.ConfigParser()
        self.c["alerts"] = {"enabled": "true", "weather": "lightning"}
        self.c["station"] = {"name": "Home"}

    def tearDown(self):
        alerts.send = self._send
        self.dir.cleanup()

    def look(self, mi, minutes_ago, today=3, now=None):
        heard = datetime.fromtimestamp(self.NOW - minutes_ago * 60, ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
        fields = {"strike_count": 259, "strikes_today": today, "last_strike_mi": mi, "last_strike_heard": heard}
        (self.root / "current.json").write_text(json.dumps({"sensors": [
            {"sensor_id": "1", "name": "Yard", "type": "Atlas", "updated": heard, "fields": fields}]}))
        return alerts.check_weather(self.c, self.root, now or self.NOW, self.root / "state.json")

    def test_a_close_strike_alerts_once_then_over_after_the_quiet(self):
        self.assertEqual(self.look(6, 2), "weather: lightning alerted")
        self.assertEqual(self.sent[-1], ("Home: lightning", "Lightning: a strike 6 mi away, heard at Yard (3 today)."))
        self.assertEqual(self.look(4, 20, today=5), "weather: nothing new")
        self.assertEqual(self.look(4, 31, today=5), "weather: lightning over")
        self.assertEqual(self.sent[-1], ("Home: lightning over", "No lightning within 10 mi for 30 minutes."))
        self.assertEqual(len(self.sent), 2)

    def test_a_far_or_old_strike_is_quiet(self):
        self.assertEqual(self.look(15, 1), "weather: nothing new")
        self.assertEqual(self.look(3, 45), "weather: nothing new")
        self.assertEqual(self.sent, [])

    def test_the_radius_and_window_are_settings(self):
        self.c["alerts"].update(lightning_mi="20", lightning_minutes="60")
        self.assertEqual(self.look(15, 45), "weather: lightning alerted")


if __name__ == "__main__":
    unittest.main()
