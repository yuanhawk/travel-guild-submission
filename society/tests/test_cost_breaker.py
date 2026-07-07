"""
test_cost_breaker.py — denial-of-wallet breaker + paid-GET rate-limit gap.

Root cause guarded here: once the auth wall drops, the expensive PAID downstreams
(DashScope LLM parse/narrate, Google Places detail/autocomplete/photo) had NO global
daily ceiling, and the one BILLABLE GET (/place_photo) was rate-limit-exempt — a
scraper could run up an unbounded bill.

Guarantees asserted:
  1. DEFAULT OFF — no cap env set ⇒ allow() always True, no counter touched, the
     served path is byte-identical (var-0 / judging unaffected).
  2. Daily cap — the N+1th expensive op degrades GRACEFULLY (no exception to the
     client, no paid call), not the Nth.
  3. Kill-switch (SOCIETY_PLANNING_DISABLED) trips every class immediately.
  4. Malformed cap fails OPEN (ungated) — a typo never downs judging.
  5. Paid GET /place_photo is now under the per-IP rate limit; cheap/SSE GETs aren't.
  6. The breaker never touches the planning digest / cache key.

All var-0-safe: no live network, no real LLM, no paid API. Paid downstreams are
monkeypatched to ASSERT they are never reached when the breaker is tripped.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from starlette.testclient import TestClient

from utils import cost_breaker


@pytest.fixture(autouse=True)
def _clean_breaker_env(monkeypatch):
    """Each test starts from a pristine, DISABLED breaker: no caps, no kill-switch,
    and the singleton's day/counter state reset (it persists across tests)."""
    for var in ("SOCIETY_DAILY_LLM_CAP", "SOCIETY_DAILY_PLACES_CAP",
                "SOCIETY_PLANNING_DISABLED"):
        monkeypatch.delenv(var, raising=False)
    b = cost_breaker.get_breaker()
    b._day = None
    b._counts = {}
    yield


# ===========================================================================
# 1. DEFAULT OFF — byte-identical no-op
# ===========================================================================

class TestDefaultOff:
    def test_disabled_by_default_allows_forever(self):
        b = cost_breaker.CostBreaker()
        for _ in range(1000):
            assert b.allow("llm") is True
            assert b.allow("places") is True
        # No cap ⇒ the disabled fast path never touched the counter (var-0: no clock
        # read, no mutation). snapshot proves nothing was recorded.
        snap = b.snapshot()
        assert snap["counts"] == {}
        assert snap["day"] is None
        assert snap["caps"] == {"llm": None, "places": None}

    def test_module_level_allow_is_disabled_by_default(self):
        assert cost_breaker.allow("llm") is True
        assert cost_breaker.allow("places") is True


# ===========================================================================
# 2. Daily caps — the N+1th op degrades
# ===========================================================================

class TestDailyCaps:
    def test_llm_cap_trips_on_n_plus_one(self, monkeypatch):
        monkeypatch.setenv("SOCIETY_DAILY_LLM_CAP", "3")
        b = cost_breaker.CostBreaker()
        assert [b.allow("llm") for _ in range(4)] == [True, True, True, False]
        # A separate class is independently ungated (no Places cap set).
        assert b.allow("places") is True

    def test_places_cap_trips_on_n_plus_one(self, monkeypatch):
        monkeypatch.setenv("SOCIETY_DAILY_PLACES_CAP", "2")
        b = cost_breaker.CostBreaker()
        assert [b.allow("places") for _ in range(3)] == [True, True, False]
        assert b.allow("llm") is True  # llm ungated

    def test_cap_zero_trips_immediately(self, monkeypatch):
        monkeypatch.setenv("SOCIETY_DAILY_PLACES_CAP", "0")
        b = cost_breaker.CostBreaker()
        assert b.allow("places") is False

    def test_classes_count_independently(self, monkeypatch):
        monkeypatch.setenv("SOCIETY_DAILY_LLM_CAP", "1")
        monkeypatch.setenv("SOCIETY_DAILY_PLACES_CAP", "1")
        b = cost_breaker.CostBreaker()
        assert b.allow("llm") is True and b.allow("places") is True
        assert b.allow("llm") is False and b.allow("places") is False


