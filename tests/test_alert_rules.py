"""1.5.0: the alert rules table ([alert_rules], the /alerts page)."""

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

from cc0ac import alert_rules, alerts, web   # noqa: E402

NOW = datetime(2026, 11, 12, 16, 0, tzinfo=timezone.utc).timestamp()
ALL = frozenset(alert_rules.TARGETS)


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def config(rules=None, **sections):
    cfg = configparser.ConfigParser()
    cfg.read_dict({"station": {"name": "Home"}, "alerts": {"enabled": "true", "ntfy_topic": "t"}, **sections})
    if rules is not None:
        cfg.read_dict({"alert_rules": rules})
    return cfg


class Rows(unittest.TestCase):
    def test_a_row_reads_and_writes_back(self):
        r = alert_rules.parse("lightning close", "lightning | any | 1 mi | 00:10:00 | replaces, over | ntfy")
        self.assertEqual((r.kind, r.trigger, r.stays, r.replaces, r.over, r.targets),
                         ("lightning", 1.0, 600.0, True, True, frozenset({"ntfy"})))
        self.assertEqual(alert_rules.show(r), "lightning | any | 1 mi | 00:10:00 | replaces, over | ntfy")

    def test_the_short_forms(self):
        r = alert_rules.parse("x", "lightning | | 5 | 20")
        self.assertEqual((r.watching, r.trigger, r.stays, r.over, r.targets), ("any", 5.0, 1200.0, True, ALL))
        self.assertEqual(alert_rules.parse("x", "frost").trigger, 35)                 # the kind's default
        self.assertEqual(alert_rules.parse("x", "nws | any | severe").trigger, 3)
        self.assertFalse(alert_rules.parse("x", "heat | any | 100 | until clears | new").over)   # flags given, no over
        self.assertEqual(alert_rules.parse("x", "storm | | | | repeat:01:00:00").repeat, 3600)
        self.assertIsNone(alert_rules.parse("x", "service").stays)                    # once
        self.assertFalse(alert_rules.parse("x", "service").over)

    def test_what_is_wrong_is_named(self):
        for line, says in (("thunder", "isn't a kind"), ("lightning | any | near", "number of mi"),
                           ("nws | any | scary", "an NWS trigger"), ("frost | any | 30 | soon", "isn't a time"),
                           ("frost | any | 30 | | loud", "isn't a flag"), ("frost | | | | repeat:00:00:10", "a minute"),
                           ("frost | | | | | phone", "isn't a target"), ("battery | any | 5", "no threshold")):
            with self.assertRaises(ValueError) as e:
                alert_rules.parse("row one", line)
            self.assertIn("row one", str(e.exception))
            self.assertIn(says, str(e.exception))

    def test_without_a_table_the_old_settings_are_the_rows(self):
        cfg = config(alerts={"enabled": "true", "weather": "frost, lightning", "frost_f": "33", "lightning_mi": "8",
                             "lightning_minutes": "45", "notices": "service", "quiet_minutes": "20"})
        rows = {r.name: r for r in alert_rules.rules(cfg)}
        self.assertEqual(set(rows), {"no readings", "frost", "lightning", "station", "battery", "upkeep"})
        self.assertEqual((rows["frost"].trigger, rows["lightning"].trigger, rows["lightning"].stays,
                          rows["no readings"].trigger), (33, 8, 45 * 60, 20))

    def test_with_a_table_only_its_rows(self):
        cfg = config({"frost": "frost | any | 30", "broken": "thunder"}, alerts={"enabled": "true", "weather": "heat"})
        self.assertEqual([r.name for r in alert_rules.rules(cfg)], ["frost"])
        self.assertEqual(len(alert_rules.problems(cfg)), 1)

    def test_saving_the_page_makes_the_section_and_drops_what_it_replaces(self):
        text = "[alerts]\nenabled = true\nquiet_minutes = 20\nweather = lightning\nlightning_mi = 8\n"
        out = alert_rules.apply_rows(text, [{"name": "Lightning Close", "kind": "lightning", "trigger": "1",
                                             "stays": "10", "flags": "replaces over", "target": "ntfy"}])
        cfg = configparser.ConfigParser()
        cfg.read_string(out)
        self.assertEqual(cfg.get("alert_rules", "lightning close"), "lightning | any | 1 mi | 00:10:00 | replaces, over | ntfy")
        self.assertFalse(cfg.has_option("alerts", "weather") or cfg.has_option("alerts", "lightning_mi"))
        self.assertEqual(cfg.get("alerts", "quiet_minutes"), "20")
        with self.assertRaises(ValueError):
            alert_rules.apply_rows(text, [{"name": "a", "kind": "frost"}, {"name": "A", "kind": "heat"}])
        with self.assertRaises(ValueError):
            alert_rules.apply_rows(text, [{"name": "a=b", "kind": "frost"}])
        none = configparser.ConfigParser()
        none.read_string(alert_rules.apply_rows(text, []))
        self.assertEqual(alert_rules.rules(none), [])            # an empty table is no alerts, not the old ones


