"""Checks for the status page: what was sent and how it went (iolog.py), the
page's data, the logs it opens and the diagnostics file it downloads.
python3 -m unittest tests/test_status.py

These need no network or radio: the uploads answer from a stand-in, and every
file goes in a folder of its own that is thrown away afterwards.
"""
import configparser
import importlib.util
import io
import json
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from cc0ac import alerts        # noqa: E402
from cc0ac import diagnostics   # noqa: E402
from cc0ac import iolog         # noqa: E402
from cc0ac.outputs import windy   # noqa: E402
from cc0ac.sources import Reading   # noqa: E402

from cc0ac import capture

TODAY = f"{datetime.now(timezone.utc):%Y-%m-%d}"


class Kept(unittest.TestCase):
    """A station home of its own: config folder, sent logs and pages' data."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.home = Path(self.dir.name)
        self.saved = (iolog.SENT_DIR, capture.CONFIG_DIR, diagnostics.CONFIG_DIR, diagnostics.service_log)
        iolog.keep(self.home / "sent")
        capture.CONFIG_DIR = diagnostics.CONFIG_DIR = self.home
        diagnostics.service_log = lambda: "service log line\n"

    def tearDown(self):
        iolog.SENT_DIR, capture.CONFIG_DIR, diagnostics.CONFIG_DIR, diagnostics.service_log = self.saved
        self.dir.cleanup()


class SentLog(Kept):
    def test_nothing_is_written_until_the_program_keeps_it(self):
        iolog.SENT_DIR = None
        iolog.report("windy", True, "200 ok")
        self.assertFalse((self.home / "sent").exists())

    def test_each_send_is_one_line_and_the_day_is_summed_up(self):
        iolog.report("windy", True, "200 ok")
        iolog.report("windy", False, "400 Provided password is invalid")
        lines = (self.home / "sent" / "windy" / f"{TODAY}.log").read_text().splitlines()
        self.assertEqual([line.split("\t")[1:] for line in lines],
                         [["ok", "200 ok"], ["FAILED", "400 Provided password is invalid"]])
        s = iolog.latest("windy")
        self.assertEqual((s["ok_count"], s["failed_count"], s["last"]["ok"]), (1, 1, False))
        self.assertEqual(s["last_ok"], lines[0].split("\t")[0])

    def test_a_month_is_kept(self):
        old = time.time() - 40 * 86400
        iolog.report("windy", True, "old", now=old)
        iolog.report("windy", True, "new")
        self.assertEqual([f.stem for f in (self.home / "sent" / "windy").glob("*.log")], [TODAY])

    def test_a_name_that_is_not_a_folder_name_is_refused(self):
        with self.assertRaises(ValueError):
            iolog.report("../../etc", True)

    def test_an_upload_reports_its_answer(self):
        cfg = configparser.ConfigParser()
        cfg["windy"] = {"enabled": "true", "station_id": "abc", "password": "secret-pw"}
        saved = windy.get
        windy.get = lambda url, params: (400, "Provided password is invalid")
        windy.station.sensor_id, windy.station.last_upload = None, None
        try:
            windy.send(cfg, Reading(source="acurite-access", sensor_id="1", type="Atlas",
                                    received_utc="2026-10-24T20:00:00Z", raw="r", fields={"temp_f": 50.0}))
        finally:
            windy.get = saved
        text = (self.home / "sent" / "windy" / f"{TODAY}.log").read_text()
        self.assertIn("FAILED\t400 Provided password is invalid", text)
        self.assertNotIn("secret-pw", text)

    def test_an_alert_that_could_not_go_is_logged(self):
        cfg = configparser.ConfigParser()
        cfg["alerts"] = {"ntfy_topic": "t", "ntfy_server": "http://127.0.0.1:9"}
        alerts.send(cfg, "Home: frost", "Frost")
        self.assertEqual(iolog.latest("ntfy")["last"]["ok"], False)
        self.assertIn("Home: frost", iolog.latest("ntfy")["last"]["detail"])


class Station(Kept):
    def station(self):
        cfg = configparser.ConfigParser()
        cfg["device"] = {"write_path": str(self.home / "out")}
        cfg["hub"] = {"raw_dir": str(self.home / "hub-raw")}
        cfg["storage"] = {"database": str(self.home / "weather.db")}
        out = self.home / "out"
        out.mkdir()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (out / "current.json").write_text(json.dumps({"written": now, "sensors": [
            {"sensor_id": "00004683", "name": "Trailer", "type": "tower", "updated": now,
             "fields": {"battery": "low"},
             "reception": {"hub": {"heard": 12, "last": now, "percent": 100},
                           "radio": {"heard": 200, "last": now, "percent": 90}}}]}))
        for d, line in (("hub-raw", f"{now}\tPOST /weatherstation/updateweatherstation?id=24C86E000000&sensor=00004683&tempf=50\n"
                                    f"{now}\tPOST /weatherstation/updateweatherstation?id=24C86E000000&sensor=00000001&tempf=60\n"),
                        ("radio-raw", '{"model" : "Acurite-Tower", "id" : 4683, "channel" : "B"}\n'
                                      '{"model" : "Acurite-Tower", "id" : 1, "channel" : "A"}\n')):
            (self.home / d).mkdir()
            (self.home / d / f"{TODAY}.log").write_text(line)
        con = capture.open_db(self.home / "weather.db")
        for i, at in enumerate(("2026-10-04T12:00:00Z", now)):
            con.execute("INSERT INTO readings (received_utc, sensor_id, query) VALUES (?, ?, ?)", (at, "00004683", f"q{i}"))
        con.commit()
        con.close()
        iolog.report("windy", True, "200 ok")
        return cfg


class StatusData(Station):
    def test_everything_the_page_shows(self):
        s = capture.system_status(self.station(), self.home / "out")
        self.assertEqual((s["history"]["first"], s["history"]["readings"], s["history"]["sensors"]),
                         ("2026-10-04T12:00:00Z", 2, 1))
        self.assertGreater(s["history"]["bytes"], 0)
        sensor = s["sensors"][0]
        self.assertEqual((sensor["name"], sensor["battery"]), ("Trailer", "low"))
        self.assertEqual(sensor["logs"]["hub"], {"f": f"hub-raw/{TODAY}.log", "find": "sensor=00004683"})
        self.assertEqual(sensor["logs"]["radio"], {"f": f"radio-raw/{TODAY}.log", "find": '"id" : 4683,'})
        self.assertEqual({r["name"] for r in s["sources"]}, {"hub", "radio"})
        self.assertEqual(s["sends"][0]["name"], "windy")
        self.assertEqual(s["sends"][0]["logs"], [f"sent/windy/{TODAY}.log"])
        self.assertGreaterEqual(s["uptime_s"], 0)


class StatusPages(Station):
    def setUp(self):
        super().setUp()
        self.cfg = self.station()
        self.cfg["station"] = {"latitude": "39.9", "longitude": "-105.1"}
        self.cfg["cwop"] = {"station_id": "FW9999"}
        config = self.home / "config.ini"
        with config.open("w") as f:
            self.cfg.write(f)
        iolog.report("cwop", True, "sent FW9999>APRS,TCPIP*:@241200z3954.00N/10506.00W_000/000g000t050; "
                                   "answered # logresp FW9999 unverified")
        handler = capture._make_web_handler(self.home / "out", self.home / "weather.db", {}, config_path=config,
                                            cfg=self.cfg)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.httpd.server_port}{path}", timeout=10) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, b"", e.headers

    def test_the_page_and_its_data(self):
        code, body, _ = self.get("/status")
        self.assertEqual(code, 200)
        self.assertIn(b"/status.json", body)
        code, body, _ = self.get("/status.json")
        self.assertEqual(json.loads(body)["sensors"][0]["name"], "Trailer")

    def test_a_sensors_lines_only(self):
        code, body, _ = self.get(f"/log?f=hub-raw/{TODAY}.log&find=sensor%3D00004683")
        self.assertEqual(code, 200)
        self.assertIn(b"sensor=00004683", body)
        self.assertNotIn(b"sensor=00000001", body)
        code, body, _ = self.get(f"/log?f=radio-raw/{TODAY}.log&find=%22id%22+%3A+4683%2C")
        self.assertEqual(body.decode().count("\n"), 1)
        self.assertEqual(self.get(f"/log?f=sent/windy/{TODAY}.log")[0], 200)
        self.assertEqual(self.get("/log?f=service")[1], b"service log line\n")

    def test_nothing_but_the_logs(self):
        for f in ("../config.ini", "hub-raw/../config.ini", "sent/../../config.ini", "config.ini",
                  f"hub-raw/{TODAY}.log/..", "/etc/passwd", f"sent/windy/{TODAY}.txt"):
            self.assertEqual(self.get("/log?" + urllib.parse.urlencode({"f": f}))[0], 404, f)

    def test_the_diagnostics_file_downloads_without_the_station_in_it(self):
        code, body, headers = self.get("/diagnostics")
        self.assertEqual(code, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        with tarfile.open(fileobj=io.BytesIO(body)) as tar:
            names = tar.getnames()
            sent = tar.extractfile(f"cc0ac-diagnostics/sent/cwop/{TODAY}.log").read().decode()
        self.assertIn(f"cc0ac-diagnostics/sent/windy/{TODAY}.log", names)
        self.assertIn("logresp", sent)
        for private in ("FW9999", "3954.00N", "10506.00W"):
            self.assertNotIn(private, sent)


class Links(unittest.TestCase):
    def test_the_dashboard_links_it_where_it_would_open(self):
        page = (HERE / "weather.html").read_text()
        self.assertIn('href="/status"', page)
        self.assertIn('fetch("/status"', page)

    def test_the_status_page_offers_the_diagnostics_file(self):
        page = (HERE / "status.html").read_text()
        self.assertIn('href="/diagnostics"', page)
        self.assertIn('target="_blank"', page)



class PageLinks(unittest.TestCase):
    """The dashboard's row of links above the version: the ones only your network
    can open are hidden until the status page answers, and stay outside-only proof."""

    def test_the_hidden_ones_are_the_ones_the_outside_cannot_open(self):
        import re
        page = (HERE / "weather.html").read_text()
        hidden = re.search(r'<span id="local-pages" hidden>(.*?)</span>', page, re.S).group(1)
        links = [urllib.parse.urlparse(h).path for h in re.findall(r'href="([^"]+)"', hidden)]
        self.assertEqual(links, ["/status", "/noaa", "/config.cgi", "/setup", "/log", "/diagnostics"])
        for path in links:
            self.assertNotIn(path, capture.OUTSIDE_PATHS)
        self.assertIn('getElementById("local-pages").hidden = false', page)
        self.assertIn("/charts.html", capture.OUTSIDE_PATHS)


if __name__ == "__main__":
    unittest.main()
