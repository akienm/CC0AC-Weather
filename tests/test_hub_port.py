"""Checks that the hub's cable can go into any wired port, not only eth0.
python3 -m unittest tests/test_hub_port.py

These need no network: the ports are a made-up /sys/class/net, and the migration
writes into a list instead of /etc.
"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "updates"))
import bootloader as bl   # noqa: E402

spec = importlib.util.spec_from_file_location("hub_network", HERE / "updates/migrations/0003_hub_network.py")
hub_network = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hub_network)


def port(net: Path, name: str, kind="1", usb=False, wifi=False, carrier="0", hardware=True):
    d = net / name
    d.mkdir()
    (d / "type").write_text(kind + "\n")
    if carrier is not None:
        (d / "carrier").write_text(carrier + "\n")
    if hardware:
        bus = net.parent / "devices" / ("usb1/1-1" if usb else "pci0000:00/0000:01:00.0") / name
        bus.mkdir(parents=True)
        (d / "device").symlink_to(bus)
    if wifi:
        (d / "wireless").mkdir()


class Ports(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.net = Path(self.tmp.name) / "net"
        self.net.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_laptop_with_a_usb_adapter(self):
        port(self.net, "lo", kind="772", hardware=False)
        port(self.net, "enp1s0", carrier="1")                  # the house network
        port(self.net, "enx00e04c000001", usb=True, carrier="1")
        port(self.net, "wlp2s0", wifi=True)
        port(self.net, "docker0", hardware=False)
        self.assertEqual(bl.hub_ports(self.net, routed={"enp1s0"}),
                         [{"name": "enx00e04c000001", "usb": True, "cable": True}])

    def test_a_pi_on_wifi_offers_eth0(self):
        port(self.net, "eth0", carrier=None)                    # down: no carrier to read
        port(self.net, "wlan0", wifi=True)
        self.assertEqual(bl.hub_ports(self.net, routed={"wlan0"}),
                         [{"name": "eth0", "usb": False, "cable": False}])

    def test_the_way_to_your_network_is_never_offered(self):
        port(self.net, "eth0", carrier="1")
        self.assertEqual(bl.hub_ports(self.net, routed={"eth0"}), [])


class Outside(unittest.TestCase):
    def ran(self, answers):
        tried = []
        def run(cmd, **kw):
            tried.append(cmd[0])
            if answers.get(cmd[0]) is None:
                raise FileNotFoundError(cmd[0])
            return SimpleNamespace(returncode=0 if answers[cmd[0]] else 1)
        return tried, run

    def test_google_answering_is_enough(self):
        tried, run = self.ran({"ping": True, "curl": False})
        self.assertTrue(bl.reaches_outside("wlan0", run))
        self.assertEqual(tried, ["ping"])

    def test_pings_blocked_but_github_answers(self):
        self.assertTrue(bl.reaches_outside("wlan0", self.ran({"ping": False, "curl": True})[1]))

    def test_no_ping_installed(self):
        self.assertTrue(bl.reaches_outside("wlan0", self.ran({"curl": True})[1]))

    def test_nothing_answers(self):
        self.assertFalse(bl.reaches_outside("wlan0", self.ran({"ping": False, "curl": False})[1]))


class Migration(unittest.TestCase):
    def apply(self, **settings):
        written = {}
        with tempfile.TemporaryDirectory() as d:
            avahi = Path(d) / "avahi-daemon.conf"
            avahi.write_text("[server]\n#deny-interfaces=eth1\n")
            hub_network.AVAHI, hub_network.NFT = avahi, Path(d) / "nftables.conf"
            ctx = SimpleNamespace(hub_network=True, image=True, log=bl.log, run=lambda *a, **k: None,
                                  ours_or_absent=lambda p: True,
                                  write_ours=lambda p, text, *a: written.__setitem__(str(p), text), **settings)
            hub_network.apply(ctx)
            written["avahi"] = avahi.read_text()
        return written

    def test_the_chosen_port_is_the_hub_s_everywhere(self):
        w = self.apply(hub_interface="enx00e04c000001")
        self.assertIn("interface-name=enx00e04c000001", w[str(hub_network.NM_FILE)])
        self.assertIn("interface=enx00e04c000001", w[str(hub_network.DNSMASQ_FILE)])
        self.assertIn('iifname "enx00e04c000001" tcp dport 443 accept', w[str(hub_network.NFT)])
        self.assertIn("deny-interfaces=enx00e04c000001", w["avahi"])
        self.assertNotIn("eth0", "".join(w.values()))

    def test_a_boot_loader_that_never_said_means_eth0(self):
        w = self.apply()
        self.assertIn("interface=eth0", w[str(hub_network.DNSMASQ_FILE)])


class Settings(unittest.TestCase):
    def test_the_port_reaches_the_migrations(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "bootloader.json").write_text(bl.json.dumps({
                "platform": "debian", "config": str(root / "c.ini"), "home": d, "user": None,
                "python": sys.executable, "releases": d, "hub_network": True, "hub_interface": "enp2s0"}))
            (root / "state.json").write_text(bl.json.dumps({"current": None, "previous": None, "migrations": [],
                                                            "security": 0, "weekday": 0, "jitter_min": 0}))
            self.assertEqual(bl.Boot(root).ctx(root).hub_interface, "enp2s0")

if __name__ == "__main__":
    unittest.main()
