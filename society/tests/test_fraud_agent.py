"""
test_fraud_agent.py — Unit + invariant tests for the Fraud / counterparty-trust
SOLVENCY GATE (L2 / DC-fraud), plus the Critic commit-time re-check interlock.

CI-safe: no LLM, no network, no DashScope. Uses the deterministic gate directly
and the in-process Starlette ASGI TestClient for the A2A surface.

Design contract: the §AGENT-EXTENSION PATTERN (insurance_agent.py) + AGENTS.md
(variance-clamp + the CI invariant tests for the gate) + the §10/§19.1
counterparty↔catalog identity contract in contracts.py.

Coverage map:
  unit:
    1.  blocked counterparty (distress watchlist) → not committable, consent_required
    2.  clear counterparty → committable, no consent required
    3.  elevated counterparty → committable (advisory), band=elevated
    4.  UNKNOWN counterparty (no seeded profile) → conservative block (band=unknown)
    5.  fresh consent token overrides the block (committable=True, consent_supplied)
    6.  band thresholds are a pure step function of the seeded score
    7.  vet() roll-up: blocked_ids / committable_ids / requires_consent_ids
    8.  counterparty identity (make_counterparty) is valid (§19.1)
    9.  provenance present + valid on every verdict
   10.  explain: factual claims derive from the structured result
  invariants (cascade-style regression — FAIL if violated):
    I1. BLOCKED-OR-UNKNOWN-NEVER-COMMITTABLE-WITHOUT-CONSENT (the gate).
    I2. UNKNOWN-NEVER-SILENT-OK (conservative block).
    I3. NO-LLM-NUMBERS: byte-identical bands/scores across N runs (variance-0).
    I4. DETERMINISTIC-BAND: the band is a pure function of the seeded score.
    I5. CHECKED-AT-CRITIC-TOO: the Critic re-derives the same band at commit time
        (COUNTERPARTY_BLOCKED) so a missed verdict can't slip an insolvent supplier
        through; consent overrides it there as well.
  closed_set_fallback:
    F1. The full vetting result is correct with NO LLM in the path (the gate is
        pure Python) — the LLM edge is purely cosmetic and clamped.
"""

from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(__file__))

from starlette.testclient import TestClient

from core import contracts as C
from agents import fraud_agent as fraud
from agents.fraud_agent import (
    FraudAgent,
    RiskBand,
    SOLVENCY_CLEAR_MIN,
    SOLVENCY_ELEVATED_MIN,
    band_for_score,
    explain,
    is_committable_band,
    solvency_profile_public,
    validate_rationale,
    vet,
    vet_counterparty,
)

# Seeded fixtures (from the solvency table).
CLEAR_CP = "carrier-singapore-airlines"      # score 91 → clear
ELEVATED_CP = "carrier-valuair-lcc"          # score 52 → elevated
BLOCKED_CP = "carrier-skylark-budget-air"    # score 28 → blocked (the DC3 trap)
UNKNOWN_CP = "carrier-who-knows-airlines"    # no seeded profile → unknown


def consent_for(counterparty_id: str, band: str, nonce: str = "n1") -> str:
    """A VALID structured consent grant (HIGH C1(a)): bound to the counterparty +
    consistent with the observed band. 'consent:{cid}:{band}:{nonce}'."""
    return f"consent:{counterparty_id}:{band}:{nonce}"


# ===========================================================================
# Unit
# ===========================================================================

def test_blocked_counterparty_not_committable():
    v = vet_counterparty(BLOCKED_CP)
    assert v["risk_band"] == RiskBand.BLOCKED
    assert v["committable"] is False
    assert v["consent_required"] is True
    assert v["consent_supplied"] is False
    assert "BLOCK_INSOLVENCY_RISK" in v["gate_reason"]


def test_clear_counterparty_committable():
    v = vet_counterparty(CLEAR_CP)
    assert v["risk_band"] == RiskBand.CLEAR
    assert v["committable"] is True
    assert v["consent_required"] is False
    assert v["gate_reason"] is None


