"""
test_insurance_agent.py — Unit + invariant tests for the Insurance specialist (L2 / DC0).

CI-safe: no LLM, no network, no DashScope. Uses the deterministic clause-matcher
directly and the in-process Starlette ASGI TestClient for the A2A surface.

Design contract: the internal design spec §17.1, §17.3 (DC0), §19; the
"Insurance" blueprint test_plan (unit_tests + invariants + closed_set_fallback).

Coverage map (blueprint test_plan):
  unit:
    1.  civil_unrest + P-STD-01 → EXCLUDED via EXC-WAR-2 (the DC0 core)
    2.  travel_delay + P-STD-01 → COVERED via COV-DELAY-1
    3.  medical + P-STD-01 → SUB_LIMITED, sub_limit_cents=5000000 (not COVERED)
    4.  unmatched peril → UNDETERMINED (never silent COVERED)
    5.  line-item emission: premium_cents from seed, integer cents, no LLM number
    6.  explain_exclusion: factual claims derive from matched clause text
    7.  source-unreachable → SEEDED_FALLBACK, never invents terms
    8.  multi-peril: excluded summary has civil_unrest AND covered has travel_delay
  invariants (cascade-style regression — FAIL if violated):
    I1. NEVER-COVER-EXCLUDED: any peril with a matching EXCLUDE clause → EXCLUDED
        + present in excluded_perils_summary, across ALL closed-set perils.
    I2. NO-LLM-NUMBERS: byte-identical numeric output across N runs (variance-0).
    I3. DETERMINISTIC-STATUS: status is a pure function of (peril, clause set).
    I4. PROVENANCE-PRESENT: premium_provenance + per-clause provenance present + valid.
  closed_set_fallback:
    F1. With the LLM disabled, the full CoverageAssessment is correct → the LLM
        edge is purely cosmetic and clamped.
"""

from __future__ import annotations

import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(__file__))

from starlette.testclient import TestClient

from core import contracts as C
from agents import insurance_agent as ins
from agents.insurance_agent import (
    CoverageStatus,
    DEFAULT_POLICY_ID,
    InsuranceAgent,
    assess_coverage,
    compute_premium_cents,
    explain_exclusion,
    premium_line_item,
    validate_rationale,
    _POLICY_CLAUSES,
    _POLICY_CATALOG,
)


# ===========================================================================
# Unit tests — the deterministic clause-matcher
# ===========================================================================

def test_civil_unrest_excluded_dc0_core():
    # D5: EXC-WAR-2 now keys to Peril.WAR only. Civil_unrest is excluded via EXC-UNREST-1.
    a = assess_coverage(peril_set=["civil_unrest"], policy_id="P-STD-01")
    rec = a["per_peril"][0]
    assert rec["status"] == CoverageStatus.EXCLUDED
    assert "EXC-UNREST-1" in rec["matched_clause_ids"]
    assert a["excluded_perils_summary"][0]["peril_class"] == "civil_unrest"


def test_travel_delay_covered():
    a = assess_coverage(peril_set=["travel_delay"], policy_id="P-STD-01")
    rec = a["per_peril"][0]
    assert rec["status"] == CoverageStatus.COVERED
    assert rec["matched_clause_ids"] == ["COV-DELAY-1"]


def test_medical_sub_limited_not_covered():
    a = assess_coverage(peril_set=["medical"], policy_id="P-STD-01")
    rec = a["per_peril"][0]
    assert rec["status"] == CoverageStatus.SUB_LIMITED
    assert rec["sub_limit_cents"] == 5_000_000
    assert rec["status"] != CoverageStatus.COVERED


def test_adventure_conditional():
    a = assess_coverage(peril_set=["adventure_activity"], policy_id="P-STD-01")
    rec = a["per_peril"][0]
    assert rec["status"] == CoverageStatus.CONDITIONAL
    assert rec["condition"]


def test_unmatched_peril_undetermined_never_covered():
    # supplier_insolvency is a valid canonical peril with NO clause in P-STD-01.
    a = assess_coverage(peril_set=["supplier_insolvency"], policy_id="P-STD-01")
    rec = a["per_peril"][0]
    assert rec["status"] == CoverageStatus.UNDETERMINED
    assert rec["status"] != CoverageStatus.COVERED
    assert rec["matched_clause_ids"] == []


def test_out_of_closed_set_peril_undetermined():
    # A peril OUTSIDE contracts.Peril must degrade to UNDETERMINED, never COVERED.
    a = assess_coverage(peril_set=["volcanic_ash"], policy_id="P-STD-01")
    rec = a["per_peril"][0]
    assert rec["status"] == CoverageStatus.UNDETERMINED


