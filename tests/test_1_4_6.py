"""1.4.6: an email_to that isn't an address is ignored, the reports as HTML email, the
station's own notices (started, stopped, a sensor quiet, a reading held back), the hub's
"Working: last heard", and Update now.
python3 -m unittest tests/test_1_4_6.py"""
import configparser
import email
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "updates"))
import bootloader as bl   # noqa: E402
from cc0ac import alerts, release, reports, store, web, wizard   # noqa: E402

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc).timestamp()


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def config(**sections):
    cfg = configparser.ConfigParser()
    cfg.read_dict({"station": {"name": "Test Station"}, **sections})
    return cfg


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "alert-state.json"
        self.sent = []
        self.real_send = alerts.send
        alerts.send = lambda cfg, title, message: self.sent.append((title, message)) or []

    def tearDown(self):
        alerts.send = self.real_send
        self.tmp.cleanup()

    def current(self, sensors, written=NOW):
        (self.root / "current.json").write_text(json.dumps({"written": iso(written), "sensors": sensors}))


class EmailTo(unittest.TestCase):
    def test_a_note_pasted_after_the_equals_is_ignored_and_logged(self):
        cfg = config(reports={"email_to": "(blank uses your alerts address)"}, alerts={"email_to": "me@example.com"})
        with self.assertLogs("acurite", "WARNING") as said:
            self.assertEqual(alerts.addresses(cfg, "reports"), "")
        self.assertIn("isn't an email address", said.output[0])
        self.assertEqual(alerts.addresses(cfg, "alerts"), "me@example.com")

    def test_addresses_among_nonsense_are_kept(self):
        cfg = config(alerts={"email_to": "a@example.com, nobody, b@example.org"})
        with self.assertLogs("acurite", "WARNING"):
            self.assertEqual(alerts.addresses(cfg), "a@example.com, b@example.org")

    def test_a_bad_reports_address_falls_back_to_the_alerts_one(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            db = root / "weather.db"
            store.open_db(db).close()
            sent = []
            real = alerts._email
            alerts._email = lambda cfg, to, title, text, html=None: sent.append(to)
            try:
                cfg = config(device={"write_path": d}, alerts={"email_to": "me@example.com"},
                             reports={"send": "daily", "email_to": "(blank uses your alerts address)"})
                with self.assertLogs("acurite", "WARNING"):
                    reports.check(cfg, db, {}, root, datetime(2026, 11, 7, 8).timestamp(), root / "s.json")
            finally:
                alerts._email = real
            self.assertEqual(sent, ["me@example.com"])


class FakeSMTP:
    got = []

    def __init__(self, *a, **kw):
        pass

    def starttls(self, **kw):
        pass

    def login(self, *a):
        pass

    def send_message(self, msg):
        FakeSMTP.got.append(msg)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class HtmlEmail(unittest.TestCase):
    def test_a_report_mails_as_html_with_the_text_inside(self):
        real = alerts.smtplib.SMTP
        alerts.smtplib.SMTP = FakeSMTP
        FakeSMTP.got = []
        try:
            alerts._email(config(alerts={"smtp_host": "mail.example.com"}), "me@example.com", "A report",
                          "plain words", "<p>rich <b>words</b></p>")
        finally:
            alerts.smtplib.SMTP = real
        msg = email.message_from_bytes(FakeSMTP.got[0].as_bytes())
        self.assertEqual(msg.get_content_type(), "multipart/alternative")
        parts = {p.get_content_type(): p.get_payload(decode=True).decode() for p in msg.walk() if not p.is_multipart()}
        self.assertIn("plain words", parts["text/plain"])
        self.assertIn("<b>words</b>", parts["text/html"])

    def test_the_report_has_an_html_table_escaped(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            db = root / "weather.db"
            con = store.open_db(db)
            con.execute("INSERT INTO readings (received_utc, sensor_id, type, query, temp_f) VALUES (?,?,?,?,?)",
                        (store.epoch_to_iso(datetime(2026, 11, 6, 12).timestamp()), "atlas1", "Atlas", "q", 50.0))
            con.commit()
            store.roll_up(con, datetime(2026, 11, 7, 8).timestamp())
            con.close()
            got = reports.report(config(device={"write_path": d}), db, {"atlas1": "Back <yard>"}, root,
                                 {"period": "day", "date": "2026-11-06"}, datetime(2026, 11, 7, 8).timestamp())
        self.assertIn("<table>", got["html"])
        self.assertIn("Back &lt;yard&gt;", got["html"])
        self.assertIn("<td>Temperature</td><td>50.0°F</td>", got["html"])
        self.assertIn("Back <yard> (Atlas): 1 of 1 days", got["text"])


class ServiceNotices(Home):
    def cfg(self, **al):
        return config(alerts={"enabled": "true", "ntfy_topic": "t", **al})

    def test_first_start_says_started(self):
        self.assertEqual(alerts.service_started(self.cfg(), "1.4.6/1", self.state, NOW), "started: told")
        self.assertEqual(self.sent[0], ("Test Station: started", "The station started (version 1.4.6)."))

    def test_a_stop_then_a_start(self):
        alerts.service_started(self.cfg(), "1.4.6/1", self.state, NOW)
        self.assertEqual(alerts.service_stopping(self.cfg(), self.state), "stopped: told")
        alerts.service_started(self.cfg(), "1.4.6/1", self.state, NOW + 60)
        self.assertEqual([t for t, _ in self.sent], ["Test Station: started", "Test Station: stopped", "Test Station: started"])
        self.assertEqual(self.sent[2][1], "The station started (version 1.4.6).")

    def test_a_start_without_a_stop_says_it_stopped_without_saying(self):
        alerts.service_started(self.cfg(), "1.4.6/1", self.state, NOW)
        alerts.service_started(self.cfg(), "1.4.6/1", self.state, NOW + 60)
        self.assertIn("stopped without saying so", self.sent[1][1])

    def test_a_new_version_is_named(self):
        alerts.service_started(self.cfg(), "1.4.5/1", self.state, NOW)
        alerts.service_stopping(self.cfg(), self.state)
        alerts.service_started(self.cfg(), "1.4.6/1", self.state, NOW + 60)
        self.assertEqual(self.sent[2][1], "The station started, now version 1.4.6 (it was 1.4.5).")

    def test_a_restart_for_saved_settings_says_nothing(self):
        alerts.service_started(self.cfg(), "1.4.6/1", self.state, NOW)
        alerts.mark_restart(self.state)
        self.assertEqual(alerts.service_stopping(self.cfg(), self.state), "stopping")
        self.assertEqual(alerts.service_started(self.cfg(), "1.4.6/1", self.state, time.time()),
                         "restarted for saved settings")
        self.assertEqual(len(self.sent), 1)
        self.assertFalse((self.root / alerts.RESTART_MARK).exists())

    def test_off_when_not_listed_or_alerts_off(self):
        self.assertEqual(alerts.service_started(self.cfg(notices="sensors"), "1.4.6/1", self.state, NOW),
                         "service notices off")
        self.assertEqual(alerts.service_started(config(), "1.4.6/1", self.state, NOW), "service notices off")
        self.assertEqual(self.sent, [])


class SensorNotices(Home):
    def cfg(self, **al):
        return config(device={"write_path": str(self.root)}, alerts={"enabled": "true", "ntfy_topic": "t", **al})

    def sensor(self, sid, name, ago_min, **extra):
        return {"sensor_id": sid, "name": name, "type": "tower", "updated": iso(NOW - ago_min * 60), "fields": {}, **extra}

    def check(self, at=NOW):
        return alerts.check_sensors(self.cfg(), self.root, at, self.state)

    def test_one_sensor_quiet_while_the_rest_report(self):
        self.current([self.sensor("a1", "Den", 1), self.sensor("b2", "Attic", 45)])
        self.assertEqual(self.check(), "sensors: told (1)")
        self.assertIn("Attic hasn't been heard since", self.sent[0][1])
        self.assertEqual(self.check(), "sensors: same as before")
        self.current([self.sensor("a1", "Den", 1), self.sensor("b2", "Attic", 2)])
        self.assertEqual(self.check(), "sensors: back (1)")
        self.assertEqual(self.sent[1], ("Test Station: sensors fine again", "Attic is being heard again."))

    def test_all_quiet_is_the_no_readings_message_not_this_one(self):
        self.current([self.sensor("a1", "Den", 45), self.sensor("b2", "Attic", 45)], written=NOW - 45 * 60)
        self.assertEqual(self.check(), "sensors: fine")

    def test_internet_weather_is_not_a_sensor_to_watch(self):
        self.current([self.sensor("a1", "Den", 1), self.sensor("open-meteo", "Online", 90)])
        self.assertEqual(self.check(), "sensors: fine")

    def test_a_reading_held_back(self):
        held = {"temp_f": {"value": 150.0, "why": "150.0°F is past anything a station reads", "since": iso(NOW)},
                "dew_point_f": {"value": 90.0, "why": "worked out from a reading held back"}}
        self.current([self.sensor("a1", "Den", 1, suspect=held)])
        self.assertEqual(self.check(), "sensors: told (1)")
        self.assertIn("Den: a reading was held back as impossible (temperature 150.0°F is past anything a station reads)",
                      self.sent[0][1])
        self.assertNotIn("dew", self.sent[0][1])
        self.current([self.sensor("a1", "Den", 1)])
        self.assertEqual(self.check(), "sensors: back (1)")
        self.assertEqual(self.sent[1][1], "Den: its readings are believed again.")


class HubHeard(Home):
    def test_working_last_heard(self):
        self.current([{"sensor_id": "00000505", "type": "Atlas",
                       "reception": {"hub": {"last": iso(NOW - 180)}, "radio": {"last": iso(NOW - 10)}}},
                      {"sensor_id": "00005358", "type": "tower", "reception": {"hub": {"last": iso(NOW - 400)}}}])
        cfg = config(device={"write_path": str(self.root)}, hub={"enabled": "true"})
        self.assertEqual(wizard.hub_heard(cfg, NOW), 3.0)
        state = wizard.setup_state(cfg, leases=self.root / "no-cable-port")
        self.assertTrue(state["hub"]["by_name"])
        self.assertIn("heard_minutes", state["hub"])

    def test_never_heard(self):
        self.current([{"sensor_id": "00000505", "type": "Atlas", "reception": {"radio": {"last": iso(NOW)}}}])
        self.assertIsNone(wizard.hub_heard(config(device={"write_path": str(self.root)}), NOW))


class UpdateNow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "root"
        self.releases = Path(self.tmp.name) / "releases"
        for tag in ("v1.4.5", "v1.4.7", "v1.10.0", "s3", "latest"):
            (self.releases / tag).mkdir(parents=True)
        self.root.mkdir()
        (self.root / "bootloader.json").write_text(json.dumps({"releases": str(self.releases),
                                                               "home": str(Path(self.tmp.name) / "home")}))
        (self.root / "state.json").write_text(json.dumps({"current": "1.4.6"}))
        release._cache.clear()

    def tearDown(self):
        release._cache.clear()
        self.tmp.cleanup()

    def test_a_newer_version_is_available(self):
        got = release.newest(self.root, "1.4.6/1", NOW)
        self.assertEqual((got["newest"], got["available"], got["current"]), ("1.10.0", True, "1.4.6"))
        self.assertFalse(got["can_update"])        # no boot loader running
        (self.root / "running").write_text("1")
        self.assertTrue(release.newest(self.root, "1.4.6/1")["can_update"])

    def test_up_to_date(self):
        got = release.newest(self.root, "1.10.0/1", NOW)
        self.assertEqual((got["newest"], got["available"]), ("1.10.0", False))

    def test_the_button_asks_the_boot_loader_in_the_stations_home(self):
        home = Path(self.tmp.name) / "home"
        release.ask("all", home / ".cc0ac-weather" / "update-now")
        boot = bl.Boot(self.root)
        self.assertEqual(bl.asked_by_hand(boot), "all")
        self.assertFalse((home / ".cc0ac-weather" / "update-now").exists())
        self.assertIsNone(bl.asked_by_hand(boot))

    def test_by_hand_in_the_root_still_works_and_nonsense_is_all(self):
        (self.root / bl.ASKED).write_text("security")
        self.assertEqual(bl.asked_by_hand(bl.Boot(self.root)), "security")
        (self.root / bl.ASKED).write_text("rm -rf /")
        self.assertEqual(bl.asked_by_hand(bl.Boot(self.root)), "all")


class SetupEndpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        cfg_path = home / "config.ini"
        cfg_path.write_text("[station]\nname = Test\n")
        handler = web._make_web_handler(home / "out", home / "w.db", {}, config_path=cfg_path,
                                        cfg=config(), mcp=False)
        self.real = release.newest
        release.newest = lambda: {"current": "1.4.6", "newest": "1.4.7", "available": True,
                                  "can_update": False, "problem": None}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        release.newest = self.real
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def call(self, path, post=False, header=True):
        req = urllib.request.Request(self.url + path, data=b"" if post else None, method="POST" if post else "GET",
                                     headers={"X-CC0AC-Config": "save"} if header else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_update_json(self):
        code, body = self.call("/update.json")
        self.assertEqual((code, json.loads(body)["newest"]), (200, "1.4.7"))

    def test_update_now_needs_the_pages_header_and_a_boot_loader(self):
        self.assertEqual(self.call("/update.cgi", post=True, header=False)[0], 403)
        code, body = self.call("/update.cgi", post=True)
        self.assertEqual(code, 409)
        self.assertIn("boot loader", body)

    def test_the_setup_page_has_the_button_and_the_dots(self):
        page = (HERE / "setup.html").read_text()
        for bit in ('id="updbtn"', "Update now", "🟢", "update.cgi", "(saved)", "Working:"):
            self.assertIn(bit, page)


if __name__ == "__main__":
    unittest.main()
