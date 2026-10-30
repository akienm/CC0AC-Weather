"""
The first-run setup page (/setup): finds the hub, reads its page, and writes the
answers into config.ini.

With the hub plugged into the Pi (README, method B), the Pi hands the hub its
address, so the hub is whatever dnsmasq last leased. The hub's own page
(http://<hub>/) shows its ID, its Weather Underground station, its elevation and
every sensor it hears; the setup page fills itself in from that, and asks only
what the hub can't know: what to call each sensor, and where the station is.

The answers are written into config.ini as it stands, comments and all: a key
already there is replaced in place, a missing one is added to its section, and
[sensors] and [sensor_order] are rewritten whole.

The hub's Weather Underground password is on its page too. It is never read.
"""

from __future__ import annotations

import configparser
import json
import re
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cc0ac import sensor_config

LEASES = Path("/var/lib/misc/dnsmasq.leases")
HUB_FIELDS = {"Mac": "hub_id", "Ver": "firmware", "Ser": "server", "ID": "wu_station",
              "Dev": "wu_sensor", "Ele": "elevation_ft"}       # never "PS": the WU password
_HUB_VALUE = re.compile(r"\$\('txt(Mac|Ver|Ser|ID|Dev|Ele)'\)\.value\s*=\s*'([^'<>]*)'")
_HUB_SENSOR = re.compile(r"<tr><td>\s*(\d+)\s*</td><td>\s*([^<]*?)\s*</td><td>\s*([0-9A-Fa-f]+)\s*</td>"
                         r"<td>\s*([^<]*?)\s*</td><td>\s*(\d*)\s*</td><td>\s*([^<]*?)\s*</td></tr>")
_HUB_PRESSURE = re.compile(r"PRESSURE:\s*([\d.]+)")


def hub_addresses(leases: Path = LEASES) -> list[dict]:
    """Everything dnsmasq has leased on the hub's cable, newest lease first."""
    try:
        lines = leases.read_text().splitlines()
    except OSError:
        return []
    found = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 4:
            found.append({"expires": int(parts[0]) if parts[0].isdigit() else 0,
                          "mac": parts[1], "ip": parts[2], "name": "" if parts[3] == "*" else parts[3]})
    return sorted(found, key=lambda f: -f["expires"])


def parse_hub_page(html: str) -> dict:
    """The hub's settings and sensors from its local page."""
    hub = {HUB_FIELDS[k]: v for k, v in _HUB_VALUE.findall(html)}
    hub["sensors"] = [{"slot": int(n), "type": kind, "id": sid, "last_utc": last,
                       "signal": int(sig) if sig else None, "battery": batt}
                      for n, kind, sid, last, sig, batt in _HUB_SENSOR.findall(html)]
    m = _HUB_PRESSURE.search(html)
    if m:
        hub["pressure_inhg"] = float(m.group(1))
    return hub


def find_hub(leases: Path = LEASES, timeout: float = 5.0) -> dict:
    """The hub on the cable, read from its own page: {"found": True, "ip", ...}
    or {"found": False, "why": ...} saying what to check."""
    addresses = hub_addresses(leases)
    if not addresses:
        return {"found": False, "why": "Nothing on the cable port has asked the Pi for an address yet. "
                                       "Check the hub's cable is in the Pi's network port and the hub has power; "
                                       "it can take a minute after plugging in."}
    tried = []
    for lease in addresses:
        try:
            with urllib.request.urlopen(f"http://{lease['ip']}/", timeout=timeout) as r:
                html = r.read(200_000).decode("utf-8", "replace")
        except OSError as e:
            tried.append(f"{lease['ip']}: {e}")
            continue
        hub = parse_hub_page(html)
        if hub.get("hub_id") or hub["sensors"]:
            return {"found": True, "ip": lease["ip"], "lease_name": lease["name"], **hub}
        tried.append(f"{lease['ip']}: answered, but not with an AcuRite Access page")
    return {"found": False, "why": "Something is on the cable port but its page didn't open: " + "; ".join(tried)}


