"""Checks for the disk check and the backup: python3 -m unittest tests/test_upkeep.py

The disk check is fed smartctl's JSON as the samples below; smartctl itself isn't run.
The backup goes to a folder; a share (smbclient) is not reached here."""
import configparser
import json
import os
import shutil
import socket
import sqlite3
import sys
import tarfile
import tempfile
import unittest
import unittest.mock
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "updates"))
from cc0ac import alerts       # noqa: E402
import bootloader as bl   # noqa: E402
from cc0ac import upkeep       # noqa: E402

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


# A stand-in for the sftp program: it runs the batch file (-b) against a folder,
# FAKE_SFTP_ROOT, as the server's disk, and keeps its arguments and what it was
# handed in FAKE_SFTP_LOG. A '-' before a command lets it fail, as sftp does.
FAKE_SFTP = r'''#!/usr/bin/env python3
import json, os, shlex, shutil, sys
from pathlib import Path
root = Path(os.environ["FAKE_SFTP_ROOT"])
args = sys.argv[1:]
batch = args[args.index("-b") + 1]
askpass = os.environ.get("SSH_ASKPASS")
with open(os.environ["FAKE_SFTP_LOG"], "a") as log:
    log.write(json.dumps({"argv": args, "password": os.popen(askpass).read().strip() if askpass else None}) + "\n")
cwd = root
def at(p):
    return root / p.lstrip("/") if p.startswith("/") else cwd / p
for line in Path(batch).read_text().splitlines():
    may_fail = line.startswith("-")
    cmd, *rest = shlex.split(line.lstrip("-"))
    try:
        if cmd == "mkdir": at(rest[0]).mkdir()
        elif cmd == "cd": cwd = at(rest[0]); assert cwd.is_dir(), rest[0]
        elif cmd == "put": shutil.copyfile(rest[0], at(rest[1]))
        elif cmd == "rm": at(rest[0]).unlink()
        elif cmd == "rename": at(rest[0]).rename(at(rest[1]))
        elif cmd == "ls": print("sftp> ls -1"); [print(p.name) for p in sorted(cwd.iterdir())]
        else: raise SystemExit(f"fake sftp: no {cmd}")
    except (OSError, AssertionError) as e:
        if not may_fail:
            print(f"{line}: {e}", file=sys.stderr); print("Connection closed", file=sys.stderr); sys.exit(1)
'''


class FakeFtp:
    """ftplib.FTP's surface, over a dict of folders; tls=None means a server with no AUTH TLS."""
    server: dict = {}
    tls_offered = True
    log: list = []

    def __init__(self, *a, **kw):
        self.path = "/"
    def connect(self, host, port): FakeFtp.log.append(("connect", host, port))
    def auth(self):
        if not FakeFtp.tls_offered:
            raise upkeep.ftplib.error_perm("500 AUTH not understood")
        FakeFtp.log.append(("auth",))
    def prot_p(self): FakeFtp.log.append(("prot_p",))
    def login(self, user, password):
        FakeFtp.log.append(("login", user, password))
        if password != "pw":
            raise upkeep.ftplib.error_perm("530 Login incorrect.")
    def set_pasv(self, on): pass
    def _here(self): return FakeFtp.server.setdefault(self.path, {})
    def cwd(self, part):
        new = "/" if part == "/" else (self.path.rstrip("/") + "/" + part)
        if new not in FakeFtp.server and new != "/":
            raise upkeep.ftplib.error_perm("550 no such folder")
        self.path = new
    def mkd(self, part): FakeFtp.server[self.path.rstrip("/") + "/" + part] = {}
    def storbinary(self, cmd, fh): self._here()[cmd.split(" ", 1)[1]] = fh.read()
    def delete(self, name):
        if name not in self._here():
            raise upkeep.ftplib.error_perm("550 not there")
        del self._here()[name]
    def rename(self, a, b): self._here()[b] = self._here().pop(a)
    def nlst(self):
        if not self._here():
            raise upkeep.ftplib.error_perm("550 No files found")
        return list(self._here())
    def quit(self): pass
    def close(self): pass


