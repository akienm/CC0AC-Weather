"""Checks for 1.3.19: a new station, with no source switched on yet, keeps running
so its setup page stays up. python3 -m unittest tests/test_first_start.py

Before 1.3.19 it ended at once, the boot loader took the clean exit for a restart
after saved settings, and a new Docker install restarted every two seconds."""
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FirstStart(unittest.TestCase):
    def test_no_source_keeps_the_setup_page_up(self):
        port = free_port()
        with tempfile.TemporaryDirectory() as d:
            config = Path(d) / "config.ini"
            config.write_text(f"[device]\nwrite_path = {d}/readings\n\n[hub]\nenabled = false\n\n"
                              f"[web]\nenabled = true\nport = {port}\n\n[forecast]\nenabled = false\n\n"
                              "[climate]\nnormals = false\n")
            proc = subprocess.Popen([sys.executable, str(HERE / "acurite-capture.py"), "--config", str(config)],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env={"HOME": d, "PATH": "/usr/bin:/bin"})
            try:
                status = None
                for _ in range(50):
                    time.sleep(0.2)
                    self.assertIsNone(proc.poll(), "the station ended with no source on")
                    try:
                        status = urllib.request.urlopen(f"http://127.0.0.1:{port}/setup", timeout=2).status
                        break
                    except OSError:
                        pass
                self.assertEqual(status, 200)
                time.sleep(2)
                self.assertIsNone(proc.poll(), "the station ended with no source on")
            finally:
                proc.terminate()
                out = proc.communicate(timeout=10)[0].decode()
        self.assertIn("The web server stays up for the setup page", out)
        self.assertNotIn("Traceback", out)


if __name__ == "__main__":
    unittest.main()