# ── Editing config.ini without losing its comments ────────────────────────────

_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def _key_of(line: str) -> str | None:
    s = line.strip()
    if not s or s[0] in ";#[" or "=" not in s:
        return None
    return s.split("=", 1)[0].strip().lower()


def _section_spans(lines: list[str]) -> dict[str, tuple[int, int]]:
    """section name -> (header line index, index after its last line)."""
    spans, name, start = {}, None, 0
    for i, line in enumerate(lines):
        m = _SECTION.match(line)
        if m:
            if name is not None:
                spans[name] = (start, i)
            name, start = m.group(1).strip(), i
    if name is not None:
        spans[name] = (start, len(lines))
    return spans


def _insert_at(lines: list[str], start: int, end: int) -> int:
    """Where a new key goes in a section: after its last non-blank line."""
    at = end
    while at - 1 > start and not lines[at - 1].strip():
        at -= 1
    return at


def set_values(text: str, section: str, values: Mapping[str, str | None]) -> str:
    """Set keys in one section: replaced where they stand, else added at the end
    of the section (the section is added if missing). A value of None removes the key."""
    lines = text.splitlines()
    spans = _section_spans(lines)
    if section not in spans:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{section}]")
        spans[section] = (len(lines) - 1, len(lines))
    start, end = spans[section]
    todo = dict(values)
    out = lines[:start + 1]
    for line in lines[start + 1:end]:
        key = _key_of(line)
        if key is not None and key in {k.lower() for k in todo}:
            name = next(k for k in todo if k.lower() == key)
            value = todo.pop(name)
            if value is not None:
                out.append(f"{name} = {value}")
            continue
        out.append(line)
    at = _insert_at(out, start, len(out))
    out[at:at] = [f"{k} = {v}" for k, v in todo.items() if v is not None]
    out += lines[end:]
    return "\n".join(out) + "\n"


def replace_entries(text: str, section: str, values: dict[str, str]) -> str:
    """Make a section's keys exactly these (comments kept), e.g. [sensors]."""
    cfg = configparser.ConfigParser()
    cfg.optionxform = str  # type: ignore[assignment,method-assign]   # keep the keys' case
    try:
        cfg.read_string(text)
        old = list(cfg[section]) if cfg.has_section(section) else []
    except configparser.Error:
        old = []
    return set_values(text, section, {**{k: None for k in old}, **values})


_GROUP = re.compile(r"^\s*;\s*──")     # "; ── Weather networks ──": a heading over the sections below it


def missing_sections(text: str, example: str) -> tuple[str, list[str]]:
    """The example's sections this config doesn't have, as the example writes them
    (comments and all, so a new feature shows up at its default and explained), and
    their names. A heading over a group goes with the first of them that's missing. A section already here is left exactly as it is."""
    try:
        cfg = configparser.ConfigParser(interpolation=None)
        cfg.read_string(text)
        have = {s.lower() for s in cfg.sections()}
    except configparser.Error:
        return "", []       # a file it can't read is the user's to fix, not to add to
    lines = example.splitlines()
    starts = [i for i, line in enumerate(lines) if _SECTION.match(line)] + [len(lines)]
    blocks: list[str] = []
    names: list[str] = []
    lead: list[str] = []
    for start, end in zip(starts, starts[1:]):
        block = lines[start:end]
        heading = next((i for i, line in enumerate(block) if _GROUP.match(line)), len(block))
        block, below = block[:heading], block[heading:]
        named = _SECTION.match(lines[start])
        assert named   # starts are the lines it matched
        name = named.group(1).strip()
        if name.lower() not in have and not sensor_config.is_sensor(name):   # a sensor is the house's own
            while block and not block[-1].strip():
                block.pop()
            blocks.append("\n".join(lead + block))
            names.append(name)
            lead = []
        lead = below or lead
    return "\n\n".join(blocks) + "\n" if blocks else "", names