class FtpBackup(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        (self.d / "readings").mkdir()
        sqlite3.connect(self.d / "readings" / "weather.db").close()
        FakeFtp.server, FakeFtp.log, FakeFtp.tls_offered = {}, [], True
        for target in ("_ReusingFTP_TLS", "ftplib.FTP"):
            p = unittest.mock.patch(f"cc0ac.upkeep.{target}", FakeFtp)
            p.start()
            self.addCleanup(p.stop)

    def run_backup(self, path, password="pw", keep=2):
        cfg = configparser.ConfigParser(interpolation=None)
        cfg.read_dict({"device": {"write_path": str(self.d / "readings")},
                       "backup": {"path": path, "user": "anthony", "password": password, "keep": str(keep)}})
        (self.d / "config.ini").write_text("")
        return upkeep.backup(cfg, self.d / "config.ini")

    def test_split_ftp(self):
        self.assertEqual(upkeep.split_sftp("ftp://nas/backups/weather", "me", default_port=21),
                         ("me", "nas", 21, "/backups/weather"))
        with self.assertRaisesRegex(ValueError, "ftp://"):
            upkeep.split_sftp("ftp://me:secret@nas/x", default_port=21)

    def test_an_encrypting_server_gets_tls_and_the_folders_are_made(self):
        r = self.run_backup("ftp://nas/backups/weather")
        self.assertTrue(r["ok"], r)
        self.assertNotIn("unencrypted", r)
        self.assertIn(("auth",), FakeFtp.log)
        self.assertIn(("prot_p",), FakeFtp.log)
        self.assertEqual(list(FakeFtp.server["/backups/weather"]), [r["file"]])   # the .part was renamed

    def test_a_server_without_tls_gets_plain_ftp_and_the_record_says_so(self):
        FakeFtp.tls_offered = False
        r = self.run_backup("ftp://nas:2121/~/weather")
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["unencrypted"])
        self.assertIn(("connect", "nas", 2121), FakeFtp.log)
        self.assertIn("weather", [k.rsplit("/", 1)[-1] for k in FakeFtp.server])

    def test_keep_deletes_only_this_computers_older_copies(self):
        old = [f"cc0ac-weather-{socket.gethostname()}-2026010{i}-0600.tar.gz" for i in (1, 2, 3)]
        FakeFtp.server["/b"] = {n: b"" for n in old + ["cc0ac-weather-otherbox-20260101-0600.tar.gz"]}
        r = self.run_backup("ftp://nas/b")
        self.assertTrue(r["ok"], r)
        self.assertEqual(sorted(FakeFtp.server["/b"]),
                         sorted([old[2], r["file"], "cc0ac-weather-otherbox-20260101-0600.tar.gz"]))

    def test_a_refused_login_says_why(self):
        r = self.run_backup("ftp://nas/b", password="wrong")
        self.assertFalse(r["ok"])
        self.assertEqual(r["problem"], "ftp nas: 530 Login incorrect.")


