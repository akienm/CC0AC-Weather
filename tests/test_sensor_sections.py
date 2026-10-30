"""Checks for 1.3.17: each sensor its own section in config.ini, named by its ID,
with a title, an icon and the chart its card opens.
python3 -m unittest tests/test_sensor_sections.py

Sensors 00001234, 00005678 and 00009999 are made up.
"""
import configparser
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from cc0ac import calibration     # noqa: E402
from cc0ac import sensor_config   # noqa: E402
from cc0ac import wizard          # noqa: E402

NEW = """[device]
write_path = ~/AcuRite

[00005678]
title = Living Room
icon = 🛋️
calibration = temp_f -1.5

[00001234]
title = Yard
icon = sky
link = outdoor

[00009999]
; A Tower sensor, first heard 2026-10-24.
"""

OLDER = """[sensors]
00001234 = Backyard
0000aaaa = Garage

[sensor_order]
1 = 0000aaaa
2 = 00001234

[calibration]
00001234 = temp_f +1
0000aaaa = humidity_pct +3
"""


def read(text):
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_string(text)
    return cfg


def capture():
    from cc0ac import capture as module
    return module


class Table(unittest.TestCase):
    def test_sections_in_file_order_with_the_id_for_a_missing_title(self):
        self.assertEqual(sensor_config.titles(read(NEW)),
                         {"00005678": "Living Room", "00001234": "Yard", "00009999": "00009999"})

    def test_the_older_form_still_reads_in_its_order(self):
        self.assertEqual(sensor_config.titles(read(OLDER)), {"0000aaaa": "Garage", "00001234": "Backyard"})

    def test_own_section_has_the_last_word_and_comes_first(self):
        cfg = read(NEW + "\n" + OLDER)
        self.assertEqual(list(sensor_config.titles(cfg)), ["00005678", "00001234", "00009999", "0000aaaa"])
        self.assertEqual(sensor_config.title(cfg, "00001234"), "Yard")
        self.assertEqual(sensor_config.title(cfg, "0000AAAA"), "Garage")
        self.assertEqual(sensor_config.title(cfg, "00000000"), "")

    def test_cards_carry_only_what_is_set(self):
        self.assertEqual(sensor_config.cards(read(NEW)),
                         {"00005678": {"icon": "🛋️"}, "00001234": {"icon": "sky", "link": "outdoor"}})

    def test_a_link_that_is_not_a_chart_is_left_out(self):
        with self.assertLogs("acurite", "WARNING") as logs:
            cards = sensor_config.cards(read("[00001234]\ntitle = Yard\nlink = moon\n"))
        self.assertEqual(cards, {})
        self.assertIn("isn't one of", logs.output[0])

    def test_no_setting_reads_as_a_sensor(self):
        example = read((HERE / "config.ini.example").read_text())
        self.assertEqual([s for s in example.sections() if sensor_config.is_sensor(s)], [])
        self.assertEqual(sensor_config.titles(example), {})

    def test_sun_moon_icon_defaults_to_the_moon(self):
        self.assertEqual(sensor_config.sun_moon_icon(read("")), "moon")
        self.assertEqual(sensor_config.sun_moon_icon(read("[sun_moon]\nicon = sky\n")), "sky")


class Calibration(unittest.TestCase):
    def test_a_sections_calibration_overlays_the_older_one(self):
        cfg = read(NEW + "\n" + OLDER.replace("00001234 = temp_f +1", "00005678 = temp_f +9"))
        self.assertEqual(sensor_config.calibration(cfg),
                         {"00005678": "temp_f -1.5", "0000aaaa": "humidity_pct +3"})
        self.assertEqual(calibration.rules(cfg)["00005678"], {"temp_f": ("+", -1.5)})


class SetupWrites(unittest.TestCase):
    TEXT = "[device]\nwrite_path = ~/AcuRite\n\n[sensors]\n; names\n00001234 = Old\n\n" \
           "[sensor_order]\n1 = 00001234\n\n[00005678]\ntitle = Kitchen\nicon = 🍳\n\n[00001234]\n" \
           "link = outdoor\n\n[0000ABCD]\ntitle = Shed\n"

    def test_each_its_own_section_in_order(self):
        out = wizard.write_sensors(self.TEXT, {"00001234": "Yard", "0000abcd": "Shed", "00005678": "Kitchen"})
        cfg = read(out)
        self.assertEqual(sensor_config.titles(cfg), {"00001234": "Yard", "0000abcd": "Shed", "00005678": "Kitchen"})
        self.assertEqual(dict(cfg["sensors"]), {})
        self.assertEqual(dict(cfg["sensor_order"]), {})
        self.assertIn("; names", out)
        # What setup doesn't ask about stays with its sensor; a section keeps its spelling.
        self.assertEqual(sensor_config.cards(cfg), {"00001234": {"link": "outdoor"}, "00005678": {"icon": "🍳"}})
        self.assertEqual(out.count("[0000ABCD]"), 1)
        self.assertNotIn("[0000abcd]", out)

    def test_a_sensor_left_off_is_removed_and_a_second_save_changes_nothing(self):
        once = wizard.write_sensors(self.TEXT, {"00005678": "Kitchen"})
        self.assertNotIn("[00001234]", once)
        self.assertNotIn("[0000ABCD]", once)
        self.assertEqual(sensor_config.titles(read(once)), {"00005678": "Kitchen"})
        self.assertEqual(wizard.write_sensors(once, {"00005678": "Kitchen"}), once)

    def test_a_setting_is_not_a_sensor_id(self):
        example = (HERE / "config.ini.example").read_text()
        with self.assertRaises(ValueError):
            wizard.apply_setup(example, {"sensors": [{"id": "web", "name": "x"}]})

    def test_missing_sections_never_adds_a_sensor(self):
        added, names = wizard.missing_sections("[device]\n", "[device]\n\n[00001234]\ntitle = Yard\n\n[sun_moon]\nicon = moon\n")
        self.assertEqual(names, ["sun_moon"])
        self.assertNotIn("00001234", added)


class HeardSensor(unittest.TestCase):
    def test_a_new_sensor_gets_a_bare_section_titled_by_its_id(self):
        cap = capture()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.ini"
            path.write_text(NEW)
            cap.add_heard_sensor(path, "00004321", "Tower")
            text = path.read_text()
        cfg = read(text)
        self.assertEqual(sensor_config.titles(cfg)["00004321"], "00004321")
        self.assertEqual(list(sensor_config.titles(cfg))[-1], "00004321")
        self.assertIn("A Tower sensor, first heard", text)
        self.assertTrue(text.startswith(NEW))


class PagesAgree(unittest.TestCase):
    """The names config.ini takes are the ones the pages know."""

    def test_live_icons(self):
        page = (HERE / "weather.html").read_text()
        block = re.search(r"const LIVE_ICONS = \{(.*?)\n\};", page, re.S).group(1)
        self.assertEqual(set(re.findall(r"^\s*(\w+):", block, re.M)), set(sensor_config.LIVE_ICONS))
        # The outdoor card's own icon stays the sun by day and the moon by night.
        self.assertIn("dressOf(s, data).icon || 'sky'", page)
        self.assertIn("sky:  sk => sk ? (sk.up ? '☀️' : sk.moon.emoji) : ''", page)

    def test_links(self):
        page = (HERE / "charts.html").read_text()
        self.assertEqual(set(re.findall(r'\.dataset\.chart = "(\w+)"', page)), set(sensor_config.LINKS) - {"none"})


if __name__ == "__main__":
    unittest.main()
