"""
Send each reading to a web address of your own ([webhook]), as JSON.

For Node-RED, n8n, Home Assistant's webhook trigger, a script of your own: every
reading from every sensor is POSTed as it is recorded, or each sensor's latest
once every `every_minutes`. The body:

    {"sensor_id": "00001234", "name": "Yard", "type": "Atlas", "outdoor": true,
     "source": "acurite-access", "time": "2026-10-24T18:00:00Z",
     "fields": {"temp_f": 61.2, "humidity_pct": 44, ...},
     "units": {"temp_f": "°F", "humidity_pct": "%", ...},
     "held_back": {"wind_mph": {"value": 200, "why": "..."}},
     "software": "CC0AC-Weather-1.3.12"}

The fields are in the stored units, whatever [units] the pages show; held_back
is what sanity.py didn't believe (it is not in fields). `header` is sent with
each one, for a token: `header = Authorization: Bearer abc123`.
"""

from __future__ import annotations

import configparser
import json
import logging
import threading
import time

import sensor_config
from iolog import report
from outputs._station import SOFTWARE, needs, post
from sources import FIELDS, OUTDOOR_TYPES, Reading

log = logging.getLogger("acurite")

NAME = "webhook"
# The unit after the comma in each field's description: "temperature, °F" → "°F".
UNITS = {k: d.rpartition(", ")[2] for k, d in FIELDS.items() if ", " in d}

_last: dict[str, float] = {}   # sensor → when its last reading was sent (monotonic)
_lock = threading.Lock()


def enabled(cfg: configparser.ConfigParser) -> bool:
    return needs(cfg, "webhook", "url")


def headers(cfg: configparser.ConfigParser) -> dict:
    out = {"Content-Type": "application/json"}
    name, colon, value = cfg.get("webhook", "header", fallback="").partition(":")
    if colon and name.strip():
        out[name.strip()] = value.strip()
    return out


def body(cfg: configparser.ConfigParser, reading: Reading) -> dict:
    name = sensor_config.title(cfg, reading.sensor_id)
    return {"sensor_id": reading.sensor_id, "name": name or reading.name or "", "type": reading.type,
            "outdoor": reading.type in OUTDOOR_TYPES, "source": reading.source,
            "time": reading.sensor_utc or reading.received_utc,
            "fields": reading.fields, "units": {k: UNITS[k] for k in reading.fields if k in UNITS},
            "held_back": reading.suspect, "software": SOFTWARE}


def due(cfg: configparser.ConfigParser, sensor_id: str) -> bool:
    every = 60 * cfg.getfloat("webhook", "every_minutes", fallback=0)
    now = time.monotonic()
    with _lock:
        last = _last.get(sensor_id.lower())
        if last is not None and now - last < every:
            return False
        _last[sensor_id.lower()] = now
        return True


def send(cfg: configparser.ConfigParser, reading: Reading) -> None:
    if not due(cfg, reading.sensor_id):
        return
    status, answer = post(cfg.get("webhook", "url").strip(),
                          json.dumps(body(cfg, reading)).encode(), headers(cfg))
    ok = 200 <= status < 300
    if not ok:
        log.warning("Webhook failed (%s): %s", status, answer[:200])
    report(NAME, ok, f"{status} {answer[:200]}")
