"""test_place_card_endpoint.py — POST /place_card + GET /place_photo endpoint tests.

All tests use monkeypatching / module-level env var manipulation — NO live Google Places
key required. Key-safety is the most critical invariant: the GOOGLE_PLACES_KEY must
NEVER appear in any response field.

Test coverage (per build plan):
1.  test_disabled_returns_unavailable_200
2.  test_detail_happy_path_shape
3.  test_key_never_in_response (KEY SAFETY)
4.  test_outage_degrades_to_unavailable_not_500
5.  test_cache_hit_skips_second_upstream_call
6.  test_autocomplete_shape
7.  test_missing_mode_400
8.  test_detail_missing_name_400
9.  test_place_card_off_var0
10. test_place_photo_attaches_key_serverside
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient
from orchestration import server
from orchestration.store import SqliteDashboardStore, set_store

_FAKE_KEY = "AIzaFAKE_TEST_KEY_MUST_NOT_APPEAR_IN_ANY_RESP"

# ---------------------------------------------------------------------------
# Canned Places API responses (mimics the real API shape)
# ---------------------------------------------------------------------------

_PLACE_RESPONSE = {
    "places": [
        {
            "displayName": {"text": "Sensō-ji"},
            "formattedAddress": "2-3-1 Asakusa, Taito City, Tokyo",
            "rating": 4.5,
            "userRatingCount": 62000,
            "currentOpeningHours": {"openNow": True},
            "photos": [
                {"name": "places/ChIJ_ABC123/photos/AXCi2Q_REAL_PHOTO_NAME"},
            ],
            "reviews": [
                {
                    "text": {"text": "Amazing temple!"},
                    "rating": 5,
                    "authorAttribution": {"displayName": "Test User"},
                }
            ],
        }
    ]
}

_AUTOCOMPLETE_RESPONSE = {
    "suggestions": [
        {"placePrediction": {"text": {"text": "Sensō-ji, Tokyo"}}},
        {"placePrediction": {"text": {"text": "Sensoji Market"}}},
    ]
}


def _make_mock_resp(json_data: dict, status_code: int = 200):
    m = MagicMock()
    m.status_code = status_code
    m.json.return_value = json_data
    return m


# ---------------------------------------------------------------------------
# Helper: build a test client with the store injected and Places key set
# ---------------------------------------------------------------------------

def _client():
    store = SqliteDashboardStore(":memory:")
    set_store(store)
    return TestClient(server.build_app())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPlaceCardEndpoint(unittest.TestCase):

    def setUp(self):
        import utils.places_card as pc
        # Reset in-process caches so hits don't bleed between tests.
        with pc._card_lock:
            pc._card_cache.clear()
        with pc._photo_lock:
            pc._photo_refs.clear()
        # Redirect ALL disk-cache paths to a fresh tmpdir: photo bytes (so stale
        # on-disk photos from previous server runs can't bypass the httpx.get
        # mock) AND the card/ref JSON cache (so a full _search_detail flow here
        # can't flush test data into the real, repo-tracked
        # places_data/places_cache.json). All three globals are patched together
        # since _CARD_CACHE_PATH/_PHOTO_DIR are derived from _CACHE_DIR once at
        # import time.
        self._tmpdir = tempfile.mkdtemp()
        self._orig_cache_dir = pc._CACHE_DIR
        self._orig_card_cache_path = pc._CARD_CACHE_PATH
        self._orig_photo_dir = pc._PHOTO_DIR
        pc._CACHE_DIR = self._tmpdir
        pc._CARD_CACHE_PATH = os.path.join(self._tmpdir, "places_cache.json")
        pc._PHOTO_DIR = os.path.join(self._tmpdir, "photos")
        os.makedirs(pc._PHOTO_DIR, exist_ok=True)

    def tearDown(self):
        import utils.places_card as pc
        pc._CACHE_DIR = self._orig_cache_dir
        pc._CARD_CACHE_PATH = self._orig_card_cache_path
        pc._PHOTO_DIR = self._orig_photo_dir
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # ---- 1. Disabled → unavailable 200 ----
    def test_disabled_returns_unavailable_200(self):
        """When PLACES_ENABLED and GOOGLE_PLACES_KEY are not set, /place_card returns unavailable."""
        import utils.places_card as pc
        orig_enabled = pc.PLACES_ENABLED
        orig_key = pc.PLACES_KEY
        try:
            pc.PLACES_ENABLED = False
            pc.PLACES_KEY = ""
            client = _client()
            with client:
                r = client.post("/place_card", json={
                    "mode": "detail", "name": "Senso-ji", "city": "tokyo",
                })
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body["status"], "unavailable")
        finally:
            pc.PLACES_ENABLED = orig_enabled
            pc.PLACES_KEY = orig_key

    # ---- 2. Detail happy path shape ----
    def test_detail_happy_path_shape(self):
        import utils.places_card as pc
        orig_enabled = pc.PLACES_ENABLED
        orig_key = pc.PLACES_KEY
        try:
            pc.PLACES_ENABLED = True
            pc.PLACES_KEY = _FAKE_KEY
            with patch("httpx.post", return_value=_make_mock_resp(_PLACE_RESPONSE)):
                client = _client()
                with client:
                    r = client.post("/place_card", json={
                        "mode": "detail", "name": "Senso-ji", "city": "tokyo", "country": "Japan",
                    })
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body["status"], "ok")
            self.assertEqual(body["source"], "live:google_places")
            self.assertIn("as_of", body)
            place = body.get("place") or {}
            self.assertEqual(place.get("display_name"), "Sensō-ji")
            self.assertEqual(place.get("rating"), 4.5)
            self.assertIsInstance(place.get("reviews"), list)
            self.assertIsInstance(place.get("photos"), list)
            self.assertIn("open_now", place)
        finally:
            pc.PLACES_ENABLED = orig_enabled
            pc.PLACES_KEY = orig_key

    # ---- 3. Key NEVER in response (KEY SAFETY) ----
    def test_key_never_in_response(self):
        """GOOGLE_PLACES_KEY must not appear in any field of the response body.
        Photo URLs must use /place_photo?ref=<opaque> — not the raw Places photo name."""
        import utils.places_card as pc
        orig_enabled = pc.PLACES_ENABLED
        orig_key = pc.PLACES_KEY
        try:
            pc.PLACES_ENABLED = True
            pc.PLACES_KEY = _FAKE_KEY
            with patch("httpx.post", return_value=_make_mock_resp(_PLACE_RESPONSE)):
                client = _client()
                with client:
                    r = client.post("/place_card", json={
                        "mode": "detail", "name": "Senso-ji", "city": "tokyo",
                    })
            self.assertEqual(r.status_code, 200)
            raw_body = r.text
            # Key must not appear in the body.
            self.assertNotIn(_FAKE_KEY, raw_body, "GOOGLE_PLACES_KEY leaked into response!")
            # Raw Places photo name (with our key derivable from it) must not appear.
            self.assertNotIn("places/ChIJ_ABC123/photos/", raw_body,
                             "Raw Places photo name leaked into response!")
            # Photo URL should be our proxied /place_photo?ref=... format.
            body = r.json()
            photos = (body.get("place") or {}).get("photos") or []
            for ph in photos:
                self.assertTrue(ph.startswith("/place_photo?ref="),
                                f"Photo URL is not proxied: {ph!r}")
        finally:
            pc.PLACES_ENABLED = orig_enabled
            pc.PLACES_KEY = orig_key

    # ---- 4. Outage degrades to unavailable, not 500 ----
    def test_outage_degrades_to_unavailable_not_500(self):
        import utils.places_card as pc
        orig_enabled = pc.PLACES_ENABLED
        orig_key = pc.PLACES_KEY
        try:
            pc.PLACES_ENABLED = True
            pc.PLACES_KEY = _FAKE_KEY
            with patch("httpx.post", side_effect=Exception("network error")):
                client = _client()
                with client:
                    r = client.post("/place_card", json={
                        "mode": "detail", "name": "Senso-ji", "city": "tokyo",
                    })
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body["status"], "unavailable")
        finally:
            pc.PLACES_ENABLED = orig_enabled
            pc.PLACES_KEY = orig_key

    # ---- 5. Cache hit skips second upstream call ----
    def test_cache_hit_skips_second_upstream_call(self):
        import utils.places_card as pc
        orig_enabled = pc.PLACES_ENABLED
        orig_key = pc.PLACES_KEY
        try:
            pc.PLACES_ENABLED = True
            pc.PLACES_KEY = _FAKE_KEY
            call_count = {"n": 0}
            def _counting_post(*args, **kwargs):
                call_count["n"] += 1
                return _make_mock_resp(_PLACE_RESPONSE)
            with patch("httpx.post", side_effect=_counting_post):
                client = _client()
                with client:
                    r1 = client.post("/place_card", json={
                        "mode": "detail", "name": "Senso-ji", "city": "tokyo",
                    })
                    r2 = client.post("/place_card", json={
                        "mode": "detail", "name": "Senso-ji", "city": "tokyo",
                    })
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(r2.status_code, 200)
            # Only one upstream call made — second was served from cache.
            self.assertEqual(call_count["n"], 1, "Cache hit should skip 2nd upstream call")
        finally:
            pc.PLACES_ENABLED = orig_enabled
            pc.PLACES_KEY = orig_key

    # ---- 6. Autocomplete shape ----
    def test_autocomplete_shape(self):
        import utils.places_card as pc
        orig_enabled = pc.PLACES_ENABLED
        orig_key = pc.PLACES_KEY
        try:
            pc.PLACES_ENABLED = True
            pc.PLACES_KEY = _FAKE_KEY
            with patch("httpx.post", return_value=_make_mock_resp(_AUTOCOMPLETE_RESPONSE)):
                client = _client()
                with client:
                    r = client.post("/place_card", json={
                        "mode": "autocomplete", "input": "sens", "city": "tokyo",
                    })
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body["status"], "ok")
            preds = body.get("predictions") or []
            self.assertGreater(len(preds), 0)
            self.assertIn("text", preds[0])
        finally:
            pc.PLACES_ENABLED = orig_enabled
            pc.PLACES_KEY = orig_key

    # ---- 7. missing mode → 400 ----
    def test_missing_mode_400(self):
        client = _client()
        with client:
            r = client.post("/place_card", json={"name": "Senso-ji"})
        self.assertEqual(r.status_code, 400)

    # ---- 8. detail missing name → 400 ----
    def test_detail_missing_name_400(self):
        client = _client()
        with client:
            r = client.post("/place_card", json={"mode": "detail", "city": "tokyo"})
        self.assertEqual(r.status_code, 400)

    # ---- 9. /place_card is off var-0 ----
    def test_place_card_off_var0(self):
        """Calling /place_card must not affect the deterministic negotiate path.
        We verify structurally: places_card and replan_ops don't import from
        orchestration.orchestrator (which contains negotiate/_request_digest).
        This ensures neither util can touch the var-0 core."""
        import utils.places_card as pc
        import utils.replan_ops as ro
        import inspect
        # Neither util should import from orchestration.orchestrator at module level.
        # Check the actual import statements (not docstrings/comments).
        import ast
        def _get_imports(module) -> list[str]:
            """Return all imported module names from the module's source."""
            src = inspect.getsource(module)
            tree = ast.parse(src)
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        imports.append(node.module)
            return imports
        pc_imports = _get_imports(pc)
        ro_imports = _get_imports(ro)
        self.assertNotIn("orchestration.orchestrator", pc_imports,
                         "places_card must not import from orchestration.orchestrator")
        self.assertNotIn("orchestration.orchestrator", ro_imports,
                         "replan_ops must not import from orchestration.orchestrator")
        # Also assert neither module calls negotiate() as a function call in non-comment code.
        # Using AST call analysis (skips docstrings and comments).
        def _has_call_to(module, func_name: str) -> bool:
            src = inspect.getsource(module)
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Name) and func.id == func_name:
                        return True
                    if isinstance(func, ast.Attribute) and func.attr == func_name:
                        return True
            return False
        self.assertFalse(_has_call_to(pc, "negotiate"),
                         "places_card must not call negotiate()")
        self.assertFalse(_has_call_to(ro, "negotiate"),
                         "replan_ops must not call negotiate()")

    # ---- 10. place_photo proxy attaches key server-side ----
    def test_place_photo_attaches_key_serverside_streams_bytes(self):
        """GET /place_photo?ref=<opaque> must attach the key server-side and return bytes.
        The client never sees the key or the raw Places photo name."""
        import utils.places_card as pc
        orig_enabled = pc.PLACES_ENABLED
        orig_key = pc.PLACES_KEY
        try:
            pc.PLACES_ENABLED = True
            pc.PLACES_KEY = _FAKE_KEY
            # First, seed an opaque ref by calling _search_detail (which mints opaque refs).
            with patch("httpx.post", return_value=_make_mock_resp(_PLACE_RESPONSE)):
                result, _ = pc.fetch_place_card({"mode": "detail", "name": "Senso-ji", "city": "tokyo"})
            self.assertEqual(result.get("status"), "ok")
            photos = (result.get("place") or {}).get("photos") or []
            self.assertTrue(photos, "Must have at least one photo URL")
            opaque = photos[0].replace("/place_photo?ref=", "")

            # Now test the /place_photo endpoint: mock httpx.get to return fake bytes.
            fake_bytes = b"JPEG_FAKE_IMAGE_BYTES"
            mock_photo_resp = MagicMock()
            mock_photo_resp.status_code = 200
            mock_photo_resp.content = fake_bytes

            with patch("httpx.get", return_value=mock_photo_resp) as mock_get:
                client = _client()
                with client:
                    r = client.get(f"/place_photo?ref={opaque}")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.content, fake_bytes)
            # The key must appear in the actual httpx.get call (server-side) but not
            # in the request that the client made (the client only sent ?ref=opaque).
            call_args = mock_get.call_args
            self.assertIsNotNone(call_args, "httpx.get must have been called")
            # Key in headers (server-side call).
            kwargs = call_args[1] if call_args[1] else {}
            headers = kwargs.get("headers") or {}
            self.assertIn("X-Goog-Api-Key", headers, "Key must be in server-side headers")
            self.assertEqual(headers["X-Goog-Api-Key"], _FAKE_KEY)
            # The client's response must NOT contain the key.
            self.assertNotIn(_FAKE_KEY, r.text)
        finally:
            pc.PLACES_ENABLED = orig_enabled
            pc.PLACES_KEY = orig_key


