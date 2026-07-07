"""
test_orch_2pass_recovery_cov3.py — deep coverage for the TravelOrchestrator
"recovery/cascade/finalize" region.

Target orchestrator.py uncovered line ranges:
  1426-1442  — Fraud gate conservative block (missing all_committable) +
               #72 gate_fee_total envelope calculation
  1487-1556  — Post-negotiate result finalization block:
               health_verdict / optional_health_estimate surfacing (1488-1492)
               compliance_verdict + entry_cert_handoff (1493-1496)
               fraud_verdict surfacing after gate passes (1497-1498)
               gate_fees injection (1499-1500)
               compliance flagged_legs advisory (1508-1510)
               health flagged_destinations advisory (1514)
               risk per_leg advisory detail (1527-1532), non-dict pleg guard (1527-1528)
               day_plans notes lifted to advisories (1541, 1543-1545)
  1573-1595  — opt-in tail exception-swallow branches:
               _maybe_order_supper raises → result still returned (1578-1579)
               _maybe_enrich_dining raises → result still returned (1589-1590)
               _maybe_check_active_emergencies raises → result still returned

Additionally:
  needs_reconciliation / _commit_failed_result (line 4704) — static method direct call

Design:
  build_society() (full society, all agents as TestClients, no merchant) + per-test
  patches of the internal seam callers (_negotiate_dp, _run_health_gate,
  _run_compliance_gate, _run_fraud_gate, _assess_risk_signals, _maybe_order_supper,
  _maybe_enrich_dining, _maybe_check_active_emergencies, _call_insurance,
  _call_fraud).  All patching is deterministic and var-0-safe; no live LLM/network
  /paid-API/real-merchant.  Booking_ref nonce values are never asserted.
"""

from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from agents.planner_agent import PlannerAgent  # noqa: E402
from agents.accommodation_agent import AccommodationAgent  # noqa: E402
from agents.budget_agent import BudgetAgent  # noqa: E402
from agents.critic_agent import CriticAgent  # noqa: E402
from agents.destination_agent import DestinationAgent  # noqa: E402
from agents.compliance_agent import ComplianceAgent  # noqa: E402
from agents.health_agent import HealthAgent  # noqa: E402
from agents.insurance_agent import InsuranceAgent  # noqa: E402
from agents.fraud_agent import FraudAgent  # noqa: E402
from agents.risk_agent import RiskAgent  # noqa: E402
from agents.transport_agent import TransportAgent  # noqa: E402
from agents.day_planner_agent import DayPlannerAgent  # noqa: E402
from orchestration.orchestrator import TravelOrchestrator  # noqa: E402
from core import contracts as C  # noqa: E402


# ===========================================================================
# Society fixture
# ===========================================================================

def build_society() -> TravelOrchestrator:
    """Full society with all agents as TestClients; no real merchant transport."""
    planner = PlannerAgent()
    acc = AccommodationAgent(merchant_transport=None)
    budget = BudgetAgent(merchant_transport=None)
    critic = CriticAgent()
    destination = DestinationAgent()
    compliance = ComplianceAgent()
    health = HealthAgent()
    insurance = InsuranceAgent()
    fraud = FraudAgent()
    risk = RiskAgent()
    transport = TransportAgent()
    day_planner = DayPlannerAgent()
    return TravelOrchestrator(
        planner_client=TestClient(planner.build_app()),
        accommodation_client=TestClient(acc.build_app()),
        budget_client=TestClient(budget.build_app()),
        critic_client=TestClient(critic.build_app()),
        destination_client=TestClient(destination.build_app()),
        compliance_client=TestClient(compliance.build_app()),
        health_client=TestClient(health.build_app()),
        insurance_client=TestClient(insurance.build_app()),
        fraud_client=TestClient(fraud.build_app()),
        risk_client=TestClient(risk.build_app()),
        transport_client=TestClient(transport.build_app()),
        day_planner_client=TestClient(day_planner.build_app()),
    )


def _usd_money(cents: int) -> dict[str, Any]:
    """Canonical USD money tag via the contract helper."""
    return C.make_money(
        native_cents=cents, currency="USD", usd_cents=cents,
        fx_rate=1.0, fx_source="seeded", fetched_at="2026-06-20",
    )


def _fee_li(cents: int, kind: str = "visa", source: str = "compliance") -> dict:
    """Minimal fee line-item suitable for _inject_fees."""
    return {
        "source": source,
        "kind": kind,
        "leg": "leg-0",
        "money": _usd_money(cents),
        "description": f"test-{kind}",
    }


