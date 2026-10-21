"""Checks for the boot loader: python3 -m unittest tests/test_bootloader.py

Releases come from a folder (bootloader.Folder) instead of GitHub, and each one's
station is a stand-in that answers the dashboard port and writes a reading every half
second, or misbehaves as its BEHAVIOR file says. Platform "docker" is used because it
has no operating-system service to install; the service migrations are not run here.
"""
import configparser
import json
import shutil
import socket
import sys
import tarfile
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "updates"))
import bootloader as bl   # noqa: E402

FAKE_STATION = r'''
import argparse, configparser, http.server, json, sys, threading, time
from datetime import datetime, timezone
from pathlib import Path
here = Path(__file__).resolve().parent
p = argparse.ArgumentParser(); p.add_argument("--config"); p.add_argument("--check", action="store_true")
a = p.parse_args()
if a.check:
    print("readings ok"); sys.exit(0)
c = configparser.ConfigParser(inline_comment_prefixes=("#", ";")); c.read(a.config)
behavior = (here / "BEHAVIOR").read_text().strip()
if behavior == "crash":
    sys.exit(3)
out = Path(c.get("device", "write_path")); out.mkdir(parents=True, exist_ok=True)
port = int(c.get("web", "port").split(",")[-1])
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass
http.server.ThreadingHTTPServer.allow_reuse_address = True
threading.Thread(target=http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever, daemon=True).start()
while True:
    if behavior == "good":
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (out / "current.json").write_text(json.dumps({"sensors": [{"sensor_id": "s1", "updated": now}]}))
    time.sleep(0.5)
'''


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def release(folder: Path, tag: str, version: str, behavior: str = "good", migrations: dict | None = None,
            security: dict | None = None) -> Path:
    tree = folder / tag
    (tree / "updates" / "migrations").mkdir(parents=True)
    (tree / "VERSION").write_text(version + "\n")
    (tree / "BEHAVIOR").write_text(behavior)
    (tree / "acurite-capture.py").write_text(FAKE_STATION)
    for name in ("wizard.py", "config.ini.example"):
        shutil.copy(REPO / name, tree / name)
    shutil.copy(REPO / "updates" / "bootloader.py", tree / "updates" / "bootloader.py")
    shutil.copy(REPO / "updates" / "migrations" / "0001_settings.py", tree / "updates" / "migrations")
    for name, text in (migrations or {}).items():
        (tree / "updates" / "migrations" / name).write_text(text)
    if security:
        (tree / "updates" / "security").mkdir()
        for name, text in security.items():
            (tree / "updates" / "security" / name).write_text(text)
    return tree


MARKER = '''PLATFORMS = ("docker",)
def apply(ctx):
    (ctx.home / "{name}").write_text("ran")
'''


