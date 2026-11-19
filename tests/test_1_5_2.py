"""1.5.2: a diagnostics file shows the alert rules, when banners came and went, and in Docker the log."""

import configparser
import json
import logging
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from cc0ac import diagnostics   # noqa: E402


class Bundle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.conf, self.out = root / "conf", root / "out"
        self.conf.mkdir(), self.out.mkdir()
        self.saved = diagnostics.CONFIG_DIR, diagnostics.in_docker
        diagnostics.CONFIG_DIR = self.conf
        self.cfg = configparser.ConfigParser()
        self.cfg.read_dict({"device": {"write_path": str(root / "readings")},
                            "00001234": {"title": "Attic"},
                            "alert_rules": {"station": "service | any | any | 00:05:00 | new | dashboard",
                                            "attic battery": "battery | Attic | any",
                                            "broken": "thunder"}})
        (root / "readings").mkdir()
        (root / "readings" / "alerts.json").write_text(json.dumps({"alerts": [
            {"row": "weather service", "kind": "nws", "title": "Tornado Warning for Nowhere County",
             "message": "take cover", "since": 1800000000.0}]}))
        (self.conf / "alert-state.json").write_text(json.dumps({
            "events": [{"row": "station", "title": "Station started", "message": "1.5.2", "at": 1800000000.0}],
            "rules": {"attic battery": {"kind": "battery", "on": {"00001234": {"so": True, "label": "Attic",
                                                                                "message": "Attic battery low"}}}}}))

    def tearDown(self):
        diagnostics.CONFIG_DIR, diagnostics.in_docker = self.saved
        for h in list(logging.getLogger().handlers):
            if getattr(h, "baseFilename", "").startswith(self.tmp.name):
                logging.getLogger().removeHandler(h)
                h.close()
        self.tmp.cleanup()

    def parts(self):
        with tarfile.open(diagnostics.bundle(self.cfg, self.out)) as tar:
            return {m.name.split("/", 1)[1]: tar.extractfile(m).read().decode() for m in tar.getmembers()}

    def test_the_rules_as_read_and_the_ones_left_out(self):
        rules = self.parts()["alert-rules.txt"]
        self.assertIn("station = service | any | any | 00:05:00 | new | dashboard", rules)
        self.assertIn("left out: broken: 'thunder' isn't a kind", rules)
        self.assertNotIn("Attic", rules)                     # a sensor's name is still ****

    def test_when_banners_came_and_went_but_not_their_words(self):
        p = self.parts()
        banners, state = json.loads(p["alerts.json"]), json.loads(p["alert-state.json"])
        self.assertEqual(banners["alerts"], [{"row": "weather service", "kind": "nws", "since": 1800000000.0}])
        self.assertEqual(state["events"], [{"row": "station", "at": 1800000000.0}])
        self.assertEqual(state["rules"]["attic battery"]["on"]["00001234"], {"so": True})
        for words in ("Nowhere", "take cover", "Attic battery low"):
            self.assertNotIn(words, p["alerts.json"] + p["alert-state.json"])

    def test_in_docker_the_station_keeps_its_log_for_the_file(self):
        diagnostics.in_docker = lambda: True
        diagnostics.keep_docker_log(self.conf)
        diagnostics.keep_docker_log(self.conf)               # once, however often asked
        logging.getLogger().setLevel(logging.INFO)
        logging.getLogger("cc0ac.test").error("[alert_rules] row left out: broken")
        mine = [h for h in logging.getLogger().handlers if getattr(h, "baseFilename", "").startswith(self.tmp.name)]
        self.assertEqual(len(mine), 1)
        mine[0].flush()
        self.assertIn("row left out: broken", self.parts()["service-log.txt"])

    def test_outside_docker_no_log_is_kept(self):
        diagnostics.in_docker = lambda: False
        self.assertFalse((self.conf / diagnostics.DOCKER_LOG).exists())


if __name__ == "__main__":
    unittest.main()
