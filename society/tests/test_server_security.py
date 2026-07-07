"""
test_server_security.py — Regression tests for Python security hardening.

Covers:
  SEC-005 (GROUP E): body-size limit → 413 on oversized requests.
  SEC-001 (GROUP E): rate-limit → 429 on excess requests.
  SEC-001 (GROUP E): optional token-auth → 401 without bearer; 200 with.
  SSRF-001 (GROUP F): MERCHANT_MCP_URL with metadata IP → startup ValueError.
  VULN-001-signing (GROUP G): strict mode re-raises on bad key.
  VULN-003 (board): complete_checkout without prior update_checkout → requires_consent.

All tests are var-0-safe: no live network, no LLM, no paid APIs.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))

import httpx
import pytest
from starlette.testclient import TestClient

from orchestration import server


# ===========================================================================
# SEC-005 — Body-size limit (always-on, ~1 MiB)
# ===========================================================================

class TestBodySizeLimit:
    """POST /negotiate with an oversized body → 413; normal body → passthrough."""

    def _app(self, max_bytes: int = 512):
        """Build a tiny app with a 512-byte body limit for fast tests."""
        import os as _os
        saved = _os.environ.get("SOCIETY_MAX_BODY_BYTES")
        _os.environ["SOCIETY_MAX_BODY_BYTES"] = str(max_bytes)
        try:
            return server.build_app()
        finally:
            if saved is None:
                _os.environ.pop("SOCIETY_MAX_BODY_BYTES", None)
            else:
                _os.environ["SOCIETY_MAX_BODY_BYTES"] = saved

    def test_oversized_body_rejected_413(self) -> None:
        """POST /negotiate with 2 MiB body → 413 (SEC-005)."""
        # Build with the default 1 MiB cap (no env override needed).
        app = server.build_app()
        client = TestClient(app, raise_server_exceptions=False)
        big = b"x" * (2 * 1024 * 1024)  # 2 MiB
        resp = client.post("/negotiate", content=big,
                           headers={"Content-Type": "application/json"})
        assert resp.status_code == 413, (
            f"Expected 413 for 2 MiB body, got {resp.status_code}: {resp.text[:200]}"
        )
        assert "too large" in resp.json().get("error", "").lower()
        print("PASS: test_oversized_body_rejected_413")

    def test_normal_body_passes_through(self) -> None:
        """POST /negotiate with a small valid body → not 413."""
        app = server.build_app()
        client = TestClient(app, raise_server_exceptions=False)
        small = json.dumps({"user_id": "u1", "legs": []}).encode()
        resp = client.post("/negotiate", content=small,
                           headers={"Content-Type": "application/json"})
        # May be 400 (validation error) but NOT 413.
        assert resp.status_code != 413, f"Small body wrongly rejected: {resp.status_code}"
        print("PASS: test_normal_body_passes_through")

    def test_get_stream_exempt(self) -> None:
        """GET /stream/* is exempt from the body-size limit (SSE path)."""
        app = server.build_app()
        client = TestClient(app, raise_server_exceptions=False)
        # Even a large-looking Content-Length header on a GET must not trigger 413.
        # (GET /stream/<id> returns 404 for non-existent id — not 413.)
        resp = client.get("/stream/nonexistent-id",
                          headers={"Content-Length": str(10 * 1024 * 1024)})
        assert resp.status_code != 413
        print("PASS: test_get_stream_exempt")


# ===========================================================================
# SEC-001 — Rate limit (per-IP token bucket)
# ===========================================================================

class TestRateLimit:
    """Rapid POST burst from same IP → 429; sequential at normal pace → pass."""

    def test_rapid_burst_hits_429(self) -> None:
        """20 requests/sec from one IP with a 2/min cap → 429."""
        saved = os.environ.get("SOCIETY_RATE_PER_MIN")
        os.environ["SOCIETY_RATE_PER_MIN"] = "2"
        try:
            app = server.build_app()
            client = TestClient(app, raise_server_exceptions=False)
            small = json.dumps({"user_id": "u1", "legs": []}).encode()
            statuses = []
            for _ in range(5):
                r = client.post("/negotiate", content=small,
                                headers={"Content-Type": "application/json"})
                statuses.append(r.status_code)
            assert 429 in statuses, (
                f"Expected at least one 429 in burst; got {statuses}"
            )
            print("PASS: test_rapid_burst_hits_429")
        finally:
            if saved is None:
                os.environ.pop("SOCIETY_RATE_PER_MIN", None)
            else:
                os.environ["SOCIETY_RATE_PER_MIN"] = saved

    def test_health_get_never_rate_limited(self) -> None:
        """GET /health is not rate-limited (only POSTs are)."""
        saved = os.environ.get("SOCIETY_RATE_PER_MIN")
        os.environ["SOCIETY_RATE_PER_MIN"] = "1"
        try:
            app = server.build_app()
            client = TestClient(app, raise_server_exceptions=False)
            for _ in range(10):
                r = client.get("/health")
                assert r.status_code == 200
            print("PASS: test_health_get_never_rate_limited")
        finally:
            if saved is None:
                os.environ.pop("SOCIETY_RATE_PER_MIN", None)
            else:
                os.environ["SOCIETY_RATE_PER_MIN"] = saved


# ===========================================================================
# CORS — allowlist (was allow_origins=["*"] unconditionally)
# ===========================================================================

class TestCORSConfig:
    """SOCIETY_CORS_ORIGINS controls the CORS allowlist; default is NOT a wildcard."""

    def test_default_origins_not_wildcard(self) -> None:
        """With SOCIETY_CORS_ORIGINS unset, the default allowlist must be explicit
        origins, never the literal wildcard string '*'."""
        saved = os.environ.pop("SOCIETY_CORS_ORIGINS", None)
        try:
            app = server.build_app()
            client = TestClient(app, raise_server_exceptions=False)
            # A known-good default origin gets its own value echoed back (proves
            # the middleware is matching a specific origin, not "allow anything").
            # (public showcase repo: default origin is now a local dev URL, not
            # the private staging host — same assertion, updated value.)
            resp = client.get(
                "/health",
                headers={"Origin": "http://localhost:5173"},
            )
            assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"
            # An origin NOT on the default allowlist gets NO CORS header at all —
            # the defining behavioral difference from a wildcard, which would
            # echo (or literally send) '*' for any origin.
            resp_evil = client.get(
                "/health",
                headers={"Origin": "https://evil.example.com"},
            )
            assert resp_evil.headers.get("access-control-allow-origin") is None
            print("PASS: test_default_origins_not_wildcard")
        finally:
            if saved is not None:
                os.environ["SOCIETY_CORS_ORIGINS"] = saved

    def test_env_override_allows_only_listed_origins(self) -> None:
        """SOCIETY_CORS_ORIGINS overrides the default to an exact, comma-separated
        allowlist; an origin not in that list is rejected (no CORS header)."""
        saved = os.environ.get("SOCIETY_CORS_ORIGINS")
        os.environ["SOCIETY_CORS_ORIGINS"] = "https://a.example.com,https://b.example.com"
        try:
            app = server.build_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp_a = client.get("/health", headers={"Origin": "https://a.example.com"})
            assert resp_a.headers.get("access-control-allow-origin") == "https://a.example.com"
            resp_c = client.get("/health", headers={"Origin": "https://c.example.com"})
            assert resp_c.headers.get("access-control-allow-origin") is None
            print("PASS: test_env_override_allows_only_listed_origins")
        finally:
            if saved is None:
                os.environ.pop("SOCIETY_CORS_ORIGINS", None)
            else:
                os.environ["SOCIETY_CORS_ORIGINS"] = saved

    def test_preflight_methods_not_wildcard(self) -> None:
        """OPTIONS preflight for an allowed origin reports a specific method list
        (GET/POST/PUT), never the literal wildcard '*'."""
        saved = os.environ.pop("SOCIETY_CORS_ORIGINS", None)
        try:
            app = server.build_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.options(
                "/negotiate",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": "POST",
                },
            )
            allow_methods = resp.headers.get("access-control-allow-methods", "")
            assert allow_methods != "*"
            assert "POST" in allow_methods
            print("PASS: test_preflight_methods_not_wildcard")
        finally:
            if saved is not None:
                os.environ["SOCIETY_CORS_ORIGINS"] = saved


# ===========================================================================
# SEC-001 — Optional token auth (OFF by default)
# ===========================================================================

class TestOptionalTokenAuth:
    """SOCIETY_API_TOKEN controls optional Bearer-token auth on POST routes."""

    def test_no_token_set_fully_open(self) -> None:
        """Without SOCIETY_API_TOKEN set, POST /negotiate is fully open."""
        saved = os.environ.pop("SOCIETY_API_TOKEN", None)
        try:
            app = server.build_app()
            client = TestClient(app, raise_server_exceptions=False)
            small = json.dumps({"user_id": "u1", "legs": []}).encode()
            resp = client.post("/negotiate", content=small,
                               headers={"Content-Type": "application/json"})
            assert resp.status_code != 401, (
                f"Expected no auth required; got {resp.status_code}"
            )
            print("PASS: test_no_token_set_fully_open")
        finally:
            if saved is not None:
                os.environ["SOCIETY_API_TOKEN"] = saved

    def test_with_token_set_requires_bearer(self) -> None:
        """With SOCIETY_API_TOKEN set: no Bearer → 401; correct Bearer → not 401."""
        saved = os.environ.get("SOCIETY_API_TOKEN")
        os.environ["SOCIETY_API_TOKEN"] = "test-secret-token"
        try:
            app = server.build_app()
            client = TestClient(app, raise_server_exceptions=False)
            small = json.dumps({"user_id": "u1", "legs": []}).encode()
            # Without Bearer → 401.
            resp_no_auth = client.post(
                "/negotiate", content=small,
                headers={"Content-Type": "application/json"},
            )
            assert resp_no_auth.status_code == 401, (
                f"Expected 401 without auth; got {resp_no_auth.status_code}"
            )
            # Wrong Bearer → 401.
            resp_wrong = client.post(
                "/negotiate", content=small,
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer wrong-token"},
            )
            assert resp_wrong.status_code == 401

            # Correct Bearer → not 401 (may be 400 from validation, but not auth error).
            resp_ok = client.post(
                "/negotiate", content=small,
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer test-secret-token"},
            )
            assert resp_ok.status_code != 401, (
                f"Expected auth pass; got {resp_ok.status_code}: {resp_ok.text[:200]}"
            )
            print("PASS: test_with_token_set_requires_bearer")
        finally:
            if saved is None:
                os.environ.pop("SOCIETY_API_TOKEN", None)
            else:
                os.environ["SOCIETY_API_TOKEN"] = saved

    def test_health_always_accessible(self) -> None:
        """GET /health is always accessible regardless of token auth."""
        saved = os.environ.get("SOCIETY_API_TOKEN")
        os.environ["SOCIETY_API_TOKEN"] = "test-secret-2"
        try:
            app = server.build_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/health")
            assert resp.status_code == 200
            print("PASS: test_health_always_accessible")
        finally:
            if saved is None:
                os.environ.pop("SOCIETY_API_TOKEN", None)
            else:
                os.environ["SOCIETY_API_TOKEN"] = saved


# ===========================================================================
# SSRF-001 — MERCHANT_MCP_URL metadata IP block
# ===========================================================================

class TestSSRFValidation:
    """MERCHANT_MCP_URL with metadata/link-local IP → ValueError at import/call time."""

    def test_metadata_url_blocked(self) -> None:
        """http://169.254.169.254/... resolves to a link-local address → ValueError."""
        from agents.budget_agent import _validate_merchant_url
        with pytest.raises(ValueError, match="169.254"):
            _validate_merchant_url("http://169.254.169.254/latest/meta-data/iam/credentials")
        print("PASS: test_metadata_url_blocked")

    def test_link_local_variant_blocked(self) -> None:
        """Any 169.254.x.x IP → ValueError (Azure/Oracle IMDS are also 169.254.x.x)."""
        from agents.budget_agent import _validate_merchant_url
        with pytest.raises(ValueError, match="169.254"):
            _validate_merchant_url("http://169.254.169.254/computeMetadata/v1/")
        print("PASS: test_link_local_variant_blocked")

    def test_non_http_scheme_blocked(self) -> None:
        """file:// and ftp:// schemes → ValueError."""
        from agents.budget_agent import _validate_merchant_url
        with pytest.raises(ValueError, match="scheme"):
            _validate_merchant_url("file:///etc/passwd")
        with pytest.raises(ValueError, match="scheme"):
            _validate_merchant_url("ftp://internal-server/resource")
        print("PASS: test_non_http_scheme_blocked")

    def test_cluster_internal_url_allowed(self) -> None:
        """http://ucp-merchant:8090/... (unresolvable cluster DNS) → allowed."""
        from agents.budget_agent import _validate_merchant_url
        # Should NOT raise (cluster-internal DNS not resolvable from dev box).
        _validate_merchant_url("http://ucp-merchant:8090/api/ucp/mcp")
        print("PASS: test_cluster_internal_url_allowed")

    def test_localhost_url_allowed(self) -> None:
        """http://127.0.0.1:8090/... → allowed (RFC 1918 / loopback OK for dev)."""
        from agents.budget_agent import _validate_merchant_url
        # 127.0.0.1 is loopback, not 169.254.x.x — should be allowed.
        _validate_merchant_url("http://127.0.0.1:8090/api/ucp/mcp")
        print("PASS: test_localhost_url_allowed")


# ===========================================================================
# VULN-001-signing — Strict mode re-raises on signing failure
# ===========================================================================

class TestSigningStrictMode:
    """UCP_CLIENT_SIGNING_STRICT=1 causes signing failures to raise, not degrade."""

    def test_strict_mode_raises_on_bad_key(self) -> None:
        """With strict mode + corrupt key, signed_headers raises (not {} + warning)."""
        from utils import ucp_signing
        with tempfile.TemporaryDirectory() as d:
            keypath = os.path.join(d, "bad_key.pem")
            with open(keypath, "wb") as fh:
                fh.write(b"this is not a valid private key file")
            saved = {k: os.environ.get(k) for k in
                     ("UCP_CLIENT_SIGNING_KEY_PATH", "UCP_AGENT_PROFILE_URL",
                      "UCP_CLIENT_SIGNING_STRICT")}
            try:
                os.environ["UCP_CLIENT_SIGNING_KEY_PATH"] = keypath
                os.environ["UCP_AGENT_PROFILE_URL"] = "https://example.com/ucp"
                os.environ["UCP_CLIENT_SIGNING_STRICT"] = "1"
                with pytest.raises(Exception):
                    ucp_signing.signed_headers("POST", "http://m:8090/x", b"{}")
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
        print("PASS: test_strict_mode_raises_on_bad_key")

    def test_non_strict_degrades_to_unsigned(self) -> None:
        """Without strict mode, corrupt key → {} (unsigned) + warning, never raises."""
        from utils import ucp_signing
        with tempfile.TemporaryDirectory() as d:
            keypath = os.path.join(d, "bad_key.pem")
            with open(keypath, "wb") as fh:
                fh.write(b"this is not a valid private key file")
            saved = {k: os.environ.get(k) for k in
                     ("UCP_CLIENT_SIGNING_KEY_PATH", "UCP_AGENT_PROFILE_URL",
                      "UCP_CLIENT_SIGNING_STRICT")}
            try:
                os.environ["UCP_CLIENT_SIGNING_KEY_PATH"] = keypath
                os.environ["UCP_AGENT_PROFILE_URL"] = "https://example.com/ucp"
                os.environ.pop("UCP_CLIENT_SIGNING_STRICT", None)
                result = ucp_signing.signed_headers("POST", "http://m:8090/x", b"{}")
                assert result == {}, f"Expected {{}} on corrupt key (non-strict); got {result}"
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
        print("PASS: test_non_strict_degrades_to_unsigned")


# ===========================================================================
# VULN-003 — Board transport: complete_checkout requires prior update_checkout
# ===========================================================================

class TestConsentGate:
    """_LocalCatalogTransport._complete_checkout gates on server-side consent."""

    def test_complete_without_update_checkout_blocked(self) -> None:
        """complete_checkout without prior update_checkout → requires_consent (not complete)."""
        t = server._LocalCatalogTransport()
        cr = t._create_checkout({"checkout": {"line_items": [{"total_cents": 10000}]}})
        co_id = cr["id"]
        # No update_checkout → no server-side consent.
        result, status = t._complete_checkout({"checkout": {"id": co_id}})
        assert result["status"] == "requires_consent", (
            f"Expected requires_consent, got {result['status']!r}: {result}"
        )
        # Must NOT be marked complete.
        assert result.get("booking_ref") is None or result.get("booking_ref") == "", (
            "booking_ref must not be set when consent not granted"
        )
        print("PASS: test_complete_without_update_checkout_blocked")

    def test_update_then_complete_succeeds(self) -> None:
        """update_checkout(buyer_consent=True) → complete_checkout → complete."""
        t = server._LocalCatalogTransport()
        cr = t._create_checkout({"checkout": {"line_items": [{"total_cents": 10000}]}})
        co_id = cr["id"]
        # Set server-side consent.
        upd = t._update_checkout({"checkout": {"id": co_id, "buyer_consent": True}})
        assert upd["buyer_consent"] is True
        result, status = t._complete_checkout({"checkout": {"id": co_id}})
        assert status == 200
        assert result["status"] == "complete"
        assert result["booking_ref"].startswith("BK-")
        print("PASS: test_update_then_complete_succeeds")

    def test_body_consent_alone_not_sufficient(self) -> None:
        """complete_checkout with buyer_consent in body (no update_checkout) → requires_consent.

        This is the VULN-003 regression: consent must never come from the caller's
        complete_checkout body — only from a prior update_checkout (server-side state).
        """
        t = server._LocalCatalogTransport()
        cr = t._create_checkout({"checkout": {"line_items": [{"total_cents": 10000}]}})
        co_id = cr["id"]
        # Attempt to bypass consent gate via body field (the old attack vector).
        # The _complete_checkout method ignores args["checkout"]["buyer_consent"].
        result, status = t._complete_checkout({
            "checkout": {"id": co_id, "buyer_consent": True}  # body consent — must be ignored
        })
        assert result["status"] == "requires_consent", (
            f"VULN-003 regression: body consent was accepted; got {result['status']!r}"
        )
        print("PASS: test_body_consent_alone_not_sufficient")


# ===========================================================================
# Entry point for standalone runs
# ===========================================================================

if __name__ == "__main__":
    classes = [
        TestBodySizeLimit, TestRateLimit, TestOptionalTokenAuth,
        TestSSRFValidation, TestSigningStrictMode, TestConsentGate,
    ]
    for cls in classes:
        obj = cls()
        for attr in dir(obj):
            if attr.startswith("test_"):
                print(f"\n--- {cls.__name__}.{attr} ---")
                getattr(obj, attr)()
    print("\nALL SECURITY REGRESSION TESTS PASSED")