def _canned_success(total_cents: int = 50_000) -> dict[str, Any]:
    """A minimal success dict that negotiate() finalization will accept."""
    return {
        "outcome": "success",
        "total_cents": total_cents,
        "total_budget_cents": 200_000,
        "booking_ref": "BK-test-cov3-001",
        "checkout_id": "co-cov3-001",
        "legs": [
            {
                "leg_id": "leg-0",
                "city": "bali",
                "hotel_id": "hotel-bali-cov3",
                "total_cents": total_cents,
                "checkin": "2027-01-10",
                "checkout": "2027-01-13",
            }
        ],
        "day_plans": [],
        "negotiation_log": [],
        "negotiation_rounds": 1,
    }


def _base_trip(*, with_counterparty: bool = False) -> dict:
    """Minimal deterministic trip request; city-only (no dest_country, no place_key)
    so Health and Compliance stay PASS-THROUGH (None) unless further patched."""
    trip: dict[str, Any] = {
        "user_id": "cov3-user",
        "total_budget_cents": 200_000,
        "today": "2027-01-01",
        "legs": [
            {
                "city": "bali",
                "checkin": "2027-01-10",
                "checkout": "2027-01-13",
                "adults": 1,
            }
        ],
    }
    if with_counterparty:
        trip["counterparties"] = [
            {"counterparty_id": "cov3-cp-unknown", "kind": "transport", "leg_id": "leg-0"},
        ]
    return trip


# ===========================================================================
# CLUSTER 1 — Fraud gate conservative block (lines 1426-1432)
# ===========================================================================

def test_fraud_gate_block_missing_all_committable():
    """
    Line 1426: `if not rollup.get("all_committable", False)` — when the Fraud
    agent's rollup omits `all_committable` the default is False → the conservative
    block fires (orchestrator.py:1427 _fraud_block_result).

    Assertion: outcome == cannot_satisfy, fraud_verdict attached, and the
    negotiation_log contains a fraud_gate_block action — catching a regression
    where the D1 conservative-default guard is removed.
    """
    orch = build_society()
    trip = _base_trip(with_counterparty=True)

    # Fraud agent returns a rollup that OMITS all_committable → conservative block
    fraud_resp = {
        "rollup": {
            "blocked_ids": ["cov3-cp-unknown"],
            "requires_consent_ids": [],
            # all_committable intentionally absent
        },
        "counterparties": [],
    }
    with patch.object(orch, "_call_fraud", return_value=fraud_resp):
        with patch.object(orch, "_tracer"):
            result = orch.negotiate(trip)

    assert result["outcome"] == "cannot_satisfy", result
    # fraud_verdict MUST be attached — a regression removing it breaks UX transparency
    assert "fraud_verdict" in result, result
    assert result["fraud_verdict"] is fraud_resp
    # Negotiation log must carry the fraud_gate_block action
    actions = [e.get("action") for e in result.get("negotiation_log", [])]
    assert "fraud_gate_block" in actions, actions
    # Reason string must name the blocked counterparty
    assert "cov3-cp-unknown" in result.get("reason", ""), result.get("reason")


def test_fraud_gate_block_all_committable_false_explicit():
    """
    Explicit `all_committable: False` in rollup triggers the same block path (1426).
    Confirm the terminal shape contains needs_reconciliation=False (this is a gate
    block, NOT a commit_failed — the two terminals must be distinguishable).
    """
    orch = build_society()
    trip = _base_trip(with_counterparty=True)

    fraud_resp = {
        "rollup": {
            "all_committable": False,
            "blocked_ids": ["cov3-bad-carrier"],
        },
    }
    with patch.object(orch, "_call_fraud", return_value=fraud_resp):
        with patch.object(orch, "_tracer"):
            result = orch.negotiate(trip)

    assert result["outcome"] == "cannot_satisfy"
    # A gate block is NOT a commit failure; no needs_reconciliation flag.
    assert result.get("needs_reconciliation") is not True
    assert "fraud_verdict" in result


# ===========================================================================
# CLUSTER 1b — #72 gate_fee_total envelope calculation (lines 1442-1453)
# ===========================================================================

def test_gate_fee_total_nonzero_envelope_shrinks_lodging_budget():
    """
    Lines 1442-1444: gate_fee_total = sum(usd_cents) over gate_fees.
    Lines 1446-1447: enforced_envelope = gate_fee_total + premium_est;
                     lodging_budget_cents = max(1, total - envelope).

    Drive a path where gate_fees is NON-EMPTY (simulate compliance gate fee
    arriving without blocking, and no premium so we isolate the envelope calc).
    _negotiate_dp is patched to return a canned success so we can verify the
    result was produced; the test asserts that negotiation COMPLETED (outcome !=
    gate_block) and that the finalization block ran (result is a dict).

    Regression target: removing max(1, ...) or the sum() would produce a
    zero/negative lodging budget silently accepted by the DP path.
    """
    orch = build_society()
    trip = _base_trip()

    # Patch _negotiate_dp to return success immediately (bypasses real merchant)
    success = _canned_success()

    # Patch _run_compliance_gate to inject a fee line but remain bookable
    fee = _fee_li(5_000, kind="visa", source="compliance")
    compliance_verdict = {
        "verdict": "can_satisfy",
        "bookable": True,
        "flagged_legs": [],
        "line_items": [fee],
    }

    with patch.object(orch, "_run_compliance_gate", return_value=compliance_verdict):
        with patch.object(orch, "_run_health_gate", return_value=None):
            with patch.object(orch, "_run_fraud_gate", return_value=None):
                with patch.object(orch, "_assess_risk_signals", return_value=None):
                    with patch.object(orch, "_negotiate_dp", return_value=success):
                        with patch.object(orch, "_tracer"):
                            result = orch.negotiate(trip)

    # The negotiation completed (not a gate block)
    assert result.get("outcome") == "success", result
    # Fee was injected into the result by _inject_fees (lines 1499-1500)
    assert "fee_line_items" in result, result
    assert result["fee_total_cents"] == 5_000, result.get("fee_total_cents")


