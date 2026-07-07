"""test_l7_replan_supersede_guard.py — LOW regression: /replan had no
superseded_by guard, unlike /confirm and /refine which both correctly refuse
to act on an already-superseded plan_ready row.

CONTEXT (bug): a parent row that /refine already superseded (status stays
plan_ready, superseded_by set to the child) could still be /replan'd — the
edit was silently applied and the response said outcome:"plan_ready" as if
the edited plan were current and confirmable, when a subsequent /confirm on
it would actually refuse with plan_superseded. No money was at risk (the
subsequent /confirm still correctly refuses), but the endpoint's honest-
outcome contract was violated — the user edits and is shown a plan that can
never be booked, with no warning.

FIX: /replan now carries the SAME superseded_by check /confirm and /refine
already have, refusing with the same plan_locked/reason=superseded shape
/refine already uses for this exact case.
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

_PARENT_IDK = "trip-l7-replan-supersede-parent-0001"
_CHILD_IDK = "trip-l7-replan-supersede-child-0001"
_OWNER = "test-owner-token-l7-replan-supersede"

_ENVELOPE = {
    "outcome": "plan_ready",
    "idempotency_key": _PARENT_IDK,
    "payment_status": "held",
    "booking_ref": None,
    "package_total_with_fees_cents": 150000,
    "package_total_cents": 150000,
    "wallet": {"debited": False, "held_cents": 150000},
    "legs": [{"city": "tokyo"}],
    "day_plans": [
        {
            "city": "tokyo",
            "days": [
                {"day_index": 0, "bad_weather": False,
                 "attractions": [{"name": "Senso-ji", "name_en": "Senso-ji",
                                   "category": "temple", "wikidata": "Q844240",
                                   "lat": 35.71, "lon": 139.79, "provenance": "test"}],
                 "meals": {}},
            ],
            "unscheduled_attractions": [],
        }
    ],
}

_OPS = [{"op": "remove_place", "leg_index": 0, "day_index": 0,
         "position": 0, "attraction_ref": "Senso-ji"}]


def _seed_superseded_parent(store: SqliteDashboardStore) -> None:
    """Seed a plan_ready parent row, then supersede it (mirrors what a
    successful /refine does: mark_superseded on the parent, pointing at a
    child key — parent stays status='plan_ready' forever)."""
    store.save_plan({
        "idempotency_key": _PARENT_IDK,
        "user_id": "",
        "owner_token": _OWNER,
        "checkout_id": "co-l7-replan-supersede-001",
        "dest_token": "JP",
        "package_total_cents": 150000,
        "request": {"user_id": "", "legs": [{"city": "tokyo"}]},
        "envelope": copy.deepcopy(_ENVELOPE),
    })
    store.save_plan({
        "idempotency_key": _CHILD_IDK,
        "user_id": "",
        "owner_token": _OWNER,
        "checkout_id": "co-l7-replan-supersede-child-001",
        "dest_token": "JP",
        "package_total_cents": 160000,
        "request": {"user_id": "", "legs": [{"city": "tokyo"}]},
        "envelope": copy.deepcopy(_ENVELOPE),
    })
    store.mark_superseded(_PARENT_IDK, child_idempotency_key=_CHILD_IDK)


def _client_with_superseded_parent():
    store = SqliteDashboardStore(":memory:")
    set_store(store)
    _seed_superseded_parent(store)
    client = TestClient(server.build_app())
    return client, store


class TestReplanRefusesSupersededRow(unittest.TestCase):
    def test_replan_on_superseded_row_is_refused_not_silently_applied(self):
        client, store = _client_with_superseded_parent()
        with client:
            r = client.post("/replan", json={
                "idempotency_key": _PARENT_IDK,
                "ops": _OPS,
                "owner_token": _OWNER,
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(
            body.get("outcome"), "plan_locked",
            f"L7 REGRESSION: /replan on an already-superseded row must refuse, "
            f"not silently apply the edit and report plan_ready. Got: {body}",
        )
        self.assertEqual(body.get("reason"), "superseded", body)
        self.assertEqual(body.get("superseded_by"), _CHILD_IDK, body)

        # The row itself must be untouched by the edit (no accidental mutation
        # of the envelope before the refusal).
        row = store.get_plan(_PARENT_IDK)
        day = row["envelope"]["day_plans"][0]["days"][0]
        self.assertEqual(len(day["attractions"]), 1, "the edit must not have applied")

    def test_confirm_on_the_same_superseded_row_agrees_with_replan(self):
        """Cross-check: /confirm's existing plan_superseded refusal and
        /replan's new refusal must agree on the SAME row — closing the
        contract gap where /replan said 'plan_ready' but /confirm said
        'plan_superseded' for the identical row."""
        client, store = _client_with_superseded_parent()
        with client:
            r = client.post("/confirm", json={
                "idempotency_key": _PARENT_IDK, "owner_token": _OWNER,
            })
        body = r.json()
        self.assertEqual(body.get("outcome"), "cannot_satisfy", body)
        self.assertEqual(body.get("reason"), "plan_superseded", body)

    def test_replan_on_a_non_superseded_plan_ready_row_still_works(self):
        """var-0/regression guard: a normal, non-superseded plan_ready row
        must still be replan-able exactly as before this fix."""
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        store.save_plan({
            "idempotency_key": _PARENT_IDK,
            "user_id": "",
            "owner_token": _OWNER,
            "checkout_id": "co-l7-replan-supersede-001",
            "dest_token": "JP",
            "package_total_cents": 150000,
            "request": {"user_id": "", "legs": [{"city": "tokyo"}]},
            "envelope": copy.deepcopy(_ENVELOPE),
        })
        client = TestClient(server.build_app())
        with client:
            r = client.post("/replan", json={
                "idempotency_key": _PARENT_IDK,
                "ops": _OPS,
                "owner_token": _OWNER,
            })
        body = r.json()
        self.assertEqual(body.get("outcome"), "plan_ready", body)


if __name__ == "__main__":
    unittest.main()
