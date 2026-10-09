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

from sources import FIELDS, Reading

log = logging.getLogger("acurite")

NAME = "mqtt"
KEEPALIVE = 120   # seconds; the connection is opened afresh after half that idle

# Field → Home Assistant unit, device_class, and whether it is a measurement.
HA = {
    "temp_f": ("°F", "temperature"), "dew_point_f": ("°F", "temperature"),
    "heat_index_f": ("°F", "temperature"), "feels_like_f": ("°F", "temperature"),
    "wind_chill_f": ("°F", "temperature"),
    "humidity_pct": ("%", "humidity"),
    "wind_mph": ("mph", "wind_speed"), "wind_avg_mph": ("mph", "wind_speed"),
    "wind_gust_mph": ("mph", "wind_speed"),
    "wind_dir_deg": ("°", None), "wind_gust_dir_deg": ("°", None),
    "rain_hour_in": ("in", "precipitation"), "rain_day_in": ("in", "precipitation"),
    "pressure_inhg": ("inHg", "atmospheric_pressure"),
    "uv_index": ("UV index", None), "light_lux": ("lx", "illuminance"),
    "strike_count": (None, None), "last_strike_mi": ("mi", "distance"),
}

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
        name = cfg.get("sensors", sid, fallback="") if cfg.has_section("sensors") else ""
        device = {"identifiers": [f"cc0ac-weather-{sid}"], "manufacturer": "AcuRite",
                  "model": reading.type or "sensor", "name": name or f"{reading.type} {sid}"}
        for field in reading.fields:
            if (sid, field) in _announced or field not in FIELDS:
                continue
            unit, device_class = HA.get(field, (None, None))
            config = {"name": FIELDS[field].split(",")[0].capitalize(),
                      "unique_id": f"cc0ac-weather-{sid}-{field}",
                      "state_topic": f"{prefix}/{sid}/{field}", "device": device}
            if unit:
                config["unit_of_measurement"] = unit
            if device_class:
                config["device_class"] = device_class
            if field in HA and field != "strike_count":
                config["state_class"] = "measurement"
            out.append((f"{ha}/sensor/cc0ac-weather-{sid}/{field}/config", json.dumps(config)))
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
            break
        except OSError as exc:
            _close()
            if attempt == 2:
                log.warning("MQTT publish failed: %s", exc)
                return
    prefix = cfg.get("mqtt", "discovery_prefix", fallback="homeassistant").strip().strip("/") + "/"
    _announced.update((reading.sensor_id, t.split("/")[-2]) for t, _ in msgs if t.startswith(prefix))
