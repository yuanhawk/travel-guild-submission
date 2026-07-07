"""
test_coverage_gap.py — #45 insurance coverage-GAP analyzer.

Light, targeted suite (the OSM sweep is running on a 3.4GB box — do NOT run the
full server / full suite). Covers:
  - var-0 byte-identical determinism (x50, json.dumps sort_keys=True),
  - war/terrorism EXCLUDED → gap + absolute-exclusion note (EXC-WAR-2 parity),
  - undeclared peril → UNDETERMINED gap "confirm", never covered,
  - sub-limit integer math (shortfall when insured > limit; none otherwise),
  - NO vendor names possible (scan table + recommendations vs catalog + brand regex),
  - opt-in no-op (orchestrator without user_policy → no coverage_gap key),
  - disclaimer present on every report,
  - out-of-Peril declaration → UNDETERMINED (dropped, never covered),
  - LLM firewall (DASHSCOPE_API_KEY unset → parse returns None; structured full).
"""

from __future__ import annotations

import copy
import json
import re

from utils import coverage_gap
from utils.coverage_gap import (
    DISCLAIMER,
    analyze_coverage_gap,
    parse_user_policy_text,
    validate_user_policy,
)
from agents.insurance_agent import CoverageStatus, policy_catalog_public


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_POLICY = {
    "policy_label": "my existing policy",
    "currency": "USD",
    "declarations": [
        {"peril_class": "medical", "stance": "sub_limited", "limit_cents": 5_000_000},
        {"peril_class": "travel_delay", "stance": "covered"},
        {"peril_class": "baggage_loss", "stance": "conditional",
         "condition_text": "only if reported within 24h"},
    ],
    "global_exclusions": ["war", "terrorism"],
}


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# var-0 determinism
# ---------------------------------------------------------------------------

def test_var0_byte_identical_x50():
    peril_set = ["medical", "war", "natural_disaster", "travel_delay", "terrorism",
                 "baggage_loss", "pandemic"]
    first = None
    for _ in range(50):
        rep = analyze_coverage_gap(
            peril_set=copy.deepcopy(peril_set),
            user_policy=copy.deepcopy(_POLICY),
            insured_trip_cost_cents=8_000_000,
        )
        blob = _canon(rep)
        if first is None:
            first = blob
        assert blob == first, "var-0 violated: report not byte-identical across runs"
    print("PASS: test_var0_byte_identical_x50")


def test_var0_order_independent_dedup_first_seen():
    # Duplicate perils de-dup first-seen; recommendations follow gap first-seen order.
    rep = analyze_coverage_gap(
        peril_set=["war", "medical", "war", "natural_disaster", "medical"],
        user_policy=_POLICY,
        insured_trip_cost_cents=8_000_000,
    )
    gap_perils = [g["peril_class"] for g in rep["gaps"]]
    assert gap_perils == ["war", "medical", "natural_disaster"], gap_perils
    print("PASS: test_var0_order_independent_dedup_first_seen")


# ---------------------------------------------------------------------------
# war / terrorism EXCLUDED → absolute-exclusion note (EXC-WAR-2 parity)
# ---------------------------------------------------------------------------

def test_war_terrorism_excluded_absolute_note():
    rep = analyze_coverage_gap(
        peril_set=["war", "terrorism", "civil_unrest"],
        user_policy=_POLICY,
        insured_trip_cost_cents=1_000_000,
    )
    by_peril = {g["peril_class"]: g for g in rep["gaps"]}
    for p in ("war", "terrorism"):
        assert by_peril[p]["status"] == CoverageStatus.EXCLUDED
        assert by_peril[p]["note"] == (
            "armed-conflict/terrorism exclusions are standard and absolute"
        )
    # civil_unrest is undeclared here → UNDETERMINED, but still carries the absolute
    # note ONLY when EXCLUDED. Undeclared → no note, conservative gap.
    assert by_peril["civil_unrest"]["status"] == CoverageStatus.UNDETERMINED
    assert "note" not in by_peril["civil_unrest"]
    print("PASS: test_war_terrorism_excluded_absolute_note")


