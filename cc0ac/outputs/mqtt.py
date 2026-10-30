"""
Publish readings to an MQTT broker ([mqtt]), for Home Assistant and the like.

Each field goes to its own topic, retained, so a newcomer gets the latest value
at once:

    cc0ac-weather/<sensor_id>/<field>     e.g. cc0ac-weather/00001234/temp_f → 61.2

With discovery on (the default), Home Assistant's MQTT integration finds every
sensor by itself: each sensor shows up as a device, named as in [sensors], with
one entity per field, in the right units.

MQTT 3.1.1, QoS 0, written out here so there is nothing to install. No TLS:
for a broker on your own network.
"""

from __future__ import annotations

import configparser
import json
import logging
import select
import socket
import struct
import time

from cc0ac import sensor_config
from cc0ac.iolog import report
from cc0ac.sources import FIELDS, Reading

log = logging.getLogger("acurite")

NAME = "mqtt"
KEEPALIVE = 120   # seconds; the connection is opened afresh after half that idle

# Field → how Home Assistant shows it: name, unit, device_class, state_class,
# decimal places. Checked against Home Assistant 2026.10: it accepts each
# device_class with the state_class given, and converts units for metric houses.
M, ANGLE, DAILY = "measurement", "measurement_angle", "total_increasing"
HA = {
    "temp_f": ("Temperature", "°F", "temperature", M, 1),
    "dew_point_f": ("Dew point", "°F", "temperature", M, 1),
    "heat_index_f": ("Heat index", "°F", "temperature", M, 1),
    "feels_like_f": ("Feels like", "°F", "temperature", M, 1),
    "wind_chill_f": ("Wind chill", "°F", "temperature", M, 1),
    "humidity_pct": ("Humidity", "%", "humidity", M, 0),
    "wind_mph": ("Wind speed", "mph", "wind_speed", M, 1),
    "wind_avg_mph": ("Average wind speed", "mph", "wind_speed", M, 1),
    "wind_gust_mph": ("Wind gust", "mph", "wind_speed", M, 1),
    "wind_dir_deg": ("Wind direction", "°", "wind_direction", ANGLE, 0),
    "wind_gust_dir_deg": ("Gust direction", "°", "wind_direction", ANGLE, 0),
    "rain_hour_in": ("Rain, last hour", "in", "precipitation", M, 2),
    "rain_day_in": ("Rain today", "in", "precipitation", DAILY, 2),   # back to 0 at midnight
    "pressure_inhg": ("Pressure", "inHg", "atmospheric_pressure", M, 2),
    "uv_index": ("UV index", "UV index", None, M, 0),
    "light_lux": ("Light", "lx", "illuminance", M, 0),
    "light_seconds": ("Light time", "s", "duration", DAILY, 0),
    "strike_count": ("Lightning strikes", None, None, DAILY, 0),
    "last_strike_mi": ("Last strike distance", "mi", "distance", M, 0),
    "signal": ("Signal strength", None, None, M, 0),
}
# "normal"/"low" fields: a battery problem sensor, on when low.
BATTERIES = {"battery": "Battery", "hub_battery": "Hub battery"}
DIAGNOSTIC = {"signal", "hub_battery"}   # shown under the device's diagnostics, not on its main card

_sock: socket.socket | None = None
_last_sent = 0.0
_announced: set[tuple[str, str]] = set()


def enabled(cfg: configparser.ConfigParser) -> bool:
    if not cfg.getboolean("mqtt", "enabled", fallback=False):
        return False
    if not cfg.get("mqtt", "host", fallback="").strip():
        log.warning("MQTT enabled but no host set in config")
        return False
    return True


# ── The protocol: just CONNECT, PUBLISH and DISCONNECT ────────────────────────

def _string(s: str) -> bytes:
    b = s.encode()
    return struct.pack("!H", len(b)) + b


def _packet(kind: int, body: bytes) -> bytes:
    """A packet: its type byte, then the length of the rest, 7 bits a byte."""
    n, length = len(body), b""
    while True:
        n, digit = n >> 7, n & 0x7F
        length += bytes([digit | (0x80 if n else 0)])
        if not n:
            return bytes([kind]) + length + body


def connect_packet(client_id: str, username: str = "", password: str = "") -> bytes:
    flags = 0x02 | (0x80 if username else 0) | (0x40 if username and password else 0)   # clean session
    body = _string("MQTT") + bytes([4, flags]) + struct.pack("!H", KEEPALIVE) + _string(client_id)
    if username:
        body += _string(username)
        if password:
            body += _string(password)
    return _packet(0x10, body)


def publish_packet(topic: str, payload: str, retain: bool = True) -> bytes:
    return _packet(0x30 | (0x01 if retain else 0), _string(topic) + payload.encode())


CONNACK_REFUSED = {1: "protocol version", 2: "client ID", 3: "server unavailable",
                   4: "bad user name or password", 5: "not authorized"}