def test_premium_no_llm_numbers_integer_cents():
    # premium = base 4500 + round(120000 * 6.0%) = 4500 + 7200 = 11700
    assert compute_premium_cents("P-STD-01", 120000) == 11700
    a = assess_coverage(peril_set=["civil_unrest"], policy_id="P-STD-01",
                        insured_trip_cost_cents=120000)
    assert a["premium_cents"] == 11700
    assert isinstance(a["premium_cents"], int)
    # The premium money tag is a valid canonical contracts.make_money() shape.
    assert C.is_valid_money(a["premium_money"])
    assert a["premium_money"]["usd_cents"] == 11700


def test_line_item_emission_keyed_idempotent():
    a = assess_coverage(peril_set=["civil_unrest"], policy_id="P-STD-01",
                        insured_trip_cost_cents=120000)
    li = premium_line_item(a)
    assert li["kind"] == "premium"
    assert li["usd_cents"] == 11700
    assert li["hotel_id"] == "fee:insurance:premium:_package"
    assert C.is_valid_money(li["money"])


def test_explain_exclusion_derives_from_clause_text():
    # D5: civil_unrest is excluded via EXC-UNREST-1 (not EXC-WAR-2 which is now WAR-only).
    a = assess_coverage(peril_set=["civil_unrest", "travel_delay"], policy_id="P-STD-01")
    e = explain_exclusion(a, "civil_unrest", region="Region-X")
    # The headline must quote the actual seeded clause text (no fabricated facts).
    clause = next(c for c in _POLICY_CLAUSES if c["clause_id"] == "EXC-UNREST-1")
    assert clause["clause_text"] in e["headline"]
    assert "EXC-UNREST-1" in e["headline"]
    assert e["status"] == CoverageStatus.EXCLUDED
    # Contrast beat: covered for delay, NOT for the conflict.
    assert "covered for travel delay" in e["headline"]
    assert "NOT for the civil unrest" in e["headline"]
    assert "Region-X" in e["headline"]


def test_source_unreachable_seeded_fallback_never_invents():
    pid, freshness = ins.fetch_policy_terms("P-STD-01", simulate_unreachable=True)
    assert freshness == "SEEDED_FALLBACK"
    a = assess_coverage(peril_set=["civil_unrest"], policy_id="P-STD-01",
                        source_freshness=freshness)
    assert a["source_freshness"] == "SEEDED_FALLBACK"
    # Terms are the SEEDED ones, not fabricated — civil_unrest still EXCLUDED.
    assert a["per_peril"][0]["status"] == CoverageStatus.EXCLUDED


def test_multi_peril_exclusion_not_masked_by_coverage():
    a = assess_coverage(peril_set=["civil_unrest", "travel_delay"], policy_id="P-STD-01")
    excluded = [x["peril_class"] for x in a["excluded_perils_summary"]]
    assert "civil_unrest" in excluded
    assert "travel_delay" in a["covered_perils"]


# ===========================================================================
# Invariants — cascade-style regression (FAIL if violated)
# ===========================================================================

def test_invariant_never_cover_excluded_all_perils():
    """
    I1 (CI-enforced): for EVERY policy, every peril with a matching EXCLUDE clause
    MUST be EXCLUDED and MUST appear in excluded_perils_summary — the society can
    never report 'covered' for an excluded peril. This is the DC0 guarantee.
    """
    for policy_id in _POLICY_CATALOG:
        excluded_perils = {
            c["peril_class"] for c in _POLICY_CLAUSES
            if c["policy_id"] == policy_id and c["clause_type"] == ins.ClauseType.EXCLUDE
        }
        a = assess_coverage(peril_set=C.all_perils(), policy_id=policy_id,
                            insured_trip_cost_cents=100000)
        summary = {x["peril_class"] for x in a["excluded_perils_summary"]}
        for p in excluded_perils:
            rec = next(r for r in a["per_peril"] if r["peril_class"] == p)
            assert rec["status"] == CoverageStatus.EXCLUDED, (
                f"{policy_id}/{p} EXCLUDE clause but status={rec['status']}")
            assert p in summary, f"{policy_id}/{p} excluded but missing from summary"
        # And NO excluded peril is ever reported COVERED.
        for rec in a["per_peril"]:
            if rec["peril_class"] in excluded_perils:
                assert rec["status"] != CoverageStatus.COVERED