def test_elevated_counterparty_committable_advisory():
    v = vet_counterparty(ELEVATED_CP)
    assert v["risk_band"] == RiskBand.ELEVATED
    assert v["committable"] is True
    assert v["consent_required"] is False


def test_unknown_counterparty_conservative_block():
    v = vet_counterparty(UNKNOWN_CP)
    assert v["risk_band"] == RiskBand.UNKNOWN
    assert v["committable"] is False         # never silent-OK
    assert v["consent_required"] is True
    assert v["solvency_score"] is None


def test_fresh_consent_overrides_block():
    # A VALID bound + band-consistent token overrides the block.
    v = vet_counterparty(
        BLOCKED_CP, consent_token=consent_for(BLOCKED_CP, RiskBand.BLOCKED))
    assert v["committable"] is True
    assert v["consent_supplied"] is True
    assert v["consent_required"] is True     # it WAS required; consent satisfied it
    assert "BLOCK_OVERRIDDEN_BY_CONSENT" in v["gate_reason"]
    # An empty / whitespace token does NOT count as consent.
    v2 = vet_counterparty(BLOCKED_CP, consent_token="   ")
    assert v2["committable"] is False
    # An UNBOUND/garbage token does NOT override (HIGH C1(a) — presence is not enough).
    v3 = vet_counterparty(BLOCKED_CP, consent_token="fresh-consent-2026-06-16")
    assert v3["committable"] is False
    assert v3["consent_supplied"] is False


def test_band_thresholds_pure_step_function():
    assert band_for_score(SOLVENCY_CLEAR_MIN) == RiskBand.CLEAR
    assert band_for_score(SOLVENCY_CLEAR_MIN - 1) == RiskBand.ELEVATED
    assert band_for_score(SOLVENCY_ELEVATED_MIN) == RiskBand.ELEVATED
    assert band_for_score(SOLVENCY_ELEVATED_MIN - 1) == RiskBand.BLOCKED
    assert band_for_score(0) == RiskBand.BLOCKED
    assert band_for_score(100) == RiskBand.CLEAR
    assert band_for_score(None) == RiskBand.UNKNOWN  # the conservative default


def test_vet_rollup():
    r = vet([
        {"counterparty_id": BLOCKED_CP, "leg_id": "leg-0"},
        {"counterparty_id": CLEAR_CP, "leg_id": "leg-1"},
        {"counterparty_id": UNKNOWN_CP, "leg_id": "leg-2"},
    ])
    rollup = r["rollup"]
    assert rollup["all_committable"] is False
    assert CLEAR_CP in rollup["committable_ids"]
    assert BLOCKED_CP in rollup["blocked_ids"]
    assert UNKNOWN_CP in rollup["blocked_ids"]
    assert BLOCKED_CP in rollup["requires_consent_ids"]
    assert rollup["risk_bands"][BLOCKED_CP] == RiskBand.BLOCKED


def test_vet_rollup_dedup_same_carrier():
    r = vet([
        {"counterparty_id": BLOCKED_CP, "leg_id": "leg-0"},
        {"counterparty_id": BLOCKED_CP, "leg_id": "leg-1"},  # same carrier twice
    ])
    assert r["rollup"]["n_counterparties"] == 1


def test_counterparty_identity_valid():
    v = vet_counterparty(BLOCKED_CP)
    assert C.is_valid_counterparty(v["counterparty"])
    # §19.1: counterparty_id == catalog_id (single namespace).
    assert v["counterparty"]["counterparty_id"] == v["counterparty"]["catalog_id"]


def test_provenance_present_and_valid():
    for cp in (CLEAR_CP, BLOCKED_CP, UNKNOWN_CP):
        v = vet_counterparty(cp)
        assert C.is_valid_provenance(v["provenance"])


def test_public_accessor_matches_society():
    p = solvency_profile_public(BLOCKED_CP)
    assert p["risk_band"] == RiskBand.BLOCKED
    assert p["committable_without_consent"] is False
    assert solvency_profile_public(UNKNOWN_CP) is None  # same gap the society sees


