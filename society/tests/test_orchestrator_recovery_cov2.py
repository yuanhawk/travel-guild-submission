"""
test_orchestrator_recovery_cov2.py — COMPLEMENTARY coverage extension for the
orchestrator recovery / contextual-overlay sub-areas. Companion to
test_orchestrator_cov2.py: that file covers the happy/primary branch of each
method; THIS file targets the SECONDARY / fail-conservative / no-op / pass-through
branches in the SAME sub-areas so the recovery + honest-degradation edges are
exercised too:

  - _apply_insurance: no-agent-wired pass-through, _call_insurance→None,
    premium-with-NO-line_item (no fee injection), unmapped reason→empty perils,
    user_policy set but coverage-gap→None.
  - _gate_block_result: health block with NO fees + NO risk_assessment (no
    risk_signals), reason-string content.
  - _do_not_recommend_block_result: single-country advisory + DO_NOT_TRAVEL level.
  - _maybe_order_supper: failed-result no-op, request-absent no-op, order=true but
    NO supper available (honest "nothing was charged").
  - _place_supper_order: full success commit path, budget.check RAISES.
  - _maybe_check_active_emergencies: request-absent no-op, check-falsy no-op,
    multi-leg mixed (active + clear + unavailable).
  - _maybe_enrich_dining: reviews-falsy no-op, cuisine-missing no-op,
    failed-result no-op, provider-venue with NO plan match (external=True),
    empty-surfaced honest note.

Design:
  build_society() (full society, all agents as TestClients) + method-level mocks
  of the seam callers (_call_insurance/_call_coverage_gap/_call_food_search/
  _call_budget_check/_do_commit/_call_emergency_feed/_call_dining_reviews) — all
  deterministic, var-0-safe. NO live network / LLM / paid-API / real merchant.
  NO golden/benchmark mutation; no asserts on booking_ref nonces.
  Verify: cd society && python -m pytest tests/test_orchestrator_recovery_cov2.py \
            -q -p no:xdist -p no:cacheprovider
"""

from __future__ import annotations

import os
import sys
import uuid
from typing import Any
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from starlette.testclient import TestClient
from agents.planner_agent import PlannerAgent
from agents.accommodation_agent import AccommodationAgent
from agents.budget_agent import BudgetAgent
from agents.critic_agent import CriticAgent
from agents.destination_agent import DestinationAgent
from agents.compliance_agent import ComplianceAgent
from agents.health_agent import HealthAgent
from agents.insurance_agent import InsuranceAgent
from agents.fraud_agent import FraudAgent
from agents.risk_agent import RiskAgent
from agents.transport_agent import TransportAgent
from agents.day_planner_agent import DayPlannerAgent
from orchestration.orchestrator import TravelOrchestrator
from core import contracts as C


def build_society():
    """Construct full society with all agents as TestClients (var-0, no merchant)."""
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


def make_usd_money(cents: int) -> dict[str, Any]:
    """Canonical USD money tag (native=USD, fx_rate=1.0) via the contract helper."""
    return C.make_money(
        native_cents=cents,
        currency="USD",
        usd_cents=cents,
        fx_rate=1.0,
        fx_source="seeded",
        fetched_at="2026-06-20",
    )


# ===========================================================================
# _apply_insurance — SECONDARY branches (pass-through / fail-conservative)
# ===========================================================================

def test_apply_insurance_no_agent_wired_pass_through():
    """No Insurance agent wired (client None + url None) → byte-identical pass-through
    even with peril signals present (orchestrator.py:2117-2118)."""
    orch = build_society()
    orch._insurance_client = None
    orch._insurance_url = None

    result = {"outcome": "success", "total_cents": 400000, "fee_line_items": []}
    risk_assessment = {"rollup": {"all_reason_codes": ["natural_disaster"]}, "per_leg": []}

    out = orch._apply_insurance(result, risk_assessment)
    assert out is result
    assert "insurance" not in out
    assert "fee_total_cents" not in out