class Engine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state.json"
        self.sent = []
        self.real = alerts.send
        alerts.send = lambda cfg, title, message, targets=None: self.sent.append((title, message, targets)) or []

    def tearDown(self):
        alerts.send = self.real
        self.tmp.cleanup()

    def strike(self, mi, heard, at, today=1):
        (self.root / "current.json").write_text(json.dumps({"written": iso(at), "sensors": [
            {"sensor_id": "1", "name": "Atlas", "type": "Atlas", "updated": iso(at),
             "fields": {"last_strike_mi": mi, "last_strike_heard": iso(heard), "strikes_today": today}}]}))

    def weather(self, cfg, at):
        return alerts.check_weather(cfg, self.root, at, self.state)

    def titles(self):
        return [t for t, _, _ in self.sent]

    def test_two_lightning_rows_take_turns(self):
        cfg = config({"lightning": "lightning | any | 10 mi | 00:30:00 | replaces, over | all",
                      "lightning close": "lightning | any | 1 mi | 00:10:00 | replaces, over | all"})
        self.strike(7, NOW, NOW)
        self.assertEqual(self.weather(cfg, NOW), "weather: lightning alerted")
        self.strike(1, NOW + 300, NOW + 300, 2)
        self.assertEqual(self.weather(cfg, NOW + 300), "weather: lightning close closer")
        self.assertIn("1 mi away", self.sent[-1][1])
        self.strike(6, NOW + 600, NOW + 600, 3)
        self.assertEqual(self.weather(cfg, NOW + 600), "weather: nothing new")
        self.strike(6, NOW + 600, NOW + 1300, 3)          # the close row ends: back to the wide one, quietly
        self.assertEqual(self.weather(cfg, NOW + 1300), "weather: nothing new")
        self.strike(1, NOW + 1400, NOW + 1400, 4)          # close again: says so again
        self.assertEqual(self.weather(cfg, NOW + 1400), "weather: lightning close closer")
        self.strike(1, NOW + 1400, NOW + 1400 + 1801, 4)
        self.assertEqual(self.weather(cfg, NOW + 1400 + 1801), "weather: lightning over")
        self.assertEqual(self.titles(), ["Home: lightning", "Home: lightning, closer", "Home: lightning, closer",
                                         "Home: lightning over"])
        self.assertEqual(self.sent[-1][1], "No lightning within 10 mi for 30 minutes.")

    def test_rows_on_their_own_each_say_their_piece(self):
        cfg = config({"far": "lightning | any | 10 | 30", "near": "lightning | any | 1 | 10 | new, over | ntfy"})
        self.strike(1, NOW, NOW)
        self.weather(cfg, NOW)
        self.assertEqual([(t, tg) for t, _, tg in self.sent], [("Home: lightning", ALL), ("Home: lightning", frozenset({"ntfy"}))])
        self.weather(cfg, NOW + 601)
        self.assertEqual(self.sent[-1][0], "Home: lightning over")
        self.assertEqual(self.sent[-1][1], "No lightning within 1 mi for 10 minutes.")

    def test_one_title_and_target_is_one_message(self):
        cfg = config({"frost": "frost | any | 35", "heat": "heat | any | 30", "a": "lightning | any | 10", "b": "lightning | any | 5"})
        self.strike(3, NOW, NOW)
        self.weather(cfg, NOW)
        self.assertEqual(self.titles(), ["Home: lightning"])     # two rows, one title, one message of two lines
        self.assertEqual(self.sent[0][1].count("\n"), 1)

    def test_repeat(self):
        cfg = config({"storm": "storm | any | 0.06 | until clears | over, repeat:01:00:00 | all"})
        (self.root / "current.json").write_text(json.dumps({"pressure_change_3h": -0.08, "sensors": []}))
        for m in (0, 30, 61, 90, 122):
            self.weather(cfg, NOW + m * 60)
        self.assertEqual(self.titles(), ["Home: storm coming"] * 3)

    def test_a_failed_send_is_tried_again(self):
        cfg = config({"frost": "frost | any | 35"})
        (self.root / "current.json").write_text(json.dumps({"sensors": [
            {"sensor_id": "1", "name": "Yard", "type": "Atlas", "updated": iso(NOW), "fields": {"temp_f": 30}}]}))
        answers = [["ntfy: down"], []]
        alerts.send = lambda cfg, title, message, targets=None: self.sent.append(title) or answers.pop(0)
        self.assertIn("failed: ntfy: down", self.weather(cfg, NOW))
        self.assertIn("frost alerted", self.weather(cfg, NOW + 60))
        self.assertEqual(len(self.sent), 2)

    def test_the_dashboard_only_is_a_banner_and_sends_nothing(self):
        cfg = config({"frost": "frost | any | 35 | until clears | over | dashboard"})
        (self.root / "current.json").write_text(json.dumps({"sensors": [
            {"sensor_id": "1", "name": "Yard", "type": "Atlas", "updated": iso(NOW), "fields": {"temp_f": 30}}]}))
        self.weather(cfg, NOW)
        self.assertEqual(self.sent[0][2], frozenset({"dashboard"}))
        shown = json.loads((self.root / "alerts.json").read_text())["alerts"]
        self.assertEqual([(a["row"], a["title"]) for a in shown], [("frost", "frost risk")])
        self.assertTrue((self.root / "alerts.js").read_text().startswith("window.ACURITE_ALERTS = "))
        (self.root / "current.json").write_text(json.dumps({"sensors": [
            {"sensor_id": "1", "name": "Yard", "type": "Atlas", "updated": iso(NOW), "fields": {"temp_f": 50}}]}))
        self.weather(cfg, NOW + 60)
        self.assertEqual(json.loads((self.root / "alerts.json").read_text())["alerts"], [])

    def test_the_real_send_skips_what_the_row_does_not_target(self):
        alerts.send = self.real
        cfg = config(alerts={"enabled": "true", "ntfy_topic": "t", "email_to": "a@example.com"})
        tried = []
        real_email, real_open = alerts._email, alerts.urllib.request.urlopen
        alerts._email = lambda *a, **k: tried.append("email")
        alerts.urllib.request.urlopen = lambda *a, **k: tried.append("ntfy") or type("R", (), {"read": lambda s: b""})()
        try:
            alerts.send(cfg, "t", "m", frozenset({"dashboard"}))
            alerts.send(cfg, "t", "m", frozenset({"email"}))
            alerts.send(cfg, "t", "m")
        finally:
            alerts._email, alerts.urllib.request.urlopen = real_email, real_open
        self.assertEqual(tried, ["email", "ntfy", "email"])

    def nws(self, *found):
        (self.root / "forecast.json").write_text(json.dumps({"alerts": [
            {"event": e, "severity": sev, "headline": f"{e} issued", "onset": "2026-11-12T10:00:00-07:00",
             "ends": "2026-11-12T22:00:00-07:00"} for e, sev in found]}))

    def test_weather_service_alerts_at_a_severity(self):
        cfg = config({"weather service": "nws | any | severe | until clears | over | ntfy"})
        (self.root / "current.json").write_text(json.dumps({"sensors": []}))
        self.nws(("Wind Advisory", "Moderate"))
        self.assertEqual(self.weather(cfg, NOW), "weather: nothing new")
        self.nws(("Wind Advisory", "Moderate"), ("High Wind Warning", "Severe"))
        self.assertEqual(self.weather(cfg, NOW + 60), "weather: weather service alerted")
        self.assertEqual(self.sent[0][0], "Home: high wind warning")
        self.assertIn("High Wind Warning until", self.sent[0][1])
        self.assertIn("High Wind Warning issued", self.sent[0][1])
        self.assertEqual(self.weather(cfg, NOW + 120), "weather: nothing new")
        self.nws()
        self.assertEqual(self.weather(cfg, NOW + 180), "weather: weather service over")
        self.assertEqual(self.sent[-1][1], "The Weather Service has ended the High Wind Warning.")

    def test_a_row_switched_off_says_nothing(self):
        cfg = config({"frost": "frost | any | 35 | until clears | over, off"})
        (self.root / "current.json").write_text(json.dumps({"sensors": [
            {"sensor_id": "1", "name": "Yard", "type": "Atlas", "updated": iso(NOW), "fields": {"temp_f": 30}}]}))
        self.weather(cfg, NOW)
        self.assertEqual(self.sent, [])

    def test_watching_one_sensor(self):
        cfg = config({"attic battery": "battery | Attic | any | until clears | over | ntfy"})
        said = alerts.check_upkeep(cfg, ["The battery in Den is low.", "The battery in Attic is low."], self.state)
        self.assertEqual(said, "upkeep alerted (1)")
        self.assertEqual(self.sent[0][1], "The battery in Attic is low.")
        alerts.check_upkeep(cfg, [], self.state)
        self.assertEqual(self.sent[-1][1], "The battery in Attic is fine again.")

    def test_upkeep_numbers_changing_is_the_same_problem(self):
        cfg = config()
        alerts.check_upkeep(cfg, ["The disk is 91% full."], self.state)
        self.assertEqual(alerts.check_upkeep(cfg, ["The disk is 92% full."], self.state), "upkeep: same problems")
        self.assertEqual(len(self.sent), 1)

    def test_the_station_starting_through_its_row(self):
        cfg = config({"station": "service | any | any | once | new | email"})
        self.assertEqual(alerts.service_started(cfg, "1.5.0/1", self.state, NOW), "started: told")
        self.assertEqual(self.sent[0][2], frozenset({"email"}))
        self.assertEqual(alerts.service_started(config({"frost": "frost"}), "1.5.0/1", self.state, NOW),
                         "service notices off")


