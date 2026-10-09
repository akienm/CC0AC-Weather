"""
rtl_433 with a USB radio dongle (RTL-SDR) as a source.

rtl_433 (https://github.com/merbanan/rtl_433) decodes the sensors' radio
messages off the air and prints each one as a line of JSON. This source runs
it, turns each line into a Reading, and restarts it if it stops. rtl_433 knows
about 250 kinds of sensor, AcuRite's among them; fields are read by rtl_433's
own names, so most weather sensors it decodes come through, not only AcuRite's.

AcuRite sensors get the same sensor IDs and types the Access hub gives them
(an 8-digit ID; Atlas, tower, 5N1), so names in [sensors] work for either.
Not yet tried with a real dongle: tested against recordings of AcuRite sensors
from the rtl_433 project (rtl_433_tests).

Rain: sensors send a running total that only goes up (and starts again from
zero when the batteries are changed). This source turns that into rain since
local midnight and rain in the last hour, the way the hub reports it, and keeps
what it needs for that in rtl_433-rain.json beside config.ini, so a restart
doesn't lose the day's rain.
"""

from __future__ import annotations

import configparser
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from sources import FIELDS, Reading, derive

log = logging.getLogger("acurite")

NAME = "rtl_433"
CONFIG_DIR = Path.home() / ".cc0ac-weather"
DEFAULT_PROTOCOLS = ["40", "74"]   # see config.ini.example; `rtl_433 -R help` lists them all

# rtl_433's model names → the hub's names for the same sensors.
ACURITE_TYPES = {"Acurite-Atlas": "Atlas", "Acurite-Iris": "Iris", "Acurite-Tower": "tower",
                 "Acurite-5n1": "5N1"}

# What differs between a sensor's repeats of one message (raw_msg carries the
# sequence number), so isn't compared when dropping repeats.
REPEAT_VARIES = ("time", "sequence_num", "raw_msg", "mic")


def enabled(cfg: configparser.ConfigParser) -> bool:
    return cfg.getboolean("capture", "enabled", fallback=False)


def command(cfg: configparser.ConfigParser) -> list[str]:
    """The rtl_433 command line. [capture] read = a recording (.cu8) or several,
    comma-separated, decodes those instead of the dongle: for testing."""
    protocols = DEFAULT_PROTOCOLS
    if cfg.has_option("capture", "protocols"):
        protocols = [p.strip() for p in cfg.get("capture", "protocols").split(",") if p.strip()]
    cmd = ["rtl_433", "-F", "json", "-M", "time:utc"]
    files = [f.strip() for f in cfg.get("capture", "read", fallback="").split(",") if f.strip()]
    if files:
        for f in files:
            cmd += ["-r", str(Path(f).expanduser())]
    else:
        cmd += ["-d", cfg.get("capture", "device_index", fallback="0")]
    for p in protocols:
        cmd += ["-R", p]
    return cmd


def _first(raw: dict, *keys):
    for k in keys:
        if raw.get(k) is not None:
            return raw[k]
    return None


def _scaled(raw: dict, units: dict, places: int = 1):
    """The first of several rtl_433 fields present, converted: units = {name: factor}."""
    for k, factor in units.items():
        if raw.get(k) is not None:
            return round(float(raw[k]) * factor, places)
    return None


def decode(raw: dict) -> tuple[str, str, dict, float | None] | None:
    """(sensor_id, type, fields, rain counter in inches) from one rtl_433 message,
    or None if it carries no sensor ID."""
    model = str(raw.get("model", ""))
    rid = raw.get("id")
    if not model or rid is None:
        return None
    acurite = model.lower().startswith("acurite")
    if acurite and str(rid).isdigit():
        sensor_id = f"{int(rid):08d}"
    else:
        # Lower case: config.ini's [sensors] names are read in lower case.
        sensor_id = (f"{model}-{rid}" + (f"-{raw['channel']}" if raw.get("channel") not in (None, "") else "")).lower()
    kind = ACURITE_TYPES.get(model, model.split("-", 1)[1] if acurite and "-" in model else model)

    fields = {
        "temp_f": _scaled(raw, {"temperature_F": 1}),
        "humidity_pct": _first(raw, "humidity"),
        "wind_mph": _scaled(raw, {"wind_avg_mi_h": 1, "wind_avg_km_h": 0.621371, "wind_avg_m_s": 2.236936}),
        "wind_gust_mph": _scaled(raw, {"wind_max_mi_h": 1, "wind_max_km_h": 0.621371, "wind_max_m_s": 2.236936}),
        "wind_dir_deg": _first(raw, "wind_dir_deg"),
        "pressure_inhg": _scaled(raw, {"pressure_inHg": 1, "pressure_hPa": 0.0295300}, 2),
        "uv_index": _first(raw, "uv", "uvi"),
        "light_lux": _first(raw, "light_lux", "lux"),
        "strike_count": _first(raw, "strike_count"),
        # rtl_433 doesn't name the unit; AcuRite's own displays show miles.
        "last_strike_mi": _first(raw, "strike_distance", "storm_dist"),
    }
    if fields["temp_f"] is None and raw.get("temperature_C") is not None:
        fields["temp_f"] = round(float(raw["temperature_C"]) * 9 / 5 + 32, 1)
    battery = _first(raw, "battery_ok")
    if battery is not None:
        fields["battery"] = "normal" if battery else "low"
    fields = {k: v for k, v in fields.items() if v is not None and k in FIELDS}
    rain = _scaled(raw, {"rain_in": 1, "rain_mm": 1 / 25.4}, 4)
    return sensor_id, kind, fields, rain