def test_explain_derives_from_result():
    r = vet([{"counterparty_id": BLOCKED_CP}])
    ex = explain(r)
    assert BLOCKED_CP in ex["blocked_ids"]
    assert "NOT committable" in ex["headline"]
    r2 = vet([{"counterparty_id": CLEAR_CP}])
    assert "committable" in explain(r2)["headline"].lower()


def test_rationale_validator_rejects_softened_block():
    r = vet([{"counterparty_id": BLOCKED_CP}])
    assert validate_rationale("This counterparty is on a distress watchlist; do not book without consent.", r) is True
    assert validate_rationale("All clear — safe to book this carrier.", r) is False


# ===========================================================================
# Invariants (cascade-style regression)
# ===========================================================================

def test_invariant_blocked_or_unknown_never_committable_without_consent():
    for cp in (BLOCKED_CP, UNKNOWN_CP):
        v = vet_counterparty(cp)
        assert v["committable"] is False, f"{cp} must NOT be committable without consent"


def test_invariant_unknown_never_silent_ok():
    # Every band string outside the closed set, and UNKNOWN, is non-committable.
    assert is_committable_band(RiskBand.UNKNOWN) is False
    assert is_committable_band("not-a-real-band") is False
    assert is_committable_band(RiskBand.CLEAR) is True
    assert is_committable_band(RiskBand.ELEVATED) is True
    assert is_committable_band(RiskBand.BLOCKED) is False


def test_invariant_no_llm_numbers_variance_zero_across_n():
    sigs = set()
    for _ in range(12):
        r = vet([
            {"counterparty_id": BLOCKED_CP},
            {"counterparty_id": CLEAR_CP},
            {"counterparty_id": ELEVATED_CP},
            {"counterparty_id": UNKNOWN_CP},
        ])
        import json
        sigs.add(json.dumps(r["rollup"], sort_keys=True))
    assert len(sigs) == 1, "fraud vetting must be byte-identical across runs (NO-LLM-NUMBERS)"


def test_invariant_deterministic_band_pure_function():
    # The band is a pure function of the seeded score — re-derive independently.
    for cp in (CLEAR_CP, ELEVATED_CP, BLOCKED_CP):
        p = solvency_profile_public(cp)
        assert p["risk_band"] == band_for_score(p["solvency_score"])


def test_invariant_seed_table_self_check_passes():
    # Importing the module runs _assert_seed_table_valid(); re-run explicitly.
    fraud._assert_seed_table_valid()


# ===========================================================================
# I5 — Critic commit-time re-check interlock (defense-in-depth)
# ===========================================================================

def test_invariant_checked_at_critic_too():
    from agents import critic_agent
    from agents.critic_agent import CriticAgent, COUNTERPARTY_BLOCKED

    assert critic_agent._fraud_vet_counterparty is not None, "Critic must wire the fraud re-check"
    c = CriticAgent()

    def codes(legs, consent=None):
        res = c._verify_itinerary(
            user_id="u", total_budget_cents=10_000_000, legs=legs,
            counterparty_consent_tokens=consent,
        )
        return [v["code"] for v in res["violations"]]

    leg_blocked = {"leg_id": "leg-0", "hotel_id": BLOCKED_CP,
                   "checkin": "2026-07-01", "checkout": "2026-07-05",
                   "total_cents": 10000, "provenance": "merchant"}
    leg_clear = {"leg_id": "leg-0", "hotel_id": "bali-alila-seminyak",
                 "checkin": "2026-07-01", "checkout": "2026-07-05",
                 "total_cents": 10000, "provenance": "merchant"}

    # Blocked counterparty → Critic rejects with COUNTERPARTY_BLOCKED even though
    # the orchestrator never called Fraud (the verdict was "missed").
    assert COUNTERPARTY_BLOCKED in codes([leg_blocked])
    # Fresh consent overrides the gate at the Critic too (VALID bound token).
    assert COUNTERPARTY_BLOCKED not in codes(
        [leg_blocked], {BLOCKED_CP: consent_for(BLOCKED_CP, RiskBand.BLOCKED)})
    # A garbage/unbound token does NOT override at the Critic (HIGH C1(a)).
    assert COUNTERPARTY_BLOCKED in codes([leg_blocked], {BLOCKED_CP: "fresh-xyz"})
    # A clear (solvent) counterparty is never flagged.
    assert COUNTERPARTY_BLOCKED not in codes([leg_clear])


