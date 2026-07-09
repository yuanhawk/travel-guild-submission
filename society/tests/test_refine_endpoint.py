"""test_refine_endpoint.py — B4: /refine endpoint integration tests.

Architecture: Starlette TestClient + in-memory store injection.
All LLM calls are monkeypatched (no network, no key).

Tests:
1. Missing body fields → 400 invalid_request.
2. Unknown idempotency_key → unknown_plan.
3. Booked plan → plan_locked.
4. Budget-down refine → new plan_ready with new idempotency_key + cheaper total.
5. add_leg refine → new plan_ready with extra leg in the plan.
6. Conversation persists and threads across multiple refines.
7. cannot_satisfy from negotiate → kept_previous:True, old plan still returned.
8. swap_item only → refine_partial, NO re-plan.
9. var-0: negotiate(new_request) produces a fresh digest → new idempotency_key differs from old.
10. Plan with no legs (legacy body) → refine_unavailable.
11. B3: plan_ready response carries the new structured "diff" field, and a city
    swap that also shrinks total nights gets total_nights.side_effect=True.
12. B5: a health/fraud/insurance/compliance QUESTION (question_domain set on the
    parsed delta) returns outcome="domain_answer" with a new "answer" field —
    grounded in the domain verdict already on the plan's envelope when present,
    honestly ungrounded when not — WITHOUT calling negotiate() (no re-plan, same
    idempotency_key, plan unchanged) and WITHOUT falling through to
    refine_unsupported.

SECURITY (IDOR fix, was VULN-AUTH-003 CVSS 8.1 HIGH): /refine now REQUIRES the
two-tier session_token/owner_token ownership proof (see server.py's
_authorize_trip_action). TestRefineOwnership below is the regression lock —
mirrors test_prefs_session.py's `_login` + wrong-user pattern, but the gate
returns 403 `not_trip_owner` (not 401 — see server.py's `_forbidden`).
TestConfirmIdempotent (also in this file) covers /confirm's own idempotency
regression and got the same ownership migration.

MIGRATION NOTE: every fixture below used to seed its trip under a bare
`"user_id": "test"` — a string that was never a real pre-seeded demo user, so
no session_token could ever be minted for it (POST /session/login 404s on an
unknown user_id). Post-fix, every one of those rows became un-actionable (any
/refine call against it would 403 unconditionally). Fixed by re-seeding every
such row ANONYMOUSLY (`user_id=""`) with an explicit `owner_token` bound at
creation (`_ANON_OWNER_TOKEN`), and threading that same `owner_token` into
every /refine (and /confirm, for TestConfirmIdempotent) call that acts on it —
per the IDOR fix's tier-2 (anonymous) ownership model, NOT the tier-2
fail-open/legacy-row escape hatch (that path is exercised separately, in
test_replan_endpoint.py's TestReplanOwnership / test_confirm_endpoint.py).
"""
from __future__ import annotations

import copy
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient
from orchestration import server
from orchestration.store import SqliteDashboardStore, set_store, get_store

# ---------------------------------------------------------------------------
# Canned responses
# ---------------------------------------------------------------------------

_PLAN_IDK = "trip-test-plan-0001"
_SESSION_ID = "sess-test-0001"

_STRUCTURED_REQUEST = {
    "user_id": "test",
    "total_budget_cents": 500000,
    "today": "2026-10-01",
    "legs": [
        {"city": "tokyo", "place_key": "tokyo", "checkin": "2026-10-15",
         "checkout": "2026-10-19", "nights": 4, "adults": 2,
         "interests": ["food", "culture"]},
    ],
}

_PLAN_ENVELOPE = {
    "outcome": "plan_ready",
    "idempotency_key": _PLAN_IDK,
    "package_total_with_fees_cents": 500000,
    "legs": [{"city": "tokyo", "checkin": "2026-10-15", "checkout": "2026-10-19"}],
    "day_plans": [],
}

_NEW_IDK = "trip-test-plan-0002"
_NEW_ENVELOPE = {
    "outcome": "plan_ready",
    "idempotency_key": _NEW_IDK,
    "package_total_with_fees_cents": 425000,
    "legs": [{"city": "tokyo", "checkin": "2026-10-15", "checkout": "2026-10-19"}],
    "day_plans": [],
}

_DELTA_CHEAPER = {
    "ops": [{"op": "budget_adjust", "direction": "cheaper", "pct": 0.15}],
    "unsupported": False,
    "reason": None,
}

_DELTA_ADD_LEG = {
    "ops": [{"op": "add_leg", "city": "osaka", "vibe": None, "position": "end"}],
    "unsupported": False,
    "reason": None,
}

_DELTA_SWAP = {
    "ops": [{"op": "swap_item", "leg_city": "tokyo", "remove_name": "Ramen Place", "kind": "dining"}],
    "unsupported": False,
    "reason": None,
}

_DELTA_UNSUPPORTED = {
    "ops": [],
    "unsupported": True,
    "reason": "I can't do that yet.",
}

# #116: one recognised op (add_leg) + one genuinely unrecognised op (typo'd
# op name, outside the closed set) — the confirmed mixed-delta repro. Must
# apply the valid op and honestly report the unrecognised one, never silently
# drop it while reporting a clean success.
_DELTA_MIXED_VALID_AND_GARBAGE = {
    "ops": [
        # "singapore" is a real city in this repo's sample catalog (unlike the
        # private repo's "osaka" — see reference/README.md's "What's not here"
        # table for why this export ships a small hand-authored sample instead
        # of the full curated catalog); must resolve so this op actually applies.
        {"op": "add_leg", "city": "singapore", "vibe": None, "position": "end"},
        {"op": "add_lgs", "city": "porto"},
    ],
    "unsupported": False,
    "reason": None,
}

# #116 regression guard: EVERY op unrecognised — must still hit the
# total-failure safety net (refine_unsupported, no re-plan), not slip through
# now that unrecognised ops get a `changed` entry of their own.
_DELTA_ALL_GARBAGE = {
    "ops": [
        {"op": "frobnicate_legs", "city": "osaka"},
        {"op": "another_bad_op"},
    ],
    "unsupported": False,
    "reason": None,
}

# IDOR fix (tier 2, anonymous): every fixture row below is seeded ANONYMOUSLY
# (user_id="") and bound to this owner_token at creation — every /refine call
# that acts on it must present this SAME owner_token to pass the ownership gate.
_ANON_OWNER_TOKEN = "test-owner-token-refine-0001"


def _seed_store(store: SqliteDashboardStore, *, status: str = "plan_ready") -> None:
    """Seed a plan_ready row with a structured trip_request. Anonymous
    (user_id="") + owner_token-bound — see _ANON_OWNER_TOKEN / module docstring
    MIGRATION NOTE (IDOR fix)."""
    store.save_plan({
        "idempotency_key": _PLAN_IDK,
        "user_id": "",
        "owner_token": _ANON_OWNER_TOKEN,
        "package_total_cents": 500000,
        "request": _STRUCTURED_REQUEST,
        "envelope": dict(_PLAN_ENVELOPE),
    })
    if status == "booked":
        store.mark_booked(_PLAN_IDK, booking_ref="BK-test-001",
                          envelope=dict(_PLAN_ENVELOPE), confirmed_at="2026-01-01T00:00:00+00:00")


def _seed_store_with_envelope_extra(store: SqliteDashboardStore, extra: dict) -> None:
    """B5: same as _seed_store, but the persisted envelope carries EXTRA keys
    (e.g. a health_verdict/compliance_verdict/fraud_verdict/insurance dict) —
    mirrors what orchestrator.py's negotiate() actually attaches onto `result`
    during a REAL initial planning pass (see build_domain_answer's docstring)."""
    envelope = dict(_PLAN_ENVELOPE)
    envelope.update(extra)
    store.save_plan({
        "idempotency_key": _PLAN_IDK,
        "user_id": "",
        "owner_token": _ANON_OWNER_TOKEN,
        "package_total_cents": 500000,
        "request": _STRUCTURED_REQUEST,
        "envelope": envelope,
    })


