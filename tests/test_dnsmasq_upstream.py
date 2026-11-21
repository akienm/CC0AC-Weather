"""dnsmasq passes the hub's other names on: python3 -m unittest tests/test_dnsmasq_upstream.py

Born of 2026-11-21: under systemd-resolved dnsmasq had no upstream; the hub restarted, was
refused pool.ntp.org, and sent nothing."""
import importlib.util
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE / "updates/migrations" / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hub_network = load("hub_network", "0003_hub_network.py")
dnsmasq_upstream = load("dnsmasq_upstream", "0011_dnsmasq_upstream.py")
MARK = "# Written by CC0AC-Weather's boot loader; it rewrites this file when it runs again."
RESOLVED = "# systemd-resolved stub\nnameserver 127.0.0.53\noptions edns0 trust-ad\nsearch .\n"
PLAIN = "nameserver 10.0.0.1\n"


class Ctx(SimpleNamespace):
    def __init__(self):
        super().__init__(image=False, log=logging.getLogger("t"), ran=[], mark=MARK)
        self.ours_or_absent = lambda p: not p.exists() or MARK in p.read_text()
        self.write_ours = lambda p, text: p.write_text(MARK + "\n" + text)

    def run(self, cmd, check=True, **kw):
        self.ran.append(cmd)


class DnsmasqUpstream(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conf = self.root / "cc0ac-hub.conf"
        self.resolv = self.root / "resolv.conf"

    def tearDown(self):
        self.tmp.cleanup()

    def old_file(self):     # what 0003 wrote before 1.5.3
        self.conf.write_text(MARK + "\n" + hub_network.DNSMASQ.format(hub="enx0", net="192.168.77"))

    def test_under_systemd_resolved_the_installer_names_the_upstream(self):
        self.resolv.write_text(RESOLVED)
        self.assertEqual(hub_network.upstream(self.resolv).splitlines()[1:], ["no-resolv", "server=127.0.0.53"])

    def test_elsewhere_the_installer_leaves_dnsmasqs_default(self):
        self.resolv.write_text(PLAIN)
        self.assertEqual(hub_network.upstream(self.resolv), "")
        self.assertEqual(hub_network.upstream(self.root / "absent"), "")

    def test_an_installed_station_gets_it_once_and_dnsmasq_restarts(self):
        self.resolv.write_text(RESOLVED)
        self.old_file()
        ctx = Ctx()
        dnsmasq_upstream.apply(ctx, self.conf, self.resolv)
        text = self.conf.read_text()
        self.assertEqual(text.count(MARK), 1)
        self.assertIn("\nno-resolv\nserver=127.0.0.53\n", text)
        self.assertIn("address=/atlasapi.myacurite.com/192.168.77.1", text)
        self.assertIn(["systemctl", "restart", "dnsmasq"], ctx.ran)
        dnsmasq_upstream.apply(Ctx(), self.conf, self.resolv)     # twice is the same as once
        self.assertEqual(self.conf.read_text(), text)

    def test_a_station_off_systemd_resolved_is_left_alone(self):
        self.resolv.write_text(PLAIN)
        self.old_file()
        before = self.conf.read_text()
        dnsmasq_upstream.apply(Ctx(), self.conf, self.resolv)
        self.assertEqual(self.conf.read_text(), before)

    def test_one_written_by_hand_is_kept(self):
        self.resolv.write_text(RESOLVED)
        self.conf.write_text("interface=eth0\n")
        dnsmasq_upstream.apply(Ctx(), self.conf, self.resolv)
        self.assertEqual(self.conf.read_text(), "interface=eth0\n")


if __name__ == "__main__":
    unittest.main()