def test_invariant_no_llm_numbers_variance_zero_across_n():
    """I2: byte-identical numeric output across N runs (NO-LLM-NUMBERS, var-0)."""
    perils = C.all_perils()
    sigs = []
    for _ in range(20):
        a = assess_coverage(peril_set=list(perils), policy_id="P-STD-01",
                            insured_trip_cost_cents=120000)
        sigs.append((a["premium_cents"], json.dumps(
            [(r["peril_class"], r["status"], r["matched_clause_ids"], r["sub_limit_cents"])
             for r in a["per_peril"]], sort_keys=True)))
    assert len(set(sigs)) == 1, "assessment drifted across runs — NOT variance-0"


def test_invariant_deterministic_status_pure_function():
    """I3: status depends ONLY on (peril, clause set) — order-independent."""
    a1 = assess_coverage(peril_set=["civil_unrest", "travel_delay", "medical"], policy_id="P-STD-01")
    a2 = assess_coverage(peril_set=["medical", "civil_unrest", "travel_delay"], policy_id="P-STD-01")
    status1 = {r["peril_class"]: r["status"] for r in a1["per_peril"]}
    status2 = {r["peril_class"]: r["status"] for r in a2["per_peril"]}
    assert status1 == status2


def test_invariant_provenance_present_and_valid():
    """I4: premium_provenance + every matched clause's provenance present + valid."""
    a = assess_coverage(peril_set=["civil_unrest", "medical"], policy_id="P-STD-01",
                        insured_trip_cost_cents=120000)
    assert C.is_valid_provenance(a["premium_provenance"])
    for rec in a["per_peril"]:
        for ct in ins.matched_clause_text(rec["matched_clause_ids"]):
            assert ct["provenance"], "matched clause missing provenance"
            assert ct["source_url"], "matched clause missing source_url"


# ===========================================================================
# Closed-set fallback — proves the LLM is cosmetic (LLM fully disabled)
# ===========================================================================

def test_closed_set_fallback_llm_disabled(monkeypatch):
    """
    F1: with the LLM-rationale writer fully disabled (no key), the deterministic
    clause-matcher produces the full, correct CoverageAssessment — civil_unrest→
    EXCLUDED, travel_delay→COVERED, medical→SUB_LIMITED, unknown→UNDETERMINED.
    Proves the LLM edge is purely cosmetic and clamped.
    """
    monkeypatch.setattr(ins, "DASHSCOPE_API_KEY", "")
    a = assess_coverage(
        peril_set=["civil_unrest", "travel_delay", "medical", "supplier_insolvency"],
        policy_id="P-STD-01", insured_trip_cost_cents=120000)
    st = {r["peril_class"]: r["status"] for r in a["per_peril"]}
    assert st["civil_unrest"] == CoverageStatus.EXCLUDED
    assert st["travel_delay"] == CoverageStatus.COVERED
    assert st["medical"] == CoverageStatus.SUB_LIMITED
    assert st["supplier_insolvency"] == CoverageStatus.UNDETERMINED
    # explain_exclusion (deterministic) still produces the headline with no LLM.
    # D5: civil_unrest is excluded via EXC-UNREST-1 (EXC-WAR-2 now keys to WAR only).
    e = explain_exclusion(a, "civil_unrest")
    assert e["status"] == CoverageStatus.EXCLUDED
    assert "EXC-UNREST-1" in e["headline"]


def test_rationale_validator_rejects_softened_exclusion():
    # D5: civil_unrest is excluded via EXC-UNREST-1. Use that clause for the civil_unrest test.
    clauses = ins.matched_clause_text(["EXC-UNREST-1"])
    # A rationale that softens EXCLUDED into 'covered' must be rejected.
    assert validate_rationale(
        "Civil unrest is covered under this policy.", CoverageStatus.EXCLUDED, clauses) is False
    # A faithful exclusion rationale passes.
    assert validate_rationale(
        "Claims arising from civil unrest are not payable under this policy.",
        CoverageStatus.EXCLUDED, clauses) is True


def test_rationale_validator_rejects_fabricated_numbers():
    clauses = ins.matched_clause_text(["EXC-WAR-2"])  # clause text has no digits (same text)
    assert validate_rationale(
        "This excludes claims up to 99999 dollars.", CoverageStatus.EXCLUDED, clauses) is False


# ===========================================================================
# A2A surface — in-process ASGI TestClient
# ===========================================================================

def _send(client: TestClient, skill_id: str, data: dict) -> dict:
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "data", "data": data}],
            "metadata": {"skillId": skill_id},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200, resp.text
    task = resp.json()["result"]
    assert task["status"]["state"] == "completed", task
    return task["artifacts"][0]["parts"][0]["data"]