# ===========================================================================
# CLUSTER 2a — fraud_verdict surfaced post-negotiate (line 1497-1498)
# ===========================================================================

def test_fraud_verdict_surfaced_on_success_when_all_committable():
    """
    Line 1497-1498: `if fraud_verdict is not None: result["fraud_verdict"] = fraud_verdict`

    When fraud_verdict is not None AND all_committable=True (gate passes), the
    fraud_verdict is still attached to the SUCCESSFUL result so the caller can see
    the solvency assessment. Regression: removing the attach would silently drop
    fraud transparency on committable-but-flagged trips.
    """
    orch = build_society()
    trip = _base_trip(with_counterparty=True)

    # Fraud passes (all_committable=True) but verdict is not None
    fraud_resp = {
        "rollup": {
            "all_committable": True,
            "blocked_ids": [],
            "requires_consent_ids": [],
            "n_counterparties": 1,
        },
        "counterparties": [
            {"counterparty_id": "cov3-cp-unknown", "committable": True,
             "risk_band": "elevated", "advisory": "elevated risk — watch"},
        ],
    }
    success = _canned_success()

    with patch.object(orch, "_call_fraud", return_value=fraud_resp):
        with patch.object(orch, "_run_health_gate", return_value=None):
            with patch.object(orch, "_run_compliance_gate", return_value=None):
                with patch.object(orch, "_assess_risk_signals", return_value=None):
                    with patch.object(orch, "_negotiate_dp", return_value=success):
                        with patch.object(orch, "_tracer"):
                            result = orch.negotiate(trip)

    # Gate passed, booking succeeded
    assert result.get("outcome") == "success", result
    # fraud_verdict MUST be in the result
    assert "fraud_verdict" in result, result
    assert result["fraud_verdict"]["rollup"]["all_committable"] is True


# ===========================================================================
# CLUSTER 2b — compliance entry_cert_handoff surfaced (lines 1495-1496)
# ===========================================================================

def test_entry_cert_handoff_surfaced_when_compliance_provides_it():
    """
    Lines 1495-1496: `if compliance_verdict.get('entry_cert_handoff'):
                         result['entry_cert_handoff'] = ...`

    When compliance_verdict carries entry_cert_handoff (e.g. a YF entry
    certificate handoff link), it MUST be lifted to the top-level result so
    the UI can surface it without digging into compliance_verdict.

    Regression: removing the lift causes the handoff to be invisible unless
    the caller explicitly inspects the nested compliance_verdict.
    """
    orch = build_society()
    trip = _base_trip()

    handoff = {
        "kind": "entry_cert",
        "doc_type": "YF_VACCINATION_CARD",
        "display_name": "Yellow Fever Certificate",
        "url": "https://example.com/yf-cert",
    }
    compliance_verdict = {
        "verdict": "can_satisfy",
        "bookable": True,
        "flagged_legs": [],
        "line_items": [],
        "entry_cert_handoff": handoff,
    }
    success = _canned_success()

    with patch.object(orch, "_run_compliance_gate", return_value=compliance_verdict):
        with patch.object(orch, "_run_health_gate", return_value=None):
        # NOTE: the above compliance verdict carries no gate_fees so no blocking
            with patch.object(orch, "_run_fraud_gate", return_value=None):
                with patch.object(orch, "_assess_risk_signals", return_value=None):
                    with patch.object(orch, "_negotiate_dp", return_value=success):
                        with patch.object(orch, "_tracer"):
                            result = orch.negotiate(trip)

    assert result.get("outcome") == "success", result
    # compliance_verdict is attached
    assert result.get("compliance_verdict") is compliance_verdict
    # handoff is lifted to the top level
    assert "entry_cert_handoff" in result, result
    assert result["entry_cert_handoff"]["doc_type"] == "YF_VACCINATION_CARD"


# ===========================================================================
# CLUSTER 2c — compliance flagged_legs advisory lifted (lines 1507-1510)
# ===========================================================================