# ===========================================================================
# Closed-set fallback (F1) — gate is correct with NO LLM in the path
# ===========================================================================

def test_closed_set_fallback_gate_is_pure_no_llm():
    # No DASHSCOPE_API_KEY → use_llm path returns deterministic rationale_source.
    old = os.environ.pop("DASHSCOPE_API_KEY", None)
    try:
        r = vet([{"counterparty_id": BLOCKED_CP}])
        # The structured decision is fully correct without any LLM call.
        assert r["rollup"]["blocked_ids"] == [BLOCKED_CP]
    finally:
        if old is not None:
            os.environ["DASHSCOPE_API_KEY"] = old


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
    agent = FraudAgent(host="0.0.0.0", port=9109)
    client = TestClient(agent.build_app())
    card = client.get("/.well-known/agent-card.json").json()
    assert card["name"] == "fraud-agent"
    skill_ids = {s["id"] for s in card["skills"]}
    assert skill_ids == {"fraud.vet", "fraud.explain"}


def test_a2a_vet_blocks_distressed_carrier():
    agent = FraudAgent()
    client = TestClient(agent.build_app())
    data = _send(client, "fraud.vet", {
        "counterparties": [
            {"counterparty_id": BLOCKED_CP, "kind": "transport", "leg_id": "leg-0"},
            {"counterparty_id": CLEAR_CP, "kind": "transport", "leg_id": "leg-1"},
        ]})
    assert data["rollup"]["all_committable"] is False
    assert BLOCKED_CP in data["rollup"]["blocked_ids"]
    assert data["rationale_source"] == "deterministic"


def test_a2a_vet_consent_token_overrides():
    agent = FraudAgent()
    client = TestClient(agent.build_app())
    data = _send(client, "fraud.vet", {
        "counterparties": [{"counterparty_id": BLOCKED_CP, "leg_id": "leg-0"}],
        "consent_tokens": {BLOCKED_CP: consent_for(BLOCKED_CP, RiskBand.BLOCKED)}})
    assert data["rollup"]["all_committable"] is True


def test_a2a_explain():
    agent = FraudAgent()
    client = TestClient(agent.build_app())
    vetted = _send(client, "fraud.vet", {"counterparties": [{"counterparty_id": BLOCKED_CP}]})
    ex = _send(client, "fraud.explain", {"result": vetted})
    assert BLOCKED_CP in ex["blocked_ids"]


# ===========================================================================
# ELEVATED vs BLOCKED distinction — explicit score range and gate behavior
# ===========================================================================