def _login(c, user_id: str) -> str:
    """POST /session/login for a KNOWN demo user_id -> its session_token.
    Mirrors test_prefs_session.py's `_login` helper. Used by TestRefineOwnership
    (the tier-1 logged-in-trip ownership cases)."""
    r = c.post("/session/login", json={"user_id": user_id})
    assert r.status_code == 200, r.text
    return r.json()["session_token"]


# ---------------------------------------------------------------------------
# We test /refine by injecting an in-memory store + monkeypatching the LLM
# and the orchestrator's negotiate call (so the test is fully deterministic
# and doesn't start the catalog-loading heavy orch).
# ---------------------------------------------------------------------------

class _MockOrch:
    """Minimal orchestrator stub: negotiate returns a pre-canned result."""
    def __init__(self, result_fn):
        self._result_fn = result_fn

    def negotiate(self, req, *, commit=True):
        return self._result_fn(req)


class _CommitCapturingOrch:
    """Orchestrator stub that RECORDS the commit kwarg passed to negotiate.

    b1 regression: /refine must always pass commit=False. This class makes
    the kwarg observable so a test can assert it — unlike _MockOrch which
    silently ignores it (the audit gap).
    """
    def __init__(self, result_fn=None):
        self.captured_commits: list[bool] = []
        self._result_fn = result_fn or (lambda req: {
            "_trip_request": req,
            "outcome": "plan_ready",
            "idempotency_key": _NEW_IDK,
            "package_total_with_fees_cents": 425000,
            "legs": req.get("legs") or [],
            "day_plans": [],
        })

    def negotiate(self, req, *, commit=True):
        self.captured_commits.append(commit)
        return self._result_fn(req)


class _CommitPlanCapturingOrch:
    """Orchestrator stub that captures commit_plan calls (for /confirm tests).

    b6 regression: /confirm must be idempotent — commit_plan is called exactly
    once regardless of how many times /confirm is requested with the same key.
    """
    def __init__(self):
        self.commit_plan_calls = 0
        self.booking_ref = "BK-ci-audit-001"

    def commit_plan(self, *, user_id, checkout_id, idempotency_key,
                    plan_envelope, dest_token, merchant_user_id=""):
        self.commit_plan_calls += 1
        return {
            "outcome": "success",
            "booking_ref": self.booking_ref,
            "idempotency_key": idempotency_key,
            "payment_status": "paid",
        }

    def negotiate(self, req, *, commit=True):
        return {
            "_trip_request": req,
            "outcome": "plan_ready",
            "idempotency_key": _NEW_IDK,
            "package_total_with_fees_cents": 425000,
            "legs": req.get("legs") or [],
            "day_plans": [],
        }