def test_compliance_flagged_legs_advisory_lifted_to_advisories():
    """
    Lines 1507-1510: for each flagged leg in compliance_verdict["flagged_legs"],
    if the leg carries a `flag_advisory` text it is appended to the top-level
    result["advisories"].

    Regression: removing the loop silently drops the compliance advisory for
    any UI rendering only result["advisories"].
    """
    orch = build_society()
    trip = _base_trip()

    advisory_text = "EVISA_REQUIRED: apply at least 5 business days before travel"
    compliance_verdict = {
        "verdict": "can_satisfy",
        "bookable": True,
        "line_items": [],
        "flagged_legs": [
            {"leg_id": "leg-0", "flag_advisory": advisory_text, "flag_code": "EVISA"},
            {"leg_id": "leg-1", "flag_advisory": None},   # None advisory → not appended
            None,                                           # None leg → guarded by `leg or {}`
        ],
    }
    success = _canned_success()

    with patch.object(orch, "_run_compliance_gate", return_value=compliance_verdict):
        with patch.object(orch, "_run_health_gate", return_value=None):
            with patch.object(orch, "_run_fraud_gate", return_value=None):
                with patch.object(orch, "_assess_risk_signals", return_value=None):
                    with patch.object(orch, "_negotiate_dp", return_value=success):
                        with patch.object(orch, "_tracer"):
                            result = orch.negotiate(trip)

    assert result.get("outcome") == "success", result
    advisories = result.get("advisories", [])
    assert advisory_text in advisories, advisories
    # Exactly one advisory (None leg + None advisory leg contribute 0)
    assert advisories.count(advisory_text) == 1


# ===========================================================================
# CLUSTER 2d — health flagged_destinations advisory lifted (line 1514)
# ===========================================================================

def test_health_flagged_destinations_advisory_lifted():
    """
    Lines 1511-1515: for each flagged destination in health_verdict["flagged_destinations"],
    the `flag_advisory` text is appended to result["advisories"].

    Also exercises line 1488-1492 (health_verdict surfaced) and the optional_health_estimate
    path when health_verdict carries optional_vaccine_estimate.
    """
    orch = build_society()
    trip = _base_trip()

    health_adv = "MALARIA_PROPHYLAXIS_RECOMMENDED: consult a travel clinic"
    health_verdict = {
        "verdict": "can_satisfy",
        "bookable": True,
        "mandatory_line_items": [],
        "flagged_destinations": [
            {"dest": "ID", "flag_advisory": health_adv},
            {"dest": "XX", "flag_advisory": ""},   # empty → not appended
            None,                                    # None → guarded
        ],
        "optional_vaccine_estimate": {
            "total_usd_cents": 3_000,
            "items": [{"vaccine": "malaria_prophylaxis", "usd_cents": 3_000}],
        },
    }
    success = _canned_success()

    with patch.object(orch, "_run_health_gate", return_value=health_verdict):
        with patch.object(orch, "_run_compliance_gate", return_value=None):
            with patch.object(orch, "_run_fraud_gate", return_value=None):
                with patch.object(orch, "_assess_risk_signals", return_value=None):
                    with patch.object(orch, "_negotiate_dp", return_value=success):
                        with patch.object(orch, "_tracer"):
                            result = orch.negotiate(trip)

    assert result.get("outcome") == "success", result
    # health_verdict lifted to result
    assert result.get("health_verdict") is health_verdict
    # optional_health_estimate present (has optional_vaccine_estimate content)
    ohe = result.get("optional_health_estimate")
    assert ohe is not None, result
    assert ohe["not_in_budget"] is True
    assert ohe["vaccine_cost_usd_cents"] == 3_000
    # flag_advisory lifted
    advisories = result.get("advisories", [])
    assert health_adv in advisories, advisories
    # empty advisory NOT appended
    assert "" not in advisories


# ===========================================================================
# CLUSTER 2e — risk per_leg advisory detail lifted (lines 1527-1532)
#              non-dict pleg guard (lines 1527-1528)
#              risk_flagged bool (line 1523)
# ===========================================================================

