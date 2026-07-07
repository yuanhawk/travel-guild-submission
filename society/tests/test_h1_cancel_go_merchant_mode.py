"""test_h1_cancel_go_merchant_mode.py — H1 HIGH regression: /cancel must
genuinely reach the merchant in Go-merchant ("faithful"/CI e2e) deployment
mode, not fabricate a synthetic {"status": "cancelled"} with zero merchant
interaction.

CONTEXT (bug): server.py's /cancel (_do_cancel) branches on
`_state.local_merchant`. When it is None — the Go-merchant deployment mode
(UCP_MERCHANT_URL set) — there was NO code path that ever called the
merchant at all: it fabricated `{"id": checkout_id, "status": "cancelled"}`
and returned. The store was still marked cancelled and the user was told
"cancelled, refunded_cents: 0", but the Go merchant's checkout session
stayed live/complete and the wallet was never credited server-side — a
silent no-op dressed up as a success.

FIX: orchestrator.cancel_plan() (new) dispatches the REAL budget.cancel A2A
skill (mirrors commit_plan's dispatch pattern) to BudgetAgent._cancel_merchant
-> cancel_checkout, which — in Go-merchant mode — makes a genuine HTTP call
to the merchant. server.py's _do_cancel now calls orchestrator.cancel_plan()
instead of fabricating a response whenever local_merchant is None.

Tests:
  1. test_go_merchant_mode_cancel_genuinely_calls_the_merchant — local_merchant
     is None; a fake orchestrator's cancel_plan() must be invoked with the
     right checkout_id/user_id, and its REAL wallet_credit_cents must reach
     the HTTP response + the stored row (not a fabricated 0).
  2. test_go_merchant_mode_merchant_error_does_not_silently_claim_success —
     when the merchant genuinely fails the void (e.g. not_owner/error), the
     endpoint must not return outcome:"cancelled" at all (L6 fix, a later
     batch from the same audit family: an `error`-shaped merchant result now
     surfaces an honest cancel_errored/needs_reconciliation terminal instead
     of a fabricated success with refunded_cents:0 — see
     test_l6_l9_l11_cancel_fixes.py for that fix's own dedicated coverage).
     This test's assertions were updated in lockstep with the L6 fix; they
     originally asserted the very "cancelled, refunded_cents:0" shape L6
     later closed as its own, distinct bug.
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

_BOOKED_IDK = "trip-h1-cancel-booked-0001"
_USER_ID = "demo-mei"  # a REAL pre-seeded demo user (session_token mintable)

_BOOKED_ENVELOPE = {
    "outcome": "success",
    "idempotency_key": _BOOKED_IDK,
    "payment_status": "charged",
    "booking_ref": "BK-H1-TEST-9001",
    "package_total_with_fees_cents": 75000,
    "wallet": {"debited": True, "debit_cents": 75000},
    "legs": [{"city": "tokyo"}],
    "day_plans": [],
}


def _seed_booked(store: SqliteDashboardStore) -> None:
    store.save_plan({
        "idempotency_key": _BOOKED_IDK,
        "user_id": _USER_ID,
        "checkout_id": "co-h1-booked-001",
        "merchant_user_id": _USER_ID,
        "dest_token": "JP",
        "package_total_cents": 75000,
        "envelope": copy.deepcopy(_BOOKED_ENVELOPE),
    })
    store.mark_booked(
        _BOOKED_IDK, booking_ref="BK-H1-TEST-9001",
        envelope=copy.deepcopy(_BOOKED_ENVELOPE),
        confirmed_at="2026-10-01T00:00:00+00:00",
    )


def _client_with_seeded_store():
    store = SqliteDashboardStore(":memory:")
    set_store(store)
    _seed_booked(store)
    return TestClient(server.build_app()), store


def _login(c, user_id: str) -> str:
    r = c.post("/session/login", json={"user_id": user_id})
    assert r.status_code == 200, r.text
    return r.json()["session_token"]


class _FakeGoMerchantOrch:
    """Stands in for the REAL TravelOrchestrator's cancel_plan() dispatch —
    this test's job is to prove server.py's /cancel genuinely INVOKES it
    (with the right args) when local_merchant is None, and genuinely
    threads its result back to the caller/store. orchestrator.py's own
    cancel_plan()/`_call_budget_cancel` dispatch correctness is covered
    separately (mirrors commit_plan/_call_budget_commit's existing coverage
    pattern — no new agent-stack fixture needed for THIS regression)."""

    def __init__(self, decision: str = "cancelled", wallet_credit_cents: int = 75000,
                 wallet_balance_cents: int = 575000):
        self.cancel_plan_calls: list[dict] = []
        self._decision = decision
        self._wallet_credit_cents = wallet_credit_cents
        self._wallet_balance_cents = wallet_balance_cents

    def cancel_plan(self, *, user_id, checkout_id, merchant_user_id="", autonomy_level="L2"):
        self.cancel_plan_calls.append({
            "user_id": user_id, "checkout_id": checkout_id,
            "merchant_user_id": merchant_user_id,
        })
        if self._decision == "cancelled":
            return {
                "decision": "cancelled",
                "checkout_id": checkout_id,
                "wallet_credit_cents": self._wallet_credit_cents,
                "wallet_balance_cents": self._wallet_balance_cents,
            }
        return {"decision": self._decision, "checkout_id": checkout_id,
                "reason": "not_session_owner" if self._decision == "not_owner" else "merchant_5xx"}


class TestH1CancelGoMerchantMode(unittest.TestCase):

    def test_go_merchant_mode_cancel_genuinely_calls_the_merchant(self):
        client, store = _client_with_seeded_store()
        fake_orch = _FakeGoMerchantOrch()
        with client:
            # Go-merchant deployment mode: no in-process local merchant.
            server._state.local_merchant = None
            server._state.orch = fake_orch
            token = _login(client, _USER_ID)
            r = client.post("/cancel", json={
                "idempotency_key": _BOOKED_IDK,
                "user_id": _USER_ID,
                "session_token": token,
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()

        # H1 regression: the merchant must have genuinely been called.
        self.assertEqual(
            len(fake_orch.cancel_plan_calls), 1,
            "H1 regression: /cancel in Go-merchant mode never called the "
            "merchant at all (fabricated a synthetic cancelled response).",
        )
        call = fake_orch.cancel_plan_calls[0]
        self.assertEqual(call["checkout_id"], "co-h1-booked-001")
        self.assertEqual(call["user_id"], _USER_ID)

        # The REAL wallet credit (from the merchant, via cancel_plan) must
        # reach the HTTP response — not a fabricated refunded_cents: 0.
        self.assertEqual(body["outcome"], "cancelled")
        self.assertEqual(
            body["refunded_cents"], 75000,
            f"H1 regression: refunded_cents={body['refunded_cents']!r} — the "
            f"merchant's real wallet credit did not reach the caller.",
        )
        self.assertEqual(body["wallet_balance_cents"], 575000)

        row = store.get_plan(_BOOKED_IDK)
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(
            row["refunded_cents"], 75000,
            "H1 regression: the stored row's refunded_cents must reflect the "
            "REAL merchant credit, not a fabricated 0.",
        )

    def test_go_merchant_mode_merchant_error_does_not_silently_claim_success(self):
        client, store = _client_with_seeded_store()
        fake_orch = _FakeGoMerchantOrch(decision="error")
        with client:
            server._state.local_merchant = None
            server._state.orch = fake_orch
            token = _login(client, _USER_ID)
            r = client.post("/cancel", json={
                "idempotency_key": _BOOKED_IDK,
                "user_id": _USER_ID,
                "session_token": token,
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(fake_orch.cancel_plan_calls), 1)
        # L6 fix: a genuine merchant-side cancel failure must NOT be reported
        # as outcome:"cancelled" (with a fabricated 0 credit) — it must
        # surface an honest, retryable terminal instead, and the row must
        # stay 'booked' (not marked cancelled) so a retry remains possible.
        self.assertNotEqual(
            body.get("outcome"), "cancelled",
            "L6 REGRESSION: a genuine merchant-side cancel error was "
            "reported as a successful cancellation.",
        )
        self.assertTrue(body.get("needs_reconciliation"), body)
        row = store.get_plan(_BOOKED_IDK)
        self.assertEqual(row["status"], "booked")


if __name__ == "__main__":
    unittest.main()