def test_a2a_card_and_skills():
    agent = InsuranceAgent(host="0.0.0.0", port=9105)
    client = TestClient(agent.build_app())
    card = client.get("/.well-known/agent-card.json").json()
    assert card["name"] == "insurance-agent"
    skill_ids = {s["id"] for s in card["skills"]}
    assert skill_ids == {
        "insurance.assess_coverage",
        "insurance.explain_exclusion",
        "insurance.coverage_gap",  # #45 — append-only coverage-gap analyzer
    }


def test_a2a_assess_coverage_dc0():
    agent = InsuranceAgent()
    client = TestClient(agent.build_app())
    data = _send(client, "insurance.assess_coverage", {
        "peril_set": ["civil_unrest", "travel_delay"],
        "policy_id": "P-STD-01", "insured_trip_cost_cents": 120000})
    excluded = [x["peril_class"] for x in data["excluded_perils_summary"]]
    assert "civil_unrest" in excluded
    assert "travel_delay" in data["covered_perils"]
    assert data["premium_cents"] == 11700
    assert data["line_item"]["kind"] == "premium"


def test_a2a_assess_via_risk_reason_codes_crosswalk():
    # Risk owns the SIGNAL; Insurance maps reason_codes→perils via the crosswalk.
    agent = InsuranceAgent()
    client = TestClient(agent.build_app())
    data = _send(client, "insurance.assess_coverage", {
        "risk_reason_codes": ["civil_unrest", "transport_disruption"],
        "policy_id": "P-STD-01", "insured_trip_cost_cents": 100000})
    st = {r["peril_class"]: r["status"] for r in data["per_peril"]}
    assert st["civil_unrest"] == CoverageStatus.EXCLUDED      # civil_unrest reason
    assert st["travel_delay"] == CoverageStatus.COVERED        # transport_disruption→travel_delay


def test_a2a_explain_exclusion_standalone():
    # D5: civil_unrest is excluded via EXC-UNREST-1 (EXC-WAR-2 now keys to WAR only).
    agent = InsuranceAgent()
    client = TestClient(agent.build_app())
    data = _send(client, "insurance.explain_exclusion", {
        "peril_class": "civil_unrest", "region": "the region"})
    assert data["status"] == CoverageStatus.EXCLUDED
    assert "EXC-UNREST-1" in data["headline"]
    assert data["rationale_source"] == "deterministic"


# ===========================================================================
# health_assessment wiring — P1 gap tests (GAP 1)
# ===========================================================================

def test_health_assessment_evacuation_true_injects_medevac():
    """top-level evacuation_recommended=True → medical_evacuation appears in per_peril."""
    r = ins.assess_coverage(
        peril_set=["medical"],
        insured_trip_cost_cents=50000,
        health_assessment={"evacuation_recommended": True},
    )
    peril_classes = [rec["peril_class"] for rec in r["per_peril"]]
    assert "medical_evacuation" in peril_classes, (
        f"medical_evacuation not injected when evacuation_recommended=True; got {peril_classes}"
    )


def test_health_assessment_evacuation_false_no_medevac():
    """top-level evacuation_recommended=False → medical_evacuation NOT injected."""
    r = ins.assess_coverage(
        peril_set=["medical"],
        insured_trip_cost_cents=50000,
        health_assessment={"evacuation_recommended": False},
    )
    peril_classes = [rec["peril_class"] for rec in r["per_peril"]]
    assert "medical_evacuation" not in peril_classes, (
        f"medical_evacuation spuriously injected when evacuation_recommended=False; got {peril_classes}"
    )


def test_health_assessment_per_destination_evacuation_true():
    """per_destination list path — at least one dest with evacuation_recommended=True → no crash, returns dict."""
    r = ins.assess_coverage(
        peril_set=[],
        insured_trip_cost_cents=50000,
        health_assessment={"per_destination": [{"place_key": "nepal", "evacuation_recommended": True}]},
    )
    assert isinstance(r, dict), f"expected dict result, got {type(r)}"
    # The per_destination path should also inject medical_evacuation.
    peril_classes = [rec["peril_class"] for rec in r["per_peril"]]
    assert "medical_evacuation" in peril_classes, (
        f"medical_evacuation not injected via per_destination path; got {peril_classes}"
    )