class TestRefineEndpoint(unittest.TestCase):

    def _client_with_store_and_orch(self, negotiate_fn=None):
        """Return (TestClient, store, mock_orch) with an injected in-memory store.

        IMPORTANT: the caller must assign `server._state.orch = mock_orch` AFTER
        entering `with client:`, NOT here — build_app()'s startup lifespan only
        runs on __enter__ and unconditionally resets _state.orch to a REAL
        orchestrator, so an assignment made before that point (as this helper
        used to do) is silently overwritten and the request runs against the
        real (slow, non-deterministic-w.r.t-catalog) orchestrator instead of the
        mock. (Discovered during the IDOR-fix test migration: this pre-existing
        bug had been silently masked because the affected tests' assertions
        happened to be loose enough to pass against either orchestrator.)"""
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        _seed_store(store)

        # Default negotiate function: return a cheaper plan_ready.
        if negotiate_fn is None:
            def negotiate_fn(req):
                return {
                    "_trip_request": req,
                    "outcome": "plan_ready",
                    "idempotency_key": _NEW_IDK,
                    "package_total_with_fees_cents": 425000,
                    "legs": req.get("legs") or [],
                    "day_plans": [],
                }

        mock_orch = _MockOrch(negotiate_fn)
        client = TestClient(server.build_app())
        return client, store, mock_orch

    def test_missing_idempotency_key_400(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        with TestClient(server.build_app()) as client:
            r = client.post("/refine", json={"message": "make it cheaper"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["outcome"], "invalid_request")

    def test_missing_message_400(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        with TestClient(server.build_app()) as client:
            r = client.post("/refine", json={"idempotency_key": "trip-x"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["outcome"], "invalid_request")

    def test_unknown_plan_returns_unknown_plan(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        with TestClient(server.build_app()) as client:
            r = client.post("/refine", json={
                "idempotency_key": "trip-does-not-exist",
                "message": "make it cheaper",
            })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["outcome"], "unknown_plan")

    def test_booked_plan_returns_plan_locked(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        _seed_store(store, status="booked")
        with TestClient(server.build_app()) as client:
            r = client.post("/refine", json={
                "idempotency_key": _PLAN_IDK,
                "message": "make it cheaper",
                "owner_token": _ANON_OWNER_TOKEN,
            })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["outcome"], "plan_locked")

    def test_budget_down_refine_returns_new_plan_ready(self):
        with patch("utils.followup_parser.parse_followup", return_value=_DELTA_CHEAPER):
            client, store, mock_orch = self._client_with_store_and_orch()
            with client:
                server._state.orch = mock_orch
                r = client.post("/refine", json={
                    "idempotency_key": _PLAN_IDK,
                    "message": "make it cheaper",
                    "owner_token": _ANON_OWNER_TOKEN,
                })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["outcome"], "plan_ready", body)
        # New idempotency_key must differ from the original.
        self.assertNotEqual(body.get("idempotency_key"), _PLAN_IDK)
        # assistant_reply is a non-empty string.
        self.assertIsInstance(body.get("assistant_reply"), str)
        self.assertGreater(len(body["assistant_reply"]), 5)
        # plan envelope is included.
        self.assertIn("plan", body)
        # session_id is present.
        self.assertIsNotNone(body.get("session_id"))

    def test_genuine_change_request_extend_by_2_days_still_structural_not_domain_answer(self):
        """B5 anti-regression at the HTTP layer: a REAL change request ('extend
        our trip by 2 days' — question_domain=None, a genuine op present) must
        still take the ORIGINAL structural-change path (re-plan via
        orch.negotiate(), new idempotency_key, outcome="plan_ready") — it must
        NOT be misrouted into the new B5 domain_answer branch just because
        that branch now exists."""
        delta_extend = {
            "ops": [{"op": "adjust_nights", "delta_nights": 2}],
            "unsupported": False, "reason": None, "question_domain": None,
        }
        with patch("utils.followup_parser.parse_followup", return_value=delta_extend):
            client, store, mock_orch = self._client_with_store_and_orch()
            with client:
                server._state.orch = mock_orch
                r = client.post("/refine", json={
                    "idempotency_key": _PLAN_IDK,
                    "message": "extend our trip by 2 days",
                    "owner_token": _ANON_OWNER_TOKEN,
                })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["outcome"], "plan_ready", body)
        self.assertNotEqual(body["outcome"], "domain_answer")
        self.assertNotIn("answer", body)
        # Real re-plan happened — new idempotency_key minted (unlike domain_answer,
        # which keeps the SAME key because nothing was replanned).
        self.assertNotEqual(body.get("idempotency_key"), _PLAN_IDK)

    def test_conversation_persists_and_threads(self):
        """Two refines thread through the same session: second gets the prior turn in context."""
        with patch("utils.followup_parser.parse_followup", return_value=_DELTA_CHEAPER):
            client, store, mock_orch = self._client_with_store_and_orch()
            with client:
                server._state.orch = mock_orch
                r1 = client.post("/refine", json={
                    "idempotency_key": _PLAN_IDK,
                    "message": "make it cheaper",
                    "owner_token": _ANON_OWNER_TOKEN,
                })
            body1 = r1.json()
            session_id = body1.get("session_id")
            new_key = body1.get("idempotency_key")

        # Seed the new plan row so the second refine can find it. Anonymous
        # (user_id="") to match the parent — the endpoint already server-side
        # propagates the parent's owner_token onto this child row (refine()'s
        # ownership-continuity step), and save_plan's owner_token column is
        # write-once, so passing "" here does not clobber the inherited token.
        store.save_plan({
            "idempotency_key": new_key,
            "user_id": "",
            "package_total_cents": 425000,
            "request": dict(_STRUCTURED_REQUEST, total_budget_cents=425000),
            "envelope": {**_NEW_ENVELOPE, "idempotency_key": new_key},
        })

        # Second refine using the returned session_id.
        def negotiate_v2(req):
            return {
                "_trip_request": req,
                "outcome": "plan_ready",
                "idempotency_key": "trip-test-plan-0003",
                "package_total_with_fees_cents": 361250,
                "legs": req.get("legs") or [],
                "day_plans": [],
            }

        with patch("utils.followup_parser.parse_followup", return_value=_DELTA_CHEAPER):
            set_store(store)
            server._state.orch = _MockOrch(negotiate_v2)
            with TestClient(server.build_app()) as client2:
                server._state.orch = _MockOrch(negotiate_v2)
                r2 = client2.post("/refine", json={
                    "idempotency_key": new_key,
                    "message": "even cheaper",
                    "session_id": session_id,
                    "owner_token": _ANON_OWNER_TOKEN,
                })
        body2 = r2.json()
        self.assertEqual(body2["outcome"], "plan_ready")
        # Check conversation has grown.
        conv = store.get_conversation(session_id)
        self.assertIsNotNone(conv)
        # Should have: seed turn + 2 user turns + 2 assistant turns = 5
        self.assertGreaterEqual(len(conv["turns"]), 4)

    def test_plan_ready_response_includes_structured_diff(self):
        """B3: /refine's plan_ready response carries the new "diff" field
        (alongside "changed", kept verbatim for backward compat), and a city
        swap (remove_leg + add_leg — NOT set_nights/adjust_nights) that also
        shrinks total nights via apply_delta's default-nights redistribution
        gets total_nights.side_effect=True — the exact live-prod finding this
        task fixes: the user only asked to swap a city, never asked for a
        shorter trip, and the OLD response had no structured way to say so."""
        swap_idk = "trip-test-plan-swap-0001"
        # "singapore"/"canggu" are real cities in this repo's sample catalog
        # (unlike the private repo's "osaka"/"kyoto" -- see reference/README.md's
        # "What's not here" table); must resolve so add_leg actually applies.
        two_leg_request = {
            "user_id": "",
            "total_budget_cents": 500000,
            "today": "2026-10-01",
            "legs": [
                {"city": "tokyo", "place_key": "tokyo", "checkin": "2026-10-15",
                 "checkout": "2026-10-19", "nights": 4, "adults": 2},
                {"city": "singapore", "place_key": "singapore", "checkin": "2026-10-19",
                 "checkout": "2026-10-23", "nights": 4, "adults": 2},
            ],
        }
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        store.save_plan({
            "idempotency_key": swap_idk,
            "user_id": "",
            "owner_token": _ANON_OWNER_TOKEN,
            "package_total_cents": 500000,
            "request": two_leg_request,
            "envelope": {**_PLAN_ENVELOPE, "idempotency_key": swap_idk, "legs": two_leg_request["legs"]},
        })

        swap_delta = {
            "ops": [
                {"op": "remove_leg", "city": "singapore"},
                {"op": "add_leg", "city": "canggu", "vibe": None, "position": "end"},
            ],
            "unsupported": False,
            "reason": None,
        }

        def negotiate_fn(req):
            return {
                "_trip_request": req,
                "outcome": "plan_ready",
                "idempotency_key": "trip-test-plan-swap-0002",
                "package_total_with_fees_cents": 500000,
                "legs": req.get("legs") or [],
                "day_plans": [],
            }

        with patch("utils.followup_parser.parse_followup", return_value=swap_delta):
            with TestClient(server.build_app()) as client:
                server._state.orch = _MockOrch(negotiate_fn)
                r = client.post("/refine", json={
                    "idempotency_key": swap_idk,
                    "message": "swap Singapore for Canggu",
                    "owner_token": _ANON_OWNER_TOKEN,
                })

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["outcome"], "plan_ready", body)
        self.assertIn("diff", body)
        self.assertIn("changed", body)  # backward-compat field untouched

        diff = body["diff"]
        self.assertEqual(diff["total_nights"]["old"], 8)
        self.assertEqual(diff["total_nights"]["new"], 6)
        self.assertEqual(diff["total_nights"]["delta"], -2)
        self.assertTrue(diff["total_nights"]["side_effect"],
                         "total nights shrank without an explicit nights op -- must be flagged")
        self.assertEqual(diff["legs_removed"], [{"city": "singapore", "nights": 4, "side_effect": False}])
        self.assertEqual(len(diff["legs_added"]), 1)
        self.assertEqual(diff["legs_added"][0]["city"], "canggu")
        self.assertFalse(diff["legs_added"][0]["side_effect"])
        self.assertFalse(diff["total_budget_cents"]["side_effect"])

    def test_cannot_satisfy_returns_kept_previous(self):
        def fail_negotiate(req):
            return {"outcome": "cannot_satisfy", "reason": "over_budget"}

        with patch("utils.followup_parser.parse_followup", return_value=_DELTA_CHEAPER):
            store = SqliteDashboardStore(":memory:")
            set_store(store)
            _seed_store(store)
            with TestClient(server.build_app()) as client:
                server._state.orch = _MockOrch(fail_negotiate)
                r = client.post("/refine", json={
                    "idempotency_key": _PLAN_IDK,
                    "message": "make it cheaper",
                    "owner_token": _ANON_OWNER_TOKEN,
                })
        body = r.json()
        self.assertTrue(body.get("kept_previous"), body)
        self.assertIsInstance(body.get("assistant_reply"), str)

    def test_swap_item_only_returns_refine_partial(self):
        with patch("utils.followup_parser.parse_followup", return_value=_DELTA_SWAP):
            store = SqliteDashboardStore(":memory:")
            set_store(store)
            _seed_store(store)
            with TestClient(server.build_app()) as client:
                r = client.post("/refine", json={
                    "idempotency_key": _PLAN_IDK,
                    "message": "swap the ramen place",
                    "owner_token": _ANON_OWNER_TOKEN,
                })
        body = r.json()
        self.assertEqual(body["outcome"], "refine_partial", body)
        # idempotency_key stays the same (no re-plan).
        self.assertEqual(body.get("idempotency_key"), _PLAN_IDK)

    def test_unsupported_delta_returns_refine_unsupported(self):
        with patch("utils.followup_parser.parse_followup", return_value=_DELTA_UNSUPPORTED):
            store = SqliteDashboardStore(":memory:")
            set_store(store)
            _seed_store(store)
            with TestClient(server.build_app()) as client:
                r = client.post("/refine", json={
                    "idempotency_key": _PLAN_IDK,
                    "message": "do something unsupported",
                    "owner_token": _ANON_OWNER_TOKEN,
                })
        body = r.json()
        self.assertEqual(body["outcome"], "refine_unsupported", body)

    def test_legacy_plan_no_legs_returns_refine_unavailable(self):
        """Plan stored with a raw free-text body (no legs) → refine_unavailable, honest."""
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        # Save a legacy row: request has no `legs` key. Anonymous + owner_token-bound
        # (IDOR fix migration — "legacy" here means legacy REQUEST SHAPE, not a
        # legacy/un-owned row; case 12 in TestRefineOwnership covers the
        # owner_token='' fail-open transition separately).
        store.save_plan({
            "idempotency_key": "trip-legacy",
            "user_id": "",
            "owner_token": _ANON_OWNER_TOKEN,
            "package_total_cents": 300000,
            "request": {"text": "a week in paris $3000"},
            "envelope": {"outcome": "plan_ready", "idempotency_key": "trip-legacy"},
        })
        with TestClient(server.build_app()) as client:
            r = client.post("/refine", json={
                "idempotency_key": "trip-legacy",
                "message": "make it cheaper",
                "owner_token": _ANON_OWNER_TOKEN,
            })
        body = r.json()
        self.assertEqual(body["outcome"], "refine_unavailable", body)
        self.assertIn("assistant_reply", body)

    def test_mixed_valid_and_unrecognized_delta_applies_valid_and_reports_rest(self):
        """#116: a delta mixing one valid add_leg with one unrecognised op
        must NOT be silently swallowed. It must re-plan on the valid op
        (plan_ready, safety net does not fire) AND the response's `changed`
        must honestly name the unrecognised op as skipped — never a clean
        success with zero trace of the dropped part of the request."""
        from utils.followup_parser import UNSUPPORTED_OP_PREFIX
        with patch("utils.followup_parser.parse_followup",
                   return_value=_DELTA_MIXED_VALID_AND_GARBAGE):
            client, store, mock_orch = self._client_with_store_and_orch()
            with client:
                server._state.orch = mock_orch
                r = client.post("/refine", json={
                    "idempotency_key": _PLAN_IDK,
                    "message": "add singapore and also add_lgs porto",
                    "owner_token": _ANON_OWNER_TOKEN,
                })
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # The valid op applied → a normal re-plan happened (safety net did NOT fire).
        self.assertEqual(body["outcome"], "plan_ready", body)
        self.assertNotEqual(body.get("idempotency_key"), _PLAN_IDK)
        changed = body.get("changed") or []
        self.assertTrue(any("legs.add" in c and "singapore" in c for c in changed), changed)
        # The garbage op is honestly reported, not silently dropped.
        unsupported_entries = [c for c in changed if c.startswith(UNSUPPORTED_OP_PREFIX)]
        self.assertEqual(len(unsupported_entries), 1, changed)
        self.assertIn("add_lgs", unsupported_entries[0])

    def test_fully_unrecognized_delta_still_returns_refine_unsupported(self):
        """#116 regression guard: when EVERY op in the delta is unrecognised
        (no valid op at all), the total-failure safety net must still fire —
        refine_unsupported, no re-plan — exactly as it did before #116 added
        `changed` entries for unrecognised ops. (The safety net filters those
        entries out before testing `changed`, specifically so this case
        doesn't regress into a false-success replan of an unmodified trip.)"""
        with patch("utils.followup_parser.parse_followup", return_value=_DELTA_ALL_GARBAGE):
            store = SqliteDashboardStore(":memory:")
            set_store(store)
            _seed_store(store)
            negotiate_called = []

            def tracking_negotiate(req):
                negotiate_called.append(req)
                return {"outcome": "plan_ready", "idempotency_key": "trip-x"}

            with TestClient(server.build_app()) as client:
                server._state.orch = _MockOrch(tracking_negotiate)
                r = client.post("/refine", json={
                    "idempotency_key": _PLAN_IDK,
                    "message": "frobnicate the legs somehow",
                    "owner_token": _ANON_OWNER_TOKEN,
                })
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["outcome"], "refine_unsupported", body)
        # No re-plan happened, so the idempotency_key stays on the original plan.
        self.assertEqual(body.get("idempotency_key"), _PLAN_IDK)
        self.assertEqual(negotiate_called, [],
                         "negotiate must NOT be called when nothing in the delta was recognised")