def test_elevated_mid_watch_score_range_and_committable():
    """
    GAP fill: ELEVATED (mid-watch, score 45–69) is committable WITHOUT consent.
    BLOCKED (confirmed-distressed, score <45) is NOT committable without consent.

    These are meaningfully different operational states — ELEVATED is an advisory
    flag to humans, not a hard block; BLOCKED is a hard stop requiring fresh consent.

    Assertions:
    - ELEVATED_CP (carrier-valuair-lcc, score=52) is in 45–69 range
    - ELEVATED band is committable (base_committable=True) with NO gate_reason
    - BLOCKED_CP (carrier-skylark-budget-air, score=28) is below 45
    - BLOCKED band is NOT committable (base_committable=False) with BLOCK_INSOLVENCY_RISK
    """
    elev = vet_counterparty(ELEVATED_CP)
    blck = vet_counterparty(BLOCKED_CP)

    # ELEVATED: score in the mid-watch range
    elev_score = elev["solvency_score"]
    assert elev_score is not None, "ELEVATED counterparty must have a seeded solvency score"
    assert SOLVENCY_ELEVATED_MIN <= elev_score < SOLVENCY_CLEAR_MIN, (
        f"ELEVATED counterparty score {elev_score} must be in [{SOLVENCY_ELEVATED_MIN}, "
        f"{SOLVENCY_CLEAR_MIN}); otherwise it's not in the mid-watch range"
    )

    # ELEVATED: committable WITHOUT consent — watchlist advisory only
    assert elev["risk_band"] == RiskBand.ELEVATED
    assert elev["committable"] is True, "ELEVATED is committable without consent"
    assert elev["consent_required"] is False, "ELEVATED does NOT require consent"
    assert elev["gate_reason"] is None, (
        "ELEVATED must have gate_reason=None (no block emitted)"
    )

    # BLOCKED: score strictly below the ELEVATED threshold
    blck_score = blck["solvency_score"]
    assert blck_score is not None, "BLOCKED counterparty must have a seeded solvency score"
    assert blck_score < SOLVENCY_ELEVATED_MIN, (
        f"BLOCKED counterparty score {blck_score} must be < {SOLVENCY_ELEVATED_MIN} "
        f"(confirmed-distressed, not mid-watch)"
    )

    # BLOCKED: NOT committable — hard stop
    assert blck["risk_band"] == RiskBand.BLOCKED
    assert blck["committable"] is False, "BLOCKED is NOT committable without consent"
    assert blck["consent_required"] is True, "BLOCKED requires explicit fresh consent"
    assert blck["gate_reason"] is not None, "BLOCKED must emit a non-None gate_reason"
    assert "BLOCK_INSOLVENCY_RISK" in blck["gate_reason"], (
        f"BLOCKED gate_reason must contain BLOCK_INSOLVENCY_RISK; got: {blck['gate_reason']!r}"
    )


def test_elevated_vs_blocked_operational_difference():
    """
    Structural: ELEVATED and BLOCKED are different bands with opposite commit behaviors.
    The vet() rollup must correctly segregate them into committable_ids vs blocked_ids.
    """
    r = vet([
        {"counterparty_id": ELEVATED_CP, "leg_id": "leg-0"},
        {"counterparty_id": BLOCKED_CP,  "leg_id": "leg-1"},
    ])
    rollup = r["rollup"]

    assert ELEVATED_CP in rollup["committable_ids"], (
        f"ELEVATED must appear in committable_ids; got {rollup['committable_ids']}"
    )
    assert ELEVATED_CP not in rollup["blocked_ids"], (
        "ELEVATED must NOT appear in blocked_ids"
    )
    assert BLOCKED_CP in rollup["blocked_ids"], (
        f"BLOCKED must appear in blocked_ids; got {rollup['blocked_ids']}"
    )
    assert BLOCKED_CP not in rollup["committable_ids"], (
        "BLOCKED must NOT appear in committable_ids (without consent)"
    )
    assert rollup["all_committable"] is False, (
        "all_committable must be False when BLOCKED is in the mix"
    )


def test_consent_overrides_blocked_but_not_required_for_elevated():
    """
    Consent token changes BLOCKED → committable.
    Consent token is irrelevant for ELEVATED (already committable without it).
    """
    # BLOCKED + VALID consent → committable, gate_reason = BLOCK_OVERRIDDEN_BY_CONSENT
    blocked_with_consent = vet_counterparty(
        BLOCKED_CP, consent_token=consent_for(BLOCKED_CP, RiskBand.BLOCKED))
    assert blocked_with_consent["committable"] is True
    assert blocked_with_consent["consent_supplied"] is True
    assert "BLOCK_OVERRIDDEN_BY_CONSENT" in blocked_with_consent["gate_reason"]

    # ELEVATED + consent → still committable, gate_reason still None
    # (consent is not needed and should not change the band; a token bound to a
    # different band is simply ignored for an already-committable counterparty)
    elevated_with_consent = vet_counterparty(
        ELEVATED_CP, consent_token=consent_for(ELEVATED_CP, RiskBand.BLOCKED))
    assert elevated_with_consent["committable"] is True
    assert elevated_with_consent["risk_band"] == RiskBand.ELEVATED
    assert elevated_with_consent["gate_reason"] is None, (
        "ELEVATED gate_reason must remain None even when consent is supplied "
        "(consent is only relevant for BLOCKED/UNKNOWN)"
    )