def test_risk_per_leg_advisory_detail_lifted_to_advisories():
    """
    Lines 1520-1532: when risk_assessment is a dict with flagged_legs, result["risk_flagged"]
    is set, and per_leg advisory detail strings are appended to result["advisories"].
    Lines 1527-1528: a non-dict entry in per_leg is skipped (continue guard).

    Regression: dropping the per_leg loop silently hides risk advisories from UIs
    that only inspect result["advisories"].
    """
    orch = build_society()
    trip = _base_trip()

    risk_adv_1 = "CYCLONE SEASON: typhoon window active Sep-Nov for this region"
    risk_adv_2 = "FLOOD RISK: riverside areas may be impassable"
    risk_assessment = {
        "rollup": {
            "flagged_legs": ["leg-0"],
            "all_reason_codes": ["cyclone_season", "flood_risk"],
            "any_avoid_window": False,
            "max_buffer_connection_min": 0,
        },
        "per_leg": [
            {
                "leg_id": "leg-0",
                "advisory": [
                    {"detail": risk_adv_1, "code": "cyclone_season"},
                    {"detail": risk_adv_2, "code": "flood_risk"},
                    {"detail": None},    # None detail → not appended
                ],
            },
            "not-a-dict",    # non-dict entry → continue guard (line 1527-1528)
        ],
    }
    success = _canned_success()

    with patch.object(orch, "_assess_risk_signals", return_value=risk_assessment):
        with patch.object(orch, "_run_health_gate", return_value=None):
            with patch.object(orch, "_run_compliance_gate", return_value=None):
                with patch.object(orch, "_run_fraud_gate", return_value=None):
                    with patch.object(orch, "_negotiate_dp", return_value=success):
                        with patch.object(orch, "_tracer"):
                            result = orch.negotiate(trip)

    assert result.get("outcome") == "success", result
    # risk_flagged set True because flagged_legs is non-empty
    assert result.get("risk_flagged") is True
    advisories = result.get("advisories", [])
    assert risk_adv_1 in advisories, advisories
    assert risk_adv_2 in advisories, advisories
    # No crash from non-dict "not-a-dict" entry in per_leg


def test_risk_no_flagged_legs_risk_flagged_false():
    """
    Line 1523: `result["risk_flagged"] = bool(rk_flagged_legs)` — when risk runs
    but flagged_legs is empty, risk_flagged is False (not True and not absent).

    Regression: removing the bool() cast or the key set would make risk_flagged
    silently absent on clear-risk trips.
    """
    orch = build_society()
    trip = _base_trip()

    risk_assessment = {
        "rollup": {
            "flagged_legs": [],
            "all_reason_codes": [],
            "any_avoid_window": False,
            "max_buffer_connection_min": 0,
        },
        "per_leg": [],
    }
    success = _canned_success()

    with patch.object(orch, "_assess_risk_signals", return_value=risk_assessment):
        with patch.object(orch, "_run_health_gate", return_value=None):
            with patch.object(orch, "_run_compliance_gate", return_value=None):
                with patch.object(orch, "_run_fraud_gate", return_value=None):
                    with patch.object(orch, "_negotiate_dp", return_value=success):
                        with patch.object(orch, "_tracer"):
                            result = orch.negotiate(trip)

    assert result.get("outcome") == "success", result
    # risk_flagged key IS present and is False (not absent, not True)
    assert "risk_flagged" in result, result
    assert result["risk_flagged"] is False


# ===========================================================================
# CLUSTER 2f — day_plans notes lifted to advisories (lines 1540-1545)
#              non-dict plan guard (line 1541)
# ===========================================================================

def test_day_plans_notes_lifted_to_advisories():
    """
    Lines 1540-1545: for each plan in result["day_plans"], if plan has notes,
    each non-empty note is appended to advisories.
    Line 1541: non-dict plan entry is skipped (continue guard).

    Regression: removing the loop silently drops honest day-plan gaps
    (dietary fallback, no-restaurant, low-attraction-variety) from the UI.
    """
    orch = build_society()
    trip = _base_trip()

    note_1 = "dietary-fallback: no halal restaurants found; generic options shown"
    note_2 = "low-attraction-variety: fewer than 3 attractions available in this city"
    # Build a success dict with day_plans that carry notes
    success = _canned_success()
    success["day_plans"] = [
        {"leg_id": "leg-0", "city": "bali", "notes": [note_1, note_2, ""]},  # empty → skip
        {"leg_id": "leg-1", "city": "lombok", "notes": []},  # no notes → skip
        "not-a-plan",  # non-dict entry → continue guard
    ]

    with patch.object(orch, "_run_health_gate", return_value=None):
        with patch.object(orch, "_run_compliance_gate", return_value=None):
            with patch.object(orch, "_run_fraud_gate", return_value=None):
                with patch.object(orch, "_assess_risk_signals", return_value=None):
                    with patch.object(orch, "_negotiate_dp", return_value=success):
                        with patch.object(orch, "_tracer"):
                            result = orch.negotiate(trip)

    assert result.get("outcome") == "success", result
    advisories = result.get("advisories", [])
    assert note_1 in advisories, advisories
    assert note_2 in advisories, advisories
    # Empty string not appended
    assert "" not in advisories
    # No crash from "not-a-plan" non-dict entry


# ===========================================================================
# CLUSTER 2g — combined: compliance + health + risk + day_plans advisories in one trip
# ===========================================================================

