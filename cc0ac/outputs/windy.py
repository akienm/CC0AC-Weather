"""
Upload to Windy ([windy]): stations.windy.com, its station API (v2, 2026).

Make a station at https://stations.windy.com/stations and copy its ID and
password. Windy shows observations in 5-minute steps, so every 5 minutes is
enough. Checked 2026-10-21: the endpoint answers a wrong password with
"Provided password is invalid" (400).
"""

from __future__ import annotations

import configparser
import logging

from cc0ac.outputs._station import Station, get, needs, wu_format
from cc0ac.iolog import report
from cc0ac.sources import Reading

log = logging.getLogger("acurite")

NAME = "windy"
URL = "https://stations.windy.com/api/v2/observation/update"
station = Station("windy", every_minutes=5)


def enabled(cfg: configparser.ConfigParser) -> bool:
    return needs(cfg, "windy", "station_id", "password")


def params(cfg: configparser.ConfigParser, fields: dict, when) -> dict:
    return {"ID": cfg.get("windy", "station_id").strip(),
            "PASSWORD": cfg.get("windy", "password").strip(), **wu_format(fields, when)}


def send(cfg: configparser.ConfigParser, reading: Reading) -> None:
    due = station.take(cfg, reading)
    if due is None:
        return
    status, body = get(URL, params(cfg, *due))
    if status == 200:
        log.info("Windy upload OK")
    else:
        log.warning("Windy upload failed (%s): %s", status, body[:200])
    report(NAME, status == 200, f"{status} {body[:200]}")