def test_apply_insurance_call_returns_none_pass_through():
    """Insurance agent wired + peril present, but _call_insurance returns None
    (assessment unavailable) → no insurance key, no fee (orchestrator.py:2151-2152)."""
    orch = build_society()

    result = {"outcome": "success", "total_cents": 400000, "fee_line_items": []}
    risk_assessment = {"rollup": {"all_reason_codes": ["flood"]}, "per_leg": []}

    with patch.object(orch, "_call_insurance", return_value=None) as mock_ins:
        with patch.object(orch, "_tracer"):
            out = orch._apply_insurance(result, risk_assessment)

    assert mock_ins.called  # the peril branch WAS entered
    assert "insurance" not in out
    assert "fee_total_cents" not in out


def test_apply_insurance_premium_without_line_item_no_fee_injection():
    """Premium attached but assessment carries NO line_item → insurance key is added
    but the fee-injection block is SKIPPED (orchestrator.py:2180-2181 guard)."""
    orch = build_society()

    result = {"outcome": "success", "total_cents": 200000, "fee_line_items": []}
    risk_assessment = {"rollup": {"all_reason_codes": ["health_hazard"]}, "per_leg": []}

    with patch.object(orch, "_call_insurance") as mock_ins:
        mock_ins.return_value = {
            "premium_cents": 15000,
            "premium_money": make_usd_money(15000),
            "excluded_perils_summary": None,
            "line_item": None,  # NO line item → no fee injection
        }
        with patch.object(orch, "_tracer"):
            out = orch._apply_insurance(result, risk_assessment)

    assert "insurance" in out
    assert out["insurance"]["premium_cents"] == 15000
    assert out["insurance"]["line_item"] is None
    # fee-injection block never ran → no package_total_with_fees / fee_total keys
    assert "fee_total_cents" not in out
    assert "package_total_with_fees_cents" not in out


def test_apply_insurance_assess_call_raises_flags_honestly_not_silent():
    """M6: on a peril-flagged, ALREADY-BOOKED trip (outcome=success + non-empty
    peril crosswalk), a RAISING insurance.assess_coverage call must NOT be a
    silent no-op. Must set result["insurance_assessment_failed"]=True and
    append an honest advisory — the old behaviour returned `result` completely
    unchanged (no insurance key, no advisory, no signal the assessment even
    ran), indistinguishable from a hazard-free trip."""
    orch = build_society()

    result = {"outcome": "success", "total_cents": 300000, "fee_line_items": []}
    risk_assessment = {"rollup": {"all_reason_codes": ["flood"]}, "per_leg": []}

    with patch.object(orch, "_call_insurance", side_effect=RuntimeError("insurance agent down")):
        with patch.object(orch, "_tracer"):
            out = orch._apply_insurance(result, risk_assessment)

    assert out is result  # same dict, mutated in place (matches existing pattern)
    assert out["insurance_assessment_failed"] is True
    assert any(
        "coverage assessment could not be completed" in a for a in out.get("advisories", [])
    ), out.get("advisories")
    # Still no fabricated insurance/premium data.
    assert "insurance" not in out
    assert "fee_total_cents" not in out


def test_apply_insurance_unmapped_reason_crosswalk_raises_pass_through():
    """A reason_code that is NOT a valid RiskReasonCode makes risk_reasons_to_perils
    RAISE (KeyError, closed-set guard) → the orchestrator's except-branch logs + returns
    the result unchanged (orchestrator.py:2131-2133). NO insurance key, _call_insurance
    never reached.

    NOTE (drift / dead-branch): the SEPARATE empty-perils guard at orchestrator.py:
    2134-2135 (`if not perils: return result`) is effectively UNREACHABLE in production:
    peril_crosswalk is asserted TOTAL over RiskReasonCode (every valid code maps to
    exactly one peril), and any INVALID code raises before reaching that line. Recorded
    as a finding rather than forced (would require a non-total crosswalk to hit)."""
    orch = build_society()

    result = {"outcome": "success", "total_cents": 300000, "fee_line_items": []}
    # 'totally_not_a_real_reason' is not a RiskReasonCode → no peril produced.
    risk_assessment = {
        "rollup": {"all_reason_codes": ["totally_not_a_real_reason"]},
        "per_leg": [],
    }

    with patch.object(orch, "_call_insurance") as mock_ins:
        with patch.object(orch, "_tracer"):
            out = orch._apply_insurance(result, risk_assessment)

    assert "insurance" not in out
    # _call_insurance must NOT have been reached (no peril → early return).
    assert not mock_ins.called


