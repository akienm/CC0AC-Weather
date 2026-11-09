"""1.4.7: [<sensor id>] disposition (sensor_config.py): what each sensor's readings are used for.

    all          shown on the page, recorded, sent out (the default)
    hide         recorded and sent out, not on the page
    record_only  recorded, not on the page, not sent out
    ignore       not recorded, not on the page, not sent out

and the diagnostics log on Windows and macOS (the boot loader's station.log).
python3 -m unittest tests/test_1_4_7.py"""

import configparser
import json
import signal
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from cc0ac import capture, config, recorder, sensor_config, store, wizard   # noqa: E402
from cc0ac.sources import Reading                                          # noqa: E402

SECTIONS = ("[00000001]\ntitle = Yard\n\n"
            "[00000002]\ntitle = Spare\ndisposition = hide\n\n"
            "[00000003]\ntitle = Garage\ndisposition = record_only\n\n"
            "[00000004]\ntitle = Next door\ndisposition = ignore\n")


def read(text):
    cfg = configparser.ConfigParser()
    cfg.read_string(text)
    return cfg


class TheSetting(unittest.TestCase):
    def test_each_sensor_has_one_and_all_is_the_default(self):
        cfg = read(SECTIONS)
        self.assertEqual([sensor_config.disposition(cfg, s) for s in ("00000001", "00000002", "00000003", "00000004")],
                         ["all", "hide", "record_only", "ignore"])
        self.assertEqual(sensor_config.disposition(cfg, "00009999"), "all")     # not in the file at all
        self.assertEqual(sensor_config.hidden(cfg), {"00000002", "00000003", "00000004"})

    def test_a_value_it_doesnt_know_is_all_with_a_warning(self):
        with self.assertLogs("acurite", "WARNING") as logs:
            self.assertEqual(sensor_config.disposition(read("[00000001]\ndisposition = nope\n"), "00000001"), "all")
        self.assertIn("nope", logs.output[0])

    def test_enabled_false_from_before_reads_as_ignore(self):
        self.assertEqual(sensor_config.disposition(read("[00000001]\nenabled = false\n"), "00000001"), "ignore")

    def test_the_names_keep_every_sensor_and_carry_the_hidden_ones(self):
        names = config.sensor_map(read(SECTIONS))
        self.assertEqual(list(names), ["00000001", "00000002", "00000003", "00000004"])
        self.assertEqual(names.hidden, {"00000002", "00000003", "00000004"})
        self.assertEqual(sensor_config.cards(read(SECTIONS + "\n[00000005]\nicon = sky\ndisposition = hide\n")), {})


class WhereReadingsGo(unittest.TestCase):
    """run_daemon with a made-up source that sends one reading from each sensor, and a
    made-up output that keeps what it is sent."""

    def test_recorded_sent_and_shown_follow_the_disposition(self):
        sent = []
        source = types.SimpleNamespace(NAME="fake", enabled=lambda cfg: True,
                                       run=lambda cfg, emit: [emit(Reading(
                                           source="acurite-access", sensor_id=f"0000000{n}", type="tower",
                                           received_utc="2026-11-09T12:00:0%dZ" % n, raw=f"q{n}",
                                           fields={"temp_f": 60.0 + n})) for n in (1, 2, 3, 4)])
        output = types.SimpleNamespace(NAME="fake", enabled=lambda cfg: True,
                                       send=lambda cfg, reading: sent.append(reading.sensor_id))
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path = root / "config.ini"
            path.write_text(f"[device]\nwrite_path = {root}\n\n{SECTIONS}")
            cfg = config.load_config(path)
            before = signal.getsignal(signal.SIGTERM)
            try:
                with mock.patch.object(capture, "load_sources", return_value=[source]), \
                     mock.patch.object(capture, "load_outputs", return_value=[output]):
                    capture.run_daemon(cfg, path)
                time.sleep(0.5)       # the output thread takes its last reading off the queue
            finally:
                signal.signal(signal.SIGTERM, before)
            con = sqlite3.connect(root / "weather.db")
            recorded = sorted(r[0] for r in con.execute("SELECT DISTINCT sensor_id FROM readings"))
            con.close()
            shown = [s["sensor_id"] for s in json.loads((root / "current.json").read_text())["sensors"]]
            text = path.read_text()
        self.assertEqual(recorded, ["00000001", "00000002", "00000003"])
        self.assertEqual(sorted(sent), ["00000001", "00000002"])
        self.assertEqual(shown, ["00000001"])
        self.assertEqual(text.count("[00000002]") + text.count("[00000003]") + text.count("[00000004]"), 3,
                         "a known sensor isn't added to config.ini again")