class TestRefineDomainAnswer(unittest.TestCase):
    """B5: /refine's third response mode — a health/fraud/insurance/compliance
    QUESTION routed to build_domain_answer, WITHOUT calling negotiate() (see
    followup_parser.build_domain_answer's docstring for why this is fast: a
    pure read of the verdict already on the plan's envelope, no re-plan)."""

    _DELTA_QUESTION_HEALTH = {
        "ops": [], "unsupported": False, "reason": None, "question_domain": "health",
    }
    _DELTA_QUESTION_FRAUD = {
        "ops": [], "unsupported": False, "reason": None, "question_domain": "fraud",
    }
    _DELTA_QUESTION_COMPLIANCE = {
        "ops": [], "unsupported": False, "reason": None, "question_domain": "compliance",
    }

    def test_domain_answer_grounded_in_stored_verdict(self):
        """The trip's envelope already carries a health_verdict (as a REAL
        initial planning pass would attach) → grounded:True, informative
        headline, plan UNCHANGED (same idempotency_key, no re-plan)."""
        from agents.health_agent import assess as health_assess
        health_verdict = health_assess(
            legs=[{"place_key": "ethiopia", "departure_date": "2026-08-01"}],
            today="2026-06-01",
        )
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        _seed_store_with_envelope_extra(store, {"health_verdict": health_verdict})
        with patch("utils.followup_parser.parse_followup",
                   return_value=self._DELTA_QUESTION_HEALTH):
            with TestClient(server.build_app()) as client:
                r = client.post("/refine", json={
                    "idempotency_key": _PLAN_IDK,
                    "message": "is this covered if I get sick",
                    "owner_token": _ANON_OWNER_TOKEN,
                })
        body = r.json()
        self.assertEqual(body["outcome"], "domain_answer", body)
        # Plan unchanged — same idempotency_key, no new generation minted.
        self.assertEqual(body["idempotency_key"], _PLAN_IDK)
        self.assertIn("answer", body)
        self.assertEqual(body["answer"]["domain"], "health")
        self.assertTrue(body["answer"]["grounded"])
        self.assertTrue(body["answer"]["headline"])
        # Backward-compat: assistant_reply is ALWAYS present (an old FE that
        # has never heard of "answer"/domain_answer still gets truthful prose).
        self.assertEqual(body["assistant_reply"], body["answer"]["headline"])

    def test_compliance_domain_answer_discloses_passport_override(self):
        """[#88 HIGH / PR #117] full-stack regression pin: a real
        compliance_verdict carrying a primary_passport_override rescue (primary
        nationality genuinely BLOCKED, substitute passport a clean allowed=True
        rescue) must reach the end user through the REAL /refine endpoint with
        grounded:True AND a headline that discloses the override -- not the
        generic "I don't have a check on file" masking message compliance_
        agent.explain_block()'s pre-fix empty-headline bug produced via
        build_domain_answer's `or` fallback."""
        from agents.compliance_agent import check_eligibility
        compliance_verdict = check_eligibility(
            legs=[{"dest_country": "US", "departure_date": "2026-07-01", "leg_id": "0"}],
            nationality="IN",
            nationalities=["IN", "DE"],
            today="2026-06-23",
        )
        # Sanity: this really is the clean-rescue shape the bug targets.
        self.assertTrue(compliance_verdict["per_leg"][0].get("primary_passport_override"))
        self.assertTrue(compliance_verdict["per_leg"][0].get("allowed"))

        store = SqliteDashboardStore(":memory:")
        set_store(store)
        _seed_store_with_envelope_extra(store, {"compliance_verdict": compliance_verdict})
        with patch("utils.followup_parser.parse_followup",
                   return_value=self._DELTA_QUESTION_COMPLIANCE):
            with TestClient(server.build_app()) as client:
                r = client.post("/refine", json={
                    "idempotency_key": _PLAN_IDK,
                    "message": "am I actually allowed to enter on this trip",
                    "owner_token": _ANON_OWNER_TOKEN,
                })
        body = r.json()
        self.assertEqual(body["outcome"], "domain_answer", body)
        self.assertEqual(body["idempotency_key"], _PLAN_IDK)
        self.assertEqual(body["answer"]["domain"], "compliance")
        self.assertTrue(body["answer"]["grounded"])
        headline = body["answer"]["headline"]
        self.assertNotIn("I don't have a visa/entry-eligibility check on file", headline)
        self.assertIn("BLOCKED", headline)
        self.assertIn("IN", headline)
        self.assertIn("DE", headline)
        self.assertEqual(body["assistant_reply"], headline)

    def test_domain_answer_honestly_ungrounded_when_domain_never_fired(self):
        """No fraud_verdict on the envelope (this trip has no counterparties) →
        grounded:False with an honest explanation, NOT refine_unsupported."""
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        _seed_store(store)  # plain envelope, no fraud_verdict
        with patch("utils.followup_parser.parse_followup",
                   return_value=self._DELTA_QUESTION_FRAUD):
            with TestClient(server.build_app()) as client:
                r = client.post("/refine", json={
                    "idempotency_key": _PLAN_IDK,
                    "message": "is my payment safe",
                    "owner_token": _ANON_OWNER_TOKEN,
                })
        body = r.json()
        self.assertEqual(body["outcome"], "domain_answer", body)
        self.assertFalse(body["answer"]["grounded"])
        self.assertEqual(body["answer"]["domain"], "fraud")
        self.assertIn("idempotency_key", body)
        self.assertEqual(body["idempotency_key"], _PLAN_IDK)

    def test_domain_answer_does_not_call_negotiate(self):
        """A question must NEVER re-enter the re-plan machinery — assert
        negotiate() is never invoked on the injected orch."""
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        _seed_store(store)
        mock_orch = _CommitCapturingOrch()
        with patch("utils.followup_parser.parse_followup",
                   return_value=self._DELTA_QUESTION_HEALTH):
            with TestClient(server.build_app()) as client:
                server._state.orch = mock_orch
                r = client.post("/refine", json={
                    "idempotency_key": _PLAN_IDK,
                    "message": "is this covered if I get sick",
                    "owner_token": _ANON_OWNER_TOKEN,
                })
        self.assertEqual(r.json()["outcome"], "domain_answer")
        self.assertEqual(mock_orch.captured_commits, [])  # negotiate() never called

    def test_domain_answer_conversation_persists(self):
        """The question + answer are threaded into the conversation like any
        other /refine turn (session_id returned, turns appended)."""
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        _seed_store(store)
        with patch("utils.followup_parser.parse_followup",
                   return_value=self._DELTA_QUESTION_FRAUD):
            with TestClient(server.build_app()) as client:
                r = client.post("/refine", json={
                    "idempotency_key": _PLAN_IDK,
                    "message": "is my payment safe",
                    "owner_token": _ANON_OWNER_TOKEN,
                })
        body = r.json()
        session_id = body.get("session_id")
        self.assertTrue(session_id)
        conv = store.get_conversation(session_id)
        roles = [t["role"] for t in (conv.get("turns") or [])]
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)