def test_civil_unrest_excluded_gets_absolute_note():
    pol = {**_POLICY, "global_exclusions": ["civil_unrest"]}
    rep = analyze_coverage_gap(peril_set=["civil_unrest"], user_policy=pol)
    g = rep["gaps"][0]
    assert g["status"] == CoverageStatus.EXCLUDED
    assert g["note"] == "armed-conflict/terrorism exclusions are standard and absolute"
    print("PASS: test_civil_unrest_excluded_gets_absolute_note")


# ---------------------------------------------------------------------------
# undeclared peril → UNDETERMINED gap, never covered
# ---------------------------------------------------------------------------

def test_undeclared_peril_undetermined_never_covered():
    rep = analyze_coverage_gap(
        peril_set=["natural_disaster", "pandemic"],
        user_policy=_POLICY,
    )
    by_peril = {g["peril_class"]: g for g in rep["gaps"]}
    for p in ("natural_disaster", "pandemic"):
        assert by_peril[p]["status"] == CoverageStatus.UNDETERMINED
        assert "confirm with your insurer" in by_peril[p]["why_not_covered"]
        assert p not in rep["covered"]
    print("PASS: test_undeclared_peril_undetermined_never_covered")


def test_covered_peril_lands_in_covered():
    rep = analyze_coverage_gap(peril_set=["travel_delay"], user_policy=_POLICY)
    assert rep["covered"] == ["travel_delay"]
    assert rep["gaps"] == []
    print("PASS: test_covered_peril_lands_in_covered")


# ---------------------------------------------------------------------------
# sub-limit integer math
# ---------------------------------------------------------------------------

def test_sublimit_shortfall_when_insured_exceeds_limit():
    rep = analyze_coverage_gap(
        peril_set=["medical"],
        user_policy=_POLICY,
        insured_trip_cost_cents=8_000_000,
    )
    g = rep["gaps"][0]
    assert g["status"] == CoverageStatus.SUB_LIMITED
    assert g["shortfall_cents"] == 3_000_000
    assert isinstance(g["shortfall_cents"], int)
    print("PASS: test_sublimit_shortfall_when_insured_exceeds_limit")


def test_sublimit_no_shortfall_when_insured_below_limit():
    rep = analyze_coverage_gap(
        peril_set=["medical"],
        user_policy=_POLICY,
        insured_trip_cost_cents=3_000_000,  # below 5_000_000 limit
    )
    g = rep["gaps"][0]
    assert g["status"] == CoverageStatus.SUB_LIMITED
    assert "shortfall_cents" not in g
    assert "verify" in g["why_not_covered"]
    print("PASS: test_sublimit_no_shortfall_when_insured_below_limit")


def test_conditional_is_partial_gap():
    rep = analyze_coverage_gap(peril_set=["baggage_loss"], user_policy=_POLICY)
    g = rep["gaps"][0]
    assert g["status"] == CoverageStatus.CONDITIONAL
    assert g["condition_text"] == "only if reported within 24h"
    assert "baggage_loss" not in rep["covered"]
    print("PASS: test_conditional_is_partial_gap")


# ---------------------------------------------------------------------------
# precedence: EXCLUDE-first / most-conservative wins (NOT last-write-wins)
# ---------------------------------------------------------------------------

def test_conflicting_declarations_exclude_first_wins():
    pol = {
        "policy_label": "conflict",
        "currency": "USD",
        "declarations": [
            {"peril_class": "medical", "stance": "covered"},
            {"peril_class": "medical", "stance": "excluded"},  # later, but EXCLUDE wins
        ],
        "global_exclusions": [],
    }
    rep = analyze_coverage_gap(peril_set=["medical"], user_policy=pol)
    assert rep["gaps"][0]["status"] == CoverageStatus.EXCLUDED, "EXCLUDE-first must win"
    assert "medical" not in rep["covered"]
    # Reverse order → same outcome (order independence).
    pol2 = copy.deepcopy(pol)
    pol2["declarations"].reverse()
    rep2 = analyze_coverage_gap(peril_set=["medical"], user_policy=pol2)
    assert rep2["gaps"][0]["status"] == CoverageStatus.EXCLUDED
    print("PASS: test_conflicting_declarations_exclude_first_wins")


