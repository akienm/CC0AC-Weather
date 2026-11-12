"""The station keeps its network profiles and puts back one found empty: python3 -m unittest tests/test_keep_network.py

Born of 2026-10-23: two power drops left the Pi's Wi-Fi and hub profiles at 0 bytes, and
the station stayed off the network until someone came to the keyboard."""
import importlib.util
import logging
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("keep_network", HERE / "updates/migrations/0010_keep_network.py")
keep_network = importlib.util.module_from_spec(spec)
spec.loader.exec_module(keep_network)
import sys   # noqa: E402
sys.path.insert(0, str(HERE))
from updates import bootloader   # noqa: E402

WIFI = "[connection]\nid=home\ntype=wifi\n\n[wifi-security]\npsk=not-a-real-one\n"
NETPLAN = "network:\n  version: 2\n  wifis:\n    NM-1234:\n      renderer: NetworkManager\n"


class KeepNetwork(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.nm, self.netplan, self.keep = self.root / "nm", self.root / "netplan", self.root / "keep"
        self.nm.mkdir()
        self.netplan.mkdir()
        self.script = self.root / "keep-network"
        self.ran = []
        self.ctx = SimpleNamespace(image=True, log=logging.getLogger("t"),
                                   run=lambda cmd, **kw: self.ran.append(cmd),
                                   ours_or_absent=bootloader.ours_or_absent, write_ours=bootloader.write_ours)
        keep_network.apply(self.ctx, self.script, self.root / "units")

    def tearDown(self):
        self.tmp.cleanup()

    def keeper(self) -> str:
        env = dict(os.environ, KEEP=str(self.keep), DIRS=f"{self.nm} {self.netplan}", RELOAD="0")
        done = subprocess.run(["sh", str(self.script)], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_whole_profiles_are_copied_where_only_root_reads_them(self):
        (self.nm / "home.nmconnection").write_text(WIFI)
        (self.netplan / "90-NM-1234.yaml").write_text(NETPLAN)
        self.assertEqual(self.keeper(), "")
        copies = sorted(self.keep.rglob("*.*"))
        self.assertEqual({p.read_text() for p in copies}, {WIFI, NETPLAN})
        self.assertEqual({p.stat().st_mode & 0o777 for p in copies}, {0o600})

    def test_a_profile_found_empty_is_put_back(self):
        wifi = self.nm / "home.nmconnection"
        wifi.write_text(WIFI)
        self.keeper()
        wifi.write_text("")     # what the power drop left
        said = self.keeper()
        self.assertEqual(wifi.read_text(), WIFI)
        self.assertIn(f"put back {wifi}: it was empty or broken", said)

    def test_a_netplan_file_cut_short_is_put_back(self):
        plan = self.netplan / "90-NM-1234.yaml"
        plan.write_text(NETPLAN)
        self.keeper()
        plan.write_text("  version: 2\n")
        self.keeper()
        self.assertEqual(plan.read_text(), NETPLAN)

    def test_a_changed_profile_replaces_its_copy(self):
        wifi = self.nm / "home.nmconnection"
        wifi.write_text(WIFI)
        self.keeper()
        wifi.write_text(WIFI.replace("not-a-real-one", "a-new-one"))
        self.keeper()
        wifi.write_text("")
        self.keeper()
        self.assertIn("a-new-one", wifi.read_text())

    def test_an_empty_profile_with_no_copy_is_left_and_not_copied(self):
        empty = self.nm / "old.nmconnection"
        empty.write_text("")
        self.assertEqual(self.keeper(), "")
        self.assertEqual(empty.read_text(), "")
        self.assertEqual(list(self.keep.rglob("*.*")), [])

    def test_a_deleted_profile_stays_deleted(self):
        wifi = self.nm / "home.nmconnection"
        wifi.write_text(WIFI)
        self.keeper()
        wifi.unlink()
        self.keeper()
        self.assertFalse(wifi.exists())

    def test_the_timer_runs_it_every_minute_and_is_enabled(self):
        timer = (self.root / "units" / "cc0ac-keep-network.timer").read_text()
        self.assertIn("OnUnitActiveSec=1min", timer)
        self.assertIn(["systemctl", "enable", "cc0ac-keep-network.timer"], self.ran)
        self.assertEqual(self.script.stat().st_mode & 0o777, 0o755)
        self.assertTrue(self.script.read_text().startswith("#!/bin/sh\n" + bootloader.MARK))

    def test_a_script_someone_else_wrote_is_left_alone(self):
        self.script.write_text("theirs\n")
        keep_network.apply(self.ctx, self.script, self.root / "units")
        self.assertEqual(self.script.read_text(), "theirs\n")


if __name__ == "__main__":
    unittest.main()
