"""test_c1_anon_ownership_fix.py — C1 CRITICAL regression: a server-minted
uuid4 user_id must never be persisted as a trip row's owner, or every
ANONYMOUS/guest trip becomes permanently unconfirmable/uncancellable/
unrefinable/unreadable-after-reload.

CONTEXT (bug, pre-fix): server.py's /negotiate and /negotiate_text stamp
body["user_id"] = str(uuid4()) whenever the raw request omitted it (browser
form omits it for a guest). That STAMPED value was later persisted verbatim
as the trips-store row's user_id (via orchestrator._to_plan_ready's
_confirm_ctx and server._persist_and_sanitize_plan). server._authorize_trip_
action treats ANY non-empty row.user_id as a Tier-1 "logged-in" trip and
demands verify_session(token, owner) — but a random per-request uuid4 can
NEVER have a session. Every subsequent /confirm, /cancel, /refine, /replan,
and GET /trips/{idk} on that guest's own trip — even with the CORRECT
Tier-2 owner_token — was rejected 403 session_invalid, forever.

FIX: capture the RAW (pre-uuid4-stamp) user_id from the body BEFORE the anon
fallback, mirroring the existing merchant_user_id pattern (server.py #161).
This rides along as trip_request["real_user_id"] (never part of
_request_digest — var-0 untouched), threaded through
utils.intent_parser.negotiate_from_text -> orchestrator.negotiate() ->
orchestrator._to_plan_ready()'s _confirm_ctx["user_id"] -> server.py's
persisted row["user_id"]. "" for a genuinely anonymous trip, correctly
routing _authorize_trip_action to its Tier-2 owner_token branch instead.

Tests:
  1. test_orchestrator_confirm_ctx_uses_real_user_id_not_server_uuid4 — REAL
     TravelOrchestrator (mocked merchant transport only) proves
     _to_plan_ready's _confirm_ctx['user_id'] is the raw identity, never the
     server-stamped uuid4, even when trip_request['user_id'] carries one.
  2. test_direct_caller_without_real_user_id_falls_back_unchanged — a
     trip_request with NO 'real_user_id' key (a pre-existing direct/test
     caller of orchestrator.negotiate()) is byte-identical to before this fix.
  3. TestAnonNegotiateConfirmRoundTripHTTP — the EXACT reported repro over the
     real HTTP surface (TestClient): POST /negotiate {plan:true, owner_token,
     no user_id} -> plan_ready; the stored row's user_id must be "" (not a
     uuid4); POST /confirm with the correct owner_token must succeed, not
     403 session_invalid.
"""
from __future__ import annotations

import copy
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

from orchestration import server
from orchestration.store import SqliteDashboardStore, set_store
from utils.ucp_signing import merchant_checkout_owner

from tests.test_trace_var0 import _PM_TRIP
from tests.test_merchant_user_id_e2e import (
    _build_society_with_budget_transport,
    _CapturingTransport,
    _merchant_result,
)


def _plan_phase_responses(co_id: str, uid: str) -> dict:
    """Merchant responses sufficient to reach commit=False's plan_ready outcome
    (create_checkout/update_checkout — the CHECK phase). Mirrors
    test_merchant_user_id_e2e.py's fixtures."""
    return {
        "create_checkout": (200, _merchant_result({
            "id": co_id, "status": "incomplete", "user_id": uid,
            "line_items": [], "total_cents": 54000, "currency": "USD",
            "buyer_consent": False, "booking_ref": "",
        })),
        "update_checkout": (200, _merchant_result({
            "id": co_id, "status": "incomplete", "total_cents": 54000,
            "buyer_consent": True, "currency": "USD",
        })),
        "complete_checkout": (200, _merchant_result({
            "id": co_id, "status": "complete", "user_id": uid,
            "line_items": [], "total_cents": 54000, "currency": "USD",
            "buyer_consent": True, "booking_ref": "BK-C1-anon",
            "wallet_session_id": "trip-c1", "wallet_debit_cents": 54000,
            "wallet_balance_cents": 446000, "simulated": True,
        })),
        "wallet_fund": (200, _merchant_result({
            "status": "ok", "wallet_session_id": "trip-c1",
            "seed_cents": 500000, "balance_cents": 500000, "simulated": True, "note": "sim",
        })),
    }


# ---------------------------------------------------------------------------
# 1/2 — orchestrator-level: real TravelOrchestrator, mocked merchant transport.
# ---------------------------------------------------------------------------