class TestPlaceCardUtil(unittest.TestCase):
    """Unit tests for places_card.py (no HTTP server needed)."""

    def setUp(self):
        import utils.places_card as pc
        with pc._card_lock:
            pc._card_cache.clear()
        with pc._photo_lock:
            pc._photo_refs.clear()
        # Redirect ALL disk-cache paths to a fresh tmpdir so tests that mint refs
        # or write cards (e.g. test_opaque_ref_roundtrip) can't leak into the
        # real, repo-tracked places_data/places_cache.json. _CARD_CACHE_PATH and
        # _PHOTO_DIR are derived from _CACHE_DIR once at import time, so all three
        # module globals must be patched together — patching _CACHE_DIR alone is
        # not enough, since _flush_disk_cache() reads _CARD_CACHE_PATH directly.
        self._tmpdir = tempfile.mkdtemp()
        self._orig_cache_dir = pc._CACHE_DIR
        self._orig_card_cache_path = pc._CARD_CACHE_PATH
        self._orig_photo_dir = pc._PHOTO_DIR
        pc._CACHE_DIR = self._tmpdir
        pc._CARD_CACHE_PATH = os.path.join(self._tmpdir, "places_cache.json")
        pc._PHOTO_DIR = os.path.join(self._tmpdir, "photos")
        os.makedirs(pc._PHOTO_DIR, exist_ok=True)

    def tearDown(self):
        import utils.places_card as pc
        pc._CACHE_DIR = self._orig_cache_dir
        pc._CARD_CACHE_PATH = self._orig_card_cache_path
        pc._PHOTO_DIR = self._orig_photo_dir
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_guard_param_rejects_url_scheme(self):
        from utils.places_card import _guard_param
        self.assertIsNone(_guard_param("http://evil.com/ssrf", "name"))
        self.assertIsNone(_guard_param("file:///etc/passwd", "name"))
        self.assertIsNone(_guard_param("/../../etc/passwd", "name"))

    def test_guard_param_allows_normal_input(self):
        from utils.places_card import _guard_param
        self.assertEqual(_guard_param("Senso-ji Temple", "name"), "Senso-ji Temple")
        self.assertEqual(_guard_param("Tokyo, Japan", "city"), "Tokyo, Japan")

    def test_opaque_ref_roundtrip(self):
        from utils.places_card import _mint_opaque_ref, resolve_photo_name
        pn = "places/ChIJ_XYZ/photos/photo_name_secret"
        opaque = _mint_opaque_ref(pn)
        self.assertNotEqual(opaque, pn, "Opaque ref must not equal the raw name")
        self.assertNotIn("photo_name_secret", opaque)
        resolved = resolve_photo_name(opaque)
        self.assertEqual(resolved, pn)

    def test_disk_cache_writes_stay_off_the_real_repo_path(self):
        """Regression lock for the test-isolation leak: minting a ref (or any
        other disk-cache write) must never touch the real, repo-tracked
        places_data/places_cache.json — only the tmpdir this setUp() redirects
        to. If setUp() ever stops patching _CACHE_DIR/_CARD_CACHE_PATH/_PHOTO_DIR,
        this fails instead of silently writing test junk into a tracked file."""
        from utils.places_card import _mint_opaque_ref
        import utils.places_card as pc

        real_cache_dir = os.path.join(os.path.dirname(pc.__file__), "..", "..", "places_data")
        real_card_cache_path = os.path.abspath(os.path.join(real_cache_dir, "places_cache.json"))

        self.assertNotEqual(
            os.path.abspath(pc._CARD_CACHE_PATH), real_card_cache_path,
            "setUp() must redirect _CARD_CACHE_PATH away from the real repo path",
        )
        real_existed_before = os.path.exists(real_card_cache_path)
        real_mtime_before = os.path.getmtime(real_card_cache_path) if real_existed_before else None

        _mint_opaque_ref("places/ChIJ_TEST_ISOLATION/photos/some_photo_name")

        self.assertTrue(os.path.exists(pc._CARD_CACHE_PATH), "mint should flush to the redirected tmpdir path")
        if real_existed_before:
            self.assertEqual(os.path.getmtime(real_card_cache_path), real_mtime_before,
                              "real repo cache file must not be modified by this test")
        else:
            self.assertFalse(os.path.exists(real_card_cache_path),
                              "real repo cache file must not be created by this test")

    def test_fetch_place_card_disabled(self):
        import utils.places_card as pc
        orig_enabled = pc.PLACES_ENABLED
        orig_key = pc.PLACES_KEY
        try:
            pc.PLACES_ENABLED = False
            pc.PLACES_KEY = ""
            result, status = pc.fetch_place_card({"mode": "detail", "name": "Senso-ji"})
            self.assertEqual(status, 200)
            self.assertEqual(result["status"], "unavailable")
        finally:
            pc.PLACES_ENABLED = orig_enabled
            pc.PLACES_KEY = orig_key


if __name__ == "__main__":
    unittest.main()