def test_health_assessment_none_no_spurious_medevac():
    """health_assessment=None (default) — must work and NOT spuriously inject medical_evacuation."""
    r = ins.assess_coverage(peril_set=["medical"], insured_trip_cost_cents=50000)
    assert isinstance(r, dict), "expected dict result"
    peril_classes = [rec["peril_class"] for rec in r["per_peril"]]
    assert "medical_evacuation" not in peril_classes, (
        f"medical_evacuation spuriously added when health_assessment is None; got {peril_classes}"
    )


# ===========================================================================
# Crosswalk all-9-reason-codes — every RiskReasonCode maps to the correct peril
# ===========================================================================

def test_crosswalk_all_9_reason_codes_produce_correct_perils():
    """
    GAP fill: the A2A crosswalk path exercises all 9 RiskReasonCode values.
    Each reason code must map to the deterministic peril prescribed by
    peril_crosswalk._RISK_TO_PERIL.  Coverage status is determined by P-STD-01
    clauses:

    P-STD-01 clauses (actual seeded data) after D5 fix:
      civil_unrest → EXCLUDE (EXC-UNREST-1)
      war → EXCLUDE (EXC-WAR-2)
      terrorism → EXCLUDE (EXC-WAR-2C)
      pandemic → EXCLUDE (EXC-PANDEMIC-1)
      travel_delay → COVER
      medical → SUBLIMIT
      adventure_activity → CONDITION
      trip_cancellation → COVER
      baggage_loss → COVER
      natural_disaster → no clause → UNDETERMINED
      personal_liability → no clause → UNDETERMINED

    Crosswalk mapping for all 9:
      CIVIL_UNREST → civil_unrest → EXCLUDED
      ARMED_CONFLICT → war → EXCLUDED
      TERRORISM → terrorism → EXCLUDED
      DISEASE_OUTBREAK → pandemic → EXCLUDED
      TRANSPORT_DISRUPTION → travel_delay → COVERED
      HEALTH_HAZARD → medical → SUB_LIMITED
      NATURAL_DISASTER → natural_disaster → UNDETERMINED (no P-STD-01 clause)
      FLOOD → natural_disaster → UNDETERMINED (de-dup, same peril as above)
      CRIME → personal_liability → UNDETERMINED (no P-STD-01 clause)
    """
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
    agent = InsuranceAgent()
    client = TestClient(agent.build_app())
    data = _send(client, "insurance.assess_coverage", {
        "risk_reason_codes": all_9,
        "policy_id": "P-STD-01",
        "insured_trip_cost_cents": 100000,
    })

    st = {r["peril_class"]: r["status"] for r in data["per_peril"]}

    # --- EXCLUDED perils (EXCLUDE always wins per _CLAUSE_PRECEDENCE) ---
    assert st["civil_unrest"] == CoverageStatus.EXCLUDED, (
        f"civil_unrest must be EXCLUDED; got {st.get('civil_unrest')!r}"
    )
    assert st["war"] == CoverageStatus.EXCLUDED, (
        f"war (armed_conflict→war) must be EXCLUDED via EXC-WAR-2; got {st.get('war')!r}"
    )
    assert st["terrorism"] == CoverageStatus.EXCLUDED, (
        f"terrorism must be EXCLUDED via EXC-WAR-2C; got {st.get('terrorism')!r}"
    )
    assert st["pandemic"] == CoverageStatus.EXCLUDED, (
        f"pandemic (disease_outbreak→pandemic) must be EXCLUDED via EXC-PANDEMIC-1; "
        f"got {st.get('pandemic')!r}"
    )

    # --- COVERED / SUB_LIMITED perils (P-STD-01 has explicit clauses) ---
    assert st["travel_delay"] == CoverageStatus.COVERED, (
        f"travel_delay (transport_disruption→travel_delay) must be COVERED; "
        f"got {st.get('travel_delay')!r}"
    )
    assert st["medical"] == CoverageStatus.SUB_LIMITED, (
        f"medical (health_hazard→medical) must be SUB_LIMITED; got {st.get('medical')!r}"
    )

    # --- UNDETERMINED perils (P-STD-01 has no clause for these) ---
    # natural_disaster: NATURAL_DISASTER and FLOOD both map here (de-dup)
    assert "natural_disaster" in st, (
        "natural_disaster must appear in per_peril (crosswalk: NATURAL_DISASTER and FLOOD both map here)"
    )
    assert st["natural_disaster"] == CoverageStatus.UNDETERMINED, (
        f"natural_disaster must be UNDETERMINED on P-STD-01 (no clause for it); "
        f"got {st.get('natural_disaster')!r}"
    )
    # personal_liability: CRIME maps here
    assert "personal_liability" in st, (
        "personal_liability must appear in per_peril (crosswalk: CRIME maps here)"
    )
    assert st["personal_liability"] == CoverageStatus.UNDETERMINED, (
        f"personal_liability must be UNDETERMINED on P-STD-01 (no clause for it); "
        f"got {st.get('personal_liability')!r}"
    )


