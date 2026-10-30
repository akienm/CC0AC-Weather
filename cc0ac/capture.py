#!/usr/bin/env python3
"""
acurite-capture.py — AcuRite weather sensor capture daemon.

Two sources, either or both:
  * the AcuRite Access hub: an HTTPS listener the hub reports to, which records
    each reading and relays it on to AcuRite unchanged ([hub], sources/acurite_access.py);
  * rtl_433 with a USB radio dongle, decoding the sensors off the air
    ([capture], sources/rtl_433.py).
Each source in sources/ turns what it hears into Readings (sources/__init__.py);
readings land in <write_path>/current.json, current.js, history.csv and the
database, and go on to each output switched on in outputs/ (such as Weather
Underground for a station heard by radio).
With [forecast] on, the National Weather Service forecast, nearest airport
observation and active alerts land in <write_path>/forecast.json and forecast.js.

Requirements: Python 3 only, for the hub. For a radio dongle:
    sudo apt install rtl-433 rtl-sdr
    sudo usermod -aG plugdev $USER    # dongle access without root; log in again after

Setup:
    cp config.ini.example ~/.cc0ac-weather/config.ini
    python3 acurite-capture.py --discover   # with a dongle: find your sensor IDs
    # edit config.ini — add sensor IDs under [sensors]
    python3 acurite-capture.py              # run daemon

Usage:
    python3 acurite-capture.py [--config PATH] [--discover] [--discover-time N] [-v]
"""

from __future__ import annotations

import argparse
import configparser
import importlib
import json
import logging
import queue
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from cc0ac.sources import OUTDOOR_TYPES, Reading, derive
from cc0ac.sources import acurite_access
from cc0ac import alerts
from cc0ac import calibration
from cc0ac import climate
from cc0ac import diagnostics
from cc0ac import importer
from cc0ac import iolog
from cc0ac import sanity
from cc0ac import secure_web
from cc0ac import sensor_config
from cc0ac import units
from cc0ac import upkeep
from cc0ac.config import DEFAULT_CONFIG, add_heard_sensor, apply_timezone, button_list, card_rows, database_path, load_config, page_title, sensor_map, station_settings, station_version
from cc0ac.forecast import run_forecast
from cc0ac.recorder import current_lock, record_reading, reference_temp, refresh_snapshot, seed_heard
from cc0ac.store import OUTDOOR, believed_before, epoch_to_iso, iso_to_epoch, noaa_summary, open_db, roll_up, store_reading, summaries
from cc0ac.web import run_web_server

log = logging.getLogger("acurite")


log = logging.getLogger("acurite")


def import_raw(cfg: configparser.ConfigParser) -> None:
    """Load every reading in the raw hub logs into the store. Safe to run again:
    readings already there are skipped."""
    con = open_db(database_path(cfg))
    corrections = calibration.rules(cfg)
    watch = sanity.Watch(cfg)
    added = skipped = other = 0
    for reading in acurite_access.raw_readings(cfg):
        if reading is None:
            other += 1
            continue
        reading.fields = calibration.apply(reading.fields, corrections.get(reading.sensor_id.lower()))
        reading.fields, reading.suspect = watch.check(reading.sensor_id, reading.fields, iso_to_epoch(reading.received_utc),
                                                      reading.type in OUTDOOR_TYPES)
        if store_reading(con, reading):
            added += 1
        else:
            skipped += 1
    days = roll_up(con, time.time(), rebuild=True)
    total = con.execute("SELECT count(*) FROM readings").fetchone()[0]
    print(f"{days} days summed up; ", end="")
    print(f"{added} readings added, {skipped} already stored, {other} other requests left out; "
          f"{total} readings in {database_path(cfg)}")


