"""
#120 Golden-envelope contract test.

Validates that every JSON fixture in tests/fixtures/golden_envelopes/ conforms
to the field schema that the Travel Guild frontend renders.  Tests are STRICTLY
read-only from fixtures — the orchestrator is never called at test time.

Frontend field audit source:
  App.svelte, Itinerary.svelte (fields enumerated 2026-06-30)
"""

import glob
import json
import os
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Fixture discovery
# ---------------------------------------------------------------------------

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "golden_envelopes")
_FIXTURE_PATHS: list[str] = sorted(glob.glob(os.path.join(FIXTURE_DIR, "*.json")))
_FIXTURE_IDS: list[str] = [os.path.basename(p) for p in _FIXTURE_PATHS]

_NO_FIXTURES = len(_FIXTURE_PATHS) == 0


def _load(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_one_of(d: dict, *keys: str) -> bool:
    """True if at least one of *keys* is present in d."""
    return any(k in d for k in keys)


# ---------------------------------------------------------------------------
# Contract tests — parametrized over all fixture files
# ---------------------------------------------------------------------------

@pytest.mark.skipif(_NO_FIXTURES, reason="No golden envelope fixtures found in tests/fixtures/golden_envelopes/")
@pytest.mark.parametrize("fixture_path", _FIXTURE_PATHS, ids=_FIXTURE_IDS)
class TestGoldenEnvelopeContract:
    """Each method is an independent contract assertion over one fixture."""

    # ---- 1. Top-level envelope keys ----------------------------------------

    def test_outcome_present_and_valid(self, fixture_path: str) -> None:
        """outcome must be a non-empty string."""
        env = _load(fixture_path)
        assert "outcome" in env, "envelope missing 'outcome'"
        assert isinstance(env["outcome"], str) and env["outcome"], (
            f"outcome must be a non-empty string, got {env['outcome']!r}"
        )

    def test_success_envelope_required_top_level_keys(self, fixture_path: str) -> None:
        """Success / plan_ready envelopes must carry legs, day_plans, booking_links
        and either package_total_cents or wallet."""
        env = _load(fixture_path)
        if env.get("outcome") not in ("success", "plan_ready"):
            pytest.skip(f"outcome={env['outcome']!r} — not a success envelope")

        assert "legs" in env, "success envelope missing 'legs'"
        assert "day_plans" in env, "success envelope missing 'day_plans'"
        assert "booking_links" in env, "success envelope missing 'booking_links'"
        assert _has_one_of(env, "package_total_cents", "total_cents", "wallet"), (
            "success envelope must have package_total_cents, total_cents, or wallet"
        )

    def test_success_envelope_booking_ref(self, fixture_path: str) -> None:
        """booking_ref, if present on a success envelope, must be a non-empty string."""
        env = _load(fixture_path)
        if env.get("outcome") not in ("success", "plan_ready"):
            pytest.skip(f"outcome={env['outcome']!r} — not a success envelope")

        ref = env.get("booking_ref")
        if ref is not None:
            assert isinstance(ref, str) and ref, (
                f"booking_ref must be a non-empty string when present, got {ref!r}"
            )

    def test_cannot_satisfy_has_reason(self, fixture_path: str) -> None:
        """cannot_satisfy envelopes must carry a reason string."""
        env = _load(fixture_path)
        if env.get("outcome") != "cannot_satisfy":
            pytest.skip("not a cannot_satisfy envelope")

        assert "reason" in env, "cannot_satisfy envelope missing 'reason'"
        assert isinstance(env["reason"], str) and env["reason"], (
            f"reason must be a non-empty string, got {env['reason']!r}"
        )

    # ---- 2. legs[] schema --------------------------------------------------

    def test_legs_is_non_empty_list(self, fixture_path: str) -> None:
        env = _load(fixture_path)
        if env.get("outcome") not in ("success", "plan_ready"):
            pytest.skip("not a success envelope")

        legs = env.get("legs", [])
        assert isinstance(legs, list) and len(legs) > 0, (
            "success envelope must have at least one leg"
        )

    def test_each_leg_required_fields(self, fixture_path: str) -> None:
        """Each leg must expose the fields rendered by Itinerary.svelte / App.svelte."""
        env = _load(fixture_path)
        if env.get("outcome") not in ("success", "plan_ready"):
            pytest.skip("not a success envelope")

        for idx, leg in enumerate(env.get("legs", [])):
            ctx = f"legs[{idx}]"
            assert isinstance(leg, dict), f"{ctx} must be a dict"

            # App.svelte renders checkin
            assert "checkin" in leg, f"{ctx} missing 'checkin'"
            assert isinstance(leg["checkin"], str) and leg["checkin"], (
                f"{ctx}.checkin must be a non-empty string"
            )

            # checkout — needed for date span
            assert "checkout" in leg, f"{ctx} missing 'checkout'"

            # city — rendered in multiple components
            assert "city" in leg, f"{ctx} missing 'city'"
            assert isinstance(leg["city"], str) and leg["city"], (
                f"{ctx}.city must be a non-empty string"
            )

            # leg_id — Itinerary.svelte keyed loop
            assert "leg_id" in leg, f"{ctx} missing 'leg_id'"

            # Hotel / lodging name (hotel_title is canonical; unverified_lodging is fallback)
            has_lodging_name = (
                ("hotel_title" in leg and leg["hotel_title"])
                or ("hotel_name" in leg and leg["hotel_name"])
                or ("unverified_lodging" in leg and leg["unverified_lodging"])
            )
            assert has_lodging_name, (
                f"{ctx} missing lodging name: need hotel_title, hotel_name, or unverified_lodging"
            )

            # booking_link — rendered in Itinerary.svelte
            assert "booking_link" in leg, f"{ctx} missing 'booking_link'"
            bl = leg["booking_link"]
            assert bl is not None, f"{ctx}.booking_link must not be None"
            # booking_link is a dict with at least booking_url
            if isinstance(bl, dict):
                assert "booking_url" in bl, f"{ctx}.booking_link missing 'booking_url'"
            elif isinstance(bl, list):
                assert len(bl) > 0, f"{ctx}.booking_link list must not be empty"
                assert "booking_url" in bl[0], (
                    f"{ctx}.booking_link[0] missing 'booking_url'"
                )

    def test_each_leg_price_overlay_optional(self, fixture_path: str) -> None:
        """price_overlay and live_price are optional but, if present, must not be
        unexpected types (guards against accidental schema breakage)."""
        env = _load(fixture_path)
        if env.get("outcome") not in ("success", "plan_ready"):
            pytest.skip("not a success envelope")

        for idx, leg in enumerate(env.get("legs", [])):
            for field in ("price_overlay", "live_price", "note"):
                val = leg.get(field)
                if val is not None:
                    assert isinstance(val, (str, int, float, dict)), (
                        f"legs[{idx}].{field} has unexpected type {type(val).__name__}"
                    )

    # ---- 3. day_plans[] schema ---------------------------------------------

    def test_day_plans_is_list(self, fixture_path: str) -> None:
        env = _load(fixture_path)
        if env.get("outcome") not in ("success", "plan_ready"):
            pytest.skip("not a success envelope")

        assert isinstance(env.get("day_plans"), list), "day_plans must be a list"
        assert len(env["day_plans"]) > 0, "day_plans must not be empty on success"

    def test_each_day_plan_required_fields(self, fixture_path: str) -> None:
        """Each day_plan must carry city (App.svelte) and a days list (activity items)."""
        env = _load(fixture_path)
        if env.get("outcome") not in ("success", "plan_ready"):
            pytest.skip("not a success envelope")

        for idx, dp in enumerate(env.get("day_plans", [])):
            ctx = f"day_plans[{idx}]"
            assert isinstance(dp, dict), f"{ctx} must be a dict"

            # App.svelte renders day_plans[].city and day_plans[].checkin
            assert _has_one_of(dp, "city", "date", "checkin"), (
                f"{ctx} must have at least one of: city, date, checkin"
            )

            # day-level activity list: real field is 'days' (list of day objects)
            # 'items' is the UI term; accept either
            has_day_list = _has_one_of(dp, "days", "items")
            assert has_day_list, (
                f"{ctx} must have a 'days' or 'items' list of daily activities"
            )
            day_list_key = "days" if "days" in dp else "items"
            assert isinstance(dp[day_list_key], list), (
                f"{ctx}.{day_list_key} must be a list"
            )

    # ---- 4. booking_links schema -------------------------------------------

    def test_booking_links_has_required_categories(self, fixture_path: str) -> None:
        """booking_links must contain lodging, transport, and insurance entries."""
        env = _load(fixture_path)
        if env.get("outcome") not in ("success", "plan_ready"):
            pytest.skip("not a success envelope")

        bl = env.get("booking_links", {})
        assert isinstance(bl, dict), "booking_links must be a dict"

        for category in ("lodging", "transport", "insurance"):
            assert category in bl, f"booking_links missing '{category}' category"

    def test_booking_links_lodging_has_kind(self, fixture_path: str) -> None:
        """Each lodging booking link must have a 'kind' field."""
        env = _load(fixture_path)
        if env.get("outcome") not in ("success", "plan_ready"):
            pytest.skip("not a success envelope")

        lodging = env.get("booking_links", {}).get("lodging", [])
        assert isinstance(lodging, list), "booking_links.lodging must be a list"

        for idx, entry in enumerate(lodging):
            assert isinstance(entry, dict), f"booking_links.lodging[{idx}] must be a dict"
            assert "kind" in entry, f"booking_links.lodging[{idx}] missing 'kind'"
            assert isinstance(entry["kind"], str) and entry["kind"], (
                f"booking_links.lodging[{idx}].kind must be a non-empty string"
            )

    def test_booking_links_transport_is_list(self, fixture_path: str) -> None:
        """booking_links.transport must be a list (may be empty if no inter-city legs)."""
        env = _load(fixture_path)
        if env.get("outcome") not in ("success", "plan_ready"):
            pytest.skip("not a success envelope")

        transport = env.get("booking_links", {}).get("transport")
        assert isinstance(transport, list), (
            f"booking_links.transport must be a list, got {type(transport).__name__}"
        )
        for idx, entry in enumerate(transport):
            assert isinstance(entry, dict), (
                f"booking_links.transport[{idx}] must be a dict"
            )
            assert "kind" in entry, f"booking_links.transport[{idx}] missing 'kind'"

    def test_booking_links_insurance_has_kind(self, fixture_path: str) -> None:
        """booking_links.insurance must have a 'kind' field (dict or first list item)."""
        env = _load(fixture_path)
        if env.get("outcome") not in ("success", "plan_ready"):
            pytest.skip("not a success envelope")

        insurance = env.get("booking_links", {}).get("insurance")
        assert insurance is not None, "booking_links.insurance must not be None"

        if isinstance(insurance, dict):
            assert "kind" in insurance, "booking_links.insurance missing 'kind'"
            assert isinstance(insurance["kind"], str) and insurance["kind"], (
                "booking_links.insurance.kind must be a non-empty string"
            )
        elif isinstance(insurance, list):
            assert len(insurance) > 0, "booking_links.insurance list must not be empty"
            assert "kind" in insurance[0], (
                "booking_links.insurance[0] missing 'kind'"
            )
        else:
            pytest.fail(
                f"booking_links.insurance has unexpected type {type(insurance).__name__}"
            )

    # ---- 5. Frontend-rendered wallet / budget fields -----------------------

    def test_frontend_budget_fields_types(self, fixture_path: str) -> None:
        """Numeric budget fields rendered by App.svelte must be integers when present."""
        env = _load(fixture_path)
        if env.get("outcome") not in ("success", "plan_ready"):
            pytest.skip("not a success envelope")

        cent_fields = [
            "package_total_cents",
            "package_total_with_fees_cents",
            "budget_shortfall_cents",
            "min_feasible_total_cents",
            "wallet_balance_cents",
            "total_cents",
            "closest_package_total_cents",
        ]
        for field in cent_fields:
            val = env.get(field)
            if val is not None:
                assert isinstance(val, int), (
                    f"{field} must be an int when present, got {type(val).__name__}: {val!r}"
                )

    def test_wallet_fields_when_present(self, fixture_path: str) -> None:
        """If a wallet object is present it must carry held_cents and debit_cents."""
        env = _load(fixture_path)
        wallet = env.get("wallet")
        if wallet is None:
            pytest.skip("no wallet field in this envelope")

        assert isinstance(wallet, dict), "wallet must be a dict"
        assert "held_cents" in wallet, "wallet missing 'held_cents'"
        assert "debit_cents" in wallet, "wallet missing 'debit_cents'"
        assert isinstance(wallet["held_cents"], int), "wallet.held_cents must be int"
        assert isinstance(wallet["debit_cents"], int), "wallet.debit_cents must be int"

    # ---- 6. transport_edges / transport_pricing (App.svelte) ---------------

    def test_transport_fields_types(self, fixture_path: str) -> None:
        """transport_edges and transport_pricing, when present, must be lists / dicts."""
        env = _load(fixture_path)
        if env.get("outcome") not in ("success", "plan_ready"):
            pytest.skip("not a success envelope")

        edges = env.get("transport_edges")
        if edges is not None:
            assert isinstance(edges, (list, dict)), (
                f"transport_edges must be a list or dict, got {type(edges).__name__}"
            )

        pricing = env.get("transport_pricing")
        if pricing is not None:
            assert isinstance(pricing, (list, dict)), (
                f"transport_pricing must be a list or dict, got {type(pricing).__name__}"
            )

    # ---- 7. Itinerary narrative (App.svelte: itinerary_narrative.overview) --

    def test_itinerary_narrative_overview_type(self, fixture_path: str) -> None:
        """If itinerary_narrative is present it must have an overview string."""
        env = _load(fixture_path)
        narrative = env.get("itinerary_narrative")
        if narrative is None:
            pytest.skip("no itinerary_narrative in this envelope")

        assert isinstance(narrative, dict), "itinerary_narrative must be a dict"
        overview = narrative.get("overview")
        if overview is not None:
            assert isinstance(overview, str), (
                f"itinerary_narrative.overview must be a string, got {type(overview).__name__}"
            )


# ---------------------------------------------------------------------------
# Meta test: verify we actually exercised some fixtures
# ---------------------------------------------------------------------------

def test_fixture_discovery_found_files() -> None:
    """Sanity check: fixture discovery must find at least one file, or clearly
    signal zero (so CI catches a missing fixture directory)."""
    if _NO_FIXTURES:
        pytest.skip(
            "No golden envelope fixtures found — "
            "generate them first (see scripts/generate_golden_envelopes.py)"
        )
    assert len(_FIXTURE_PATHS) >= 1, "Expected at least one golden envelope fixture"


def test_all_fixtures_are_valid_json() -> None:
    """Every *.json in the fixture dir must parse without error."""
    if _NO_FIXTURES:
        pytest.skip("No fixture files to validate")

    for path in _FIXTURE_PATHS:
        try:
            _load(path)
        except json.JSONDecodeError as exc:
            pytest.fail(f"{os.path.basename(path)} is not valid JSON: {exc}")
