"""Offline smoke tests (no network, no Telegram):  python3 -m unittest discover tests"""
import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["VR_CONFIG"] = os.path.join(ROOT, "config.example.json")
os.environ["VR_NO_PUSH"] = "1"

import vrcommon as vc  # noqa: E402
import pricealert  # noqa: E402


class DSL(unittest.TestCase):
    def test_conditions(self):
        m = {"vr": 6, "chgN": 0.1, "green": True, "x": 2, "y": 3}
        self.assertTrue(vc.all_true([["vr", ">=", 5], ["green", "==", True]], m))
        self.assertFalse(vc.all_true([["vr", ">=", 7]], m))
        self.assertTrue(vc.all_true([["x", "<", "$y"]], m))
        self.assertFalse(vc.all_true([["missing", ">", 0]], m))
        self.assertTrue(vc.all_true([{"any": [["vr", ">", 100], ["chgN", ">", 0.05]]}], m))


class Tracker(unittest.TestCase):
    def test_crossing_and_rearm(self):
        with tempfile.TemporaryDirectory() as d:
            t = pricealert.Tracker(os.path.join(d, "s"), band=0.01, cooldown=0)
            a = {"key": "BTCUSDT>=100", "symbol": "BTCUSDT", "op": ">=", "price": 100, "note": "", "repeat": True}
            self.assertFalse(t.update(a, 99))      # arm
            self.assertTrue(t.update(a, 101))      # cross
            self.assertFalse(t.update(a, 102))     # still above, not re-armed
            self.assertFalse(t.update(a, 98))      # moved away -> re-arm
            self.assertTrue(t.update(a, 100.5))    # cross again


class RadarOnce(unittest.TestCase):
    def test_once_against_mock(self):
        port = "8799"
        srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "tests", "mock_binance.py"), port])
        try:
            time.sleep(0.8)
            with tempfile.TemporaryDirectory() as d:
                env = dict(os.environ, VR_FAPI_BASE="http://127.0.0.1:" + port, VR_HEARTBEAT_DIR=d)
                out = subprocess.run([sys.executable, os.path.join(ROOT, "radar.py"), "--once", "--no-push",
                                      "--log", os.path.join(d, "s.csv")], env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertIn("ETHUSDT", out.stdout)
                self.assertIn("one pass done: 2/2", out.stdout)
                self.assertTrue(os.path.exists(os.path.join(d, "radar.json")))
        finally:
            srv.kill(); srv.wait()


if __name__ == "__main__":
    unittest.main()
