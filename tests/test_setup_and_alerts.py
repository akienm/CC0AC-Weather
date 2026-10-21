"""Checks for the setup page's helpers and the alert check: python3 -m unittest tests/test_setup_and_alerts.py"""
import configparser
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import alerts   # noqa: E402
import wizard   # noqa: E402

HERE = Path(__file__).resolve().parent.parent
HUB_PAGE = """<script>$('txtVer').value = '051';
$('txtMac').value = '24C86E000001';
$('txtSer').value = 'atlasapi.myacurite.com';
$('txtID').value = 'KXXTEST1';
$('txtPS').value = 'secret-never-read';
$('txtDev').value = '00000505';
$('txtEle').value = '6031';</script>
<p>PRESSURE: 24.21 Inch</p>
<table><tbody><tr><td># </td><td>Type      </td><td>Id      </td><td>Last Time (UTC)   </td><td>Signal</td><td>Battery</td></tr>
<tr><td>0</td><td>tower</td><td>00005358</td><td>2026-10-12T17:27:03</td><td>4</td><td>Normal</td></tr>
           <tr><td>1</td><td>Atlas</td><td>00000505</td><td>2026-10-12T17:27:08</td><td>3</td><td>Low</td></tr>
</tbody></table>"""


class HubPage(unittest.TestCase):
    def test_reads_settings_and_sensors_never_the_password(self):
        hub = wizard.parse_hub_page(HUB_PAGE)
        self.assertEqual(hub["hub_id"], "24C86E000001")
        self.assertEqual(hub["wu_station"], "KXXTEST1")
        self.assertEqual(hub["elevation_ft"], "6031")
        self.assertEqual(hub["pressure_inhg"], 24.21)
        self.assertEqual([(s["type"], s["id"], s["battery"]) for s in hub["sensors"]],
                         [("tower", "00005358", "Normal"), ("Atlas", "00000505", "Low")])
        self.assertNotIn("secret-never-read", json.dumps(hub))

    def test_leases_newest_first(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "leases"
            p.write_text("100 aa:bb 192.168.77.50 old *\n200 cc:dd 192.168.77.53 W550-0B0001 01:cc\n")
            self.assertEqual([l["ip"] for l in wizard.hub_addresses(p)], ["192.168.77.53", "192.168.77.50"])
            self.assertFalse(wizard.find_hub(Path(d) / "none")["found"])


class ConfigEditing(unittest.TestCase):
    def setUp(self):
        self.text = (HERE / "config.ini.example").read_text()

    def answers(self, **kw):
        a = {"sensors": [{"id": "00005358", "name": "Kitchen"}, {"id": "00000505", "name": "Outside"}],
             "station": {"name": "Test Station", "latitude": "32.5", "longitude": "-108.25", "elevation_ft": "6031"},
             "forecast": True, "alerts": {"ntfy_topic": "weather-abc", "quiet_minutes": "30", "smtp_password": ""}}
        a.update(kw)
        return a

    def test_answers_land_and_comments_stay(self):
        out = wizard.apply_setup(self.text, self.answers())
        cfg = configparser.ConfigParser()
        cfg.read_string(out)
        self.assertEqual(dict(cfg["sensors"]), {"00005358": "Kitchen", "00000505": "Outside"})
        self.assertEqual(dict(cfg["sensor_order"]), {"1": "00005358", "2": "00000505"})
        self.assertEqual(cfg.get("station", "latitude"), "32.5")
        self.assertEqual(cfg.get("station", "name"), "Test Station")
        self.assertTrue(cfg.getboolean("forecast", "enabled"))
        self.assertTrue(cfg.getboolean("alerts", "enabled"))
        self.assertEqual(cfg.get("alerts", "ntfy_topic"), "weather-abc")
        # Every comment line of the example is still there.
        comments = [l for l in self.text.splitlines() if l.lstrip().startswith(";")]
        self.assertEqual([l for l in out.splitlines() if l.lstrip().startswith(";")], comments)
        # Untouched settings are untouched.
        self.assertEqual(cfg.get("hub", "port"), "443")

    def test_second_save_replaces_not_appends(self):
        once = wizard.apply_setup(self.text, self.answers())
        twice = wizard.apply_setup(once, self.answers(sensors=[{"id": "00000505", "name": "Yard"}]))
        cfg = configparser.ConfigParser()
        cfg.read_string(twice)
        self.assertEqual(dict(cfg["sensors"]), {"00000505": "Yard"})
        self.assertEqual(dict(cfg["sensor_order"]), {"1": "00000505"})
        self.assertEqual(twice.count("[alerts]"), 1)
        self.assertEqual(wizard.apply_setup(twice, self.answers(sensors=[{"id": "00000505", "name": "Yard"}])), twice)

    def test_blank_password_keeps_the_saved_one(self):
        once = wizard.apply_setup(self.text, self.answers(alerts={"email_to": "a@b.c", "smtp_password": "pw1"}))
        again = wizard.apply_setup(once, self.answers(alerts={"email_to": "a@b.c", "smtp_password": ""}))
        cfg = configparser.ConfigParser()
        cfg.read_string(again)
        self.assertEqual(cfg.get("alerts", "smtp_password"), "pw1")

    def test_refuses_bad_values(self):
        for bad in ({"sensors": [{"id": "x y\n[evil]", "name": "n"}]},
                    {"station": {"latitude": "123", "longitude": "0"}}):
            with self.assertRaises(ValueError):
                wizard.apply_setup(self.text, self.answers(**bad))
        out = wizard.apply_setup(self.text, self.answers(sensors=[{"id": "1", "name": "a\n[evil]\nx=1"}]))
        self.assertNotIn("\n[evil]", out)

    def test_needs_setup(self):
        cfg = configparser.ConfigParser()
        cfg.read_string(self.text)
        self.assertTrue(wizard.needs_setup(cfg))
        cfg.read_string(wizard.apply_setup(self.text, self.answers()))
        self.assertFalse(wizard.needs_setup(cfg))

    def test_internet_weather_alone_is_a_finished_setup(self):
        out = wizard.apply_setup(self.text, self.answers(sensors=[], online=True))
        cfg = configparser.ConfigParser()
        cfg.read_string(out)
        self.assertTrue(cfg.getboolean("online", "enabled"))
        self.assertFalse(wizard.needs_setup(cfg))

    def test_the_updates_answer(self):
        cfg = configparser.ConfigParser()
        cfg.read_string(wizard.apply_setup(self.text, self.answers(updates={"automatic": False, "time": "5:30"})))
        self.assertEqual((cfg.get("updates", "automatic"), cfg.get("updates", "time")), ("no", "05:30"))
        with self.assertRaises(ValueError):
            wizard.apply_setup(self.text, self.answers(updates={"automatic": True, "time": "25:00"}))

    def test_no_hub_means_no_looking_for_one(self):
        cfg = configparser.ConfigParser()
        cfg.read_string(wizard.set_values(self.text, "hub", {"enabled": "false"}))
        state = wizard.setup_state(cfg, leases=Path("/nonexistent"))
        self.assertTrue(state["hub"]["off"])
        self.assertTrue(state["current"]["updates"]["automatic"])


class AlertCheck(unittest.TestCase):
    def test_one_message_going_quiet_one_coming_back(self):
        sent = []
        real = alerts.send
        alerts.send = lambda cfg, title, msg: sent.append(title) or []
        try:
            with tempfile.TemporaryDirectory() as d:
                d = Path(d)
                cfg = configparser.ConfigParser()
                cfg.read_string("[alerts]\nenabled = true\nquiet_minutes = 30\nntfy_topic = t\n[station]\nname = S\n")
                state = d / "state.json"
                t0 = time.time()

                def reading_at(epoch):
                    from datetime import datetime, timezone
                    (d / "current.json").write_text(json.dumps(
                        {"written": datetime.fromtimestamp(epoch, timezone.utc).isoformat()}))

                self.assertIn("no reading", alerts.check(cfg, d, t0, state))
                reading_at(t0)
                self.assertEqual(alerts.check(cfg, d, t0 + 60, state), "fresh")
                self.assertEqual(alerts.check(cfg, d, t0 + 31 * 60, state), "alerted")
                self.assertEqual(alerts.check(cfg, d, t0 + 40 * 60, state), "still quiet")
                reading_at(t0 + 45 * 60)
                self.assertEqual(alerts.check(cfg, d, t0 + 46 * 60, state), "all clear sent")
                self.assertEqual(alerts.check(cfg, d, t0 + 47 * 60, state), "fresh")
                self.assertEqual(sent, ["S: no readings", "S: readings are back"])
        finally:
            alerts.send = real

    def test_a_failed_send_tries_again(self):
        calls = []
        real = alerts.send
        alerts.send = lambda cfg, title, msg: calls.append(title) or ["ntfy: down"]
        try:
            with tempfile.TemporaryDirectory() as d:
                d = Path(d)
                (d / "current.json").write_text(json.dumps({"written": "2026-01-01T00:00:00+00:00"}))
                cfg = configparser.ConfigParser()
                cfg.read_string("[alerts]\nenabled = true\nntfy_topic = t\n")
                alerts.check(cfg, d, None, d / "s.json")
                alerts.check(cfg, d, None, d / "s.json")
                self.assertEqual(len(calls), 2)
        finally:
            alerts.send = real

    def test_off_by_default(self):
        cfg = configparser.ConfigParser()
        self.assertEqual(alerts.check(cfg, Path("/nonexistent")), "alerts off")


if __name__ == "__main__":
    unittest.main()
