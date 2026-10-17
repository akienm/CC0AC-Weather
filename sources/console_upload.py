"""
A weather station console's "custom server" upload as a source.

Many consoles and WiFi gateways can send their readings to a server you name,
as well as to their own cloud: Ambient Weather (WS-2902, WS-2000 and their
hubs, set in the awnet app under "Customized"), Ecowitt and the Fine Offset
stations sold under other names (GW1000/GW1100/GW2000, HP2551, Misol, Froggit,
Sainlogic; set in the WS View app or the gateway's web page), and anything that
speaks Weather Underground's upload format. Point that setting at this machine
and this source records each upload. The console keeps uploading to its own
cloud too; we only listen alongside it.

Three formats arrive here, told apart by what they carry:
  Ecowitt:      POST, form fields PASSKEY, stationtype, tempf, tempinf, ...
  Ambient:      GET, query MAC or PASSKEY, stationtype=AMBWeather..., tempf, ...
  Wunderground: GET, query ID, PASSWORD, tempf, indoortempf, baromin, ...
All three send US units. One upload carries every sensor the console hears, so
it becomes several readings: console-outdoor, console-indoor, and
console-ch1 ... console-ch8 for extra room sensors.

NOT TESTED ON REAL HARDWARE. The field names come from the published formats
and from what other open-source receivers read. Every request is kept in
console-raw/<date>.log beside config.ini, and any field this source doesn't
read is logged once by name, so a tester's --diagnostics bundle shows what
their console really sends.
"""

from __future__ import annotations

import configparser
import logging
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sources import Reading, derive

log = logging.getLogger("acurite")

NAME = "console"
CONFIG_DIR = Path.home() / ".cc0ac-weather"
DEFAULT_PORT = 12346
CONNECTION_TIMEOUT = 15

# Upload fields → reading fields, per sensor. Units are the same in all three formats.
OUTDOOR = {
    "tempf": "temp_f", "humidity": "humidity_pct", "dewptf": "dew_point_f",
    "windchillf": "wind_chill_f", "feelslike": "feels_like_f", "heatindexf": "heat_index_f",
    "winddir": "wind_dir_deg", "windspeedmph": "wind_mph", "windgustmph": "wind_gust_mph",
    "windspdmph_avg10m": "wind_avg_mph", "winddir_avg10m": "wind_gust_dir_deg",
    "hourlyrainin": "rain_hour_in", "rainin": "rain_hour_in", "dailyrainin": "rain_day_in",
    "uv": "uv_index", "UV": "uv_index",
    "baromrelin": "pressure_inhg", "baromin": "pressure_inhg",
    "lightning_num": "strike_count", "lightning_day": "strike_count",
    "lightning_time": "last_strike_ts",
}
INDOOR = {"tempinf": "temp_f", "indoortempf": "temp_f", "humidityin": "humidity_pct", "indoorhumidity": "humidity_pct"}
CHANNEL = {"temp{n}f": "temp_f", "humidity{n}": "humidity_pct"}
CHANNELS = range(1, 9)

# Battery flags, per sensor. Ambient sends 1 for good; Ecowitt sends 0 for good.
OUTDOOR_BATTERY = ("battout", "wh65batt", "wh80batt", "wh68batt")
INDOOR_BATTERY = ("battin", "wh25batt", "wh26batt")
CHANNEL_BATTERY = "batt{n}"

# Read, but not into a sensor's fields: identity, time and housekeeping.
NOT_READINGS = {"PASSKEY", "MAC", "ID", "PASSWORD", "stationtype", "dateutc", "freq", "model",
                "action", "realtime", "rtfreq", "softwaretype", "runtime", "interval", "heap"}
# Shown as **** in the raw log: the Weather Underground key is a password.
SECRET = ("PASSWORD",)


def enabled(cfg: configparser.ConfigParser) -> bool:
    return cfg.getboolean("console", "enabled", fallback=False)


def _number(v: str):
    try:
        f = float(v)
    except ValueError:
        return None
    return int(f) if f.is_integer() and "." not in v else f


def _battery(value: str, ecowitt: bool) -> str | None:
    if value not in ("0", "1"):
        return None   # a voltage or a 0-5 level; kept in the raw log, not read
    good = value == ("0" if ecowitt else "1")
    return "normal" if good else "low"


def station(params: dict) -> str:
    """Who sent it: the console's MAC or passkey, or its Weather Underground station ID."""
    return params.get("MAC") or params.get("PASSKEY") or params.get("ID") or ""