class Page(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.cfg_path = home / "config.ini"
        self.cfg_path.write_text("[station]\nname = Test\n[alerts]\nenabled = true\nweather = lightning\n")
        handler = web._make_web_handler(home / "out", home / "w.db", {}, config_path=self.cfg_path,
                                        cfg=config(), mcp=False)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def call(self, path, body=None, header=True):
        req = urllib.request.Request(self.url + path, data=body, method="POST" if body is not None else "GET",
                                     headers={"X-CC0AC-Config": "save"} if header else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_the_page_and_its_rows(self):
        code, page = self.call("/alerts")
        self.assertEqual(code, 200)
        self.assertIn("alerts.cgi", page)
        state = json.loads(self.call("/alert-rules.json")[1])
        self.assertTrue(state["from_settings"])
        self.assertIn("lightning", [r["name"] for r in state["rows"]])
        self.assertEqual(state["kinds"]["lightning"]["stricter"], "closer")

    def test_saving(self):
        rows = json.dumps({"rows": [{"name": "near", "kind": "lightning", "trigger": "2", "stays": "00:15:00",
                                     "flags": "over", "target": "ntfy, dashboard"}]}).encode()
        self.assertEqual(self.call("/alerts.cgi", rows, header=False)[0], 403)
        code, said = self.call("/alerts.cgi", rows)
        self.assertEqual(code, 200, said)
        cfg = configparser.ConfigParser()
        cfg.read(self.cfg_path)
        self.assertEqual(cfg.get("alert_rules", "near"), "lightning | any | 2 mi | 00:15:00 | new, over | ntfy, dashboard")
        self.assertFalse(cfg.has_option("alerts", "weather"))
        bad = json.dumps({"rows": [{"name": "x", "kind": "thunder"}]}).encode()
        code, said = self.call("/alerts.cgi", bad)
        self.assertEqual(code, 400)
        self.assertIn("isn't a kind", said)

    def test_the_dashboard_loads_the_banners(self):
        page = (HERE / "weather.html").read_text()
        self.assertIn("alerts.js", page)
        self.assertIn('id="station-alerts"', page)
        self.assertIn("/alerts.js", web.OUTSIDE_PATHS)


if __name__ == "__main__":
    unittest.main()
