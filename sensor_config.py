"""
sensor_config.py — each sensor's own section in config.ini, named by its ID.

    [00001111]
    title = Living Room
    icon = 🛋️
    link = indoor
    calibration = temp_f -1.5

Every section that isn't one of the program's own settings ([web], [windy], …)
is a sensor, and the cards show in the order the sections come in the file. A
sensor first heard from the hub is added at the end as a bare section, its ID
for a title, until someone fills it in.

The older form, names in [sensors], order in [sensor_order] and corrections in
[calibration], still reads; where a sensor is in both, its own section has the
last word, and the sensors with sections come first.

An icon is an emoji, or one of LIVE_ICONS: one that changes by itself, worked
out on the station from [station] latitude and longitude, no outside service.
"""

from __future__ import annotations

import configparser
import logging

log = logging.getLogger("acurite")

# The program's own sections: every other section in config.ini is a sensor.
SETTINGS = frozenset({
    "device", "sensors", "sensor_order", "outdoor_rows", "sensor_rows", "units", "calibration", "sanity",
    "hub", "storage", "web", "duckdns", "updates", "backup", "buttons", "station", "weather_underground",
    "mqtt", "webhook", "influxdb", "windy", "pwsweather", "weathercloud", "cwop", "forecast", "climate",
    "online", "alerts", "console", "capture", "sun_moon"})

LIVE_ICONS = {
    "sky": "the sun while it's up, else the moon in its phase",
    "moon": "the moon in its phase",
}

# link = <one of these>: the chart a card opens. Unset, the outdoor cards open
# "outdoor" and the rest "indoor"; "none" opens nothing.
LINKS = ("outdoor", "wind", "pressure", "rain", "light", "indoor", "humidity", "none")

# Sources whose sensors are the house's own (rtl_433 hears the neighbours' too).
OWN_SOURCES = ("acurite-access", "console")


def is_sensor(section: str) -> bool:
    return section.strip().lower() not in SETTINGS


def table(cfg: configparser.ConfigParser) -> dict[str, dict]:
    """{sensor ID: {"title", "icon", "link", "calibration"}}, in display order;
    a value not set is "" (a title not set is the ID)."""
    own = {}
    for section in cfg.sections():
        if not is_sensor(section):
            continue
        sid = section.strip().lower()
        entry = {k: cfg.get(section, k, raw=True, fallback="").strip() for k in ("title", "icon", "link", "calibration")}
        if entry["link"] and entry["link"].lower() not in LINKS:
            log.warning("[%s] link = %s isn't one of %s; left out", section, entry["link"], ", ".join(LINKS))
            entry["link"] = ""
        entry["link"] = entry["link"].lower()
        entry["title"] = entry["title"] or sid
        own[sid] = entry
    older = {}
    for sid, name in _older_names(cfg).items():
        if sid not in own:
            older[sid] = {"title": name, "icon": "", "link": "", "calibration": ""}
    return {**own, **older}


def _older_names(cfg: configparser.ConfigParser) -> dict[str, str]:
    """[sensors] in [sensor_order]'s order, then the rest by name."""
    if not cfg.has_section("sensors"):
        return {}
    names = {k.strip().lower(): v.strip() for k, v in cfg.items("sensors")}
    ordered = []
    if cfg.has_section("sensor_order"):
        slots = sorted(cfg.items("sensor_order"),
                       key=lambda kv: (0, int(kv[0]), "") if kv[0].isdigit() else (1, 0, kv[0]))
        for _, sid in slots:
            sid = sid.strip().lower()
            if sid not in names:
                log.warning("[sensor_order] lists %s, which isn't in [sensors]; left out", sid)
            elif sid not in ordered:
                ordered.append(sid)
    rest = sorted((s for s in names if s not in ordered), key=lambda s: names[s].lower())
    return {s: names[s] for s in ordered + rest}


def titles(cfg: configparser.ConfigParser) -> dict[str, str]:
    """{sensor ID: title}, in display order."""
    return {sid: e["title"] for sid, e in table(cfg).items()}


def title(cfg: configparser.ConfigParser, sensor_id: str) -> str:
    """A sensor's title, or "" when the config doesn't know it."""
    e = table(cfg).get(sensor_id.strip().lower())
    return e["title"] if e else ""


def cards(cfg: configparser.ConfigParser) -> dict[str, dict]:
    """What the dashboard needs to dress each card: {ID: {"icon", "link"}}, only what is set."""
    out = {}
    for sid, e in table(cfg).items():
        dress = {k: e[k] for k in ("icon", "link") if e[k]}
        if dress:
            out[sid] = dress
    return out


def sun_moon_icon(cfg: configparser.ConfigParser) -> str:
    return cfg.get("sun_moon", "icon", fallback="").strip() or "moon"


def calibration(cfg: configparser.ConfigParser) -> dict[str, str]:
    """{sensor ID: its calibration text}, from [calibration] and then each sensor's own section."""
    out = {}
    if cfg.has_section("calibration"):
        out.update({k.strip().lower(): v for k, v in cfg.items("calibration") if v.strip()})
    out.update({sid: e["calibration"] for sid, e in table(cfg).items() if e["calibration"]})
    return out


def new_section(sensor_id: str, kind: str, day: str) -> str:
    """The bare section a sensor gets when it is first heard."""
    return (f"\n[{sensor_id}]\n; A {kind} sensor, first heard {day}. Give it a title (and an icon, an emoji)\n"
            f"; and it shows by that name; move this section to move its card.\n; title =\n; icon =\n")
