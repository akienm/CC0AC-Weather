"""1.4.8: private values kept out of config.ini (private.py).

    smtp_password = file:secrets.env#ALERTS_SMTP_PASSWORD

points into secrets.env beside config.ini. The setup page writes what it saves that is
private there; diagnostics and the backup follow the reference.
python3 -m unittest tests/test_1_4_8.py"""

import configparser
import http.server
import io
import json
import os
import stat
import sys
import tarfile
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from cc0ac import config, diagnostics, private, upkeep, web   # noqa: E402


class World(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.root = Path(self._d.name)
        self.config = self.root / "config.ini"
        self.secrets = self.root / "secrets.env"

    def tearDown(self):
        self._d.cleanup()

    def load(self, text: str) -> configparser.ConfigParser:
        self.config.write_text(text)
        return config.load_config(self.config)


class References(World):
    def test_a_reference_reads_the_line_it_points_at(self):
        self.secrets.write_text("# mine\nSMTP=hunter2\nexport TOPIC = 'weather-abc'\nPCT=100%sure\n")
        os.chmod(self.secrets, 0o600)
        other = self.root / "elsewhere.env"
        other.write_text("KEY=abs-value\n")
        os.chmod(other, 0o600)
        cfg = self.load("[alerts]\nsmtp_password = file:secrets.env#SMTP\nntfy_topic = file:secrets.env#TOPIC\n"
                        f"email_to = file://{other}#KEY\n\n[backup]\npassword = file:secrets.env#PCT\n")
        self.assertEqual(cfg.get("alerts", "smtp_password"), "hunter2")
        self.assertEqual(cfg.get("alerts", "ntfy_topic"), "weather-abc")      # export and quotes taken off
        self.assertEqual(cfg.get("alerts", "email_to"), "abs-value")         # file:///absolute
        self.assertEqual(cfg.get("backup", "password"), "100%sure")          # a % from the file is just a %
        self.assertEqual(private.get_raw(cfg, "backup", "password"), "100%sure")

    def test_a_plain_value_and_interpolation_work_as_before(self):
        cfg = self.load("[device]\nwrite_path = /x\n\n[storage]\ndatabase = %(here)s/w.db\nhere = /y\n\n"
                        "[alerts]\nsmtp_password = plain\n")
        self.assertEqual(cfg.get("alerts", "smtp_password"), "plain")
        self.assertEqual(cfg.get("storage", "database"), "/y/w.db")

    def test_a_reference_that_points_at_nothing_is_loud_and_never_shows_a_value(self):
        self.secrets.write_text("OTHER=s3cret-other\n")
        os.chmod(self.secrets, 0o600)
        with self.assertLogs("acurite", "ERROR") as logs:
            cfg = self.load("[alerts]\nsmtp_password = file:secrets.env#SMTP\nntfy_topic = file:nope.env#T\n")
        said = "\n".join(logs.output)
        self.assertIn("file:secrets.env#SMTP", said)
        self.assertIn("file:nope.env#T", said)
        self.assertNotIn("s3cret-other", said)
        with self.assertLogs("acurite", "ERROR"):
            self.assertEqual(cfg.get("alerts", "smtp_password"), "")
        refused = config.check_config(self.config.read_text(), self.config)
        self.assertTrue(refused and refused.startswith("Not saved:") and "SMTP" in refused, refused)
        self.assertIsNone(config.check_config("[alerts]\nsmtp_password = file:secrets.env#OTHER\n", self.config))

    @unittest.skipUnless(os.name == "posix", "file modes")
    def test_a_secrets_file_others_can_read_is_warned_about_once(self):
        self.secrets.write_text("SMTP=x\n")
        os.chmod(self.secrets, 0o644)
        private._warned.clear()
        with self.assertLogs("acurite", "WARNING") as logs:
            cfg = self.load("[alerts]\nsmtp_password = file:secrets.env#SMTP\n")
            cfg.get("alerts", "smtp_password")
            cfg.get("alerts", "smtp_password")
        self.assertEqual(sum("chmod 600" in line for line in logs.output), 1)


FILLED = """[station]
name = Home
latitude = 32.123456
longitude = -108.654321

[web]
enabled = true
key = ~/.cc0ac-weather/tls/key.pem

[backup]
path = //nas/share
user = bob
password = 50%%off%(x)s

[alerts]
enabled = true
ntfy_topic = weather-secret-topic
email_to = bob@example.com
smtp_password = pa%%ss
smtp_user =
healthcheck_url = file:secrets.env#ALREADY

[weather_underground]
station_id = KXX1234
station_key = wu-key-9999
"""


class Moving(World):
    def test_private_values_move_and_the_station_reads_the_same(self):
        self.secrets.write_text("ALREADY=https://hc-ping.com/abc\n")
        os.chmod(self.secrets, 0o600)
        self.config.write_text(FILLED)
        before = config.load_config(self.config)
        moved = private.move_private(FILLED, self.config)
        for secret in ("32.123456", "-108.654321", "bob@example.com", "weather-secret-topic", "50%%off",
                       "pa%%ss", "KXX1234", "wu-key-9999", "user = bob"):
            self.assertNotIn(secret, moved)
        self.assertIn("key = ~/.cc0ac-weather/tls/key.pem", moved)            # a path, not a secret
        self.assertIn("healthcheck_url = file:secrets.env#ALREADY", moved)    # a reference stays
        self.assertIn("smtp_user =\n", moved)                                # blank stays blank
        self.assertIn("ntfy_topic = file:secrets.env#ALERTS_NTFY_TOPIC", moved)
        self.assertEqual(stat.S_IMODE(self.secrets.stat().st_mode), 0o600)
        self.config.write_text(moved)
        after = config.load_config(self.config)
        for section, option in (("station", "latitude"), ("alerts", "ntfy_topic"), ("alerts", "smtp_password"),
                                ("alerts", "email_to"), ("alerts", "healthcheck_url"),
                                ("weather_underground", "station_key"), ("backup", "user")):
            self.assertEqual(after.get(section, option), before.get(section, option), (section, option))
        self.assertEqual(after.get("alerts", "smtp_password"), "pa%ss")       # the %% the station read as %
        self.assertEqual(private.get_raw(after, "backup", "password"), "50%%off%(x)s")   # read raw, moved raw
        self.assertIn("ALREADY=https://hc-ping.com/abc", self.secrets.read_text())       # its other lines kept
        self.assertEqual(private.move_private(moved, self.config), moved)               # a second time: nothing

    def test_a_new_value_replaces_its_line(self):
        private.write_values(self.secrets, {"A": "1", "B": "2"})
        private.write_values(self.secrets, {"A": "3"})
        self.assertEqual(private.read_file(self.secrets), {"A": "3", "B": "2"})


class SetupPage(World):
    def test_a_setup_save_leaves_only_references_in_config_ini(self):
        self.config.write_text(f"[device]\nwrite_path = {self.root}\n\n[web]\nenabled = true\n")
        handler = web._make_web_handler(self.root, config_path=self.config, mcp=False)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        answers = {"station": {"name": "Home", "latitude": "32.5", "longitude": "-108.25"},
                   "alerts": {"ntfy_topic": "weather-page-topic", "email_to": "me@example.com"}}
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/setup.cgi",
                                     data=json.dumps(answers).encode(), method="POST",
                                     headers={"X-CC0AC-Config": "save"})
        with mock.patch.object(web, "restart_soon"), mock.patch.object(web.alerts, "mark_restart"):
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertEqual(r.status, 200)
        text = self.config.read_text()
        for secret in ("32.5", "-108.25", "weather-page-topic", "me@example.com"):
            self.assertNotIn(secret, text)
        cfg = config.load_config(self.config)
        self.assertEqual(cfg.get("alerts", "ntfy_topic"), "weather-page-topic")
        self.assertEqual(cfg.get("station", "latitude"), "32.5")


