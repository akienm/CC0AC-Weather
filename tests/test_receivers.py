"""A receiver that has heard nothing for 5 minutes: python3 -m unittest tests/test_receivers.py

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
        self.saved = alerts.send, alerts.RTL_433_LAST_EXIT, alerts._raw_log_written
        alerts.send = lambda cfg, title, message: self.sent.append((title, message)) or []
        alerts.RTL_433_LAST_EXIT = self.root / "rtl_433-last-exit.txt"
        self.logs: dict = {}                     # receiver -> its raw log's last write
        alerts._raw_log_written = lambda cfg: dict(self.logs)

    def tearDown(self):
        alerts.send, alerts.RTL_433_LAST_EXIT, alerts._raw_log_written = self.saved
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

    def test_everything_quiet_tells_of_each_receiver_too(self):
        # Akien 2026-11-11: "any radio we haven't heard from in 5 minutes merits a small alert
        # pointing at the status page" -- whether or not another still hears.
        self.heard(hub_ago=0, radio_ago=0, at=NOW - 45 * 60)
        self.assertEqual(self.check(), "sensors: told (2)")

    def test_five_minutes_is_the_line_and_the_message_points_at_the_status_page(self):
        self.heard(radio_ago=4)
        self.assertEqual(self.check(), "sensors: fine")
        self.heard(radio_ago=6, at=NOW + 2 * 60)     # the same radio hearing, two minutes on
        self.assertEqual(self.check(NOW + 2 * 60), "sensors: told (1)")
        self.assertRegex(self.sent[0][1], r"Status: http://[^ ]+\.local:12345/status$")

    def test_the_line_is_a_setting(self):
        self.heard(radio_ago=6)
        cfg = self.cfg()
        cfg.set("alerts", "receiver_quiet_minutes", "15")
        self.assertEqual(self.check(cfg=cfg), "sensors: fine")

    def test_the_status_link_uses_the_dashboard_port(self):
        self.heard(radio_ago=6)
        self.check(cfg=self.cfg(web={"port": "8080, 80"}))
        self.assertTrue(self.sent[0][1].endswith(":8080/status"))

    def test_a_restarted_station_knows_from_the_raw_log(self):
        # 2026-11-11: after a reboot current.json had no hub at all; the hub's log said 14:12.
        self.heard(radio_ago=0)
        self.check()
        self.state.unlink()
        self.heard(radio_ago=0, at=NOW + 3600)        # no hub in reception: drop it
        data = json.loads((self.root / "current.json").read_text())
        for s in data["sensors"]:
            del s["reception"]["hub"]
        (self.root / "current.json").write_text(json.dumps(data))
        self.logs = {"hub": NOW + 3600 - 2 * 60}
        self.assertEqual(self.check(NOW + 3600), "sensors: fine")
        self.heard(radio_ago=0, at=NOW + 3600 + 8 * 60)   # eight minutes on, the hub's log still unwritten
        data = json.loads((self.root / "current.json").read_text())
        for s in data["sensors"]:
            del s["reception"]["hub"]
        (self.root / "current.json").write_text(json.dumps(data))
        self.assertEqual(self.check(NOW + 3600 + 8 * 60), "sensors: told (1)")
        self.assertIn("The hub hasn't heard any sensor since", self.sent[0][1])

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



class Rtl433GetsGoingAgain(unittest.TestCase):
    """Akien 2026-11-11: "it's supposed to be an appliance ... like a maytag, just keeps on working"."""

    def step(self, failed, restarted_ago=None):
        from cc0ac.sources import rtl_433
        return rtl_433.next_step(failed, NOW, None if restarted_ago is None else NOW - restarted_ago)

    def test_the_ladder(self):
        self.assertEqual([self.step(n) for n in range(1, 9)],
                         ["retry", "retry", "reset", "retry", "replug", "retry", "restart", "restart"])

    def test_the_station_is_restarted_at_most_once_an_hour(self):
        self.assertEqual(self.step(7, restarted_ago=600), "wait")
        self.assertEqual(self.step(40, restarted_ago=3599), "wait")
        self.assertEqual(self.step(41, restarted_ago=3600), "restart")

    def test_only_rtl_sdr_dongles_are_reset(self):
        from cc0ac.sources import rtl_433
        with tempfile.TemporaryDirectory() as d:
            for name, vendor, product in (("1-1", "0bda", "2838"), ("1-2", "046d", "c52b")):
                dev = Path(d) / name
                dev.mkdir()
                for f, v in (("idVendor", vendor), ("idProduct", product), ("busnum", "1"), ("devnum", "250")):
                    (dev / f).write_text(v + "\n")
            did = rtl_433.reset_dongles(Path(d))
        self.assertEqual(len(did), 1)       # the mouse is left alone; /dev/bus/usb/001/250 isn't there
        self.assertIn("could not reset 1-1", did[0])

    def test_no_dongle_no_reset(self):
        from cc0ac.sources import rtl_433
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(rtl_433.reset_dongles(Path(d) / "absent"), [])

    def test_a_radio_that_will_not_start_climbs_the_ladder(self):
        # The loop itself, with an rtl_433 that dies at once as on 2026-11-09.
        from cc0ac.sources import rtl_433
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            saved = (rtl_433.command, rtl_433.time.sleep, rtl_433.os._exit, rtl_433.reset_dongles, rtl_433.replug_dongles,
                     rtl_433.CONFIG_DIR, rtl_433.LAST_EXIT, rtl_433.RESTARTED)
            resets, replugs, slept = [], [], []

            class Ended(Exception):
                pass

            def end(code):
                raise Ended(code)
            rtl_433.command = lambda cfg: ["sh", "-c", "echo Input device start failed >&2; exit 3"]
            rtl_433.time.sleep, rtl_433.os._exit = slept.append, end
            rtl_433.reset_dongles = lambda: resets.append(1) or ["reset 0bda:2838"]
            rtl_433.replug_dongles = lambda: replugs.append(1) or ["replugged 1-1.3"]
            rtl_433.CONFIG_DIR, rtl_433.LAST_EXIT, rtl_433.RESTARTED = home, home / "last.txt", home / "restarted.txt"
            try:
                with self.assertRaises(Ended) as ended:
                    rtl_433.run(configparser.ConfigParser(), lambda r: {})
                self.assertEqual(ended.exception.args, (75,))
                self.assertEqual((len(resets), len(replugs), slept), (1, 1, [10] * 6))
                self.assertTrue(rtl_433.RESTARTED.exists())
                self.assertIn("exit code 3: Input device start failed", rtl_433.LAST_EXIT.read_text())
            finally:
                (rtl_433.command, rtl_433.time.sleep, rtl_433.os._exit, rtl_433.reset_dongles, rtl_433.replug_dongles,
                 rtl_433.CONFIG_DIR, rtl_433.LAST_EXIT, rtl_433.RESTARTED) = saved

if __name__ == "__main__":
    unittest.main()