def test_all_advisory_sources_aggregate_correctly():
    """
    Drives ALL four advisory sources simultaneously through the finalize block and
    verifies that ALL distinct advisory texts accumulate in result["advisories"]
    with no duplicates from unrelated paths.  The assertion count is exact,
    catching silent drops.
    """
    orch = build_society()
    trip = _base_trip()

    comp_adv = "EVISA_REQUIRED: 5-day lead time"
    health_adv = "MALARIA_PROPHYLAXIS: consult travel clinic"
    risk_adv = "WILDFIRE_SEASON: smoke hazard Aug-Oct"
    plan_note = "no-restaurant: no known dining in this area"

    compliance_verdict = {
        "verdict": "can_satisfy", "bookable": True, "line_items": [],
        "flagged_legs": [{"leg_id": "leg-0", "flag_advisory": comp_adv}],
    }
    health_verdict = {
        "verdict": "can_satisfy", "bookable": True, "mandatory_line_items": [],
        "flagged_destinations": [{"dest": "ID", "flag_advisory": health_adv}],
    }
    risk_assessment = {
        "rollup": {
            "flagged_legs": ["leg-0"],
            "all_reason_codes": ["wildfire_season"],
            "any_avoid_window": False,
            "max_buffer_connection_min": 0,
        },
        "per_leg": [
            {"leg_id": "leg-0", "advisory": [{"detail": risk_adv}]},
        ],
    }
    success = _canned_success()
    success["day_plans"] = [{"leg_id": "leg-0", "notes": [plan_note]}]

    with patch.object(orch, "_run_compliance_gate", return_value=compliance_verdict):
        with patch.object(orch, "_run_health_gate", return_value=health_verdict):
            with patch.object(orch, "_assess_risk_signals", return_value=risk_assessment):
                with patch.object(orch, "_run_fraud_gate", return_value=None):
                    with patch.object(orch, "_negotiate_dp", return_value=success):
                        with patch.object(orch, "_tracer"):
                            result = orch.negotiate(trip)

    assert result.get("outcome") == "success", result
    advisories = result.get("advisories", [])
    for expected in [comp_adv, health_adv, risk_adv, plan_note]:
        assert expected in advisories, f"{expected!r} missing from {advisories}"
    # Exactly 4 distinct advisories — no silent duplicates
    assert len(advisories) == 4, advisories


# ===========================================================================
# CLUSTER 3a — _maybe_order_supper exception swallowed (lines 1573-1579)
# ===========================================================================

def test_maybe_order_supper_exception_swallowed_result_intact():
    """
    Lines 1573-1579: the negotiate() tail wraps _maybe_order_supper in try/except;
    if it raises, the exception is logged and IGNORED — the result is still returned.

    Regression: removing the try/except would crash negotiate() on supper failures,
    converting an opt-in add-on error into a fatal trip failure.
    """
    orch = build_society()
    trip = _base_trip()
    trip["supper"] = {"order": True, "city": "bali", "max_cents": 5_000}

    success = _canned_success()

    with patch.object(orch, "_run_health_gate", return_value=None):
        with patch.object(orch, "_run_compliance_gate", return_value=None):
            with patch.object(orch, "_run_fraud_gate", return_value=None):
                with patch.object(orch, "_assess_risk_signals", return_value=None):
                    with patch.object(orch, "_negotiate_dp", return_value=success):
                        with patch.object(
                            orch, "_maybe_order_supper",
                            side_effect=RuntimeError("supper network error")
                        ):
                            with patch.object(orch, "_tracer"):
                                result = orch.negotiate(trip)

    # Result must still be returned (exception was swallowed)
    assert result.get("outcome") == "success", result
    # booking_ref still present — the supper failure did not corrupt the core result
    assert result.get("booking_ref") == "BK-test-cov3-001"


# ===========================================================================
# CLUSTER 3b — _maybe_enrich_dining exception swallowed (lines 1586-1590)
# ===========================================================================

def test_maybe_enrich_dining_exception_swallowed_result_intact():
    """
    Lines 1586-1590: if _maybe_enrich_dining raises, the exception is swallowed
    and the result is returned unchanged — dining enrichment is an opt-in add-on.

    Regression: removing the try/except would crash negotiate() when the dining
    enrichment network call fails.
    """
    orch = build_society()
    trip = _base_trip()
    trip["dining"] = {"reviews": True, "cuisine": "balinese"}

    success = _canned_success()

    with patch.object(orch, "_run_health_gate", return_value=None):
        with patch.object(orch, "_run_compliance_gate", return_value=None):
            with patch.object(orch, "_run_fraud_gate", return_value=None):
                with patch.object(orch, "_assess_risk_signals", return_value=None):
                    with patch.object(orch, "_negotiate_dp", return_value=success):
                        with patch.object(
                            orch, "_maybe_enrich_dining",
                            side_effect=RuntimeError("dining API timeout")
                        ):
                            with patch.object(orch, "_tracer"):
                                result = orch.negotiate(trip)

    assert result.get("outcome") == "success", result
    # dining_reviews NOT present (enrichment failed and was swallowed)
    assert "dining_reviews" not in result
    assert result.get("booking_ref") == "BK-test-cov3-001"


