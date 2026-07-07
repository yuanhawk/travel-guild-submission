"""test_confirm_endpoint.py — POST /confirm ownership-gate regression tests.

SECURITY (IDOR fix, was STORE-002 CVSS 9.1 CRITICAL): /confirm now REQUIRES the
two-tier session_token/owner_token ownership proof (see server.py's
_authorize_trip_action). This is the dedicated home for /confirm's OWNERSHIP
coverage — general /confirm mechanics (missing idempotency_key, the double-
confirm idempotency guarantee) already live in test_refine_endpoint.py's
TestConfirmIdempotent class (b6); this file does not duplicate that, it adds
the ownership-gate regression lock mirrored across /confirm /cancel /refine
/replan.

Mirrors test_prefs_session.py's `_login` + wrong-user pattern, but the gate
returns 403 `not_trip_owner` (server.py's `_forbidden` — NOT 401, so the FE
can render an honest "not your trip" refusal instead of throwing).

Tests:
1. test_owner_with_valid_session_token_succeeds
2. test_owner_trip_no_session_token_403_no_state_change
3. test_owner_trip_wrong_user_session_token_403
4. test_anon_trip_correct_owner_token_succeeds
5. test_anon_trip_wrong_or_missing_owner_token_403_no_state_change
6. test_cross_class_callers_403
7. test_booked_trip_idempotent_replay_by_non_owner_is_403_not_leaked (targeted
   regression — proves the gate precedes the booked-replay short-circuit)
"""
from __future__ import annotations

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient
from orchestration import server
from orchestration.store import SqliteDashboardStore, set_store

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ANON_TOKEN = "anon-secret-confirm-A"

_ENVELOPE_TEMPLATE = {
    "outcome": "plan_ready",
    "payment_status": "held",
    "booking_ref": None,
    "package_total_with_fees_cents": 150000,
    "wallet": {"debited": False, "held_cents": 150000},
    "legs": [{"city": "tokyo"}],
    "day_plans": [],
}


class _MockCommitOrch:
    """Minimal orchestrator stub: commit_plan always succeeds, tracks calls
    (so a test can assert commit_plan was/was-not invoked — the "no state
    change on a 403" proof)."""

    def __init__(self):
        self.commit_plan_calls: list[str] = []
        # #161 — records merchant_user_id per call (idempotency_key -> value) so
        # tests can assert the canonical merchant identity that server.py's
        # /confirm actually threaded into commit_plan.
        self.merchant_user_ids: dict[str, str] = {}

    def commit_plan(self, *, user_id, checkout_id, idempotency_key,
                    plan_envelope, dest_token, merchant_user_id=""):
        self.commit_plan_calls.append(idempotency_key)
        self.merchant_user_ids[idempotency_key] = merchant_user_id
        return {
            "outcome": "success",
            "booking_ref": f"BK-{idempotency_key}",
            "idempotency_key": idempotency_key,
            "payment_status": "paid",
        }


def _login(c, user_id: str) -> str:
    """POST /session/login for a KNOWN demo user_id -> its session_token.
    Mirrors test_prefs_session.py's `_login` helper."""
    r = c.post("/session/login", json={"user_id": user_id})
    assert r.status_code == 200, r.text
    return r.json()["session_token"]