class TestRefineShorthandOpShapeLaunchBlocker(unittest.TestCase):
    """LAUNCH BLOCKER (live QA, 2026-07-08): a real single-leg trip, "swap
    Tokyo for Singapore" (private-repo trace used Lisbon/Porto — swapped here
    for cities that exist in this repo's sample catalog; see
    reference/README.md's "What's not here" table). The live parser returned
    ops in SHORTHAND shape ({"remove_leg":"tokyo"} / {"add_leg":{...}}, no "op"
    key) instead of the documented {"op":"remove_leg","city":"tokyo"} shape.
    apply_delta's dispatch (keyed off `op.get("op")`) silently dropped both
    ops as unknown: changed==[], plan.legs stayed Tokyo-only, yet the endpoint
    still reported outcome="plan_ready", unsupported=false, reason=None, and
    build_assistant_reply fabricated a confident-sounding summary line.

    This end-to-end test drives the REAL /refine handler (server.refine, real
    apply_delta/compute_refine_diff/build_assistant_reply — only parse_followup's
    LLM call and the orchestrator's negotiate() are stubbed, per this file's
    existing harness pattern) with the EXACT malformed delta shape observed
    live, over a real single-leg trip. Pre-fix this fails (Tokyo survives,
    changed==[]); post-fix Singapore replaces Tokyo and `changed` truthfully
    reflects the swap.
    """

    _TOKYO_IDK = "trip-test-plan-tokyo-0001"

    _TOKYO_REQUEST = {
        "user_id": "",
        "total_budget_cents": 126000,
        "today": "2026-10-01",
        "legs": [
            {"city": "tokyo", "place_key": "tokyo", "checkin": "2026-10-15",
             "checkout": "2026-10-21", "nights": 6, "adults": 1},
        ],
    }

    # The EXACT shape quoted in the live QA trace — op-name-as-key shorthand,
    # no "op" field, unlike every hand-authored fixture elsewhere in this file.
    _DELTA_SHORTHAND_SWAP = {
        "ops": [
            {"remove_leg": "tokyo"},
            {"add_leg": {"city": "Singapore", "position": "end"}},
        ],
        "unsupported": False,
        "reason": None,
    }

    def _seed_tokyo(self, store: SqliteDashboardStore) -> None:
        store.save_plan({
            "idempotency_key": self._TOKYO_IDK,
            "user_id": "",
            "owner_token": _ANON_OWNER_TOKEN,
            "package_total_cents": 126000,
            "request": self._TOKYO_REQUEST,
            "envelope": {
                "outcome": "plan_ready",
                "idempotency_key": self._TOKYO_IDK,
                "package_total_with_fees_cents": 126000,
                "legs": self._TOKYO_REQUEST["legs"],
                "day_plans": [],
            },
        })

    def test_shorthand_swap_actually_replaces_tokyo_with_singapore(self):
        def negotiate_fn(req):
            # Mirrors this file's existing negotiate stubs: echoes req["legs"]
            # back into the plan_ready envelope, exactly like the real
            # orchestrator would for a plan-only re-negotiate.
            return {
                "_trip_request": req,
                "outcome": "plan_ready",
                "idempotency_key": "trip-test-plan-tokyo-0002",
                "package_total_with_fees_cents": 126000,
                "legs": req.get("legs") or [],
                "day_plans": [],
            }

        store = SqliteDashboardStore(":memory:")
        set_store(store)
        self._seed_tokyo(store)

        with patch("utils.followup_parser.parse_followup", return_value=self._DELTA_SHORTHAND_SWAP):
            with TestClient(server.build_app()) as client:
                server._state.orch = _MockOrch(negotiate_fn)
                r = client.post("/refine", json={
                    "idempotency_key": self._TOKYO_IDK,
                    "message": "swap Tokyo for Singapore",
                    "owner_token": _ANON_OWNER_TOKEN,
                })

        self.assertEqual(r.status_code, 200)
        body = r.json()

        # The plan must ACTUALLY reflect the swap — not silently stay on Tokyo.
        plan_cities = [l.get("city") for l in (body.get("plan") or {}).get("legs") or []]
        self.assertNotIn("tokyo", plan_cities,
                          f"Tokyo must be gone from the returned plan; body={body}")
        self.assertIn("singapore", plan_cities,
                       f"Singapore must be present in the returned plan; body={body}")

        # `changed` must truthfully record a real modification, not [].
        self.assertTrue(body.get("changed"),
                         f"a real city swap must not report changed=[]; body={body}")

        # If the handler decided nothing could be applied, it must say so
        # honestly (refine_unsupported/refine_partial) rather than claim
        # plan_ready with an unchanged plan. Given the swap DID apply above,
        # outcome must be the honest plan_ready success case.
        self.assertEqual(body.get("outcome"), "plan_ready", body)


class TestRefineVar0(unittest.TestCase):
    """var-0 guard: negotiate(req) is byte-identical for the same input; /refine uses a NEW digest."""

    def test_two_negotiate_calls_same_input_identical_digest(self):
        """If apply_delta produces the same new_request twice, negotiate returns the same key."""
        from orchestration.orchestrator import TravelOrchestrator
        from tests.test_trace_var0 import _PM_TRIP
        from tests.test_wallet_sim import _build_society
        from tests.test_trace_var0 import _merchant_result
        import copy

        COMPLETE_OK = _merchant_result({
            "id": "co_pm", "status": "complete", "user_id": "trace-test",
            "line_items": [], "total_cents": 54000, "currency": "USD",
            "buyer_consent": True, "booking_ref": "BK-trace-1",
            "wallet_session_id": "trip-x", "wallet_debit_cents": 54000,
            "wallet_balance_cents": 446000, "simulated": True,
        })
        FUND_OK = _merchant_result({
            "status": "ok", "wallet_session_id": "trip-x", "seed_cents": 500000,
            "balance_cents": 500000, "simulated": True, "note": "sim",
        })
        orch = _build_society(complete_resp=(200, COMPLETE_OK), fund_resp=(200, FUND_OK))
        r1 = orch.negotiate(copy.deepcopy(_PM_TRIP), commit=False)
        r2 = orch.negotiate(copy.deepcopy(_PM_TRIP), commit=False)
        self.assertEqual(r1.get("idempotency_key"), r2.get("idempotency_key"),
                         "Same input must produce same idempotency_key (var-0)")

    def test_negotiate_not_called_on_refine_partial(self):
        """When /refine can't apply ops (swap_item only), negotiate is never called."""
        with patch("utils.followup_parser.parse_followup", return_value=_DELTA_SWAP):
            store = SqliteDashboardStore(":memory:")
            set_store(store)
            _seed_store(store)
            negotiate_called = []

            def tracking_negotiate(req):
                negotiate_called.append(req)
                return {"outcome": "plan_ready", "idempotency_key": "trip-x"}

            with TestClient(server.build_app()) as client:
                server._state.orch = _MockOrch(tracking_negotiate)
                r = client.post("/refine", json={
                    "idempotency_key": _PLAN_IDK,
                    "message": "swap the ramen place",
                    "owner_token": _ANON_OWNER_TOKEN,
                })
        # Threading owner_token keeps this exercising the REAL swap-only path
        # (not the ownership 403, which would make the assertion below vacuous).
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(negotiate_called, [],
                         "negotiate must NOT be called on swap_item-only refine")