def _connect(cfg: configparser.ConfigParser) -> socket.socket:
    host = cfg.get("mqtt", "host").strip()
    port = cfg.getint("mqtt", "port", fallback=1883)
    sock = socket.create_connection((host, port), timeout=10)
    try:
        sock.sendall(connect_packet(cfg.get("mqtt", "client_id", fallback="cc0ac-weather"),
                                    cfg.get("mqtt", "username", fallback="").strip(),
                                    cfg.get("mqtt", "password", fallback="")))
        ack = b""
        while len(ack) < 4:
            chunk = sock.recv(4 - len(ack))
            if not chunk:
                raise ConnectionError("broker closed the connection")
            ack += chunk
        if ack[0] != 0x20:
            raise ConnectionError(f"unexpected reply from broker: {ack.hex()}")
        if ack[3]:
            raise ConnectionError(f"broker refused: {CONNACK_REFUSED.get(ack[3], ack[3])}")
    except Exception:
        sock.close()
        raise
    log.info("MQTT connected to %s:%d", host, port)
    return sock


def _close() -> None:
    global _sock
    if _sock is not None:
        try:
            _sock.sendall(_packet(0xE0, b""))
            _sock.close()
        except OSError:
            pass
    _sock = None


def _closed_by_broker(sock: socket.socket) -> bool:
    """A write to a connection the broker has closed still succeeds, once, and
    is lost; so look first. The broker sends nothing unasked at QoS 0, so
    anything to read means it has hung up."""
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        return bool(readable)
    except (OSError, ValueError):
        return True


def _connection(cfg: configparser.ConfigParser) -> socket.socket:
    """The open connection, or a fresh one if it was idle long or the broker hung up."""
    global _sock
    if _sock is not None and (time.monotonic() - _last_sent > KEEPALIVE / 2 or _closed_by_broker(_sock)):
        _close()
    if _sock is None:
        _sock = _connect(cfg)
        _announced.clear()   # a new session: announce to Home Assistant again
    return _sock


# ── Readings → topics ─────────────────────────────────────────────────────────

def messages(cfg: configparser.ConfigParser, reading: Reading) -> list[tuple[str, str]]:
    """(topic, payload) for one reading: Home Assistant announcements for fields
    not yet announced, then one message per field."""
    prefix = cfg.get("mqtt", "topic", fallback="cc0ac-weather").strip().strip("/")
    sid = reading.sensor_id
    out = []
    if cfg.getboolean("mqtt", "discovery", fallback=True):
        ha = cfg.get("mqtt", "discovery_prefix", fallback="homeassistant").strip().strip("/")
        name = sensor_config.title(cfg, sid)
        device = {"identifiers": [f"cc0ac-weather-{sid}"], "model": reading.type or "sensor",
                  "name": name or reading.name or f"{reading.type} {sid}"}
        # The hub's sensors, and AcuRite's heard by radio (their IDs are all digits).
        if reading.source == "acurite-access" or (reading.source == "rtl_433" and sid.isdigit()):
            device["manufacturer"] = "AcuRite"
        # A sensor silent this long shows as unavailable, not as its last value forever.
        stale_s = 60 * cfg.getint("mqtt", "stale_after_minutes", fallback=60)
        for field in reading.fields:
            if (sid, field) in _announced or field not in FIELDS:
                continue
            config = {"unique_id": f"cc0ac-weather-{sid}-{field}",
                      "state_topic": f"{prefix}/{sid}/{field}", "device": device}
            if stale_s:
                config["expire_after"] = stale_s
            if field in DIAGNOSTIC:
                config["entity_category"] = "diagnostic"
            if field in BATTERIES:
                component = "binary_sensor"
                config.update(name=BATTERIES[field], device_class="battery",
                              payload_on="low", payload_off="normal")
            else:
                component = "sensor"
                label, unit, device_class, state_class, places = HA.get(
                    field, (FIELDS[field].split(",")[0].capitalize(), None, None, None, None))
                config["name"] = label
                for key, value in (("unit_of_measurement", unit), ("device_class", device_class),
                                   ("state_class", state_class), ("suggested_display_precision", places)):
                    if value is not None:
                        config[key] = value
            out.append((f"{ha}/{component}/cc0ac-weather-{sid}/{field}/config", json.dumps(config)))
    for field, value in reading.fields.items():
        if field in FIELDS and value is not None:
            out.append((f"{prefix}/{sid}/{field}", str(value)))
    return out


def send(cfg: configparser.ConfigParser, reading: Reading) -> None:
    global _last_sent
    for attempt in (1, 2):   # a second try, on a fresh connection
        try:
            sock = _connection(cfg)
            msgs = messages(cfg, reading)
            if msgs:
                sock.sendall(b"".join(publish_packet(t, p) for t, p in msgs))
                _last_sent = time.monotonic()
                report(NAME, True, f"{len(msgs)} messages for {reading.sensor_id}")
            break
        except OSError as exc:
            _close()
            if attempt == 2:
                log.warning("MQTT publish failed: %s", exc)
                report(NAME, False, str(exc))
                return
    prefix = cfg.get("mqtt", "discovery_prefix", fallback="homeassistant").strip().strip("/") + "/"
    _announced.update((reading.sensor_id, t.split("/")[-2]) for t, _ in msgs
                      if t.startswith(prefix) and t.endswith("/config"))