class TestConfirmOwnership(unittest.TestCase):
    """SECURITY regression: /confirm ownership gate (was STORE-002, CVSS 9.1
    CRITICAL — IDOR)."""

    _OWNER_IDK = "trip-confirm-own-001"     # logged-in owner (demo-mei)
    _ANON_IDK = "trip-confirm-anon-001"     # anonymous, owner_token-bound

    def _seed_plan_ready(self, store: SqliteDashboardStore, idk: str,
                         *, user_id: str = "", owner_token: str = "") -> None:
        store.save_plan({
            "idempotency_key": idk, "user_id": user_id, "owner_token": owner_token,
            "checkout_id": f"co-{idk}", "dest_token": "JP",
            "package_total_cents": 150000,
            "request": {"user_id": user_id, "legs": [
                {"city": "tokyo", "checkin": "2026-10-15", "checkout": "2026-10-19"},
            ]},
            "envelope": {**copy.deepcopy(_ENVELOPE_TEMPLATE), "idempotency_key": idk},
        })

    def _seed(self, store: SqliteDashboardStore) -> None:
        self._seed_plan_ready(store, self._OWNER_IDK, user_id="demo-mei")
        self._seed_plan_ready(store, self._ANON_IDK, user_id="", owner_token=_ANON_TOKEN)

    def _client(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        self._seed(store)
        return TestClient(server.build_app()), store

    # ---- 1. Logged-in owner + valid session_token -> 200, proceeds ----
    def test_owner_with_valid_session_token_succeeds(self):
        client, store = self._client()
        mock_orch = _MockCommitOrch()
        with client:
            server._state.orch = mock_orch
            token = _login(client, "demo-mei")
            r = client.post("/confirm", json={"idempotency_key": self._OWNER_IDK,
                                              "session_token": token})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json().get("outcome"), "success")
        self.assertEqual(mock_orch.commit_plan_calls, [self._OWNER_IDK])
        self.assertEqual(store.get_plan(self._OWNER_IDK)["status"], "booked")

    # ---- 2. Logged-in trip, no session_token -> 403; no state change ----
    def test_owner_trip_no_session_token_403_no_state_change(self):
        client, store = self._client()
        mock_orch = _MockCommitOrch()
        with client:
            server._state.orch = mock_orch
            r = client.post("/confirm", json={"idempotency_key": self._OWNER_IDK})
        self.assertEqual(r.status_code, 403)
        # #203: Tier 1 (logged-in trip) denials return `session_invalid`, NOT
        # `not_trip_owner` — see server.py's _authorize_trip_action docstring.
        self.assertEqual(r.json().get("reason"), "session_invalid")
        self.assertEqual(mock_orch.commit_plan_calls, [])
        self.assertEqual(store.get_plan(self._OWNER_IDK)["status"], "plan_ready")

    # ---- 3. session_token minted for a DIFFERENT demo user -> 403 ----
    def test_owner_trip_wrong_user_session_token_403(self):
        client, store = self._client()
        mock_orch = _MockCommitOrch()
        with client:
            server._state.orch = mock_orch
            other_token = _login(client, "demo-alex")   # real user, NOT the trip owner
            r = client.post("/confirm", json={"idempotency_key": self._OWNER_IDK,
                                              "session_token": other_token})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json().get("reason"), "session_invalid")  # #203: Tier 1
        self.assertEqual(mock_orch.commit_plan_calls, [])
        self.assertEqual(store.get_plan(self._OWNER_IDK)["status"], "plan_ready")

    # ---- 4. Anon trip + correct owner_token -> proceeds ----
    def test_anon_trip_correct_owner_token_succeeds(self):
        client, store = self._client()
        mock_orch = _MockCommitOrch()
        with client:
            server._state.orch = mock_orch
            r = client.post("/confirm", json={"idempotency_key": self._ANON_IDK,
                                              "owner_token": _ANON_TOKEN})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json().get("outcome"), "success")
        self.assertEqual(mock_orch.commit_plan_calls, [self._ANON_IDK])
        self.assertEqual(store.get_plan(self._ANON_IDK)["status"], "booked")

    # ---- 5. Anon trip, wrong/missing owner_token -> 403; no state change ----
    def test_anon_trip_wrong_or_missing_owner_token_403_no_state_change(self):
        client, store = self._client()
        mock_orch = _MockCommitOrch()
        with client:
            server._state.orch = mock_orch
            r_wrong = client.post("/confirm", json={"idempotency_key": self._ANON_IDK,
                                                     "owner_token": "not-the-right-secret"})
            r_missing = client.post("/confirm", json={"idempotency_key": self._ANON_IDK})
        self.assertEqual(r_wrong.status_code, 403)
        self.assertEqual(r_wrong.json().get("reason"), "not_trip_owner")
        self.assertEqual(r_missing.status_code, 403)
        self.assertEqual(r_missing.json().get("reason"), "not_trip_owner")
        self.assertEqual(mock_orch.commit_plan_calls, [])
        self.assertEqual(store.get_plan(self._ANON_IDK)["status"], "plan_ready")

    # ---- 6. Cross-class: anon-only caller vs logged-in trip, and
    #         logged-in-only caller vs anon trip (no owner_token) -> both 403 ----
    def test_cross_class_callers_403(self):
        client, store = self._client()
        mock_orch = _MockCommitOrch()
        with client:
            server._state.orch = mock_orch
            r1 = client.post("/confirm", json={"idempotency_key": self._OWNER_IDK,
                                               "owner_token": "some-random-anon-secret"})
            token = _login(client, "demo-mei")
            r2 = client.post("/confirm", json={"idempotency_key": self._ANON_IDK,
                                               "session_token": token})
        self.assertEqual(r1.status_code, 403)
        self.assertEqual(r1.json().get("reason"), "session_invalid")  # #203: r1 targets the Tier-1 (logged-in) trip
        self.assertEqual(r2.status_code, 403)
        self.assertEqual(r2.json().get("reason"), "not_trip_owner")  # r2 targets the Tier-2 (anon) trip
        self.assertEqual(mock_orch.commit_plan_calls, [])
        self.assertEqual(store.get_plan(self._OWNER_IDK)["status"], "plan_ready")
        self.assertEqual(store.get_plan(self._ANON_IDK)["status"], "plan_ready")

    # ---- 7 (targeted regression). /confirm idempotent replay of a BOOKED
    #        trip by a non-owner -> 403, does NOT leak the booked envelope —
    #        proves the ownership gate precedes the booked-replay branch ----
    def test_booked_trip_idempotent_replay_by_non_owner_is_403_not_leaked(self):
        client, store = self._client()
        mock_orch = _MockCommitOrch()
        with client:
            server._state.orch = mock_orch
            token = _login(client, "demo-mei")
            # The real owner confirms/books for real.
            r1 = client.post("/confirm", json={"idempotency_key": self._OWNER_IDK,
                                               "session_token": token})
            self.assertEqual(r1.json().get("outcome"), "success", r1.json())
            booking_ref = r1.json().get("booking_ref")
            self.assertIsNotNone(booking_ref)

            # A non-owner replays /confirm on the now-BOOKED trip. If the
            # ownership gate did NOT precede the `status == "booked"`
            # idempotent-replay short-circuit, this would leak the stranger's
            # full booked envelope (booking_ref, payment_status, wallet debit
            # amount) to a caller with no proof of ownership.
            other_token = _login(client, "demo-alex")
            r2 = client.post("/confirm", json={"idempotency_key": self._OWNER_IDK,
                                               "session_token": other_token})
        self.assertEqual(r2.status_code, 403)
        self.assertEqual(r2.json().get("reason"), "session_invalid")  # #203: Tier 1
        body2 = r2.json()
        self.assertNotIn("booking_ref", body2)
        self.assertNotIn("payment_status", body2)
        self.assertNotIn("wallet", body2)
        self.assertNotEqual(body2.get("outcome"), "success")
        # commit_plan was called exactly once (the real owner's confirm only).
        self.assertEqual(mock_orch.commit_plan_calls, [self._OWNER_IDK])