class Install(unittest.TestCase):
    def setUp(self):
        bl.HEALTH_POLL_S = 0.3
        self.tmp = Path(tempfile.mkdtemp(prefix="cc0ac-bootloader-test-"))
        self.releases = self.tmp / "releases"
        self.home = self.tmp / "home"
        self.root = self.tmp / "root"
        self.station = None

    def tearDown(self):
        if self.station:
            self.station.wanted = False
            self.station.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def install(self, tag="v1.2.0"):
        args = SimpleNamespace(platform="docker", root=str(self.root), config=str(self.home / "config.ini"),
                               home=str(self.home), user=None, python=sys.executable, source=None, tag=tag,
                               releases=str(self.releases), hub_network=False, image=False)
        boot = bl.install(args)
        text = (self.home / "config.ini").read_text()
        sys.path.insert(0, str(REPO))
        import wizard
        (self.home / "config.ini").write_text(wizard.set_values(text, "web", {"port": str(free_port())}))
        return boot

    def start(self, boot):
        self.station = bl.Station(boot)
        self.station.start()
        deadline = time.time() + 15
        while not bl.reporting(boot) and time.time() < deadline:
            time.sleep(0.2)
        self.assertEqual(bl.reporting(boot), ["s1"])

    def test_a_fresh_install_runs_every_migration_and_writes_the_settings(self):
        release(self.releases, "v1.2.0", "1.2.0/1", migrations={"0002_marker.py": MARKER.format(name="m2")})
        boot = self.install()
        self.assertEqual((boot.state["current"], boot.state["security"]), ("1.2.0", 1))
        self.assertEqual(boot.state["migrations"], ["0001_settings", "0002_marker"])
        self.assertTrue((self.home / "m2").exists())
        self.assertTrue((self.root / "bootloader.py").exists())
        c = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
        c.read(self.home / "config.ini")
        self.assertFalse(c.getboolean("hub", "enabled"))       # Docker can't take the hub's cable
        self.assertEqual(c.get("device", "write_path"), str(self.home / "readings"))
        self.assertTrue(c.getboolean("updates", "automatic"))
        self.assertIn(boot.state["weekday"], range(7))

    def test_installing_again_keeps_the_state_and_runs_nothing_twice(self):
        release(self.releases, "v1.2.0", "1.2.0/1", migrations={"0002_marker.py": MARKER.format(name="m2")})
        first = self.install()
        (self.home / "m2").unlink()
        again = self.install(tag=None)
        self.assertEqual((again.state["weekday"], again.state["jitter_min"]),
                         (first.state["weekday"], first.state["jitter_min"]))
        self.assertFalse((self.home / "m2").exists())

    def test_a_healthy_update_runs_only_the_new_migrations_and_keeps_the_old_version(self):
        release(self.releases, "v1.2.0", "1.2.0/1")
        boot = self.install()
        self.start(boot)
        release(self.releases, "v1.2.1", "1.2.1/1", migrations={"0002_marker.py": MARKER.format(name="m2")})
        self.assertEqual(bl.check_for_updates(boot, self.station, "all", wait_s=20), "v1.2.1: installed")
        self.assertEqual((boot.state["current"], boot.state["previous"]), ("1.2.1", "1.2.0"))
        self.assertEqual(boot.state["migrations"], ["0001_settings", "0002_marker"])
        self.assertTrue(self.station.alive())
        self.assertEqual(sorted(p.name for p in (self.root / "versions").iterdir()), ["1.2.0", "1.2.1"])

    def test_a_version_that_hears_no_sensor_is_rolled_back_and_tried_again_next_time(self):
        release(self.releases, "v1.2.0", "1.2.0/1")
        boot = self.install()
        self.start(boot)
        release(self.releases, "v1.3.0", "1.3.0/1", behavior="silent")
        self.assertEqual(bl.check_for_updates(boot, self.station, "all", wait_s=3), "v1.3.0: rolled back")
        self.assertEqual(boot.state["current"], "1.2.0")
        self.assertIn("1.3.0", boot.state["failed"])
        self.assertTrue(self.station.alive())
        self.assertEqual(bl.check_for_updates(boot, self.station, "all", wait_s=1), "v1.3.0: rolled back")

    def test_a_version_that_crashes_is_rolled_back(self):
        release(self.releases, "v1.2.0", "1.2.0/1")
        boot = self.install()
        self.start(boot)
        release(self.releases, "v1.2.1", "1.2.1/1", behavior="crash")
        self.assertEqual(bl.check_for_updates(boot, self.station, "all", wait_s=3), "v1.2.1: rolled back")
        self.assertEqual(boot.state["current"], "1.2.0")

    def test_no_rollback_past_a_migration_that_cant_be_undone(self):
        release(self.releases, "v1.2.0", "1.2.0/1")
        boot = self.install()
        self.start(boot)
        step = "NO_ROLLBACK = True\n" + MARKER.format(name="m2")
        release(self.releases, "v1.2.1", "1.2.1/1", behavior="silent", migrations={"0002_one_way.py": step})
        self.assertEqual(bl.check_for_updates(boot, self.station, "all", wait_s=2), "v1.2.1: rolled back")
        self.assertEqual(boot.state["current"], "1.2.1")   # stays, as UPDATES.md says

    def test_a_security_fix_goes_on_a_copy_so_rolling_back_is_a_switch(self):
        release(self.releases, "v1.2.0", "1.2.0/1")
        boot = self.install()
        self.start(boot)
        fix = 'def apply(ctx):\n    (ctx.version_dir / "PATCHED").write_text("s2")\n'
        later = 'def apply(ctx):\n    (ctx.version_dir / "TOO-NEW").write_text("s3")\n'
        release(self.releases, "s2", "1.2.0/2", security={"0002_close_a_hole.py": fix, "0003_later.py": later})
        self.assertEqual(bl.check_for_updates(boot, self.station, "security", wait_s=20), "s2 (1 fix(es)): installed")
        self.assertEqual((boot.state["current"], boot.state["security"]), ("1.2.0-s2", 2))
        self.assertTrue((self.root / "versions" / "1.2.0-s2" / "PATCHED").exists())
        self.assertFalse((self.root / "versions" / "1.2.0-s2" / "TOO-NEW").exists())
        self.assertFalse((self.root / "versions" / "1.2.0" / "PATCHED").exists())
        self.assertEqual(bl.check_for_updates(boot, self.station, "security", wait_s=1), "up to date")

    def test_an_update_by_hand_asks_the_running_boot_loader(self):
        release(self.releases, "v1.2.0", "1.2.0/1")
        boot = self.install()
        (self.root / bl.ALIVE).write_text("1")
        bl.main(["update", "--root", str(self.root)])
        self.assertEqual((self.root / bl.ASKED).read_text(), "all")
        self.assertEqual(json.loads((self.root / "state.json").read_text())["current"], "1.2.0")
        del boot


