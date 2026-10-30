"""
The AcuRite Access hub (09155M) as a source.

The Access sends one HTTPS request per sensor reading to the server named on
its local page (Server Name, default atlasapi.myacurite.com):
  /weatherstation/updateweatherstation?id=<MAC>&mt=<Atlas|tower|...>&sensor=<id>&tempf=...
We record the reading, then pass the request on to AcuRite unchanged and hand
AcuRite's answer back to the hub, so AcuRite keeps working while we listen.
The hub uploads to Weather Underground on its own; nothing here touches that.
"""

from __future__ import annotations

import configparser
import json
import logging
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from cc0ac.iolog import report
from cc0ac.sources import Emit, Reading

log = logging.getLogger("acurite")

NAME = "acurite-access"
CONFIG_DIR = Path.home() / ".cc0ac-weather"
ACCESS_RELAY_URL = "https://atlasapi.myacurite.com"
HUB_UPDATE_PATH = "/weatherstation/updateweatherstation"

# Hub query keys → reading fields. Every key also lands in the raw log.
HUB_FIELDS = {
    "tempf": "temp_f", "indoortempf": "temp_f",
    "humidity": "humidity_pct", "indoorhumidity": "humidity_pct",
    "dewptf": "dew_point_f", "heatindex": "heat_index_f",
    "feelslike": "feels_like_f", "windchill": "wind_chill_f",
    "windspeedmph": "wind_mph", "windspeedavgmph": "wind_avg_mph",
    "windgustmph": "wind_gust_mph", "winddir": "wind_dir_deg",
    "windgustdir": "wind_gust_dir_deg",
    "rainin": "rain_hour_in", "dailyrainin": "rain_day_in",
    "baromin": "pressure_inhg", "uvindex": "uv_index",
    "lightintensity": "light_lux", "measured_light_seconds": "light_seconds",
    "strikecount": "strike_count", "last_strike_distance": "last_strike_mi",
    "last_strike_ts": "last_strike_ts", "interference": "interference",
    "sensorbattery": "battery", "rssi": "signal", "hubbattery": "hub_battery",
}

CONNECTION_TIMEOUT = 15  # seconds a client may stall before its connection is dropped


def enabled(cfg: configparser.ConfigParser) -> bool:
    return cfg.has_section("hub") and cfg.getboolean("hub", "enabled", fallback=True)


def _hub_value(v: str) -> Any:
    """Numbers as numbers; anything else (battery 'normal'/'low', timestamps) as text."""
    try:
        f = float(v)
    except ValueError:
        return v
    return int(f) if f.is_integer() and "." not in v else f


def reading(query: str, received_utc: str) -> Reading:
    """One hub reading, from the query string the hub sent."""
    params = dict(urllib.parse.parse_qsl(query))
    hub_utc = params.get("dateutc", "")
    return Reading(
        source=NAME, sensor_id=params.get("sensor", ""), type=params.get("mt"),
        received_utc=received_utc, raw=query, receiver=params.get("id"),
        sensor_utc=hub_utc + "Z" if len(hub_utc) == 19 else (hub_utc or None),
        fields={HUB_FIELDS[k]: _hub_value(v) for k, v in params.items() if k in HUB_FIELDS and v != ""})


def _raw_dir(cfg: configparser.ConfigParser) -> Path:
    return Path(cfg.get("hub", "raw_dir", fallback=str(CONFIG_DIR / "hub-raw"))).expanduser()


def raw_readings(cfg: configparser.ConfigParser) -> Iterator[Any]:
    """Every request in the raw hub logs: a Reading for each one from our hub,
    None for anything else (other paths, other hubs)."""
    hub_id = cfg.get("hub", "hub_id", fallback="").strip().upper()
    for log_file in sorted(_raw_dir(cfg).glob("*.log")):
        for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines():
            stamp, _, request = line.partition("\t")
            target = request.split(" ")[1] if request.count(" ") else ""
            path, _, query = target.partition("?")
            params = dict(urllib.parse.parse_qsl(query))
            # The same test the live listener applies before it records a reading.
            if path != HUB_UPDATE_PATH or (hub_id and params.get("id", "").upper() != hub_id):
                yield None
            else:
                yield reading(query, stamp)


