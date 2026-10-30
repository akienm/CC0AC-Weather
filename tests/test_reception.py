"""Checks for the sensor card's line: channel, battery and reception per receiver.
python3 -m unittest tests/test_reception.py

These need no network or radio.
"""
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from cc0ac.sources import rtl_433   # noqa: E402


from cc0ac import recorder

from cc0ac import store

NOW = 1_800_000_000.0


def heard(sid, who, times, snr=None):
    for t in times:
        recorder._note_heard(sid, who, t, snr)


class Reception(unittest.TestCase):
    def setUp(self):
        recorder._heard.clear()

    def test_every_reading_arrived(self):
        heard("t1", "radio", [NOW - 3600 + i * 16.25 for i in range(222)], snr=18.0)
        r = recorder.reception("t1", NOW)["radio"]
        self.assertAlmostEqual(r["every_s"], 16.2, delta=0.1)
        self.assertGreaterEqual(r["percent"], 99)
        self.assertEqual(r["snr_db"], 18.0)

    def test_missed_readings_lower_the_count_not_the_interval(self):
        # Every third reading lost, and the times rounded to the second as stored.
        times = [float(round(NOW - 3600 + i * 16.25)) for i in range(222) if i % 3]
        heard("t2", "radio", times)
        r = recorder.reception("t2", NOW)["radio"]
        self.assertAlmostEqual(r["every_s"], 16.25, delta=0.2)
        self.assertTrue(64 <= r["percent"] <= 69, r)

    def test_a_silent_half_hour_shows(self):
        times = [NOW - 3600 + i * 16.25 for i in range(222)]
        heard("t3", "radio", [t for t in times if not NOW - 2400 < t < NOW - 600])
        self.assertTrue(45 <= recorder.reception("t3", NOW)["radio"]["percent"] <= 55)

    def test_a_new_sensor_is_expected_only_since_first_heard(self):
        heard("t4", "radio", [NOW - 600 + i * 16.25 for i in range(37)])
        self.assertGreaterEqual(recorder.reception("t4", NOW)["radio"]["percent"], 95)

    def test_receivers_counted_apart_and_old_readings_dropped(self):
        heard("t5", "hub", [NOW - 7200 + i * 300 for i in range(25)])
        heard("t5", "radio", [NOW - 100])
        r = recorder.reception("t5", NOW)
        self.assertEqual(r["hub"]["heard"], 13)
        self.assertEqual((r["hub"]["expected"], r["hub"]["percent"]), (12, 100))
        self.assertEqual(r["radio"], {"heard": 1, "last": r["radio"]["last"]})

    def test_seeded_from_the_store_after_a_restart(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "weather.db"
            con = store.open_db(db)
            # The hub's 5-minute uploads over the last two hours, off the hour's edge as real ones are.
            for i in range(24):
                at = datetime.fromtimestamp(NOW - 7100 + i * 300, timezone.utc)
                con.execute("INSERT INTO readings (received_utc, hub_id, sensor_id, query) VALUES (?, ?, ?, ?)",
                            (at.strftime("%Y-%m-%dT%H:%M:%SZ"), "24C86E000000", "t6", f"q{i}"))
            con.commit()
            con.close()
            recorder.seed_heard(db, NOW)
        r = recorder.reception("t6", NOW)["hub"]
        self.assertEqual((r["heard"], r["expected"], r["percent"]), (12, 12, 100))

    def test_stored_rows_name_their_receiver(self):
        self.assertEqual(recorder._receiver_of("", "rtl_433 dongle 0"), "radio")
        self.assertEqual(recorder._receiver_of("", "24C86E000000"), "hub")
        self.assertEqual(recorder._receiver_of("", "api.weather.gov"), "online")
        self.assertEqual(recorder._receiver_of("acurite-access"), "hub")


class RadioLine(unittest.TestCase):
    LINE = {"time": "2026-10-09 16:20:00", "model": "Acurite-Tower", "id": 1234, "channel": "C",
            "battery_ok": 1, "temperature_C": 23.3, "humidity": 51, "mic": "CHECKSUM",
            "mod": "ASK", "freq": 433.95, "rssi": -0.1, "snr": 21.4, "noise": -21.5}

    def test_channel_and_snr_reach_the_reading(self):
        r = next(rtl_433.readings([json.dumps(self.LINE)], "rtl_433 dongle 0", rtl_433.RainCounter(None)))
        self.assertEqual((r.channel, r.snr_db, r.fields["battery"]), ("C", 21.4, "normal"))

    def test_repeats_differing_only_in_level_are_dropped(self):
        again = dict(self.LINE, rssi=-0.4, snr=20.9, noise=-21.3, freq=433.94)
        out = list(rtl_433.readings([json.dumps(self.LINE), json.dumps(again)], "r", rtl_433.RainCounter(None)))
        self.assertEqual(len(out), 1)

    def test_command_asks_for_levels(self):
        import configparser
        cmd = rtl_433.command(configparser.ConfigParser())
        self.assertIn("level", [cmd[i + 1] for i, c in enumerate(cmd) if c == "-M"])


if __name__ == "__main__":
    unittest.main()
