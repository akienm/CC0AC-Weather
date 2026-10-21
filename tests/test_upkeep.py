"""Checks for the disk check and the backup: python3 -m unittest tests/test_upkeep.py

The disk check is fed smartctl's JSON as the samples below; smartctl itself isn't run.
The backup goes to a folder; a share (smbclient) is not reached here."""
import configparser
import json
import os
import socket
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "updates"))
import alerts       # noqa: E402
import bootloader as bl   # noqa: E402
import upkeep       # noqa: E402

HEALTHY_SSD = {"smart_status": {"passed": True}, "ata_smart_attributes": {"table": [
    {"id": 5, "raw": {"value": 0}}, {"id": 9, "raw": {"value": 4012}}, {"id": 197, "raw": {"value": 0}}]}}
WORN_SSD = {"smart_status": {"passed": True}, "ata_smart_attributes": {"table": [
    {"id": 5, "raw": {"value": 12}}, {"id": 198, "raw": {"value": 3}}]}}
FAILING_NVME = {"smart_status": {"passed": False},
                "nvme_smart_health_information_log": {"critical_warning": 4, "media_errors": 2}}


class Disks(unittest.TestCase):
    def test_a_healthy_disk_has_no_problems(self):
        self.assertEqual(upkeep.disk_problems(HEALTHY_SSD, 0), [])

    def test_moved_and_unreadable_sectors_are_problems(self):
        self.assertEqual(upkeep.disk_problems(WORN_SSD, 0),
                         ["12 sectors moved to spares", "3 unreadable sectors"])

    def test_a_disk_that_says_it_is_failing(self):
        self.assertEqual(upkeep.disk_problems(FAILING_NVME, 8),
                         ["says it is failing", "critical warning 4", "2 media errors"])
        self.assertIn("has an attribute its maker counts as failing", upkeep.disk_problems(HEALTHY_SSD, 16))


