"""dnsmasq is started again whenever it stops: python3 -m unittest tests/test_dnsmasq_restarts.py

Born of 2026-11-11: a reboot started dnsmasq before the hub's port; it quit and the hub
went unheard for three hours."""
import importlib.util
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("dnsmasq_restarts", HERE / "updates/migrations/0008_dnsmasq_restarts.py")
dnsmasq_restarts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dnsmasq_restarts)
spec = importlib.util.spec_from_file_location("hub_network", HERE / "updates/migrations/0003_hub_network.py")
hub_network = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hub_network)


class Ctx(SimpleNamespace):
    def __init__(self):
        super().__init__(image=True, log=logging.getLogger("t"), ran=[])
        self.ours_or_absent = lambda p: not p.exists() or "ours" in p.read_text()
        self.write_ours = lambda p, text: (p.parent.mkdir(parents=True, exist_ok=True), p.write_text("# ours\n" + text))

    def run(self, cmd, check=True, **kw):
        self.ran.append(cmd)


class DnsmasqRestarts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conf = self.root / "cc0ac-hub.conf"
        self.drop_in = self.root / "dnsmasq.service.d" / "cc0ac-restart.conf"

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_hub_on_the_cable_gets_restarts_set_up_by_hand_or_not(self):
        self.conf.write_text("interface=eth0\nbind-interfaces\n")     # akienpi's, by hand from the README
        dnsmasq_restarts.apply(Ctx(), self.conf, self.drop_in)
        text = self.drop_in.read_text()
        self.assertIn("Restart=always", text)
        self.assertIn("StartLimitIntervalSec=0", text)      # never gives up for restarting too often

    def test_no_hub_on_the_cable_no_change(self):
        dnsmasq_restarts.apply(Ctx(), self.conf, self.drop_in)
        self.assertFalse(self.drop_in.exists())

    def test_one_written_by_hand_is_kept(self):
        self.conf.write_text("interface=eth0\n")
        self.drop_in.parent.mkdir()
        self.drop_in.write_text("[Service]\nRestart=no\n")
        dnsmasq_restarts.apply(Ctx(), self.conf, self.drop_in)
        self.assertEqual(self.drop_in.read_text(), "[Service]\nRestart=no\n")

    def test_the_installer_waits_for_the_port(self):
        self.assertIn("bind-dynamic", hub_network.DNSMASQ)
        self.assertNotIn("bind-interfaces", hub_network.DNSMASQ)

    def test_the_readme_says_the_same(self):
        readme = (HERE / "README.md").read_text()
        self.assertIn("   bind-dynamic\n", readme)
        self.assertNotIn("   bind-interfaces\n", readme)


if __name__ == "__main__":
    unittest.main()
