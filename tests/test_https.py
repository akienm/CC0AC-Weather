"""Checks for 1.3.15: https:// beside http:// on the web server's own port, and DuckDNS.
python3 -m unittest tests/test_https.py

The certificates are self-signed for localhost, made here with openssl; DuckDNS
is a stand-in that answers like the real one, so nothing leaves the machine.
Name cc0test and its token are made up.
"""
import configparser
import http.client
import importlib.util
import io
import logging
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import diagnostics   # noqa: E402
import secure_web    # noqa: E402

TOKEN = "0000aaaa-1111-2222-3333-444455556666"


def make_cert(folder: Path, name: str):
    cert, key = folder / f"{name}.pem", folder / f"{name}.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                    "-subj", f"/CN={name}", "-addext", "subjectAltName=DNS:localhost",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    return cert, key


class Echo(BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"{self.path} from {self.client_address[0]}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def client_context(cert: Path) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=str(cert))
    return ctx


def served_cn(port: int) -> str:
    """The name in the certificate the server answers with (not checked against a CA)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw, \
            ctx.wrap_socket(raw, server_hostname="localhost") as tls:
        der = tls.getpeercert(binary_form=True)
    pem = ssl.DER_cert_to_PEM_cert(der)
    out = subprocess.run(["openssl", "x509", "-noout", "-subject"], input=pem, capture_output=True, text=True)
    return out.stdout.strip().rsplit("=", 1)[-1].strip()


class Server(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cert, self.key = make_cert(self.dir, "first")
        self.httpd = secure_web.WebServer(("127.0.0.1", 0), Echo)
        self.httpd.certificate = secure_web.Certificate(self.cert, self.key)
        self.port = self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def https(self, path="/", cert=None):
        with urllib.request.urlopen(f"https://localhost:{self.port}{path}", timeout=10,
                                    context=client_context(cert or self.cert)) as r:
            return r.read().decode()

    def http(self, path="/"):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
            return r.read().decode()

    def test_one_port_answers_http_and_https(self):
        self.assertEqual(self.http("/plain"), "/plain from 127.0.0.1")
        self.assertEqual(self.https("/secure"), "/secure from 127.0.0.1")   # the local check still sees who

    def test_a_renewed_certificate_is_used_without_a_restart(self):
        self.assertEqual(served_cn(self.port), "first")
        new_cert, new_key = make_cert(self.dir, "second")
        os.replace(new_key, self.key)
        os.replace(new_cert, self.cert)
        os.utime(self.cert, ns=(time.time_ns() + 10**9,) * 2)     # a clock too coarse to see the change
        self.assertEqual(served_cn(self.port), "second")
        self.assertEqual(self.https("/after"), "/after from 127.0.0.1")

    def test_a_silent_client_holds_up_only_itself(self):
        silent = socket.create_connection(("127.0.0.1", self.port))
        try:
            start = time.monotonic()
            self.assertEqual(self.http("/next"), "/next from 127.0.0.1")
            self.assertLess(time.monotonic() - start, 3)
        finally:
            silent.close()

    def test_dropped_connections_are_quiet_and_faults_stay_loud(self):
        with self.assertLogs("acurite", level="DEBUG") as logs:
            try:
                raise ConnectionResetError(104, "Connection reset by peer")
            except ConnectionResetError:
                self.httpd.handle_error(None, ("127.0.0.1", 1))
        self.assertTrue(all(r.levelno == logging.DEBUG for r in logs.records))
        err = io.StringIO()
        real, sys.stderr = sys.stderr, err
        try:
            try:
                raise ValueError("a bug in a page")
            except ValueError:
                self.httpd.handle_error(None, ("127.0.0.1", 1))
        finally:
            sys.stderr = real
        self.assertIn("a bug in a page", err.getvalue())


class NoCertificate(unittest.TestCase):
    def test_missing_files_leave_plain_http_and_say_so_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd = secure_web.WebServer(("127.0.0.1", 0), Echo)
            httpd.certificate = secure_web.Certificate(Path(tmp) / "none.pem", Path(tmp) / "none.key")
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                with self.assertLogs("acurite", level="ERROR") as logs:
                    for _ in range(3):
                        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}/x", timeout=10) as r:
                            self.assertEqual(r.read(), b"/x from 127.0.0.1")
                self.assertEqual(len(logs.records), 1)
                self.assertIn("plain http only", logs.output[0])
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_settings(self):
        cfg = configparser.ConfigParser()
        cfg.read_string("[web]\n")
        self.assertIsNone(secure_web.certificate(cfg))
        cfg["web"]["certificate"] = "~/c.pem"
        with self.assertLogs("acurite", level="ERROR"):
            self.assertIsNone(secure_web.certificate(cfg))
        cfg["web"]["key"] = "~/k.pem"
        self.assertEqual(secure_web.certificate(cfg).cert, Path("~/c.pem").expanduser())


class TheDaemon(unittest.TestCase):
    def test_run_web_server_serves_the_dashboard_over_https(self):
        spec = importlib.util.spec_from_file_location("capture", HERE / "acurite-capture.py")
        capture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(capture)
        with tempfile.TemporaryDirectory() as tmp:
            cert, key = make_cert(Path(tmp), "station")
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            cfg = configparser.ConfigParser()
            cfg.read_string(f"[device]\nwrite_path = {tmp}\n[sensors]\n[web]\nport = {port}\ncertificate = {cert}\nkey = {key}\n"
                            f"[storage]\ndatabase = {tmp}/weather.db\n")
            threading.Thread(target=capture.run_web_server, args=(cfg, Path(tmp)), daemon=True).start()
            for _ in range(50):
                try:
                    conn = http.client.HTTPSConnection("localhost", port, timeout=5, context=client_context(cert))
                    conn.request("GET", "/")
                    resp = conn.getresponse()
                    break
                except ConnectionRefusedError:
                    time.sleep(0.1)
            self.assertEqual(resp.status, 200)
            self.assertIn(b"<!DOCTYPE html>", resp.read())


class Answer:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self):
        return self.body


class DuckDNS(unittest.TestCase):
    def test_update_asks_for_the_name_with_the_token(self):
        asked = []
        got = secure_web.duckdns_update("cc0test", TOKEN, lambda url, timeout: asked.append(url) or Answer(b"OK"))
        self.assertEqual(got, "OK")
        self.assertTrue(asked[0].startswith("https://www.duckdns.org/update?"))
        self.assertIn("domains=cc0test", asked[0])
        self.assertIn(f"token={TOKEN}", asked[0])

    def test_a_failure_never_repeats_the_token(self):
        def fail(url, timeout):
            raise OSError(f"cannot reach {url}")
        got = secure_web.duckdns_update("cc0test", TOKEN, fail)
        self.assertTrue(got.startswith("unreachable"))
        self.assertNotIn(TOKEN, got)

    def test_run_says_each_change_once(self):
        answers = iter([b"OK", b"OK", b"OK", b"KO", b"OK"])
        asked, slept = [], []

        class Stop(Exception):
            pass

        def sleep(s):
            slept.append(s)
            if len(slept) == 5:
                raise Stop

        cfg = configparser.ConfigParser()
        cfg.read_string(f"[duckdns]\nenabled = true\ndomain = CC0test.duckdns.org\ntoken = {TOKEN}\n")
        with self.assertLogs("acurite", level="INFO") as logs, self.assertRaises(Stop):
            secure_web.run_duckdns(cfg, lambda url, timeout: asked.append(url) or Answer(next(answers)), sleep)
        self.assertEqual(len(logs.records), 3)             # OK, then KO, then OK again
        self.assertIn("domains=cc0test&", asked[0])          # the suffix and capitals are dropped
        self.assertEqual(slept[0], 300)
        self.assertFalse(any("cc0test" in line or TOKEN in line for line in logs.output))

    def test_without_a_token_it_does_not_start(self):
        cfg = configparser.ConfigParser()
        cfg.read_string("[duckdns]\nenabled = true\ndomain = cc0test\n")
        with self.assertLogs("acurite", level="ERROR"):
            secure_web.run_duckdns(cfg, lambda *a: self.fail("asked DuckDNS"), lambda s: self.fail("slept"))

    def test_diagnostics_hide_the_name_and_token(self):
        self.assertIn("domain", diagnostics.SECRET_KEYS)
        self.assertIn("token", diagnostics.SECRET_KEYS)


class Docs(unittest.TestCase):
    def test_readme_and_example_explain_it(self):
        readme = (HERE / "README.md").read_text()
        self.assertIn("https and a name of your own", readme)
        self.assertIn("acme.sh", readme)
        example = (HERE / "config.ini.example").read_text()
        self.assertIn("[duckdns]", example)
        self.assertIn("certificate = ~/.cc0ac-weather/tls/fullchain.pem", example)


if __name__ == "__main__":
    unittest.main()
