"""
Write each reading to InfluxDB ([influxdb]), for Grafana and the like.

InfluxDB 2 and 3 (and InfluxDB Cloud): `url`, `org`, `bucket` and a `token`
that may write to the bucket. InfluxDB 1.x: `url`, `database`, and `username`
and `password` if it asks for them. Each reading is one line of InfluxDB's line
protocol in the measurement `weather` (`measurement` to change it):

    weather,sensor_id=00001234,name=Yard,type=Atlas,source=acurite-access temp_f=61.2,humidity_pct=44.0 1792864800

Tagged by sensor, so a Grafana panel picks a sensor by its name. The fields are
in the stored units (°F, mph, inHg, in); every number is written as a float, so
a field never changes type (InfluxDB refuses that); battery is a string. Fields
sanity.py held back are left out.
"""

from __future__ import annotations

import configparser
import logging
import urllib.parse

from cc0ac import sensor_config
from cc0ac.iolog import report
from cc0ac.outputs._station import _utc, needs, post
from cc0ac.sources import Reading

log = logging.getLogger("acurite")

NAME = "influxdb"


def enabled(cfg: configparser.ConfigParser) -> bool:
    if not needs(cfg, "influxdb", "url"):
        return False
    if not any(cfg.get("influxdb", k, fallback="").strip() for k in ("bucket", "database")):
        log.warning("[influxdb] is enabled but neither bucket (InfluxDB 2, 3) nor database (1.x) is set")
        return False
    return True


def _key(text: str) -> str:
    """A measurement, tag key or tag value as line protocol writes it."""
    return text.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def line(cfg: configparser.ConfigParser, reading: Reading) -> str | None:
    """The reading as one line of line protocol; None when it has no fields."""
    name = sensor_config.title(cfg, reading.sensor_id)
    tags = {"sensor_id": reading.sensor_id, "name": name or reading.name or "",
            "type": reading.type or "", "source": reading.source}
    fields = []
    for k, v in sorted(reading.fields.items()):
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, (int, float)):
            fields.append(f"{_key(k)}={float(v)!r}")
        else:
            fields.append(f'{_key(k)}="' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"')
    if not fields:
        return None
    measurement = cfg.get("influxdb", "measurement", fallback="weather").strip() or "weather"
    head = ",".join([_key(measurement).replace("\\=", "=")] +
                    [f"{k}={_key(v)}" for k, v in tags.items() if v])
    when = int(_utc(reading.sensor_utc or reading.received_utc).timestamp())
    return f"{head} {','.join(fields)} {when}"


def target(cfg: configparser.ConfigParser) -> tuple[str, dict]:
    """The write URL and headers: InfluxDB 2/3's API when a bucket is set, else 1.x's."""
    get = lambda k: cfg.get("influxdb", k, fallback="").strip()   # noqa: E731
    base = get("url").rstrip("/")
    if get("bucket"):
        query = {"org": get("org"), "bucket": get("bucket"), "precision": "s"}
        headers = {"Authorization": f"Token {get('token')}"} if get("token") else {}
        return f"{base}/api/v2/write?{urllib.parse.urlencode(query)}", headers
    query = {"db": get("database"), "precision": "s"}
    if get("username"):
        query.update(u=get("username"), p=get("password"))
    return f"{base}/write?{urllib.parse.urlencode(query)}", {}


def send(cfg: configparser.ConfigParser, reading: Reading) -> None:
    text = line(cfg, reading)
    if text is None:
        return
    url, headers = target(cfg)
    status, answer = post(url, text.encode(), {"Content-Type": "text/plain; charset=utf-8", **headers})
    ok = 200 <= status < 300
    if not ok:
        log.warning("InfluxDB write failed (%s): %s", status, answer[:200])
    report(NAME, ok, f"{status} {answer[:200]}")
