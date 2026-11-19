"""1.5.1: [alert_rules] is a setting, not a sensor; a Station row says how long its banner stays."""

import configparser
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from cc0ac import alert_rules, alerts, private, sensor_config, wizard   # noqa: E402

EXAMPLE = (HERE / "config.ini.example").read_text()
NOW = 1_800_000_000.0
RULES = "\n[alert_rules]\nstation = service | any | any | once | new | dashboard\nfrost = frost\n"


def parsed(text):
    cfg = configparser.ConfigParser()
    cfg.read_string(text)
    return cfg


class NotASensor(unittest.TestCase):
    def test_every_section_the_example_names_is_a_setting(self):
        named = {s.lower() for s in re.findall(r"^;?\s*\[([a-z_]+)\]\s*$", EXAMPLE, re.M)}
        self.assertEqual(named - sensor_config.SETTINGS, set())

    def test_the_setup_page_saves_beside_an_alert_rules_section(self):
        text = EXAMPLE + RULES
        cfg = parsed(text)
        self.assertNotIn("alert_rules", sensor_config.table(cfg))
        sensors = [{"id": sid, "name": name} for sid, name in sensor_config.titles(cfg).items()]
        out = wizard.apply_setup(text, {"sensors": sensors + [{"id": "00001234", "name": "Yard"}],
                                        "backup": {"path": "/mnt/backup"}})
        self.assertEqual(parsed(out).get("alert_rules", "frost"), "frost")   # 1.5.0 deleted the section
        self.assertNotIn("alert_rules", sensor_config.titles(cfg))           # 1.5.0's page listed it as a sensor


class NotASecret(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name) / "config.ini"

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_row_named_like_a_private_setting_stays_where_it_is(self):
        text = "[alerts]\nntfy_topic = mine\n" + RULES + "key = battery\n"
        out = private.move_private(text, self.config)
        cfg = parsed(out)
        self.assertTrue(private.is_reference(cfg.get("alerts", "ntfy_topic")))    # still moved
        self.assertEqual(cfg.get("alert_rules", "station"), "service | any | any | once | new | dashboard")
        self.assertEqual(cfg.get("alert_rules", "key"), "battery")

    def test_a_row_moved_into_secrets_is_read_back_through_its_reference(self):
        (self.config.parent / private.SECRETS_FILE).write_text(
            "ALERT_RULES_STATION=service | any | any | 00:05:00 | new | dashboard\n")
        self.config.write_text("[alert_rules]\nstation = file:secrets.env#ALERT_RULES_STATION\nfrost = frost\n")
        cfg = private.parser(self.config)
        cfg.read(self.config)
        rows = {r.name: r for r in alert_rules.rules(cfg)}
        self.assertEqual((rows["station"].kind, rows["station"].stays), ("service", 300.0))
        self.assertEqual(alert_rules.problems(cfg), [])
        state = alert_rules.page_state(cfg)
        self.assertEqual(state["rows"][0]["stays"], "00:05:00")
        out = alert_rules.apply_rows(self.config.read_text(), state["rows"])   # the page's next save
        self.assertEqual(parsed(out).get("alert_rules", "station"), "service | any | any | 00:05:00 | new | dashboard")


class StationBanner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state.json"
        self.real = alerts.send
        alerts.send = lambda *a, **k: []

    def tearDown(self):
        alerts.send = self.real
        self.tmp.cleanup()

    def banners(self, stays, age):
        cfg = configparser.ConfigParser()
        cfg.read_dict({"device": {"write_path": str(self.root)}, "alerts": {"enabled": "true"},
                       "alert_rules": {"station": f"service | any | any | {stays} | new | dashboard"}})
        self.state.unlink(missing_ok=True)
        alerts._say_service(cfg, "Station started", "1.5.1", self.state, NOW - age)
        alerts.check_upkeep(cfg, [], self.state, self.root, NOW)
        return json.loads((self.root / "alerts.json").read_text())["alerts"]

    def test_once_is_an_hour(self):
        self.assertEqual(len(self.banners("once", 59 * 60)), 1)
        self.assertEqual(self.banners("once", 61 * 60), [])

    def test_a_time_is_that_long(self):
        self.assertEqual(len(self.banners("00:05:00", 4 * 60)), 1)
        self.assertEqual(self.banners("00:05:00", 6 * 60), [])

    def test_the_row_keeps_what_was_written(self):
        r = alert_rules.parse("station", "service | any | any | 00:05:00 | new | dashboard")
        self.assertEqual((r.stays, r.over), (300.0, False))
        self.assertEqual(alert_rules.show(r), "service | any | any | 00:05:00 | new | dashboard")
        self.assertIsNone(alert_rules.parse("station", "service").stays)

    def test_until_clears_is_refused_not_changed(self):
        with self.assertRaises(ValueError) as e:
            alert_rules.parse("station", "service | any | any | until clears")
        self.assertIn("said once", str(e.exception))


if __name__ == "__main__":
    unittest.main()
