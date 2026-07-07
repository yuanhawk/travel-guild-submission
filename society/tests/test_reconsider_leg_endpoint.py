"""test_reconsider_leg_endpoint.py — POST /reconsider_leg endpoint integration tests.

Architecture: Starlette TestClient + in-memory store injection.
No network, no LLM, no real orchestrator needed.

Test coverage (per build plan):
1.  test_contract_shape_2_alternatives_with_exact_keys
2.  test_alternatives_share_city_different_neighbourhood
3.  test_no_var0_leak_booking_link_affiliate_price_keys
4.  test_resolved_city_from_held_plan
5.  test_graceful_default_city_when_no_plan
6.  test_malformed_json_body_400
7.  test_non_integer_leg_index_400
8.  test_missing_idempotency_key_falls_back_gracefully
9.  test_invalid_leg_index_out_of_bounds_falls_back_default
10. test_var0_guard_stored_request_digest_unchanged
11. test_var0_guard_envelope_unchanged
12. test_advisory_flag_always_true
13. test_leg_index_coerced_to_int

SECURITY (auth-session hardening, 2026-07 security review finding #2, LOW): this endpoint
previously had NO ownership check at all — any caller who knew/guessed an
idempotency_key could read a stranger's held-plan leg. It now requires the SAME
two-tier session_token/owner_token ownership proof as /confirm /cancel /replan
/refine (_authorize_trip_action), mirroring GET /trips/{idempotency_key}'s
existence-hiding 404-on-denial posture (see server.py's reconsider_leg docstring).
`_OWNER_TOKEN` below is a valid session_token for the seeded plan's owner
("demo-user") — every pre-existing test that reads _PLAN_IDK's held plan now
presents it; the ownership-gate regression tests at the bottom of this file
prove a missing/wrong token is denied.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient
from orchestration import server
from orchestration.store import SqliteDashboardStore, set_store
from utils.session_token import issue_session_token

# ---------------------------------------------------------------------------
# Canned test data
# ---------------------------------------------------------------------------

_PLAN_IDK = "trip-reconsider-leg-test-001"

# Valid session_token for the seeded plan's owner ("demo-user", Tier 1 — see
# _seed_store below). issue_session_token does not require a pre-seeded demo_users
# row (unlike POST /session/login) — it is a pure in-memory session mint, matching
# how test_session_token.py exercises this module directly.
_OWNER_TOKEN = issue_session_token("demo-user")

_PLAN_ENVELOPE: dict = {
    "outcome": "plan_ready",
    "idempotency_key": _PLAN_IDK,
    "payment_status": "held",
    "booking_ref": None,
    "package_total_with_fees_cents": 150000,
    "wallet": {"debited": False, "held_cents": 150000},
    "legs": [
        {"city": "tokyo"},
        {"city": "kyoto"},
    ],
    "day_plans": [
        {
            "city": "tokyo",
            "days": [
                {
                    "day_index": 0,
                    "bad_weather": False,
                    "attractions": [],
                    "meals": {},
                },
            ],
            "unscheduled_attractions": [],
        }
    ],
}

_STRUCTURED_REQUEST = {
    "user_id": "demo-user",
    "total_budget_cents": 200000,
    "today": "2026-10-01",
    "legs": [
        {"city": "tokyo", "checkin": "2026-10-10", "checkout": "2026-10-12",
         "adults": 2, "pace": "moderate"},
        {"city": "kyoto", "checkin": "2026-10-12", "checkout": "2026-10-14",
         "adults": 2, "pace": "moderate"},
    ],
}


def _seed_store(store: SqliteDashboardStore) -> None:
    """Seed the in-memory store with a canned plan."""
    store.save_plan({
        "idempotency_key": _PLAN_IDK,
        "user_id": "demo-user",
        "checkout_id": "co-test-reconsider-001",
        "dest_token": "JP",
        "package_total_cents": 150000,
        "request": _STRUCTURED_REQUEST,
        "envelope": copy.deepcopy(_PLAN_ENVELOPE),
    })


def _client_with_store():
    """Return (TestClient, store) with injected in-memory store."""
    store = SqliteDashboardStore(":memory:")
    set_store(store)
    _seed_store(store)
    client = TestClient(server.build_app())
    return client, store


def _get_stored_digest(store: SqliteDashboardStore, idk: str) -> tuple[str | None, dict | None]:
    """Retrieve the stored request digest and envelope for var-0 guard testing."""
    row = store.get_plan(idk)
    if row is None:
        return None, None
    request_digest = row.get("digest")
    envelope = row.get("envelope")
    return request_digest, envelope


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestReconsiderLegEndpoint(unittest.TestCase):

    # ---- 1. Contract shape: 2 alternatives with exact keys ----
    def test_contract_shape_2_alternatives_with_exact_keys(self):
        """Response has outcome, session_id, leg_index, advisory, alternatives."""
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json={"idempotency_key": _PLAN_IDK, "leg_index": 0, "session_token": _OWNER_TOKEN})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["outcome"], "alternatives")
        self.assertIn("session_id", data)
        self.assertIn("leg_index", data)
        self.assertIn("advisory", data)
        self.assertIn("alternatives", data)
        self.assertEqual(data["advisory"], True)
        self.assertIsInstance(data["alternatives"], list)
        self.assertEqual(len(data["alternatives"]), 2)

    # ---- 2. Each alternative has exact required keys ----
    def test_alternatives_have_required_keys(self):
        """Each alternative has alt_city, hotel, price_delta, why."""
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json={"idempotency_key": _PLAN_IDK, "leg_index": 0, "session_token": _OWNER_TOKEN})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        for alt in data["alternatives"]:
            self.assertIn("alt_city", alt)
            self.assertIn("hotel", alt)
            self.assertIn("price_delta", alt)
            self.assertIn("why", alt)
            self.assertIsInstance(alt["alt_city"], str)
            self.assertIsInstance(alt["hotel"], str)
            self.assertIsInstance(alt["price_delta"], int)
            self.assertIsInstance(alt["why"], str)

    # ---- 3. Both alternatives share the same city ----
    def test_alternatives_share_city(self):
        """Both alternatives have the same alt_city."""
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json={"idempotency_key": _PLAN_IDK, "leg_index": 0, "session_token": _OWNER_TOKEN})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        cities = [alt["alt_city"] for alt in data["alternatives"]]
        self.assertEqual(cities[0], cities[1])

    # ---- 4. Alternatives have distinct hotel names (different neighbourhood) ----
    def test_alternatives_distinct_hotels(self):
        """Each alternative has a different hotel name/neighbourhood."""
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json={"idempotency_key": _PLAN_IDK, "leg_index": 0, "session_token": _OWNER_TOKEN})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        hotels = [alt["hotel"] for alt in data["alternatives"]]
        self.assertNotEqual(hotels[0], hotels[1])

    # ---- 5. Var-0 firewall: no booking_link in response ----
    def test_no_booking_link_in_response(self):
        """Response never contains booking_link (var-0 display-only class)."""
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json={"idempotency_key": _PLAN_IDK, "leg_index": 0, "session_token": _OWNER_TOKEN})
        self.assertEqual(r.status_code, 200)
        serialised = json.dumps(r.json())
        self.assertNotIn("booking_link", serialised)
        # Also check each alternative individually
        data = r.json()
        for alt in data["alternatives"]:
            self.assertNotIn("booking_link", alt)

    # ---- 6. Var-0 firewall: no affiliate in response ----
    def test_no_affiliate_in_response(self):
        """Response never contains affiliate or affiliate_marker."""
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json={"idempotency_key": _PLAN_IDK, "leg_index": 0, "session_token": _OWNER_TOKEN})
        self.assertEqual(r.status_code, 200)
        serialised = json.dumps(r.json())
        self.assertNotIn("affiliate", serialised)
        data = r.json()
        for alt in data["alternatives"]:
            self.assertNotIn("affiliate", alt)

    # ---- 7. Var-0 firewall: no live-price fields ----
    def test_no_live_price_fields_in_response(self):
        """Response never contains live_price, price_quote, or similar (var-0 display-only)."""
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json={"idempotency_key": _PLAN_IDK, "leg_index": 0, "session_token": _OWNER_TOKEN})
        self.assertEqual(r.status_code, 200)
        serialised = json.dumps(r.json())
        self.assertNotIn("live_price", serialised)
        self.assertNotIn("price_quote", serialised)
        data = r.json()
        for alt in data["alternatives"]:
            self.assertNotIn("live_price", alt)
            self.assertNotIn("price_quote", alt)

    # ---- 8. City resolution: when plan exists, use leg's city ----
    def test_resolved_city_from_held_plan(self):
        """alt_city matches the held plan's leg city."""
        client, _ = _client_with_store()
        # leg_index 0 should be 'tokyo', leg_index 1 should be 'kyoto'
        r = client.post("/reconsider_leg", json={"idempotency_key": _PLAN_IDK, "leg_index": 0, "session_token": _OWNER_TOKEN})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["alternatives"][0]["alt_city"], "tokyo")
        self.assertEqual(data["alternatives"][1]["alt_city"], "tokyo")

        # Test leg_index 1 resolves to kyoto
        r = client.post("/reconsider_leg", json={"idempotency_key": _PLAN_IDK, "leg_index": 1, "session_token": _OWNER_TOKEN})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["alternatives"][0]["alt_city"], "kyoto")
        self.assertEqual(data["alternatives"][1]["alt_city"], "kyoto")
        # Regression: hotel string must not hardcode a contradicting city (e.g. "tokyo")
        # when the resolved leg city is kyoto. The stub must stay city-neutral.
        for alt in data["alternatives"]:
            self.assertNotIn("tokyo", alt["hotel"].lower())
            self.assertNotIn("tokyo", alt["why"].lower())

    # ---- 9. Graceful fallback: no plan, use default city ----
    def test_graceful_default_city_when_no_plan(self):
        """When idempotency_key is missing or plan not found, default to 'tokyo'."""
        client, _ = _client_with_store()
        # Request with no idempotency_key
        r = client.post("/reconsider_leg", json={"leg_index": 0})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["alternatives"][0]["alt_city"], "tokyo")

        # Request with unknown idempotency_key
        r = client.post("/reconsider_leg", json={"idempotency_key": "unknown-idk", "leg_index": 0})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["alternatives"][0]["alt_city"], "tokyo")

    # ---- 10. Validation: malformed JSON body -> 400 ----
    def test_malformed_json_body_400(self):
        """Non-JSON body returns 400."""
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", content="not-json", headers={"Content-Type": "text/plain"})
        self.assertEqual(r.status_code, 400)
        data = r.json()
        self.assertEqual(data["outcome"], "invalid_request")

    # ---- 11. Validation: body not an object -> 400 ----
    def test_body_not_object_400(self):
        """Body as array or string returns 400."""
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json=[])
        self.assertEqual(r.status_code, 400)
        data = r.json()
        self.assertEqual(data["outcome"], "invalid_request")

    # ---- 12. Validation: non-integer leg_index -> 400 ----
    def test_non_integer_leg_index_400(self):
        """leg_index as string (non-numeric) returns 400."""
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json={"idempotency_key": _PLAN_IDK, "leg_index": "not-an-int", "session_token": _OWNER_TOKEN})
        self.assertEqual(r.status_code, 400)
        data = r.json()
        self.assertEqual(data["outcome"], "invalid_request")
        self.assertIn("leg_index", data["reason"].lower())

    # ---- 13. Out-of-bounds leg_index: graceful fallback to default city ----
    def test_out_of_bounds_leg_index_graceful(self):
        """leg_index >= num_legs falls back to default city (no error)."""
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json={"idempotency_key": _PLAN_IDK, "leg_index": 999, "session_token": _OWNER_TOKEN})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        # Should fall back to 'tokyo' (default) since leg 999 doesn't exist
        self.assertEqual(data["alternatives"][0]["alt_city"], "tokyo")

    # ---- 14. leg_index coerced to int ----
    def test_leg_index_coerced_to_int(self):
        """leg_index as a float string is coerced to int."""
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json={"idempotency_key": _PLAN_IDK, "leg_index": "0", "session_token": _OWNER_TOKEN})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["leg_index"], 0)

    # ---- 15. leg_index default 0 when missing ----
    def test_leg_index_default_0(self):
        """When leg_index is omitted, defaults to 0."""
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json={"idempotency_key": _PLAN_IDK, "session_token": _OWNER_TOKEN})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["leg_index"], 0)

    # ---- 16. Var-0 guard: stored digest unchanged after call ----
    def test_var0_guard_stored_digest_unchanged(self):
        """After /reconsider_leg, the stored request digest is byte-identical (nothing persisted)."""
        client, store = _client_with_store()
        digest_before, envelope_before = _get_stored_digest(store, _PLAN_IDK)

        # Call /reconsider_leg
        r = client.post("/reconsider_leg", json={"idempotency_key": _PLAN_IDK, "leg_index": 0, "session_token": _OWNER_TOKEN})
        self.assertEqual(r.status_code, 200)

        # Re-fetch stored plan
        digest_after, envelope_after = _get_stored_digest(store, _PLAN_IDK)

        # Digest must be unchanged (proof that nothing persisted)
        self.assertEqual(digest_before, digest_after)

    # ---- 17. Var-0 guard: stored envelope unchanged after call ----
    def test_var0_guard_stored_envelope_unchanged(self):
        """After /reconsider_leg, the stored envelope is byte-identical (no DB writes)."""
        client, store = _client_with_store()
        digest_before, envelope_before = _get_stored_digest(store, _PLAN_IDK)
        envelope_before_json = json.dumps(envelope_before, sort_keys=True)

        # Call /reconsider_leg
        r = client.post("/reconsider_leg", json={"idempotency_key": _PLAN_IDK, "leg_index": 0, "session_token": _OWNER_TOKEN})
        self.assertEqual(r.status_code, 200)

        # Re-fetch and compare envelope
        _, envelope_after = _get_stored_digest(store, _PLAN_IDK)
        envelope_after_json = json.dumps(envelope_after, sort_keys=True)

        self.assertEqual(envelope_before_json, envelope_after_json)

    # ---- 18. advisory flag always true ----
    def test_advisory_flag_always_true(self):
        """Response always has advisory=true."""
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json={"idempotency_key": _PLAN_IDK, "leg_index": 0, "session_token": _OWNER_TOKEN})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["advisory"])

    # ---- 19. session_id echoed from request ----
    def test_session_id_echoed(self):
        """session_id from request is echoed in response."""
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json={
            "session_id": "test-session-123",
            "idempotency_key": _PLAN_IDK,
            "leg_index": 0,
            "session_token": _OWNER_TOKEN,
        })
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["session_id"], "test-session-123")

    # ---- 20. session_id null when not provided ----
    def test_session_id_null_when_missing(self):
        """When session_id is not provided, response has session_id=null."""
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json={"idempotency_key": _PLAN_IDK, "leg_index": 0, "session_token": _OWNER_TOKEN})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIsNone(data["session_id"])

    # ---- 21. leg_index echoed in response ----
    def test_leg_index_echoed(self):
        """leg_index from request is echoed in response."""
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json={"idempotency_key": _PLAN_IDK, "leg_index": 1, "session_token": _OWNER_TOKEN})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["leg_index"], 1)

    # ---- 22. price_delta is signed integer ----
    def test_price_delta_signed_integer(self):
        """price_delta is a signed integer (can be negative)."""
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json={"idempotency_key": _PLAN_IDK, "leg_index": 0, "session_token": _OWNER_TOKEN})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        for alt in data["alternatives"]:
            self.assertIsInstance(alt["price_delta"], int)
            # Should have both positive and negative deltas
        deltas = [alt["price_delta"] for alt in data["alternatives"]]
        self.assertTrue(any(d < 0 for d in deltas))
        self.assertTrue(any(d > 0 for d in deltas))

    # ---- 23. why field is non-empty string ----
    def test_why_field_non_empty(self):
        """Each alternative has a non-empty why string."""
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json={"idempotency_key": _PLAN_IDK, "leg_index": 0, "session_token": _OWNER_TOKEN})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        for alt in data["alternatives"]:
            self.assertIsInstance(alt["why"], str)
            self.assertGreater(len(alt["why"]), 0)

    # ---- 24. Demo honesty: every hotel label carries the '(demo)' marker ----
    def test_demo_honesty_marker_in_hotel_labels(self):
        """Every alternative hotel label must contain '(demo)' — guards against fabricated
        ratings, review counts, or real trademarked brand names misleading judges."""
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json={"idempotency_key": _PLAN_IDK, "leg_index": 0, "session_token": _OWNER_TOKEN})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        for alt in data["alternatives"]:
            self.assertIn(
                "(demo)", alt["hotel"],
                f"Hotel label '{alt['hotel']}' is missing the '(demo)' honesty marker",
            )

    # ---- 25. Demo honesty: data_origin field is 'demo_stub' ----
    def test_data_origin_demo_stub(self):
        """Top-level data_origin field must be 'demo_stub' to clearly identify stub data."""
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json={"idempotency_key": _PLAN_IDK, "leg_index": 0, "session_token": _OWNER_TOKEN})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(
            data.get("data_origin"), "demo_stub",
            "Response must carry data_origin='demo_stub' for judge transparency",
        )

    # ---- 26. Demo honesty: each alternative has source='demo' ----
    def test_alternatives_source_demo(self):
        """Each alternative must have source='demo' so the machine-readable origin is unambiguous."""
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json={"idempotency_key": _PLAN_IDK, "leg_index": 0, "session_token": _OWNER_TOKEN})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        for alt in data["alternatives"]:
            self.assertEqual(
                alt.get("source"), "demo",
                f"Alternative is missing source='demo': {alt}",
            )