def test_crosswalk_war_peril_excluded_unit():
    """Unit path: assess_coverage directly with peril_set=['war'] → EXCLUDED via EXC-WAR-2 (D5)."""
    r = ins.assess_coverage(
        peril_set=["war"],
        policy_id="P-STD-01",
        insured_trip_cost_cents=80000,
    )
    st = {rec["peril_class"]: rec["status"] for rec in r["per_peril"]}
    assert st["war"] == CoverageStatus.EXCLUDED, (
        f"war must be EXCLUDED via EXC-WAR-2; got {st.get('war')!r}"
    )
    # D5: EXC-WAR-2 now keys to Peril.WAR exclusively.
    war_rec = next(rec for rec in r["per_peril"] if rec["peril_class"] == "war")
    assert "EXC-WAR-2" in war_rec["matched_clause_ids"], (
        f"EXC-WAR-2 must match the war peril; matched={war_rec['matched_clause_ids']}"
    )


def test_crosswalk_terrorism_peril_excluded_unit():
    """Unit path: assess_coverage directly with peril_set=['terrorism'] → EXCLUDED."""
    r = ins.assess_coverage(
        peril_set=["terrorism"],
        policy_id="P-STD-01",
        insured_trip_cost_cents=80000,
    )
    st = {rec["peril_class"]: rec["status"] for rec in r["per_peril"]}
    assert st["terrorism"] == CoverageStatus.EXCLUDED, (
        f"terrorism must be EXCLUDED via EXC-WAR-2C; got {st.get('terrorism')!r}"
    )


# ===========================================================================
# Hardening-pass gap tests (D5 / #29 / #45 / #50)
# ===========================================================================

def test_d5_exc_war_2_keys_only_to_war_not_civil_unrest():
    """
    D5 #38: EXC-WAR-2 must key ONLY to Peril.WAR — never to CIVIL_UNREST, CRIME,
    or ADVISORY_ELEVATED.  Civil_unrest has its own dedicated exclusion clause
    (EXC-UNREST-1) so generic elevated-advisory perils (personal_liability,
    advisory_elevated) do NOT inadvertently inherit the war exclusion.
    """
    # 1. EXC-WAR-2 peril_class must be 'war'
    exc_war_2 = next(c for c in _POLICY_CLAUSES if c["clause_id"] == "EXC-WAR-2")
    assert exc_war_2["peril_class"] == "war", (
        f"D5: EXC-WAR-2 must key to Peril.WAR; got {exc_war_2['peril_class']!r}"
    )
    assert exc_war_2["peril_class"] != "civil_unrest", (
        "D5: EXC-WAR-2 must NOT key to civil_unrest"
    )
    # 2. civil_unrest exclusion must NOT be carried by EXC-WAR-2
    a = assess_coverage(peril_set=["civil_unrest"], policy_id="P-STD-01")
    civil_rec = a["per_peril"][0]
    assert "EXC-WAR-2" not in civil_rec["matched_clause_ids"], (
        f"D5: civil_unrest must not match EXC-WAR-2; got {civil_rec['matched_clause_ids']}"
    )
    assert civil_rec["status"] == CoverageStatus.EXCLUDED, (
        "civil_unrest must still be EXCLUDED (via EXC-UNREST-1)"
    )
    # 3. war exclusion IS carried by EXC-WAR-2
    a2 = assess_coverage(peril_set=["war"], policy_id="P-STD-01")
    war_rec = a2["per_peril"][0]
    assert "EXC-WAR-2" in war_rec["matched_clause_ids"], (
        f"D5: EXC-WAR-2 must appear in war matched_clause_ids; got {war_rec['matched_clause_ids']}"
    )
    # 4. personal_liability (CRIME/ADVISORY_ELEVATED proxy) is NOT excluded via EXC-WAR-2
    a3 = assess_coverage(peril_set=["personal_liability"], policy_id="P-STD-01")
    pl_rec = a3["per_peril"][0]
    assert pl_rec["status"] != CoverageStatus.EXCLUDED, (
        "personal_liability (crime/advisory_elevated proxy) must NOT be EXCLUDED by war clause"
    )
    assert "EXC-WAR-2" not in pl_rec["matched_clause_ids"]


