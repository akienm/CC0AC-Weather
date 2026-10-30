"""The settings file: reading it, the options the pages and the daemon share, and saving it."""

from __future__ import annotations

import configparser
import logging
import os
import re
import shutil
import sys
import threading
import time
from datetime import date
from pathlib import Path
from typing import Dict

from cc0ac import sensor_config, wizard

log = logging.getLogger("acurite")

SensorNames = Dict[str, str]   # sensor id → the name [sensors] gives it
Query = Dict[str, str]         # a request's ?name=value pairs


ROOT = Path(__file__).resolve().parent.parent   # the install folder: pages, VERSION, config.ini.example

DEFAULT_CONFIG = Path.home() / ".cc0ac-weather" / "config.ini"


def load_config(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if not path.exists():
        log.error("Config not found: %s", path)
        log.error("Copy config.ini.example to %s and edit it.", path)
        sys.exit(1)
    cfg.read(path)
    if add_new_settings(path):
        cfg.read(path)
    return cfg


def add_new_settings(path: Path, example: Path = ROOT / "config.ini.example") -> list[str]:
    """Settings that came with an update, written into config.ini as they stand by
    default (every upload off), so they can be found where the rest are. Only added at the end: nothing already
    in the file is touched."""
    try:
        text = path.read_text()
        added, names = wizard.missing_sections(text, example.read_text())
        if added:
            with open(path, "a") as f:      # appended, so the file keeps its owner and permissions
                f.write(("" if text.endswith("\n") or not text else "\n") +
                        f"\n; ── Added by {station_version().split('/')[0]} on {date.today()}: settings newer than "
                        f"this file, at their defaults ──\n\n" + added)
            log.info("Config: added new settings, at their defaults: %s", ", ".join(f"[{n}]" for n in names))
        return names
    except OSError as e:
        log.warning("Config: could not add new settings to %s: %s", path, e)
        return []


ZONEINFO = Path("/usr/share/zoneinfo")


def apply_timezone(cfg: configparser.ConfigParser) -> None:
    """[station] timezone: "today", each day's rain and the records are the
    station's own days, wherever the computer's clock is set (a container's is
    usually UTC), and so is the time the hub is told. Unset, the computer's own."""
    tz = cfg.get("station", "timezone", fallback="").strip()
    if not tz:
        return
    if not re.fullmatch(r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*", tz) or not (ZONEINFO / tz).is_file():
        log.error("[station] timezone = %s is not a time zone this computer has (%s); "
                  "days follow the computer's clock", tz, ZONEINFO)
        return
    os.environ["TZ"] = tz
    time.tzset()
    log.info("Time zone: %s", tz)


def sensor_map(cfg: configparser.ConfigParser) -> SensorNames:
    """{sensor_id: display name}, in display order: each sensor's own section in
    the order they come in the file, then the older [sensors] (sensor_config.py)."""
    return sensor_config.titles(cfg)


def add_heard_sensor(path: Path | None, sensor_id: str, kind: str) -> None:
    """A sensor of the house's own heard for the first time: a bare section for it
    at the end of config.ini, its ID for a title until someone gives it one."""
    if path is None:
        return
    try:
        with open(path, "a") as f:      # appended, so the file keeps its owner and permissions
            f.write(sensor_config.new_section(sensor_id, kind, date.today().isoformat()))
        log.info("Config: a new sensor, %s (%s); added [%s] for its title and icon", sensor_id, kind, sensor_id)
    except OSError as e:
        log.warning("Config: could not add new sensor %s to %s: %s", sensor_id, path, e)


def display_order(sensors: SensorNames, sensor_id: str, name: str) -> tuple:
    """Sort key: sensors in sensor_map's order, then any others by name."""
    position = list(sensors).index(sensor_id) if sensor_id in sensors else len(sensors)
    return position, name.lower()


def database_path(cfg: configparser.ConfigParser) -> Path:
    write_path = Path(cfg.get("device", "write_path")).expanduser()
    return Path(cfg.get("storage", "database", fallback=str(write_path / "weather.db"))).expanduser()


def station_settings(cfg: configparser.ConfigParser) -> dict:
    """[station] latitude/longitude/elevation_ft, for the page's sun and moon.
    Empty (and the sun/moon card hidden) unless latitude and longitude are set."""
    out = {}
    for key in ("latitude", "longitude", "elevation_ft"):
        try:
            out[key] = cfg.getfloat("station", key)
        except (configparser.Error, ValueError):
            pass
    return out if "latitude" in out and "longitude" in out else {}


def page_title(cfg: configparser.ConfigParser) -> str:
    """[station] name: the pages' title and heading."""
    return cfg.get("station", "name", fallback="").strip() or DEFAULT_TITLE


def button_list(cfg: configparser.ConfigParser) -> list[dict]:
    """[buttons] button1..buttonN = Label | URL, in number order, for the lower pane.
    A third part, Target=<window name>, opens the page in that browser window or
    tab instead of the pane (Target=_blank: a new tab every time). A blank button,
    or one missing its label or URL, is left out."""
    if not cfg.has_section("buttons"):
        return []
    numbered = []
    for key, value in cfg.items("buttons"):
        if not (key.startswith("button") and key[6:].isdigit()):
            continue
        label, url, *rest = [part.strip() for part in value.split("|")] + [""]
        button = {"label": label, "url": url}
        for part in rest:
            name, eq, target = part.partition("=")
            if eq and name.strip().lower() == "target" and target.strip():
                button["target"] = target.strip()
        if label and url:
            numbered.append((int(key[6:]), button))
    return [b for _, b in sorted(numbered, key=lambda n: n[0])]


# The row names each kind of dashboard card knows (weather.html's OUTDOOR_ROWS and
# SENSOR_ROWS), so a misspelt one in config.ini is reported rather than ignored.
CARD_ROWS = {
    "outdoor_rows": ("feels_like", "humidity", "dew_point", "wind", "gust", "pressure",
                     "rain_today", "rain_rate", "uv", "light", "lightning", "last_strike"),
    "sensor_rows": ("humidity", "dew_point"),
}


def card_rows(cfg: configparser.ConfigParser, section: str) -> list[str]:
    """[outdoor_rows] / [sensor_rows]: number = row name, the order of the rows on
    that kind of card. Rows not listed follow in their usual order."""
    if not cfg.has_section(section):
        return []
    numbered = []
    for key, value in cfg.items(section):
        name = value.strip().lower()
        if not key.isdigit():
            log.warning("[%s] %s: the key should be a number", section, key)
        elif name not in CARD_ROWS[section]:
            log.warning("[%s] %s = %s: no such row; the rows are %s",
                        section, key, value, ", ".join(CARD_ROWS[section]))
        else:
            numbered.append((int(key), name))
    return [n for _, n in sorted(numbered)]


#
# Serves the dashboard and its data, read-only, and nothing else: no directory
# listing, no other files. The pages come from beside this script (so a repo
# update shows at once); the data comes from write_path.
#
# [web] pages names a folder of your own: a page, stylesheet, script or image
# put there is served by its name, and one named like a shipped page (theme.css,
# weather.html, charts.html, units.js) replaces it. An update never touches that folder.

DEFAULT_TITLE = "CC0AC Weather"

# /config.cgi edits config.ini in the browser: one pane, Save and Revert. It is
# answered only for addresses on your own network (and this machine); a request
# through the router's port forward comes from an outside address and gets a 404.
# There is no password yet, so anyone on your network can use it. Save checks the
# file reads as an ini file, keeps the old one as config.ini.previous, writes the
# new one and restarts the service so it takes effect.

CONFIG_MAX_BYTES = 1_000_000


def check_config(text: str) -> str | None:
    """Why this text can't be config.ini, or None if it reads cleanly."""
    cfg = configparser.ConfigParser()
    try:
        cfg.read_string(text, source="config.ini")
        for section in cfg.sections():
            cfg.items(section)          # a broken %(name)s shows up only when read
    except configparser.Error as e:
        return f"Not saved: {e}"
    return None


def save_config(config_path: Path, text: str) -> None:
    """Keep the old file as config.ini.previous, then replace it in one step,
    with the same permissions (it holds passwords)."""
    mode = config_path.stat().st_mode & 0o777 if config_path.exists() else 0o600
    if config_path.exists():
        shutil.copy2(config_path, config_path.with_name(config_path.name + ".previous"))
    tmp = config_path.with_name(config_path.name + ".saving")
    tmp.write_text(text)
    tmp.chmod(mode)
    tmp.replace(config_path)


def restart_soon(delay: float = 1.0) -> None:
    """Restart the whole program once the browser has its answer. Under the boot
    loader or systemd it exits and they start it again, which also ends a running
    rtl_433; run by hand, it starts itself over in place."""
    def restart() -> None:
        time.sleep(delay)
        for handler in logging.getLogger().handlers:
            handler.flush()
        if os.environ.get("CC0AC_BOOTLOADER_ROOT") or os.environ.get("INVOCATION_ID"):
            os._exit(0)
        os.execv(sys.executable, [sys.executable, str(Path(sys.argv[0]).resolve()), *sys.argv[1:]])
    threading.Thread(target=restart, daemon=True).start()


def station_version() -> str:
    """This copy's version, from the VERSION file beside it: shown on the dashboard and to MCP."""
    try:
        return (ROOT / "VERSION").read_text().strip()
    except OSError:
        return "unknown"