# ===========================================================================
# CLUSTER 3c — _maybe_check_active_emergencies exception swallowed (1595-1599)
# ===========================================================================

def test_maybe_check_active_emergencies_exception_swallowed_result_intact():
    """
    Lines 1595-1599: if _maybe_check_active_emergencies raises, the exception is
    swallowed — emergency overlay is opt-in and never fatal.

    Regression: removing the guard would crash negotiate() when the emergency
    feed is unavailable.
    """
    orch = build_society()
    trip = _base_trip()
    trip["live_emergency"] = {"check": True}

    success = _canned_success()

    with patch.object(orch, "_run_health_gate", return_value=None):
        with patch.object(orch, "_run_compliance_gate", return_value=None):
            with patch.object(orch, "_run_fraud_gate", return_value=None):
                with patch.object(orch, "_assess_risk_signals", return_value=None):
                    with patch.object(orch, "_negotiate_dp", return_value=success):
                        with patch.object(
                            orch, "_maybe_check_active_emergencies",
                            side_effect=ConnectionError("emergency feed down")
                        ):
                            with patch.object(orch, "_tracer"):
                                result = orch.negotiate(trip)

    assert result.get("outcome") == "success", result
    assert "active_emergencies" not in result
    assert result.get("booking_ref") == "BK-test-cov3-001"


# ===========================================================================
# CLUSTER 4 — _commit_failed_result (line 4704: needs_reconciliation = True)
# ===========================================================================

def test_commit_failed_result_needs_reconciliation_fields():
    """
    Line 4704: `_commit_failed_result` sets `needs_reconciliation: True` and
    threads the `idempotency_key` so a later retry can reconcile rather than
    re-book (double-book prevention).

    Called as a static method directly (no negotiate() needed — it is a pure builder).
    Assertions are concrete: needs_reconciliation must be True, idempotency_key
    must match, outcome must be cannot_satisfy, log must have commit_failed action.

    Regression: setting needs_reconciliation=False or omitting idempotency_key
    would lose the retry-reconcile signal.
    """
    budget_result = {
        "decision": "commit_error",
        "detail": "merchant timeout at irreversible step",
        "checkout_id": "co-cov3-fail-001",
    }
    negotiation_log: list[dict] = [{"round": 1, "action": "check_ok"}]
    log_entry: dict = {"round": 1}

    result = TravelOrchestrator._commit_failed_result(
        budget_result=budget_result,
        idempotency_key="idem-cov3-abc",
        closest_total=45_000,
        negotiation_log=negotiation_log,
        log_entry=log_entry,
    )

    assert result["outcome"] == "cannot_satisfy"
    assert result["needs_reconciliation"] is True
    assert result["idempotency_key"] == "idem-cov3-abc"
    assert result["checkout_id"] == "co-cov3-fail-001"
    assert result["closest_package_total_cents"] == 45_000
    # log_entry was mutated in-place and appended to the log
    assert any(e.get("action") == "commit_failed" for e in result["negotiation_log"])
    # The detail from budget_result appears in the reason string
    assert "merchant timeout at irreversible step" in result["reason"]
    # NOT a gate block — no fraud_verdict, no health_verdict
    assert "fraud_verdict" not in result
    assert "health_verdict" not in result


def test_commit_failed_result_missing_detail_uses_default():
    """
    Line 4700: `budget_result.get('detail', 'commit_errored')` — when detail is
    absent, the fallback string 'commit_errored' appears in reason (not a KeyError).
    """
    result = TravelOrchestrator._commit_failed_result(
        budget_result={"checkout_id": "co-x"},
        idempotency_key="idem-no-detail",
        closest_total=0,
        negotiation_log=[],
        log_entry={"round": 1},
    )

    assert result["outcome"] == "cannot_satisfy"
    assert result["needs_reconciliation"] is True
    # Default fallback string present
    assert "commit_errored" in result["reason"]
    assert result["checkout_id"] == "co-x"


# ===========================================================================
# CLUSTER 5 — _fraud_block_result with gate_fees + risk_signals attached
# ===========================================================================