class TestRefineFixAndLeaks(unittest.TestCase):
    """FIX-FIRST regression + envelope-leak + plan-only guards."""

    def test_anon_refines_on_shared_key_do_not_join(self):
        """FIX-FIRST regression: two ANONYMOUS /refine on the SAME idempotency_key with NO
        session_id must each get a FRESH thread — never join via get_conversation_by_active_key
        (which bled anon user A's conversation into anon user B). Uses the swap path (no re-plan,
        so the active key stays _PLAN_IDK and the old fallback WOULD have matched)."""
        with patch("utils.followup_parser.parse_followup", return_value=_DELTA_SWAP):
            store = SqliteDashboardStore(":memory:")
            set_store(store)
            _seed_store(store)
            with TestClient(server.build_app()) as client:
                ra = client.post("/refine", json={"idempotency_key": _PLAN_IDK,
                                                  "message": "swap the ramen place for A",
                                                  "owner_token": _ANON_OWNER_TOKEN})
                sa = ra.json()["session_id"]
                rb = client.post("/refine", json={"idempotency_key": _PLAN_IDK,
                                                  "message": "swap the ramen place for B",
                                                  "owner_token": _ANON_OWNER_TOKEN})
                sb = rb.json()["session_id"]
        self.assertIsNotNone(sa)
        self.assertIsNotNone(sb)
        self.assertNotEqual(sa, sb, "anon refines on a shared idk must NOT join the same thread")
        conv_a = store.get_conversation(sa)
        a_msgs = [t.get("content") for t in (conv_a["turns"] if conv_a else [])]
        self.assertNotIn("swap the ramen place for B", a_msgs,
                         "anonymous user B's message must NOT bleed into user A's thread")

    def test_refine_strips_confirm_ctx_and_checkout_id(self):
        """The refined plan_ready returned to the client must NOT leak _confirm_ctx or the
        server-only merchant checkout_id (sanitized via _persist_and_sanitize_plan)."""
        def negotiate_fn(req):
            return {"_trip_request": req, "outcome": "plan_ready", "idempotency_key": _NEW_IDK,
                    "legs": req.get("legs") or [], "package_total_with_fees_cents": 425000,
                    "day_plans": [], "checkout_id": "co-secret-1",
                    "_confirm_ctx": {"checkout_id": "co-secret-1", "x": 1}}
        with patch("utils.followup_parser.parse_followup", return_value=_DELTA_CHEAPER):
            store = SqliteDashboardStore(":memory:")
            set_store(store)
            _seed_store(store)
            with TestClient(server.build_app()) as client:
                server._state.orch = _MockOrch(negotiate_fn)
                r = client.post("/refine", json={"idempotency_key": _PLAN_IDK, "message": "cheaper",
                                                 "owner_token": _ANON_OWNER_TOKEN})
        plan = r.json().get("plan") or {}
        self.assertNotIn("_confirm_ctx", plan, "must strip _confirm_ctx from the returned plan")
        self.assertNotIn("checkout_id", plan, "must strip server-only checkout_id from the returned plan")

    def test_refine_success_is_held_never_books_or_debits(self):
        """A budget-down refine returns a HELD plan_ready — no booking_ref, no wallet debit."""
        def negotiate_fn(req):
            return {"_trip_request": req, "outcome": "plan_ready", "idempotency_key": _NEW_IDK,
                    "booking_ref": None, "payment_status": "held", "wallet": {"debited": False},
                    "legs": req.get("legs") or [], "package_total_with_fees_cents": 425000,
                    "day_plans": []}
        with patch("utils.followup_parser.parse_followup", return_value=_DELTA_CHEAPER):
            store = SqliteDashboardStore(":memory:")
            set_store(store)
            _seed_store(store)
            with TestClient(server.build_app()) as client:
                server._state.orch = _MockOrch(negotiate_fn)
                r = client.post("/refine", json={"idempotency_key": _PLAN_IDK, "message": "cheaper",
                                                 "owner_token": _ANON_OWNER_TOKEN})
        plan = r.json().get("plan") or {}
        self.assertIsNone(plan.get("booking_ref"))
        self.assertEqual(plan.get("payment_status"), "held")
        self.assertFalse((plan.get("wallet") or {}).get("debited"))

    def test_refine_is_plan_only_commit_false(self):
        """b1 — PLAN-ONLY regression: /refine must pass commit=False to negotiate.

        _MockOrch silently ignores the commit kwarg (the audit gap). This test
        uses _CommitCapturingOrch to OBSERVE the kwarg.  It MUST fail (red) if
        someone flips server.py line 1775 from commit=False → commit=True."""
        with patch("utils.followup_parser.parse_followup", return_value=_DELTA_CHEAPER):
            store = SqliteDashboardStore(":memory:")
            set_store(store)
            _seed_store(store)
            capturing = _CommitCapturingOrch()
            with TestClient(server.build_app()) as client:
                server._state.orch = capturing
                r = client.post("/refine", json={
                    "idempotency_key": _PLAN_IDK,
                    "message": "make it cheaper",
                    "owner_token": _ANON_OWNER_TOKEN,
                })
        self.assertEqual(r.status_code, 200)
        self.assertTrue(capturing.captured_commits,
                        "negotiate was never called — test is broken")
        for i, commit_val in enumerate(capturing.captured_commits):
            self.assertFalse(
                commit_val,
                f"negotiate call #{i}: commit={commit_val!r} — /refine MUST pass "
                f"commit=False (plan-only); if this fails, server.py commit kwarg flipped to True",
            )

    def test_refine_money_invariant_cheaper_reduces_total(self):
        """b3 — MONEY-INVARIANT: a budget-down refine produces a LOWER package total.

        The test captures the original total (500000¢) from the stored plan and
        the new total (425000¢) from negotiate's response, then asserts:
          - new_total < original_total  (cheaper means cheaper)
          - new_total == exactly what negotiate returned  (no silent rounding up)
          - status stays plan_ready (HELD, not booked or debited)
        """
        ORIGINAL_TOTAL = 500_000   # seed value in _PLAN_ENVELOPE
        NEGOTIATE_TOTAL = 425_000  # cheaper plan negotiate returns

        def negotiate_fn(req):
            return {
                "_trip_request": req,
                "outcome": "plan_ready",
                "idempotency_key": _NEW_IDK,
                "package_total_with_fees_cents": NEGOTIATE_TOTAL,
                "legs": req.get("legs") or [],
                "day_plans": [],
                "booking_ref": None,
                "payment_status": "held",
                "wallet": {"debited": False},
            }

        with patch("utils.followup_parser.parse_followup", return_value=_DELTA_CHEAPER):
            store = SqliteDashboardStore(":memory:")
            set_store(store)
            _seed_store(store)
            with TestClient(server.build_app()) as client:
                server._state.orch = _MockOrch(negotiate_fn)
                r = client.post("/refine", json={
                    "idempotency_key": _PLAN_IDK,
                    "message": "make it cheaper",
                    "owner_token": _ANON_OWNER_TOKEN,
                })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body.get("outcome"), "plan_ready", body)
        plan = body.get("plan") or {}
        new_total = plan.get("package_total_with_fees_cents")
        self.assertIsNotNone(new_total, "plan must contain package_total_with_fees_cents")
        self.assertLess(new_total, ORIGINAL_TOTAL,
                        f"cheaper refine must reduce total: {new_total} >= {ORIGINAL_TOTAL}")
        self.assertEqual(new_total, NEGOTIATE_TOTAL,
                         f"plan total must match what negotiate returned: {new_total} != {NEGOTIATE_TOTAL}")
        # Held, not booked.
        self.assertIsNone(plan.get("booking_ref"),
                          "refine must NEVER produce a booking_ref (plan-only)")
        self.assertNotEqual(plan.get("payment_status"), "paid",
                            "refine must NOT set payment_status=paid")

    def test_sql_injection_payloads_are_safe(self):
        """Injection via message / idempotency_key / session_id must not drop the table."""
        evil = "'; DROP TABLE conversations;--"
        with patch("utils.followup_parser.parse_followup", return_value=_DELTA_SWAP):
            store = SqliteDashboardStore(":memory:")
            set_store(store)
            _seed_store(store)
            with TestClient(server.build_app()) as client:
                client.post("/refine", json={"idempotency_key": _PLAN_IDK, "message": evil,
                                             "owner_token": _ANON_OWNER_TOKEN})
                client.post("/refine", json={"idempotency_key": _PLAN_IDK, "message": "hi",
                                             "session_id": evil, "owner_token": _ANON_OWNER_TOKEN})
        # Table intact + still usable.
        store.create_conversation("sess-after", "u", _PLAN_IDK, seed_turns=[])
        self.assertIsNotNone(store.get_conversation("sess-after"))


