"""
Upload to PWSweather ([pwsweather]): pwsweather.com, run by Vaisala Xweather.

Add a station at https://www.pwsweather.com and copy its station ID and its
API key (from the station's page; not your login password). Checked
2026-10-21: the endpoint answers a wrong key with AUTH_FAILED (401), and
requires dateutc.
"""

from __future__ import annotations

import configparser
import json
import logging

from cc0ac.outputs._station import Station, get, needs, wu_format
from cc0ac.iolog import report
from cc0ac.sources import Reading

log = logging.getLogger("acurite")

NAME = "pwsweather"
URL = "https://pwsupdate.pwsweather.com/api/v1/submitwx"
station = Station("pwsweather", every_minutes=5)


def enabled(cfg: configparser.ConfigParser) -> bool:
    return needs(cfg, "pwsweather", "station_id", "api_key")


def params(cfg: configparser.ConfigParser, fields: dict, when) -> dict:
    return {"ID": cfg.get("pwsweather", "station_id").strip(),
            "PASSWORD": cfg.get("pwsweather", "api_key").strip(),
            **wu_format(fields, when), "action": "updateraw"}


def send(cfg: configparser.ConfigParser, reading: Reading) -> None:
    due = station.take(cfg, reading)
    if due is None:
        return
    status, body = get(URL, params(cfg, *due))
    try:
        ok = status == 200 and json.loads(body).get("success") is not False
    except ValueError:
        ok = status == 200
    if ok:
        log.info("PWSweather upload OK")
    else:
        log.warning("PWSweather upload failed (%s): %s", status, body[:200])
    report(NAME, ok, f"{status} {body[:200]}")