class ThePagesLeaveThemOut(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "weather.db"
        con = store.open_db(self.db)
        for n, (sid, kind, temp) in enumerate([("00000001", "Atlas", 50.0), ("00000002", "Atlas", 90.0),
                                               ("00000003", "tower", 70.0)]):
            con.execute("INSERT INTO readings (received_utc, sensor_id, type, query, temp_f) VALUES (?, ?, ?, ?, ?)",
                        [store.epoch_to_iso(time.time() - 600 + n), sid, kind, f"q{n}", temp])
        con.commit()
        con.close()
        self.names = config.sensor_map(read("[00000001]\ntitle = Yard\n\n[00000002]\ndisposition = hide\n\n"
                                            "[00000003]\ntitle = Garage\ndisposition = record_only\n"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_charts(self):
        h = store.history(self.db, self.names, {"period": "today"})
        self.assertEqual(h["rooms"], [])
        temps = [v for v in h["outdoor"]["temp_f"] if v is not None]
        self.assertEqual(temps, [50.0], "a hidden outdoor sensor is not averaged into the outdoor chart")

    def test_summaries_and_noaa(self):
        self.assertEqual([s["id"] for s in store.summaries(self.db, self.names, {})["sensors"]], ["00000001"])
        cfg = read("[station]\nname = Test\n")
        self.assertEqual([c["id"] for c in store.noaa_summary(cfg, self.db, self.names, {})["sensors"]], ["00000001"])

    def test_the_database_keeps_them(self):
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT count(*) FROM readings").fetchone()[0], 3)
        con.close()
        everything = config.sensor_map(read("[00000001]\ntitle = Yard\n"))
        self.assertEqual(len(store.summaries(self.db, everything, {})["sensors"]), 3)


class SnapshotAtStart(unittest.TestCase):
    def test_a_sensor_hidden_since_the_last_snapshot_is_written_out_of_it(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            (path / "current.json").write_text(json.dumps({"written": "now", "sensors": [
                {"sensor_id": "00000002", "fields": {"temp_f": 70}}, {"sensor_id": "00000001", "fields": {"temp_f": 71}}]}))
            recorder.Recorder().refresh_snapshot(path, config.sensor_map(read(SECTIONS)), None)
            self.assertEqual([s["sensor_id"] for s in json.loads((path / "current.json").read_text())["sensors"]],
                             ["00000001"])
            (path / "current.json").write_text(json.dumps({"sensors": [{"sensor_id": "00000002", "fields": {}}]}))
            recorder.Recorder().refresh_snapshot(path, config.sensor_map(read(SECTIONS)), None)
            self.assertEqual(json.loads((path / "current.json").read_text())["sensors"], [])


class TheSetupPage(unittest.TestCase):
    TEXT = "[00000001]\ntitle = Yard\n\n[00000002]\ntitle = Kitchen\nenabled = false\n"

    def test_saved_and_read_back_and_all_is_not_written(self):
        out = wizard.write_sensors(self.TEXT, {"00000001": "Yard", "00000002": "Kitchen"},
                                   {"00000001": "all", "00000002": "record_only"})
        cfg = read(out)
        self.assertFalse(cfg.has_option("00000001", "disposition"))
        self.assertEqual(cfg.get("00000002", "disposition"), "record_only")
        self.assertFalse(cfg.has_option("00000002", "enabled"), "the older enabled = false is replaced")
        back = wizard.write_sensors(out, {"00000001": "Yard", "00000002": "Kitchen"})
        self.assertFalse(read(back).has_option("00000002", "disposition"))

    def test_a_disposition_it_doesnt_know_is_refused(self):
        with self.assertRaises(ValueError):
            wizard.apply_setup("", {"sensors": [{"id": "00000001", "name": "Yard", "disposition": "sometimes"}]})


class DiagnosticsLog(unittest.TestCase):
    def test_the_boot_loaders_station_log_comes_first_then_docker(self):
        from cc0ac import diagnostics
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "station.log").write_text("started\n")
            with mock.patch.object(diagnostics, "BOOT_ROOT", Path(d)), \
                 mock.patch.object(diagnostics.os.path, "exists", side_effect=lambda p: True):
                self.assertEqual(diagnostics.service_log(), "started\n")
            with mock.patch.object(diagnostics, "BOOT_ROOT", Path(d) / "none"), \
                 mock.patch.object(diagnostics.os.path, "exists", side_effect=lambda p: p == "/.dockerenv"):
                self.assertIn("docker compose logs cc0ac-weather", diagnostics.service_log())


if __name__ == "__main__":
    unittest.main()