class TestOrchestratorRealUserIdThreading(unittest.TestCase):

    def test_orchestrator_confirm_ctx_uses_real_user_id_not_server_uuid4(self):
        server_minted_uuid = "anon-uuid-c1-repro-0001"
        owner_token = "owner-token-c1-repro"
        expected_mid = merchant_checkout_owner(user_id=None, owner_token=owner_token)

        transport = _CapturingTransport(_plan_phase_responses("co-c1-anon", expected_mid))
        orch = _build_society_with_budget_transport(transport)

        trip_request = copy.deepcopy(_PM_TRIP)
        # Reproduces server.py's exact post-stamp shape for a guest request that
        # arrived with NO user_id: trip_request['user_id'] carries the throwaway
        # server-minted uuid4, but trip_request['real_user_id'] (the C1 fix's
        # raw, pre-stamp capture) is "" — genuinely anonymous.
        trip_request["user_id"] = server_minted_uuid
        trip_request["real_user_id"] = ""
        trip_request["merchant_user_id"] = expected_mid

        res = orch.negotiate(trip_request, commit=False)
        self.assertEqual(res.get("outcome"), "plan_ready", res)
        ctx = res["_confirm_ctx"]
        self.assertEqual(
            ctx["user_id"], "",
            f"C1 regression: _confirm_ctx['user_id']={ctx['user_id']!r} — the "
            f"server-minted anon uuid4 leaked into the row-ownership field "
            f"instead of staying empty for a genuinely anonymous trip.",
        )
        self.assertNotEqual(ctx["user_id"], server_minted_uuid)

    def test_direct_caller_without_real_user_id_falls_back_unchanged(self):
        """A trip_request with no 'real_user_id' key at all (every pre-existing
        direct/test caller of orchestrator.negotiate()) must behave exactly as
        before this fix: _confirm_ctx['user_id'] == trip_request['user_id']."""
        real_uid = "user-existing-caller-42"
        transport = _CapturingTransport(
            _plan_phase_responses("co-c1-directcaller", real_uid))
        orch = _build_society_with_budget_transport(transport)

        trip_request = copy.deepcopy(_PM_TRIP)
        trip_request["user_id"] = real_uid
        trip_request["merchant_user_id"] = merchant_checkout_owner(
            user_id=real_uid, owner_token=None)
        self.assertNotIn("real_user_id", trip_request)

        res = orch.negotiate(trip_request, commit=False)
        self.assertEqual(res.get("outcome"), "plan_ready", res)
        self.assertEqual(res["_confirm_ctx"]["user_id"], real_uid)


# ---------------------------------------------------------------------------
# 3 — HTTP-level: the EXACT reported repro over TestClient.
# ---------------------------------------------------------------------------

_OWNER_TOKEN = "owner-token-c1-http-repro"


class _FakePlanReadyOrch:
    """Cheap orchestrator stub for the HTTP-layer round trip — no agent stack
    needed here (that is covered above); this isolates server.py's own
    stamp -> persist -> authorize plumbing. Mirrors the _confirm_ctx contract
    the REAL (fixed) orchestrator now produces: ctx['user_id'] is the raw
    real_user_id, never trip_request['user_id']."""

    def __init__(self):
        self.commit_plan_calls: list[str] = []

    def negotiate(self, trip_request: dict, *, commit: bool = True) -> dict:
        idk = trip_request.get("idempotency_key") or "trip-c1-http-0001"
        real_user_id = trip_request.get(
            "real_user_id", trip_request.get("user_id", "")) or ""
        result = {
            "outcome": "plan_ready",
            "payment_status": "held",
            "booking_ref": None,
            "idempotency_key": idk,
            "package_total_with_fees_cents": 150000,
            "legs": trip_request.get("legs") or [],
            "day_plans": [],
            "wallet": {"balance_cents": 500000, "held_cents": 150000, "debited": False},
            "_confirm_ctx": {
                "checkout_id": "co-c1-http-1",
                "idempotency_key": idk,
                "user_id": real_user_id,
                "dest_token": "PG",
                "merchant_user_id": trip_request.get("merchant_user_id", ""),
            },
        }
        return result

    def commit_plan(self, *, user_id, checkout_id, idempotency_key,
                     plan_envelope, dest_token, merchant_user_id=""):
        self.commit_plan_calls.append(idempotency_key)
        return {
            "outcome": "success",
            "booking_ref": f"BK-{idempotency_key}",
            "idempotency_key": idempotency_key,
            "payment_status": "charged",
        }


