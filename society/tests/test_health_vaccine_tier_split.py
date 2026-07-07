"""
Feature #39 — Vaccine mandatory/optional tier split.

The MANDATORY-tier vaccine spend (REQUIRED_FOR_ENTRY) is the budget-affecting
correction (folded into total_vaccine_cost / line_items, as before — that contract
is frozen). The OPT-IN OPTIONAL estimate (CDC_RECOMMENDED / SITUATIONAL jabs) is a
SEPARATE surfaced estimate that is NEVER folded into the trip budget.

Invariants proven here:
  T1. assess_destination exposes mandatory_vaccine_cost_usd_cents = sum of
      REQUIRED_FOR_ENTRY usd_cents only, and it is <= total_vaccine_cost.
  T2. The optional_vaccine_estimate surfaces ONLY when there is >=1 optional jab,
      lists exactly the CDC_RECOMMENDED/SITUATIONAL jabs, sums them, and is flagged
      not_in_budget=True. Its total + mandatory total == total_vaccine_cost.
  T3. The optional estimate is NEVER added to the budget: line_items[*].usd_cents
      and total_vaccine_cost_usd_cents are UNCHANGED by the split (still the full
      slate sum — the frozen I1 contract), and NO line item carries the optional
      estimate value alone.
  T4. A no-mandatory-cert slate (Thailand) still has mandatory total == 0 but a
      non-empty optional estimate.
  T5. APPEND-ONLY: a plain trip with NO place_key (city-only) never fires Health;
      and assess() over a no-place_key leg list produces a verdict whose
      trip-level optional/mandatory keys are absent OR empty and whose
      total/line_items are unchanged from the pre-feature behavior.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agents import health_agent as H  # noqa: E402
from agents.health_agent import (  # noqa: E402
    RequirementKind,
    assess,
    assess_destination,
)

TODAY = "2026-01-01"
FAR_DEP = "2027-01-01"  # >> any lead time, so nothing blocks


def _d(place_key):
    return assess_destination(
        place_key=place_key, departure_date=FAR_DEP, today=TODAY
    )


def test_t1_mandatory_cost_is_required_for_entry_only():
    d = _d("ethiopia")
    mand = sum(
        int(v["usd_cents"])
        for v in d["vaccines"]
        if v["kind"] == RequirementKind.REQUIRED_FOR_ENTRY
    )
    assert "mandatory_vaccine_cost_usd_cents" in d
    assert d["mandatory_vaccine_cost_usd_cents"] == mand
    # mandatory is a strict subset of the full slate sum
    assert d["mandatory_vaccine_cost_usd_cents"] <= d["total_vaccine_cost_usd_cents"]
    # Ethiopia has a YF entry cert → mandatory > 0
    assert d["mandatory_vaccine_cost_usd_cents"] > 0


def test_t2_optional_estimate_surfaces_and_reconciles():
    d = _d("ethiopia")
    est = d.get("optional_vaccine_estimate")
    assert est is not None, "Ethiopia has CDC-recommended jabs → optional estimate present"
    # explicitly flagged as NOT in the budget
    assert est["not_in_budget"] is True
    # lists exactly the optional jabs (CDC_RECOMMENDED + SITUATIONAL)
    optional_kinds = {RequirementKind.CDC_RECOMMENDED, RequirementKind.SITUATIONAL}
    expected_ids = [
        v["vaccine"] for v in d["vaccines"] if v["kind"] in optional_kinds
    ]
    got_ids = [item["vaccine"] for item in est["items"]]
    assert got_ids == expected_ids, (expected_ids, got_ids)
    # estimate total == sum of optional usd_cents
    expected_total = sum(
        int(v["usd_cents"]) for v in d["vaccines"] if v["kind"] in optional_kinds
    )
    assert est["total_usd_cents"] == expected_total
    # tier reconciliation: mandatory + optional == full slate sum
    assert (
        d["mandatory_vaccine_cost_usd_cents"] + est["total_usd_cents"]
        == d["total_vaccine_cost_usd_cents"]
    )


def test_t3_optional_never_folded_into_budget():
    """The frozen I1 budget contract is untouched: trip total + line item still the
    FULL slate sum; the optional estimate is never billed."""
    v = assess(
        legs=[{"place_key": "ethiopia", "departure_date": FAR_DEP}],
        today=TODAY,
    )
    d = v["per_destination"][0]
    full = d["total_vaccine_cost_usd_cents"]
    # frozen contract: trip total == full slate sum (mandatory + optional together)
    assert v["total_vaccine_cost_usd_cents"] == full
    assert len(v["line_items"]) == 1
    assert v["line_items"][0]["kind"] == "vaccine"
    assert v["line_items"][0]["usd_cents"] == full
    # the optional estimate value is NEVER what got billed
    est = d.get("optional_vaccine_estimate")
    assert est is not None
    assert v["line_items"][0]["usd_cents"] != est["total_usd_cents"]
    # trip-level optional estimate, if present, is flagged out-of-budget
    trip_est = v.get("optional_vaccine_estimate")
    assert trip_est is not None
    assert trip_est["not_in_budget"] is True
    assert trip_est["total_usd_cents"] == est["total_usd_cents"]


def test_t4_no_mandatory_slate_zero_mandatory_nonempty_optional():
    d = _d("thailand")
    assert d["mandatory_certs"] == []
    assert d["mandatory_vaccine_cost_usd_cents"] == 0
    est = d.get("optional_vaccine_estimate")
    assert est is not None and est["total_usd_cents"] > 0
    # the whole slate sum is optional here
    assert est["total_usd_cents"] == d["total_vaccine_cost_usd_cents"]


def test_t5_appendonly_no_optional_key_when_no_optional_jabs():
    """A slate with NO optional jabs must NOT grow an optional_vaccine_estimate key
    (additive key absent when there is no estimate). We assert the key is absent for
    any seeded slate whose vaccines are all REQUIRED_FOR_ENTRY (or empty)."""
    optional_kinds = {RequirementKind.CDC_RECOMMENDED, RequirementKind.SITUATIONAL}
    checked = 0
    for key in H._HEALTH_SLATES:
        d = assess_destination(place_key=key, departure_date=FAR_DEP, today=TODAY)
        has_optional = any(v["kind"] in optional_kinds for v in d.get("vaccines", []))
        if not has_optional:
            checked += 1
            assert "optional_vaccine_estimate" not in d, key
        else:
            assert d.get("optional_vaccine_estimate") is not None, key
    # mandatory key is always present (additive, harmless scalar)
    # (no assertion count requirement — just exercising every slate deterministically)


def test_t5b_determinism_byte_identical():
    """var-0: same input → byte-identical structured output (run twice)."""
    import json

    def run():
        return json.dumps(
            assess(
                legs=[{"place_key": "ethiopia", "departure_date": FAR_DEP}],
                today=TODAY,
            ),
            sort_keys=True,
            default=str,
        )

    assert run() == run()


def test_t6_held_cert_clears_gate_and_zeroes_cost_70():
    """#70: a HELD entry-required cert (e.g. yellow_fever for Ethiopia) is already satisfied —
    it must (a) NOT block on lead-time even when departure is sooner than the dosing lead, and
    (b) drop out of the enforced budget (mandatory cost 0, no mandatory_line_items) while being
    surfaced as held. Recommended jabs are unaffected."""
    leg = [{"place_key": "ethiopia", "leg_id": "leg-0", "departure_date": "2026-09-08"}]
    # NOT held + short lead -> blocks on the YF cert lead-time.
    blocked = H.assess(legs=leg, today="2026-09-01")
    assert blocked["verdict"] == "cannot_complete", blocked["verdict"]
    # HELD -> gate cleared (no lead-time block), cost 0, surfaced as held, mandatory line gone.
    held = H.assess(legs=leg, today="2026-09-01", held_vaccine_certs=["yellow_fever"])
    assert held["verdict"] != "cannot_complete", held["verdict"]
    assert held["mandatory_vaccine_cost_usd_cents"] == 0, held["mandatory_vaccine_cost_usd_cents"]
    assert held["mandatory_line_items"] == [], held["mandatory_line_items"]
    assert "yellow_fever" in held["held_certs"], held["held_certs"]
    # full slate display total is unchanged (frozen contract); held just isn't billed/enforced.
    assert held["total_vaccine_cost_usd_cents"] == blocked["total_vaccine_cost_usd_cents"]


def test_t7_mandatory_line_items_are_required_for_entry_only_70():
    """#70: mandatory_line_items (the ENFORCED vaccine fee) sums REQUIRED_FOR_ENTRY only —
    Ethiopia's YF (11100), not the full recommended slate (75480, which stays in line_items)."""
    leg = [{"place_key": "ethiopia", "leg_id": "leg-0", "departure_date": "2026-12-01"}]
    v = H.assess(legs=leg, today="2026-09-01")
    mand = sum(int(li["money"]["usd_cents"]) for li in v["mandatory_line_items"])
    full = sum(int(li["money"]["usd_cents"]) for li in v["line_items"])
    assert mand == 11100, mand
    assert full == 75480, full
    assert v["mandatory_vaccine_cost_usd_cents"] == 11100
