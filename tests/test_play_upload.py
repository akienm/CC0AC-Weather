"""android/play-upload.py against a stand-in for Google: the login's signature verifies
with the key's public half, and the edit is opened, filled, then committed (or, on a dry
run or a failure, validated and deleted). python3 -m unittest tests/test_play_upload.py"""
import base64
import importlib.util
import json
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("play_upload", HERE / "android/play-upload.py")
play = importlib.util.module_from_spec(spec)
spec.loader.exec_module(play)


def unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class FakeGoogle(BaseHTTPRequestHandler):
    seen = []          # (method, path) in order
    pub = None         # the service account's public key, to check the login with
    fail_on = ""       # a path fragment to answer 403 to

    def log_message(self, *a):
        pass

    def answer(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_any(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        path = self.path
        FakeGoogle.seen.append((self.command, path.split("?")[0], body))
        if path == "/token":
            jwt = dict(x.split("=", 1) for x in body.decode().split("&"))["assertion"]
            jwt = __import__("urllib.parse").parse.unquote(jwt)
            head, claims, sig = jwt.split(".")
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / "s").write_bytes(unb64(sig))
                ok = subprocess.run(["openssl", "dgst", "-sha256", "-verify", FakeGoogle.pub, "-signature",
                                     str(Path(d) / "s")], input=f"{head}.{claims}".encode(), capture_output=True)
            c = json.loads(unb64(claims))
            if ok.returncode or c["scope"] != play.SCOPE or c["iss"] != "robot@test.iam":
                return self.answer(401, {"error": "bad login"})
            return self.answer(200, {"access_token": "tok"})
        if self.headers.get("Authorization") != "Bearer tok":
            return self.answer(401, {"error": "no login"})
        if FakeGoogle.fail_on and FakeGoogle.fail_on in path:
            return self.answer(403, {"error": "not allowed"})
        if path.endswith("/edits"):
            return self.answer(200, {"id": "e1"})
        if "/bundles" in path:
            return self.answer(200, {"versionCode": 3})
        return self.answer(200, {})

    do_GET = do_POST = do_PUT = do_DELETE = handle_any


class UploadsToPlay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        subprocess.run(["openssl", "genrsa", "-out", str(t / "k.pem"), "2048"], capture_output=True, check=True)
        subprocess.run(["openssl", "rsa", "-in", str(t / "k.pem"), "-pubout", "-out", str(t / "p.pem")],
                       capture_output=True, check=True)
        self.server = HTTPServer(("127.0.0.1", 0), FakeGoogle)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.server.server_port}"
        play.API = base
        FakeGoogle.seen, FakeGoogle.pub, FakeGoogle.fail_on = [], str(t / "p.pem"), ""
        self.key = {"client_email": "robot@test.iam", "token_uri": base + "/token",
                    "private_key": (t / "k.pem").read_text()}
        self.aab = t / "app.aab"
        self.aab.write_bytes(b"PK-not-really-a-bundle")
        self.said = []

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def steps(self):
        return [(m, p.rsplit("/", 1)[-1]) for m, p, _ in FakeGoogle.seen]

    def test_uploads_puts_on_the_track_and_commits(self):
        code = play.upload(self.aab, self.key, "internal", "completed", "Kiosk mode.", False, self.said.append)
        self.assertEqual(code, 3)
        self.assertEqual(self.steps(), [("POST", "token"), ("POST", "edits"), ("POST", "bundles"),
                                        ("PUT", "internal"), ("POST", "e1:commit")])
        self.assertEqual(FakeGoogle.seen[2][2], b"PK-not-really-a-bundle")
        track = json.loads(FakeGoogle.seen[3][2])
        self.assertEqual(track["releases"][0], {"versionCode": ["3"], "status": "completed",
                                                "releaseNotes": [{"language": "en-US", "text": "Kiosk mode."}]})

    def test_a_dry_run_validates_and_leaves_nothing(self):
        play.upload(self.aab, self.key, "production", "draft", "", True, self.said.append)
        self.assertEqual(self.steps()[-2:], [("POST", "e1:validate"), ("DELETE", "e1")])
        self.assertNotIn(("POST", "e1:commit"), self.steps())
        self.assertIn("Nothing was released", self.said[-1])

    def test_a_refusal_says_so_and_closes_the_edit(self):
        FakeGoogle.fail_on = "/tracks/"
        with self.assertRaises(SystemExit) as e:
            play.upload(self.aab, self.key, "internal", "completed", "", False, self.said.append)
        self.assertIn("403", str(e.exception))
        self.assertEqual(self.steps()[-1], ("DELETE", "e1"))

    def test_a_wrong_key_is_not_let_in(self):
        other = Path(self.tmp.name) / "other.pem"
        subprocess.run(["openssl", "genrsa", "-out", str(other), "2048"], capture_output=True, check=True)
        with self.assertRaises(SystemExit) as e:
            play.upload(self.aab, dict(self.key, private_key=other.read_text()), "internal", "completed", "",
                        False, self.said.append)
        self.assertIn("401", str(e.exception))

    def test_no_key_file_says_how_to_make_one(self):
        with self.assertRaises(SystemExit):
            play.main(["--aab", str(self.aab), "--key", str(Path(self.tmp.name) / "none.json")])


if __name__ == "__main__":
    unittest.main()