class TestAnonNegotiateConfirmRoundTripHTTP(unittest.TestCase):
    """The exact reported repro: POST /negotiate with plan:true and an
    owner_token but NO user_id -> plan_ready; POST /confirm with the correct
    owner_token must succeed, not 403 session_invalid."""

    def setUp(self) -> None:
        self.store = SqliteDashboardStore(":memory:")
        set_store(self.store)
        self.client = TestClient(server.build_app())

    def _negotiate_body(self) -> dict:
        return {
            # NO "user_id" key — the guest/anonymous path.
            "owner_token": _OWNER_TOKEN,
            "plan": True,
            "total_budget_cents": 150000,
            "legs": [{"city": "port moresby", "checkin": "2026-03-01",
                      "checkout": "2026-03-04", "adults": 1}],
        }

    def test_anon_guest_negotiate_then_confirm_succeeds_end_to_end(self):
        with self.client:
            server._state.orch = _FakePlanReadyOrch()

            resp = self.client.post("/negotiate", json=self._negotiate_body())
            self.assertEqual(resp.status_code, 200)
            stream_id = resp.json()["stream_id"]

            # Drain the SSE stream to completion (server runs negotiate in a
            # worker and pushes the final result as a "negotiate_finished" event).
            result = self._drain_stream(stream_id)
            self.assertEqual(result.get("outcome"), "plan_ready", result)
            idk = result["idempotency_key"]

            # The persisted row must NOT carry a server-minted uuid4 as its owner.
            row = self.store.get_plan(idk)
            self.assertIsNotNone(row)
            self.assertEqual(
                row.get("user_id"), "",
                f"C1 regression: stored row.user_id={row.get('user_id')!r} — a "
                f"server-minted anon uuid4 (or any non-empty value) was persisted "
                f"as the owner, which permanently locks Tier-1 auth against this "
                f"guest trip's own Tier-2 owner_token.",
            )

            # The ONE human consent: /confirm with the correct owner_token.
            confirm_resp = self.client.post("/confirm", json={
                "idempotency_key": idk, "owner_token": _OWNER_TOKEN,
            })
            self.assertEqual(confirm_resp.status_code, 200)
            confirm_out = confirm_resp.json()
            self.assertNotEqual(
                confirm_out.get("outcome"), "forbidden",
                f"C1 regression: guest /confirm with the CORRECT owner_token was "
                f"rejected: {confirm_out!r}",
            )
            self.assertEqual(confirm_out.get("outcome"), "success", confirm_out)
            self.assertTrue(confirm_out.get("booking_ref"))

    def test_anon_guest_wrong_owner_token_still_403(self):
        """The fix must not accidentally open the trip to ANY caller — only
        the correct owner_token still authorizes it."""
        with self.client:
            server._state.orch = _FakePlanReadyOrch()

            resp = self.client.post("/negotiate", json=self._negotiate_body())
            stream_id = resp.json()["stream_id"]
            result = self._drain_stream(stream_id)
            idk = result["idempotency_key"]

            confirm_resp = self.client.post("/confirm", json={
                "idempotency_key": idk, "owner_token": "wrong-token",
            })
            self.assertEqual(confirm_resp.status_code, 403)
            self.assertEqual(confirm_resp.json().get("outcome"), "forbidden")

    def _drain_stream(self, stream_id: str) -> dict:
        with self.client.stream("GET", f"/stream/{stream_id}") as resp:
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                import json as _json
                payload = _json.loads(line[len("data:"):].strip())
                if payload.get("type") == "negotiate_finished":
                    return payload["result"]
        raise AssertionError("stream ended without a negotiate_finished event")


# ---------------------------------------------------------------------------
# 4 — HTTP-level: the REAL orchestrator through the REAL /negotiate_text route
# (Follow-up: the class above drives /negotiate with a fake
# orchestrator; /negotiate_text — the actual originally-reported repro and
# the free-text demo path — was previously only verified "by construction"
# via the orchestrator-level tests + code tracing. This exercises it directly).
# ---------------------------------------------------------------------------

_TEXT_OWNER_TOKEN = "owner-token-c1-negotiate-text-repro"


