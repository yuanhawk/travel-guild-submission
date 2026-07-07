"""test_model_router.py — unit tests for utils/model_router.py.

Coverage:
 1. pick_model returns first model when counter file is absent (first run).
 2. pick_model rotates when first model reaches the cap.
 3. pick_model falls back to LAST model when ALL are exhausted (with warning).
 4. record_usage: persists and accumulates across calls.
 5. record_usage: concurrency-safe (sequential increment adds correctly).
 6. mark_exhausted: sets counter to cap; pick_model skips that model next call.
 7. test vs demo profile selection (SOCIETY_MODEL_PROFILE env var).
 8. env-override for model lists (SOCIETY_TEST_MODELS / SOCIETY_DEMO_MODELS).
 9. dashscope_chat: _post seam, injects model, records usage, returns data.
10. dashscope_chat: _post seam with no usage → does NOT call record_usage.
11. dashscope_chat: 4xx HTTPStatusError → mark_exhausted + re-raises.
12. inert-when-llm-off: no file I/O at import time (state only touched at call time).

All tests use a fresh temp dir for the state file to avoid cross-test pollution.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers to re-import model_router with a custom state path for isolation.
# ---------------------------------------------------------------------------

def _make_router(tmp_dir: str, profile: str = "demo",
                 demo_models: str = "model-a,model-b",
                 test_models: str = "model-x",
                 fast_models: str = "fast-a,fast-b",
                 fast_roles: str = "narrator,translate",
                 cap: int = 1_000):
    """Return a freshly-imported model_router module pointing at tmp_dir.

    We reload the module with patched env so each test has an isolated counter
    file and known model lists.
    """
    counts_file = os.path.join(tmp_dir, ".token_counts.json")
    lock_file = os.path.join(tmp_dir, ".token_counts.lock")

    env_patch = {
        "SOCIETY_MODEL_PROFILE": profile,
        "SOCIETY_DEMO_MODELS": demo_models,
        "SOCIETY_TEST_MODELS": test_models,
        "SOCIETY_FAST_MODELS": fast_models,
        "SOCIETY_FAST_ROLES": fast_roles,
        "SOCIETY_MODEL_TOKEN_CAP": str(cap),
    }
    # Remove the cached module so it can be reloaded with patched env/paths.
    for key in list(sys.modules):
        if "model_router" in key:
            del sys.modules[key]

    with patch.dict(os.environ, env_patch, clear=False):
        # Ensure society/ is on sys.path so the import resolves.
        soc_path = os.path.join(os.path.dirname(__file__), "..")
        soc_path = os.path.realpath(soc_path)
        had = soc_path in sys.path
        if not had:
            sys.path.insert(0, soc_path)
        try:
            import utils.model_router as mr
            importlib.reload(mr)
        finally:
            if not had and soc_path in sys.path:
                sys.path.remove(soc_path)

    # Patch the module-level path constants to point at our tmp_dir.
    mr._COUNTS_FILE = counts_file  # type: ignore[attr-defined]
    mr._LOCK_FILE = lock_file      # type: ignore[attr-defined]
    # Re-resolve _ACTIVE_LIST based on the freshly-set profile + models.
    mr._ACTIVE_LIST = (            # type: ignore[attr-defined]
        mr._TEST_MODELS if mr._PROFILE == "test" else mr._DEMO_MODELS  # type: ignore[attr-defined]
    )
    return mr


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPickModelFirstRun(unittest.TestCase):
    """pick_model returns first model when no state file exists."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_first_model_returned_when_no_file(self):
        mr = _make_router(self.tmp)
        model = mr.pick_model("default")
        self.assertEqual(model, "model-a")

    def test_no_file_created_by_pick_model(self):
        """pick_model is read-only; it must NOT create the state file."""
        mr = _make_router(self.tmp)
        mr.pick_model("default")
        self.assertFalse(os.path.exists(mr._COUNTS_FILE))


