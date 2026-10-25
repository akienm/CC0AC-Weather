"""Checks for other brands' stations: python3 -m unittest tests/test_brands.py

The radio checks run decode() over rtl_433_samples.jsonl: one recorded rtl_433
output line per weather model, taken from the rtl_433 project's own test
recordings (https://github.com/merbanan/rtl_433_tests; each line names the file
it came from). They prove our reading of rtl_433's fields, not rtl_433's decoding.

The console checks send uploads shaped like each format's documentation to a
live listener. No real Ambient, Ecowitt or Wunderground console was used.
"""
import configparser
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import diagnostics                       # noqa: E402
from sources import FIELDS, OUTDOOR_TYPES, console_upload, rtl_433   # noqa: E402

HERE = Path(__file__).resolve().parent
SAMPLES = [json.loads(line) for line in (HERE / "rtl_433_samples.jsonl").read_text().splitlines()]


class RadioBrands(unittest.TestCase):
    def test_every_sample_decodes_into_known_fields(self):
        for s in SAMPLES:
            with self.subTest(model=s["line"]["model"], source=s["from"]):
                sensor_id, kind, fields, rain = rtl_433.decode(s["line"])
                self.assertTrue(sensor_id)
                self.assertEqual(sensor_id, sensor_id.lower() if not sensor_id.isdigit() else sensor_id)
                self.assertTrue(fields or rain is not None)
                self.assertTrue(set(fields) <= set(FIELDS))

    def test_units_are_converted(self):
        for s in SAMPLES:
            raw = s["line"]
            _, _, fields, rain = rtl_433.decode(raw)
            with self.subTest(model=raw["model"]):
                if "temperature_F" not in raw and isinstance(raw.get("temperature_C"), (int, float)):
                    self.assertAlmostEqual(fields["temp_f"], raw["temperature_C"] * 9 / 5 + 32, delta=0.06)
                if isinstance(raw.get("wind_avg_m_s"), (int, float)):
                    self.assertAlmostEqual(fields["wind_mph"], raw["wind_avg_m_s"] * 2.236936, delta=0.06)
                if isinstance(raw.get("wind_avg_km_h"), (int, float)):
                    self.assertAlmostEqual(fields["wind_mph"], raw["wind_avg_km_h"] * 0.621371, delta=0.06)
                if isinstance(raw.get("rain_mm"), (int, float)):
                    self.assertAlmostEqual(rain, raw["rain_mm"] / 25.4, delta=0.0001)
                if "uvi" in raw:
                    self.assertEqual(fields["uv_index"], raw["uvi"])
                if isinstance(raw.get("pressure_hPa"), (int, float)):
                    self.assertAlmostEqual(fields["pressure_inhg"], raw["pressure_hPa"] * 0.02953, delta=0.006)

    def test_wind_or_rain_means_an_outdoor_card(self):
        for s in SAMPLES:
            raw = s["line"]
            _, kind, fields, rain = rtl_433.decode(raw)
            if raw["model"].lower().startswith("acurite"):
                continue
            with self.subTest(model=raw["model"]):
                outdoorish = rain is not None or any(k in fields for k in rtl_433.OUTDOOR_FIELDS)
                self.assertEqual(kind == "outdoor", outdoorish)
                if not outdoorish:
                    self.assertEqual(kind, raw["model"])
        self.assertIn("outdoor", OUTDOOR_TYPES)

    def test_ambient_ecowitt_outdoor_arrays_are_outdoor(self):
        models = {s["line"]["model"]: s["line"] for s in SAMPLES}
        for m in ("Fineoffset-WH65B", "Fineoffset-WH24", "Fineoffset-WS90", "Bresser-5in1", "LaCrosse-WS2310"):
            with self.subTest(model=m):
                self.assertEqual(rtl_433.decode(models[m])[1], "outdoor")

    def test_acurite_keeps_hub_ids_and_names(self):
        atlas = next(s["line"] for s in SAMPLES if s["line"]["model"] == "Acurite-Atlas")
        sensor_id, kind, _, _ = rtl_433.decode(atlas)
        self.assertEqual((len(sensor_id), sensor_id.isdigit(), kind), (8, True, "Atlas"))

    def test_command_frequency_hop_and_all_protocols(self):
        cfg = configparser.ConfigParser()
        cfg.read_string("[capture]\nprotocols = all\nfrequency = 433.92M, 915M\nhop_seconds = 20\n")
        cmd = rtl_433.command(cfg)
        self.assertNotIn("-R", cmd)
        self.assertEqual(cmd[cmd.index("-H") + 1], "20")
        self.assertEqual([cmd[i + 1] for i, c in enumerate(cmd) if c == "-f"], ["433.92M", "915M"])
        cfg.read_string("[capture]\nprotocols = 78, 113\nfrequency = 915M\n")
        cmd = rtl_433.command(cfg)
        self.assertNotIn("-H", cmd)
        self.assertEqual([cmd[i + 1] for i, c in enumerate(cmd) if c == "-R"], ["78", "113"])

    def test_raw_log_keeps_every_line_and_prunes(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            for day in range(1, 20):
                (folder / f"2020-01-{day:02d}.log").write_text("old\n")
            log = rtl_433.RawLog(folder)
            lines = [json.dumps(SAMPLES[0]["line"]), "not json"]
            list(rtl_433.readings(lines, "test", rtl_433.RainCounter(None), raw_log=log))
            files = sorted(folder.glob("*.log"))
            self.assertEqual(len(files), rtl_433.RAW_KEEP_DAYS + 1)
            self.assertEqual(files[-1].read_text().splitlines(), lines)


# Shaped after each format's documentation. Values are made up.
ECOWITT_POST = ("PASSKEY=ABCDEF0123456789ABCDEF0123456789&stationtype=GW2000A_V3.1.0&runtime=5"
                "&dateutc=2026-10-16+18:04:00&tempinf=71.6&humidityin=40&baromrelin=30.012&baromabsin=24.123"
                "&tempf=55.4&humidity=62&winddir=270&windspeedmph=4.5&windgustmph=8.1&maxdailygust=12.3"
                "&solarradiation=100.00&uv=2&rainratein=0.000&eventrainin=0.000&hourlyrainin=0.010"
                "&dailyrainin=0.120&weeklyrainin=0.5&monthlyrainin=1.0&yearlyrainin=9.0&totalrainin=9.0"
                "&temp1f=68.0&humidity1=45&batt1=0&wh65batt=1&lightning=8&lightning_num=3"
                "&soilmoisture1=31&freq=915M&model=GW2000A")
AMBIENT_GET = ("/data/report/?&MAC=AA:BB:CC:DD:EE:FF&stationtype=AMBWeatherV4.3.4&dateutc=2026-10-16+18:04:00"
               "&tempinf=70.1&humidityin=38&baromrelin=29.95&baromabsin=24.05&tempf=54.0&battout=1"
               "&humidity=60&winddir=180&windspeedmph=3.1&windgustmph=5.8&maxdailygust=9.2"
               "&hourlyrainin=0.00&dailyrainin=0.05&solarradiation=210.5&uv=3&batt_co2=1")
WU_GET = ("/weatherstation/updateweatherstation.php?ID=KXXTEST1&PASSWORD=secretkey&dateutc=now"
          "&tempf=50.2&humidity=70&dewptf=40.9&winddir=90&windspeedmph=2&windgustmph=4&rainin=0.01"
          "&dailyrainin=0.2&baromin=30.10&UV=1&indoortempf=69.5&indoorhumidity=41&action=updateraw")


class ConsoleUploads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.got = []
        cfg = configparser.ConfigParser()
        cfg.read_dict({"console": {"enabled": "true", "raw_dir": self.tmp.name}})
        self.server = console_upload._Server(("127.0.0.1", 0), console_upload._make_handler(
            cfg, lambda r: self.got.append(r) or {"type": r.type, "name": r.sensor_id, "fields": r.fields}))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def send(self, path, body=None):
        req = urllib.request.Request(self.base + path, data=body.encode() if body else None,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"} if body else {})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()

    def by_id(self):
        return {r.sensor_id: r for r in self.got}

    def test_ecowitt_post(self):
        self.assertEqual(self.send("/data/report/", ECOWITT_POST), (200, b"success\n"))
        got = self.by_id()
        self.assertEqual(set(got), {"console-outdoor", "console-indoor", "console-ch1"})
        out = got["console-outdoor"]
        self.assertEqual(out.type, "outdoor")
        self.assertEqual((out.fields["temp_f"], out.fields["wind_mph"], out.fields["rain_day_in"]), (55.4, 4.5, 0.12))
        self.assertEqual((out.fields["pressure_inhg"], out.fields["rain_hour_in"]), (30.012, 0.01))
        self.assertEqual(out.fields["battery"], "low")          # Ecowitt: 1 is low
        self.assertEqual(got["console-ch1"].fields["battery"], "normal")
        self.assertEqual(out.fields["last_strike_mi"], 5.0)      # 8 km
        self.assertEqual(out.fields["light_lux"], 12670)
        self.assertEqual(out.sensor_utc, "2026-10-16T18:04:00Z")
        self.assertIn("dew_point_f", out.fields)                 # derived
        self.assertEqual(got["console-indoor"].fields["temp_f"], 71.6)

    def test_ambient_get(self):
        self.send(AMBIENT_GET)
        got = self.by_id()
        self.assertEqual(set(got), {"console-outdoor", "console-indoor"})
        self.assertEqual(got["console-outdoor"].fields["battery"], "normal")   # Ambient: 1 is good
        self.assertEqual(got["console-outdoor"].receiver, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(got["console-outdoor"].fields["wind_dir_deg"], 180)

    def test_wunderground_get_and_password_never_logged(self):
        self.send(WU_GET)
        got = self.by_id()
        self.assertEqual(got["console-outdoor"].fields["pressure_inhg"], 30.10)
        self.assertEqual(got["console-outdoor"].fields["uv_index"], 1)
        self.assertEqual(got["console-indoor"].fields["humidity_pct"], 41)
        self.assertIsNone(got["console-outdoor"].sensor_utc)
        raw = "".join(p.read_text() for p in Path(self.tmp.name).glob("*.log"))
        self.assertIn("PASSWORD=%2A%2A%2A%2A", raw)
        self.assertNotIn("secretkey", raw + "".join(r.raw for r in self.got))

    def test_unread_fields_are_named(self):
        with self.assertLogs("acurite", "INFO") as logs:
            self.send("/data/report/", ECOWITT_POST)
        said = "\n".join(logs.output)
        self.assertIn("soilmoisture1", said)
        self.assertNotIn("tempf", said.split("not read")[1])

    def test_other_station_not_recorded(self):
        self.server.RequestHandlerClass = console_upload._make_handler(
            _cfg(station="KNOTME", raw_dir=self.tmp.name), lambda r: self.got.append(r))
        self.send(WU_GET)
        self.assertEqual(self.got, [])


def _cfg(**console):
    cfg = configparser.ConfigParser()
    cfg.read_dict({"console": {"enabled": "true", **console}})
    return cfg


class Diagnostics(unittest.TestCase):
    def test_scrub(self):
        line = "GET /x?ID=KXX&PASSWORD=pw&PASSKEY=ABC&MAC=AA:BB&tempf=50 from 8.8.8.8 and 10.0.0.5"
        out = diagnostics.scrub(line)
        for secret in ("KXX", "pw", "ABC", "AA:BB", "8.8.8.8"):
            self.assertNotIn(secret, out)
        self.assertIn("tempf=50", out)
        self.assertIn("10.0.0.5", out)

    def test_sensor_names_scrubbed(self):
        out = diagnostics.scrub("HUB tower Back porch {'temp_f': 70}", ["Back porch", ""])
        self.assertNotIn("Back porch", out)
        self.assertIn("tower", out)

    def test_settings_leave_out_names_and_location(self):
        cfg = configparser.ConfigParser()
        cfg.read_string("[sensors]\n00004321 = Back porch\n[forecast]\nlatitude = 1.23\n"
                        "[console]\nport = 12346\nstation = AA:BB\n[hub]\nport = 443\nhub_id = 24C8\n")
        out = diagnostics.settings(cfg)
        for secret in ("Back porch", "1.23", "AA:BB", "24C8"):
            self.assertNotIn(secret, out)
        self.assertIn("00004321", out)
        self.assertIn("port = 12346", out)


if __name__ == "__main__":
    unittest.main()