# ===========================================================================
# D8 — OTA reseller kind (finding #61)
# ===========================================================================

OTA_CP = "ota-cheapfly-reseller"  # seeded OTA reseller (kind must be "ota", not "transport")


def test_d8_ota_reseller_kind_is_ota_not_transport():
    """
    D8 / finding #61: OTA resellers must be seeded with kind='ota', not 'transport'.
    A funds-holding reseller is a different risk class from an operating carrier.
    """
    p = solvency_profile_public(OTA_CP)
    assert p is not None, f"{OTA_CP} must be in the seed table"
    assert p["kind"] == "ota", (
        f"OTA reseller {OTA_CP!r} must have kind='ota' (CounterpartyKind.OTA), "
        f"got kind={p['kind']!r}. Mislabeling a reseller as 'transport' confuses "
        "downstream consumers that branch on counterparty kind."
    )
    assert p["kind"] != "transport", (
        f"{OTA_CP!r} must NOT have kind='transport' — it is a reseller, not a carrier"
    )


def test_d8_ota_reseller_still_blocked_after_kind_fix():
    """OTA reseller is distressed (score 31 < 45) → blocked, consistent with carrier-blocking."""
    p = solvency_profile_public(OTA_CP)
    assert p["risk_band"] == RiskBand.BLOCKED
    assert p["committable_without_consent"] is False


def test_d8_seed_table_self_check_accepts_ota_kind():
    """
    Seed table self-check must pass with the OTA kind in the table.
    This covers the _assert_seed_table_valid() path that includes _KIND_OTA.
    """
    from agents.fraud_agent import _assert_seed_table_valid
    _assert_seed_table_valid()  # must not raise


def test_d8_ota_rollup_distinguishes_ota_from_transport():
    """vet() rollup: OTA and carrier appear with their distinct kinds in per_counterparty."""
    r = vet([
        {"counterparty_id": OTA_CP, "leg_id": "leg-0"},
        {"counterparty_id": "carrier-skylark-budget-air", "leg_id": "leg-1"},
    ])
    kinds = {
        v["counterparty_id"]: v["counterparty"]["kind"]
        for v in r["per_counterparty"]
    }
    assert kinds[OTA_CP] == "ota", (
        f"OTA reseller must emit kind='ota' in the counterparty identity; got {kinds[OTA_CP]!r}"
    )
    assert kinds["carrier-skylark-budget-air"] == "transport", (
        "Carrier must still emit kind='transport'"
    )


# ===========================================================================
# Finding #18 — Unknown-hotel gate policy is explicit (lodging out-of-scope)
# ===========================================================================

def test_finding18_unknown_hotel_returns_unknown_band():
    """
    Finding #18: vet_counterparty returns band=UNKNOWN (conservative block) for any
    hotel_id not in the seed table. The gate policy is that seeding is limited to
    the demo/DC corpus; bulk lodging solvency is out-of-scope at this phase.
    """
    arbitrary_hotel = "paris-le-grand-hotel-unknown"
    v = vet_counterparty(arbitrary_hotel)
    assert v["risk_band"] == RiskBand.UNKNOWN
    assert v["committable"] is False, (
        f"Unseeded hotel {arbitrary_hotel!r} must NOT be committable without consent "
        "(UNKNOWN → conservative block per finding #18)"
    )
    assert v["consent_required"] is True


def test_finding18_seeded_lodging_operators_are_gated():
    """Seeded Bali lodging operators ARE gated — not bypassed — by the seed table."""
    # All seeded Bali hotels are CLEAR or ELEVATED (score >= 58) → committable.
    seeded_hotels = [
        "bali-alila-seminyak",    # score 80 → clear
        "bali-como-canggu",       # score 83 → clear
        "bali-ubud-garden",       # score 58 → elevated
    ]
    for hid in seeded_hotels:
        p = solvency_profile_public(hid)
        assert p is not None, f"Seeded hotel {hid!r} must have a public profile"
        assert p["kind"] == "hotel"
        assert p["committable_without_consent"] is True, (
            f"Seeded hotel {hid!r} (score {p['solvency_score']}) should be committable"
        )


