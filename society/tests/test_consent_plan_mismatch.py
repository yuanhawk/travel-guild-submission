"""test_consent_plan_mismatch.py — HIGH regression: consent must bind to the
plan CONTENT actually shown, not merely the request digest.

CONTEXT: the idempotency_key/consent handle is orchestrator._request_digest()
— a sha256 over REQUEST fields (user_id, total_budget_cents, nationality,
today, legs, wallet_balance_cents). It never covers the resulting PLAN content
(chosen hotels, checkout_id, priced total). Before the fix,
server._persist_and_sanitize_plan's call to store.save_plan() was an
unconditional upsert on that same idempotency_key: a second /negotiate or
/negotiate_text run under the SAME digest (a stale-tab double-submit, a second
device, or a re-run after catalog/price data changed) would silently OVERWRITE
the held row's checkout_id/envelope/total — while a stale tab still showing the
OLD total remained free to POST /confirm and would then book/charge the NEW,
never-reviewed content under the SAME consent key.

FIX: _persist_and_sanitize_plan now compares the freshly-computed priced total
against any row ALREADY held under the same idempotency_key. If they diverge,
it mints a distinct derived key for the NEW content (mirrors /refine's existing
pattern of minting a new key for a new generation) instead of overwriting the
original row in place — so a stale tab's /confirm always sees exactly the
total it originally reviewed.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration import server
from orchestration.store import SqliteDashboardStore, set_store

_IDK = "trip-consent-mismatch-0001"
_OWNER = "owner-token-consent-mismatch"


def _held_plan_body(total_cents: int, checkout_id: str) -> dict:
    """A plan_ready result shaped like what orchestrator.negotiate(commit=False)
    returns, about to be persisted via _persist_and_sanitize_plan."""
    return {
        "outcome": "plan_ready",
        "idempotency_key": _IDK,
        "package_total_with_fees_cents": total_cents,
        "legs": [{"city": "tokyo", "hotel_id": f"hotel-{checkout_id}",
                  "checkin": "2026-10-15", "checkout": "2026-10-19"}],
        "day_plans": [],
        "_confirm_ctx": {
            "user_id": "", "checkout_id": checkout_id, "dest_token": "JP",
            "idempotency_key": _IDK, "merchant_user_id": "",
        },
    }


class TestConsentPlanMismatch(unittest.TestCase):

    def setUp(self) -> None:
        self.store = SqliteDashboardStore(":memory:")
        set_store(self.store)

    def test_same_digest_diverging_total_gets_a_derived_key_not_overwritten(self):
        """Tab A holds $1,200 (co-1). A later same-digest re-plan produces
        $1,450 (co-2) — content genuinely diverged. The original row/key must
        be left completely untouched; the new content must persist under a
        DIFFERENT key."""
        first = _held_plan_body(120_000, "co-1")
        body = {"user_id": "", "owner_token": _OWNER}
        out_first = server._persist_and_sanitize_plan(dict(first), dict(body))
        self.assertEqual(out_first["idempotency_key"], _IDK)

        row_after_first = self.store.get_plan(_IDK)
        self.assertEqual(row_after_first["package_total_cents"], 120_000)
        self.assertEqual(row_after_first["checkout_id"], "co-1")

        # Later: same digest (same idempotency_key), DIFFERENT priced content.
        second = _held_plan_body(145_000, "co-2")
        out_second = server._persist_and_sanitize_plan(dict(second), dict(body))

        # The second result must NOT be persisted under the same key.
        self.assertNotEqual(
            out_second["idempotency_key"], _IDK,
            "REGRESSION: a same-digest re-plan with a DIFFERENT total was "
            "persisted under the SAME idempotency_key — a stale tab holding "
            "the original consent key can now confirm unseen content.",
        )

        # The ORIGINAL row must be byte-for-byte untouched — a stale tab's
        # /confirm on the original key must still see exactly $1,200/co-1.
        row_still_first = self.store.get_plan(_IDK)
        self.assertEqual(row_still_first["package_total_cents"], 120_000,
                         "REGRESSION: the original held plan's total was clobbered")
        self.assertEqual(row_still_first["checkout_id"], "co-1",
                         "REGRESSION: the original held plan's checkout_id was clobbered")

        # The new content IS persisted, just under its own distinct key.
        derived_row = self.store.get_plan(out_second["idempotency_key"])
        self.assertIsNotNone(derived_row, "the diverged content must still be persisted somewhere")
        self.assertEqual(derived_row["package_total_cents"], 145_000)
        self.assertEqual(derived_row["checkout_id"], "co-2")

    def test_same_digest_same_total_still_reuses_the_original_key(self):
        """Sanity counterpart: when the re-run's total is UNCHANGED (the normal,
        harmless idempotent-replay case), the fix must NOT fork a new key —
        confirms the guard doesn't break ordinary re-POST idempotency."""
        first = _held_plan_body(120_000, "co-1")
        body = {"user_id": "", "owner_token": _OWNER}
        server._persist_and_sanitize_plan(dict(first), dict(body))

        # Re-run with the SAME total (a harmless identical replay — e.g. a
        # fresh merchant checkout session with the exact same priced content).
        replay = _held_plan_body(120_000, "co-1-replay")
        out_replay = server._persist_and_sanitize_plan(dict(replay), dict(body))

        self.assertEqual(
            out_replay["idempotency_key"], _IDK,
            "an identical-total re-run should NOT be forked to a new key",
        )
        row = self.store.get_plan(_IDK)
        self.assertEqual(row["package_total_cents"], 120_000)

    def test_no_existing_row_persists_normally_under_the_base_key(self):
        """Sanity counterpart: the very FIRST plan for a digest (no existing
        row yet) is unaffected by the guard."""
        first = _held_plan_body(99_000, "co-only")
        body = {"user_id": "", "owner_token": _OWNER}
        out = server._persist_and_sanitize_plan(dict(first), dict(body))
        self.assertEqual(out["idempotency_key"], _IDK)
        row = self.store.get_plan(_IDK)
        self.assertEqual(row["package_total_cents"], 99_000)

    # -- M6: a diverging-total fork must ALSO supersede the original row -----

    def test_total_diverged_fork_supersedes_the_original_row(self):
        """M6: after a total-divergence fork, the ORIGINAL row must be stamped
        superseded_by the new derived key — otherwise both keys stay
        independently /confirm-able for the same trip intent (the exact
        cross-generation double-book class /refine's own supersede mechanism
        exists to prevent)."""
        first = _held_plan_body(120_000, "co-1")
        body = {"user_id": "", "owner_token": _OWNER}
        server._persist_and_sanitize_plan(dict(first), dict(body))

        second = _held_plan_body(145_000, "co-2")
        out_second = server._persist_and_sanitize_plan(dict(second), dict(body))
        derived_idk = out_second["idempotency_key"]
        self.assertNotEqual(derived_idk, _IDK)

        original_row = self.store.get_plan(_IDK)
        self.assertEqual(
            original_row.get("superseded_by"), derived_idk,
            "REGRESSION (M6): the original row was forked away from but never "
            "stamped superseded — it stays independently confirmable, "
            "recreating the exact double-book class /refine's supersede "
            "mechanism exists to prevent.",
        )

    # -- M4: a same-day identical re-POST of a CANCELLED row must fork -------

    def test_repost_of_a_cancelled_row_forks_instead_of_silently_no_opping(self):
        """M4: save_plan's own `WHERE status='plan_ready'` guard makes an
        in-place overwrite of a cancelled row a silent NO-OP — the caller
        would be told outcome='plan_ready' under the ORIGINAL key, but
        /confirm on that key still returns plan_cancelled and /refine returns
        plan_locked. A same-day identical re-POST (deterministic var-0, same
        total) must instead get a genuinely fresh, usable key."""
        first = _held_plan_body(120_000, "co-1")
        body = {"user_id": "", "owner_token": _OWNER}
        server._persist_and_sanitize_plan(dict(first), dict(body))
        # mark_cancelled requires status='booked' first (mirrors the real
        # confirm-then-cancel lifecycle) — go through that transition here.
        self.store.mark_booked(_IDK, booking_ref="BK-cancelled-repro",
                               envelope={"outcome": "success"}, confirmed_at="2026-01-01T00:00:00+00:00")
        self.store.mark_cancelled(_IDK, refunded_cents=120_000)
        self.assertEqual(self.store.get_plan(_IDK)["status"], "cancelled")

        # Same-day identical re-POST — SAME total (var-0), SAME idempotency_key.
        repost = _held_plan_body(120_000, "co-1")
        out = server._persist_and_sanitize_plan(dict(repost), dict(body))

        self.assertNotEqual(
            out["idempotency_key"], _IDK,
            "REGRESSION (M4): a re-POST of a cancelled trip was silently "
            "no-op'd under the cancelled key instead of getting a fresh, "
            "confirmable key.",
        )
        # The cancelled row itself must stay completely untouched.
        cancelled_row = self.store.get_plan(_IDK)
        self.assertEqual(cancelled_row["status"], "cancelled")
        # The new key must be genuinely usable — a fresh plan_ready row.
        new_row = self.store.get_plan(out["idempotency_key"])
        self.assertIsNotNone(new_row)
        self.assertEqual(new_row["status"], "plan_ready")

    # -- M5: a re-POST of an ALREADY-superseded row's original content -------

    def test_repost_of_a_superseded_row_forks_and_the_fork_stays_superseded(self):
        """M5: after a /refine, the parent row stays status='plan_ready' with
        superseded_by=<child> set. A same-day identical re-POST of the
        ORIGINAL (pre-refine) request must NOT overwrite the parent's
        envelope in place (which would silently rewrite a superseded row's
        historical content while /confirm on it still (correctly) redirects
        to the child) — it must fork to a distinct key, AND that new key must
        itself be superseded to the SAME child (never an independently-
        confirmable duplicate of already-replaced content)."""
        first = _held_plan_body(120_000, "co-1")
        body = {"user_id": "", "owner_token": _OWNER}
        server._persist_and_sanitize_plan(dict(first), dict(body))
        self.store.mark_superseded(_IDK, child_idempotency_key="trip-child-9999")
        self.assertEqual(
            self.store.get_plan(_IDK).get("superseded_by"), "trip-child-9999")

        # Same-day identical re-POST of the ORIGINAL pre-refine request.
        repost = _held_plan_body(120_000, "co-1-repost")
        out = server._persist_and_sanitize_plan(dict(repost), dict(body))

        self.assertNotEqual(
            out["idempotency_key"], _IDK,
            "REGRESSION (M5): a re-POST of an already-superseded row's "
            "original content overwrote the superseded row's envelope in "
            "place instead of forking.",
        )
        # The original superseded row's envelope/pointer must be untouched.
        original_row = self.store.get_plan(_IDK)
        self.assertEqual(original_row["checkout_id"], "co-1")
        self.assertEqual(original_row.get("superseded_by"), "trip-child-9999")
        # The new fork must ALSO point at the same real current generation —
        # never an independently-confirmable duplicate of stale content.
        derived_row = self.store.get_plan(out["idempotency_key"])
        self.assertIsNotNone(derived_row)
        self.assertEqual(
            derived_row.get("superseded_by"), "trip-child-9999",
            "REGRESSION (M5): the forked duplicate of already-superseded "
            "content is independently confirmable — recreates the exact "
            "cross-generation double-book class the supersede mechanism "
            "exists to prevent.",
        )

    # -- M1: a re-POST racing a concurrent /confirm must fork, never clobber -

    def test_repost_racing_an_inflight_confirm_forks_instead_of_clobbering(self):
        """M1: the negotiate worker's persist runs on an executor thread
        OUTSIDE the per-idk asyncio trip lock, so a same-digest re-POST
        racing a concurrent /confirm (which holds server._state.trip_locks[idk]
        for its whole read-validate-commit-write sequence) could otherwise
        overwrite checkout_id out from under a commit that already captured
        the OLD checkout_id at its pre-commit read. Simulate the in-flight
        window directly via _state.trip_locks (the same registry the H3
        sweep-exclusion guard consults) and assert the persist forks instead
        of touching the row."""
        first = _held_plan_body(120_000, "co-1")
        body = {"user_id": "", "owner_token": _OWNER}
        server._persist_and_sanitize_plan(dict(first), dict(body))

        # Simulate a concurrent /confirm currently holding this idk's trip lock.
        server._state.trip_locks = {_IDK: object()}
        try:
            # Same total (a harmless-looking identical re-POST) — the OLD
            # guard only forked on total divergence, so this exact case used
            # to sail through and overwrite checkout_id in place.
            repost = _held_plan_body(120_000, "co-1-fresh-session")
            out = server._persist_and_sanitize_plan(dict(repost), dict(body))
        finally:
            server._state.trip_locks = {}

        self.assertNotEqual(
            out["idempotency_key"], _IDK,
            "REGRESSION (M1): a same-digest re-POST racing an in-flight "
            "/confirm was persisted under the SAME key — a concurrent "
            "commit's captured checkout_id can now silently diverge from "
            "the row's stored checkout_id.",
        )
        original_row = self.store.get_plan(_IDK)
        self.assertEqual(
            original_row["checkout_id"], "co-1",
            "REGRESSION (M1): the row an in-flight /confirm is using had its "
            "checkout_id swapped out from under it.",
        )


if __name__ == "__main__":
    unittest.main()