def readings(params: dict, method: str, received_utc: str, raw: str) -> list[Reading]:
    """The readings in one upload: outdoor, indoor and each extra channel it carries."""
    ecowitt = method == "POST" and not params.get("stationtype", "").startswith("AMBWeather")
    stamp = params.get("dateutc", "")
    sensor_utc = stamp.replace(" ", "T") + "Z" if len(stamp) == 19 else None
    receiver = station(params)

    def one(sensor_id: str, kind: str, names: dict, batteries) -> Reading | None:
        fields = {}
        for key, field_name in names.items():
            v = _number(params.get(key, ""))
            if v is not None:
                fields.setdefault(field_name, v)
        if not fields:
            return None
        for key in batteries:
            b = _battery(params.get(key, ""), ecowitt)
            if b is not None:
                fields["battery"] = b
                break
        return Reading(source=NAME, sensor_id=sensor_id, type=kind, received_utc=received_utc,
                       raw=raw, fields=derive(fields), receiver=receiver, sensor_utc=sensor_utc)

    found = [one("console-outdoor", "outdoor", OUTDOOR, OUTDOOR_BATTERY),
             one("console-indoor", "console", INDOOR, INDOOR_BATTERY)]
    for n in CHANNELS:
        found.append(one(f"console-ch{n}", "room", {k.format(n=n): v for k, v in CHANNEL.items()},
                         [CHANNEL_BATTERY.format(n=n)]))
    found = [r for r in found if r is not None]

    # Ecowitt's lightning distance is in km; Ambient's (lightning_distance) in miles.
    if found and found[0].sensor_id == "console-outdoor":
        km, mi = _number(params.get("lightning", "")), _number(params.get("lightning_distance", ""))
        if mi is None and km is not None:
            mi = round(km * 0.621371, 1)
        if mi is not None:
            found[0].fields["last_strike_mi"] = mi
        # Ecowitt's solar radiation is W/m²; the dashboard shows lux. 126.7 lux per W/m²
        # is the factor Ecowitt's own displays use for sunlight.
        wm2 = _number(params.get("solarradiation", ""))
        if wm2 is not None:
            found[0].fields["light_lux"] = round(wm2 * 126.7)
    return found


def known(key: str) -> bool:
    if key in NOT_READINGS or key in OUTDOOR or key in INDOOR or key in OUTDOOR_BATTERY or key in INDOOR_BATTERY:
        return True
    if key in ("lightning", "lightning_distance", "solarradiation"):
        return True
    return any(key in (k.format(n=n) for k in (*CHANNEL, CHANNEL_BATTERY)) for n in CHANNELS)


def _shown(params: dict) -> str:
    return urllib.parse.urlencode({k: ("****" if k in SECRET else v) for k, v in params.items()})


def _raw_dir(cfg: configparser.ConfigParser) -> Path:
    return Path(cfg.get("console", "raw_dir", fallback=str(CONFIG_DIR / "console-raw"))).expanduser()


def _log_raw(raw_dir: Path, line: str) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc)
    with (raw_dir / f"{stamp:%Y-%m-%d}.log").open("a", encoding="utf-8") as f:
        f.write(f"{stamp:%Y-%m-%dT%H:%M:%SZ}\t{line}\n")


def _make_handler(cfg: configparser.ConfigParser, emit):
    # If set, only uploads from this console (its MAC, passkey or station ID) are recorded.
    only = cfg.get("console", "station", fallback="").strip().upper()
    raw_dir = _raw_dir(cfg)
    unread: set = set()

    class ConsoleHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _handle(self, method: str, body: bytes):
            parsed = urllib.parse.urlparse(self.path)
            # Ambient's app makes you end the path with "?", so the query may start "?&".
            params = dict(urllib.parse.parse_qsl(parsed.query.lstrip("?&"), keep_blank_values=True))
            if body:
                params.update(urllib.parse.parse_qsl(body.decode(errors="replace"), keep_blank_values=True))
            _log_raw(raw_dir, f"{method} {parsed.path} {_shown(params)}")
            new = sorted(k for k in params if not known(k) and k not in unread)
            if new:
                unread.update(new)
                log.info("CONSOLE sends fields not read (kept in console-raw): %s", ", ".join(new))
            if only and station(params).upper() != only:
                log.warning("CONSOLE upload from unknown station %r not recorded", station(params))
            else:
                now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                found = readings(params, method, now, _shown(params))
                if not found:
                    log.info("CONSOLE %s %s carried no reading we read", method, parsed.path)
                for r in found:
                    try:
                        e = emit(r)
                        log.info("CONSOLE %s %s %s", e["type"], e["name"],
                                 {k: r.fields[k] for k in ("temp_f", "humidity_pct", "wind_mph") if k in r.fields})
                    except Exception as exc:
                        log.error("CONSOLE record failed: %s (raw=%s)", exc, r.raw)
            # Weather Underground's own answer; the others accept any 200.
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "8")
            self.end_headers()
            self.wfile.write(b"success\n")

        def do_GET(self):
            self._handle("GET", b"")

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self._handle("POST", self.rfile.read(length) if length else b"")

    return ConsoleHandler


class _Server(ThreadingHTTPServer):
    request_queue_size = 64

    def finish_request(self, request, client_address):
        request.settimeout(CONNECTION_TIMEOUT)
        super().finish_request(request, client_address)

    def handle_error(self, request, client_address):
        log.debug("Console connection from %s dropped", client_address[0])


def run(cfg: configparser.ConfigParser, emit) -> None:
    port = cfg.getint("console", "port", fallback=DEFAULT_PORT)
    server = _Server(("", port), _make_handler(cfg, emit))
    log.info("Console listener on port %d", port)
    server.serve_forever()