def test_hardening_29_explain_exclusion_final_else_preserves_status():
    """
    #29: The final-else branch in explain_exclusion must preserve the authoritative
    status in the headline, not emit UNDETERMINED when the status is actually
    EXCLUDED/SUB_LIMITED/CONDITIONAL.

    We simulate the 'clauses unavailable' path by building an assessment dict
    with a matched_clause_id that does not exist in _POLICY_CLAUSES.
    """
    # Build a synthetic assessment with an EXCLUDED peril whose clause_id is unknown.
    synthetic_assessment = {
        "policy_id": "P-STD-01",
        "per_peril": [
            {
                "peril_class": "war",
                "status": CoverageStatus.EXCLUDED,
                "matched_clause_ids": ["NONEXISTENT-CLAUSE-ID"],
                "sub_limit_cents": None,
                "condition": None,
            }
        ],
        "covered_perils": [],
    }
    result = explain_exclusion(synthetic_assessment, "war")
    # The headline must preserve EXCLUDED — not say UNDETERMINED.
    assert "EXCLUDES" in result["headline"].upper() or "EXCLUDE" in result["headline"].upper(), (
        f"#29: headline must preserve EXCLUDED status when clauses are unavailable; "
        f"got: {result['headline']!r}"
    )
    assert "UNDETERMINED" not in result["headline"], (
        f"#29: headline must NOT say UNDETERMINED when status is EXCLUDED; "
        f"got: {result['headline']!r}"
    )
    assert result["status"] == CoverageStatus.EXCLUDED, (
        f"#29: status field must still be EXCLUDED; got {result['status']!r}"
    )


def test_hardening_45_integer_bps_premium_math():
    """
    #45: Premium must be computed via integer basis-point arithmetic (no float).
    trip_cost_bps replaces trip_cost_pct (float) in the policy catalog.
    Verify the exact integer-math result matches expectations.
    """
    # P-STD-01: base=4500, bps=600 (6%)
    # trip=120000: (120000 * 600 + 5000) // 10000 = 72005000 // 10000 = 7200
    # premium = 4500 + 7200 = 11700
    p = compute_premium_cents("P-STD-01", 120000)
    assert p == 11700, f"#45: P-STD-01 premium expected 11700 got {p}"
    assert isinstance(p, int), "#45: premium must be int (no float)"

    # P-PREM-02: base=12000, bps=950 (9.5%)
    # trip=120000: (120000 * 950 + 5000) // 10000 = 114005000 // 10000 = 11400
    # premium = 12000 + 11400 = 23400
    p2 = compute_premium_cents("P-PREM-02", 120000)
    assert p2 == 23400, f"#45: P-PREM-02 premium expected 23400 got {p2}"
    assert isinstance(p2, int), "#45: premium must be int (no float)"

    # Zero trip cost → only base premium
    p0 = compute_premium_cents("P-STD-01", 0)
    assert p0 == 4500, f"#45: zero trip cost should give base only; got {p0}"

    # trip_cost_bps must be an int in the catalog (no float)
    for pol in _POLICY_CATALOG.values():
        assert "trip_cost_bps" in pol, (
            f"#45: policy {pol['policy_id']} missing trip_cost_bps (must use bps not pct)"
        )
        assert isinstance(pol["trip_cost_bps"], int), (
            f"#45: policy {pol['policy_id']} trip_cost_bps must be int; "
            f"got {type(pol['trip_cost_bps'])}"
        )
        assert "trip_cost_pct" not in pol, (
            f"#45: policy {pol['policy_id']} still has trip_cost_pct float — must use bps"
        )


def test_hardening_50_sub_limited_docstring_drop_below_trip_cost_claim():
    """
    #50: CoverageStatus.SUB_LIMITED docstring must no longer claim 'capped BELOW
    the trip cost'. The semantics are 'covered subject to a sub-limit clause',
    regardless of whether the cap exceeds the trip cost. Verify the docstring
    does not contain the misleading 'below the trip cost' wording.
    """
    doc = CoverageStatus.SUB_LIMITED if hasattr(CoverageStatus, '__doc__') else None
    # Check the module-level docstring for the constant via the class docstring
    import inspect
    cls_source = inspect.getsource(CoverageStatus)
    # The old claim: "covered but capped BELOW the trip cost" must be gone.
    assert "below the trip cost" not in cls_source.lower(), (
        "#50: SUB_LIMITED docstring must not claim 'below the trip cost' — "
        "drop or rephrase so SUB_LIMITED means 'sub-limit clause applies' not 'cap<cost'"
    )
    # A large sub-limit (bigger than trip cost) still yields SUB_LIMITED today,
    # which is conservative (over-flagging) rather than fail-open.
    a = assess_coverage(
        peril_set=["medical"],
        policy_id="P-STD-01",
        insured_trip_cost_cents=1000,  # trip cost $10 — sub_limit is $50,000 >> trip
    )
    medical_rec = next(r for r in a["per_peril"] if r["peril_class"] == "medical")
    assert medical_rec["status"] == CoverageStatus.SUB_LIMITED, (
        "#50: medical must be SUB_LIMITED regardless of cap vs trip-cost comparison "
        "(conservative direction)"
    )
    assert medical_rec["sub_limit_cents"] == 5_000_000  # $50k cap