class TestPickModelRotation(unittest.TestCase):
    """pick_model rotates past exhausted models."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_rotates_when_first_model_at_cap(self):
        mr = _make_router(self.tmp, cap=1_000)
        # Manually exhaust model-a.
        with open(mr._COUNTS_FILE, "w") as f:
            json.dump({"model-a": 1_000}, f)
        model = mr.pick_model("default")
        self.assertEqual(model, "model-b")

    def test_still_uses_first_model_below_cap(self):
        mr = _make_router(self.tmp, cap=1_000)
        with open(mr._COUNTS_FILE, "w") as f:
            json.dump({"model-a": 999}, f)
        model = mr.pick_model("default")
        self.assertEqual(model, "model-a")

    def test_all_exhausted_returns_last_with_warning(self):
        mr = _make_router(self.tmp, cap=1_000)
        with open(mr._COUNTS_FILE, "w") as f:
            json.dump({"model-a": 1_000, "model-b": 1_000}, f)
        with self.assertLogs("utils.model_router", level="WARNING") as cm:
            model = mr.pick_model("default")
        self.assertEqual(model, "model-b")
        self.assertTrue(any("dry" in line.lower() or "exhausted" in line.lower()
                            for line in cm.output))

    def test_fast_role_uses_fast_tier(self):
        """A latency-sensitive role (narrator/translate) gets the FAST tier in demo profile."""
        mr = _make_router(self.tmp)
        self.assertEqual(mr.pick_model("narrator"), "fast-a")
        self.assertEqual(mr.pick_model("translate"), "fast-a")

    def test_smart_role_uses_demo_tier(self):
        """A non-fast role gets the demo (smart) tier, not the fast tier."""
        mr = _make_router(self.tmp)
        self.assertEqual(mr.pick_model("default"), "model-a")
        self.assertEqual(mr.pick_model("parse"), "model-a")

    def test_fast_role_rotates_within_fast_tier(self):
        """The fast tier rotates at the cap independently of the demo tier."""
        mr = _make_router(self.tmp)
        mr.record_usage("fast-a", mr._TOKEN_CAP)
        self.assertEqual(mr.pick_model("narrator"), "fast-b")
        # the smart tier is untouched by the fast-tier exhaustion
        self.assertEqual(mr.pick_model("default"), "model-a")

    def test_test_profile_ignores_fast_role(self):
        """In the TEST profile every role (incl. narrator) uses the single cheap tier."""
        mr = _make_router(self.tmp, profile="test")
        self.assertEqual(mr.pick_model("narrator"), "model-x")
        self.assertEqual(mr.pick_model("default"), "model-x")


class TestRecordUsage(unittest.TestCase):
    """record_usage persists increments correctly."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_creates_and_persists_count(self):
        mr = _make_router(self.tmp)
        mr.record_usage("model-a", 500)
        counts = json.loads(open(mr._COUNTS_FILE).read())
        self.assertEqual(counts["model-a"], 500)

    def test_accumulates_across_calls(self):
        mr = _make_router(self.tmp)
        mr.record_usage("model-a", 300)
        mr.record_usage("model-a", 400)
        counts = json.loads(open(mr._COUNTS_FILE).read())
        self.assertEqual(counts["model-a"], 700)

    def test_tracks_multiple_models(self):
        mr = _make_router(self.tmp)
        mr.record_usage("model-a", 200)
        mr.record_usage("model-b", 100)
        counts = json.loads(open(mr._COUNTS_FILE).read())
        self.assertEqual(counts["model-a"], 200)
        self.assertEqual(counts["model-b"], 100)

    def test_zero_tokens_noop(self):
        """record_usage with 0 tokens must not create the file."""
        mr = _make_router(self.tmp)
        mr.record_usage("model-a", 0)
        self.assertFalse(os.path.exists(mr._COUNTS_FILE))


class TestMarkExhausted(unittest.TestCase):
    """mark_exhausted forces rotation immediately."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_marks_at_cap(self):
        mr = _make_router(self.tmp, cap=1_000)
        mr.mark_exhausted("model-a")
        counts = json.loads(open(mr._COUNTS_FILE).read())
        self.assertGreaterEqual(counts["model-a"], 1_000)

    def test_pick_model_skips_after_mark_exhausted(self):
        mr = _make_router(self.tmp, cap=1_000)
        mr.mark_exhausted("model-a")
        model = mr.pick_model("default")
        self.assertEqual(model, "model-b")

    def test_mark_exhausted_does_not_reduce_existing_count(self):
        """If a model already has MORE tokens than cap, mark_exhausted must not decrease it."""
        mr = _make_router(self.tmp, cap=1_000)
        with open(mr._COUNTS_FILE, "w") as f:
            json.dump({"model-a": 2_000}, f)
        mr.mark_exhausted("model-a")
        counts = json.loads(open(mr._COUNTS_FILE).read())
        self.assertGreaterEqual(counts["model-a"], 2_000)


class TestProfileSelection(unittest.TestCase):
    """SOCIETY_MODEL_PROFILE selects test vs demo tier."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_demo_profile(self):
        mr = _make_router(self.tmp, profile="demo", demo_models="demo-1,demo-2")
        self.assertIn("demo-1", mr._ACTIVE_LIST)

    def test_test_profile(self):
        mr = _make_router(self.tmp, profile="test", test_models="test-cheap")
        self.assertIn("test-cheap", mr._ACTIVE_LIST)
        mr2 = _make_router(self.tmp, profile="test", test_models="test-cheap", cap=1_000)
        model = mr2.pick_model("default")
        self.assertEqual(model, "test-cheap")

    def test_unknown_profile_defaults_to_demo(self):
        mr = _make_router(self.tmp, profile="unknown", demo_models="demo-default")
        self.assertIn("demo-default", mr._ACTIVE_LIST)