def _own_lines(lines: list[str], start: int, end: int) -> int:
    """Where a section's own lines stop: before a heading over the sections below it."""
    return next((i for i in range(start + 1, end) if _GROUP.match(lines[i])), end)


def order_sections(text: str, names: list[str]) -> str:
    """These sections in this order, where the first of them stands; everything else as it was."""
    lines = text.splitlines()
    spans = _section_spans(lines)
    present = [n for n in names if n in spans]
    if len(present) < 2:
        return text
    cut = {n: (spans[n][0], _own_lines(lines, *spans[n])) for n in present}
    blocks = []
    for n in present:
        block = lines[slice(*cut[n])]
        while block and not block[-1].strip():
            block.pop()
        blocks += block + [""]
    first = min(a for a, _ in cut.values())
    out, i = [], 0
    while i < len(lines):
        if i == first:
            out += blocks
        span = next((b for a, b in cut.values() if a == i), None)
        if span is not None:
            i = span
            continue
        out.append(lines[i])
        i += 1
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n"


def write_sensors(text: str, names: dict[str, str]) -> str:
    """Each sensor its own section with its title, in this order; a sensor's section
    not among them removed; the older [sensors] and [sensor_order] emptied (their
    comments kept)."""
    lines = text.splitlines()
    spans = _section_spans(lines)
    gone = [n for n in spans if sensor_config.is_sensor(n) and n.strip().lower() not in names]
    for n in sorted(gone, key=lambda n: -spans[n][0]):
        start, end = spans[n]
        del lines[start:_own_lines(lines, start, end)]
    text = "\n".join(lines) + "\n"
    spelled = {n.strip().lower(): n for n in _section_spans(text.splitlines())}
    for sid, name in names.items():
        text = set_values(text, spelled.get(sid, sid), {"title": name})
    text = order_sections(text, [spelled.get(sid, sid) for sid in names])
    spans = _section_spans(text.splitlines())
    for older in ("sensors", "sensor_order"):
        if older in spans:
            text = replace_entries(text, older, {})
    return text