def _log_raw(raw_dir: Path, path_and_query: str) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc)
    with (raw_dir / f"{stamp:%Y-%m-%d}.log").open("a", encoding="utf-8") as f:
        f.write(f"{stamp:%Y-%m-%dT%H:%M:%SZ}\t{path_and_query}\n")


def without(query: str, fields: Any) -> str:
    """The hub's query string with the keys of these reading fields taken out."""
    keep = [(k, v) for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True)
            if HUB_FIELDS.get(k) not in fields]
    return urllib.parse.urlencode(keep)


def _relay(relay_url: str, path_and_query: str, method: str = "GET", body: bytes | None = None,
           ctype: str | None = None, agent: str | None = None) -> tuple[int, str, bytes] | None:
    req = urllib.request.Request(relay_url + path_and_query, data=body, method=method)
    if ctype:
        req.add_header("Content-Type", ctype)
    if agent:
        # AcuRite answers "Invalid checkin data" to anything not calling itself Atlas/<fw>.
        req.add_header("User-Agent", agent)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.headers.get("Content-Type", "application/json"), resp.read()
    except urllib.error.HTTPError as exc:
        # AcuRite answered; hand its answer to the hub exactly as given.
        return exc.code, exc.headers.get("Content-Type", "application/json"), exc.read()
    except Exception as exc:
        log.warning("Relay to %s failed: %s", relay_url, exc)
        return None


def _make_hub_handler(cfg: configparser.ConfigParser, emit: Emit) -> type[BaseHTTPRequestHandler]:
    relay_on = cfg.getboolean("hub", "relay", fallback=True)
    relay_url = cfg.get("hub", "relay_url", fallback=ACCESS_RELAY_URL).rstrip("/")
    # If set, only readings from this hub (its Device ID / MAC) are recorded;
    # anything else is relayed but not kept. Matters once 443 faces the internet.
    hub_id = cfg.get("hub", "hub_id", fallback="").strip().upper()
    raw_dir = _raw_dir(cfg)
    # [sanity] hold_back_from_acurite: what sanity.py held back is taken out of the
    # reading before it is passed on, so AcuRite (and Weather Underground through
    # it) never shows it. Off unless set: AcuRite gets what the hub sent.
    hold_back = cfg.getboolean("sanity", "hold_back_from_acurite", fallback=False)

    class HubHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # silence default access log
            pass

        def _answer(self, status: int, ctype: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle(self, method: str, body: bytes | None) -> None:
            # The Access sends its readings in the query string; firmware 051 uses POST.
            _log_raw(raw_dir, f"{method} {self.path}" + (f" {body[:500]!r}" if body else ""))
            parsed = urllib.parse.urlparse(self.path)
            relayed = self.path
            if parsed.path == HUB_UPDATE_PATH:
                query = parsed.query
                if body and "form-urlencoded" in self.headers.get("Content-Type", ""):
                    query += "&" + body.decode(errors="replace")
                params = dict(urllib.parse.parse_qsl(query))
                if hub_id and params.get("id", "").upper() != hub_id:
                    log.warning("HUB reading from unknown id %r not recorded", params.get("id"))
                    self._answer(403, "application/json", b'{"error":"unknown hub"}')
                    return
                try:
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    r = reading(query, now)
                    e = emit(r)
                    if hold_back and r.suspect:
                        relayed = parsed._replace(query=without(parsed.query, r.suspect)).geturl()
                        if body and "form-urlencoded" in self.headers.get("Content-Type", ""):
                            body = without(body.decode(errors="replace"), r.suspect).encode()
                    log.info("HUB %s %s %s", e["type"], e["name"],
                             {k: e["fields"].get(k) for k in ("temp_f", "humidity_pct", "wind_mph") if k in e["fields"]})
                except Exception as exc:
                    log.error("HUB record failed: %s (query=%s)", exc, parsed.query)
            else:
                # Only the upload path is the hub's; with 443 open to the internet,
                # everything else is scanners and gets nothing, not a relay.
                log.info("HUB other path refused: %s %s from %s", method, self.path, self.client_address[0])
                self._answer(404, "text/plain", b"not found")
                return

            answer = (_relay(relay_url, relayed, method, body, self.headers.get("Content-Type"),
                             self.headers.get("User-Agent"))
                      if relay_on else None)
            if relay_on:
                report("acurite-relay", answer is not None and answer[0] < 300,
                       "AcuRite did not answer (see the service log)" if answer is None
                       else f"{answer[0]} {answer[2][:200].decode(errors='replace')}")
            if answer is not None and answer[0] >= 300:
                # AcuRite refused it. Passing the refusal on makes the Access resend the
                # same reading forever, so log it and answer as if no relay were set.
                log.warning("Relay: AcuRite answered %d %r; headers sent were %s",
                            answer[0], answer[2][:200], dict(self.headers))
                answer = None
            if answer is None:
                # What the Access accepts when AcuRite is not answering (per acuparse).
                offset = datetime.now().astimezone().strftime("%z")
                answer = (200, "application/json",
                          json.dumps({"timezone": f"{offset[:3]}:{offset[3:]}"}).encode())
            self._answer(*answer)

        def do_GET(self) -> None:
            self._handle("GET", None)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            self._handle("POST", self.rfile.read(length) if length else b"")

    return HubHandler


def _hub_tls_context(cfg: configparser.ConfigParser) -> ssl.SSLContext:
    cert_dir = Path(cfg.get("hub", "cert_dir", fallback=str(CONFIG_DIR))).expanduser()
    crt, key = cert_dir / "hub.crt", cert_dir / "hub.key"
    if not crt.exists() or not key.exists():
        log.info("Generating self-signed hub certificate in %s", cert_dir)
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
             "-subj", "/CN=atlasapi.myacurite.com", "-keyout", str(key), "-out", str(crt)],
            check=True, capture_output=True,
        )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # The Access speaks TLS 1.1; modern OpenSSL refuses that by default.
    ctx.minimum_version = ssl.TLSVersion.TLSv1
    ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    ctx.load_cert_chain(crt, key)
    return ctx