class Choosing(unittest.TestCase):
    def test_newest_version_crosses_minor_versions_but_not_major_ones(self):
        tags = ["v1.2.0", "v1.2.1", "v1.3.0", "v2.0.0", "s4", "latest"]
        self.assertEqual(bl.newest_version(tags, (1, 2, 0)), "v1.3.0")
        self.assertIsNone(bl.newest_version(tags, (1, 3, 0)))
        self.assertEqual(bl.newest_security(tags), 4)

    def test_when_it_checks(self):
        boot = SimpleNamespace(state={"weekday": 2, "jitter_min": 7, "last_check": "2026-10-20"},
                               cfg=lambda: configparser.ConfigParser())
        wed, thu = datetime(2026, 10, 21, 6, 8), datetime(2026, 10, 22, 6, 8)
        self.assertEqual(bl.due(boot, wed), "all")                        # its weekday (Wednesday)
        self.assertEqual(bl.due(boot, thu), "security")                   # any other day
        self.assertIsNone(bl.due(boot, datetime(2026, 10, 21, 6, 6)))      # before 06:00 + 7 min
        boot.state["last_check"] = "2026-10-21"
        self.assertIsNone(bl.due(boot, wed))                              # once a day
        c = configparser.ConfigParser()
        c["updates"] = {"automatic": "no"}
        boot.cfg = lambda: c
        self.assertIsNone(bl.due(boot, thu))                              # No: never

    def test_a_release_archive_cannot_write_outside_its_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.tar.gz"
            evil = Path(tmp) / "evil.txt"
            evil.write_text("x")
            with tarfile.open(bad, "w:gz") as tar:
                tar.add(evil, arcname="CC0AC-Weather-1.2.0/../../escaped.txt")
            (Path(tmp) / "x").mkdir()
            with self.assertRaises(Exception):
                bl._safe_extract(bad, Path(tmp) / "x")
            self.assertFalse((Path(tmp).parent / "escaped.txt").exists())


if __name__ == "__main__":
    unittest.main()