def _fake_parse_intent_pm_trip(free_text, user_id="guest", nationality=None, today=None):
    """Stand-in for utils.intent_parser.parse_intent: returns the SAME validated
    _PM_TRIP-shaped request the orchestrator-level tests above already prove
    reaches plan_ready under this fixture set. Free-text grammar itself is
    covered elsewhere (test_negotiate_text.py, test_intent_corpus*); patching
    it here keeps this test focused on the C1 identity-threading question
    (server.py -> intent_parser.negotiate_from_text -> orchestrator.negotiate()
    -> _persist_and_sanitize_plan -> store -> /confirm) without depending on
    (or flaking on) the deterministic parser's exact wording."""
    req = copy.deepcopy(_PM_TRIP)
    req["user_id"] = user_id
    if today:
        req["today"] = today
    return req


class TestAnonNegotiateTextConfirmRoundTripHTTP(unittest.TestCase):
    """The EXACT reported repro over /negotiate_text (not /negotiate): POST
    /negotiate_text {text, plan:true, owner_token, NO user_id} -> plan_ready
    via the REAL TravelOrchestrator; the stored row's user_id must be "" (not
    a uuid4); POST /confirm with the correct owner_token must succeed."""

    def setUp(self) -> None:
        self.store = SqliteDashboardStore(":memory:")
        set_store(self.store)
        self.client = TestClient(server.build_app())

    def _negotiate_text_body(self) -> dict:
        return {
            "text": "3 nights in port moresby, city, $1200",
            # NO "user_id" key — the guest/anonymous path.
            "owner_token": _TEXT_OWNER_TOKEN,
            "plan": True,
        }

    def test_anon_guest_negotiate_text_then_confirm_succeeds_end_to_end(self):
        expected_mid = merchant_checkout_owner(user_id=None, owner_token=_TEXT_OWNER_TOKEN)
        transport = _CapturingTransport(
            _plan_phase_responses("co-c1-text-anon", expected_mid))
        orch = _build_society_with_budget_transport(transport)

        with self.client:
            server._state.orch = orch
            with patch("utils.intent_parser.parse_intent",
                       side_effect=_fake_parse_intent_pm_trip):
                resp = self.client.post("/negotiate_text", json=self._negotiate_text_body())

            self.assertEqual(resp.status_code, 200, resp.text)
            result = resp.json()
            self.assertEqual(result.get("outcome"), "plan_ready", result)
            idk = result["idempotency_key"]

            # The persisted row must NOT carry a server-minted uuid4 as its owner.
            row = self.store.get_plan(idk)
            self.assertIsNotNone(row)
            self.assertEqual(
                row.get("user_id"), "",
                f"C1 regression (/negotiate_text): stored row.user_id="
                f"{row.get('user_id')!r} — a server-minted anon uuid4 (or any "
                f"non-empty value) was persisted as the owner via the free-text "
                f"guest path, which permanently locks Tier-1 auth against this "
                f"guest trip's own Tier-2 owner_token.",
            )

            # The ONE human consent: /confirm with the correct owner_token.
            confirm_resp = self.client.post("/confirm", json={
                "idempotency_key": idk, "owner_token": _TEXT_OWNER_TOKEN,
            })
            self.assertEqual(confirm_resp.status_code, 200)
            confirm_out = confirm_resp.json()
            self.assertNotEqual(
                confirm_out.get("outcome"), "forbidden",
                f"C1 regression (/negotiate_text): guest /confirm with the "
                f"CORRECT owner_token was rejected: {confirm_out!r}",
            )
            self.assertEqual(confirm_out.get("outcome"), "success", confirm_out)
            self.assertTrue(confirm_out.get("booking_ref"))

    def test_anon_guest_negotiate_text_wrong_owner_token_still_403(self):
        """Mirrors TestAnonNegotiateConfirmRoundTripHTTP's wrong-token test: the
        fix must not accidentally open the /negotiate_text guest trip to ANY
        caller — only the correct owner_token still authorizes it."""
        expected_mid = merchant_checkout_owner(user_id=None, owner_token=_TEXT_OWNER_TOKEN)
        transport = _CapturingTransport(
            _plan_phase_responses("co-c1-text-anon-wrong", expected_mid))
        orch = _build_society_with_budget_transport(transport)

        with self.client:
            server._state.orch = orch
            with patch("utils.intent_parser.parse_intent",
                       side_effect=_fake_parse_intent_pm_trip):
                resp = self.client.post("/negotiate_text", json=self._negotiate_text_body())
            idk = resp.json()["idempotency_key"]

            confirm_resp = self.client.post("/confirm", json={
                "idempotency_key": idk, "owner_token": "wrong-token",
            })
            self.assertEqual(confirm_resp.status_code, 403)
            self.assertEqual(confirm_resp.json().get("outcome"), "forbidden")


if __name__ == "__main__":
    unittest.main()
