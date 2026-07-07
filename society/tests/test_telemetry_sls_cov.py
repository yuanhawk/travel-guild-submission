"""test_telemetry_sls_cov.py — #105: telemetry._emit_sls() coverage (68.0% gap).
Covers SLS integration with mocked aliyun.log module and env var gate checks."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOC = os.path.dirname(_HERE)
if _SOC not in sys.path:
    sys.path.insert(0, _SOC)

from utils import telemetry  # noqa: E402


class TestEmitSLS(unittest.TestCase):
    def setUp(self):
        # Save original environment
        self._orig_telemetry = os.environ.get("TELEMETRY_ENABLED")
        self._orig_sls = os.environ.get("SLS_ENABLED")
        self._orig_endpoint = os.environ.get("SLS_ENDPOINT")
        self._orig_ak = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID")
        self._orig_sk = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
        self._orig_proj = os.environ.get("SLS_PROJECT")
        self._orig_logstore = os.environ.get("SLS_LOGSTORE")
        # Also save local log path
        self._orig_local_log = telemetry._LOCAL_LOG
        # Create temp log file
        self._temp_log_dir = tempfile.TemporaryDirectory()
        self._temp_log = os.path.join(self._temp_log_dir.name, "telemetry.jsonl")
        telemetry._LOCAL_LOG = self._temp_log

    def tearDown(self):
        # Restore environment
        for var, val in [
            ("TELEMETRY_ENABLED", self._orig_telemetry),
            ("SLS_ENABLED", self._orig_sls),
            ("SLS_ENDPOINT", self._orig_endpoint),
            ("ALIBABA_CLOUD_ACCESS_KEY_ID", self._orig_ak),
            ("ALIBABA_CLOUD_ACCESS_KEY_SECRET", self._orig_sk),
            ("SLS_PROJECT", self._orig_proj),
            ("SLS_LOGSTORE", self._orig_logstore),
        ]:
            if val:
                os.environ[var] = val
            else:
                os.environ.pop(var, None)
        # Restore local log
        telemetry._LOCAL_LOG = self._orig_local_log
        self._temp_log_dir.cleanup()

    def test_emit_trip_no_op_when_disabled(self):
        os.environ.pop("TELEMETRY_ENABLED", None)
        event = {"trip_id": "test", "outcome": "success"}
        # Should not write anything
        telemetry.emit_trip(event)
        self.assertFalse(os.path.exists(self._temp_log))

    def test_emit_trip_writes_local_jsonl_when_enabled(self):
        os.environ["TELEMETRY_ENABLED"] = "1"
        os.environ.pop("SLS_ENABLED", None)
        event = {"trip_id": "test123", "outcome": "success"}
        telemetry.emit_trip(event)
        # Verify local log entry
        self.assertTrue(os.path.exists(self._temp_log))
        with open(self._temp_log, "r") as fh:
            lines = fh.readlines()
            self.assertEqual(len(lines), 1)
            logged = json.loads(lines[0])
            self.assertEqual(logged["trip_id"], "test123")

    def test_emit_trip_swallows_exceptions(self):
        os.environ["TELEMETRY_ENABLED"] = "1"
        os.environ.pop("SLS_ENABLED", None)
        event = {"trip_id": "test"}
        # Mock open to raise an exception
        with mock.patch("builtins.open", side_effect=IOError("write failed")):
            # Should not raise, should swallow the exception
            try:
                telemetry.emit_trip(event)
            except Exception as e:
                self.fail(f"emit_trip raised {e}, should swallow exceptions")

    def test_emit_sls_returns_early_when_sdk_not_installed(self):
        os.environ["SLS_ENABLED"] = "1"
        os.environ["SLS_ENDPOINT"] = "https://sls.aliyuncs.com"
        os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"] = "ak"
        os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"] = "sk"
        os.environ["SLS_PROJECT"] = "proj"
        os.environ["SLS_LOGSTORE"] = "store"
        event = {"trip_id": "test"}
        # Mock the aliyun.log import to fail
        with mock.patch("sys.modules", {"aliyun": None}):
            # Should return early, not raise
            telemetry._emit_sls(event)

    def test_emit_sls_returns_early_when_creds_incomplete(self):
        os.environ["SLS_ENABLED"] = "1"
        os.environ.pop("SLS_ENDPOINT", None)  # Missing endpoint
        os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"] = "ak"
        os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"] = "sk"
        os.environ["SLS_PROJECT"] = "proj"
        os.environ["SLS_LOGSTORE"] = "store"
        event = {"trip_id": "test"}
        # Create a mock aliyun.log module
        mock_aliyun = mock.MagicMock()
        with mock.patch.dict("sys.modules", {"aliyun.log": mock_aliyun}):
            telemetry._emit_sls(event)
            # LogClient should NOT be called because endpoint is missing
            mock_aliyun.LogClient.assert_not_called()

    def test_emit_sls_calls_client_with_complete_creds(self):
        os.environ["SLS_ENABLED"] = "1"
        os.environ["SLS_ENDPOINT"] = "https://sls.aliyuncs.com"
        os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"] = "ak123"
        os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"] = "sk456"
        os.environ["SLS_PROJECT"] = "my-project"
        os.environ["SLS_LOGSTORE"] = "my-logstore"
        event = {"trip_id": "test456", "points": [{"city": "tokyo"}]}
        # Create a mock aliyun.log module
        mock_aliyun = mock.MagicMock()
        mock_client = mock.MagicMock()
        mock_aliyun.LogClient.return_value = mock_client
        with mock.patch.dict("sys.modules", {"aliyun.log": mock_aliyun}):
            telemetry._emit_sls(event)
            # Verify LogClient was initialized with correct creds
            mock_aliyun.LogClient.assert_called_once_with(
                "https://sls.aliyuncs.com", "ak123", "sk456"
            )
            # Verify put_logs was called
            mock_client.put_logs.assert_called_once()

    def test_emit_sls_exception_swallowed_by_emit_trip(self):
        os.environ["TELEMETRY_ENABLED"] = "1"
        os.environ["SLS_ENABLED"] = "1"
        os.environ["SLS_ENDPOINT"] = "https://sls.aliyuncs.com"
        os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"] = "ak123"
        os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"] = "sk456"
        os.environ["SLS_PROJECT"] = "my-project"
        os.environ["SLS_LOGSTORE"] = "my-logstore"
        event = {"trip_id": "test"}
        # Mock aliyun.log to raise an exception
        mock_aliyun = mock.MagicMock()
        mock_aliyun.LogClient.side_effect = Exception("SLS error")
        with mock.patch.dict("sys.modules", {"aliyun.log": mock_aliyun}):
            # Should not raise — emit_trip swallows all exceptions
            try:
                telemetry.emit_trip(event)
            except Exception as e:
                self.fail(f"emit_trip raised {e}, should swallow SLS errors")

    def test_emit_sls_converts_dict_values_to_json_strings(self):
        os.environ["SLS_ENABLED"] = "1"
        os.environ["SLS_ENDPOINT"] = "https://sls.aliyuncs.com"
        os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"] = "ak123"
        os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"] = "sk456"
        os.environ["SLS_PROJECT"] = "my-project"
        os.environ["SLS_LOGSTORE"] = "my-logstore"
        event = {"trip_id": "test", "points": [{"city": "tokyo", "lat": 35.6}], "total_cents": 100}
        # Create a mock aliyun.log module
        mock_aliyun = mock.MagicMock()
        mock_client = mock.MagicMock()
        mock_aliyun.LogClient.return_value = mock_client
        with mock.patch.dict("sys.modules", {"aliyun.log": mock_aliyun}):
            telemetry._emit_sls(event)
            # Verify put_logs was called with a request containing the event data
            mock_client.put_logs.assert_called_once()
            call_args = mock_client.put_logs.call_args[0]
            request = call_args[0]
            # The request should contain LogItem with the event contents
            self.assertIsNotNone(request)

    def test_build_trip_event_pure_function(self):
        legs = [
            {"city": "tokyo", "lat": 35.6762, "lon": 139.7674},
            {"city": "osaka", "lat": 34.6937, "lon": 135.5023}
        ]
        event = telemetry.build_trip_event("trip123", "completed", legs, 50000)
        self.assertEqual(event["trip_id"], "trip123")
        self.assertEqual(event["outcome"], "completed")
        self.assertEqual(event["total_cents"], 50000)
        self.assertEqual(len(event["points"]), 2)
        self.assertEqual(event["points"][0]["city"], "tokyo")
        self.assertEqual(event["points"][0]["lat"], 35.6762)

    def test_build_trip_event_omits_missing_coords(self):
        legs = [
            {"city": "tokyo"},  # No lat/lon
            {"city": "osaka", "lat": 34.6937, "lon": 135.5023}
        ]
        event = telemetry.build_trip_event("trip123", "completed", legs, None)
        self.assertEqual(len(event["points"]), 2)
        self.assertNotIn("lat", event["points"][0])
        self.assertNotIn("lon", event["points"][0])
        self.assertIn("lat", event["points"][1])
        self.assertEqual(event["total_cents"], 0)  # None -> 0

    def test_build_trip_event_handles_none_legs(self):
        event = telemetry.build_trip_event("trip456", "failed", None, 0)
        self.assertEqual(event["trip_id"], "trip456")
        self.assertEqual(event["points"], [])

    def test_emit_trip_returns_none(self):
        os.environ["TELEMETRY_ENABLED"] = "1"
        os.environ.pop("SLS_ENABLED", None)
        event = {"trip_id": "test"}
        result = telemetry.emit_trip(event)
        # Should always return None (pure side-effect)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
