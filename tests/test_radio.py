"""A USB radio by plugging it in and ticking a box: the setup page sees the radio, the
answer turns [capture] on with its band, and the migration installs the radio software.
python3 -m unittest tests/test_radio.py"""
import configparser
import importlib.util
import json
import logging
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from cc0ac import wizard   # noqa: E402

spec = importlib.util.spec_from_file_location("radio_migration", HERE / "updates/migrations/0007_radio.py")
radio_migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(radio_migration)

EXAMPLE = (HERE / "config.ini.example").read_text()


def usb_device(root: Path, name: str, vendor: str, product: str, labels=("", ""), driver: str = "") -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "idVendor").write_text(vendor + "\n")
    (d / "idProduct").write_text(product + "\n")
    for f, text in zip(("manufacturer", "product"), labels):
        if text:
            (d / f).write_text(text + "\n")
    if driver:
        drivers = root / "_drivers" / driver
        drivers.mkdir(parents=True, exist_ok=True)
        (d / f"{name}:1.0").mkdir()
        os.symlink(drivers, d / f"{name}:1.0" / "driver")


def parsed(text: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"), interpolation=None)
    cfg.read_string(text)
    return cfg


class SeesTheRadio(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.usb = Path(self.tmp.name) / "usb"
        self.usb.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_rtl_sdr_blog_v3_is_found_by_its_usb_id(self):
        usb_device(self.usb, "1-1.2", "0bda", "2838", ("RTLSDRBlog", "Blog V3"))
        usb_device(self.usb, "1-1.3", "046d", "c52b", ("Logitech", "USB Receiver"))   # a keyboard's
        self.assertEqual(wizard.radios(self.usb), [{"name": "RTLSDRBlog Blog V3", "tv_driver": ""}])

    def test_an_older_stick_without_names(self):
        usb_device(self.usb, "1-1.4", "0BDA", "2832")
        self.assertEqual(wizard.radios(self.usb), [{"name": "RTL-SDR radio", "tv_driver": ""}])

    def test_a_radio_the_tv_driver_holds_says_so(self):
        usb_device(self.usb, "1-1.2", "0bda", "2838", ("Realtek", "RTL2838UHIDIR"), driver="dvb_usb_rtl28xxu")
        self.assertEqual(wizard.radios(self.usb)[0]["tv_driver"], "dvb_usb_rtl28xxu")

    def test_nothing_plugged_in(self):
        self.assertEqual(wizard.radios(self.usb), [])

    def test_a_computer_that_does_not_show_its_usb(self):
        self.assertIsNone(wizard.radios(Path(self.tmp.name) / "no-sysfs"))

    def test_the_setup_state_carries_the_radio(self):
        usb_device(self.usb, "1-1.2", "0bda", "2838", ("RTLSDRBlog", "Blog V4"))
        now = datetime.now(timezone.utc).timestamp()
        (Path(self.tmp.name) / "current.json").write_text(json.dumps({"sensors": [
            {"sensor_id": "00000505", "type": "Atlas",
             "reception": {"radio": {"last": datetime.fromtimestamp(now - 120, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}}}]}))
        cfg = parsed(wizard.set_values(EXAMPLE, "device", {"write_path": self.tmp.name}))
        with mock.patch.object(wizard.shutil, "which", return_value="/usr/bin/rtl_433"):
            state = wizard.radio_state(cfg, self.usb)
        self.assertTrue(state["installed"])
        self.assertFalse(state["enabled"])
        self.assertEqual(state["frequency"], "433.92M")   # unset in config.ini.example: rtl_433's default
        self.assertEqual(state["radios"][0]["name"], "RTLSDRBlog Blog V4")
        self.assertIn("915M", state["bands"])
        self.assertAlmostEqual(state["heard_minutes"], 2.0, delta=0.5)
        with mock.patch.object(wizard.shutil, "which", return_value=None):
            self.assertFalse(wizard.radio_state(cfg, self.usb)["installed"])

    def test_setup_state_includes_it(self):
        cfg = parsed(wizard.set_values(EXAMPLE, "hub", {"enabled": "false"}))
        self.assertIn("radio", wizard.setup_state(cfg))


class SavesTheRadio(unittest.TestCase):
    def test_ticked_with_915_turns_capture_on(self):
        text = wizard.apply_setup(EXAMPLE, {"radio": {"enabled": True, "frequency": "915M"}})
        cfg = parsed(text)
        self.assertTrue(cfg.getboolean("capture", "enabled"))
        self.assertEqual(cfg.get("capture", "frequency"), "915M")
        self.assertIn("; Radio capture with a USB SDR and rtl_433.", text)   # comments kept

    def test_915_hears_more_than_acurite(self):
        cfg = parsed(wizard.apply_setup(EXAMPLE, {"radio": {"enabled": True, "frequency": "915M"}}))
        self.assertEqual(cfg.get("capture", "protocols"), "all")
        own = wizard.set_values(EXAMPLE, "capture", {"protocols": "78, 113"})
        self.assertEqual(parsed(wizard.apply_setup(own, {"radio": {"enabled": True, "frequency": "868M"}}))
                         .get("capture", "protocols"), "78, 113")   # chosen by hand: kept
        self.assertEqual(parsed(wizard.apply_setup(EXAMPLE, {"radio": {"enabled": True, "frequency": "433.92M"}}))
                         .get("capture", "protocols"), "40,74")

    def test_433_is_left_unset(self):
        text = wizard.apply_setup(wizard.set_values(EXAMPLE, "capture", {"frequency": "915M"}),
                                  {"radio": {"enabled": True, "frequency": "433.92M"}})
        self.assertFalse(parsed(text).has_option("capture", "frequency"))

    def test_unticked_turns_it_off(self):
        on = wizard.set_values(EXAMPLE, "capture", {"enabled": "true"})
        self.assertFalse(parsed(wizard.apply_setup(on, {"radio": {"enabled": False, "frequency": ""}}))
                         .getboolean("capture", "enabled"))

    def test_hopping_between_two_bands_as_config_ini_has_it(self):
        text = wizard.apply_setup(EXAMPLE, {"radio": {"enabled": True, "frequency": "433.92M, 915M"}})
        self.assertEqual(parsed(text).get("capture", "frequency"), "433.92M, 915M")

    def test_not_a_frequency_is_refused(self):
        for bad in ("915M; rm -rf /", "lots", "915M\nread = /etc/shadow"):
            with self.assertRaises(ValueError):
                wizard.apply_setup(EXAMPLE, {"radio": {"enabled": True, "frequency": bad}})

    def test_no_radio_answer_leaves_capture_alone(self):
        on = wizard.set_values(EXAMPLE, "capture", {"enabled": "true"})
        self.assertTrue(parsed(wizard.apply_setup(on, {})).getboolean("capture", "enabled"))

    def test_the_page_sends_it(self):
        page = (HERE / "setup.html").read_text()
        self.assertIn('radio: {enabled: $("radioon").checked, frequency: $("band").value}', page)
        self.assertIn("sudo apt install rtl-433 rtl-sdr", page)


class Ctx(SimpleNamespace):
    def __init__(self, tmp: Path, codes, image=False):
        super().__init__(image=image, log=logging.getLogger("t"), ran=[], codes=list(codes))
        self.ours_or_absent = lambda p: not p.exists() or "ours" in p.read_text()
        self.write_ours = lambda p, text: p.write_text("# ours\n" + text)

    def run(self, cmd, check=True, **kw):
        self.ran.append(cmd)
        code = self.codes.pop(0) if cmd[:2] == ["apt-get", "install"] and self.codes else 0
        return SimpleNamespace(returncode=code)


class InstallsTheSoftware(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.blacklist = Path(self.tmp.name) / "cc0ac-weather-radio.conf"

    def tearDown(self):
        self.tmp.cleanup()

    def test_installs_keeps_the_tv_driver_off_and_lets_go_of_a_radio(self):
        ctx = Ctx(Path(self.tmp.name), [0])
        radio_migration.apply(ctx, self.blacklist)
        self.assertEqual(ctx.ran[0][-2:], ["rtl-433", "rtl-sdr"])
        self.assertIn("blacklist dvb_usb_rtl28xxu", self.blacklist.read_text())
        self.assertIn(["modprobe", "--remove", "dvb_usb_rtl28xxu"], ctx.ran)

    def test_in_the_image_nothing_is_unloaded(self):
        ctx = Ctx(Path(self.tmp.name), [0], image=True)
        radio_migration.apply(ctx, self.blacklist)
        self.assertTrue(self.blacklist.exists())
        self.assertFalse([c for c in ctx.ran if c[0] == "modprobe"])

    def test_an_old_package_list_is_refreshed_once(self):
        ctx = Ctx(Path(self.tmp.name), [100, 0])
        radio_migration.apply(ctx, self.blacklist)
        self.assertEqual([c[:2] for c in ctx.ran[:3]], [["apt-get", "install"], ["apt-get", "update"], ["apt-get", "install"]])
        self.assertTrue(self.blacklist.exists())

    def test_no_packages_is_a_warning_not_a_failed_update(self):
        ctx = Ctx(Path(self.tmp.name), [100, 100])
        with self.assertLogs("t", "WARNING") as said:
            radio_migration.apply(ctx, self.blacklist)
        self.assertIn("sudo apt install rtl-433 rtl-sdr", said.output[0])
        self.assertFalse(self.blacklist.exists())

    def test_a_blacklist_written_by_hand_is_kept(self):
        self.blacklist.write_text("blacklist dvb_usb_rtl28xxu  # mine\n")
        radio_migration.apply(Ctx(Path(self.tmp.name), [0]), self.blacklist)
        self.assertEqual(self.blacklist.read_text(), "blacklist dvb_usb_rtl28xxu  # mine\n")


if __name__ == "__main__":
    unittest.main()
