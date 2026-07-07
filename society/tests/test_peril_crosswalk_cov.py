"""
test_peril_crosswalk_cov.py — Comprehensive coverage of peril_crosswalk.py.

This module provides exhaustive deterministic testing of the Risk→Insurance peril
crosswalk (§19.1, §19.5). The crosswalk is a pure dict lookup with NO variance;
every RiskReasonCode has exactly one Peril mapping, asserted by self-check at
module load and validated here.

All tests are deterministic (var-0): no LLM, no network, no random seeds.
Determinism is guaranteed by the closed RiskReasonCode and Peril sets.

Coverage goals:
  - Every reason-code individually mapped + canonical peril validation
  - All crosswalk invariants (total, deterministic, no out-of-set codes/perils)
  - List path: de-duplication, stable order, empty/single-element edge cases
  - Error handling: KeyError on invalid reason-codes, both unit and list paths
  - Module-load self-check exercised indirectly (import validates the state)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from core import contracts as C
from utils import peril_crosswalk as px


# ===========================================================================
# Individual Reason-Code → Peril Mappings (Deterministic)
# ===========================================================================


def test_civil_unrest_maps_to_civil_unrest_peril():
    """CIVIL_UNREST reason-code must map to the canonical CIVIL_UNREST peril (DC0)."""
    peril = px.risk_reason_to_peril("civil_unrest")
    assert peril == C.Peril.CIVIL_UNREST.value
    assert peril == "civil_unrest"
    assert C.is_valid_peril(peril)


def test_armed_conflict_maps_to_war_peril():
    """ARMED_CONFLICT reason-code must map to the canonical WAR peril (DC0)."""
    peril = px.risk_reason_to_peril("armed_conflict")
    assert peril == C.Peril.WAR.value
    assert peril == "war"
    assert C.is_valid_peril(peril)


def test_terrorism_maps_to_terrorism_peril():
    """TERRORISM reason-code must map to the canonical TERRORISM peril."""
    peril = px.risk_reason_to_peril("terrorism")
    assert peril == C.Peril.TERRORISM.value
    assert peril == "terrorism"
    assert C.is_valid_peril(peril)


def test_natural_disaster_maps_to_natural_disaster_peril():
    """NATURAL_DISASTER reason-code must map to the canonical NATURAL_DISASTER peril."""
    peril = px.risk_reason_to_peril("natural_disaster")
    assert peril == C.Peril.NATURAL_DISASTER.value
    assert peril == "natural_disaster"
    assert C.is_valid_peril(peril)


def test_flood_maps_to_natural_disaster_peril():
    """FLOOD reason-code must map to NATURAL_DISASTER peril (consolidation)."""
    peril = px.risk_reason_to_peril("flood")
    assert peril == C.Peril.NATURAL_DISASTER.value
    assert peril == "natural_disaster"
    assert C.is_valid_peril(peril)


def test_disease_outbreak_maps_to_pandemic_peril():
    """DISEASE_OUTBREAK reason-code must map to the canonical PANDEMIC peril."""
    peril = px.risk_reason_to_peril("disease_outbreak")
    assert peril == C.Peril.PANDEMIC.value
    assert peril == "pandemic"
    assert C.is_valid_peril(peril)


def test_crime_maps_to_personal_liability_peril():
    """CRIME reason-code must map to PERSONAL_LIABILITY peril (proxy)."""
    peril = px.risk_reason_to_peril("crime")
    assert peril == C.Peril.PERSONAL_LIABILITY.value
    assert peril == "personal_liability"
    assert C.is_valid_peril(peril)


def test_transport_disruption_maps_to_travel_delay_peril():
    """TRANSPORT_DISRUPTION reason-code must map to TRAVEL_DELAY peril."""
    peril = px.risk_reason_to_peril("transport_disruption")
    assert peril == C.Peril.TRAVEL_DELAY.value
    assert peril == "travel_delay"
    assert C.is_valid_peril(peril)


def test_health_hazard_maps_to_medical_peril():
    """HEALTH_HAZARD reason-code must map to the canonical MEDICAL peril."""
    peril = px.risk_reason_to_peril("health_hazard")
    assert peril == C.Peril.MEDICAL.value
    assert peril == "medical"
    assert C.is_valid_peril(peril)


def test_advisory_elevated_maps_to_personal_liability_not_war():
    """
    ADVISORY_ELEVATED (generic elevated advisory, no specific cause) must map
    to PERSONAL_LIABILITY proxy, NEVER WAR. This is a critical D5 invariant:
    only ARMED_CONFLICT maps to WAR; generic elevation does not inherit war
    exclusion.
    """
    peril = px.risk_reason_to_peril("advisory_elevated")
    assert peril == C.Peril.PERSONAL_LIABILITY.value
    assert peril == "personal_liability"
    assert peril != C.Peril.WAR.value
    assert C.is_valid_peril(peril)


# ===========================================================================
# Deterministic Single-Lookup (Idempotency)
# ===========================================================================


def test_risk_reason_to_peril_deterministic():
    """The mapping is pure: same input always yields same output (variance-0)."""
    code = "terrorism"
    results = [px.risk_reason_to_peril(code) for _ in range(10)]
    assert len(set(results)) == 1
    assert all(r == "terrorism" for r in results)


def test_all_reason_codes_yield_deterministic_results():
    """Every RiskReasonCode yields the same peril on repeated calls (variance-0)."""
    for reason_code in C.RiskReasonCode:
        perils = [px.risk_reason_to_peril(reason_code.value) for _ in range(3)]
        assert len(set(perils)) == 1, (
            f"{reason_code.value} drifted across runs — NOT variance-0"
        )


# ===========================================================================
# Error Handling — Invalid Reason-Codes Rejected
# ===========================================================================


def test_invalid_reason_code_raises_key_error():
    """An unknown reason-code must raise KeyError (contract violation, not input)."""
    try:
        px.risk_reason_to_peril("not_a_code")
        assert False, "expected KeyError on invalid reason-code"
    except KeyError as e:
        assert "not_a_code" in str(e)
        assert "valid" in str(e).lower()


def test_invalid_reason_code_error_message_lists_valid_set():
    """The KeyError message must list the closed set of valid codes."""
    try:
        px.risk_reason_to_peril("fake_reason")
        assert False, "expected KeyError"
    except KeyError as e:
        msg = str(e)
        # Must mention closed set and valid RiskReasonCode values
        assert "closed set" in msg.lower() or "valid" in msg.lower()


def test_empty_string_reason_code_raises_key_error():
    """Empty string is not a valid reason-code."""
    try:
        px.risk_reason_to_peril("")
        assert False, "expected KeyError on empty string"
    except KeyError:
        pass


def test_none_reason_code_raises_key_error():
    """None is not a valid reason-code."""
    try:
        px.risk_reason_to_peril(None)
        assert False, "expected KeyError or TypeError on None"
    except (KeyError, TypeError):
        pass


# ===========================================================================
# List Path: risk_reasons_to_perils() — De-Duplication + Stable Order
# ===========================================================================


def test_single_element_list():
    """A one-element reason-code list yields a one-element peril list."""
    result = px.risk_reasons_to_perils(["terrorism"])
    assert result == ["terrorism"]
    assert len(result) == 1


def test_empty_list():
    """An empty reason-code list yields an empty peril list."""
    result = px.risk_reasons_to_perils([])
    assert result == []
    assert len(result) == 0


def test_no_duplicates_in_list_result():
    """Duplicate reason-codes in input are de-duplicated in output."""
    result = px.risk_reasons_to_perils([
        "civil_unrest",
        "civil_unrest",
        "terrorism"
    ])
    assert result == ["civil_unrest", "terrorism"]
    assert len(result) == 2


def test_dedup_by_first_appearance_order():
    """
    De-duplication preserves FIRST APPEARANCE order (not input order).
    This ensures stable, deterministic output across runs.
    """
    result = px.risk_reasons_to_perils([
        "civil_unrest",
        "armed_conflict",
        "civil_unrest",  # duplicate of first
        "terrorism"
    ])
    assert result == ["civil_unrest", "war", "terrorism"]
    # civil_unrest first, war second, terrorism last (in order of first appearance)


def test_consolidation_flood_and_natural_disaster_both_to_natural_disaster():
    """
    FLOOD and NATURAL_DISASTER both map to NATURAL_DISASTER peril.
    When both appear in a reason-code list, output should have exactly one
    NATURAL_DISASTER entry (first appearance order).
    """
    result = px.risk_reasons_to_perils([
        "natural_disaster",
        "flood",
        "natural_disaster"
    ])
    assert result == ["natural_disaster"]
    assert len(result) == 1


def test_consolidation_crime_and_advisory_elevated_both_to_personal_liability():
    """
    CRIME and ADVISORY_ELEVATED both map to PERSONAL_LIABILITY.
    When both appear, output should have exactly one PERSONAL_LIABILITY
    (first appearance order).
    """
    result = px.risk_reasons_to_perils([
        "crime",
        "advisory_elevated",
        "crime"
    ])
    assert result == ["personal_liability"]
    assert len(result) == 1


def test_list_all_9_reason_codes():
    """All 9 RiskReasonCode values in a single list yield their perils, de-duped."""
    all_9 = [
        "civil_unrest",
        "armed_conflict",
        "terrorism",
        "natural_disaster",
        "flood",
        "disease_outbreak",
        "crime",
        "transport_disruption",
        "health_hazard",
    ]
    result = px.risk_reasons_to_perils(all_9)
    # Expect: civil_unrest, war, terrorism, natural_disaster, pandemic,
    #         personal_liability, travel_delay, medical
    # (flood→natural_disaster = dup, crime→personal_liability, advisory_elevated not in list)
    expected = [
        "civil_unrest",      # civil_unrest
        "war",               # armed_conflict
        "terrorism",         # terrorism
        "natural_disaster",  # natural_disaster (flood is dup)
        "pandemic",          # disease_outbreak
        "personal_liability", # crime
        "travel_delay",      # transport_disruption
        "medical",           # health_hazard
    ]
    assert result == expected, f"got {result}, expected {expected}"


def test_list_with_advisory_elevated():
    """advisory_elevated must map to PERSONAL_LIABILITY in list context."""
    result = px.risk_reasons_to_perils(["advisory_elevated"])
    assert result == ["personal_liability"]


def test_list_order_preserves_first_appearance():
    """
    First-appearance order determines output order (deterministic).
    Two different orderings with the same codes yield different ordered results.
    """
    a = px.risk_reasons_to_perils(["civil_unrest", "terrorism", "health_hazard"])
    b = px.risk_reasons_to_perils(["terrorism", "civil_unrest", "health_hazard"])
    # a: civil_unrest (first), terrorism, medical
    # b: terrorism (first), civil_unrest, medical
    # Different output order because different first-appearance order.
    assert a == ["civil_unrest", "terrorism", "medical"]
    assert b == ["terrorism", "civil_unrest", "medical"]
    assert a != b


def test_list_invalid_reason_code_raises_key_error():
    """An invalid reason-code in the list must raise KeyError (not silent skip)."""
    try:
        px.risk_reasons_to_perils(["civil_unrest", "not_a_code", "terrorism"])
        assert False, "expected KeyError on invalid reason-code in list"
    except KeyError as e:
        assert "not_a_code" in str(e)


def test_list_with_multiple_duplicates():
    """Multiple duplicate reason-codes de-dup to single peril entry."""
    result = px.risk_reasons_to_perils([
        "terrorism",
        "terrorism",
        "terrorism",
        "civil_unrest",
        "terrorism"
    ])
    assert result == ["terrorism", "civil_unrest"]


# ===========================================================================
# Crosswalk Table Inspection
# ===========================================================================


def test_crosswalk_table_returns_copy():
    """crosswalk_table() must return a copy (not the internal dict)."""
    t1 = px.crosswalk_table()
    t2 = px.crosswalk_table()
    assert t1 == t2
    # Mutating t1 must not affect t2 or subsequent calls
    t1["mutated_key"] = "mutated_value"
    t3 = px.crosswalk_table()
    assert "mutated_key" not in t3


def test_crosswalk_table_all_values_canonical():
    """Every value in the crosswalk table must be a canonical Peril."""
    table = px.crosswalk_table()
    for reason_code, peril in table.items():
        assert C.is_valid_risk_reason(reason_code), (
            f"table key {reason_code!r} is not a valid RiskReasonCode"
        )
        assert C.is_valid_peril(peril), (
            f"table value {peril!r} (for {reason_code}) is not a canonical Peril"
        )


def test_crosswalk_table_is_total_over_reason_codes():
    """The crosswalk table must include every RiskReasonCode exactly once."""
    table = px.crosswalk_table()
    reason_codes = {r.value for r in C.RiskReasonCode}
    assert set(table.keys()) == reason_codes


# ===========================================================================
# Module-Load Self-Check (Import-Time Validation)
# ===========================================================================


def test_module_load_self_check_executed():
    """
    The _assert_total_and_valid() is called at module load (line 84).
    If the crosswalk is malformed, the import would fail. This test proves
    the module loaded successfully with a valid crosswalk.
    """
    # If we reach here, the module loaded (import succeeded).
    table = px.crosswalk_table()
    # Prove the invariants that _assert_total_and_valid checks:
    # 1. No missing reason-codes
    reason_codes = {r.value for r in C.RiskReasonCode}
    missing = reason_codes - set(table.keys())
    assert not missing, f"Missing reason-codes (should have failed at import): {missing}"
    # 2. No extra/unknown reason-codes in the map
    extra = set(table.keys()) - reason_codes
    assert not extra, f"Extra reason-codes in map (should have failed at import): {extra}"
    # 3. All perils are canonical
    for peril in table.values():
        assert C.is_valid_peril(peril), (
            f"Non-canonical peril in map (should have failed at import): {peril!r}"
        )


# ===========================================================================
# Symmetry: Risk→Peril translation across insurance agent integration
# ===========================================================================


def test_risk_reason_codes_are_valid_for_each_mapping():
    """Every reason-code in the map is a valid RiskReasonCode."""
    table = px.crosswalk_table()
    for reason_code in table.keys():
        assert C.is_valid_risk_reason(reason_code), (
            f"Crosswalk maps invalid reason-code {reason_code!r}"
        )


def test_all_peril_values_in_crosswalk_are_from_closed_set():
    """Every peril in the crosswalk is from the canonical closed Peril set."""
    table = px.crosswalk_table()
    all_canonical_perils = set(C.all_perils())
    for peril in table.values():
        assert peril in all_canonical_perils, (
            f"Crosswalk maps to non-canonical peril {peril!r}"
        )


# ===========================================================================
# Variance-0 Guarantee (Determinism Over Multiple Runs)
# ===========================================================================


def test_list_path_variance_zero_across_n_runs():
    """
    The list path must be deterministic (variance-0) over N runs.
    Input order of de-duplication is first-appearance; output is stable.
    """
    test_input = ["civil_unrest", "terrorism", "flood", "civil_unrest", "terrorism"]
    results = [px.risk_reasons_to_perils(test_input) for _ in range(10)]
    # All 10 runs must produce identical output
    assert len(set(tuple(r) for r in results)) == 1, "list path drifted across runs"


def test_single_reason_code_variance_zero():
    """Single-call mappings are deterministic across many runs (variance-0)."""
    results = [px.risk_reason_to_peril("transport_disruption") for _ in range(20)]
    assert len(set(results)) == 1
    assert all(r == "travel_delay" for r in results)


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call(["python3", "-m", "pytest", __file__, "-v"]))