class Backup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        readings = self.d / "readings"
        readings.mkdir()
        with sqlite3.connect(readings / "weather.db") as db:
            db.execute("create table r (t text, v real)")
            db.execute("insert into r values ('2026-10-21T06:00', 61.5)")
        db.close()
        (readings / "history.csv").write_text("time,temp\n2026-10-21T06:00,61.5\n")
        self.dest = self.d / "nas" / "weather"
        self.config = self.d / "config.ini"
        self.config.write_text(f"[device]\nwrite_path = {readings}\n[backup]\npath = {self.dest}\nkeep = 3\n")
        self.cfg = configparser.ConfigParser()
        self.cfg.read(self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def test_split_share(self):
        self.assertEqual(upkeep.split_share(r"\\nas\backups\weather\pi"), ("//nas/backups", r"weather\pi"))
        self.assertEqual(upkeep.split_share("//nas/backups"), ("//nas/backups", ""))
        with self.assertRaises(ValueError):
            upkeep.split_share(r"\\nas")

    def test_a_backup_holds_settings_and_readings_and_keeps_the_newest(self):
        self.dest.mkdir(parents=True)
        host = socket.gethostname()
        old = [f"cc0ac-weather-{host}-2026010{n}-0600.tar.gz" for n in range(1, 5)]
        for name in old + ["cc0ac-weather-otherpc-20260101-0600.tar.gz", "notes.txt"]:
            (self.dest / name).write_text("x")
        result = upkeep.backup(self.cfg, self.config)
        self.assertTrue(result["ok"], result)
        left = sorted(p.name for p in self.dest.iterdir())
        # This computer's newest 3 stay (the new one and two old); another computer's and other files are left alone.
        self.assertEqual(left, sorted([result["file"], old[2], old[3],
                                       "cc0ac-weather-otherpc-20260101-0600.tar.gz", "notes.txt"]))
        with tarfile.open(self.dest / result["file"]) as tar:
            self.assertEqual(sorted(tar.getnames()), ["cc0ac-weather-backup/config.ini",
                                                      "cc0ac-weather-backup/history.csv",
                                                      "cc0ac-weather-backup/weather.db"])
            (self.d / "out.db").write_bytes(tar.extractfile("cc0ac-weather-backup/weather.db").read())
        with sqlite3.connect(self.d / "out.db") as db:
            self.assertEqual(db.execute("select v from r").fetchall(), [(61.5,)])
        db.close()

    def test_a_backup_that_cannot_land_says_why(self):
        (self.d / "nas").write_text("a file where the folder should be")
        result = upkeep.backup(self.cfg, self.config)
        self.assertFalse(result["ok"])
        self.assertTrue(result["problem"])

    def test_no_path_no_backup(self):
        cfg = configparser.ConfigParser()
        cfg.read_string("[device]\nwrite_path = /nonexistent\n")
        self.assertIsNone(upkeep.backup(cfg, self.config)["ok"])


class WhatTheStationShows(unittest.TestCase):
    def test_disk_problems_and_a_failed_backup_are_warnings(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "disks.json").write_text(json.dumps({"problems": ["Disk X (sda): 3 unreadable sectors"]}))
            home = os.environ.get("HOME")
            os.environ["HOME"] = str(d)
            try:
                self.assertEqual(upkeep.problems(d), ["Disk X (sda): 3 unreadable sectors"])
                upkeep.write_record({"ok": False, "to": "//nas/b", "problem": "NT_STATUS_LOGON_FAILURE"})
                self.assertEqual(upkeep.problems(d)[1], "The backup to //nas/b failed: NT_STATUS_LOGON_FAILURE")
                upkeep.write_record({"ok": True, "to": "//nas/b"})
                self.assertEqual(len(upkeep.problems(d)), 1)
                self.assertEqual(upkeep.problems(None), [])
            finally:
                os.environ["HOME"] = home

    def test_one_alert_then_one_all_clear(self):
        sent = []
        real = alerts.send
        alerts.send = lambda cfg, title, msg: sent.append(title) or []
        try:
            with tempfile.TemporaryDirectory() as d:
                state = Path(d) / "state.json"
                state.write_text(json.dumps({"alerted": True}))
                cfg = configparser.ConfigParser()
                cfg.read_string("[alerts]\nenabled = true\nntfy_topic = t\n[station]\nname = S\n")
                self.assertEqual(alerts.check_upkeep(cfg, ["disk bad"], state), "upkeep alerted (1)")
                self.assertEqual(alerts.check_upkeep(cfg, ["disk bad"], state), "upkeep: same problems")
                self.assertEqual(alerts.check_upkeep(cfg, [], state), "upkeep all clear sent")
                self.assertEqual(alerts.check_upkeep(cfg, [], state), "upkeep: fine")
                self.assertEqual(sent, ["S: needs looking after", "S: looked after"])
                self.assertTrue(json.loads(state.read_text())["alerted"])   # the readings check's state is kept
        finally:
            alerts.send = real


class WhenUpkeepRuns(unittest.TestCase):
    def test_daily_disks_weekly_backup_with_updates_off_too(self):
        c = configparser.ConfigParser()
        c["updates"] = {"automatic": "no", "time": "06:00"}
        boot = SimpleNamespace(state={"weekday": 2, "jitter_min": 7}, cfg=lambda: c)
        self.assertIsNone(bl.upkeep_due(boot, datetime(2026, 10, 21, 6, 36)))          # before 06:00 + 7 + 30
        self.assertEqual(bl.upkeep_due(boot, datetime(2026, 10, 21, 6, 37)), "disks+backup")  # its weekday
        self.assertEqual(bl.upkeep_due(boot, datetime(2026, 10, 22, 6, 37)), "disks")
        boot.state["upkeep_day"] = "2026-10-22"
        self.assertIsNone(bl.upkeep_due(boot, datetime(2026, 10, 22, 23, 0)))         # once a day


if __name__ == "__main__":
    unittest.main()
