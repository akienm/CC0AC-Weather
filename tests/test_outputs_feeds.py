"""Checks for the webhook and InfluxDB outputs: python3 -m unittest tests/test_outputs_feeds.py

Each sends to a server in the test itself, on 127.0.0.1, which keeps what it is
sent and answers as it is told; nothing leaves the computer.
"""
import configparser
import json
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from cc0ac import iolog   # noqa: E402
from cc0ac.outputs import influxdb, webhook   # noqa: E402
from cc0ac.sources import Reading   # noqa: E402


def cfg(**sections) -> configparser.ConfigParser:
    c = configparser.ConfigParser()
    c.optionxform = str
    for name, keys in sections.items():
        c[name] = keys
    return c


def reading(sid="00001234", type="Atlas", suspect=None, **fields) -> Reading:
    return Reading(source="acurite-access", sensor_id=sid, type=type, received_utc="2026-10-24T18:00:00Z",
                   raw="", fields=fields or {"temp_f": 61, "humidity_pct": 44, "battery": "normal"},
                   suspect=suspect or {})


class Catcher:
    """A web server that keeps each request: (method, path, headers, body)."""

    def __init__(self, status=204, answer=b""):
        self.got = []
        catcher = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("Content-Length", 0))
                catcher.got.append(("POST", self.path, dict(self.headers), self.rfile.read(size)))
                self.send_response(status)
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Sends(unittest.TestCase):
    """What every output test needs: a catcher, and the send log kept somewhere thrown away."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.saved = iolog.SENT_DIR
        iolog.keep(Path(self.dir.name))
        webhook._last.clear()

    def tearDown(self):
        iolog.SENT_DIR = self.saved
        self.dir.cleanup()

    def catcher(self, **kw):
        c = Catcher(**kw)
        self.addCleanup(c.close)
        return c

    def sent_log(self, name):
        return "".join(p.read_text() for p in (Path(self.dir.name) / name).glob("*.log"))


class Webhook(Sends):
    def test_off_until_it_has_a_url(self):
        self.assertFalse(webhook.enabled(cfg(webhook={"enabled": "true"})))
        self.assertFalse(webhook.enabled(cfg(webhook={"url": "http://x"})))
        self.assertTrue(webhook.enabled(cfg(webhook={"enabled": "true", "url": "http://x"})))

    def test_each_reading_is_posted_as_json(self):
        c = self.catcher(status=200, answer=b"thanks")
        conf = cfg(webhook={"enabled": "true", "url": c.url + "/hook", "header": "Authorization: Bearer abc"},
                   sensors={"00001234": "Yard"})
        held = {"wind_mph": {"value": 200, "why": "200 mph is past anything a station reads"}}
        webhook.send(conf, reading(suspect=held, temp_f=61.2, humidity_pct=44))
        webhook.send(conf, reading(sid="00005678", type="tower", temp_f=70))
        self.assertEqual(len(c.got), 2)
        method, path, headers, raw = c.got[0]
        self.assertEqual(path, "/hook")
        self.assertEqual(headers["Authorization"], "Bearer abc")
        self.assertEqual(headers["Content-Type"], "application/json")
        body = json.loads(raw)
        self.assertEqual((body["sensor_id"], body["name"], body["type"], body["outdoor"]),
                         ("00001234", "Yard", "Atlas", True))
        self.assertEqual(body["time"], "2026-10-24T18:00:00Z")
        self.assertEqual(body["fields"], {"temp_f": 61.2, "humidity_pct": 44})
        self.assertEqual(body["units"], {"temp_f": "°F", "humidity_pct": "%"})
        self.assertEqual(body["held_back"], held)
        self.assertFalse(json.loads(c.got[1][3])["outdoor"])
        self.assertIn("ok\t200 thanks", self.sent_log("webhook"))

    def test_every_minutes_sends_each_sensor_once_a_while(self):
        c = self.catcher()
        conf = cfg(webhook={"enabled": "true", "url": c.url, "every_minutes": "10"})
        for sid in ("00001234", "00001234", "00005678", "00005678"):
            webhook.send(conf, reading(sid=sid))
        self.assertEqual(len(c.got), 2)

    def test_a_refusal_or_no_server_is_logged_failed(self):
        c = self.catcher(status=401, answer=b"bad token")
        webhook.send(cfg(webhook={"url": c.url}), reading())
        dead = self.catcher()
        dead.close()
        webhook.send(cfg(webhook={"url": dead.url}), reading(sid="00009999"))
        log = self.sent_log("webhook").splitlines()
        self.assertEqual(len(log), 2)
        self.assertIn("FAILED\t401 bad token", log[0])
        self.assertIn("FAILED\t0 ", log[1])


class LineProtocol(unittest.TestCase):
    def test_tags_numbers_as_floats_and_strings_quoted(self):
        conf = cfg(influxdb={"bucket": "b"}, sensors={"00001234": "Back yard, north"})
        got = influxdb.line(conf, reading(temp_f=61, humidity_pct=44.5, battery="low", flag=True))
        self.assertEqual(got, "weather,sensor_id=00001234,name=Back\\ yard\\,\\ north,type=Atlas,"
                              "source=acurite-access battery=\"low\",humidity_pct=44.5,temp_f=61.0 1792864800")

    def test_measurement_and_an_unnamed_sensor(self):
        conf = cfg(influxdb={"measurement": "my house"})
        got = influxdb.line(conf, reading(type=None, temp_f=1))
        self.assertTrue(got.startswith("my\\ house,sensor_id=00001234,source=acurite-access temp_f=1.0 "))

    def test_nothing_to_write(self):
        self.assertIsNone(influxdb.line(cfg(influxdb={}), reading(battery=None, flag=False)))


class Influx(Sends):
    def test_needs_a_url_and_a_bucket_or_database(self):
        self.assertFalse(influxdb.enabled(cfg(influxdb={"enabled": "true", "url": "http://x"})))
        self.assertTrue(influxdb.enabled(cfg(influxdb={"enabled": "true", "url": "http://x", "bucket": "b"})))
        self.assertTrue(influxdb.enabled(cfg(influxdb={"enabled": "true", "url": "http://x", "database": "d"})))

    def test_version_2_writes_to_the_bucket_with_the_token(self):
        c = self.catcher()
        conf = cfg(influxdb={"url": c.url + "/", "org": "home", "bucket": "weather", "token": "T0k"})
        influxdb.send(conf, reading())
        _, path, headers, body = c.got[0]
        url = urllib.parse.urlparse(path)
        self.assertEqual(url.path, "/api/v2/write")
        self.assertEqual(urllib.parse.parse_qs(url.query), {"org": ["home"], "bucket": ["weather"], "precision": ["s"]})
        self.assertEqual(headers["Authorization"], "Token T0k")
        self.assertTrue(body.decode().startswith("weather,sensor_id=00001234"))
        self.assertIn("ok\t204", self.sent_log("influxdb"))

    def test_version_1_writes_to_the_database(self):
        c = self.catcher()
        conf = cfg(influxdb={"url": c.url, "database": "wx", "username": "me", "password": "pw"})
        influxdb.send(conf, reading())
        url = urllib.parse.urlparse(c.got[0][1])
        self.assertEqual(url.path, "/write")
        self.assertEqual(urllib.parse.parse_qs(url.query), {"db": ["wx"], "precision": ["s"], "u": ["me"], "p": ["pw"]})
        self.assertNotIn("Authorization", c.got[0][2])

    def test_a_refusal_is_logged_failed_without_the_token(self):
        c = self.catcher(status=401, answer=b'{"code":"unauthorized"}')
        influxdb.send(cfg(influxdb={"url": c.url, "bucket": "b", "token": "S3cret"}), reading())
        log = self.sent_log("influxdb")
        self.assertIn("FAILED\t401", log)
        self.assertNotIn("S3cret", log)


class Loaded(unittest.TestCase):
    def test_the_program_finds_both(self):
        import importlib.util
        from cc0ac import capture
        names = {o.NAME for o in capture.load_outputs()}
        self.assertTrue({"webhook", "influxdb"} <= names)


if __name__ == "__main__":
    unittest.main()