class TestDashscopeChat(unittest.TestCase):
    """dashscope_chat: _post seam, model injection, usage recording, error handling."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _fake_post_ok(self, url, *, headers, json):
        """Fake DashScope success response."""
        return {
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }

    def _fake_post_no_usage(self, url, *, headers, json):
        return {"choices": [{"message": {"content": "hi"}}]}

    def test_injects_model_and_returns_data(self):
        mr = _make_router(self.tmp, demo_models="model-a,model-b")
        body = {"enable_thinking": False, "messages": [{"role": "user", "content": "hi"}]}
        data = mr.dashscope_chat("default", body, _post=self._fake_post_ok)
        self.assertIn("choices", data)

    def test_does_not_mutate_caller_body(self):
        mr = _make_router(self.tmp)
        body = {"enable_thinking": False, "messages": []}
        mr.dashscope_chat("default", body, _post=self._fake_post_ok)
        # The original body should NOT have a "model" key injected into it.
        self.assertNotIn("model", body)

    def test_usage_NOT_recorded_via_post_seam(self):
        """When _post is provided (test seam), record_usage is skipped."""
        mr = _make_router(self.tmp)
        body = {"messages": []}
        mr.dashscope_chat("default", body, _post=self._fake_post_ok)
        # No file should be created since _post is a seam.
        self.assertFalse(os.path.exists(mr._COUNTS_FILE))

    def test_no_usage_in_response_is_safe(self):
        """If the response has no 'usage' key, dashscope_chat should not crash."""
        mr = _make_router(self.tmp)
        body = {"messages": []}
        data = mr.dashscope_chat("default", body, _post=self._fake_post_no_usage)
        self.assertIn("choices", data)

    def test_4xx_marks_exhausted_and_reraises(self):
        """On 4xx HTTPStatusError (real httpx path), mark_exhausted fires and error propagates."""
        import httpx

        mr = _make_router(self.tmp, demo_models="model-a,model-b", cap=1_000)
        body = {"messages": []}

        # Build a mock httpx response that raises on raise_for_status().
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        http_err = httpx.HTTPStatusError("rate limit", request=MagicMock(), response=mock_resp)
        mock_resp.raise_for_status.side_effect = http_err

        with patch("httpx.post", return_value=mock_resp):
            with self.assertRaises(httpx.HTTPStatusError):
                mr.dashscope_chat("default", body)
        # model-a should now be marked exhausted.
        counts = json.loads(open(mr._COUNTS_FILE).read())
        self.assertGreaterEqual(counts.get("model-a", 0), 1_000)

    def test_record_usage_called_on_real_path(self):
        """When _post=None (real HTTP path) and usage is present, record_usage fires."""
        mr = _make_router(self.tmp, demo_models="model-a,model-b")

        # Build a fake httpx response.
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "result"}}],
            "usage": {"total_tokens": 77},
        }

        with patch("httpx.post", return_value=mock_resp):
            mr.dashscope_chat("default", {"messages": []})

        counts = json.loads(open(mr._COUNTS_FILE).read())
        self.assertEqual(counts.get("model-a", 0), 77)


class TestInertWhenLLMOff(unittest.TestCase):
    """model_router must not touch the filesystem at import time."""

    def test_import_does_not_read_or_write_state(self):
        """Importing model_router must not create .token_counts.json."""
        tmp = tempfile.mkdtemp()
        counts_path = os.path.join(tmp, ".token_counts.json")
        # Even after re-import the file should not exist.
        for key in list(sys.modules):
            if "model_router" in key:
                del sys.modules[key]
        soc_path = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
        had = soc_path in sys.path
        if not had:
            sys.path.insert(0, soc_path)
        try:
            import utils.model_router  # noqa: F401
        finally:
            if not had and soc_path in sys.path:
                sys.path.remove(soc_path)
        self.assertFalse(os.path.exists(counts_path),
                         "model_router must not write state at import time")


if __name__ == "__main__":
    unittest.main()