# ===========================================================================
# 3. Kill-switch
# ===========================================================================

class TestKillSwitch:
    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
    def test_kill_switch_trips_every_class(self, monkeypatch, val):
        monkeypatch.setenv("SOCIETY_PLANNING_DISABLED", val)
        b = cost_breaker.CostBreaker()
        assert b.allow("llm") is False
        assert b.allow("places") is False

    def test_kill_switch_overrides_generous_cap(self, monkeypatch):
        monkeypatch.setenv("SOCIETY_DAILY_LLM_CAP", "9999")
        monkeypatch.setenv("SOCIETY_PLANNING_DISABLED", "1")
        b = cost_breaker.CostBreaker()
        assert b.allow("llm") is False

    def test_kill_switch_off_value_does_not_trip(self, monkeypatch):
        monkeypatch.setenv("SOCIETY_PLANNING_DISABLED", "0")
        b = cost_breaker.CostBreaker()
        assert b.allow("llm") is True


# ===========================================================================
# 4. Malformed cap fails OPEN (never downs judging)
# ===========================================================================

class TestMalformedCapFailsOpen:
    @pytest.mark.parametrize("bad", ["abc", "  ", "1.5", "ten"])
    def test_bad_cap_is_ungated(self, monkeypatch, bad):
        monkeypatch.setenv("SOCIETY_DAILY_LLM_CAP", bad)
        b = cost_breaker.CostBreaker()
        assert b.allow("llm") is True


# ===========================================================================
# 5. LLM path — tripped ⇒ deterministic fallback, no paid call
# ===========================================================================

class TestLLMDegrade:
    def test_llm_call_raises_when_capped_and_never_pays(self, monkeypatch):
        from utils import intent_parser, model_router
        monkeypatch.setattr(intent_parser, "DASHSCOPE_API_KEY", "fake-key")
        monkeypatch.setenv("SOCIETY_DAILY_LLM_CAP", "0")

        def _boom(*a, **kw):
            raise AssertionError("paid DashScope call made despite tripped breaker")
        monkeypatch.setattr(model_router, "dashscope_chat", _boom)

        with pytest.raises(RuntimeError, match="cost breaker"):
            intent_parser._llm_call("5 days in Tokyo")

    def test_parse_intent_degrades_to_deterministic_when_capped(self, monkeypatch):
        """With the LLM breaker tripped, parse_intent must NOT raise and must NOT
        make a paid call — it falls back to the deterministic parser (the SAME path
        used when no key is present)."""
        from utils import intent_parser, model_router
        monkeypatch.setattr(intent_parser, "DASHSCOPE_API_KEY", "fake-key")
        monkeypatch.setenv("SOCIETY_DAILY_LLM_CAP", "0")

        def _boom(*a, **kw):
            raise AssertionError("paid DashScope call made despite tripped breaker")
        monkeypatch.setattr(model_router, "dashscope_chat", _boom)

        out = intent_parser.parse_intent("5 days in Tokyo, $3000", user_id="u1")
        assert isinstance(out, dict)  # graceful, deterministic — no exception, no pay

    def test_llm_call_proceeds_under_cap(self, monkeypatch):
        from utils import intent_parser, model_router
        monkeypatch.setattr(intent_parser, "DASHSCOPE_API_KEY", "fake-key")
        monkeypatch.setenv("SOCIETY_DAILY_LLM_CAP", "5")

        calls = {"n": 0}

        def _ok(_role, _body, timeout=None):
            calls["n"] += 1
            return {"choices": [{"message": {"content": '{"legs": []}'}}]}
        monkeypatch.setattr(model_router, "dashscope_chat", _ok)

        assert intent_parser._llm_call("5 days in Tokyo") == '{"legs": []}'
        assert calls["n"] == 1