class RainCounter:
    """Turns each sensor's running rain total into rain today and in the last hour."""

    def __init__(self, path: Path | None):
        self.path = path
        self.state: dict = {}
        if path is not None and path.exists():
            try:
                self.state = json.loads(path.read_text())
            except (OSError, ValueError) as exc:
                log.warning("rtl_433 rain state %s unreadable, starting over: %s", path, exc)

    def update(self, sensor_id: str, counter: float, now: float) -> dict:
        s = self.state.setdefault(sensor_id, {"day": None, "today": 0.0, "last": None, "total": 0.0, "hour": []})
        grew = 0.0
        if s["last"] is not None:
            # A smaller total means the counter started again from zero.
            grew = counter - s["last"] if counter >= s["last"] else counter
        s["last"] = counter
        day = datetime.fromtimestamp(now).astimezone().strftime("%Y-%m-%d")
        if s["day"] != day:
            s["day"], s["today"] = day, 0.0
        s["today"] += grew
        s["total"] += grew
        s["hour"] = [h for h in s["hour"] if now - h[0] <= 3600] + [[now, s["total"]]]
        self._save()
        return {"rain_day_in": round(s["today"], 2), "rain_hour_in": round(s["total"] - s["hour"][0][1], 2)}

    def _save(self) -> None:
        if self.path is None:
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.state))
        os.replace(tmp, self.path)


def _utc(stamp) -> str | None:
    """rtl_433's time with -M time:utc, '2026-10-09 16:20:00', as ISO; None for a
    recording's offset ('@0.28s')."""
    s = str(stamp or "")
    return s.replace(" ", "T") + "Z" if len(s) == 19 and s[4] == "-" else None


def readings(lines, receiver: str, rain: RainCounter, sensors: dict | None = None):
    """Readings from rtl_433's JSON lines. sensors: if not empty, only these IDs
    are kept (neighbours' sensors are heard too). A sensor repeats each message
    two or three times in a row; the repeats are dropped."""
    recent: dict[str, tuple[str, float]] = {}
    for line in lines:
        line = line.strip()
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        decoded = decode(raw) if isinstance(raw, dict) else None
        if decoded is None:
            continue
        sensor_id, kind, fields, counter = decoded
        if sensors and sensor_id not in sensors:
            log.debug("rtl_433: skipping sensor %s (not in [sensors])", sensor_id)
            continue
        now = time.time()
        same = json.dumps({k: v for k, v in raw.items() if k not in REPEAT_VARIES}, sort_keys=True)
        if recent.get(sensor_id, ("", 0))[0] == same and now - recent[sensor_id][1] < 5:
            continue
        recent[sensor_id] = (same, now)
        if counter is not None:
            fields.update(rain.update(sensor_id, counter, now))
        yield Reading(source=NAME, sensor_id=sensor_id, type=kind,
                      received_utc=datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                      raw=line, fields=derive(fields), receiver=receiver, sensor_utc=_utc(raw.get("time")))


def _sensors(cfg: configparser.ConfigParser) -> set:
    return set(cfg.options("sensors")) if cfg.has_section("sensors") else set()


def run(cfg: configparser.ConfigParser, emit) -> None:
    cmd = command(cfg)
    reading_files = "-r" in cmd
    receiver = "rtl_433 file" if reading_files else f"rtl_433 dongle {cfg.get('capture', 'device_index', fallback='0')}"
    rain = RainCounter(CONFIG_DIR / "rtl_433-rain.json" if not reading_files else None)
    sensors = _sensors(cfg)
    if not sensors:
        log.info("rtl_433: no [sensors] listed, so every sensor heard is recorded, neighbours' too")
    while True:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            log.info("rtl_433 started (pid %d): %s", proc.pid, " ".join(cmd))
            for r in readings(proc.stdout, receiver, rain, sensors):
                try:
                    e = emit(r)
                    log.info("RADIO %s %s %s", e["type"], e["name"],
                             {k: r.fields[k] for k in ("temp_f", "humidity_pct", "wind_mph", "rain_day_in") if k in r.fields})
                except Exception as exc:
                    log.error("RADIO record failed: %s (raw=%s)", exc, r.raw)
            ret = proc.wait()
        except FileNotFoundError:
            log.error("rtl_433 is not installed (sudo apt install rtl-433); radio capture off")
            return
        except Exception as exc:
            log.error("rtl_433 source error: %s", exc)
            ret = -1
        if reading_files:
            log.info("rtl_433 finished reading %s", cfg.get("capture", "read"))
            return
        log.warning("rtl_433 exited (code %s); restarting in 10s", ret)
        time.sleep(10)


def discover(cfg: configparser.ConfigParser, duration_s: int = 300) -> None:
    """Print every sensor heard for duration_s seconds, then exit."""
    cmd = command(cfg)
    print(f"\n{'─' * 60}\nDISCOVER: listening {duration_s}s for sensors\nrtl_433: {' '.join(cmd)}\n{'─' * 60}\n")
    seen: dict[str, Reading] = {}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    deadline = time.monotonic() + duration_s

    def lines():
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                return
            yield line

    try:
        for r in readings(lines(), "discover", RainCounter(None)):
            if r.sensor_id not in seen:
                seen[r.sensor_id] = r
                shown = {k: r.fields[k] for k in ("temp_f", "humidity_pct", "wind_mph", "wind_dir_deg") if k in r.fields}
                print(f"  {r.sensor_id}  {r.type:<10} {json.loads(r.raw).get('model')}  {shown}")
    except KeyboardInterrupt:
        print("\n(stopped early)")
    finally:
        proc.terminate()
        proc.wait()
    print(f"\n{'─' * 60}\nFound {len(seen)} sensor(s). Name them in config.ini under [sensors]:\n")
    for sid, r in seen.items():
        print(f"  {sid} = My {r.type}")
    print(f"\n{'─' * 60}\n")
