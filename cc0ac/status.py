"""System status: /status.json, what the status page shows (uptime, history depth, each receiver and service)."""

from __future__ import annotations

import configparser
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from cc0ac import diagnostics, iolog, sensor_config, upkeep
from cc0ac.config import database_path, page_title, station_version
from cc0ac.sources import acurite_access
from cc0ac.store import epoch_to_iso

#
# /status (status.html, from /status.json): how long it has run, how deep the
# history goes, each sensor and each receiver with when it was last heard, and
# each service with when it was last sent to and how that went. Every log
# behind them opens from the page (/log), and the diagnostics file downloads
# from it (/diagnostics). Your own network only, like the settings.

STARTED = time.time()

CONFIG_DIR = Path.home() / ".cc0ac-weather"

LOG_DAYS_LISTED = 7

LOG_PATH = re.compile(r"(hub-raw|radio-raw|console-raw|sent/[a-z0-9][a-z0-9_-]*)/(\d{4}-\d{2}-\d{2}\.log)")


def _raw_dirs(cfg: configparser.ConfigParser) -> dict[str, Path]:
    """Each receiver's raw log folder: what it heard, exactly, one file per UTC day."""
    return {"hub": acurite_access._raw_dir(cfg), "radio": CONFIG_DIR / "radio-raw",
            "console": CONFIG_DIR / "console-raw"}


def log_folder(cfg: configparser.ConfigParser, folder: str) -> Path | None:
    if folder.startswith("sent/"):
        return iolog.SENT_DIR / folder[5:] if iolog.SENT_DIR else None
    return _raw_dirs(cfg)[folder.split("-")[0]]


def _log_days(folder: Path | None) -> list[str]:
    """The newest few days' log files in a folder, newest first."""
    if folder is None or not folder.is_dir():
        return []
    days = sorted((f.name for f in folder.glob("*.log") if iolog.LOG_FILE.fullmatch(f.name)), reverse=True)
    return days[:LOG_DAYS_LISTED]


def _raw_mark(receiver: str, sensor_id: str) -> str:
    """What marks this sensor's lines in a receiver's raw log, to show only those."""
    if receiver == "hub":
        return f"sensor={sensor_id}"
    if receiver == "radio" and sensor_id.isdigit():
        return f'"id" : {int(sensor_id)},'          # rtl_433's AcuRite id, as it prints it
    return ""                                       # other brands: the whole log


def _machine_uptime() -> float | None:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _history_depth(db_path: Path | None) -> dict:
    if db_path is None or not db_path.exists():
        return {"readings": 0}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        first, last, count = con.execute(
            "SELECT MIN(received_utc), MAX(received_utc), COUNT(*) FROM readings").fetchone()
        sensors = con.execute("SELECT COUNT(DISTINCT sensor_id) FROM readings").fetchone()[0]
    finally:
        con.close()
    return {"first": first, "last": last, "readings": count, "sensors": sensors,
            "bytes": sum(f.stat().st_size for f in db_path.parent.glob(db_path.name + "*") if f.is_file())}


def _held_back(db_path: Path, now: float) -> dict[str, int]:
    """{sensor_id: readings with something held back by sanity.py in the last day}."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        return dict(con.execute("""SELECT sensor_id, count(*) FROM readings
            WHERE received_utc >= ? AND suspect IS NOT NULL GROUP BY sensor_id""", [epoch_to_iso(now - 86400)]).fetchall())
    finally:
        con.close()


def system_status(cfg: configparser.ConfigParser, write_path: Path, now: float | None = None) -> dict:
    now = now or time.time()
    try:
        current = json.loads((write_path / "current.json").read_text())
    except (OSError, ValueError):
        current = {}
    raw = _raw_dirs(cfg)
    try:
        held = _held_back(database_path(cfg), now)
    except sqlite3.Error:
        held = {}
    sensors: list[dict] = []
    receivers: dict[str, Any] = {}
    for e in current.get("sensors", []):
        heard = e.get("reception") or {}
        for who, r in heard.items():
            if r.get("last") and r["last"] > receivers.get(who, ""):
                receivers[who] = r["last"]
        sensors.append({"id": e.get("sensor_id"), "name": e.get("name"), "type": e.get("type"),
                        "updated": e.get("updated"), "battery": (e.get("fields") or {}).get("battery"),
                        "calibration": sensor_config.calibration(cfg).get((e.get("sensor_id") or "").lower()),
                        "suspect": e.get("suspect") or {}, "held_back_24h": held.get(e.get("sensor_id"), 0),
                        "reception": heard, "logs": {who: {"f": f"{who}-raw/{_log_days(raw[who])[0]}",
                                                           "find": _raw_mark(who, e.get("sensor_id") or "")}
                                                     for who in heard if who in raw and _log_days(raw[who])}})
    sources = []
    for who, folder in raw.items():
        days = _log_days(folder)
        if days or who in receivers:
            sources.append({"name": who, "last": receivers.get(who),
                            "logs": [f"{who}-raw/{d}" for d in days]})
    if "online" in receivers:
        sources.append({"name": "online", "last": receivers["online"], "logs": []})
    sends = []
    for name in iolog.names():
        s = iolog.latest(name) or {"name": name}
        s["logs"] = [f"sent/{name}/{d}" for d in _log_days(iolog.SENT_DIR / name if iolog.SENT_DIR else None)]
        sends.append(s)
    try:
        depth = _history_depth(database_path(cfg))
    except sqlite3.Error as e:
        depth = {"error": str(e)}
    try:
        backup = json.loads(upkeep.backup_record_path().read_text())
    except (OSError, ValueError):
        backup = None
    return {"title": page_title(cfg), "version": station_version(), "now": epoch_to_iso(now),
            "timezone": time.strftime("%Z"), "started": epoch_to_iso(STARTED), "uptime_s": round(now - STARTED),
            "machine_uptime_s": _machine_uptime(), "readings_written": current.get("written"),
            "history": depth, "sensors": sensors, "sources": sources, "sends": sends,
            "warnings": upkeep.problems(diagnostics.BOOT_ROOT) + upkeep.low_batteries(current.get("sensors", [])),
            "backup": backup}