# ---------------------------------------------------------------------------
# NO vendor / brand names possible
# ---------------------------------------------------------------------------

def test_no_vendor_or_brand_names():
    catalog = policy_catalog_public()
    forbidden = set()
    for row in catalog:
        forbidden.add(str(row["policy_id"]).lower())
        forbidden.add(str(row["name"]).lower())
    # Brand-ish regex: capitalized multiword product names / common brand tokens.
    brand_re = re.compile(
        r"\b(allianz|axa|aig|chubb|nib|cover[- ]?more|world ?nomads|insurer|plus|premier|"
        r"standard traveler|p-std|p-prem)\b",
        re.IGNORECASE,
    )

    # Collect every string the module can emit as a recommendation.
    phrases = list(coverage_gap._PERIL_TO_COVERAGE_TYPE.values())
    # Also exercise live recommendations across all perils.
    all_perils = list(coverage_gap._PERIL_TO_COVERAGE_TYPE.keys())
    rep = analyze_coverage_gap(
        peril_set=all_perils,
        user_policy={"policy_label": "x", "currency": "USD",
                     "declarations": [], "global_exclusions": []},
    )
    phrases.extend(rep["recommendations"])

    for phrase in phrases:
        low = phrase.lower()
        for f in forbidden:
            assert f not in low, f"vendor id/name {f!r} leaked into recommendation: {phrase!r}"
        assert not brand_re.search(phrase), f"brand-like token in recommendation: {phrase!r}"
    print("PASS: test_no_vendor_or_brand_names")


def test_war_recommendation_is_uninsurable_contingency():
    rep = analyze_coverage_gap(peril_set=["war"], user_policy=_POLICY)
    assert any("treat as uninsurable" in r for r in rep["recommendations"])
    print("PASS: test_war_recommendation_is_uninsurable_contingency")


# ---------------------------------------------------------------------------
# disclaimer present on EVERY report
# ---------------------------------------------------------------------------

def test_disclaimer_present_on_every_report():
    for ps in ([], ["medical"], ["war", "pandemic", "travel_delay"]):
        rep = analyze_coverage_gap(peril_set=ps, user_policy=_POLICY)
        assert rep["disclaimer"] == DISCLAIMER
        assert rep["source"] == "coverage_gap: user-declared policy cross-check (#45)"
    print("PASS: test_disclaimer_present_on_every_report")


# ---------------------------------------------------------------------------
# validators
# ---------------------------------------------------------------------------

def test_out_of_peril_declaration_dropped_undetermined():
    pol = {
        "policy_label": "bad-peril",
        "currency": "USD",
        "declarations": [
            {"peril_class": "alien_invasion", "stance": "covered"},  # not a Peril
        ],
        "global_exclusions": [],
    }
    policy, warnings = validate_user_policy(pol)
    assert policy["declarations"] == [], "out-of-set peril must be dropped"
    assert any("not in canonical Peril set" in w for w in warnings)
    # And in a report it surfaces as UNDETERMINED, never covered.
    rep = analyze_coverage_gap(peril_set=["alien_invasion"], user_policy=pol)
    assert rep["gaps"][0]["status"] == CoverageStatus.UNDETERMINED
    assert rep["covered"] == []
    print("PASS: test_out_of_peril_declaration_dropped_undetermined")


def test_non_usd_currency_rejected():
    pol = {**_POLICY, "currency": "EUR"}
    try:
        validate_user_policy(pol)
    except coverage_gap.PolicyValidationError as e:
        assert "USD only" in str(e)
    else:
        raise AssertionError("non-USD currency must be rejected (never mint FX)")
    print("PASS: test_non_usd_currency_rejected")


def test_limit_coerced_to_int():
    pol = {
        "policy_label": "coerce",
        "currency": "USD",
        "declarations": [{"peril_class": "medical", "stance": "sub_limited",
                          "limit_cents": "5000000"}],  # string → int
        "global_exclusions": [],
    }
    policy, _ = validate_user_policy(pol)
    assert policy["declarations"][0]["limit_cents"] == 5_000_000
    assert isinstance(policy["declarations"][0]["limit_cents"], int)
    print("PASS: test_limit_coerced_to_int")