# ===========================================================================
# CONDITIONAL peril honest-disclosure tests (defect fix: CONDITIONAL silently vanished)
# ===========================================================================

def test_conditional_peril_not_silently_dropped():
    """
    adventure_activity has a CONDITION clause (CON-ADV-1) on P-STD-01.
    It must appear in conditional_perils AND in residual_ev.uncovered_perils
    (fail-conservative: if the condition is unmet, it is effectively uncovered).
    It must NOT appear in covered_perils (it is not unconditionally covered).
    """
    a = assess_coverage(
        peril_set=["adventure_activity", "travel_delay"],
        policy_id="P-STD-01",
        insured_trip_cost_cents=120000,
    )
    # 1. conditional_perils list must exist and include adventure_activity
    assert "conditional_perils" in a, "conditional_perils key missing from assessment"
    cond_classes = [c["peril_class"] for c in a["conditional_perils"]]
    assert "adventure_activity" in cond_classes, (
        f"adventure_activity must appear in conditional_perils; got {cond_classes}"
    )
    # 2. condition text must be surfaced
    cond_entry = next(c for c in a["conditional_perils"] if c["peril_class"] == "adventure_activity")
    assert cond_entry["condition"], "conditional_perils entry must carry condition text"

    # 3. MUST appear in residual_ev.uncovered_perils (fail-conservative)
    uncovered = a["residual_ev"]["uncovered_perils"]
    assert "adventure_activity" in uncovered, (
        f"adventure_activity (CONDITIONAL) must be in residual uncovered_perils; got {uncovered}"
    )

    # 4. Must NOT appear in covered_perils (not unconditionally covered)
    assert "adventure_activity" not in a["covered_perils"], (
        "adventure_activity must NOT appear in covered_perils (it is conditional, not unconditionally covered)"
    )

    # 5. Unconditionally covered peril still works correctly
    assert "travel_delay" in a["covered_perils"]
    assert "travel_delay" not in uncovered


def test_conditional_peril_present_in_invariant_all_perils():
    """
    Invariant: running assess_coverage over all canonical perils, every CONDITIONAL
    peril must appear in conditional_perils and in residual_ev.uncovered_perils,
    and must NOT appear in covered_perils. This catches any future clause addition
    where a CONDITIONAL might silently vanish.
    """
    from core import contracts as C
    for policy_id in _POLICY_CATALOG:
        a = assess_coverage(peril_set=C.all_perils(), policy_id=policy_id,
                            insured_trip_cost_cents=100000)
        cond_classes = {c["peril_class"] for c in a.get("conditional_perils", [])}
        uncovered = set(a["residual_ev"]["uncovered_perils"])
        covered = set(a["covered_perils"])

        for rec in a["per_peril"]:
            if rec["status"] == CoverageStatus.CONDITIONAL:
                p = rec["peril_class"]
                assert p in cond_classes, (
                    f"{policy_id}/{p}: CONDITIONAL peril must appear in conditional_perils"
                )
                assert p in uncovered, (
                    f"{policy_id}/{p}: CONDITIONAL peril must appear in residual uncovered_perils "
                    f"(fail-conservative)"
                )
                assert p not in covered, (
                    f"{policy_id}/{p}: CONDITIONAL peril must NOT appear in covered_perils"
                )


def test_conditional_residual_note_mentions_conditional():
    """
    The residual_ev.note must now mention CONDITIONAL so callers understand why a
    CONDITIONAL peril appears in the residual set.
    """
    a = assess_coverage(
        peril_set=["adventure_activity"],
        policy_id="P-STD-01",
    )
    note = a["residual_ev"]["note"]
    assert "CONDITIONAL" in note, (
        f"residual_ev.note must mention CONDITIONAL; got: {note!r}"
    )


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call(["python3", "-m", "pytest", __file__, "-v"]))
