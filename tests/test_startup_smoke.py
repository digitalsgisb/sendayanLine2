import os
import runpy
import sys
import tempfile
import types
import unittest
from unittest import mock


SYSTEM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_PATH = os.path.join(SYSTEM_DIR, "sdy2.py")


class FakeMQTTClient:
    def __init__(self, *_args, **_kwargs):
        self.on_connect = None
        self.on_message = None

    def subscribe(self, *_args, **_kwargs): pass
    def publish(self, *_args, **_kwargs): return None
    def will_set(self, *_args, **_kwargs): pass
    def connect(self, *_args, **_kwargs): pass
    def loop_start(self): pass
    def loop_stop(self): pass
    def disconnect(self): pass


class StartupSmokeTests(unittest.TestCase):
    def test_tracker_initializes_and_shuts_down_cleanly(self):
        gpio = types.ModuleType("RPi.GPIO")
        gpio.BCM = gpio.IN = gpio.PUD_UP = 1
        gpio.setmode = lambda *_args, **_kwargs: None
        gpio.setup = lambda *_args, **_kwargs: None
        gpio.input = lambda *_args, **_kwargs: 0
        gpio.cleanup = lambda: None
        rpi = types.ModuleType("RPi")
        rpi.GPIO = gpio

        mqtt_client_module = types.ModuleType("paho.mqtt.client")
        mqtt_client_module.Client = FakeMQTTClient
        mqtt_module = types.ModuleType("paho.mqtt")
        mqtt_module.client = mqtt_client_module
        paho = types.ModuleType("paho")
        paho.mqtt = mqtt_module

        requests_module = types.ModuleType("requests")
        requests_module.post = lambda *_args, **_kwargs: None

        modules = {
            "RPi": rpi,
            "RPi.GPIO": gpio,
            "paho": paho,
            "paho.mqtt": mqtt_module,
            "paho.mqtt.client": mqtt_client_module,
            "requests": requests_module,
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = os.path.join(temporary_directory, "sdy2.db")
            with mock.patch.dict(sys.modules, modules), mock.patch.dict(
                os.environ,
                {"SDY2_SQLITE_PATH": database_path, "SDY2_API_ENABLED": "false"},
            ), mock.patch("time.sleep", side_effect=KeyboardInterrupt):
                runpy.run_path(SCRIPT_PATH, run_name="__main__")
            self.assertTrue(os.path.exists(database_path))


if __name__ == "__main__":
    unittest.main()
