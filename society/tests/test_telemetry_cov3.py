"""test_telemetry_cov3.py — #64 telemetry emitter: off-by-default, var-0-firewalled, fail-safe."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOC = os.path.dirname(_HERE)
if _SOC not in sys.path:
    sys.path.insert(0, _SOC)

from utils import telemetry as tel  # noqa: E402


class TestTelemetry(unittest.TestCase):
    def setUp(self):
        self._orig_log = tel._LOCAL_LOG
        self._tmp = tempfile.mkdtemp()
        tel._LOCAL_LOG = os.path.join(self._tmp, "telemetry.jsonl")
        for k in ("TELEMETRY_ENABLED", "SLS_ENABLED"):
            os.environ.pop(k, None)

    def tearDown(self):
        tel._LOCAL_LOG = self._orig_log
        os.environ.pop("TELEMETRY_ENABLED", None)

    def test_off_by_default_is_noop(self):
        tel.emit_trip({"trip_id": "t1", "outcome": "success"})
        self.assertFalse(os.path.exists(tel._LOCAL_LOG), "no telemetry written when disabled")

    def test_enabled_writes_jsonl(self):
        os.environ["TELEMETRY_ENABLED"] = "1"
        tel.emit_trip({"trip_id": "t1", "outcome": "success", "total_cents": 150000})
        self.assertTrue(os.path.exists(tel._LOCAL_LOG))
        with open(tel._LOCAL_LOG) as fh:
            rec = json.loads(fh.readline())
        self.assertEqual(rec["trip_id"], "t1")
        self.assertEqual(rec["outcome"], "success")

    def test_emit_never_raises_on_bad_input(self):
        os.environ["TELEMETRY_ENABLED"] = "1"
        # an unserializable value must not raise (telemetry can't break a trip)
        tel.emit_trip({"trip_id": "t", "bad": object()})  # should swallow

    def test_build_trip_event_shape_and_var0(self):
        legs = [{"city": "bali", "lat": -8.4, "lon": 115.2}, {"city": "tokyo"}, "junk"]
        ev = tel.build_trip_event("trip-x", "success", legs, 150000)
        self.assertEqual(ev["trip_id"], "trip-x")
        self.assertEqual(ev["total_cents"], 150000)
        self.assertEqual([p["city"] for p in ev["points"]], ["bali", "tokyo"])
        self.assertIn("lat", ev["points"][0])
        self.assertNotIn("lat", ev["points"][1])  # tokyo has no coords -> omitted
        # var-0: deterministic
        ev2 = tel.build_trip_event("trip-x", "success", legs, 150000)
        self.assertEqual(json.dumps(ev, sort_keys=True), json.dumps(ev2, sort_keys=True))

    def test_build_trip_event_empty_legs(self):
        ev = tel.build_trip_event("t", "cannot_satisfy", None, None)
        self.assertEqual(ev["points"], [])
        self.assertEqual(ev["total_cents"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