class Diagnostics(World):
    def test_a_secret_held_by_reference_is_still_scrubbed_from_the_bundle(self):
        self.secrets.write_text("T=weather-very-private\n")
        os.chmod(self.secrets, 0o600)
        cfg = self.load(f"[device]\nwrite_path = {self.root}\n\n[alerts]\nntfy_topic = file:secrets.env#T\n")
        with mock.patch.object(diagnostics, "CONFIG_DIR", self.root), \
                mock.patch.object(diagnostics, "service_log", return_value="sent to ntfy.sh/weather-very-private\n"), \
                mock.patch.object(diagnostics, "info", return_value=""):
            path = diagnostics.bundle(cfg, self.root)
        with tarfile.open(path) as tar:
            log = tar.extractfile("cc0ac-diagnostics/service-log.txt").read().decode()
        self.assertNotIn("weather-very-private", log)
        self.assertIn("****", log)


class Backup(World):
    def test_the_backup_carries_the_secrets_file(self):
        self.secrets.write_text("T=x\n")
        cfg = self.load(f"[device]\nwrite_path = {self.root}\n\n[alerts]\nntfy_topic = file:secrets.env#T\n")
        out = upkeep.snapshot(cfg, self.config, self.root)
        with tarfile.open(out) as tar:
            names = tar.getnames()
        self.assertIn("cc0ac-weather-backup/secrets.env", names)
        self.assertIn("cc0ac-weather-backup/config.ini", names)


if __name__ == "__main__":
    unittest.main()
