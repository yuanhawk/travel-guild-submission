"""test_replan_endpoint.py — POST /replan endpoint integration tests.

Architecture: Starlette TestClient + in-memory store injection.
No network, no LLM, no real orchestrator needed.

Test coverage (per build plan):
1.  test_missing_idempotency_key_400
2.  test_empty_ops_400
3.  test_unknown_plan_returns_unknown_plan
4.  test_booked_plan_returns_plan_locked
5.  test_remove_place_moves_to_unscheduled
6.  test_add_place_from_unscheduled_inserts_at_position
7.  test_add_place_not_in_pool_is_rejected_not_fabricated  (HONESTY)
8.  test_swap_place_atomic
9.  test_switch_variant_fair_wet_is_reversible
10. test_switch_variant_on_clear_day_rejected
11. test_reflow_from_fills_gap_pace_capped
12. test_stale_position_ref_mismatch_rejected
13. test_replan_never_books_or_debits
14. test_replan_preserves_idempotency_key_and_price
15. test_replan_does_not_touch_request_digest (var-0)
16. test_persisted_envelope_roundtrips
17. test_noop_all_rejected_returns_previous_plan

SECURITY (IDOR fix, was VULN-AUTH-002 CVSS 8.2 HIGH): /replan now REQUIRES the
two-tier session_token/owner_token ownership proof (see server.py's
_authorize_trip_action). TestReplanOwnership below is the regression lock —
mirrors test_prefs_session.py's `_login` + wrong-user pattern, but the gate
returns 403 `not_trip_owner` (not 401 — see server.py's `_forbidden`).

MIGRATION NOTE: every fixture below used to seed its trip under a bare
`"user_id": "demo-user"` — a string that was never a real pre-seeded demo
user, so no session_token could ever be minted for it (POST /session/login
404s on an unknown user_id). Post-fix, that row became un-actionable (any
/replan call against it would 403 unconditionally). Fixed by re-seeding it
ANONYMOUSLY (`user_id=""`) with an explicit `owner_token` bound at creation
(`_ANON_OWNER_TOKEN`), and threading that same owner_token into every /replan
call — per the IDOR fix's tier-2 (anonymous) ownership model.
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

# ---------------------------------------------------------------------------
# Canned test data
# ---------------------------------------------------------------------------

_PLAN_IDK = "trip-replan-test-0001"

_ATT_A = {
    "name": "Senso-ji Temple", "name_en": "Senso-ji", "category": "temple",
    "wikidata": "Q844240", "lat": 35.71, "lon": 139.79, "provenance": "test",
}
_ATT_B = {
    "name": "Ueno Park", "name_en": "Ueno Park", "category": "park",
    "wikidata": None, "lat": 35.71, "lon": 139.77, "provenance": "test",
}
_ATT_C = {
    "name": "Tokyo Tower", "name_en": "Tokyo Tower", "category": "observation",
    "wikidata": None, "lat": 35.65, "lon": 139.74, "provenance": "test",
}
_ATT_D = {
    "name": "Tokyo Skytree", "name_en": "Tokyo Skytree", "category": "observation",
    "wikidata": None, "lat": 35.71, "lon": 139.81, "provenance": "test",
}
_ATT_FAIR = {
    "name": "Shinjuku Gyoen", "name_en": "Shinjuku Gyoen", "category": "garden",
    "wikidata": None, "lat": 35.68, "lon": 139.71, "provenance": "test",
}
_ATT_E = {
    "name": "Meiji Shrine", "name_en": "Meiji Shrine", "category": "shrine",
    "wikidata": None, "lat": 35.67, "lon": 139.70, "provenance": "test",
}
_ATT_F = {
    "name": "Akihabara", "name_en": "Akihabara", "category": "district",
    "wikidata": None, "lat": 35.70, "lon": 139.77, "provenance": "test",
}
_ATT_G = {
    "name": "Odaiba", "name_en": "Odaiba", "category": "district",
    "wikidata": None, "lat": 35.62, "lon": 139.78, "provenance": "test",
}
_ATT_FAIR2 = {
    "name": "Nezu Museum", "name_en": "Nezu Museum", "category": "museum",
    "wikidata": None, "lat": 35.66, "lon": 139.71, "provenance": "test",
}

# A plan with 1 leg, 2 days; day 0 has [A, C], day 1 has []; unscheduled=[B, D].
# Day 0 is a bad-weather day with fair_weather_attractions=[FAIR].
_PLAN_ENVELOPE: dict = {
    "outcome": "plan_ready",
    "idempotency_key": _PLAN_IDK,
    "payment_status": "held",
    "booking_ref": None,
    "package_total_with_fees_cents": 150000,
    "wallet": {"debited": False, "held_cents": 150000},
    "legs": [{"city": "tokyo"}],
    "day_plans": [
        {
            "city": "tokyo",
            "days": [
                {
                    "day_index": 0,
                    "bad_weather": True,
                    "attractions": [copy.deepcopy(_ATT_A), copy.deepcopy(_ATT_C)],
                    "fair_weather_attractions": [copy.deepcopy(_ATT_FAIR)],
                    "meals": {},
                },
                {
                    "day_index": 1,
                    "bad_weather": False,
                    "attractions": [],
                    "meals": {},
                },
            ],
            "unscheduled_attractions": [copy.deepcopy(_ATT_B), copy.deepcopy(_ATT_D)],
        }
    ],
}

_STRUCTURED_REQUEST = {
    "user_id": "",   # IDOR fix migration: anonymous, not a bare non-seeded string
    "total_budget_cents": 200000,
    "today": "2026-10-01",
    "legs": [
        {"city": "tokyo", "checkin": "2026-10-10", "checkout": "2026-10-12",
         "adults": 2, "pace": "moderate"},
    ],
}

# IDOR fix (tier 2, anonymous): _PLAN_IDK is seeded ANONYMOUSLY (user_id="")
# and bound to this owner_token at creation — every /replan call that acts on
# it must present this SAME owner_token to pass the ownership gate.
_ANON_OWNER_TOKEN = "test-owner-token-replan-0001"


def _seed_store(store: SqliteDashboardStore, *, status: str = "plan_ready") -> None:
    store.save_plan({
        "idempotency_key": _PLAN_IDK,
        "user_id": "",
        "owner_token": _ANON_OWNER_TOKEN,
        "checkout_id": "co-test-cancel-001",
        "dest_token": "JP",
        "package_total_cents": 150000,
        "request": _STRUCTURED_REQUEST,
        "envelope": copy.deepcopy(_PLAN_ENVELOPE),
    })
    if status == "booked":
        store.mark_booked(
            _PLAN_IDK, booking_ref="BK-test-001",
            envelope=copy.deepcopy(_PLAN_ENVELOPE),
            confirmed_at="2026-01-01T00:00:00+00:00",
        )


def _client_with_store(*, status: str = "plan_ready"):
    """Return (TestClient, store) with injected in-memory store."""
    store = SqliteDashboardStore(":memory:")
    set_store(store)
    _seed_store(store, status=status)
    client = TestClient(server.build_app())
    return client, store


def _login(c, user_id: str) -> str:
    """POST /session/login for a KNOWN demo user_id -> its session_token.
    Mirrors test_prefs_session.py's `_login` helper. Used by TestReplanOwnership
    (the tier-1 logged-in-trip ownership cases)."""
    r = c.post("/session/login", json={"user_id": user_id})
    assert r.status_code == 200, r.text
    return r.json()["session_token"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestReplanEndpoint(unittest.TestCase):

    # ---- 1. Validation: missing idempotency_key ----
    def test_missing_idempotency_key_400(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        with TestClient(server.build_app()) as client:
            r = client.post("/replan", json={"ops": [{"op": "remove_place"}]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["outcome"], "invalid_request")

    # ---- 2. Validation: empty ops ----
    def test_empty_ops_400(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        with TestClient(server.build_app()) as client:
            r = client.post("/replan", json={"idempotency_key": _PLAN_IDK, "ops": []})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["outcome"], "invalid_request")

    # ---- 3. Unknown idempotency_key ----
    def test_unknown_plan_returns_unknown_plan(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        with TestClient(server.build_app()) as client:
            r = client.post("/replan", json={
                "idempotency_key": "trip-does-not-exist",
                "ops": [{"op": "remove_place", "leg_index": 0, "day_index": 0,
                          "position": 0, "attraction_ref": "Senso-ji"}],
            })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["outcome"], "unknown_plan")

    # ---- 4. Booked plan IS editable (POI rearrange only); money + booking preserved ----
    def test_booked_plan_editable_money_and_status_preserved(self):
        """A BOOKED trip can be re-planned (the post-booking wet-weather switch / in-trip
        edit): ops rearrange only UNPRICED POIs, so status stays 'booked', booking_ref +
        the package price are untouched, and the trip is NOT reverted to plan_ready."""
        client, store = _client_with_store(status="booked")
        with client:
            r = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK,
                "owner_token": _ANON_OWNER_TOKEN,
                "ops": [{"op": "switch_variant", "leg_index": 0, "day_index": 0,
                          "variant": "fair"}],
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["outcome"], "plan_ready")     # edit applied
        self.assertTrue(body["applied"])                     # the op took effect
        # The stored trip is STILL booked — money/booking columns preserved.
        row = store.get_plan(_PLAN_IDK)
        self.assertEqual(row["status"], "booked")
        self.assertEqual(row["booking_ref"], "BK-test-001")
        self.assertEqual(row["package_total_cents"], 150000)
        # Envelope money fields byte-identical to the fixture (held/None/150000).
        self.assertEqual(row["envelope"].get("payment_status"), "held")
        self.assertEqual(row["envelope"].get("booking_ref"), None)
        self.assertEqual(row["envelope"].get("package_total_with_fees_cents"), 150000)

    # ---- 4b. CANCELLED plan stays locked ----
    def test_cancelled_plan_returns_plan_locked(self):
        client, store = _client_with_store(status="booked")
        store.mark_cancelled(_PLAN_IDK, refunded_cents=150000)
        with client:
            r = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK,
                "owner_token": _ANON_OWNER_TOKEN,
                "ops": [{"op": "switch_variant", "leg_index": 0, "day_index": 0,
                          "variant": "fair"}],
            })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["outcome"], "plan_locked")

    # ---- 5. remove_place moves attraction to unscheduled ----
    def test_remove_place_moves_to_unscheduled(self):
        client, store = _client_with_store()
        with client:
            r = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK,
                "owner_token": _ANON_OWNER_TOKEN,
                "ops": [{"op": "remove_place", "leg_index": 0, "day_index": 0,
                          "position": 0, "attraction_ref": "Senso-ji"}],
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["outcome"], "plan_ready")
        self.assertEqual(len(body["applied"]), 1)
        # Day 0 should now only have [C]
        leg = body["plan"]["day_plans"][0]
        day0_atts = leg["days"][0]["attractions"]
        names = [a.get("name_en") or a.get("name") for a in day0_atts]
        self.assertNotIn("Senso-ji", names, "Senso-ji must be removed from day 0")
        # Senso-ji must appear in unscheduled
        unatts = [a.get("name_en") or a.get("name") for a in leg["unscheduled_attractions"]]
        self.assertIn("Senso-ji", unatts, "Senso-ji must be moved to unscheduled")

    # ---- 6. add_place from unscheduled inserts at position ----
    def test_add_place_from_unscheduled_inserts_at_position(self):
        client, store = _client_with_store()
        with client:
            r = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK,
                "owner_token": _ANON_OWNER_TOKEN,
                "ops": [{"op": "add_place", "leg_index": 0, "day_index": 1,
                          "position": 0, "attraction_ref": "Ueno Park", "from": "unscheduled"}],
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["outcome"], "plan_ready")
        leg = body["plan"]["day_plans"][0]
        day1_atts = leg["days"][1]["attractions"]
        names = [a.get("name_en") or a.get("name") for a in day1_atts]
        self.assertIn("Ueno Park", names)
        # Ueno Park must no longer be in unscheduled
        unatts = [a.get("name_en") or a.get("name") for a in leg["unscheduled_attractions"]]
        self.assertNotIn("Ueno Park", unatts)

    # ---- 6b. item_transport_gaps recompute after a structural edit (BUG FIX regression) ----
    def test_add_place_recomputes_item_transport_gaps_not_stale(self):
        """POST /replan must recompute item_transport_gaps for any day whose
        attractions were touched by an applied op. Before the fix,
        attach_intracity_transport's idempotent ("only if absent") guard meant
        the ORIGINAL /negotiate-computed gaps stayed in the envelope forever —
        a drag-add (or a drag-move, which is remove_place+add_place) would
        leave the frontend showing stale distance/mode chips for the wrong
        item pairs. force=True on the post-apply_ops recompute fixes this."""
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        env = copy.deepcopy(_PLAN_ENVELOPE)
        # Pre-seed day 0 with a deliberately WRONG placeholder, simulating what
        # /negotiate computed for the OLD 2-item [Senso-ji, Tokyo Tower] order —
        # if the endpoint fails to recompute, this exact stale value survives.
        env["day_plans"][0]["days"][0]["item_transport_gaps"] = [
            {"mode": "taxi", "minutes": 999, "estimate": True},
        ]
        store.save_plan({
            "idempotency_key": _PLAN_IDK,
            "user_id": "",
            "owner_token": _ANON_OWNER_TOKEN,
            "checkout_id": "co-test-cancel-001",
            "dest_token": "JP",
            "package_total_cents": 150000,
            "request": _STRUCTURED_REQUEST,
            "envelope": env,
        })
        with TestClient(server.build_app()) as client:
            r = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK,
                "owner_token": _ANON_OWNER_TOKEN,
                "ops": [{"op": "add_place", "leg_index": 0, "day_index": 0,
                          "position": 1, "attraction_ref": "Ueno Park", "from": "unscheduled"}],
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["outcome"], "plan_ready")
        day0 = body["plan"]["day_plans"][0]["days"][0]
        # Day 0 is now [Senso-ji, Ueno Park, Tokyo Tower] — 3 items, no meals -> 2 gaps.
        names = [a.get("name_en") or a.get("name") for a in day0["attractions"]]
        self.assertEqual(names, ["Senso-ji", "Ueno Park", "Tokyo Tower"])
        gaps = day0.get("item_transport_gaps")
        self.assertIsNotNone(gaps)
        self.assertEqual(
            len(gaps), 2,
            "a 3-item day with no meals must produce 2 inline gaps, not the stale 1-gap value",
        )
        for g in gaps:
            self.assertNotEqual(g, {"mode": "taxi", "minutes": 999, "estimate": True},
                                 "stale placeholder gap must not survive the edit")
        # Cross-check against the pure builder run directly on the NEW attraction order.
        from utils.intracity_transport import build_item_transport_gaps
        expected = build_item_transport_gaps(day0, {}, "tokyo")
        self.assertEqual(gaps, expected)

    # ---- 6c. intracity_hops recompute after a structural edit (BUG FIX regression, cd92dd4 —
    #          the OTHER half of the force-recompute; 6b only locked item_transport_gaps) ----
    def test_add_place_recomputes_intracity_hops_not_stale(self):
        """Same bug as 6b, other field. Before the fix, attach_intracity_transport's
        idempotent guard meant a day's `intracity_hops` (hotel->attractions->meals->hotel)
        kept whatever /negotiate originally computed for the OLD [Senso-ji, Tokyo Tower]
        order forever. Reverting just the intracity_hops half of force=True (i.e.
        `force or "intracity_hops" not in day` back to `"intracity_hops" not in day`)
        must fail this test — it previously passed the whole suite with that half
        reverted, which is the gap this test closes."""
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        env = copy.deepcopy(_PLAN_ENVELOPE)
        # Deliberately WRONG placeholder standing in for whatever /negotiate computed
        # for the OLD 2-item order — must NOT survive a structural edit.
        env["day_plans"][0]["days"][0]["intracity_hops"] = [
            {"from": "STALE", "to": "PLACEHOLDER", "mode": "taxi", "minutes": 999},
        ]
        store.save_plan({
            "idempotency_key": _PLAN_IDK,
            "user_id": "",
            "owner_token": _ANON_OWNER_TOKEN,
            "checkout_id": "co-test-cancel-001",
            "dest_token": "JP",
            "package_total_cents": 150000,
            "request": _STRUCTURED_REQUEST,
            "envelope": env,
        })
        with TestClient(server.build_app()) as client:
            r = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK,
                "owner_token": _ANON_OWNER_TOKEN,
                "ops": [{"op": "add_place", "leg_index": 0, "day_index": 0,
                          "position": 1, "attraction_ref": "Ueno Park", "from": "unscheduled"}],
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["outcome"], "plan_ready")
        day0 = body["plan"]["day_plans"][0]["days"][0]
        names = [a.get("name_en") or a.get("name") for a in day0["attractions"]]
        self.assertEqual(names, ["Senso-ji", "Ueno Park", "Tokyo Tower"])
        hops = day0.get("intracity_hops")
        self.assertIsNotNone(hops)
        for h in hops:
            self.assertNotEqual(
                h, {"from": "STALE", "to": "PLACEHOLDER", "mode": "taxi", "minutes": 999},
                "stale placeholder hop must not survive the edit",
            )
        # Cross-check against the pure builder run directly on the NEW attraction order.
        from utils.intracity_transport import build_intracity_hops
        expected = build_intracity_hops(day0, {}, "tokyo")
        self.assertEqual(hops, expected)

    # ---- 7. add_place with ref not in pool → REJECTED, not fabricated (HONESTY) ----
    def test_add_place_not_in_pool_is_rejected_not_fabricated(self):
        client, store = _client_with_store()
        with client:
            r = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK,
                "owner_token": _ANON_OWNER_TOKEN,
                "ops": [{"op": "add_place", "leg_index": 0, "day_index": 0,
                          "position": 0, "attraction_ref": "MADE UP PLACE XYZ",
                          "from": "unscheduled"}],
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        # All ops rejected → noop
        self.assertEqual(body["outcome"], "noop")
        self.assertEqual(len(body["applied"]), 0)
        self.assertGreater(len(body["rejected"]), 0)
        # The plan is unchanged (original attractions still there).
        leg = body["plan"]["day_plans"][0]
        day0_atts = leg["days"][0]["attractions"]
        names = [a.get("name_en") or a.get("name") for a in day0_atts]
        self.assertNotIn("MADE UP PLACE XYZ", names)

    # ---- 8. swap_place atomic ----
    def test_swap_place_atomic(self):
        client, store = _client_with_store()
        with client:
            r = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK,
                "owner_token": _ANON_OWNER_TOKEN,
                "ops": [{"op": "swap_place", "leg_index": 0, "day_index": 0,
                          "position": 0, "remove_ref": "Senso-ji",
                          "add_ref": "Ueno Park", "from": "unscheduled"}],
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["outcome"], "plan_ready")
        leg = body["plan"]["day_plans"][0]
        day0_atts = leg["days"][0]["attractions"]
        names = [a.get("name_en") or a.get("name") for a in day0_atts]
        # Ueno Park is now at position 0; Senso-ji is removed.
        self.assertEqual(names[0], "Ueno Park")
        self.assertNotIn("Senso-ji", names)
        # Senso-ji is in unscheduled (was there before + removed item added).
        unatts = [a.get("name_en") or a.get("name") for a in leg["unscheduled_attractions"]]
        self.assertIn("Senso-ji", unatts)

    # ---- 9. swap_place aborts atomically when add_ref not in pool ----
    def test_swap_place_missing_add_ref_aborts_atomically(self):
        """swap_place with a non-existent add_ref must abort without mutating the envelope.
        When ALL ops are rejected the endpoint returns outcome='noop' with a 'rejected' list."""
        client, store = _client_with_store()
        with client:
            r = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK,
                "owner_token": _ANON_OWNER_TOKEN,
                "ops": [{"op": "swap_place", "leg_index": 0, "day_index": 0,
                          "position": 0, "remove_ref": "Senso-ji",
                          "add_ref": "NOT IN POOL AT ALL", "from": "unscheduled"}],
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        # All ops rejected → outcome is 'noop'; 'rejected' list has the reason
        self.assertEqual(body.get("outcome"), "noop")
        rejected = body.get("rejected", [])
        self.assertEqual(len(rejected), 1)
        rejected_reason = rejected[0].get("reason") or ""
        self.assertIn("not fabricated", rejected_reason)
        # The original day attractions must be UNCHANGED (Senso-ji still at position 0)
        leg = body["plan"]["day_plans"][0]
        day0_atts = leg["days"][0]["attractions"]
        names = [a.get("name_en") or a.get("name") for a in day0_atts]
        self.assertEqual(names[0], "Senso-ji",
                         "Atomic abort: Senso-ji must remain at position 0 after failed swap")

    # ---- 10. switch_variant fair/wet reversible ----
    def test_switch_variant_fair_wet_is_reversible(self):
        client, store = _client_with_store()
        # Switch to fair.
        with client:
            r1 = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK,
                "owner_token": _ANON_OWNER_TOKEN,
                "ops": [{"op": "switch_variant", "leg_index": 0, "day_index": 0,
                          "variant": "fair"}],
            })
        self.assertEqual(r1.status_code, 200)
        body1 = r1.json()
        self.assertEqual(body1["outcome"], "plan_ready")
        leg = body1["plan"]["day_plans"][0]
        # After switch_variant:fair, attractions should be the fair-weather ones.
        day0_atts = leg["days"][0]["attractions"]
        names = [a.get("name_en") or a.get("name") for a in day0_atts]
        self.assertIn("Shinjuku Gyoen", names, "fair_weather_attractions should now be in attractions")

        # Now update store to persisted envelope (simulate real usage). Anonymous
        # (user_id="") to match _seed_store — this is an UPDATE (idempotency_key
        # already exists), and save_plan's owner_token column is write-once
        # (COALESCE keeps the existing non-empty value), so the row stays bound
        # to _ANON_OWNER_TOKEN regardless of what's passed here.
        store.save_plan({
            "idempotency_key": _PLAN_IDK,
            "user_id": "",
            "checkout_id": "co-test-cancel-001",
            "dest_token": "JP",
            "package_total_cents": 150000,
            "request": _STRUCTURED_REQUEST,
            "envelope": body1["plan"],
        })

        # Switch back to wet.
        with client:
            r2 = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK,
                "owner_token": _ANON_OWNER_TOKEN,
                "ops": [{"op": "switch_variant", "leg_index": 0, "day_index": 0,
                          "variant": "wet"}],
            })
        self.assertEqual(r2.status_code, 200)
        body2 = r2.json()
        self.assertEqual(body2["outcome"], "plan_ready")
        day0_atts2 = body2["plan"]["day_plans"][0]["days"][0]["attractions"]
        names2 = [a.get("name_en") or a.get("name") for a in day0_atts2]
        self.assertNotIn("Shinjuku Gyoen", names2, "After switch back to wet, fair attractions should be swapped out")

    # ---- 10. switch_variant on a clear-weather day (no fair_weather_attractions) → rejected ----
    def test_switch_variant_on_clear_day_rejected(self):
        client, store = _client_with_store()
        with client:
            r = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK,
                "owner_token": _ANON_OWNER_TOKEN,
                "ops": [{"op": "switch_variant", "leg_index": 0, "day_index": 1,
                          "variant": "fair"}],
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        # day 1 has bad_weather=False, no fair_weather_attractions → rejected or noop.
        # Either the op is rejected or outcome is noop.
        self.assertIn(body["outcome"], ("noop", "plan_ready"))
        if body["outcome"] == "plan_ready":
            # It must show up in rejected (applied won't include it).
            self.assertGreater(len(body["rejected"]), 0)
        else:
            self.assertEqual(body["outcome"], "noop")

    # ---- 11. reflow_from fills gaps pace-capped ----
    def test_reflow_from_fills_gap_pace_capped(self):
        client, store = _client_with_store()
        with client:
            r = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK,
                "owner_token": _ANON_OWNER_TOKEN,
                "ops": [{"op": "reflow_from", "leg_index": 0, "from_day_index": 0}],
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["outcome"], "plan_ready")
        leg = body["plan"]["day_plans"][0]
        # With pace=moderate (cap=3): day 0 had [A,C], unscheduled=[B,D].
        # reflow picks up all from day 0 onward (2 atts) + unscheduled (2) = 4 total.
        # 2 days at cap 3 = days fill [0:3]=3 and [3:6]=1.
        total_scheduled = sum(len(d["attractions"]) for d in leg["days"])
        self.assertGreaterEqual(total_scheduled, 2)  # at minimum the original 2
        # Total scheduled + unscheduled = 4 (original total).
        total_unscheduled = len(leg["unscheduled_attractions"])
        self.assertEqual(total_scheduled + total_unscheduled, 4)

    # ---- 12. Stale position ref mismatch → rejected ----
    def test_stale_position_ref_mismatch_rejected(self):
        client, store = _client_with_store()
        with client:
            r = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK,
                "owner_token": _ANON_OWNER_TOKEN,
                "ops": [{"op": "remove_place", "leg_index": 0, "day_index": 0,
                          "position": 0,
                          "attraction_ref": "Wrong Place Name"}],  # pos 0 = Senso-ji, not this
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn(body["outcome"], ("noop", "plan_ready"))
        if body["outcome"] == "noop":
            self.assertGreater(len(body["rejected"]), 0)
        else:
            # If some ops applied and this one was rejected
            self.assertGreater(len(body["rejected"]), 0)

    # ---- 13. /replan NEVER books or debits ----
    def test_replan_never_books_or_debits(self):
        client, store = _client_with_store()
        with client:
            r = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK,
                "owner_token": _ANON_OWNER_TOKEN,
                "ops": [{"op": "remove_place", "leg_index": 0, "day_index": 0,
                          "position": 0, "attraction_ref": "Senso-ji"}],
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        plan = body.get("plan") or {}
        # payment_status stays 'held'
        self.assertEqual(plan.get("payment_status"), "held")
        # booking_ref stays None
        self.assertIsNone(plan.get("booking_ref"))
        # wallet.debited stays False
        wallet = plan.get("wallet") or {}
        self.assertIs(wallet.get("debited"), False)
        # The stored row is still plan_ready
        row = store.get_plan(_PLAN_IDK)
        self.assertEqual(row["status"], "plan_ready")
        self.assertIsNone(row.get("booking_ref"))

    # ---- 14. /replan preserves idempotency_key and price ----
    def test_replan_preserves_idempotency_key_and_price(self):
        client, store = _client_with_store()
        with client:
            r = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK,
                "owner_token": _ANON_OWNER_TOKEN,
                "ops": [{"op": "remove_place", "leg_index": 0, "day_index": 0,
                          "position": 0, "attraction_ref": "Senso-ji"}],
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        # The idempotency_key in the response must be the SAME (request unchanged).
        self.assertEqual(body["idempotency_key"], _PLAN_IDK)
        # Price unchanged.
        plan = body.get("plan") or {}
        self.assertEqual(plan.get("package_total_with_fees_cents"), 150000)
        # Stored row: checkout_id and package_total_cents unchanged.
        row = store.get_plan(_PLAN_IDK)
        self.assertEqual(row.get("checkout_id"), "co-test-cancel-001")
        self.assertEqual(row.get("package_total_cents"), 150000)

    # ---- 15. /replan does not touch the request digest (var-0) ----
    def test_replan_does_not_touch_request_digest(self):
        """A subsequent identical /negotiate would produce the same digest as before
        /replan. We verify this structurally: /replan must not call negotiate() or
        modify the stored request — confirmed by checking the stored request is
        identical to the original after /replan."""
        client, store = _client_with_store()
        original_request = copy.deepcopy(store.get_plan(_PLAN_IDK)["request"])
        with client:
            client.post("/replan", json={
                "idempotency_key": _PLAN_IDK,
                "owner_token": _ANON_OWNER_TOKEN,
                "ops": [{"op": "remove_place", "leg_index": 0, "day_index": 0,
                          "position": 0, "attraction_ref": "Senso-ji"}],
            })
        # The stored structured request must be byte-identical.
        after_request = store.get_plan(_PLAN_IDK)["request"]
        self.assertEqual(json.dumps(original_request, sort_keys=True),
                         json.dumps(after_request, sort_keys=True))

    # ---- 16. Persisted envelope roundtrips via get_plan ----
    def test_persisted_envelope_roundtrips(self):
        client, store = _client_with_store()
        with client:
            r = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK,
                "owner_token": _ANON_OWNER_TOKEN,
                "ops": [{"op": "remove_place", "leg_index": 0, "day_index": 0,
                          "position": 0, "attraction_ref": "Senso-ji"}],
            })
        body = r.json()
        # The persisted envelope must match the response plan.
        row = store.get_plan(_PLAN_IDK)
        # Check the removed item is NOT in the stored day 0.
        leg = row["envelope"]["day_plans"][0]
        day0_names = [a.get("name_en") or a.get("name") for a in leg["days"][0]["attractions"]]
        self.assertNotIn("Senso-ji", day0_names)
        # And matches the response.
        resp_leg = body["plan"]["day_plans"][0]
        resp_day0_names = [a.get("name_en") or a.get("name")
                           for a in resp_leg["days"][0]["attractions"]]
        self.assertEqual(day0_names, resp_day0_names)

    # ---- 17. noop (all rejected) returns previous plan unchanged ----
    def test_noop_all_rejected_returns_previous_plan(self):
        client, store = _client_with_store()
        original_env = copy.deepcopy(store.get_plan(_PLAN_IDK)["envelope"])
        with client:
            r = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK,
                "owner_token": _ANON_OWNER_TOKEN,
                "ops": [{"op": "add_place", "leg_index": 0, "day_index": 0,
                          "position": 0, "attraction_ref": "GHOST PLACE", "from": "unscheduled"}],
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["outcome"], "noop")
        # The plan echoed back must be the ORIGINAL (unchanged).
        echoed_leg = body["plan"]["day_plans"][0]
        original_leg = original_env["day_plans"][0]
        self.assertEqual(
            json.dumps(echoed_leg["days"][0]["attractions"], sort_keys=True),
            json.dumps(original_leg["days"][0]["attractions"], sort_keys=True),
        )

    # ---- 18. itinerary_narrative flagged stale after a structural edit (BUG FIX) ----
    def test_structural_edit_marks_narrative_stale(self):
        """A trip narrated at booking time (#3's OPT-IN cosmetic itinerary_narrative,
        attached ONCE by _maybe_narrate_itinerary) names concrete attractions. /replan
        explicitly allows editing an already-BOOKED trip, but never touched the
        narrative — so a removed/added/reordered stop would keep being described by
        stale prose, served back verbatim (assistant summary panel + ICS export).
        Cheapest honest fix: mark it stale in place (no LLM re-invocation on every
        edit) rather than silently serving it as current."""
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        env = copy.deepcopy(_PLAN_ENVELOPE)
        env["itinerary_narrative"] = {
            "legs": [{"leg_id": "leg-0", "city": "tokyo", "summary": "A lovely city.",
                       "days": [{"day_number": 1, "title": "Old Town",
                                  "narrative": "Start at Senso-ji, then Tokyo Tower."}]}],
            "disclosure": "AI-written narrative over your booked plan; every place shown "
                           "is verified against your deterministic itinerary.",
        }
        store.save_plan({
            "idempotency_key": _PLAN_IDK,
            "user_id": "",
            "owner_token": _ANON_OWNER_TOKEN,
            "checkout_id": "co-test-cancel-001",
            "dest_token": "JP",
            "package_total_cents": 150000,
            "request": _STRUCTURED_REQUEST,
            "envelope": env,
        })
        with TestClient(server.build_app()) as client:
            r = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK,
                "owner_token": _ANON_OWNER_TOKEN,
                "ops": [{"op": "add_place", "leg_index": 0, "day_index": 0,
                          "position": 1, "attraction_ref": "Ueno Park", "from": "unscheduled"}],
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["outcome"], "plan_ready")
        narr = body["plan"].get("itinerary_narrative")
        self.assertIsNotNone(narr)
        self.assertTrue(narr.get("stale"), "narrative must be flagged stale after a structural edit")
        self.assertIn("stale_reason", narr)
        # The narrative prose itself is NEVER silently rewritten/regenerated by /replan
        # (that would require an LLM call on a plan-only, deterministic edit endpoint).
        self.assertEqual(
            narr["legs"][0]["days"][0]["narrative"], "Start at Senso-ji, then Tokyo Tower.",
        )

    def test_noop_replan_does_not_mark_narrative_stale(self):
        """If every op is rejected (nothing actually applied), the narrative must NOT
        be touched — mirrors the item_transport_gaps/intracity_hops `if applied:` gate."""
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        env = copy.deepcopy(_PLAN_ENVELOPE)
        env["itinerary_narrative"] = {"legs": [], "disclosure": "x"}
        store.save_plan({
            "idempotency_key": _PLAN_IDK,
            "user_id": "",
            "owner_token": _ANON_OWNER_TOKEN,
            "checkout_id": "co-test-cancel-001",
            "dest_token": "JP",
            "package_total_cents": 150000,
            "request": _STRUCTURED_REQUEST,
            "envelope": env,
        })
        with TestClient(server.build_app()) as client:
            r = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK,
                "owner_token": _ANON_OWNER_TOKEN,
                "ops": [{"op": "add_place", "leg_index": 0, "day_index": 0,
                          "position": 0, "attraction_ref": "GHOST PLACE", "from": "unscheduled"}],
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["outcome"], "noop")
        narr = body["plan"].get("itinerary_narrative")
        self.assertIsNotNone(narr)
        self.assertNotIn("stale", narr)


class TestReplanOwnership(unittest.TestCase):
    """SECURITY regression: /replan ownership gate (was VULN-AUTH-002, CVSS 8.2
    HIGH — IDOR). Mirrors test_prefs_session.py's `_login` + wrong-user
    pattern, but the gate returns 403 `not_trip_owner` (server.py's
    `_forbidden` — NOT 401; see server.py's `_authorize_trip_action` docstring
    for the full two-tier session_token/owner_token model)."""

    _OWNER_IDK = "trip-replan-own-001"      # logged-in owner (demo-mei)
    _ANON_IDK = "trip-replan-anon-001"      # anonymous, owner_token-bound
    _ANON_TOKEN = "anon-secret-replan-A"
    _OP = [{"op": "remove_place", "leg_index": 0, "day_index": 0,
            "position": 0, "attraction_ref": "Senso-ji"}]

    def _seed(self, store: SqliteDashboardStore) -> None:
        store.save_plan({
            "idempotency_key": self._OWNER_IDK, "user_id": "demo-mei",
            "package_total_cents": 150000,
            "request": dict(_STRUCTURED_REQUEST, user_id="demo-mei"),
            "envelope": copy.deepcopy({**_PLAN_ENVELOPE, "idempotency_key": self._OWNER_IDK}),
        })
        store.save_plan({
            "idempotency_key": self._ANON_IDK, "user_id": "",
            "owner_token": self._ANON_TOKEN,
            "package_total_cents": 150000,
            "request": dict(_STRUCTURED_REQUEST, user_id=""),
            "envelope": copy.deepcopy({**_PLAN_ENVELOPE, "idempotency_key": self._ANON_IDK}),
        })

    def _client(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        self._seed(store)
        return TestClient(server.build_app()), store

    # ---- 1. Logged-in owner + valid session_token -> 200, proceeds ----
    def test_owner_with_valid_session_token_succeeds(self):
        client, store = self._client()
        with client:
            token = _login(client, "demo-mei")
            r = client.post("/replan", json={"idempotency_key": self._OWNER_IDK,
                                             "ops": self._OP, "session_token": token})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["outcome"], "plan_ready")

    # ---- 2. Logged-in trip, no session_token -> 403; no state change ----
    def test_owner_trip_no_session_token_403_no_state_change(self):
        client, store = self._client()
        before = copy.deepcopy(store.get_plan(self._OWNER_IDK)["envelope"])
        with client:
            r = client.post("/replan", json={"idempotency_key": self._OWNER_IDK, "ops": self._OP})
        self.assertEqual(r.status_code, 403)
        # #203: Tier 1 (logged-in trip) denials return `session_invalid`.
        self.assertEqual(r.json().get("reason"), "session_invalid")
        self.assertEqual(store.get_plan(self._OWNER_IDK)["envelope"], before)

    # ---- 3. session_token minted for a DIFFERENT demo user -> 403 ----
    def test_owner_trip_wrong_user_session_token_403(self):
        client, store = self._client()
        with client:
            other_token = _login(client, "demo-alex")
            r = client.post("/replan", json={"idempotency_key": self._OWNER_IDK,
                                             "ops": self._OP, "session_token": other_token})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json().get("reason"), "session_invalid")  # #203: Tier 1

    # ---- 4. Anon trip + correct owner_token -> proceeds ----
    def test_anon_trip_correct_owner_token_succeeds(self):
        client, store = self._client()
        with client:
            r = client.post("/replan", json={"idempotency_key": self._ANON_IDK,
                                             "ops": self._OP, "owner_token": self._ANON_TOKEN})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["outcome"], "plan_ready")

    # ---- 5. Anon trip, wrong/missing owner_token -> 403; no state change ----
    def test_anon_trip_wrong_or_missing_owner_token_403_no_state_change(self):
        client, store = self._client()
        before = copy.deepcopy(store.get_plan(self._ANON_IDK)["envelope"])
        with client:
            r_wrong = client.post("/replan", json={"idempotency_key": self._ANON_IDK,
                                                    "ops": self._OP,
                                                    "owner_token": "not-the-right-secret"})
            r_missing = client.post("/replan", json={"idempotency_key": self._ANON_IDK, "ops": self._OP})
        self.assertEqual(r_wrong.status_code, 403)
        self.assertEqual(r_wrong.json().get("reason"), "not_trip_owner")
        self.assertEqual(r_missing.status_code, 403)
        self.assertEqual(r_missing.json().get("reason"), "not_trip_owner")
        self.assertEqual(store.get_plan(self._ANON_IDK)["envelope"], before)

    # ---- 6. Cross-class: anon-only caller vs logged-in trip, and
    #         logged-in-only caller vs anon trip (no owner_token) -> both 403 ----
    def test_cross_class_callers_403(self):
        client, store = self._client()
        with client:
            r1 = client.post("/replan", json={"idempotency_key": self._OWNER_IDK,
                                              "ops": self._OP,
                                              "owner_token": "some-random-anon-secret"})
            token = _login(client, "demo-mei")
            r2 = client.post("/replan", json={"idempotency_key": self._ANON_IDK,
                                              "ops": self._OP, "session_token": token})
        self.assertEqual(r1.status_code, 403)
        self.assertEqual(r1.json().get("reason"), "session_invalid")  # #203: r1 targets the Tier-1 (logged-in) trip
        self.assertEqual(r2.status_code, 403)
        self.assertEqual(r2.json().get("reason"), "not_trip_owner")

    # ---- 9 (targeted regression). Store write-once: save_plan an anon trip
    #        with owner_token=A, then re-save_plan the SAME key with
    #        owner_token=B -> get_plan().owner_token stays "A" (attacker cannot
    #        seize a trip's ownership by racing a second save_plan call) ----
    def test_store_owner_token_is_write_once(self):
        store = SqliteDashboardStore(":memory:")
        idk = "trip-write-once-001"
        store.save_plan({
            "idempotency_key": idk, "user_id": "",
            "owner_token": "A-original-secret",
            "package_total_cents": 100000,
            "request": {}, "envelope": {"outcome": "plan_ready", "idempotency_key": idk},
        })
        store.save_plan({
            "idempotency_key": idk, "user_id": "",
            "owner_token": "B-attacker-secret",
            "package_total_cents": 100000,
            "request": {}, "envelope": {"outcome": "plan_ready", "idempotency_key": idk},
        })
        self.assertEqual(store.get_plan(idk).get("owner_token"), "A-original-secret",
                         "owner_token must be write-once — a re-save_plan must NOT let a "
                         "second caller seize ownership of an existing trip")

    # ---- 12 (targeted regression). Legacy anon row (owner_token="") ->
    #        documents the fail-open transition (proceeds) ----
    def test_legacy_anon_row_no_owner_token_fails_open(self):
        """A row with NO owner_token bound (pre-dating the column, or an anon
        plan created by a caller that never supplied one) fail-opens — any
        caller may act on it. Documented transient-rollout behavior (see
        server.py's _authorize_trip_action), not a silent bypass."""
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        idk = "trip-legacy-anon-no-token-001"
        store.save_plan({
            "idempotency_key": idk, "user_id": "",   # no owner_token at all
            "package_total_cents": 150000,
            "request": dict(_STRUCTURED_REQUEST, user_id=""),
            "envelope": copy.deepcopy({**_PLAN_ENVELOPE, "idempotency_key": idk}),
        })
        self.assertEqual(store.get_plan(idk).get("owner_token"), "",
                         "fixture sanity: row must have no owner_token bound")
        with TestClient(server.build_app()) as client:
            r = client.post("/replan", json={"idempotency_key": idk, "ops": self._OP})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["outcome"], "plan_ready")


