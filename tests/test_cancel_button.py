"""1.5.3: a dashboard banner has a cancel button (✕, upper right). Cancelling takes
the banner off every screen; the alert itself, and its "fine again", are untouched."""

import configparser
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from cc0ac import alerts, web   # noqa: E402

NOW = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc).timestamp()


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Cancel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "alert-state.json"
        self.sent = []
        self.real = alerts.send
        alerts.send = lambda cfg, title, message, targets=None: self.sent.append(title) or []
        self.cfg = configparser.ConfigParser()
        self.cfg.read_dict({"device": {"write_path": str(self.root)}, "station": {"name": "Home"},
                            "alerts": {"enabled": "true"},
                            "alert_rules": {"station": "service | any | any | once | new | ntfy, dashboard",
                                            "frost": "frost | any | 35 | until clears | over | ntfy, dashboard"}})

    def tearDown(self):
        alerts.send = self.real
        self.tmp.cleanup()

    def banners(self):
        return json.loads((self.root / "alerts.json").read_text())["alerts"]

    def yard(self, temp_f):
        (self.root / "current.json").write_text(json.dumps({"sensors": [
            {"sensor_id": "1", "name": "Yard", "type": "Atlas", "updated": iso(NOW), "fields": {"temp_f": temp_f}}]}))

    def test_a_station_started_banner_cancels(self):
        alerts._say_service(self.cfg, "Station started", "1.5.3", self.state, NOW)
        [banner] = self.banners()
        self.assertTrue(banner["id"])
        alerts.dismiss(self.cfg, banner["id"], self.state, NOW + 60)
        self.assertEqual(self.banners(), [])

    def test_cancelling_one_leaves_the_others(self):
        alerts._say_service(self.cfg, "Station stopped", "", self.state, NOW)
        alerts._say_service(self.cfg, "Station started", "1.5.3", self.state, NOW + 30)
        first, second = self.banners()
        alerts.dismiss(self.cfg, first["id"], self.state, NOW + 60)
        self.assertEqual([b["title"] for b in self.banners()], ["Station started"])

    def test_a_cancelled_alert_still_stands_and_still_clears(self):
        self.yard(30)
        alerts.run_rules(self.cfg, self.root, ("frost",), NOW, self.state)
        [banner] = self.banners()
        alerts.dismiss(self.cfg, banner["id"], self.state, NOW + 30)
        self.assertEqual(self.banners(), [])
        alerts.run_rules(self.cfg, self.root, ("frost",), NOW + 60, self.state)
        self.assertEqual(self.banners(), [], "the next check doesn't bring it back")
        self.assertEqual(len(self.sent), 1, "nor send it again")
        self.yard(50)
        alerts.run_rules(self.cfg, self.root, ("frost",), NOW + 120, self.state)
        self.assertEqual(len(self.sent), 2, "its 'fine again' still goes")

    def test_the_same_alert_starting_again_is_a_new_banner(self):
        self.yard(30)
        alerts.run_rules(self.cfg, self.root, ("frost",), NOW, self.state)
        alerts.dismiss(self.cfg, self.banners()[0]["id"], self.state, NOW + 30)
        self.yard(50)
        alerts.run_rules(self.cfg, self.root, ("frost",), NOW + 60, self.state)
        self.yard(30)
        alerts.run_rules(self.cfg, self.root, ("frost",), NOW + 120, self.state)
        self.assertEqual(len(self.banners()), 1)

    def test_old_cancels_are_forgotten(self):
        alerts.dismiss(self.cfg, "x|event|1", self.state, NOW)
        alerts.dismiss(self.cfg, "y|event|2", self.state, NOW + alerts.DISMISSED_KEPT_S + 1)
        self.assertEqual(list(json.loads(self.state.read_text())["dismissed"]), ["y|event|2"])


class Endpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.real_state, alerts.STATE_FILE = alerts.STATE_FILE, home / "alert-state.json"
        self.real_send, alerts.send = alerts.send, lambda *a, **k: []
        cfg_path = home / "config.ini"
        cfg_path.write_text("[station]\nname = Test\n")
        self.cfg = configparser.ConfigParser()
        self.cfg.read_dict({"device": {"write_path": str(home)}, "alerts": {"enabled": "true"},
                            "alert_rules": {"station": "service | any | any | once | new | dashboard"}})
        alerts._say_service(self.cfg, "Station started", "1.5.3", alerts.STATE_FILE, NOW)
        self.home = home
        handler = web._make_web_handler(home, home / "w.db", {}, config_path=cfg_path, cfg=self.cfg, mcp=False)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/dismiss.cgi"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        alerts.STATE_FILE, alerts.send = self.real_state, self.real_send
        self.tmp.cleanup()

    def post(self, body, header=True):
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"X-CC0AC-Config": "save"} if header else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_the_button_cancels_through_the_station(self):
        banner_id = json.loads((self.home / "alerts.json").read_text())["alerts"][0]["id"].encode()
        self.assertEqual(self.post(banner_id, header=False), 403, "a form on another site can't")
        self.assertEqual(self.post(b""), 400)
        self.assertEqual(self.post(banner_id), 200)
        self.assertEqual(json.loads((self.home / "alerts.json").read_text())["alerts"], [])

    def test_the_page_has_the_button(self):
        page = (HERE / "weather.html").read_text()
        self.assertIn('class="cancel"', page)
        self.assertIn("dismiss.cgi", page)


if __name__ == "__main__":
    unittest.main()