class TestTripsIsolation(unittest.TestCase):
    """b2 — /trips per-user isolation: GET /trips?user_id=A excludes user B's rows.

    MIGRATION NOTE (read-IDOR fix, Group D): GET /trips now REQUIRES a
    session_token proving possession of a live session for user_id (see
    server.py trips_list's SECURITY docstring). The original fixtures below
    used "alice"/"bob"/"nobody" — non-seeded user_ids that can never mint a
    session_token (POST /session/login 404s for an unknown user_id) — so the
    two isolation-test users are re-pointed at REAL pre-seeded demo users
    (demo-mei / demo-alex), authenticated via `_login`, same pattern as
    test_cancel_trips_endpoints.py.
    """

    def _make_client_and_store(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        client = TestClient(server.build_app())
        return client, store

    def test_trips_list_excludes_other_users(self):
        """Seed demo-mei + demo-alex each with a distinct plan; demo-mei's
        GET /trips must not include demo-alex's idempotency_key, and vice versa."""
        client, store = self._make_client_and_store()

        # Seed demo-mei's plan.
        alice_idk = "trip-alice-0001"
        store.save_plan({
            "idempotency_key": alice_idk,
            "user_id": "demo-mei",
            "package_total_cents": 300_000,
            "request": {"user_id": "demo-mei", "legs": []},
            "envelope": {"outcome": "plan_ready", "idempotency_key": alice_idk},
        })
        # Seed demo-alex's plan.
        bob_idk = "trip-bob-0001"
        store.save_plan({
            "idempotency_key": bob_idk,
            "user_id": "demo-alex",
            "package_total_cents": 200_000,
            "request": {"user_id": "demo-alex", "legs": []},
            "envelope": {"outcome": "plan_ready", "idempotency_key": bob_idk},
        })

        with client:
            alice_token = _login(client, "demo-mei")
            bob_token = _login(client, "demo-alex")
            ra = client.get("/trips?user_id=demo-mei", headers={"X-Session-Token": alice_token})
            rb = client.get("/trips?user_id=demo-alex", headers={"X-Session-Token": bob_token})

        self.assertEqual(ra.status_code, 200, ra.text)
        self.assertEqual(rb.status_code, 200, rb.text)

        alice_keys = {t["idempotency_key"] for t in ra.json().get("trips", [])}
        bob_keys   = {t["idempotency_key"] for t in rb.json().get("trips", [])}

        # alice sees her own plan
        self.assertIn(alice_idk, alice_keys,
                      "alice's own trip must be in her trips list")
        # alice does NOT see bob's plan
        self.assertNotIn(bob_idk, alice_keys,
                         "alice's /trips must NOT include bob's trip (isolation failure)")
        # bob sees his own plan
        self.assertIn(bob_idk, bob_keys,
                      "bob's own trip must be in his trips list")
        # bob does NOT see alice's plan
        self.assertNotIn(alice_idk, bob_keys,
                         "bob's /trips must NOT include alice's trip (isolation failure)")

    def test_trips_list_missing_user_id_400(self):
        """GET /trips without user_id must return 400 invalid_request."""
        client, _ = self._make_client_and_store()
        with client:
            r = client.get("/trips")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["outcome"], "invalid_request")

    def test_trips_list_empty_for_unknown_user(self):
        """GET /trips?user_id=<real demo user with no trips> returns an empty
        list (not a 404/error). Re-pointed at demo-jack (see class MIGRATION
        NOTE) — a non-seeded user_id like the original "nobody" can no longer
        reach this branch at all (session_token can't be minted for it), so
        this now genuinely tests the empty-list behaviour rather than the
        auth gate (TestTripsListOwnership in test_cancel_trips_endpoints.py
        covers the unauthenticated/unknown-user rejection explicitly)."""
        client, _ = self._make_client_and_store()
        with client:
            token = _login(client, "demo-jack")
            r = client.get("/trips?user_id=demo-jack", headers={"X-Session-Token": token})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json().get("trips"), [],
                         "user with no trips should have an empty trips list")


class TestConfirmIdempotent(unittest.TestCase):
    """b6 — /confirm idempotent double-confirm: second call must not re-book/charge.

    MIGRATION NOTE (IDOR fix): was seeded under `user_id="ci-user"` (a non-seeded
    user_id — no session_token could ever be minted for it, so every /confirm
    below would now 403 unconditionally). Fixed by seeding ANONYMOUSLY
    (`user_id=""`) + an `owner_token` bound at creation, and threading that same
    owner_token into both /confirm calls (see _ANON_OWNER_TOKEN above)."""

    def _seed_plan_ready(self, store: SqliteDashboardStore, idk: str,
                         user_id: str = "") -> None:
        store.save_plan({
            "idempotency_key": idk,
            "user_id": user_id,
            "owner_token": _ANON_OWNER_TOKEN,
            "package_total_cents": 425_000,
            "request": {"user_id": user_id, "legs": [
                {"city": "tokyo", "checkin": "2026-10-15", "checkout": "2026-10-19"},
            ]},
            "envelope": {
                "outcome": "plan_ready",
                "idempotency_key": idk,
                "package_total_with_fees_cents": 425_000,
                "legs": [],
                "day_plans": [],
            },
        })

    def test_double_confirm_does_not_double_book(self):
        """b6 — a second POST /confirm with the same idempotency_key must:
          1. Return HTTP 200 both times.
          2. Return the same booking_ref on the second call as the first.
          3. NOT invoke commit_plan a second time (the route-layer short-circuit fires).
        """
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        idk = "trip-confirm-idempotent-001"
        self._seed_plan_ready(store, idk)

        capturing_orch = _CommitPlanCapturingOrch()

        with TestClient(server.build_app()) as client:
            server._state.orch = capturing_orch

            # First /confirm — should succeed and call commit_plan once.
            r1 = client.post("/confirm", json={"idempotency_key": idk,
                                               "owner_token": _ANON_OWNER_TOKEN})
            self.assertEqual(r1.status_code, 200, f"first /confirm failed: {r1.text}")
            body1 = r1.json()

            # Second /confirm — idempotent: same key, already booked.
            r2 = client.post("/confirm", json={"idempotency_key": idk,
                                               "owner_token": _ANON_OWNER_TOKEN})
            self.assertEqual(r2.status_code, 200, f"second /confirm failed: {r2.text}")
            body2 = r2.json()

        # commit_plan must be called exactly once across both /confirm calls.
        self.assertEqual(
            capturing_orch.commit_plan_calls, 1,
            f"commit_plan was called {capturing_orch.commit_plan_calls} times — "
            f"expected exactly 1 (idempotency broken: double-book/charge risk)",
        )

        # Both calls must reference the SAME booking (no new ref created on 2nd call).
        ref1 = body1.get("booking_ref")
        ref2 = body2.get("booking_ref")
        self.assertIsNotNone(ref1, f"first /confirm must return a booking_ref: {body1}")
        self.assertEqual(ref1, ref2,
                         f"second /confirm returned a DIFFERENT booking_ref — "
                         f"double-book risk: {ref1!r} vs {ref2!r}")


