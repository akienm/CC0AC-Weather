"""
Send to CWOP ([cwop]), the Citizen Weather Observer Program, which feeds the US
National Weather Service and NOAA's MADIS.

Sign up at https://weather.gladstonefamily.net/cwop_signup.html for a station
ID (like FW1234), or use your ham callsign with its APRS passcode. Needs
[station] latitude and longitude: CWOP places a report by its position.

A report is one APRS weather packet over APRS-IS (cwop.aprs.net:14580): log in,
send the packet, hang up. CWOP asks for one every 5 to 10 minutes, no more.

    FW1234>APRS,TCPIP*:@211654z3230.00N/10815.00W_220/004g005t077r000P000h50b10150CC0AC-Weather-1.2.0
                        time   position (_ = weather) wind/gust temp rain hum pressure
"""

from __future__ import annotations

import configparser
import logging
import socket
from datetime import datetime

from outputs._station import SOFTWARE, VERSION, Station, needs, number
from iolog import report
from sources import Reading

log = logging.getLogger("acurite")

NAME = "cwop"
SERVER, PORT = "cwop.aprs.net", 14580
station = Station("cwop", every_minutes=10)


def enabled(cfg: configparser.ConfigParser) -> bool:
    if not needs(cfg, "cwop", "station_id"):
        return False
    try:
        _position(cfg)
    except ValueError:
        log.warning("[cwop] is enabled but [station] latitude / longitude are not set")
        return False
    return True


def _position(cfg: configparser.ConfigParser) -> str:
    """[station] latitude/longitude as APRS writes them: 3230.00N/10815.00W."""
    def part(value: float, width: int, pos: str, neg: str) -> str:
        hundredths = round(abs(value) * 6000)        # minutes, to 2 places
        degrees, minutes = divmod(hundredths, 6000)
        return f"{degrees:0{width}d}{minutes / 100:05.2f}{pos if value >= 0 else neg}"
    lat = float(cfg.get("station", "latitude", fallback=""))
    lon = float(cfg.get("station", "longitude", fallback=""))
    return part(lat, 2, "N", "S") + "/" + part(lon, 3, "E", "W")


def _three(v: float | None) -> str:
    return "..." if v is None else f"{max(0, min(999, int(round(v)))):03d}"


def packet(cfg: configparser.ConfigParser, fields: dict, when: datetime) -> str:
    call = cfg.get("cwop", "station_id").strip().upper()
    wdir, wind = number(fields, "wind_dir_deg"), number(fields, "wind_mph")
    if wdir is not None:
        wdir = round(wdir) % 360 or 360               # APRS: 360 is north, 000 is calm/unknown
    temp, hum = number(fields, "temp_f"), number(fields, "humidity_pct")
    pressure = number(fields, "pressure_inhg")
    rain_hour, rain_day = number(fields, "rain_hour_in"), number(fields, "rain_day_in")
    if temp is None:
        t = "..."
    elif temp < 0:
        t = f"-{min(99, int(round(-temp))):02d}"
    else:
        t = f"{min(999, int(round(temp))):03d}"
    out = (f"{call}>APRS,TCPIP*:@{when:%d%H%M}z{_position(cfg)}_"
           f"{_three(wdir)}/{_three(wind)}g{_three(number(fields, 'wind_gust_mph'))}t{t}")
    if rain_hour is not None:
        out += f"r{_three(rain_hour * 100)}"
    if rain_day is not None:
        out += f"P{_three(rain_day * 100)}"
    if hum is not None:
        out += f"h{int(round(hum)) % 100:02d}"         # 100% is written 00
    if pressure is not None:
        out += f"b{int(round(pressure * 338.639)):05d}"   # tenths of a millibar
    return out + SOFTWARE


def _send_packet(cfg: configparser.ConfigParser, text: str) -> str:
    """Log in, send one packet, and return the server's login answer."""
    call = cfg.get("cwop", "station_id").strip().upper()
    passcode = cfg.get("cwop", "passcode", fallback="-1").strip() or "-1"
    server = cfg.get("cwop", "server", fallback=SERVER).strip() or SERVER
    with socket.create_connection((server, cfg.getint("cwop", "port", fallback=PORT)), timeout=20) as sock:
        reader = sock.makefile("r", encoding="ascii", errors="replace", newline="\n")
        reader.readline()                                       # the server's banner
        sock.sendall(f"user {call} pass {passcode} vers CC0AC-Weather {VERSION}\r\n".encode())
        answer = reader.readline().strip()                      # "# logresp FW1234 unverified, ..."
        sock.sendall((text + "\r\n").encode())
    return answer


def send(cfg: configparser.ConfigParser, reading: Reading) -> None:
    due = station.take(cfg, reading)
    if due is None:
        return
    text = packet(cfg, *due)
    try:
        answer = _send_packet(cfg, text)
    except OSError as exc:
        log.warning("CWOP send failed: %s", exc)
        report(NAME, False, str(exc))
        return
    if "logresp" in answer:
        log.info("CWOP sent: %s (%s)", text, answer)
    else:
        log.warning("CWOP login not answered as expected: %r; sent %s", answer, text)
    report(NAME, "logresp" in answer, f"sent {text}; answered {answer}")
