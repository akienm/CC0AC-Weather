"""Checks for the weather-network and MQTT outputs: python3 -m unittest tests/test_networks.py

These need no network. On 2026-10-21 the live endpoints were checked by hand
(each refuses a wrong key in its own way), the CWOP packet was read back by
aprslib, and the MQTT announcements by Home Assistant 2026.10 on Mosquitto.
"""
import configparser
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from outputs import _station, cwop, mqtt, pwsweather, weathercloud, windy   # noqa: E402
from sources import Reading   # noqa: E402

WHEN = datetime(2026, 10, 22, 0, 0, tzinfo=timezone.utc)
FIELDS = {"temp_f": 70.0, "humidity_pct": 59, "wind_mph": 8.0, "wind_gust_mph": 11.0,
          "wind_dir_deg": 182, "pressure_inhg": 30.32, "rain_hour_in": 0.0, "rain_day_in": 0.0}


def cfg(**sections) -> configparser.ConfigParser:
    c = configparser.ConfigParser()
    for name, keys in sections.items():
        c[name] = keys
    return c


def reading(sid="00001234", source="acurite-access", type="Atlas", **fields) -> Reading:
    return Reading(source=source, sensor_id=sid, type=type,
                   received_utc="2026-10-22T00:00:00Z", raw="", fields=fields or dict(FIELDS))


class TheStation(unittest.TestCase):
    def test_first_outdoor_sensor_is_the_station_and_uploads_wait(self):
        s, c = _station.Station("windy", 5), cfg(windy={})
        self.assertIsNone(s.take(c, reading(type="tower")))
        self.assertIsNone(s.take(c, reading(source="online", type="outdoor")))
        fields, when = s.take(c, reading())
        self.assertEqual(fields["temp_f"], 70.0)
        self.assertEqual(when, WHEN)
        self.assertIsNone(s.take(c, reading()))                    # not due again yet
        self.assertIsNone(s.take(c, reading(sid="00009999")))      # another sensor
        with mock.patch.object(_station.time, "monotonic", return_value=s.last_upload + 301):
            self.assertIsNotNone(s.take(c, reading()))

    def test_named_sensor_wins(self):
        s, c = _station.Station("windy", 5), cfg(windy={"sensor": "00005678"})
        self.assertIsNone(s.take(c, reading()))
        self.assertIsNotNone(s.take(c, reading(sid="00005678")))

    def test_readings_merge(self):
        s, c = _station.Station("windy", 5), cfg(windy={})
        s.take(c, reading(temp_f=70.0))
        s.last_upload = None
        fields, _ = s.take(c, reading(humidity_pct=40))
        self.assertEqual((fields["temp_f"], fields["humidity_pct"]), (70.0, 40))

    def test_enabled_needs_its_keys(self):
        self.assertFalse(windy.enabled(cfg(windy={"enabled": "true", "station_id": "x"})))
        self.assertTrue(windy.enabled(cfg(windy={"enabled": "true", "station_id": "x", "password": "p"})))
        self.assertFalse(cwop.enabled(cfg(cwop={"enabled": "true", "station_id": "FW1"})))   # no position


class Formats(unittest.TestCase):
    def test_wu_format(self):
        p = _station.wu_format(FIELDS, WHEN)
        self.assertEqual(p["dateutc"], "2026-10-22 00:00:00")
        self.assertEqual(p["softwaretype"], _station.SOFTWARE)
        self.assertEqual(p["tempf"], "70.0")
        self.assertEqual(p["winddir"], "182")

    def test_pwsweather_adds_its_action(self):
        p = pwsweather.params(cfg(pwsweather={"station_id": "S", "api_key": "K"}), FIELDS, WHEN)
        self.assertEqual((p["ID"], p["PASSWORD"], p["action"]), ("S", "K", "updateraw"))

    def test_weathercloud_is_metric_times_ten(self):
        p = weathercloud.params(cfg(weathercloud={"weathercloud_id": "W", "key": "K"}), FIELDS, WHEN)
        self.assertEqual(p["temp"], "211")      # 21.1 °C
        self.assertEqual(p["wspd"], "36")       # 3.6 m/s
        self.assertEqual(p["bar"], "10268")     # 1026.75 hPa
        self.assertEqual((p["hum"], p["wdir"]), ("59", "182"))

    def test_cwop_packet(self):
        c = cfg(cwop={"station_id": "fw9999"}, station={"latitude": "40.0", "longitude": "-105.25"})
        self.assertEqual(cwop.packet(c, FIELDS, WHEN),
                         "FW9999>APRS,TCPIP*:@220000z4000.00N/10515.00W_182/008g011t070r000P000h59b10268"
                         + _station.SOFTWARE)

    def test_cwop_missing_cold_and_saturated(self):
        c = cfg(cwop={"station_id": "FW1"}, station={"latitude": "-1.5", "longitude": "2.25"})
        text = cwop.packet(c, {"temp_f": -4.6, "humidity_pct": 100, "wind_dir_deg": 0}, WHEN)
        self.assertIn("0130.00S/00215.00E_360/...g...t-05h00", text)


class HomeAssistant(unittest.TestCase):
    def setUp(self):
        mqtt._announced.clear()

    def configs(self, r, **keys):
        return {t: json.loads(p) for t, p in mqtt.messages(cfg(mqtt=keys, sensors={}), r) if t.endswith("/config")}

    def test_sensor_and_battery_announcements(self):
        out = self.configs(reading(temp_f=70.0, battery="low", wind_dir_deg=182))
        temp = out["homeassistant/sensor/cc0ac-weather-00001234/temp_f/config"]
        self.assertEqual((temp["unit_of_measurement"], temp["device_class"], temp["state_class"]),
                         ("°F", "temperature", "measurement"))
        self.assertEqual(temp["expire_after"], 3600)
        self.assertEqual(temp["device"]["manufacturer"], "AcuRite")
        battery = out["homeassistant/binary_sensor/cc0ac-weather-00001234/battery/config"]
        self.assertEqual((battery["device_class"], battery["payload_on"]), ("battery", "low"))
        self.assertEqual(out["homeassistant/sensor/cc0ac-weather-00001234/wind_dir_deg/config"]
                         ["state_class"], "measurement_angle")

    def test_stale_off_and_no_maker_for_others(self):
        out = self.configs(reading(sid="abc", source="rtl_433", temp_f=1.0), stale_after_minutes="0")
        temp = out["homeassistant/sensor/cc0ac-weather-abc/temp_f/config"]
        self.assertNotIn("expire_after", temp)
        self.assertNotIn("manufacturer", temp["device"])


if __name__ == "__main__":
    unittest.main()
