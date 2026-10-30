"""
What the weather-network outputs share: which sensor is the station, its latest
values, and how often to upload.

A network wants one station, every few minutes, not every reading. Readings
from the station's sensor are merged as they come (a hub and a radio can both
hear it), and an upload is due once `every_minutes` have passed since the last.

The station is `sensor = <ID>` in the output's section, or else the first
outdoor sensor heard. Internet weather (the online source) is never a station:
it is someone else's measurement, or a model's.
"""

from __future__ import annotations

import configparser
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from cc0ac.sources import OUTDOOR_TYPES, Reading

log = logging.getLogger("acurite")

VERSION = (Path(__file__).resolve().parent.parent.parent / "VERSION").read_text().split("/")[0].strip()
SOFTWARE = f"CC0AC-Weather-{VERSION}"


class Station:
    def __init__(self, section: str, every_minutes: int):
        self.section, self.every_minutes = section, every_minutes
        self.sensor_id: str | None = None
        self.fields: dict = {}
        self.when: datetime | None = None   # the latest reading's time, UTC
        self.last_upload: float | None = None
        self.lock = threading.Lock()

    def take(self, cfg: configparser.ConfigParser, reading: Reading) -> tuple[dict, datetime] | None:
        """Add a reading; the station's fields and their time if an upload is due now."""
        if reading.source == "online" or reading.type not in OUTDOOR_TYPES:
            return None
        with self.lock:
            wanted = cfg.get(self.section, "sensor", fallback="").strip().lower()
            if self.sensor_id is None:
                if wanted and reading.sensor_id.lower() != wanted:
                    return None
                self.sensor_id = reading.sensor_id
            if reading.sensor_id != self.sensor_id:
                return None
            self.fields.update({k: v for k, v in reading.fields.items() if v is not None})
            self.when = _utc(reading.sensor_utc or reading.received_utc)
            every = 60 * cfg.getint(self.section, "every_minutes", fallback=self.every_minutes)
            now = time.monotonic()
            if self.last_upload is not None and now - self.last_upload < every:
                return None
            self.last_upload = now
            return dict(self.fields), self.when


def _utc(text: str) -> datetime:
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def number(fields: dict, name: str) -> float | None:
    v = fields.get(name)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def wu_format(fields: dict, when: datetime) -> dict:
    """The fields in Weather Underground's upload format, which Windy and
    PWSweather take too: their names, US units, and dateutc."""
    from cc0ac.outputs.weather_underground import WU_FIELDS
    out = {"dateutc": when.strftime("%Y-%m-%d %H:%M:%S"), "softwaretype": SOFTWARE}
    for field, (name, places) in WU_FIELDS.items():
        v = number(fields, field)
        if v is not None:
            out[name] = str(round(v, places) if places else int(round(v)))
    return out


def needs(cfg: configparser.ConfigParser, section: str, *keys: str) -> bool:
    """Is the output switched on, with every key it needs set? Logs what's missing."""
    if not cfg.getboolean(section, "enabled", fallback=False):
        return False
    missing = [k for k in keys if not cfg.get(section, k, fallback="").strip()]
    if missing:
        log.warning("[%s] is enabled but %s not set in config", section, " / ".join(missing))
    return not missing


def get(url: str, params: dict) -> tuple[int, str]:
    """GET url?params; the status and body, errors included (they say why)."""
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": SOFTWARE})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode(errors="replace").strip()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace").strip()


def post(url: str, body: bytes, headers: dict) -> tuple[int, str]:
    """POST body to url; the status and body. A server that can't be reached is
    status 0 with the reason, so it is reported like any other failure."""
    req = urllib.request.Request(url, data=body, method="POST", headers={"User-Agent": SOFTWARE, **headers})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode(errors="replace").strip()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace").strip()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 0, str(getattr(exc, "reason", exc))
