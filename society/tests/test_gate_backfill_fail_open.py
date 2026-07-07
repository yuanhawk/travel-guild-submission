"""
#26 — regression: the city-only-leg gate-skip fail-open is CLOSED, without
breaking the append-only contract (a non-opted-in trip stays byte-identical).

The fail-open: a leg named only by `city` (no place_key/dest_country) silently
skipped the health + compliance gates — a silent vaccine/visa pass. Fix: once a
gate is ACTIVE (nationality for compliance; place_key for health), backfill
dest_country from CITY_TO_ISO2 so every leg is checked. A trip that opted into
neither stays a pass-through.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tests.test_composition_wiring import build_society


def test_compliance_fires_for_city_only_leg_when_nationality_present():
    """Nationality + a CITY-ONLY leg (no dest_country) → compliance now FIRES
    (backfilled dest_country), instead of silently skipping the visa check."""
    orch = build_society()
    trip = {
        "user_id": "failopen26-fire",
        "total_budget_cents": 200000,
        "today": "2026-06-16",
        "nationality": "US",
        # CITY ONLY — the exact fail-open shape (no dest_country, no place_key).
        "legs": [{"city": "bali", "checkin": "2026-09-01", "checkout": "2026-09-04",
                  "adults": 1, "vibe": "beach"}],
    }
    res = orch.negotiate(trip)
    # The gate ran: a verdict is surfaced (success path) or the trip blocked on it.
    fired = ("compliance_verdict" in res) or (
        res.get("outcome") == "cannot_satisfy"
        and "compliance" in (res.get("reason", "") + str(res.get("blocked_gate", "")))
    )
    assert fired, f"compliance did not fire for city-only leg + nationality: {res.get('outcome')}"
    print("[#26] PASS — compliance fires for a city-only leg when nationality present")


def test_passthrough_unchanged_without_optin():
    """Append-only: the SAME city-only trip WITHOUT nationality/place_key stays a
    pure pass-through — no gate keys (S1–S5 byte-identical)."""
    orch = build_society()
    trip = {
        "user_id": "failopen26-passthrough",
        "total_budget_cents": 200000,
        "legs": [{"city": "bali", "checkin": "2026-09-01", "checkout": "2026-09-04",
                  "adults": 1, "vibe": "beach"}],
    }
    res = orch.negotiate(trip)
    assert "compliance_verdict" not in res, "compliance must NOT fire without opt-in"
    assert "health_verdict" not in res, "health must NOT fire without opt-in"
    print("[#26] PASS — non-opted-in city trip unchanged (append-only preserved)")


if __name__ == "__main__":
    test_compliance_fires_for_city_only_leg_when_nationality_present()
    test_passthrough_unchanged_without_optin()
    print("ALL #26 FAIL-OPEN REGRESSION TESTS PASSED")