# ---------------------------------------------------------------------------
# Unit tests for replan_ops (pure functions, no HTTP)
# ---------------------------------------------------------------------------

class TestReplanOps(unittest.TestCase):

    def _make_envelope(self) -> dict:
        return copy.deepcopy({
            "outcome": "plan_ready",
            "payment_status": "held",
            "booking_ref": None,
            "package_total_with_fees_cents": 150000,
            "wallet": {"debited": False},
            "day_plans": [
                {
                    "days": [
                        {
                            "day_index": 0,
                            "bad_weather": True,
                            "attractions": [copy.deepcopy(_ATT_A), copy.deepcopy(_ATT_C)],
                            "fair_weather_attractions": [copy.deepcopy(_ATT_FAIR)],
                        },
                        {
                            "day_index": 1,
                            "bad_weather": False,
                            "attractions": [],
                        },
                    ],
                    "unscheduled_attractions": [copy.deepcopy(_ATT_B), copy.deepcopy(_ATT_D)],
                }
            ],
        })

    def test_remove_and_add_roundtrip(self):
        from utils.replan_ops import apply_ops
        env = self._make_envelope()
        applied, rejected = apply_ops(env, [
            {"op": "remove_place", "leg_index": 0, "day_index": 0, "position": 0,
             "attraction_ref": "Senso-ji"},
            {"op": "add_place", "leg_index": 0, "day_index": 1, "position": 0,
             "attraction_ref": "Senso-ji", "from": "unscheduled"},
        ])
        self.assertEqual(len(applied), 2)
        self.assertEqual(len(rejected), 0)
        day0 = env["day_plans"][0]["days"][0]["attractions"]
        day1 = env["day_plans"][0]["days"][1]["attractions"]
        names0 = [a.get("name_en") for a in day0]
        names1 = [a.get("name_en") for a in day1]
        self.assertNotIn("Senso-ji", names0)
        self.assertIn("Senso-ji", names1)

    def test_money_fields_untouched_by_ops(self):
        from utils.replan_ops import apply_ops
        env = self._make_envelope()
        apply_ops(env, [
            {"op": "remove_place", "leg_index": 0, "day_index": 0, "position": 0,
             "attraction_ref": "Senso-ji"},
        ])
        self.assertEqual(env["payment_status"], "held")
        self.assertIsNone(env["booking_ref"])
        self.assertIs(env["wallet"]["debited"], False)
        self.assertEqual(env["package_total_with_fees_cents"], 150000)

    def test_invalid_op_type_rejected(self):
        from utils.replan_ops import apply_ops
        env = self._make_envelope()
        applied, rejected = apply_ops(env, [{"op": "teleport_place"}])
        self.assertEqual(len(applied), 0)
        self.assertEqual(len(rejected), 1)
        self.assertIn("unknown op type", rejected[0]["reason"])

    def test_out_of_range_position_rejected(self):
        from utils.replan_ops import apply_ops
        env = self._make_envelope()
        applied, rejected = apply_ops(env, [
            {"op": "remove_place", "leg_index": 0, "day_index": 0, "position": 99,
             "attraction_ref": "Senso-ji"},
        ])
        self.assertEqual(len(rejected), 1)
        self.assertIn("out of range", rejected[0]["reason"])

    # ---- 0-indexed-leak regression: user-facing note/reason text must be
    # 1-indexed (day 1/leg 1, not day 0/leg 0), matching the rest of the UI
    # (web/'s Itinerary.svelte "Leg " + (i+1) convention). Both
    # rejection reasons AND _replan_notes reach the end user verbatim: /replan's
    # "rejected"/"notes" are rendered as-is by Itinerary.svelte's runOps flash
    # (`Updated, but: ${r.rejected.map(x=>x.reason).join('; ')}` and
    # `Updated · ${r.notes.join('; ')}`). See live-test finding: a user saw the
    # raw "day 0 (leg 0) now exceeds pace cap (3>2) after add — kept, flagged."

    def test_add_place_pace_cap_note_is_1_indexed(self):
        """The pace-cap-exceeded note (envelope["_replan_notes"]) must read
        'day 1 (leg 1)', not the raw 0-indexed 'day 0 (leg 0)' — this is the
        exact live-test finding."""
        from utils.replan_ops import apply_ops
        env = copy.deepcopy({
            "outcome": "plan_ready",
            "payment_status": "held",
            "booking_ref": None,
            "package_total_with_fees_cents": 150000,
            "wallet": {"debited": False},
            "day_plans": [
                {
                    "days": [
                        {
                            "day_index": 0,
                            "bad_weather": False,
                            "attractions": [copy.deepcopy(_ATT_A), copy.deepcopy(_ATT_C),
                                             copy.deepcopy(_ATT_E)],
                        },
                    ],
                    "unscheduled_attractions": [copy.deepcopy(_ATT_B)],
                }
            ],
        })
        applied, rejected = apply_ops(env, [
            {"op": "add_place", "leg_index": 0, "day_index": 0, "position": 0,
             "attraction_ref": "Ueno Park", "from": "unscheduled"},
        ], pace="moderate")
        self.assertEqual(len(rejected), 0, rejected)
        self.assertEqual(len(applied), 1)
        notes = env.get("_replan_notes") or []
        self.assertEqual(len(notes), 1)
        self.assertIn("day 1 (leg 1) now exceeds pace cap (4>3) after add", notes[0])
        self.assertNotIn("day 0 (leg 0)", notes[0])
        # Regression guard: the internal diff tag is a machine-readable
        # identifier (used for undo/tracking) and must stay 0-indexed — the
        # display fix above must NOT leak into it.
        self.assertEqual(applied[0], "add:leg0.day0.pos0:Ueno Park")

    def test_leg_index_out_of_range_reason_is_1_indexed(self):
        from utils.replan_ops import apply_ops
        env = self._make_envelope()  # 1 leg total: leg_index 0 valid, 1 is out of range
        applied, rejected = apply_ops(env, [
            {"op": "remove_place", "leg_index": 1, "day_index": 0, "position": 0,
             "attraction_ref": "Senso-ji"},
        ])
        self.assertEqual(len(rejected), 1)
        self.assertIn("leg 2 out of range", rejected[0]["reason"])
        self.assertNotIn("leg 1 out of range", rejected[0]["reason"])
        # Regression guard: message text must not leak the raw field-name
        # jargon either (only the numeric value was previously fixed by #198).
        self.assertNotIn("leg_index", rejected[0]["reason"])

    def test_day_index_out_of_range_reason_is_1_indexed(self):
        from utils.replan_ops import apply_ops
        env = self._make_envelope()  # leg 0 has 2 days: day_index 0/1 valid, 5 is out of range
        applied, rejected = apply_ops(env, [
            {"op": "remove_place", "leg_index": 0, "day_index": 5, "position": 0,
             "attraction_ref": "Senso-ji"},
        ])
        self.assertEqual(len(rejected), 1)
        self.assertIn("day 6 out of range for leg 1", rejected[0]["reason"])
        self.assertNotIn("day 5 out of range for leg 0", rejected[0]["reason"])
        self.assertNotIn("day_index", rejected[0]["reason"])

    def test_switch_variant_no_fair_weather_reason_is_1_indexed(self):
        from utils.replan_ops import apply_ops
        env = self._make_envelope()  # day_index 1: bad_weather=False, no fair_weather_attractions
        applied, rejected = apply_ops(env, [
            {"op": "switch_variant", "leg_index": 0, "day_index": 1, "variant": "fair"},
        ])
        self.assertEqual(len(rejected), 1)
        self.assertIn("day 2 (leg 1) has no rain-ready alternative to switch to", rejected[0]["reason"])
        self.assertNotIn("day 1 (leg 0)", rejected[0]["reason"])
        self.assertNotIn("fair_weather_attractions", rejected[0]["reason"])

    # ---- swap_meal unit tests ----

    def _make_envelope_with_meals(self) -> dict:
        env = self._make_envelope()
        day0 = env["day_plans"][0]["days"][0]
        day0["meals"] = {"lunch": {"name": "Sukiya", "cuisine": "gyudon"}}
        day0["meal_pool"] = {
            "lunch": [
                {"name": "Yoshinoya", "cuisine": "gyudon"},
                {"name": "Matsuya", "cuisine": "gyudon"},
            ]
        }
        return env

    def test_swap_meal_happy_path(self):
        from utils.replan_ops import apply_ops
        env = self._make_envelope_with_meals()
        applied, rejected = apply_ops(env, [
            {"op": "swap_meal", "leg_index": 0, "day_index": 0,
             "slot": "lunch", "restaurant_name": "Yoshinoya"},
        ])
        self.assertEqual(len(rejected), 0, rejected)
        self.assertEqual(len(applied), 1)
        self.assertIn("swap_meal", applied[0])
        # The meal should now be Yoshinoya.
        lunch = env["day_plans"][0]["days"][0]["meals"]["lunch"]
        self.assertEqual(lunch["name"], "Yoshinoya")
        # Sukiya should now appear in the pool (evicted back).
        pool = env["day_plans"][0]["days"][0]["meal_pool"]["lunch"]
        pool_names = [m["name"] for m in pool]
        self.assertIn("Sukiya", pool_names)
        self.assertNotIn("Yoshinoya", pool_names)

    def test_swap_meal_unknown_restaurant_rejected(self):
        from utils.replan_ops import apply_ops
        env = self._make_envelope_with_meals()
        applied, rejected = apply_ops(env, [
            {"op": "swap_meal", "leg_index": 0, "day_index": 0,
             "slot": "lunch", "restaurant_name": "Made Up Place"},
        ])
        self.assertEqual(len(applied), 0)
        self.assertEqual(len(rejected), 1)
        self.assertIn("not found", rejected[0]["reason"])
        # Meal should be unchanged (atomic guarantee).
        self.assertEqual(env["day_plans"][0]["days"][0]["meals"]["lunch"]["name"], "Sukiya")

    def test_swap_meal_invalid_slot_rejected(self):
        from utils.replan_ops import apply_ops
        env = self._make_envelope_with_meals()
        applied, rejected = apply_ops(env, [
            {"op": "swap_meal", "leg_index": 0, "day_index": 0,
             "slot": "tea_time", "restaurant_name": "Yoshinoya"},
        ])
        self.assertEqual(len(applied), 0)
        self.assertEqual(len(rejected), 1)
        self.assertIn("invalid slot", rejected[0]["reason"])

    def test_swap_meal_case_insensitive(self):
        from utils.replan_ops import apply_ops
        env = self._make_envelope_with_meals()
        applied, rejected = apply_ops(env, [
            {"op": "swap_meal", "leg_index": 0, "day_index": 0,
             "slot": "lunch", "restaurant_name": "YOSHINOYA"},
        ])
        self.assertEqual(len(rejected), 0, rejected)
        lunch = env["day_plans"][0]["days"][0]["meals"]["lunch"]
        self.assertEqual(lunch["name"], "Yoshinoya")

    # ---- swap_days unit tests ----

    def _make_envelope_multiday(self) -> dict:
        """A 5-day leg: day 0 and day 4 are boundary (arrival/checkout) days;
        days 1-3 are interior and eligible for swap_days. Day 2 is the only
        bad_weather=True day and carries fair_weather_attractions."""
        return copy.deepcopy({
            "outcome": "plan_ready",
            "payment_status": "held",
            "booking_ref": None,
            "package_total_with_fees_cents": 150000,
            "wallet": {"debited": False},
            "day_plans": [
                {
                    "city": "tokyo",
                    "days": [
                        {
                            "day_index": 0,
                            "bad_weather": False,
                            "attractions": [copy.deepcopy(_ATT_A)],
                            "meals": {},
                        },
                        {
                            "day_index": 1,
                            "bad_weather": False,
                            "attractions": [copy.deepcopy(_ATT_E)],
                            "meals": {},
                        },
                        {
                            "day_index": 2,
                            "bad_weather": True,
                            "attractions": [copy.deepcopy(_ATT_F)],
                            "fair_weather_attractions": [copy.deepcopy(_ATT_FAIR)],
                            "meals": {},
                        },
                        {
                            "day_index": 3,
                            "bad_weather": False,
                            "attractions": [copy.deepcopy(_ATT_G)],
                            "meals": {},
                        },
                        {
                            "day_index": 4,
                            "bad_weather": False,
                            "attractions": [copy.deepcopy(_ATT_C)],
                            "meals": {},
                        },
                    ],
                    "unscheduled_attractions": [copy.deepcopy(_ATT_B), copy.deepcopy(_ATT_D)],
                }
            ],
        })

    def test_swap_days_happy_path_content_moves(self):
        from utils.replan_ops import apply_ops
        env = self._make_envelope_multiday()
        applied, rejected = apply_ops(env, [
            {"op": "swap_days", "leg_index": 0, "day_a": 1, "day_b": 3},
        ])
        self.assertEqual(len(rejected), 0, rejected)
        self.assertEqual(len(applied), 1)
        days = env["day_plans"][0]["days"]
        names1 = [a.get("name_en") for a in days[1]["attractions"]]
        names3 = [a.get("name_en") for a in days[3]["attractions"]]
        # day 1 (was Meiji Shrine) now carries day 3's original content (Odaiba).
        self.assertEqual(names1, ["Odaiba"])
        # day 3 (was Odaiba) now carries day 1's original content (Meiji Shrine).
        self.assertEqual(names3, ["Meiji Shrine"])

    def test_swap_days_day_index_stays_positional(self):
        from utils.replan_ops import apply_ops
        env = self._make_envelope_multiday()
        applied, rejected = apply_ops(env, [
            {"op": "swap_days", "leg_index": 0, "day_a": 1, "day_b": 3},
        ])
        self.assertEqual(len(rejected), 0, rejected)
        days = env["day_plans"][0]["days"]
        for i, d in enumerate(days):
            self.assertEqual(d["day_index"], i, f"day slot {i} has stale day_index {d['day_index']}")

    def test_swap_days_bad_weather_stays_positional_with_honest_note(self):
        from utils.replan_ops import apply_ops
        env = self._make_envelope_multiday()
        applied, rejected = apply_ops(env, [
            {"op": "swap_days", "leg_index": 0, "day_a": 1, "day_b": 2},
        ])
        self.assertEqual(len(rejected), 0, rejected)
        days = env["day_plans"][0]["days"]
        # day 1's slot was fair (False) before the swap; it must STILL be
        # False afterwards even though day 2's (bad-weather) content moved in.
        self.assertFalse(days[1]["bad_weather"])
        # day 2's slot was bad (True) before the swap; it must STILL be True
        # even though day 1's (fair-only) content moved in.
        self.assertTrue(days[2]["bad_weather"])
        notes = " ".join(env.get("_replan_notes") or [])
        self.assertIn("bad-weather advisory stays with the date", notes)
        # day_a=1/day_b=2 are 0-indexed op params; the note is user-facing and
        # must show 1-indexed day/leg numbers ("day 3 (leg 1)"), matching the
        # rest of the UI (web/'s "Leg " + (i+1) convention).
        self.assertIn("day 3 (leg 1)", notes)
        self.assertNotIn("day 2 (leg 0)", notes)

    def test_swap_days_wet_fair_reconciliation(self):
        from utils.replan_ops import apply_ops

        # Case 1 + 2: fair slot (day 1) swaps with the bad slot (day 2).
        env = self._make_envelope_multiday()
        applied, rejected = apply_ops(env, [
            {"op": "swap_days", "leg_index": 0, "day_a": 1, "day_b": 2},
        ])
        self.assertEqual(len(rejected), 0, rejected)
        days = env["day_plans"][0]["days"]
        # Case 1: a fair-date day (day 1) never retains fair_weather_attractions
        # or _variant_active, even though the incoming content had them.
        self.assertNotIn("fair_weather_attractions", days[1])
        self.assertNotIn("_variant_active", days[1])
        # Case 2: day 2 is still bad_weather=True but the moved-in content
        # (originally day 1's) has no fair_weather_attractions — must NOT be
        # fabricated, and an honest note must be emitted.
        self.assertNotIn("fair_weather_attractions", days[2])
        notes = " ".join(env.get("_replan_notes") or [])
        self.assertIn("no rain-ready alternative ordering (not fabricated)", notes)
        # day index 2 (0-indexed) displays as "day 3" (1-indexed, user-facing).
        self.assertIn("day 3 (leg 1)", notes)
        self.assertNotIn("day 2 (leg 0)", notes)

        # Case 3: two bad_weather=True interior days, BOTH carrying
        # fair_weather_attractions — both slots keep their (swapped) fair set.
        custom_env = copy.deepcopy({
            "payment_status": "held",
            "booking_ref": None,
            "package_total_with_fees_cents": 150000,
            "wallet": {"debited": False},
            "day_plans": [
                {
                    "days": [
                        {"day_index": 0, "bad_weather": False,
                         "attractions": [copy.deepcopy(_ATT_A)]},
                        {"day_index": 1, "bad_weather": True,
                         "attractions": [copy.deepcopy(_ATT_E)],
                         "fair_weather_attractions": [copy.deepcopy(_ATT_FAIR)],
                         "_variant_active": "wet"},
                        {"day_index": 2, "bad_weather": True,
                         "attractions": [copy.deepcopy(_ATT_F)],
                         "fair_weather_attractions": [copy.deepcopy(_ATT_FAIR2)]},
                        {"day_index": 3, "bad_weather": False,
                         "attractions": [copy.deepcopy(_ATT_C)]},
                    ],
                    "unscheduled_attractions": [],
                }
            ],
        })
        applied2, rejected2 = apply_ops(custom_env, [
            {"op": "swap_days", "leg_index": 0, "day_a": 1, "day_b": 2},
        ])
        self.assertEqual(len(rejected2), 0, rejected2)
        cdays = custom_env["day_plans"][0]["days"]
        self.assertTrue(cdays[1]["bad_weather"])
        self.assertTrue(cdays[2]["bad_weather"])
        # day 1 now carries day 2's original content, including its fair set.
        self.assertEqual(
            [a.get("name_en") for a in cdays[1]["fair_weather_attractions"]],
            ["Nezu Museum"],
        )
        self.assertEqual([a.get("name_en") for a in cdays[1]["attractions"]], ["Akihabara"])
        # day 2 now carries day 1's original content, including its fair set
        # AND its _variant_active marker (moved with the content, untouched).
        self.assertEqual(
            [a.get("name_en") for a in cdays[2]["fair_weather_attractions"]],
            ["Shinjuku Gyoen"],
        )
        self.assertEqual([a.get("name_en") for a in cdays[2]["attractions"]], ["Meiji Shrine"])
        self.assertEqual(cdays[2]["_variant_active"], "wet")

    def test_swap_days_involution_is_var0(self):
        from utils.replan_ops import apply_ops
        env = self._make_envelope_multiday()
        original_day_plans = copy.deepcopy(env["day_plans"])
        applied, rejected = apply_ops(env, [
            {"op": "swap_days", "leg_index": 0, "day_a": 1, "day_b": 3},
            {"op": "swap_days", "leg_index": 0, "day_a": 1, "day_b": 3},
        ])
        self.assertEqual(len(rejected), 0, rejected)
        self.assertEqual(len(applied), 2, "both swaps of an involution must be marked applied")
        self.assertEqual(
            json.dumps(env["day_plans"], sort_keys=True),
            json.dumps(original_day_plans, sort_keys=True),
        )

    def test_swap_days_rejections_are_atomic(self):
        from utils.replan_ops import apply_ops

        scenarios = [
            ("boundary day", {"leg_index": 0, "day_a": 0, "day_b": 1}, "boundary day"),
            ("same day", {"leg_index": 0, "day_a": 2, "day_b": 2}, "same day"),
            ("out of range day", {"leg_index": 0, "day_a": 1, "day_b": 99}, "out of range"),
            ("bad leg_index", {"leg_index": 5, "day_a": 1, "day_b": 3}, "out of range"),
        ]
        for label, op_fields, expect_substr in scenarios:
            with self.subTest(label=label):
                env = self._make_envelope_multiday()
                before = copy.deepcopy(env["day_plans"])
                applied, rejected = apply_ops(env, [
                    {"op": "swap_days", **op_fields},
                ])
                self.assertEqual(len(applied), 0, f"{label}: expected 0 applied")
                self.assertEqual(len(rejected), 1, f"{label}: expected 1 rejected")
                self.assertIn(expect_substr, rejected[0]["reason"], f"{label}: {rejected[0]['reason']!r}")
                self.assertEqual(
                    json.dumps(env["day_plans"], sort_keys=True),
                    json.dumps(before, sort_keys=True),
                    f"{label}: envelope must be unchanged after a rejected op",
                )

    def test_swap_days_money_fields_untouched(self):
        from utils.replan_ops import apply_ops
        env = self._make_envelope_multiday()
        apply_ops(env, [
            {"op": "swap_days", "leg_index": 0, "day_a": 1, "day_b": 3},
        ])
        self.assertEqual(env["payment_status"], "held")
        self.assertIsNone(env["booking_ref"])
        self.assertIs(env["wallet"]["debited"], False)
        self.assertEqual(env["package_total_with_fees_cents"], 150000)

    def test_swap_days_applied_tag_canonical(self):
        from utils.replan_ops import apply_ops
        env1 = self._make_envelope_multiday()
        applied1, rejected1 = apply_ops(env1, [
            {"op": "swap_days", "leg_index": 0, "day_a": 1, "day_b": 3},
        ])
        env2 = self._make_envelope_multiday()
        applied2, rejected2 = apply_ops(env2, [
            {"op": "swap_days", "leg_index": 0, "day_a": 3, "day_b": 1},
        ])
        self.assertEqual(len(rejected1), 0, rejected1)
        self.assertEqual(len(rejected2), 0, rejected2)
        self.assertEqual(applied1, applied2)
        self.assertEqual(applied1[0], "swap_days:leg0.day1<->day3")

    # ---- 0-indexed-leak regression: swap_days rejection reasons/notes ----

    def test_swap_days_out_of_range_reasons_are_1_indexed(self):
        from utils.replan_ops import apply_ops
        env = self._make_envelope_multiday()  # leg 0 has 5 days (indices 0-4)
        applied, rejected = apply_ops(env, [
            {"op": "swap_days", "leg_index": 0, "day_a": 1, "day_b": 99},
        ])
        self.assertEqual(len(rejected), 1)
        self.assertIn("day 100 out of range for leg 1", rejected[0]["reason"])
        self.assertNotIn("day 99 out of range for leg 0", rejected[0]["reason"])
        self.assertNotIn("day_b", rejected[0]["reason"])

    def test_swap_days_bad_leg_index_reason_is_1_indexed(self):
        from utils.replan_ops import apply_ops
        env = self._make_envelope_multiday()  # 1 leg total: leg_index 0 valid, 5 is out of range
        applied, rejected = apply_ops(env, [
            {"op": "swap_days", "leg_index": 5, "day_a": 1, "day_b": 3},
        ])
        self.assertEqual(len(rejected), 1)
        self.assertIn("leg 6 out of range", rejected[0]["reason"])
        self.assertNotIn("leg_index", rejected[0]["reason"])

    def test_swap_days_boundary_day_reason_is_1_indexed(self):
        from utils.replan_ops import apply_ops
        env = self._make_envelope_multiday()  # day 0 is a boundary day (0-indexed)
        applied, rejected = apply_ops(env, [
            {"op": "swap_days", "leg_index": 0, "day_a": 0, "day_b": 1},
        ])
        self.assertEqual(len(rejected), 1)
        self.assertIn("day 1 is a boundary day (arrival/checkout) of leg 1", rejected[0]["reason"])
        self.assertNotIn("day 0 is a boundary day", rejected[0]["reason"])

    def test_swap_days_bad_weather_note_bad_di_is_1_indexed(self):
        """bad_di (the note's day-number source) must be display-converted the
        same as day_a/day_b — regression guard distinct from
        test_swap_days_bad_weather_stays_positional_with_honest_note, which
        already asserts the full 'day 3 (leg 1)' text; this pins the tag stays
        untouched alongside it."""
        from utils.replan_ops import apply_ops
        env = self._make_envelope_multiday()
        applied, rejected = apply_ops(env, [
            {"op": "swap_days", "leg_index": 0, "day_a": 1, "day_b": 2},
        ])
        self.assertEqual(len(rejected), 0, rejected)
        # Regression guard: the internal diff tag is 0-indexed (undo/tracking
        # identifier) and must NOT be touched by the display-text fix.
        self.assertEqual(applied[0], "swap_days:leg0.day1<->day2")


if __name__ == "__main__":
    unittest.main()