def test_apply_insurance_user_policy_gap_none_no_coverage_key():
    """user_policy set, premium minted, but _call_coverage_gap returns None → the
    coverage_gap key is NOT added (orchestrator.py:2205 false branch), while the
    premium/insurance key + fee are still present."""
    orch = build_society()
    orch._user_policy = {"provider": "ACME", "exclusions": ["war"]}

    result = {"outcome": "success", "total_cents": 250000, "fee_line_items": []}
    risk_assessment = {"rollup": {"all_reason_codes": ["disease_outbreak"]}, "per_leg": []}

    with patch.object(orch, "_call_insurance") as mock_ins:
        with patch.object(orch, "_call_coverage_gap", return_value=None) as mock_gap:
            mock_ins.return_value = {
                "premium_cents": 22000,
                "premium_money": make_usd_money(22000),
                "excluded_perils_summary": "war excluded",
                "line_item": {
                    "source": "insurance", "kind": "premium",
                    "money": make_usd_money(22000),
                    "description": "Travel insurance premium",
                },
            }
            with patch.object(orch, "_tracer"):
                out = orch._apply_insurance(result, risk_assessment)

    assert mock_gap.called  # the user_policy branch WAS entered
    assert "insurance" in out
    assert out["fee_total_cents"] == 22000
    assert "coverage_gap" not in out  # None report → no key


def test_apply_insurance_premium_pushes_committed_total_over_budget_flags_honestly():
    """M4: #72's pre-commit envelope only guards the budget when the PRE-COMMIT
    premium estimate succeeds. If that estimate call failed (silently degraded
    to 0 — see _estimate_insurance_premium_cents), lodging can book right up to
    the full budget; when THIS (separate, post-booking) insurance.assess_coverage
    call then succeeds and injects a real premium, the committed total can end
    up over the user's stated budget. Must be surfaced HONESTLY (a flag +
    advisory), not silently absorbed — this is the M4 regression: previously
    NOTHING detected or reported this."""
    orch = build_society()

    total_budget_cents = 200000
    # The trip was booked right up to the full budget (envelope=0 because the
    # pre-commit estimate call had failed) — total_cents already == budget.
    result = {
        "outcome": "success",
        "total_cents": total_budget_cents,
        "fee_line_items": [],
    }
    risk_assessment = {"rollup": {"all_reason_codes": ["flood"]}, "per_leg": []}

    with patch.object(orch, "_call_insurance") as mock_ins:
        mock_ins.return_value = {
            "premium_cents": 5000,  # pushes the committed total $50 over budget
            "premium_money": make_usd_money(5000),
            "excluded_perils_summary": None,
            "line_item": {
                "source": "insurance", "kind": "premium",
                "money": make_usd_money(5000),
                "description": "Travel insurance premium",
            },
        }
        with patch.object(orch, "_tracer"):
            out = orch._apply_insurance(result, risk_assessment, total_budget_cents)

    assert out["package_total_with_fees_cents"] == total_budget_cents + 5000
    assert out["insurance"]["premium_exceeds_budget"] is True
    assert out["insurance"]["over_budget_cents"] == 5000
    assert any("over your stated budget" in a for a in out.get("advisories", [])), (
        f"expected an honest over-budget advisory, got {out.get('advisories')!r}"
    )


def test_apply_insurance_within_budget_no_over_budget_flag():
    """Negative case for M4: when the committed total (with premium) stays
    within total_budget_cents, no over-budget flag/advisory is added — the
    fix is purely additive/honesty, never a false alarm."""
    orch = build_society()

    total_budget_cents = 500000
    result = {"outcome": "success", "total_cents": 200000, "fee_line_items": []}
    risk_assessment = {"rollup": {"all_reason_codes": ["flood"]}, "per_leg": []}

    with patch.object(orch, "_call_insurance") as mock_ins:
        mock_ins.return_value = {
            "premium_cents": 5000,
            "premium_money": make_usd_money(5000),
            "excluded_perils_summary": None,
            "line_item": {
                "source": "insurance", "kind": "premium",
                "money": make_usd_money(5000),
                "description": "Travel insurance premium",
            },
        }
        with patch.object(orch, "_tracer"):
            out = orch._apply_insurance(result, risk_assessment, total_budget_cents)

    assert "premium_exceeds_budget" not in out["insurance"]
    assert "advisories" not in out