# ===========================================================================
# HIGH C1(a) — Consent token contract: VALIDATED binding + band-consistency,
# NOT presence-only. A garbage/unbound/band-mismatched token NEVER overrides.
# ===========================================================================

def test_c1a_consent_is_validated_not_presence_only():
    """
    HIGH C1(a): the consent token overrides the gate ONLY if it VALIDATES — bound
    to this counterparty_id AND consistent with the observed band. Mere non-empty
    presence is NOT enough (fail-conservative — HONESTY/var-0).
    """
    # A deliberately unstructured, non-empty token does NOT override (the fix).
    v = vet_counterparty(BLOCKED_CP, consent_token="any-truthy-string")
    assert v["committable"] is False
    assert v["consent_supplied"] is False

    # A VALID structured + bound + band-consistent token DOES override.
    v_ok = vet_counterparty(
        BLOCKED_CP, consent_token=consent_for(BLOCKED_CP, RiskBand.BLOCKED))
    assert v_ok["committable"] is True
    assert v_ok["consent_supplied"] is True

    # A whitespace-only token does NOT count.
    v_ws = vet_counterparty(BLOCKED_CP, consent_token="   ")
    assert v_ws["committable"] is False
    assert v_ws["consent_supplied"] is False

    # None / empty string → no consent.
    v_none = vet_counterparty(BLOCKED_CP, consent_token=None)
    assert v_none["committable"] is False
    assert v_none["consent_supplied"] is False

    v_empty = vet_counterparty(BLOCKED_CP, consent_token="")
    assert v_empty["committable"] is False
    assert v_empty["consent_supplied"] is False


def test_c1a_consent_must_be_bound_to_counterparty():
    """A token minted for counterparty-A must NOT release counterparty-B (BINDING)."""
    # Token bound to a DIFFERENT (clear) counterparty does not override BLOCKED_CP.
    wrong_binding = consent_for(CLEAR_CP, RiskBand.BLOCKED)
    v = vet_counterparty(BLOCKED_CP, consent_token=wrong_binding)
    assert v["committable"] is False
    assert v["consent_supplied"] is False
    # Token bound to THIS counterparty does override.
    right_binding = consent_for(BLOCKED_CP, RiskBand.BLOCKED)
    v2 = vet_counterparty(BLOCKED_CP, consent_token=right_binding)
    assert v2["committable"] is True


def test_c1a_consent_must_be_consistent_with_observed_band():
    """A token claiming a band that doesn't match the seed-derived band is ignored."""
    # BLOCKED_CP observed band is BLOCKED; a token claiming 'unknown' must NOT match.
    mismatched = consent_for(BLOCKED_CP, RiskBand.UNKNOWN)
    v = vet_counterparty(BLOCKED_CP, consent_token=mismatched)
    assert v["committable"] is False
    assert v["consent_supplied"] is False
    # UNKNOWN counterparty: a token bound + claiming 'unknown' DOES override.
    ok = consent_for(UNKNOWN_CP, RiskBand.UNKNOWN)
    v_unknown = vet_counterparty(UNKNOWN_CP, consent_token=ok)
    assert v_unknown["committable"] is True
    assert v_unknown["consent_supplied"] is True


def test_c1a_consent_does_not_change_risk_band():
    """A VALID consent overrides the commit decision but never changes the risk_band."""
    v_without = vet_counterparty(BLOCKED_CP)
    v_with = vet_counterparty(
        BLOCKED_CP, consent_token=consent_for(BLOCKED_CP, RiskBand.BLOCKED))
    # band is unchanged — the seeded score drives the band, not the consent token.
    assert v_without["risk_band"] == v_with["risk_band"] == RiskBand.BLOCKED
    assert v_without["solvency_score"] == v_with["solvency_score"]
    # consent_required remains True even when consent was supplied (it WAS required).
    assert v_with["consent_required"] is True
    assert v_with["committable"] is True


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