# ===========================================================================
# 6. Places path — tripped ⇒ unavailable, no paid HTTP call
# ===========================================================================

class TestPlacesDegrade:
    def test_search_detail_capped_returns_unavailable_no_paid_call(self, monkeypatch):
        from utils import places_card
        monkeypatch.setattr(places_card, "_is_enabled", lambda: True)
        monkeypatch.setenv("SOCIETY_DAILY_PLACES_CAP", "0")

        def _boom(*a, **kw):
            raise AssertionError("paid Places call made despite tripped breaker")
        monkeypatch.setattr(places_card.httpx, "post", _boom)

        res = places_card._search_detail("Hotel X", "Tokyo", "Japan", None, None)
        assert res["status"] == "unavailable"
        assert "cost cap" in res["reason"]

    def test_place_photo_capped_returns_none_no_paid_call(self, monkeypatch):
        from utils import places_card
        monkeypatch.setattr(places_card, "_is_enabled", lambda: True)
        monkeypatch.setattr(places_card, "resolve_photo_name", lambda ref: "places/AbC/photos/xyz")
        monkeypatch.setenv("SOCIETY_DAILY_PLACES_CAP", "0")

        def _boom(*a, **kw):
            raise AssertionError("paid Places photo fetch made despite tripped breaker")
        monkeypatch.setattr(places_card.httpx, "get", _boom)

        assert places_card.fetch_place_photo("opaque-ref-never-on-disk") is None


# ===========================================================================
# 7. Paid GET /place_photo is now rate-limited; cheap/SSE GETs are not
# ===========================================================================

class TestPaidGetRateLimit:
    def _app(self, monkeypatch, per_min):
        monkeypatch.setenv("SOCIETY_RATE_PER_MIN", str(per_min))
        from orchestration import server
        return server.build_app()

    def test_place_photo_get_hits_429(self, monkeypatch):
        app = self._app(monkeypatch, per_min=2)
        client = TestClient(app, raise_server_exceptions=False)
        statuses = [client.get("/place_photo?ref=abc").status_code for _ in range(6)]
        assert 429 in statuses, f"expected a 429 on billable GET burst; got {statuses}"

    def test_health_get_never_rate_limited(self, monkeypatch):
        app = self._app(monkeypatch, per_min=1)
        client = TestClient(app, raise_server_exceptions=False)
        assert all(client.get("/health").status_code == 200 for _ in range(10))

    def test_sse_stream_get_never_rate_limited(self, monkeypatch):
        """GET /stream/* (SSE) must never be throttled — it is cheap and long-lived."""
        app = self._app(monkeypatch, per_min=1)
        client = TestClient(app, raise_server_exceptions=False)
        for _ in range(6):
            r = client.get("/stream/nonexistent-id")
            assert r.status_code != 429


# ===========================================================================
# 8. var-0: the breaker never contaminates the planning digest / cache key
# ===========================================================================

class TestDigestUntouched:
    def test_request_digest_identical_disabled_vs_llm_cap_set(self, monkeypatch):
        """The deterministic request digest must be byte-identical whether the LLM
        breaker is unset (judging) or enabled — the breaker is a request gate, not a
        digest input. (No DASHSCOPE key here, so the deterministic parse is used in
        both cases; the digest must not depend on any breaker env.)"""
        from orchestration import orchestrator

        req = {
            "user_id": "u1",
            "legs": [{"origin": "Tokyo", "destination": "Osaka",
                      "depart_date": "2026-08-01", "nights": 3, "adults": 1}],
            "budget_cents": 300000,
        }
        d_off = orchestrator._request_digest(req)
        monkeypatch.setenv("SOCIETY_DAILY_LLM_CAP", "5")
        monkeypatch.setenv("SOCIETY_DAILY_PLACES_CAP", "5")
        d_on = orchestrator._request_digest(req)
        assert d_off == d_on