# ===========================================================================
# _gate_block_result — health block, NO fees, NO risk_assessment
# ===========================================================================

def test_gate_block_result_health_no_fees_no_risk_signals():
    """Health block with EMPTY gate_fees and risk_assessment=None: cannot_satisfy
    terminal, health_verdict attached, log entry present, BUT no fee keys and NO
    risk_signals (orchestrator.py:2747 guard false + _attach_risk_signals no-op)."""
    orch = build_society()

    verdict = {
        "verdict": "cannot_satisfy",
        "bookable": False,
        "blocking_legs": ["leg-0"],
    }
    negotiation_log: list[dict] = []

    out = orch._gate_block_result(
        gate="health",
        verdict=verdict,
        negotiation_log=negotiation_log,
        gate_fees=[],          # no fees → injection skipped
        risk_assessment=None,  # no risk signals attached
    )

    assert out["outcome"] == "cannot_satisfy"
    assert "Health gate BLOCKED the trip" in out["reason"]
    assert out["health_verdict"] is verdict
    assert any(e.get("action") == "health_gate_block" for e in out["negotiation_log"])
    # No fees were injected.
    assert "fee_total_cents" not in out
    # risk_assessment None → no risk_signals block.
    assert "risk_signals" not in out


def test_gate_block_result_compliance_no_resequence_no_key():
    """Compliance block whose verdict has NO resequence field → the resequence key is
    NOT attached (orchestrator.py:2745 false), but the compliance_verdict + log are."""
    orch = build_society()

    verdict = {
        "verdict": "cannot_satisfy",
        "bookable": False,
        "blocking": ["leg-1: eVisa lead time"],
    }
    out = orch._gate_block_result(
        gate="compliance",
        verdict=verdict,
        negotiation_log=[],
        gate_fees=[],
        risk_assessment=None,
    )

    assert out["outcome"] == "cannot_satisfy"
    assert out["compliance_verdict"] is verdict
    assert "resequence" not in out
    assert any(e.get("action") == "compliance_gate_block" for e in out["negotiation_log"])


# ===========================================================================
# _do_not_recommend_block_result — single-country advisory terminal
# ===========================================================================

def test_do_not_recommend_block_result_single_country_advisory():
    """Single declined country: declined terminal, advisory names the country, war
    exclusion + DO_NOT_TRAVEL level + uninsurable marker, trip_id threaded."""
    orch = build_society()
    trip_id = "trip-" + str(uuid.uuid4())[:8]

    out = orch._do_not_recommend_block_result(
        declined_countries=["AF"],
        trip_id=trip_id,
    )

    assert out["outcome"] == "declined"
    assert out["bookable"] is False
    assert out["declined"] is True
    assert out["trip_id"] == trip_id
    assert out["declined_countries"] == ["AF"]
    assert out["advisory_level"] == "DO_NOT_TRAVEL"
    assert out["war_exclusion"] == "EXC-WAR-2"
    assert out["insurance_available"] is False
    assert "AF" in out["advisory"]
    assert "EXC-WAR-2" in out["advisory"]
    # reason string carries the armed-conflict rationale.
    assert "EXC-WAR-2" in out["reason"]
    assert any(
        e.get("action") == "do_not_recommend_decline"
        and e.get("war_exclusion") == "EXC-WAR-2"
        for e in out.get("negotiation_log", [])
    )


# ===========================================================================
# _maybe_order_supper — no-op + order-but-unavailable branches
# ===========================================================================

def test_maybe_order_supper_failed_result_no_op():
    """Trip did NOT book (outcome != success) → supper is never ordered/selected
    (orchestrator.py:2263-2264). NO _call_food_search, no supper key."""
    orch = build_society()
    orch._supper_request = {"order": True, "city": "osaka", "max_cents": 5000}
    orch._trip_request_legs = [{"city": "osaka"}]

    result = {"outcome": "cannot_satisfy", "reason": "no fit"}
    with patch.object(orch, "_call_food_search") as mock_search:
        orch._maybe_order_supper(result, user_id="u", idempotency_key="k")

    assert "supper" not in result
    assert not mock_search.called