class TestRefineOwnership(unittest.TestCase):
    """SECURITY regression: /refine ownership gate (was VULN-AUTH-003, CVSS 8.1
    HIGH — IDOR). Mirrors test_prefs_session.py's `_login` + wrong-user
    pattern, but the gate returns 403 `not_trip_owner` (server.py's
    `_forbidden` — NOT 401; see server.py's `_authorize_trip_action` docstring
    for the full two-tier session_token/owner_token model).

    Cases 1-6 use the swap-only delta (_DELTA_SWAP) so no re-plan/orchestrator
    is needed — the ownership gate fires before any op processing regardless."""

    _OWNER_IDK = "trip-refine-own-001"      # logged-in owner (demo-mei)
    _ANON_IDK = "trip-refine-anon-001"      # anonymous, owner_token-bound
    _ANON_TOKEN = "anon-secret-refine-A"

    def _seed(self, store: SqliteDashboardStore) -> None:
        store.save_plan({
            "idempotency_key": self._OWNER_IDK, "user_id": "demo-mei",
            "package_total_cents": 500000,
            "request": dict(_STRUCTURED_REQUEST, user_id="demo-mei"),
            "envelope": {**_PLAN_ENVELOPE, "idempotency_key": self._OWNER_IDK},
        })
        store.save_plan({
            "idempotency_key": self._ANON_IDK, "user_id": "",
            "owner_token": self._ANON_TOKEN,
            "package_total_cents": 500000,
            "request": dict(_STRUCTURED_REQUEST, user_id=""),
            "envelope": {**_PLAN_ENVELOPE, "idempotency_key": self._ANON_IDK},
        })

    def _client(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        self._seed(store)
        return TestClient(server.build_app()), store

    # ---- 1. Logged-in owner + valid session_token -> 200, proceeds ----
    def test_owner_with_valid_session_token_succeeds(self):
        with patch("utils.followup_parser.parse_followup", return_value=_DELTA_SWAP):
            client, store = self._client()
            with client:
                token = _login(client, "demo-mei")
                r = client.post("/refine", json={"idempotency_key": self._OWNER_IDK,
                                                  "message": "swap it",
                                                  "session_token": token})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["outcome"], "refine_partial")

    # ---- 2. Logged-in trip, no session_token -> 403; no state change ----
    def test_owner_trip_no_session_token_403_no_state_change(self):
        with patch("utils.followup_parser.parse_followup", return_value=_DELTA_SWAP):
            client, store = self._client()
            before = copy.deepcopy(store.get_plan(self._OWNER_IDK)["envelope"])
            with client:
                r = client.post("/refine", json={"idempotency_key": self._OWNER_IDK,
                                                  "message": "swap it"})
        self.assertEqual(r.status_code, 403)
        # #203: Tier 1 (logged-in trip) denials return `session_invalid`.
        self.assertEqual(r.json().get("reason"), "session_invalid")
        self.assertEqual(store.get_plan(self._OWNER_IDK)["envelope"], before)

    # ---- 3. session_token minted for a DIFFERENT demo user -> 403 ----
    def test_owner_trip_wrong_user_session_token_403(self):
        with patch("utils.followup_parser.parse_followup", return_value=_DELTA_SWAP):
            client, store = self._client()
            with client:
                other_token = _login(client, "demo-alex")   # real user, NOT the trip owner
                r = client.post("/refine", json={"idempotency_key": self._OWNER_IDK,
                                                  "message": "swap it",
                                                  "session_token": other_token})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json().get("reason"), "session_invalid")  # #203: Tier 1

    # ---- 4. Anon trip + correct owner_token -> proceeds ----
    def test_anon_trip_correct_owner_token_succeeds(self):
        with patch("utils.followup_parser.parse_followup", return_value=_DELTA_SWAP):
            client, store = self._client()
            with client:
                r = client.post("/refine", json={"idempotency_key": self._ANON_IDK,
                                                  "message": "swap it",
                                                  "owner_token": self._ANON_TOKEN})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["outcome"], "refine_partial")

    # ---- 5. Anon trip, wrong/missing owner_token -> 403; no state change ----
    def test_anon_trip_wrong_or_missing_owner_token_403_no_state_change(self):
        with patch("utils.followup_parser.parse_followup", return_value=_DELTA_SWAP):
            client, store = self._client()
            before = copy.deepcopy(store.get_plan(self._ANON_IDK)["envelope"])
            with client:
                r_wrong = client.post("/refine", json={"idempotency_key": self._ANON_IDK,
                                                        "message": "swap it",
                                                        "owner_token": "not-the-right-secret"})
                r_missing = client.post("/refine", json={"idempotency_key": self._ANON_IDK,
                                                          "message": "swap it"})
        self.assertEqual(r_wrong.status_code, 403)
        self.assertEqual(r_wrong.json().get("reason"), "not_trip_owner")
        self.assertEqual(r_missing.status_code, 403)
        self.assertEqual(r_missing.json().get("reason"), "not_trip_owner")
        self.assertEqual(store.get_plan(self._ANON_IDK)["envelope"], before)

    # ---- 6. Cross-class: anon-only caller vs logged-in trip, and
    #         logged-in-only caller vs anon trip (no owner_token) -> both 403 ----
    def test_cross_class_callers_403(self):
        with patch("utils.followup_parser.parse_followup", return_value=_DELTA_SWAP):
            client, store = self._client()
            with client:
                r1 = client.post("/refine", json={"idempotency_key": self._OWNER_IDK,
                                                   "message": "swap it",
                                                   "owner_token": "some-random-anon-secret"})
                token = _login(client, "demo-mei")
                r2 = client.post("/refine", json={"idempotency_key": self._ANON_IDK,
                                                   "message": "swap it",
                                                   "session_token": token})
        self.assertEqual(r1.status_code, 403)
        self.assertEqual(r1.json().get("reason"), "session_invalid")  # #203: r1 targets the Tier-1 (logged-in) trip
        self.assertEqual(r2.status_code, 403)
        self.assertEqual(r2.json().get("reason"), "not_trip_owner")  # r2 targets the Tier-2 (anon) trip

    # ---- 11 (targeted regression). Refine continuity: the child row of an
    #         anon re-plan inherits the PARENT's owner_token (server-side
    #         propagation), so the original owner can keep refining and a
    #         caller lacking that owner_token cannot ----
    def test_anon_refine_continuity_child_inherits_owner_token(self):
        with patch("utils.followup_parser.parse_followup", return_value=_DELTA_CHEAPER):
            client, store = self._client()
            child_key = "trip-refine-anon-child-001"
            mock_orch = _MockOrch(lambda req: {
                "_trip_request": req, "outcome": "plan_ready",
                "idempotency_key": child_key,
                "package_total_with_fees_cents": 425000,
                "legs": req.get("legs") or [], "day_plans": [],
            })
            with client:
                server._state.orch = mock_orch
                r1 = client.post("/refine", json={"idempotency_key": self._ANON_IDK,
                                                   "message": "make it cheaper",
                                                   "owner_token": self._ANON_TOKEN})
                self.assertEqual(r1.json().get("outcome"), "plan_ready", r1.json())
                self.assertEqual(r1.json().get("idempotency_key"), child_key)
                child_row = store.get_plan(child_key)
                self.assertEqual(child_row.get("owner_token"), self._ANON_TOKEN,
                                 "child row must inherit the PARENT's owner_token")

                # The original owner_token can refine the CHILD again.
                r2 = client.post("/refine", json={"idempotency_key": child_key,
                                                   "message": "even cheaper",
                                                   "owner_token": self._ANON_TOKEN})
                self.assertEqual(r2.status_code, 200, r2.text)
                self.assertNotEqual(r2.json().get("outcome"), "forbidden")

                # A caller WITHOUT the owner_token cannot act on the child.
                r3 = client.post("/refine", json={"idempotency_key": child_key,
                                                   "message": "even cheaper"})
        self.assertEqual(r3.status_code, 403)
        self.assertEqual(r3.json().get("reason"), "not_trip_owner")


if __name__ == "__main__":
    unittest.main(verbosity=2)