# ---------------------------------------------------------------------------
# LLM firewall
# ---------------------------------------------------------------------------

def test_llm_parse_none_when_key_unset(monkeypatch):
    monkeypatch.setattr(coverage_gap, "DASHSCOPE_API_KEY", "")
    assert parse_user_policy_text("I have medical cover up to $50k") is None
    # The structured path still produces a FULL report regardless.
    rep = analyze_coverage_gap(peril_set=["medical"], user_policy=_POLICY)
    assert rep["disclaimer"] == DISCLAIMER
    assert rep["gaps"]  # medical is sub_limited → a gap
    print("PASS: test_llm_parse_none_when_key_unset")


def test_gap_algorithm_never_imports_httpx(monkeypatch):
    # Prove the deterministic path never reaches the network: poison httpx.
    import builtins
    real_import = builtins.__import__

    def _no_httpx(name, *a, **k):
        if name == "httpx":
            raise AssertionError("gap path must NOT import httpx")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_httpx)
    rep = analyze_coverage_gap(
        peril_set=["medical", "war"], user_policy=_POLICY,
        insured_trip_cost_cents=8_000_000,
    )
    assert rep["gaps"]
    print("PASS: test_gap_algorithm_never_imports_httpx")


# ---------------------------------------------------------------------------
# opt-in no-op (orchestrator without user_policy → no coverage_gap key)
# ---------------------------------------------------------------------------

def test_opt_in_no_op_no_user_policy():
    from starlette.testclient import TestClient
    from agents.insurance_agent import InsuranceAgent
    from orchestration.orchestrator import TravelOrchestrator

    insurance = InsuranceAgent()
    orch = TravelOrchestrator(insurance_client=TestClient(insurance.build_app()))

    risk_assessment = {"rollup": {"all_reason_codes": ["health_hazard"]}}
    base_result = {"outcome": "success", "total_cents": 8_000_000}

    # No user_policy → NOTHING runs; no coverage_gap key; result otherwise unchanged
    # except for the existing vendor insurance block (unchanged behavior).
    orch._user_policy = None
    res_none = orch._apply_insurance(copy.deepcopy(base_result), risk_assessment)
    assert "coverage_gap" not in res_none, "opt-in violated: key added without user_policy"

    # With a user_policy → the coverage_gap key IS present (feature active).
    orch._user_policy = copy.deepcopy(_POLICY)
    res_yes = orch._apply_insurance(copy.deepcopy(base_result), risk_assessment)
    assert "coverage_gap" in res_yes
    assert res_yes["coverage_gap"]["disclaimer"] == DISCLAIMER

    # Byte-identical: the no-policy result minus the always-present vendor block
    # has no coverage_gap, and the two runs differ ONLY by that key.
    a = {k: v for k, v in res_none.items() if k != "coverage_gap"}
    b = {k: v for k, v in res_yes.items() if k != "coverage_gap"}
    assert _canon(a) == _canon(b), "opt-in must add ONLY the coverage_gap key"
    print("PASS: test_opt_in_no_op_no_user_policy")


def test_coverage_gap_handler_crosswalks_reason_codes():
    import asyncio
    from agents.insurance_agent import InsuranceAgent

    insurance = InsuranceAgent()
    msg = {"parts": [{"kind": "data", "data": {
        "risk_reason_codes": ["armed_conflict"],   # → war peril
        "user_policy": _POLICY,
        "insured_trip_cost_cents": 1_000_000,
    }}]}
    artifact = asyncio.run(insurance._coverage_gap_handler(msg, {}))
    data = artifact["parts"][0]["data"]
    perils = [g["peril_class"] for g in data["gaps"]]
    assert "war" in perils, "armed_conflict reason-code must crosswalk to war peril"
    print("PASS: test_coverage_gap_handler_crosswalks_reason_codes")


if __name__ == "__main__":
    import sys
    mod = sys.modules[__name__]
    for name in sorted(dir(mod)):
        if name.startswith("test_"):
            fn = getattr(mod, name)
            try:
                import inspect
                if "monkeypatch" in inspect.signature(fn).parameters:
                    continue  # needs pytest fixture
            except (TypeError, ValueError):
                pass
            fn()
