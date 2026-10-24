"""
--diagnostics: one file to attach to a problem report.

It bundles what a fix needs and nothing personal: the raw uploads and radio
lines of the last few days, what was sent to other services and their answers,
the service's log, the radio and console settings, the sensor IDs, and
versions. Passwords, station keys and IDs, MACs, public IP addresses, positions
and sensor names are replaced with ****. Location, alert addresses and
everything else in config.ini are left out.

Download it from the status page (/status), or: python3 acurite-capture.py --diagnostics
"""

from __future__ import annotations

import configparser
import io
import ipaddress
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONFIG_DIR = Path.home() / ".cc0ac-weather"
HERE = Path(__file__).resolve().parent
RAW_DIRS = ("hub-raw", "console-raw", "radio-raw")
# Set by the boot loader (updates/bootloader.py) for the station it starts.
BOOT_ROOT = Path(os.environ["CC0AC_BOOTLOADER_ROOT"]) if os.environ.get("CC0AC_BOOTLOADER_ROOT") else None
DAYS = 3

_KEYS = re.compile(r"\b((?:PASSKEY|MAC|ID|id|PASSWORD|hub_id|station)=)[^&\s'\"]+")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_APRS_POSITION = re.compile(r"\d{4}\.\d{2}[NS]/\d{5}\.\d{2}[EW]")   # in a CWOP packet: 3230.00N/10815.00W
# Settings whose values are someone's identity or secret, in any section.
SECRET_KEYS = ("station_id", "station_key", "password", "passcode", "key", "api_key", "user", "smtp_user",
               "smtp_password", "ntfy_topic", "email_to", "email_from", "healthcheck_url", "hub_id", "station")


def _public(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_unspecified)


def scrub(text: str, names=()) -> str:
    """The text with station keys, passwords, MACs, public IP addresses and the
    given sensor names as ****."""
    for name in sorted((n for n in names if n.strip()), key=len, reverse=True):
        text = text.replace(name, "****")
    text = _KEYS.sub(r"\1****", text)
    text = _APRS_POSITION.sub("****", text)
    return _IPV4.sub(lambda m: "****" if _public(m.group(0)) else m.group(0), text)


def _run(cmd: list[str]) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return (p.stdout + p.stderr).strip()
    except Exception as exc:   # not installed, or not allowed
        return f"({' '.join(cmd)}: {exc})"


def settings(cfg: configparser.ConfigParser) -> str:
    out = []
    for section, keys in (("capture", None), ("console", None), ("hub", ("port", "tls", "relay"))):
        if not cfg.has_section(section):
            continue
        out.append(f"[{section}]")
        for k, v in cfg.items(section, raw=True):
            if keys is None or k in keys:
                out.append(f"{k} = {'****' if k in ('station', 'hub_id') and v else v}")
        out.append("")
    if cfg.has_section("sensors"):
        out.append("[sensors] IDs (names left out): " + ", ".join(cfg.options("sensors")))
    return "\n".join(out) + "\n"


def info(cfg: configparser.ConfigParser) -> str:
    rtl = _run(["rtl_433", "-V"])
    return "\n".join([
        f"made: {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}",
        f"version: {_tail(HERE / 'VERSION', 1).strip() or '?'}",
        f"commit: {_run(['git', '-C', str(HERE), 'rev-parse', '--short', 'HEAD']) if (HERE / '.git').exists() else 'none'}",
        f"boot loader: {BOOT_ROOT or 'none'}",
        f"python: {sys.version.split()[0]}",
        f"system: {platform.platform()}",
        f"rtl_433: {rtl.splitlines()[0] if rtl else '?'}",
        f"usb: \n{_run(['lsusb'])}",
    ]) + "\n"


def recent_raw(days: int = DAYS):
    """(name in the bundle, path) for each raw log, and each log of what was
    sent (iolog.py), of the last `days` days."""
    since = f"{datetime.now(timezone.utc) - timedelta(days=days):%Y-%m-%d}"
    folders = list(RAW_DIRS) + [f"sent/{d.name}" for d in sorted((CONFIG_DIR / "sent").glob("*")) if d.is_dir()]
    for d in folders:
        for f in sorted((CONFIG_DIR / d).glob("*.log")):
            if f.stem >= since:
                yield f"{d}/{f.name}", f


def _tail(path: Path, lines: int) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:]) + "\n"
    except OSError:
        return ""


def service_log() -> str:
    """The service's own log: systemd's journal, or the file the boot loader keeps where there is none."""
    if BOOT_ROOT is None:
        return _run(["journalctl", "-u", f"cc0ac-weather@{os.environ.get('USER', '')}", "-n", "3000", "--no-pager"])
    if (BOOT_ROOT / "station.log").exists():
        return _tail(BOOT_ROOT / "station.log", 3000)
    return _run(["journalctl", "-u", "cc0ac-weather", "-n", "3000", "--no-pager"])


def bundle(cfg: configparser.ConfigParser, out_dir: Path | None = None) -> Path:
    out_dir = out_dir or Path.home()
    path = out_dir / f"cc0ac-diagnostics-{datetime.now():%Y%m%d-%H%M}.tar.gz"
    names = [v for _, v in cfg.items("sensors", raw=True)] if cfg.has_section("sensors") else []
    # Station IDs, keys and the like, wherever they turn up (a CWOP answer names its station).
    names += [v.strip() for s in cfg.sections() if s != "sensors"
              for k, v in cfg.items(s, raw=True) if k in SECRET_KEYS and len(v.strip()) >= 4]
    parts = {
        "info.txt": info(cfg),
        "settings.txt": settings(cfg),
        "service-log.txt": service_log(),
    }
    if BOOT_ROOT is not None:
        parts["updates.log"] = _tail(BOOT_ROOT / "updates.log", 3000)
        parts["boot-state.json"] = _tail(BOOT_ROOT / "state.json", 200)
        parts["disks.json"] = _tail(BOOT_ROOT / "disks.json", 200)
    parts["backup.json"] = _tail(CONFIG_DIR / "backup.json", 50)
    write_path = Path(cfg.get("device", "write_path", fallback="~/AcuRite")).expanduser()
    try:
        # What the readings decoded to, with the sensors' names left out.
        current = json.loads((write_path / "current.json").read_text())
        parts["current.json"] = json.dumps([{k: v for k, v in e.items() if k != "name"}
                                            for e in current.get("sensors", [])], indent=1)
    except (OSError, ValueError):
        pass
    for name, f in recent_raw():
        parts[name] = f.read_text(errors="replace")
    with tarfile.open(path, "w:gz") as tar:
        for name, text in parts.items():
            data = scrub(text, names).encode()
            member = tarfile.TarInfo(f"cc0ac-diagnostics/{name}")
            member.size, member.mtime = len(data), int(time.time())
            tar.addfile(member, io.BytesIO(data))
    return path