def import_history(cfg: configparser.ConfigParser, args: argparse.Namespace) -> None:
    """--import: readings from before this station (importer.py)."""
    con = open_db(database_path(cfg))
    what = args.import_from
    if what.lower() == "wu":
        station = args.wu_station or cfg.get("weather_underground", "station_id", fallback="").strip()
        if not station or not args.wu_api_key or not args.date_from:
            sys.exit("--import wu needs --wu-station (or [weather_underground] station_id), --wu-api-key and --from")
        start = datetime.strptime(args.date_from, "%Y-%m-%d").date()
        end = (datetime.strptime(args.date_to, "%Y-%m-%d").date() if args.date_to
               else datetime.fromordinal(datetime.now().toordinal() - 1).date())
        kind, rows = "wu", importer.rows_wu(station, args.wu_api_key, start, end)
    else:
        path = Path(what).expanduser()
        if not path.exists():
            sys.exit(f"{path} not found")
        if path.is_dir() or importer.CUMULUS_LOG.fullmatch(path.name):
            kind, rows = "cumulus", importer.rows_cumulus(path, args.metric)
        elif path.suffix.lower() in (".sdb", ".db", ".sqlite"):
            kind, rows = "weewx", importer.rows_weewx(path)
        else:
            kind, rows = "csv", importer.rows_csv(path, args.metric)
    # The sensor it all belongs to: named, or the first outdoor one heard here.
    target: str = args.sensor
    if target is None:
        found = con.execute(f"SELECT sensor_id FROM readings WHERE type IN {OUTDOOR} AND query NOT LIKE 'import:%' "
                            "AND sensor_id NOT LIKE 'open-meteo%' AND sensor_id NOT LIKE 'nws-%' "
                            "ORDER BY received_utc LIMIT 1").fetchone()
        target = found[0] if found else "imported"
    target_type = con.execute("SELECT max(type) FROM readings WHERE sensor_id = ?", [target]).fetchone()[0] or "outdoor"
    first_live: dict[str, float | None] = {}

    def cutoff(sid: str) -> float | None:
        if sid not in first_live:
            t = con.execute("SELECT min(received_utc) FROM readings WHERE sensor_id = ? "
                            "AND query NOT LIKE 'import:%'", [sid]).fetchone()[0]
            first_live[sid] = iso_to_epoch(t) if t else None
        return first_live[sid]

    added = same = later = empty = 0
    into: set[str] = set()
    try:
        for row in rows:
            sid: str = (row.get("sensor") if args.sensor is None else None) or target
            kind_of = (row.get("type") if sid == row.get("sensor") else None) or target_type
            limit = cutoff(sid)
            if limit is not None and row["epoch"] >= limit:
                later += 1
                continue
            fields = importer.clean(row["fields"])
            if not fields:
                empty += 1
                continue
            when = epoch_to_iso(row["epoch"])
            reading = Reading(source=f"import-{kind}", sensor_id=sid, type=kind_of, received_utc=when,
                              raw=f"import:{kind}:{sid}:{when}", fields=derive(fields), receiver="import")
            into.add(sid)
            if store_reading(con, reading):
                added += 1
            else:
                same += 1
    except (ValueError, OSError, sqlite3.Error) as e:
        sys.exit(f"Import stopped: {e} ({added} readings added before it)")
    days = roll_up(con, time.time(), rebuild=True)
    print(f"{added} readings added (sensor {', '.join(sorted(into)) or target}), "
          f"{same} already there, {later} left out as newer than the station's own readings, "
          f"{empty} with nothing usable; {days} days summed up. The records card counts them within the hour.")


def _plugins(folder: str) -> list:
    """Every module in a plug-in folder (sources/, outputs/) beside this script."""
    here = Path(__file__).resolve().parent / folder
    return [importlib.import_module(f"cc0ac.{folder}.{p.stem}")
            for p in sorted(here.glob("*.py")) if not p.name.startswith("_")]


def load_sources() -> list:
    return _plugins("sources")


def load_outputs() -> list:
    return _plugins("outputs")


def _run_output(cfg: configparser.ConfigParser, output, todo: queue.Queue) -> None:
    """Hand each reading to one output, in order; a failure loses that reading only."""
    while True:
        reading = todo.get()
        try:
            output.send(cfg, reading)
        except Exception as exc:
            log.warning("%s output failed: %s", output.NAME, exc)