class TestConfirmSessionInvalidReasonSplit(unittest.TestCase):
    """#203 (Class E) — regression lock for the session_invalid/not_trip_owner
    reason-code split (server.py's `_forbidden`/`_authorize_trip_action`).

    Live-test finding (2026-07-03): after a backend redeploy wiped the
    in-memory `_SESSIONS` dict (session_token.py's documented, deliberate
    in-memory-only design), a user with a perfectly legitimate, previously-
    logged-in trip saw the same generic "this trip belongs to another
    session" refusal as a genuine cross-user IDOR attempt. This class proves
    the restart-wipe case now gets its own reason code, AND that the new
    code introduces no new existence-oracle signal.
    """

    _OWNER_IDK = "trip-confirm-session-split-001"

    def _seed(self, store: SqliteDashboardStore) -> None:
        store.save_plan({
            "idempotency_key": self._OWNER_IDK, "user_id": "demo-mei",
            "checkout_id": f"co-{self._OWNER_IDK}", "dest_token": "JP",
            "package_total_cents": 150000,
            "request": {"user_id": "demo-mei", "legs": [
                {"city": "tokyo", "checkin": "2026-10-15", "checkout": "2026-10-19"},
            ]},
            "envelope": {**copy.deepcopy(_ENVELOPE_TEMPLATE), "idempotency_key": self._OWNER_IDK},
        })

    def _client(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        self._seed(store)
        return TestClient(server.build_app()), store

    # ---- 1. Simulated backend restart: a token minted BEFORE the restart is
    #        no longer in the (in-memory, wiped-on-restart) _SESSIONS dict —
    #        the exact live-test scenario. Must surface session_invalid, NOT
    #        the generic/attacker-implying not_trip_owner. ----
    def test_session_wiped_by_restart_returns_session_invalid(self):
        from utils import session_token
        client, store = self._client()
        mock_orch = _MockCommitOrch()
        # bug fix (test isolation): _SESSIONS is a module-level GLOBAL, shared by
        # every test file in the same process (xdist loadscope groups whole test
        # files per worker). A bare .clear() below wiped tokens OTHER test files
        # had already issued at their own module-import time (e.g.
        # test_reconsider_leg_endpoint.py's _OWNER_TOKEN), silently invalidating
        # them and turning an otherwise-valid owner request into a false 404 —
        # only reproducible in a full-suite run, never in isolation. Save/restore
        # around the deliberate wipe so the restart simulation stays local to
        # this one test.
        _saved_sessions = dict(session_token._SESSIONS)
        try:
            with client:
                server._state.orch = mock_orch
                token = _login(client, "demo-mei")
                # Simulate a process restart: the in-memory session store is gone,
                # exactly as session_token.py documents ("do NOT survive a process
                # restart"). The client still holds (and sends) its old token.
                session_token._SESSIONS.clear()
                r = client.post("/confirm", json={"idempotency_key": self._OWNER_IDK,
                                                  "session_token": token})
            self.assertEqual(r.status_code, 403)
            self.assertEqual(r.json().get("reason"), "session_invalid")
            self.assertEqual(mock_orch.commit_plan_calls, [])
            self.assertEqual(store.get_plan(self._OWNER_IDK)["status"], "plan_ready")
        finally:
            session_token._SESSIONS.clear()
            session_token._SESSIONS.update(_saved_sessions)

    # ---- 2. No-existence-oracle guard: verify_session collapses FOUR distinct
    #        causes (no token / unknown token / expired token / token bound to
    #        a different user) into a single bool. Prove _forbidden's output
    #        is byte-identical across all of them for the SAME real trip — a
    #        caller who holds any kind of rejected token cannot use the
    #        response to tell "close" from "nowhere near valid", which would
    #        otherwise leak information about the session-token namespace. ----
    def test_session_invalid_body_identical_across_all_verify_session_failure_causes(self):
        from utils import session_token
        client, store = self._client()
        mock_orch = _MockCommitOrch()
        with client:
            server._state.orch = mock_orch
            # (a) no token given at all.
            r_missing = client.post("/confirm", json={"idempotency_key": self._OWNER_IDK})
            # (b) a well-formed but entirely unknown/fabricated token.
            r_unknown = client.post("/confirm", json={"idempotency_key": self._OWNER_IDK,
                                                       "session_token": "fabricated-not-a-real-token"})
            # (c) a real, live token — but minted for a DIFFERENT demo user.
            other_token = _login(client, "demo-alex")
            r_wrong_user = client.post("/confirm", json={"idempotency_key": self._OWNER_IDK,
                                                          "session_token": other_token})
            # (d) a real token for the RIGHT user, but expired (simulated by
            #     back-dating its expiry in the in-memory store directly).
            mine_token = _login(client, "demo-mei")
            user_id, _old_expiry = session_token._SESSIONS[mine_token]
            session_token._SESSIONS[mine_token] = (user_id, 0.0)  # already expired
            r_expired = client.post("/confirm", json={"idempotency_key": self._OWNER_IDK,
                                                       "session_token": mine_token})
        bodies = [r_missing.json(), r_unknown.json(), r_wrong_user.json(), r_expired.json()]
        for r in (r_missing, r_unknown, r_wrong_user, r_expired):
            self.assertEqual(r.status_code, 403)
        for body in bodies:
            self.assertEqual(body, bodies[0],
                             "session_invalid response must be byte-identical across every "
                             "verify_session failure cause — any difference would leak which "
                             "kind of rejection occurred (existence/validity oracle on the "
                             "session-token namespace)")
        self.assertEqual(bodies[0], {"outcome": "forbidden", "reason": "session_invalid",
                                     "idempotency_key": self._OWNER_IDK})
        self.assertEqual(mock_orch.commit_plan_calls, [])

    # ---- 3. Tier boundary is preserved: a genuine anon-trip owner_token
    #        mismatch on a DIFFERENT (Tier 2) trip still returns the ORIGINAL
    #        not_trip_owner code, not session_invalid — no regression on the
    #        cross-user anonymous-trip case this split must not touch. ----
    def test_anon_tier_still_returns_not_trip_owner_unchanged(self):
        anon_idk = "trip-confirm-session-split-anon-001"
        client, store = self._client()
        store.save_plan({
            "idempotency_key": anon_idk, "user_id": "", "owner_token": "real-anon-secret",
            "checkout_id": f"co-{anon_idk}", "dest_token": "JP", "package_total_cents": 150000,
            "request": {"user_id": "", "legs": [{"city": "tokyo"}]},
            "envelope": {**copy.deepcopy(_ENVELOPE_TEMPLATE), "idempotency_key": anon_idk},
        })
        mock_orch = _MockCommitOrch()
        with client:
            server._state.orch = mock_orch
            r = client.post("/confirm", json={"idempotency_key": anon_idk,
                                              "owner_token": "wrong-secret"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json().get("reason"), "not_trip_owner")


class TestConfirmMerchantUserIdThreading(unittest.TestCase):
    """#161 — /confirm must thread the canonical, session-verified merchant
    identity into commit_plan(merchant_user_id=...), so the Go merchant's
    END-USER OWNERSHIP check (checkoutSession.UserID) has something real to
    verify against. Covers: (1) a plan-time-persisted value rides through
    verbatim (plan==confirm consistency), and (2) a legacy row (saved before
    merchant_user_id existed) falls back to the recompute from the AUTHORIZED
    row fields — never from unauthenticated body input."""

    _PERSISTED_IDK = "trip-confirm-mid-persisted-001"
    _LEGACY_ANON_IDK = "trip-confirm-mid-legacy-001"
    _ANON_TOKEN = "anon-secret-mid-legacy-B"

    def _client(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        return TestClient(server.build_app()), store

    def test_confirm_uses_persisted_merchant_user_id_verbatim(self):
        """A plan-time-computed merchant_user_id (e.g. an anon pseudo-id) is
        stored on the row and must ride through to commit_plan UNCHANGED —
        this is the plan==confirm consistency guarantee (design spec A2)."""
        client, store = self._client()
        persisted_mid = "anon:cafefeed12345678deadbeef"
        store.save_plan({
            "idempotency_key": self._PERSISTED_IDK, "user_id": "", "owner_token": self._ANON_TOKEN,
            "checkout_id": f"co-{self._PERSISTED_IDK}", "dest_token": "JP",
            "package_total_cents": 150000,
            "request": {"user_id": "", "legs": [
                {"city": "tokyo", "checkin": "2026-10-15", "checkout": "2026-10-19"},
            ]},
            "envelope": {**copy.deepcopy(_ENVELOPE_TEMPLATE), "idempotency_key": self._PERSISTED_IDK},
            "merchant_user_id": persisted_mid,
        })
        self.assertEqual(store.get_plan(self._PERSISTED_IDK)["merchant_user_id"], persisted_mid)

        mock_orch = _MockCommitOrch()
        with client:
            server._state.orch = mock_orch
            r = client.post("/confirm", json={"idempotency_key": self._PERSISTED_IDK,
                                              "owner_token": self._ANON_TOKEN})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(mock_orch.commit_plan_calls, [self._PERSISTED_IDK])
        self.assertEqual(mock_orch.merchant_user_ids[self._PERSISTED_IDK], persisted_mid)

    def test_confirm_recomputes_merchant_user_id_for_legacy_row(self):
        """A legacy row saved before merchant_user_id existed (empty on the row)
        must fall back to merchant_checkout_owner() recomputed from the row's
        OWN authorized owner_token — never from raw request body input — and
        that recompute must match utils.ucp_signing.merchant_checkout_owner."""
        client, store = self._client()
        store.save_plan({
            "idempotency_key": self._LEGACY_ANON_IDK, "user_id": "", "owner_token": self._ANON_TOKEN,
            "checkout_id": f"co-{self._LEGACY_ANON_IDK}", "dest_token": "JP",
            "package_total_cents": 150000,
            "request": {"user_id": "", "legs": [
                {"city": "tokyo", "checkin": "2026-10-15", "checkout": "2026-10-19"},
            ]},
            "envelope": {**copy.deepcopy(_ENVELOPE_TEMPLATE), "idempotency_key": self._LEGACY_ANON_IDK},
            # merchant_user_id intentionally omitted -> "" on the row (legacy).
        })
        self.assertEqual(store.get_plan(self._LEGACY_ANON_IDK).get("merchant_user_id", ""), "")

        from utils.ucp_signing import merchant_checkout_owner
        expect = merchant_checkout_owner(user_id="", owner_token=self._ANON_TOKEN)
        self.assertTrue(expect.startswith("anon:"))

        mock_orch = _MockCommitOrch()
        with client:
            server._state.orch = mock_orch
            r = client.post("/confirm", json={"idempotency_key": self._LEGACY_ANON_IDK,
                                              "owner_token": self._ANON_TOKEN})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(mock_orch.merchant_user_ids[self._LEGACY_ANON_IDK], expect)

    def test_confirm_denied_for_wrong_owner_never_reaches_commit_plan(self):
        """Defense-in-depth ordering: _authorize_trip_action's 403 must precede
        ANY merchant_user_id computation or commit_plan dispatch — the merchant
        binding is a second layer, not the primary gate."""
        client, store = self._client()
        store.save_plan({
            "idempotency_key": self._LEGACY_ANON_IDK, "user_id": "", "owner_token": self._ANON_TOKEN,
            "checkout_id": f"co-{self._LEGACY_ANON_IDK}", "dest_token": "JP",
            "package_total_cents": 150000,
            "request": {"user_id": "", "legs": [
                {"city": "tokyo", "checkin": "2026-10-15", "checkout": "2026-10-19"},
            ]},
            "envelope": {**copy.deepcopy(_ENVELOPE_TEMPLATE), "idempotency_key": self._LEGACY_ANON_IDK},
        })
        mock_orch = _MockCommitOrch()
        with client:
            server._state.orch = mock_orch
            r = client.post("/confirm", json={"idempotency_key": self._LEGACY_ANON_IDK,
                                              "owner_token": "totally-wrong-secret"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json().get("reason"), "not_trip_owner")
        self.assertEqual(mock_orch.commit_plan_calls, [])
        self.assertEqual(mock_orch.merchant_user_ids, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