class SftpBackup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        readings = self.d / "readings"
        readings.mkdir()
        (readings / "history.csv").write_text("time,temp\n")
        bindir = self.d / "bin"
        bindir.mkdir()
        (bindir / "sftp").write_text(FAKE_SFTP)
        os.chmod(bindir / "sftp", 0o755)
        self.server = self.d / "server"
        self.server.mkdir()
        self.log = self.d / "sftp.log"
        self.env = {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "FAKE_SFTP_ROOT": str(self.server),
                    "FAKE_SFTP_LOG": str(self.log), "HOME": str(self.d / "home"),
                    "SSH_ASKPASS": "/bin/false-the-desktops-own"}
        self.readings = readings

    def tearDown(self):
        self.tmp.cleanup()

    def run_backup(self, backup_section: str) -> dict:
        config = self.d / "config.ini"
        config.write_text(f"[device]\nwrite_path = {self.readings}\n[backup]\n{backup_section}")
        cfg = configparser.ConfigParser(interpolation=None)
        cfg.read(config)
        with unittest.mock.patch.dict(os.environ, self.env):
            return upkeep.backup(cfg, config)

    def calls(self) -> list[dict]:
        return [json.loads(ln) for ln in self.log.read_text().splitlines()]

    def test_split_sftp(self):
        self.assertEqual(upkeep.split_sftp("sftp://ann@nas:2222/backups/weather"), ("ann", "nas", 2222, "/backups/weather"))
        self.assertEqual(upkeep.split_sftp("sftp://nas/~/weather/", "bob"), ("bob", "nas", 22, "weather"))
        self.assertEqual(upkeep.split_sftp("SFTP://nas"), ("", "nas", 22, ""))
        self.assertEqual(upkeep.split_sftp("sftp://nas/my%20backups"), ("", "nas", 22, "/my backups"))
        for bad in ("sftp://ann:secret@nas/x", "sftp:///x", "sftp://nas:port/x"):
            with self.assertRaises(ValueError, msg=bad):
                upkeep.split_sftp(bad)

    def test_a_backup_over_sftp_makes_the_folders_and_keeps_the_newest(self):
        host = socket.gethostname()
        folder = self.server / "backups" / "weather station"
        folder.mkdir(parents=True)
        old = [f"cc0ac-weather-{host}-2026010{n}-0600.tar.gz" for n in range(1, 4)]
        for name in old + ["cc0ac-weather-otherpc-20260101-0600.tar.gz"]:
            (folder / name).write_text("x")
        result = self.run_backup("path = sftp://ann@nas:2222/backups/weather%20station/pi\nkeep = 2\n")
        self.assertTrue(result["ok"], result)
        self.assertEqual(sorted(p.name for p in (folder / "pi").iterdir()), [result["file"]])   # made, then landed
        self.assertEqual(len(list(folder.iterdir())), 5)                                      # the old ones untouched
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[-1], "ann@nas")
        self.assertEqual(argv[argv.index("-P") + 1], "2222")
        self.assertIn("PasswordAuthentication=no", argv)     # no password: keys only, never a prompt
        self.assertNotIn("BatchMode=no", argv)
        self.assertIsNone(self.calls()[0]["password"])       # the desktop's own askpass isn't passed on
        with tarfile.open(folder / "pi" / result["file"]) as tar:
            self.assertIn("cc0ac-weather-backup/history.csv", tar.getnames())

    def test_keep_deletes_only_this_computers_older_copies(self):
        host = socket.gethostname()
        old = [f"cc0ac-weather-{host}-2026010{n}-0600.tar.gz" for n in range(1, 5)]
        for name in old + ["cc0ac-weather-otherpc-20260101-0600.tar.gz", "notes.txt"]:
            (self.server / name).write_text("x")
        result = self.run_backup("path = sftp://nas/\nuser = bob\nkeep = 3\n")
        self.assertTrue(result["ok"], result)
        self.assertEqual(sorted(p.name for p in self.server.iterdir()),
                         sorted([result["file"], old[2], old[3], "cc0ac-weather-otherpc-20260101-0600.tar.gz", "notes.txt"]))
        self.assertEqual(self.calls()[0]["argv"][-1], "bob@nas")

    @unittest.skipIf(os.name == "nt", "a password login isn't offered on Windows")
    def test_the_password_is_handed_over_but_never_on_the_command_line(self):
        result = self.run_backup("path = sftp://nas/w\npassword = pa ss%word\n")
        self.assertTrue(result["ok"], result)
        for call in self.calls():
            self.assertEqual(call["password"], "pa ss%word")
            self.assertNotIn("pa ss%word", " ".join(call["argv"]))
            self.assertNotIn("PasswordAuthentication=no", call["argv"])
            self.assertLess(call["argv"].index("BatchMode=no"), call["argv"].index("-b"))
        self.assertNotIn("pa ss%word", json.dumps(result))

    def test_a_refused_login_says_why(self):
        (self.server / "w").write_text("a file where the folder should be")
        result = self.run_backup("path = sftp://nas/w\n")
        self.assertFalse(result["ok"])
        self.assertIn("sftp nas:", result["problem"])
        self.assertNotIn("Connection closed", result["problem"])

    def test_a_key_file_that_isnt_there_says_so(self):
        result = self.run_backup(f"path = sftp://nas/w\nkey_file = {self.d / 'nokey'}\n")
        self.assertFalse(result["ok"])
        self.assertIn("key_file", result["problem"])


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

    def test_low_batteries_are_warnings_the_hubs_once(self):
        sensors = [{"sensor_id": "00001234", "name": "Yard", "fields": {"battery": "normal", "hub_battery": "low"}},
                   {"sensor_id": "00005678", "name": "Shed", "fields": {"battery": "low", "hub_battery": "low"}},
                   {"sensor_id": "00009999", "fields": {"battery": "Low"}}]
        self.assertEqual(upkeep.low_batteries(sensors), ["The hub's batteries are low.", "The battery in Shed is low.",
                                                         "The battery in 00009999 is low."])
        # New batteries: the hub's next readings say normal, and the warning goes.
        self.assertEqual(upkeep.low_batteries([{"name": "Yard", "fields": {"battery": "normal", "hub_battery": "normal"}}]), [])

    def test_one_alert_then_one_all_clear(self):
        sent = []
        real = alerts.send
        alerts.send = lambda cfg, title, msg, targets=None: sent.append(title) or []
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
