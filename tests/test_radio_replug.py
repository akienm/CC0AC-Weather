"""The station may unplug and replug its radio, in software: python3 -m unittest tests/test_radio_replug.py

Born of 2026-11-09..11: rtl_433 would not start for two days, and only a hand at the
USB port was left to try."""
import importlib.util
import logging
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("radio_replug", HERE / "updates/migrations/0009_radio_replug.py")
radio_replug = importlib.util.module_from_spec(spec)
spec.loader.exec_module(radio_replug)
from updates import bootloader   # noqa: E402
from cc0ac.sources import rtl_433   # noqa: E402


class RadioReplug(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.script, self.sudoers = self.root / "libexec" / "replug-radio", self.root / "sudoers.d" / "radio"
        self.ctx = SimpleNamespace(user="akien", image=False, log=logging.getLogger("t"),
                                   ours_or_absent=bootloader.ours_or_absent, write_ours=bootloader.write_ours)

    def tearDown(self):
        self.tmp.cleanup()

    def apply(self):
        radio_replug.apply(self.ctx, self.script, self.sudoers)

    def test_the_rule_allows_the_script_alone_with_no_arguments(self):
        self.assertEqual(radio_replug.rule("akien"),
                         'akien ALL=(root) NOPASSWD: /usr/local/libexec/cc0ac-weather/replug-radio ""\n')

    def test_the_script_is_runnable_and_names_only_rtl_sdr_radios(self):
        self.apply()
        text = self.script.read_text()
        self.assertTrue(text.startswith("#!/bin/sh\n# " + bootloader.MARK_TEXT))
        self.assertEqual(self.script.stat().st_mode & 0o777, 0o755)
        self.assertIn("0bda:2838|0bda:2832) ;;", text)
        self.assertIn('echo 0 > "$d/authorized"', text)

    @unittest.skipUnless(shutil.which("visudo"), "no visudo here")
    def test_the_rule_is_written_when_visudo_passes_it(self):
        self.apply()
        self.assertEqual(self.sudoers.stat().st_mode & 0o777, 0o440)
        self.assertIn(f'akien ALL=(root) NOPASSWD: {self.script} ""', self.sudoers.read_text())

    def test_a_rule_visudo_refuses_is_not_written(self):
        self.ctx.user = "akien bad="     # not a sudoers user name
        with self.assertLogs("t", "WARNING"):
            self.apply()
        self.assertFalse(self.sudoers.exists())
        self.assertTrue(self.script.exists())

    def test_a_station_run_as_root_needs_no_rule(self):
        self.ctx.user = "root"
        self.apply()
        self.assertFalse(self.sudoers.exists())
        self.assertTrue(self.script.exists())

    def test_a_file_someone_else_wrote_is_left_alone(self):
        self.sudoers.parent.mkdir(parents=True)
        self.sudoers.write_text("theirs\n")
        self.apply()
        self.assertEqual(self.sudoers.read_text(), "theirs\n")

    def test_without_the_script_the_station_says_so_and_goes_on(self):
        said = rtl_433.replug_dongles(self.root / "absent")
        self.assertIn("isn't installed", said[0])

    def test_the_station_reports_what_the_script_said(self):
        fake = self.root / "fake"
        fake.write_text("#!/bin/sh\necho no RTL-SDR radio on USB; exit 2\n")
        fake.chmod(0o755)
        saved = rtl_433.os.geteuid
        rtl_433.os.geteuid = lambda: 0      # as root: run it directly, no sudo
        try:
            self.assertEqual(rtl_433.replug_dongles(fake), ["replug failed (exit 2)", "no RTL-SDR radio on USB"])
        finally:
            rtl_433.os.geteuid = saved


if __name__ == "__main__":
    unittest.main()
