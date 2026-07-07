"""#70 multi-passport — best-passport selection (PURE unit tests).

Covers gate_leg_best_passport + check_eligibility(nationalities=...): the gate picks, PER LEG,
the passport giving the best entry outcome (visa-free < eVisa/ETA < VOA < visa-required, then
cheapest). Two invariants protect the rest of the system:
  • FAIL-CONSERVATIVE — a leg blocked under EVERY passport still BLOCKS (a strong passport
    never silently rescues a seeded block);
  • BYTE-IDENTICAL — the single-nationality path (nationalities absent/None) is unchanged, so
    every existing trip keeps its var-0 output.
All deterministic, no LLM / network / merchant.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.compliance_agent import (  # noqa: E402
    GateVerdict,
    check_eligibility,
    gate_leg_best_passport,
)

TODAY = "2026-06-23"
FAR = "2026-12-01"   # ample lead — no lead-time block confound


def test_best_passport_prefers_visa_free_over_visa_required():
    # CN: IN passport is VISA_REQUIRED (fee), JP is VISA_FREE (fee 0). Best = JP.
    # (SG/TH/MY/JP became visa-free to CN in 2024 — IN is a still-visa-required pair.
    # E1 fix: US is NOT visa-free to China — China's waivers never covered the US,
    # only a 240-hour transit exemption — so this test no longer uses US as the
    # visa-free exemplar; JP is a corridor unaffected by that correction.)
    b = gate_leg_best_passport(dest_country="CN", nationalities=["IN", "JP"],
                               departure_date=FAR, today=TODAY)
    assert b["selected_passport"] == "JP", b
    assert b["kind"] == "VISA_FREE" and b["fee_cents"] == 0, b
    assert b["passports_considered"] == ["IN", "JP"]


def test_best_passport_prefers_visa_free_india():
    # IN: US passport is EVISA (fee), the IN passport is VISA_FREE. Best = IN.
    b = gate_leg_best_passport(dest_country="IN", nationalities=["US", "IN"],
                               departure_date=FAR, today=TODAY)
    assert b["selected_passport"] == "IN" and b["kind"] == "VISA_FREE", b


def test_best_passport_never_rescues_a_block_for_all_passports():
    # eVisa with INSUFFICIENT lead time -> every passport is unbookable in time. The BEST
    # verdict is still a block (fail-conservative), and the trip-level gate BLOCKS.
    soon = "2026-06-26"  # ~3 days out, below the eVisa processing lead
    b = gate_leg_best_passport(dest_country="ET", nationalities=["US", "SG"],
                               departure_date=soon, today=TODAY)
    assert b["allowed"] is False, b
    v = check_eligibility(legs=[{"dest_country": "ET", "departure_date": soon}],
                          nationality="US", nationalities=["US", "SG"], today=TODAY)
    assert v["verdict"] == GateVerdict.BLOCK and v["bookable"] is False, v


def test_single_nationality_path_is_byte_identical():
    # The opt-in must not perturb the existing single-nationality path: nationalities=None (or
    # absent) -> output identical to the prior call. This is what preserves var-0 for every
    # existing trip (no passports[] field == unchanged behavior).
    legs = [{"dest_country": "IN", "departure_date": FAR, "leg_id": "0"},
            {"dest_country": "TH", "departure_date": FAR, "leg_id": "1"}]
    base = check_eligibility(legs=legs, nationality="US", today=TODAY)
    same = check_eligibility(legs=legs, nationality="US", nationalities=None, today=TODAY)
    assert (json.dumps(base, sort_keys=True, default=str)
            == json.dumps(same, sort_keys=True, default=str))


def test_multi_passport_selects_per_leg():
    # Different legs can favor DIFFERENT passports: the CN leg picks JP (visa-free), the IN
    # leg picks IN (visa-free, home-country entry) — from the SAME passport set.
    # E1 fix: swapped from US to JP for the CN leg — US is NOT visa-free to China.
    legs = [{"dest_country": "CN", "departure_date": FAR, "leg_id": "cn"},
            {"dest_country": "IN", "departure_date": FAR, "leg_id": "in"}]
    v = check_eligibility(legs=legs, nationality="JP",
                          nationalities=["JP", "IN"], today=TODAY)
    by_leg = {pl["leg_id"]: pl for pl in v["per_leg"]}
    assert by_leg["cn"]["selected_passport"] == "JP" and by_leg["cn"]["kind"] == "VISA_FREE", by_leg["cn"]
    assert by_leg["in"]["selected_passport"] == "IN" and by_leg["in"]["kind"] == "VISA_FREE", by_leg["in"]