def test_maybe_order_supper_request_absent_no_op():
    """No supper request on the orchestrator → byte-identical no-op even for a booked
    trip (orchestrator.py:2261-2262)."""
    orch = build_society()
    orch._supper_request = None

    result = {"outcome": "success", "total_cents": 100000}
    with patch.object(orch, "_call_food_search") as mock_search:
        orch._maybe_order_supper(result, user_id="u", idempotency_key="k")

    assert "supper" not in result
    assert not mock_search.called


def test_maybe_order_supper_order_true_but_unavailable():
    """order=true but the merchant returned NO rows → build_supper_order yields
    available=False → honest 'No supper available to order; nothing was charged.'
    and ordered=False, NO checkout attempted (orchestrator.py:2307-2310)."""
    orch = build_society()
    orch._trip_id = "trip-supper-none"
    orch._trip_request_legs = [{"city": "reykjavik", "checkin": "2026-08-01",
                                "checkout": "2026-08-03"}]
    orch._supper_request = {"order": True, "city": "reykjavik", "max_cents": 4000}

    result = {"outcome": "success", "total_cents": 90000}
    with patch.object(orch, "_call_food_search") as mock_search:
        mock_search.return_value = {"results": [], "disclosure": "Simulated inventory"}
        with patch.object(orch, "_place_supper_order") as mock_place:
            with patch.object(orch, "_tracer"):
                orch._maybe_order_supper(result, user_id="u", idempotency_key="k")

    assert "supper" in result
    supper = result["supper"]
    assert supper.get("available") is False
    assert supper.get("ordered") is False
    assert "nothing was charged" in supper.get("order_note", "").lower()
    # No checkout attempted for an unavailable supper.
    assert not mock_place.called


# ===========================================================================
# _place_supper_order — success commit + budget.check RAISES
# ===========================================================================

def test_place_supper_order_success_commit():
    """budget.check ok + commit accept → ordered=True with booking_ref/checkout_id and
    charged_cents from the commit (orchestrator.py:2397-2402). booking_ref identity is
    NOT asserted (nonce), only presence + the charged amount."""
    orch = build_society()
    orch._trip_id = "trip-supper-ok"

    supper = {
        "available": True,
        "total_cents": 5000,
        "line_item": {
            "source": "food_delivery", "kind": "FOOD_DELIVERY",
            "food_id": "food-ok", "qty": 1, "money": make_usd_money(5000),
        },
    }
    with patch.object(orch, "_call_budget_check") as mock_check:
        with patch.object(orch, "_do_commit") as mock_commit:
            mock_check.return_value = {"decision": "check_ok", "checkout_id": "chk-1"}
            mock_commit.return_value = {
                "decision": "accept",
                "booking_ref": "SBR-success",
                "checkout_id": "chk-1",
                "total_cents": 5000,
            }
            with patch.object(orch, "_tracer"):
                out = orch._place_supper_order(
                    supper=supper, user_id="u",
                    idempotency_key="k", max_cents=6000,
                )

    assert out["ordered"] is True
    assert out.get("supper_booking_ref")  # present (nonce — value not asserted)
    assert out.get("supper_checkout_id") == "chk-1"
    assert out["charged_cents"] == 5000


def test_place_supper_order_budget_check_raises():
    """budget.check RAISES → honest fail-conservative terminal: ordered=False +
    'nothing was charged', NO commit attempted (orchestrator.py:2370-2373)."""
    orch = build_society()
    orch._trip_id = "trip-supper-raise"

    supper = {
        "available": True,
        "total_cents": 5000,
        "line_item": {
            "source": "food_delivery", "kind": "FOOD_DELIVERY",
            "food_id": "food-x", "qty": 1, "money": make_usd_money(5000),
        },
    }
    with patch.object(orch, "_call_budget_check", side_effect=RuntimeError("seam down")):
        with patch.object(orch, "_do_commit") as mock_commit:
            with patch.object(orch, "_tracer"):
                out = orch._place_supper_order(
                    supper=supper, user_id="u",
                    idempotency_key="k", max_cents=6000,
                )

    assert out["ordered"] is False
    assert "nothing was charged" in out.get("order_note", "").lower()
    assert not mock_commit.called


