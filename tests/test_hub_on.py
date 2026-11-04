"""The hub is on unless something else records: new installs, older ones, a busy port.
python3 -m unittest tests/test_hub_on.py"""
import importlib.util
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from cc0ac import wizard   # noqa: E402
from cc0ac.config import load_config   # noqa: E402
from cc0ac.sources import acurite_access   # noqa: E402

spec = importlib.util.spec_from_file_location("hub_on", HERE / "updates/migrations/0006_hub_on.py")
hub_on = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hub_on)


class OlderInstalls(unittest.TestCase):
    def run_on(self, hub: str, capture: str) -> str:
        with tempfile.TemporaryDirectory() as d:
            config = Path(d) / "config.ini"
            text = (HERE / "config.ini.example").read_text()
            config.write_text(wizard.set_values(wizard.set_values(text, "hub", {"enabled": hub}),
                                                "capture", {"enabled": capture}))
            hub_on.apply(SimpleNamespace(config=config, version_dir=HERE, log=logging.getLogger("t")))
            cfg = load_config(config)
            self.assertIn("; AcuRite Access 09155M hub relay", config.read_text())   # comments kept
            return cfg.get("hub", "enabled")

    def test_a_station_recording_nothing_gets_the_hub(self):
        self.assertEqual(self.run_on("false", "false"), "true")

    def test_a_station_on_the_radio_is_left_alone(self):
        self.assertEqual(self.run_on("false", "true"), "false")

    def test_a_hub_already_on_is_untouched(self):
        self.assertEqual(self.run_on("true", "false"), "true")

    def test_no_config_yet_is_fine(self):
        hub_on.apply(SimpleNamespace(config=Path("/nonexistent/config.ini"), version_dir=HERE,
                                     log=logging.getLogger("t")))


class BusyPort(unittest.TestCase):
    def test_a_busy_port_is_tried_again_not_given_up(self):
        tries, slept = [], []

        class Server:
            def __init__(self, addr, handler):
                tries.append(addr)
                if len(tries) == 1:
                    raise PermissionError("not allowed")
                if len(tries) == 2:
                    raise OSError(98, "Address already in use")
            def serve_forever(self):
                pass

        real = acurite_access._HubServer, acurite_access.time.sleep, acurite_access._hub_tls_context
        acurite_access._HubServer = Server
        acurite_access.time.sleep = slept.append
        acurite_access._hub_tls_context = lambda cfg: None
        try:
            cfg = load_config_text(wizard.set_values((HERE / "config.ini.example").read_text(), "hub", {"port": "8443"}))
            with self.assertLogs("", level="ERROR") as logs:
                acurite_access.run(cfg, lambda reading: None)
        finally:
            acurite_access._HubServer, acurite_access.time.sleep, acurite_access._hub_tls_context = real
        self.assertEqual(len(tries), 3)
        self.assertEqual(slept, [acurite_access.BIND_RETRY_S] * 2)
        self.assertIn("something else using it", "\n".join(logs.output))


def load_config_text(text: str):
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "config.ini").write_text(text)
        return load_config(Path(d) / "config.ini")


if __name__ == "__main__":
    unittest.main()
