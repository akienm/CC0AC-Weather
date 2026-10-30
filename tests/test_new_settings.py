"""Checks for 1.3.16: settings that came with an update appear in an older config.ini.
python3 -m unittest tests/test_new_settings.py

A config written before [windy] existed gets [windy] added at its end, at its
default (off) and explained, as config.ini.example has it; nothing already in the file
moves. Sensor 00001234 is made up.
"""
import configparser
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from cc0ac import wizard   # noqa: E402

OLD = """[device]
; where the files go
write_path = ~/AcuRite

[sensors]
00001234 = Yard
"""

EXAMPLE = """[device]
write_path = ~/AcuRite

[sensors]

[webhook]
; Posts each reading.
enabled = false
; url =

; ── Weather networks ──────────
; Each one uploads one station.

[windy]
; stations.windy.com: add a station.
enabled = false
; station_id =

[cwop]
enabled = false
"""


def capture():
    from cc0ac import config as module
    return module


class MissingSections(unittest.TestCase):
    def test_only_the_missing_ones_as_the_example_writes_them(self):
        added, names = wizard.missing_sections(OLD, EXAMPLE)
        self.assertEqual(names, ["webhook", "windy", "cwop"])
        self.assertIn("[webhook]\n; Posts each reading.\nenabled = false\n; url =", added)
        self.assertNotIn("[device]", added)

    def test_a_heading_travels_with_the_sections_under_it(self):
        added, _ = wizard.missing_sections(OLD, EXAMPLE)
        self.assertIn("; url =\n\n; ── Weather networks", added)
        self.assertIn("; Each one uploads one station.\n\n[windy]", added)
        self.assertEqual(added.count("Weather networks"), 1)
        added, names = wizard.missing_sections(OLD + "[windy]\nenabled = true\n", EXAMPLE)
        self.assertEqual(names, ["webhook", "cwop"])
        self.assertIn("Weather networks", added)     # still over [cwop]

    def test_names_match_whatever_their_case(self):
        self.assertEqual(wizard.missing_sections(OLD + "[Windy]\n", EXAMPLE)[1], ["webhook", "cwop"])

    def test_nothing_missing_adds_nothing(self):
        self.assertEqual(wizard.missing_sections(EXAMPLE, EXAMPLE), ("", []))

    def test_a_config_that_doesnt_read_is_left_alone(self):
        self.assertEqual(wizard.missing_sections("no section header\n", EXAMPLE), ("", []))


class AtStart(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name) / "config.ini"
        self.example = Path(self.tmp.name) / "config.ini.example"
        self.config.write_text(OLD)
        self.example.write_text(EXAMPLE)
        os.chmod(self.config, 0o600)
        self.capture = capture()

    def tearDown(self):
        self.tmp.cleanup()

    def test_appended_off_and_the_rest_untouched(self):
        with self.assertLogs("acurite", level="INFO") as logs:
            self.assertEqual(self.capture.add_new_settings(self.config, self.example), ["webhook", "windy", "cwop"])
        text = self.config.read_text()
        self.assertTrue(text.startswith(OLD))
        self.assertIn("settings newer than this file, at their defaults", text)
        cfg = configparser.ConfigParser()
        cfg.read_string(text)
        self.assertFalse(cfg.getboolean("windy", "enabled"))
        self.assertEqual(cfg.get("sensors", "00001234"), "Yard")
        self.assertIn("[webhook], [windy], [cwop]", logs.output[0])
        self.assertEqual(stat.S_IMODE(os.stat(self.config).st_mode), 0o600)   # it holds passwords

    def test_a_second_start_adds_nothing(self):
        self.capture.add_new_settings(self.config, self.example)
        once = self.config.read_text()
        self.assertEqual(self.capture.add_new_settings(self.config, self.example), [])
        self.assertEqual(self.config.read_text(), once)

    def test_a_file_it_cannot_write_is_a_warning_not_a_stop(self):
        os.chmod(self.config, 0o400)
        if os.access(self.config, os.W_OK):
            self.skipTest("running as root")
        with self.assertLogs("acurite", level="WARNING"):
            self.assertEqual(self.capture.add_new_settings(self.config, self.example), [])
        self.assertEqual(self.config.read_text(), OLD)

    def test_the_real_example_fills_an_old_config(self):
        added, names = wizard.missing_sections(OLD, (HERE / "config.ini.example").read_text())
        for name in ("windy", "pwsweather", "weathercloud", "cwop", "mqtt"):
            self.assertIn(name, names)
        cfg = configparser.ConfigParser(interpolation=None)
        cfg.read_string(OLD + "\n" + added)       # what results still reads, and uploads nothing
        for name in ("windy", "pwsweather", "weathercloud", "cwop", "mqtt", "webhook", "influxdb"):
            self.assertFalse(cfg.getboolean(name, "enabled"), name)

    def test_load_config_does_it(self):
        real = self.capture.add_new_settings
        seen = []
        self.capture.add_new_settings = lambda path: seen.append(path) or []
        try:
            self.capture.load_config(self.config)
        finally:
            self.capture.add_new_settings = real
        self.assertEqual(seen, [self.config])


if __name__ == "__main__":
    unittest.main()