# ===========================================================================
# _maybe_check_active_emergencies — no-op + multi-leg mixed
# ===========================================================================

def test_maybe_check_active_emergencies_request_absent_no_op():
    """No emergency request → byte-identical no-op (orchestrator.py:2605-2606)."""
    orch = build_society()
    orch._emergency_request = None

    result = {"outcome": "success", "day_plans": [{"leg_id": "leg-0", "city": "oslo"}]}
    with patch.object(orch, "_call_emergency_feed") as mock_feed:
        orch._maybe_check_active_emergencies(result)

    assert "active_emergencies" not in result
    assert not mock_feed.called


def test_maybe_check_active_emergencies_check_falsy_no_op():
    """Emergency request present but check=False → no-op (orchestrator.py:2607-2608)."""
    orch = build_society()
    orch._emergency_request = {"check": False}

    result = {"outcome": "success", "day_plans": [{"leg_id": "leg-0", "city": "oslo"}]}
    with patch.object(orch, "_call_emergency_feed") as mock_feed:
        orch._maybe_check_active_emergencies(result)

    assert "active_emergencies" not in result
    assert not mock_feed.called


def test_maybe_check_active_emergencies_multi_leg_mixed():
    """Three legs returning active / clear / unavailable respectively → one entry per
    leg with the right status; exercises all three branches in one pass + the per-leg
    iteration (orchestrator.py:2611-2647)."""
    orch = build_society()
    orch._trip_id = "trip-emergency-multi"
    orch._emergency_request = {"check": True}

    result = {
        "outcome": "success",
        "day_plans": [
            {"leg_id": "leg-0", "city": "manila", "iso2": "PH",
             "region": "PH", "checkin": "2026-09-01", "checkout": "2026-09-03", "days": []},
            {"leg_id": "leg-1", "city": "tokyo", "iso2": "JP",
             "region": "JP", "checkin": "2026-09-03", "checkout": "2026-09-06", "days": []},
            {"leg_id": "leg-2", "city": "seoul", "iso2": "KR",
             "region": "KR", "checkin": "2026-09-06", "checkout": "2026-09-08", "days": []},
        ],
    }

    def _feed(query):
        if query.get("iso2") == "PH":
            return {"active": True, "hazard": "typhoon", "severity": "HIGH",
                    "headline": "Typhoon warning", "source": "PAGASA",
                    "as_of": "2026-09-01T00:00:00Z"}
        if query.get("iso2") == "JP":
            return {"active": False, "source": "JMA", "as_of": "2026-09-03T00:00:00Z"}
        return None  # KR feed unconfigured

    with patch.object(orch, "_call_emergency_feed", side_effect=_feed):
        with patch.object(orch, "_tracer"):
            orch._maybe_check_active_emergencies(result)

    entries = result["active_emergencies"]
    assert len(entries) == 3
    by_leg = {e["leg_id"]: e for e in entries}
    assert by_leg["leg-0"]["status"] == "active"
    assert by_leg["leg-0"]["hazard"] == "typhoon"
    assert "DO NOT TRAVEL" in by_leg["leg-0"]["notice"]
    assert by_leg["leg-1"]["status"] == "clear"
    assert "No active emergency reported" in by_leg["leg-1"]["note"]
    assert by_leg["leg-2"]["status"] == "unavailable"
    assert "no feed configured" in by_leg["leg-2"]["note"].lower()


# ===========================================================================
# _maybe_enrich_dining — no-op gates + external-venue + empty-surfaced note
# ===========================================================================

def test_maybe_enrich_dining_reviews_falsy_no_op():
    """dining request present but reviews=False → no-op (orchestrator.py:2522-2523)."""
    orch = build_society()
    orch._dining_request = {"reviews": False, "cuisine": "thai"}

    result = {"outcome": "success", "day_plans": [{"leg_id": "leg-0", "city": "bkk"}]}
    with patch.object(orch, "_call_dining_reviews") as mock_rev:
        orch._maybe_enrich_dining(result)

    assert "dining_reviews" not in result
    assert not mock_rev.called


