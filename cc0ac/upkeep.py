"""
Looking after the computer the station runs on: its disks' health, and a backup.

    python3 upkeep.py disks --out <root>/disks.json      # as root: smartctl needs it
    python3 upkeep.py backup --config config.ini         # as the station's user

The boot loader runs `disks` once a day and `backup` once a week, at the update
time ([updates] time). The station reads what they found: a problem becomes one
message the way "no readings" is sent ([alerts]), and a line on the dashboard.

Disks: each disk smartctl can see (smartmontools). A problem is the disk saying
it is failing, an attribute its maker counts as failing, or sectors moved,
waiting to be moved, or unreadable (SMART 5, 197, 198); for NVMe a critical
warning or media errors. An SD card has no SMART, so a Pi on one reports nothing.

Backup ([backup] path): config.ini, the readings database (a consistent copy
taken while the station runs), and history.csv, in one file named
cc0ac-weather-<computer>-<date>.tar.gz. The newest `keep` (8) are kept and older
ones of this computer deleted. The path is a folder, or a Windows share,
\\\\server\\share\\folder, reached with smbclient on Linux and macOS (user and
password from [backup]) and directly on Windows.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

KEEP = 8
WATCHED = {5: "sectors moved to spares", 197: "sectors waiting to be moved", 198: "unreadable sectors"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1))
    os.chmod(tmp, 0o644)
    tmp.replace(path)


# ── Disks ────────────────────────────────────────────────────────────────────

def _smartctl(*args: str) -> tuple[int, dict]:
    p = subprocess.run(["smartctl", "-j", *args], capture_output=True, text=True, timeout=120)
    try:
        return p.returncode, json.loads(p.stdout)
    except ValueError:
        return p.returncode, {}


def disk_problems(doc: dict, rc: int) -> list[str]:
    """What is wrong with one disk, from `smartctl -j -H -A -i` and its exit code."""
    problems = []
    if doc.get("smart_status", {}).get("passed") is False:
        problems.append("says it is failing")
    if rc & 16:
        problems.append("has an attribute its maker counts as failing")
    for a in doc.get("ata_smart_attributes", {}).get("table", []):
        raw = a.get("raw", {}).get("value") or 0
        if a.get("id") in WATCHED and raw > 0:
            problems.append(f"{raw} {WATCHED[a['id']]}")
    nvme = doc.get("nvme_smart_health_information_log") or {}
    if nvme.get("critical_warning"):
        problems.append(f"critical warning {nvme['critical_warning']}")
    if nvme.get("media_errors"):
        problems.append(f"{nvme['media_errors']} media errors")
    return problems


def check_disks() -> dict:
    if not shutil.which("smartctl"):
        return {"checked": _now(), "problems": [], "unchecked": "smartctl isn't installed (smartmontools)"}
    _, scan = _smartctl("--scan-open")
    disks, problems = [], []
    for d in scan.get("devices", []):
        rc, doc = _smartctl("-H", "-A", "-i", "-d", d.get("type", "auto"), d["name"])
        if rc & 3 or not doc:   # couldn't open it, or it doesn't do SMART
            continue
        model = doc.get("model_name") or doc.get("model_family") or "disk"
        name = f"{model} ({Path(d['name']).name})"
        found = disk_problems(doc, rc)
        disks.append({"name": name, "problems": found})
        problems += [f"Disk {name}: {p}" for p in found]
    out = {"checked": _now(), "disks": disks, "problems": problems}
    if not disks:
        out["unchecked"] = "no disk here reports SMART (an SD card can't)"
    return out


# ── Backup ───────────────────────────────────────────────────────────────────

def is_share(path: str) -> bool:
    return path.startswith(("\\\\", "//"))


def split_share(path: str) -> tuple[str, str]:
    r"""\\server\share\a\b -> ("//server/share", "a\b")"""
    parts = [p for p in re.split(r"[\\/]+", path) if p]
    if len(parts) < 2:
        raise ValueError(f"a share path looks like \\\\server\\share\\folder, not {path!r}")
    return f"//{parts[0]}/{parts[1]}", "\\".join(parts[2:])


def snapshot(cfg: configparser.ConfigParser, config_path: Path, dest: Path) -> Path:
    """The backup file, made in folder dest."""
    write_path = Path(cfg.get("device", "write_path")).expanduser()
    db = Path(cfg.get("storage", "database", fallback=str(write_path / "weather.db"))).expanduser()
    name = f"cc0ac-weather-{socket.gethostname()}-{datetime.now():%Y%m%d-%H%M}.tar.gz"
    out = dest / name
    with tempfile.TemporaryDirectory(dir=dest) as tmp:
        files = {"config.ini": config_path}
        if db.exists():
            copy = Path(tmp) / "weather.db"
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as src, sqlite3.connect(copy) as dst:
                src.backup(dst)
            files["weather.db"] = copy
        if (write_path / "history.csv").exists():
            files["history.csv"] = write_path / "history.csv"
        with tarfile.open(out, "w:gz") as tar:
            for arcname, path in files.items():
                tar.add(path, arcname=f"cc0ac-weather-backup/{arcname}")
    return out


def ours(name: str) -> bool:
    return re.fullmatch(rf"cc0ac-weather-{re.escape(socket.gethostname())}-\d{{8}}-\d{{4}}\.tar\.gz", name) is not None


class Folder:
    def __init__(self, path: str):
        self.path = Path(path)

    def put(self, file: Path) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        tmp = self.path / (file.name + ".part")
        shutil.copyfile(file, tmp)
        tmp.replace(self.path / file.name)

    def names(self) -> list[str]:
        return [p.name for p in self.path.iterdir()]

    def delete(self, name: str) -> None:
        (self.path / name).unlink()


class Share:
    """A Windows share through smbclient, nothing mounted. The password goes in a
    file only this user can read, never on the command line where ps shows it."""

    def __init__(self, path: str, user: str, password: str):
        if not shutil.which("smbclient"):
            raise RuntimeError("smbclient isn't installed")
        self.service, self.folder = split_share(path)
        self.user, self.password = user, password

    def _do(self, commands: str) -> str:
        with tempfile.NamedTemporaryFile("w", prefix="cc0ac-smb-", delete=False) as auth:
            os.chmod(auth.name, 0o600)
            domain, _, user = self.user.rpartition("\\")
            auth.write(f"username = {user}\npassword = {self.password}\n" + (f"domain = {domain}\n" if domain else ""))
        try:
            cmd = ["smbclient", self.service, "-A", auth.name] if self.user else ["smbclient", self.service, "-N"]
            p = subprocess.run(cmd + ["-c", commands], capture_output=True, text=True, timeout=600)
        finally:
            os.unlink(auth.name)
        text = p.stdout + p.stderr
        bad = re.findall(r"NT_STATUS_\w+", text)
        bad = [b for b in bad if b not in ("NT_STATUS_OBJECT_NAME_COLLISION", "NT_STATUS_NO_SUCH_FILE")]
        if bad:
            raise RuntimeError(f"{self.service}: {bad[0] if bad else text.strip()[-200:]}")
        return text

    def _cd(self) -> str:
        steps, path = [], ""
        for part in self.folder.split("\\") if self.folder else []:
            path = f"{path}\\{part}" if path else part
            steps.append(f'mkdir "{path}"')
        return "; ".join(steps + ([f'cd "{self.folder}"'] if self.folder else []))

    def put(self, file: Path) -> None:
        self._do(f'{self._cd()}; lcd "{file.parent}"; put "{file.name}"'.lstrip("; "))

    def names(self) -> list[str]:
        out = self._do(f'{self._cd()}; ls cc0ac-weather-*'.lstrip("; "))
        return re.findall(r"^\s+(cc0ac-weather-\S+\.tar\.gz)\s", out, re.M)

    def delete(self, name: str) -> None:
        self._do(f'{self._cd()}; del "{name}"'.lstrip("; "))


def destination(cfg: configparser.ConfigParser):
    path = cfg.get("backup", "path", fallback="").strip()
    if is_share(path) and os.name != "nt":
        return Share(path, cfg.get("backup", "user", fallback="").strip(), cfg.get("backup", "password", raw=True, fallback=""))
    return Folder(path)


def backup(cfg: configparser.ConfigParser, config_path: Path) -> dict:
    """Make one backup, send it, keep the newest `keep`. Returns the record of it."""
    path = cfg.get("backup", "path", fallback="").strip()
    record = {"at": _now(), "to": path}
    if not path:
        return {**record, "ok": None, "problem": "no [backup] path"}
    keep = cfg.getint("backup", "keep", fallback=KEEP)
    try:
        with tempfile.TemporaryDirectory(prefix="cc0ac-backup-") as tmp:
            file = snapshot(cfg, config_path, Path(tmp))
            dest = destination(cfg)
            dest.put(file)
            mine = sorted(n for n in dest.names() if ours(n))
            if file.name not in mine:
                raise RuntimeError("the backup isn't there after copying it")
            for old in mine[:-keep] if keep > 0 else []:
                dest.delete(old)
            return {**record, "ok": True, "file": file.name, "bytes": file.stat().st_size,
                    "kept": min(len(mine), keep) if keep > 0 else len(mine)}
    except (OSError, RuntimeError, ValueError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        return {**record, "ok": False, "problem": str(exc)}


def backup_record_path() -> Path:
    return Path.home() / ".cc0ac-weather" / "backup.json"


def write_record(result: dict) -> None:
    _write_json(backup_record_path(), result)


# ── What the station shows ───────────────────────────────────────────────────

def low_batteries(sensors) -> list[str]:
    """A line for each sensor whose battery reads low, and one for the hub's own.
    A sensor sends only normal or low; the Access hub sends hubbattery with every
    reading, though AcuRite's own app never said when it was low."""
    out, hub = [], False
    for e in sensors:
        f = e.get("fields") or {}
        if f.get("battery") and str(f["battery"]).lower() != "normal":
            out.append(f"The battery in {e.get('name') or e.get('sensor_id')} is low.")
        hub = hub or bool(f.get("hub_battery") and str(f["hub_battery"]).lower() != "normal")
    return (["The hub's batteries are low."] if hub else []) + out


def problems(boot_root: Path | None) -> list[str]:
    """Every standing problem, one line each, for the dashboard and the alerts."""
    out = []
    if boot_root is not None:
        try:
            out += json.loads((boot_root / "disks.json").read_text()).get("problems", [])
        except (OSError, ValueError):
            pass
    try:
        b = json.loads(backup_record_path().read_text())
        if b.get("ok") is False:
            out.append(f"The backup to {b.get('to')} failed: {b.get('problem')}")
    except (OSError, ValueError):
        pass
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("disks")
    d.add_argument("--out", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--config", required=True)
    args = p.parse_args(argv)
    if args.cmd == "disks":
        result = check_disks()
        _write_json(Path(args.out), result)
        print("; ".join(result["problems"]) or result.get("unchecked") or f"{len(result['disks'])} disk(s) fine")
    else:
        cfg = configparser.ConfigParser()   # read as the station reads it
        cfg.read(args.config)
        result = backup(cfg, Path(args.config))
        if result["ok"] is not None:
            write_record(result)
        print(f"backed up: {result['file']} ({result['kept']} kept)" if result["ok"] else f"not backed up: {result['problem']}")
        sys.exit(0 if result["ok"] is not False else 1)


if __name__ == "__main__":
    main()