class _HubServer(ThreadingHTTPServer):
    """Does the TLS handshake in each connection's own thread, under a timeout.

    Wrapping the listening socket instead puts the handshake inside accept(),
    in the one serving thread: a single client that connects and says nothing
    stalls every connection after it. Internet scanners do exactly that, and
    the hub went unheard for an hour (2026-10-04).
    """
    request_queue_size = 64
    tls_context: ssl.SSLContext | None = None

    def finish_request(self, request: Any, client_address: Any) -> None:
        request.settimeout(CONNECTION_TIMEOUT)
        if self.tls_context is not None:
            request = self.tls_context.wrap_socket(request, server_side=True)
        super().finish_request(request, client_address)

    def handle_error(self, request: Any, client_address: Any) -> None:
        log.debug("Hub connection from %s dropped: %s", client_address[0], sys.exc_info()[1])


def run(cfg: configparser.ConfigParser, emit: Emit) -> None:
    port = cfg.getint("hub", "port", fallback=443)
    handler = _make_hub_handler(cfg, emit)
    try:
        server = _HubServer(("", port), handler)
    except PermissionError:
        log.error("Hub listener: cannot bind port %d — run with CAP_NET_BIND_SERVICE "
                  "(see weather_monitor.service) or use a port above 1024", port)
        return
    if cfg.getboolean("hub", "tls", fallback=True):
        server.tls_context = _hub_tls_context(cfg)
    log.info("Hub listener on port %d (tls=%s, relay=%s)", port,
             cfg.getboolean("hub", "tls", fallback=True), cfg.getboolean("hub", "relay", fallback=True))
    server.serve_forever()