def test_maybe_enrich_dining_cuisine_missing_no_op():
    """reviews=True but no cuisine pref → the interactive-consent gate keeps it a no-op
    (orchestrator.py:2524-2526)."""
    orch = build_society()
    orch._dining_request = {"reviews": True}  # no cuisine

    result = {"outcome": "success", "day_plans": [{"leg_id": "leg-0", "city": "bkk"}]}
    with patch.object(orch, "_call_dining_reviews") as mock_rev:
        orch._maybe_enrich_dining(result)

    assert "dining_reviews" not in result
    assert not mock_rev.called


def test_maybe_enrich_dining_failed_result_no_op():
    """Trip did not book → dining never enriched (orchestrator.py:2527-2528)."""
    orch = build_society()
    orch._dining_request = {"reviews": True, "cuisine": "italian"}

    result = {"outcome": "cannot_satisfy", "day_plans": [{"leg_id": "leg-0", "city": "rome"}]}
    with patch.object(orch, "_call_dining_reviews") as mock_rev:
        orch._maybe_enrich_dining(result)

    assert "dining_reviews" not in result
    assert not mock_rev.called


def test_maybe_enrich_dining_provider_venue_no_match_external():
    """A provider venue that does NOT match any plan venue (no name/lat-lon overlap) is
    surfaced as external=True (provider-only), NOT injected into day_plans
    (orchestrator.py:2561-2566 + _dining_match false). day_plans stays untouched."""
    orch = build_society()
    orch._trip_id = "trip-dining-external"
    orch._dining_request = {"reviews": True, "cuisine": "thai"}

    plan_meal = {"name": "Green Curry House", "lat": 13.7563, "lon": 100.5018,
                 "cuisine": "thai"}
    result = {
        "outcome": "success",
        "day_plans": [{
            "leg_id": "leg-0", "city": "bangkok", "iso2": "TH", "region": "TH",
            "days": [{"meals": {"dinner": dict(plan_meal)}}],
        }],
    }

    with patch.object(orch, "_call_dining_reviews") as mock_rev:
        # Provider venue: different name AND far-away coords → no match.
        mock_rev.return_value = {
            "source": "google",
            "venues": [{"name": "Completely Different Eatery",
                        "lat": 40.0, "lon": -70.0,
                        "rating": 4.2, "review_count": 88}],
        }
        with patch.object(orch, "_tracer"):
            orch._maybe_enrich_dining(result)

    entry = result["dining_reviews"][0]
    assert entry["provider"] == "google"
    assert len(entry["venues"]) == 1
    venue = entry["venues"][0]
    assert venue["external"] is True  # provider-only, not in the deterministic plan
    assert venue["rating"] == 4.2
    # The deterministic plan meal was NOT mutated.
    assert result["day_plans"][0]["days"][0]["meals"]["dinner"] == plan_meal


def test_maybe_enrich_dining_empty_venues_honest_note():
    """Provider returns a dict but NO venues → entry with an honest 'no live reviews
    returned for this cuisine' note (orchestrator.py:2571-2572)."""
    orch = build_society()
    orch._trip_id = "trip-dining-empty"
    orch._dining_request = {"reviews": True, "cuisine": "thai"}

    result = {
        "outcome": "success",
        "day_plans": [{
            "leg_id": "leg-0", "city": "bangkok", "iso2": "TH", "region": "TH",
            "days": [{"meals": {"dinner": {"name": "Some Place",
                                           "lat": 13.7, "lon": 100.5}}}],
        }],
    }
    with patch.object(orch, "_call_dining_reviews") as mock_rev:
        mock_rev.return_value = {"source": "google", "venues": []}
        with patch.object(orch, "_tracer"):
            orch._maybe_enrich_dining(result)

    entry = result["dining_reviews"][0]
    assert entry["provider"] == "google"
    assert entry["venues"] == []
    assert "no live reviews returned" in entry.get("note", "").lower()


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v", "-p", "no:xdist", "-p", "no:cacheprovider"])