def run_daemon(cfg: configparser.ConfigParser, config_path: Path | None = None) -> None:
    iolog.keep()
    write_path = Path(cfg.get("device", "write_path")).expanduser()
    write_path.mkdir(parents=True, exist_ok=True)
    sensors = sensor_map(cfg)

    # The dashboard reads these settings from current.json; it cannot read config.ini.
    # The lower pane is a row of [buttons], each loading its page below; none set, no pane.
    page = {"title": page_title(cfg), "version": station_version(), "lower_buttons": button_list(cfg), "station": station_settings(cfg),
            "units": units.chosen(cfg),
            "outdoor_rows": card_rows(cfg, "outdoor_rows"),
            "sensor_rows": card_rows(cfg, "sensor_rows"),
            "sensor_cards": sensor_config.cards(cfg), "sun_moon_icon": sensor_config.sun_moon_icon(cfg)}
    db_path = database_path(cfg)
    with current_lock:
        seed_heard(db_path, time.time())
    refresh_snapshot(write_path, sensors, page)

    queues = []
    for output in load_outputs():
        if output.enabled(cfg):
            todo: queue.Queue = queue.Queue(maxsize=1000)
            threading.Thread(target=_run_output, args=(cfg, output, todo), daemon=True,
                             name=output.NAME).start()
            queues.append((output.NAME, todo))
            log.info("Output on: %s", output.NAME)

    # [calibration]: a sensor that reads a little off is corrected before anything uses its reading;
    # then [sanity]: a value no weather could produce is held back (sanity.py).
    corrections = calibration.rules(cfg)
    watch = sanity.Watch(cfg, recall=lambda sensor_id: believed_before(db_path, sensor_id))

    def emit(reading: Reading) -> dict:
        sid = reading.sensor_id.lower()
        if (reading.source in sensor_config.OWN_SOURCES and sid and sid not in sensors
                and sensor_config.is_sensor(sid) and re.fullmatch(r"[0-9a-z-]{1,24}", sid)):
            sensors[reading.sensor_id.lower()] = reading.sensor_id.lower()
            add_heard_sensor(config_path, reading.sensor_id.lower(), reading.type or "unknown")
        reading.fields = calibration.apply(reading.fields, corrections.get(reading.sensor_id.lower()))
        outdoor = reading.type in OUTDOOR_TYPES
        at = iso_to_epoch(reading.received_utc)
        reading.fields, reading.suspect = watch.check(
            reading.sensor_id, reading.fields, at, outdoor, reference_temp(reading.sensor_id, at) if outdoor else None)
        entry = record_reading(reading, write_path, sensors, page, db_path)
        for name, todo in queues:
            try:
                todo.put_nowait(reading)
            except queue.Full:
                log.warning("%s output is behind; dropped a reading", name)
        return entry

    if cfg.getboolean("web", "enabled", fallback=False):
        threading.Thread(target=run_web_server, args=(cfg, write_path, config_path), daemon=True).start()

    if cfg.getboolean("duckdns", "enabled", fallback=False):
        threading.Thread(target=secure_web.run_duckdns, args=(cfg,), daemon=True, name="duckdns").start()

    if cfg.getboolean("forecast", "enabled", fallback=False):
        threading.Thread(target=run_forecast, args=(cfg, write_path), daemon=True).start()

    # Records and normals: climate.json every hour (climate.py).
    def outdoor_days() -> list[dict]:
        if db_path is None or not db_path.exists():   # a new station: nothing recorded yet
            return []
        return [{"id": e["id"], "name": e["name"], "days": e["days"]}
                for e in summaries(db_path, sensors, {"days": "1000000"})["sensors"] if e["outdoor"]]
    where = station_settings(cfg)
    bases = (cfg.getfloat("climate", "heating_base_f", fallback=65.0),
             cfg.getfloat("climate", "cooling_base_f", fallback=65.0))
    threading.Thread(target=climate.run, args=(where.get("latitude"), where.get("longitude"), write_path,
                                               outdoor_days, cfg.getboolean("climate", "normals", fallback=True),
                                               bases), daemon=True, name="climate").start()

    source_threads = []
    for source in load_sources():
        if source.enabled(cfg):
            thread = threading.Thread(target=source.run, args=(cfg, emit), daemon=True, name=source.NAME)
            thread.start()
            source_threads.append(thread)
            log.info("Source on: %s", source.NAME)
    if not source_threads:
        # A new station has none until setup switches one on. Returning would end
        # the program, and the boot loader takes a clean exit for a restart after
        # saved settings: the setup page came up for a second at a time, forever.
        log.warning("No source switched on ([hub] or [capture] enabled); nothing to record. "
                    "The web server stays up for the setup page.")
        threading.Event().wait()
    for thread in source_threads:
        thread.join()
    # A source reading recordings ([capture] read) finishes; let the outputs drain.
    for _, todo in queues:
        while not todo.empty():
            time.sleep(0.2)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AcuRite weather sensor capture"
    )
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG,
        help=f"Path to config.ini (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--discover", action="store_true",
        help="With a radio dongle: print every sensor heard for --discover-time seconds and exit",
    )
    parser.add_argument(
        "--discover-time", type=int, default=300,
        metavar="SECONDS",
        help="How long to listen in discover mode (default: 300)",
    )
    parser.add_argument(
        "--import-raw", action="store_true",
        help="Load every reading in the raw hub logs into the database and exit (safe to re-run)",
    )
    parser.add_argument(
        "--import", dest="import_from", metavar="FILE|FOLDER|wu",
        help="Add older weather: a WeeWX .sdb, a CSV file, a Cumulus data folder or month log, "
             "or wu (Weather Underground); see importer.py",
    )
    parser.add_argument(
        "--noaa", metavar="YYYY-MM|YYYY",
        help="Print a month's or a year's NOAA-style climate summary (noaa.py); --sensor picks the sensor",
    )
    parser.add_argument("--sensor", help="With --import: the sensor id it belongs to (default: the first outdoor one); "
                        "with --noaa: the sensor to sum up")
    parser.add_argument("--metric", action="store_true", help="With --import of a CSV: °C, km/h, hPa and mm (of Cumulus without its Cumulus.ini: km/h wind)")
    parser.add_argument("--wu-station", help="With --import wu: the station ID (default: [weather_underground])")
    parser.add_argument("--wu-api-key", help="With --import wu: your Weather Underground API key")
    parser.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", help="With --import wu: the first day")
    parser.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="With --import wu: the last day (default: yesterday)")
    parser.add_argument(
        "--check", action="store_true",
        help="Alert if no reading has come in for [alerts] quiet_minutes, or that they're back; for a timer",
    )
    parser.add_argument("--test-alert", action="store_true", help="Send a test message every way [alerts] sets up")
    parser.add_argument("--diagnostics", action="store_true",
                        help="Write one file to attach to a problem report (no passwords, keys or location) and exit")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cfg = load_config(args.config)
    apply_timezone(cfg)

    if args.check or args.test_alert:
        iolog.keep()
    if args.check:
        print(alerts.check(cfg, Path(cfg.get("device", "write_path")).expanduser()))
        print(alerts.check_weather(cfg, Path(cfg.get("device", "write_path")).expanduser()))
        try:
            sensors = json.loads((Path(cfg.get("device", "write_path")).expanduser() / "current.json").read_text())["sensors"]
        except (OSError, ValueError, KeyError):
            sensors = []
        print(alerts.check_upkeep(cfg, upkeep.problems(diagnostics.BOOT_ROOT) + upkeep.low_batteries(sensors)))
    elif args.test_alert:
        problems = alerts.send(cfg, f"{page_title(cfg)}: test", "A test message from the weather station.")
        print("Sent." if not problems else "Not sent: " + "; ".join(problems))
        sys.exit(1 if problems else 0)
    elif args.diagnostics:
        path = diagnostics.bundle(cfg)
        print(f"Wrote {path}\nAttach it to an issue at https://github.com/akienm/CC0AC-Weather/issues "
              "with a line saying what looks wrong. Passwords, station keys, MACs, public IP addresses "
              "and your location are left out; open it first if you'd like to check.")
    elif args.import_raw:
        import_raw(cfg)
    elif args.import_from:
        import_history(cfg, args)
    elif args.noaa:
        try:
            print(noaa_summary(cfg, database_path(cfg), sensor_map(cfg), {"period": args.noaa, "sensor": args.sensor})["text"],
                  end="")
        except (ValueError, sqlite3.Error) as e:
            sys.exit(str(e))
    elif args.discover:
        from cc0ac.sources import rtl_433
        rtl_433.discover(cfg, args.discover_time)
    else:
        run_daemon(cfg, args.config.expanduser().resolve())


if __name__ == "__main__":
    main()