def apply_setup(text: str, answers: dict) -> str:
    """config.ini with the setup page's answers written in.

    answers: {"sensors": [{"id", "name"}, ...] in display order,
              "station": {"name", "latitude", "longitude", "elevation_ft", "timezone"},
              "forecast": bool, "online": bool, "alerts": {"ntfy_topic", "email_to", ...},
              "updates": {"automatic": bool, "time": "HH:MM"},
              "backup": {"path", "user", "password", "keep"}}"""
    sensors = [s for s in answers.get("sensors", []) if str(s.get("id", "")).strip()]
    for s in sensors:
        if not re.fullmatch(r"[0-9A-Za-z-]{1,24}", str(s["id"]).strip()) or not sensor_config.is_sensor(str(s["id"])):
            raise ValueError(f"not a sensor ID: {s['id']!r}")
    names = {str(s["id"]).strip().lower(): _one_line(s.get("name")) or f"Sensor {s['id']}" for s in sensors}
    text = write_sensors(text, names)
    station = answers.get("station") or {}
    values = {"name": _one_line(station.get("name")) or None}
    for key, lo, hi in (("latitude", -90, 90), ("longitude", -180, 180), ("elevation_ft", -1500, 30000)):
        v = station.get(key)
        if v in (None, ""):
            values[key] = None
            continue
        f = float(v)
        if not lo <= f <= hi:
            raise ValueError(f"{key} {v} is out of range")
        values[key] = f"{f:g}"
    if "timezone" in station:
        tz = str(station.get("timezone") or "").strip()
        if tz and not re.fullmatch(r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*", tz):
            raise ValueError(f"not a time zone: {tz!r}")
        values["timezone"] = tz or None
    text = set_values(text, "station", values)
    if "forecast" in answers:
        text = set_values(text, "forecast", {"enabled": "true" if answers["forecast"] else "false"})
    if "online" in answers:
        text = set_values(text, "online", {"enabled": "true" if answers["online"] else "false"})
    updates = answers.get("updates")
    if updates is not None:
        when = str(updates.get("time") or "06:00").strip()
        m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", when)
        if not m:
            raise ValueError(f"not a time of day: {when!r}")
        text = set_values(text, "updates", {"automatic": "yes" if updates.get("automatic", True) else "no",
                                            "time": f"{int(m[1]):02d}:{m[2]}"})
    backup = answers.get("backup")
    if backup is not None:
        keep = {k: _one_line(backup.get(k)) for k in ("path", "user", "password", "keep") if k in backup}
        if not keep.get("password"):   # the page never shows it; blank means keep it
            keep.pop("password", None)
        if keep.get("keep", "") != "" and not re.fullmatch(r"\d{1,3}", keep["keep"]):
            raise ValueError(f"not a number of backups: {keep['keep']!r}")
        text = set_values(text, "backup", {k: v or "" for k, v in keep.items()})
    alerts = answers.get("alerts")
    if alerts is not None:
        kept: dict[str, str | None] = {k: _one_line(alerts.get(k)) or None for k in ALERT_KEYS if k in alerts}
        if not kept.get("smtp_password"):   # the page never shows it; blank means keep it
            kept.pop("smtp_password", None)
        text = set_values(text, "alerts", kept)
        cfg = configparser.ConfigParser()
        cfg.read_string(text)
        on = any(cfg.get("alerts", k, fallback="").strip() for k in ("ntfy_topic", "email_to", "healthcheck_url"))
        keep = {"enabled": "true" if on else "false"}
        text = set_values(text, "alerts", keep)
    return text


ALERT_KEYS = ("quiet_minutes", "ntfy_topic", "ntfy_server", "email_to", "email_from",
              "smtp_host", "smtp_port", "smtp_user", "smtp_password", "healthcheck_url")


def _one_line(v: Any) -> str:
    return re.sub(r"[\r\n]+", " ", str(v or "")).strip()


def needs_setup(cfg: configparser.ConfigParser) -> bool:
    """No sensor named yet, and no internet weather: the first visit from home goes to /setup."""
    if cfg.getboolean("online", "enabled", fallback=False):
        return False
    return not sensor_config.titles(cfg)


def setup_state(cfg: configparser.ConfigParser, leases: Path = LEASES) -> dict:
    """What the setup page shows: the hub as found, and the settings now."""
    current = {
        "sensors": sensor_config.titles(cfg),
        "sensor_order": {str(n): sid for n, sid in enumerate(sensor_config.titles(cfg), 1)},
        "station": {k: cfg.get("station", k, fallback="") for k in ("name", "latitude", "longitude", "elevation_ft", "timezone")},
        "forecast": cfg.getboolean("forecast", "enabled", fallback=False),
        "online": cfg.getboolean("online", "enabled", fallback=False),
        "updates": {"automatic": cfg.getboolean("updates", "automatic", fallback=True),
                    "time": cfg.get("updates", "time", fallback="06:00")},
        "alerts": {k: ("" if k == "smtp_password" else cfg.get("alerts", k, fallback="")) for k in ALERT_KEYS},
        "smtp_password_set": bool(cfg.get("alerts", "smtp_password", fallback="").strip()),
        "backup": {"path": cfg.get("backup", "path", fallback=""), "user": cfg.get("backup", "user", fallback=""),
                   "keep": cfg.get("backup", "keep", fallback="8"),
                   "password_set": bool(cfg.get("backup", "password", raw=True, fallback="").strip())},
    }
    hub_on = cfg.has_section("hub") and cfg.getboolean("hub", "enabled", fallback=True)
    hub = find_hub(leases) if hub_on else {"found": False, "off": True, "sensors": [],
                                           "why": "This computer doesn't take the hub's cable."}
    return {"hub": hub, "current": current}


if __name__ == "__main__":       # python3 wizard.py: what the setup page would find
    print(json.dumps(find_hub(), indent=1))