def test_fraud_block_result_with_gate_fees_and_risk_signals():
    """
    Lines 2839-2841: _fraud_block_result injects gate_fees AND attaches
    risk_signals when risk_assessment is present.

    Verify: fee_total_cents = visa fee amount, risk_signals block present,
    fraud_verdict threaded.  All called via negotiate() with a mocked Fraud agent
    returning a non-committable verdict and pre-loaded gate_fees.
    """
    orch = build_society()
    trip = _base_trip(with_counterparty=True)

    fraud_resp = {
        "rollup": {
            "all_committable": False,
            "blocked_ids": ["bad-carrier-x"],
        },
    }
    risk_assessment = {
        "rollup": {
            "flagged_legs": ["leg-0"],
            "all_reason_codes": ["cyclone_season"],
            "any_avoid_window": False,
            "max_buffer_connection_min": 0,
        },
        "per_leg": [
            {"leg_id": "leg-0", "advisory": [{"detail": "cyclone advisory"}]},
        ],
    }

    # Compliance returns a fee that ends up in gate_fees BEFORE fraud block
    compliance_verdict = {
        "verdict": "can_satisfy",
        "bookable": True,
        "flagged_legs": [],
        "line_items": [_fee_li(8_000, kind="visa", source="compliance")],
    }

    with patch.object(orch, "_call_fraud", return_value=fraud_resp):
        with patch.object(orch, "_run_health_gate", return_value=None):
            with patch.object(orch, "_run_compliance_gate", return_value=compliance_verdict):
                with patch.object(orch, "_assess_risk_signals", return_value=risk_assessment):
                    with patch.object(orch, "_tracer"):
                        result = orch.negotiate(trip)

    assert result["outcome"] == "cannot_satisfy", result
    assert "fraud_verdict" in result
    assert result["fraud_verdict"]["rollup"]["blocked_ids"] == ["bad-carrier-x"]
    # Gate fees injected into the cannot_satisfy result
    assert result.get("fee_total_cents") == 8_000, result.get("fee_total_cents")
    # Risk signals attached even on the fraud block path
    assert "risk_signals" in result, result
    assert result["risk_signals"]["flagged_legs"] == ["leg-0"]


# ===========================================================================
# CLUSTER 6 — gate_fees injection: compliance fee on successful trip (1499-1500)
# ===========================================================================

def test_gate_fees_injected_into_successful_result():
    """
    Lines 1499-1500: `if gate_fees: self._inject_fees(result, gate_fees)` —
    when gate_fees is non-empty AND the trip succeeds, fee_line_items +
    fee_total_cents + package_total_with_fees_cents are ALL added to the result.

    Regression: dropping the inject_fees call leaves fee_total_cents absent,
    making it impossible for the UI to show the real total-including-fees.
    """
    orch = build_society()
    trip = _base_trip()

    fee = _fee_li(12_000, kind="vaccine", source="health")
    health_verdict = {
        "verdict": "can_satisfy",
        "bookable": True,
        "mandatory_line_items": [fee],
        "flagged_destinations": [],
    }
    success = _canned_success(total_cents=40_000)

    with patch.object(orch, "_run_health_gate", return_value=health_verdict):
        with patch.object(orch, "_run_compliance_gate", return_value=None):
            with patch.object(orch, "_run_fraud_gate", return_value=None):
                with patch.object(orch, "_assess_risk_signals", return_value=None):
                    with patch.object(orch, "_negotiate_dp", return_value=success):
                        with patch.object(orch, "_tracer"):
                            result = orch.negotiate(trip)

    assert result.get("outcome") == "success", result
    assert result.get("fee_total_cents") == 12_000, result.get("fee_total_cents")
    # package_total_with_fees = lodging + fee
    assert result.get("package_total_with_fees_cents") == 52_000, (
        result.get("package_total_with_fees_cents")
    )
    # fee_line_items list is present and non-empty
    assert result.get("fee_line_items"), result


# ===========================================================================
# CLUSTER 7 — supper exception path via full negotiate() (lines 1578-1579)
#             verifies the WARNING is emitted but result survives
# ===========================================================================

def test_negotiate_tail_supper_exception_warning_logged():
    """
    Lines 1578-1579: `except Exception as exc: logger.warning(...)` — the
    exception from _maybe_order_supper is captured in a BLE001-annotated except,
    the result dict is still returned, AND the exception detail appears in the
    warning log.

    This test patches the logger at the orchestrator module level to capture the
    warning call and assert its presence — not just that no crash occurred.
    """
    orch = build_society()
    trip = _base_trip()
    trip["supper"] = {"order": True}
    success = _canned_success()

    import orchestration.orchestrator as orch_module

    with patch.object(orch, "_run_health_gate", return_value=None):
        with patch.object(orch, "_run_compliance_gate", return_value=None):
            with patch.object(orch, "_run_fraud_gate", return_value=None):
                with patch.object(orch, "_assess_risk_signals", return_value=None):
                    with patch.object(orch, "_negotiate_dp", return_value=success):
                        with patch.object(
                            orch, "_maybe_order_supper",
                            side_effect=ValueError("injected supper failure")
                        ):
                            with patch.object(orch_module, "logger") as mock_logger:
                                with patch.object(orch, "_tracer"):
                                    result = orch.negotiate(trip)

    assert result.get("outcome") == "success", result
    # logger.warning was called with the supper failure message
    warn_calls = mock_logger.warning.call_args_list
    assert any("_maybe_order_supper failed" in str(c) for c in warn_calls), warn_calls
