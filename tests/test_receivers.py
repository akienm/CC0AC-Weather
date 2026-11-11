"""A receiver gone quiet while another still hears the same sensors: python3 -m unittest tests/test_receivers.py

Born of 2026-11-09..11: the radio dongle stopped starting (rtl_433 exit code 3) and the
hub kept hearing every sensor, so no sensor went quiet and nothing was said for two days.
"""
import configparser
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cc0ac import alerts   # noqa: E402

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc).timestamp()


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Receivers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "alert-state.json"
        self.sent = []
        self.saved = alerts.send, alerts.RTL_433_LAST_EXIT
        alerts.send = lambda cfg, title, message: self.sent.append((title, message)) or []
        alerts.RTL_433_LAST_EXIT = self.root / "rtl_433-last-exit.txt"

    def tearDown(self):
        alerts.send, alerts.RTL_433_LAST_EXIT = self.saved
        self.tmp.cleanup()

    def cfg(self, **more):
        cfg = configparser.ConfigParser()
        cfg.read_dict({"station": {"name": "Test Station"}, "device": {"write_path": str(self.root)},
                       "alerts": {"enabled": "true", "ntfy_topic": "t"},
                       "hub": {"enabled": "true"}, "capture": {"enabled": "true"}, **more})
        return cfg

    def heard(self, hub_ago=1, radio_ago=None, at=NOW):
        """Two sensors as of `at`, both heard by the hub; by the radio too while radio_ago is given."""
        reception = {"hub": {"last": iso(at - hub_ago * 60)}}
        if radio_ago is not None:
            reception["radio"] = {"last": iso(at - radio_ago * 60)}
        sensors = [{"sensor_id": sid, "name": name, "type": "tower", "updated": iso(at - min(hub_ago, radio_ago or hub_ago) * 60),
                    "fields": {}, "reception": reception} for sid, name in (("a1", "Den"), ("b2", "Attic"))]
        (self.root / "current.json").write_text(json.dumps({"written": iso(at), "sensors": sensors}))

    def check(self, at=NOW, cfg=None):
        return alerts.check_sensors(cfg or self.cfg(), self.root, at, self.state)

    def test_the_radio_quiet_while_the_hub_hears_is_told_once_and_back_once(self):
        self.heard(radio_ago=1)
        self.assertEqual(self.check(), "sensors: fine")
        # An hour and more later the radio drops out of reception entirely; only the kept time says when.
        self.heard(at=NOW + 45 * 60)
        self.assertEqual(self.check(NOW + 45 * 60), "sensors: told (1)")
        title, message = self.sent[0]
        self.assertEqual(title, "Test Station: a sensor needs a look")
        self.assertIn("The radio (rtl_433) hasn't heard any sensor since", message)
        self.assertIn("unplug the dongle and plug it back in", message)
        self.heard(at=NOW + 50 * 60)
        self.assertEqual(self.check(NOW + 50 * 60), "sensors: same as before")
        self.heard(radio_ago=0, at=NOW + 56 * 60)
        self.assertEqual(self.check(NOW + 56 * 60), "sensors: back (1)")
        self.assertEqual(self.sent[1][1], "The radio (rtl_433) is hearing the sensors again.")

    def test_rtl_433s_last_words_are_quoted_when_newer_than_its_last_reading(self):
        self.heard(radio_ago=40)
        alerts.RTL_433_LAST_EXIT.write_text("exit code 3: Input: Input device start failed\n")
        self.assertEqual(self.check(), "sensors: told (1)")
        self.assertIn("rtl_433's last words: exit code 3: Input: Input device start failed", self.sent[0][1])

    def test_old_last_words_are_not_quoted(self):
        alerts.RTL_433_LAST_EXIT.write_text("exit code 3: long ago\n")
        os.utime(alerts.RTL_433_LAST_EXIT, (NOW - 7200, NOW - 7200))
        self.heard(radio_ago=40)
        self.check()
        self.assertNotIn("last words", self.sent[0][1])

    def test_a_receiver_switched_off_is_not_watched(self):
        self.heard(radio_ago=40)
        self.assertEqual(self.check(cfg=self.cfg(capture={"enabled": "false"})), "sensors: fine")

    def test_everything_quiet_is_the_no_readings_message_not_this_one(self):
        self.heard(hub_ago=0, radio_ago=0, at=NOW - 45 * 60)
        self.assertEqual(self.check(), "sensors: fine")

    def test_a_receiver_never_heard_says_nothing(self):
        self.heard()
        self.assertEqual(self.check(), "sensors: fine")

    def test_off_with_the_sensors_notice(self):
        self.heard(radio_ago=40)
        cfg = self.cfg()
        cfg.set("alerts", "notices", "service")
        self.assertEqual(self.check(cfg=cfg), "sensor notices off")


class Rtl433Stderr(unittest.TestCase):
    def test_the_last_lines_are_kept(self):
        import io
        from cc0ac.sources import rtl_433
        tail, reader = rtl_433._keep_tail(io.StringIO("Found 1 device\n\nsetting gain\nAllocating 15 zero-copy buffers\n"
                                                      "Input: Input device start failed\nbye\n"))
        reader.join(2)
        self.assertEqual(list(tail), ["setting gain", "Allocating 15 zero-copy buffers",
                                      "Input: Input device start failed", "bye"])


if __name__ == "__main__":
    unittest.main()