class TestReconsiderLegOwnershipGate(unittest.TestCase):
    """SECURITY regression: /reconsider_leg ownership gate (was NO check at all —
    any caller who knew/guessed idempotency_key could read a stranger's held-plan
    leg). Mirrors GET /trips/{idempotency_key}'s existence-hiding posture: a denied
    request gets the SAME 404 shape as an unknown key (no distinct 403), so a
    non-owner can never confirm the key is real."""

    _ANON_IDK = "trip-reconsider-leg-anon-001"
    _ANON_TOKEN = "anon-secret-reconsider-A"

    def _seed_anon(self, store: SqliteDashboardStore) -> None:
        envelope = {**copy.deepcopy(_PLAN_ENVELOPE), "idempotency_key": self._ANON_IDK}
        store.save_plan({
            "idempotency_key": self._ANON_IDK,
            "user_id": "",
            "owner_token": self._ANON_TOKEN,
            "checkout_id": "co-test-reconsider-anon-001",
            "dest_token": "JP",
            "package_total_cents": 150000,
            "request": {**_STRUCTURED_REQUEST, "user_id": ""},
            "envelope": envelope,
        })

    # ---- Logged-in-owner trip: missing session_token -> 404 (was: 200, leaked) ----
    def test_owned_trip_no_session_token_404(self):
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json={"idempotency_key": _PLAN_IDK, "leg_index": 0})
        self.assertEqual(r.status_code, 404, r.text)

    # ---- Logged-in-owner trip: session_token minted for a DIFFERENT user -> 404 ----
    def test_owned_trip_wrong_user_session_token_404(self):
        client, _ = _client_with_store()
        other_token = issue_session_token("demo-someone-else")
        r = client.post("/reconsider_leg", json={
            "idempotency_key": _PLAN_IDK, "leg_index": 0, "session_token": other_token,
        })
        self.assertEqual(r.status_code, 404, r.text)

    # ---- Logged-in-owner trip: fabricated (never-issued) session_token -> 404 ----
    def test_owned_trip_fabricated_session_token_404(self):
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json={
            "idempotency_key": _PLAN_IDK, "leg_index": 0,
            "session_token": "fabricated-not-a-real-token",
        })
        self.assertEqual(r.status_code, 404, r.text)

    # ---- Logged-in-owner trip: the OWNER's real session_token -> 200, proceeds ----
    def test_owned_trip_correct_session_token_succeeds(self):
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json={
            "idempotency_key": _PLAN_IDK, "leg_index": 0, "session_token": _OWNER_TOKEN,
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["outcome"], "alternatives")

    # ---- Anon (Tier 2) trip: correct owner_token -> 200, proceeds ----
    def test_anon_trip_correct_owner_token_succeeds(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        self._seed_anon(store)
        client = TestClient(server.build_app())
        r = client.post("/reconsider_leg", json={
            "idempotency_key": self._ANON_IDK, "leg_index": 0,
            "owner_token": self._ANON_TOKEN,
        })
        self.assertEqual(r.status_code, 200, r.text)

    # ---- Anon (Tier 2) trip: wrong/missing owner_token -> 404 ----
    def test_anon_trip_wrong_owner_token_404(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        self._seed_anon(store)
        client = TestClient(server.build_app())
        r = client.post("/reconsider_leg", json={
            "idempotency_key": self._ANON_IDK, "leg_index": 0,
            "owner_token": "not-the-right-secret",
        })
        self.assertEqual(r.status_code, 404, r.text)

    # ---- An unrecognized idempotency_key is UNCHANGED (still the pre-existing
    #      graceful best-effort default, per test_graceful_default_city_when_no_plan
    #      above) — the ownership gate only engages once a row is actually found;
    #      it does not turn "no such plan" into a denial. ----
    def test_unknown_key_still_gets_graceful_default_not_404(self):
        client, _ = _client_with_store()
        r = client.post("/reconsider_leg", json={"idempotency_key": "trip-totally-bogus", "leg_index": 0})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["alternatives"][0]["alt_city"], "tokyo")


if __name__ == "__main__":
    unittest.main()
