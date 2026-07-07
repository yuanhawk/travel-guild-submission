"""
COMPOSED SMOKE — prove a gate fires INSIDE the orchestrated society via
orchestrator.negotiate() (NOT a direct agent.method() call).

Builds the FULL society (all 5 specialist gates wired as TestClients) and routes
DC scenarios through negotiate(). Also proves a no-condition trip (S-like) passes
through unchanged.
"""
import json
import os
import sys
import threading
import uuid
from typing import Any

import httpx
import pytest

# Make the society package importable regardless of the cwd pytest runs from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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


def build_society():
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


def test_compliance_fires_in_society():
    orch = build_society()
    # DC-compliance shape: US passport, depart IN (India) in 3 business days (eVisa needs
    # lead time). India is NOT YF-endemic, so this ISOLATES the visa gate from Health — after
    # #70 widened Health to fire on dest_country, an endemic country (e.g. ET) would health-
    # block first (covered by its own test); India keeps this a clean compliance-gate assertion.
    trip = {
        "user_id": "smoke-dc-compliance",
        "total_budget_cents": 200000,
        "today": "2026-06-16",
        "nationality": "US",
        "legs": [
            {"city": "mumbai", "dest_country": "IN",
             "departure_date": "2026-06-19", "checkin": "2026-06-19",
             "checkout": "2026-06-22", "adults": 1, "vibe": "culture"},
        ],
    }
    res = orch.negotiate(trip)
    print("[COMPLIANCE] outcome=", res.get("outcome"))
    print("[COMPLIANCE] reason=", res.get("reason"))
    cv = res.get("compliance_verdict", {})
    print("[COMPLIANCE] verdict=", cv.get("verdict"), "bookable=", cv.get("bookable"))
    assert res["outcome"] == "cannot_satisfy", res
    assert cv.get("verdict") == "cannot_satisfy", cv
    # Prove it was COMPOSED: routed via negotiate, the log carries the gate action.
    actions = [e.get("action") for e in res.get("negotiation_log", [])]
    print("[COMPLIANCE] log actions=", actions)
    assert "compliance_gate_block" in actions
    print("[COMPLIANCE] PASS — visa gate fired INSIDE negotiate()\n")


def test_health_fires_for_structured_dest_country_endemic_70():
    """#70 SAFETY: a STRUCTURED {city, dest_country} trip (NO place_key) to a YF-endemic
    country must trigger the Health gate. Previously only the free-text path (which sets
    place_key=city) did, so a structured/API trip SILENTLY SKIPPED the vaccine/entry-cert
    mandate (under-warn). Widening Health to fire on dest_country fixes it; the empty-guard
    keeps non-endemic structured trips byte-identical. ET in 3 business days fails the YF-cert
    lead time -> Health blocks (merchant-independent, deterministic)."""
    orch = build_society()
    endemic = {
        "user_id": "h70-et", "total_budget_cents": 200000, "today": "2026-06-16",
        "nationality": "US",
        "legs": [{"city": "addis ababa", "dest_country": "ET",
                  "departure_date": "2026-06-19", "checkin": "2026-06-19",
                  "checkout": "2026-06-22", "adults": 1, "vibe": "culture"}],
    }
    res = orch.negotiate(endemic)
    assert res["outcome"] == "cannot_satisfy", res
    assert "health gate blocked" in (res.get("reason") or "").lower(), res.get("reason")

    # NON-ENDEMIC structured trip (Philippines) -> Health must NOT block/attach (empty-guard;
    # byte-identical to the prior place_key-gated pass-through).
    non_endemic = {
        "user_id": "h70-ph", "total_budget_cents": 200000, "today": "2026-06-16",
        "nationality": "US",
        "legs": [{"city": "manila", "dest_country": "PH",
                  "departure_date": "2026-09-01", "checkin": "2026-09-01",
                  "checkout": "2026-09-04", "adults": 1, "vibe": "relax"}],
    }
    rne = orch.negotiate(non_endemic)
    assert "health gate blocked" not in (rne.get("reason") or "").lower(), rne.get("reason")
    print("[HEALTH-70] PASS — structured endemic dest_country triggers Health; non-endemic passes through\n")


def test_health_fires_in_society():
    orch = build_society()
    trip = {
        "user_id": "smoke-dc-health",
        "total_budget_cents": 200000,
        "today": "2026-06-16",
        "legs": [
            {"city": "addis ababa", "place_key": "ethiopia", "dest_country": "ET",
             "departure_date": "2026-06-23", "checkin": "2026-06-23",
             "checkout": "2026-06-26", "adults": 1, "vibe": "culture"},
        ],
    }
    res = orch.negotiate(trip)
    print("[HEALTH] outcome=", res.get("outcome"))
    hv = res.get("health_verdict", {})
    print("[HEALTH] verdict=", hv.get("verdict"), "bookable=", hv.get("bookable"),
          "vaccine_fee=", hv.get("total_vaccine_cost_usd_cents"),
          "jabs=", len(hv.get("jab_records") or []))
    print("[HEALTH] fee_line_items=", res.get("fee_line_items"))
    assert res["outcome"] == "cannot_satisfy", res
    assert hv.get("verdict") == "cannot_complete", hv
    # FEE-INJECTION: the vaccine fee surfaced even on the block.
    assert res.get("fee_line_items"), "expected vaccine fee injected"
    print("[HEALTH] PASS — health gate fired INSIDE negotiate() + fee injected\n")


def test_fraud_fires_in_society():
    orch = build_society()
    trip = {
        "user_id": "smoke-dc-fraud",
        "total_budget_cents": 200000,
        "today": "2026-06-16",
        # Only the blocked cheapest carrier offered → no committable alternative.
        "carriers": [
            {"counterparty_id": "carrier-skylark-budget-air", "kind": "transport"},
        ],
        "legs": [
            {"city": "bali", "checkin": "2026-07-15", "checkout": "2026-07-18",
             "adults": 1, "vibe": "beach"},
        ],
    }
    res = orch.negotiate(trip)
    print("[FRAUD] outcome=", res.get("outcome"))
    fv = res.get("fraud_verdict", {})
    print("[FRAUD] rollup=", fv.get("rollup"))
    assert res["outcome"] == "cannot_satisfy", res
    assert fv.get("rollup", {}).get("blocked_ids"), fv
    actions = [e.get("action") for e in res.get("negotiation_log", [])]
    assert "fraud_gate_block" in actions, actions
    print("[FRAUD] PASS — fraud advisory gate #1 fired INSIDE negotiate()\n")


def test_no_condition_passthrough():
    """A plain Bali trip (no visa/vaccine/carrier/peril condition) must NOT be
    altered by the gates — no gate keys, no fee block."""
    orch = build_society()
    trip = {
        "user_id": "smoke-passthrough",
        "total_budget_cents": 150000,
        "legs": [
            {"city": "bali", "checkin": "2026-10-01", "checkout": "2026-10-04",
             "adults": 1, "vibe": "culture"},
        ],
    }
    res = orch.negotiate(trip)
    print("[PASSTHROUGH] outcome=", res.get("outcome"))
    # No gate condition present → no gate verdict keys, no injected fees.
    assert "compliance_verdict" not in res, "compliance should pass through"
    assert "health_verdict" not in res, "health should pass through"
    assert "fraud_verdict" not in res, "fraud should pass through"
    assert "fee_line_items" not in res, "no fee should be injected"
    assert "insurance" not in res, "no insurance without a peril signal"
    print("[PASSTHROUGH] PASS — no-condition trip unchanged by gates\n")


def test_health_compliance_interlock():
    """Health→Compliance jab interlock: a leg with BOTH place_key + nationality +
    dest_country exercises Health (jab record) → Compliance (reads the jab for the
    YF entry-cert). Health blocks first here; prove the jab record flows."""
    orch = build_society()
    # Use a date far enough out that Health's cert lead-time clears, so Compliance
    # also runs and consumes the jab record.
    trip = {
        "user_id": "smoke-interlock",
        "total_budget_cents": 200000,
        "today": "2026-06-16",
        "nationality": "US",
        "legs": [
            {"city": "addis ababa", "place_key": "ethiopia", "dest_country": "ET",
             "departure_date": "2026-09-01", "checkin": "2026-09-01",
             "checkout": "2026-09-04", "adults": 1, "vibe": "culture"},
        ],
    }
    res = orch.negotiate(trip)
    hv = res.get("health_verdict", {})
    cv = res.get("compliance_verdict", {})
    print("[INTERLOCK] health bookable=", hv.get("bookable"),
          "jabs=", len(hv.get("jab_records") or []))
    print("[INTERLOCK] compliance bookable=", cv.get("bookable"),
          "handoff=", cv.get("entry_cert_handoff"))
    # Health cleared (far-out date) → Compliance ran → consumed the jab record.
    assert hv.get("jab_records"), "expected Health jab records"
    assert cv.get("entry_cert_handoff"), "expected Compliance to consume jab record"
    assert cv["entry_cert_handoff"]["n_jab_records"] >= 1
    print("[INTERLOCK] PASS — Health jab record consumed by Compliance\n")


def test_insurance_fires_in_society():
    """Insurance is CONTEXTUAL on a Risk peril signal: a Darwin-Feb (cyclone) leg
    makes Risk emit natural_disaster → peril_crosswalk → Insurance premium.
    Booking won't complete offline (no merchant), so we directly exercise the
    composed _apply_insurance against a synthetic successful result + the live
    Risk assessment routed via the society."""
    orch = build_society()
    legs = [{"leg_id": "leg-0", "city": "darwin", "checkin": "2026-02-15",
             "checkout": "2026-02-22", "mode": "flight"}]
    risk_assessment = orch._assess_risk_signals(legs)
    codes = (risk_assessment or {}).get("rollup", {}).get("all_reason_codes", [])
    print("[INSURANCE] risk reason_codes=", codes)
    assert codes, "expected Risk to emit a peril reason code for Darwin-Feb"
    synthetic = {"outcome": "success", "total_cents": 180000}
    res = orch._apply_insurance(synthetic, risk_assessment)
    ins = res.get("insurance", {})
    print("[INSURANCE] premium_cents=", ins.get("premium_cents"),
          "peril_set=", ins.get("peril_set"))
    print("[INSURANCE] fee_line_items=", res.get("fee_line_items"))
    assert ins.get("premium_cents"), "expected an insurance premium"
    assert res.get("fee_line_items"), "expected premium fee injected"
    print("[INSURANCE] PASS — Insurance fired on Risk peril (composed) + fee injected\n")


# ===========================================================================
# COMPOSED BOOKING SMOKE — Insurance surfaces EXC-UNREST-1 on a BOOKABLE
# civil-unrest trip via a REAL negotiate() that COMPLETES a booking.
#
# This is the FULL composition (not _apply_insurance directly, not an injected
# reason code): a bookable Port Moresby trip → the live Risk agent emits
# civil_unrest from its seeded standing-advisory level → peril_crosswalk →
# Insurance assesses → civil_unrest is EXCLUDED, governed by its own named
# clause EXC-UNREST-1 with provenance (D5: EXC-WAR-2 is reserved for armed
# conflict). The merchant is the in-process mock transport (same pattern as
# test_booking_spine — the live merchant is reached over the network in the
# runner; offline we mock the HTTP edge so the booking COMPLETES and the
# post-commit insurance hook fires inside negotiate()).
#
# HONESTY: Insurance remains a TIE (a fair single agent given the same clause
# corpus also surfaces the exclusion, per the DC0 fair baseline). The point
# proven here is COMPOSITION — named-clause provenance (EXC-UNREST-1) + determinism
# inside the orchestrated society — NOT a new correctness gain.
# ===========================================================================

class _CountingTransport(httpx.BaseTransport):
    """Intercept merchant MCP calls; return canned per-tool responses."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses: dict[str, list] = {}
        for tool, resp in responses.items():
            self._responses[tool] = list(resp) if isinstance(resp, list) else [resp]
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            payload = json.loads(request.read())
        except Exception:
            return httpx.Response(400, text="bad request")
        tool = (payload.get("params") or {}).get("name", "")
        with self._lock:
            self._counts[tool] = self._counts.get(tool, 0) + 1
        resps = self._responses.get(tool)
        if not resps:
            return httpx.Response(500, json={"error": f"mock: no response for {tool!r}"})
        idx = min(self._counts[tool] - 1, len(resps) - 1)
        status, body = resps[idx]
        return httpx.Response(status, json=body)


def _merchant_result(domain: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "result": {
        "structuredContent": domain,
        "content": [{"type": "text", "text": json.dumps(domain)}],
    }}


def _catalog_result(results: list[dict]) -> tuple[int, dict]:
    return (200, _merchant_result({"source": "mock", "count": len(results), "results": results}))


def test_insurance_exc_unrest1_composes_on_bookable_civil_unrest_trip() -> None:
    """
    FULL COMPOSITION: a BOOKABLE Port Moresby trip books to success via
    negotiate(); the live Risk agent emits civil_unrest from the seeded
    standing-advisory level; Insurance surfaces the EXC-UNREST-1 civil-unrest
    exclusion with provenance — all inside the orchestrated society. (D5: the
    war clause EXC-WAR-2 is reserved for armed conflict, not civil unrest.)
    """
    PM_GRAND = {
        "hotel_id": "portmoresby-grand", "title": "Port Moresby Grand Hotel",
        "city": "port moresby", "review_score": 8.0, "star_rating": 4.0,
        "nights": 3, "total_cents": 54000, "amenities": ["pool", "wifi"],
        "provenance": "merchant",
    }
    CREATE_OK = _merchant_result({
        "id": "co_pm", "status": "incomplete", "user_id": "smoke-dc-insurance-compose",
        "line_items": [], "total_cents": 54000, "currency": "USD",
        "buyer_consent": False, "booking_ref": "",
    })
    COMPLETE_OK = _merchant_result({
        "id": "co_pm", "status": "complete", "user_id": "smoke-dc-insurance-compose",
        "line_items": [], "total_cents": 54000, "currency": "USD",
        "buyer_consent": True, "booking_ref": "BK-pm-compose-1",
    })
    LOOKUP_OK = _merchant_result({
        "hotel": {"id": "portmoresby-grand", "review_score": 8.0, "star_rating": 4.0},
        "available": True, "total_cents": 54000, "nights": 3, "source": "mock",
    })

    acc_transport = _CountingTransport({"search_catalog": _catalog_result([PM_GRAND])})
    budget_transport = _CountingTransport({
        "create_checkout": (200, CREATE_OK),
        "complete_checkout": (200, COMPLETE_OK),
    })
    critic_transport = _CountingTransport({"lookup_catalog": (200, LOOKUP_OK)})

    orch = TravelOrchestrator(
        planner_client=TestClient(PlannerAgent().build_app()),
        accommodation_client=TestClient(
            AccommodationAgent(merchant_transport=acc_transport).build_app()),
        budget_client=TestClient(
            BudgetAgent(merchant_transport=budget_transport).build_app()),
        critic_client=TestClient(
            CriticAgent(merchant_transport=critic_transport).build_app()),
        transport_client=TestClient(TransportAgent().build_app()),
        risk_client=TestClient(RiskAgent().build_app()),
        insurance_client=TestClient(InsuranceAgent().build_app()),
    )

    trip = {
        "user_id": "smoke-dc-insurance-compose",
        "total_budget_cents": 120000,
        # Port Moresby is BOOKABLE (catalog.go) AND carries a SEEDED civil-unrest
        # advisory (risk_agent _CIVIL_UNREST_BY_REGION). No tip-off; no injected code.
        "legs": [
            {"city": "port moresby", "checkin": "2026-03-01", "checkout": "2026-03-04",
             "adults": 1, "vibe": "city", "mode": "flight"},
        ],
    }
    res = orch.negotiate(trip)
    print("[INSURANCE-COMPOSE] outcome=", res.get("outcome"),
          "booking_ref=", res.get("booking_ref"))
    assert res["outcome"] == "success", res
    assert res.get("booking_ref"), "expected a real booking_ref (booking completed)"

    # Risk emitted civil_unrest INSIDE negotiate (not injected).
    risk_codes = (res.get("risk_signals") or {}).get("reason_codes", [])
    print("[INSURANCE-COMPOSE] risk reason_codes=", risk_codes)
    assert "civil_unrest" in risk_codes, f"Risk must emit civil_unrest: {risk_codes}"

    ins = res.get("insurance", {})
    print("[INSURANCE-COMPOSE] peril_set=", ins.get("peril_set"))
    print("[INSURANCE-COMPOSE] excluded=", ins.get("excluded_perils_summary"))
    print("[INSURANCE-COMPOSE] premium_cents=", ins.get("premium_cents"))
    assert ins, "expected an insurance block on the composed success"
    assert "civil_unrest" in (ins.get("peril_set") or []), ins
    excluded = ins.get("excluded_perils_summary") or []
    civil = next((e for e in excluded if e.get("peril_class") == "civil_unrest"), None)
    assert civil is not None, f"civil_unrest must be EXCLUDED: {excluded}"
    # D5 split: EXC-WAR-2 is reserved for armed conflict (Peril.WAR); civil unrest
    # is governed by its OWN named clause EXC-UNREST-1. A civil-unrest exclusion
    # must therefore cite EXC-UNREST-1, never EXC-WAR-2.
    assert "EXC-UNREST-1" in (civil.get("matched_clause_ids") or []), (
        f"the governing clause must be EXC-UNREST-1 (named-clause provenance): {civil}")
    assert "EXC-WAR-2" not in (civil.get("matched_clause_ids") or []), (
        f"civil unrest must NOT be charged to the war clause (D5): {civil}")
    assert ins.get("premium_cents"), "expected a premium on the composed booking"
    assert res.get("fee_line_items"), "expected the premium fee injected into the package"
    print("[INSURANCE-COMPOSE] PASS — Insurance surfaced EXC-UNREST-1 on a BOOKABLE "
          "civil-unrest trip INSIDE negotiate() (composes; TIE per DC0)\n")


def test_arrival_minutes_map_wiring():
    """Travel-day reservation wiring (the integration the unit tests can't reach without a merchant):
    the orchestrator maps transport edges → per-arrival-leg (minutes, mode), excluding sentinels
    (-1 unknown, 0 same-area), infeasible/cancelled edges, float/None minutes, and empty to_leg —
    and captures the mode (flight vs rail) the airport-transfer gate depends on."""
    from orchestration.orchestrator import _arrival_minutes_map
    tr = {"edges": [
        {"to_leg": "leg-1", "transfer_minutes": 120, "mode": "flight", "feasible": True},   # ✓
        {"to_leg": "leg-2", "transfer_minutes": 90, "mode": "rail", "feasible": True},        # ✓ rail
        {"to_leg": "leg-3", "transfer_minutes": -1, "mode": "unknown", "feasible": True},     # ✗ sentinel -1
        {"to_leg": "leg-4", "transfer_minutes": 0, "mode": "same_area", "feasible": True},    # ✗ same-area 0
        {"to_leg": "leg-5", "transfer_minutes": 300, "mode": "flight", "feasible": False},    # ✗ infeasible
        {"to_leg": "leg-6", "transfer_minutes": 200.0, "mode": "flight", "feasible": True},   # ✗ float
        {"to_leg": "", "transfer_minutes": 100, "mode": "flight", "feasible": True},          # ✗ no to_leg
    ]}
    minutes, mode = _arrival_minutes_map(tr)
    assert minutes == {"leg-1": 120, "leg-2": 90}, minutes
    assert mode == {"leg-1": "flight", "leg-2": "rail"}, mode
    assert _arrival_minutes_map(None) == ({}, {})   # robustness
    assert _arrival_minutes_map({}) == ({}, {})
    print("[TRAVELDAY] PASS — arrival-minutes wiring excludes sentinels/infeasible, keeps mode\n")


@pytest.mark.skip(reason="requires the real airport/air-network data (NRT Tokyo transfer minutes), "
                         "excluded from this public showcase repo as competitive-moat data")
def test_arrival_transport_minutes_flight_only_gate():
    """The airport→hotel transfer is added to the arrival-day reservation ONLY for a FLIGHT arrival;
    a rail/road/ferry arrival lands in-city (inter-city minutes only — no phantom airport leg). Origin
    / no-edge / zero-intercity → 0 (no trim). Uses committed Tokyo coords + NRT (CI-safe, no merchant)."""
    from orchestration.orchestrator import _arrival_transport_minutes
    leg = {"leg_id": "leg-1", "city": "tokyo", "hotel_title": "H"}
    mins = {"leg-1": 120}
    flight = _arrival_transport_minutes(leg, "leg-1", "tokyo", mins, {"leg-1": "flight"})
    assert flight > 120, f"flight arrival must add the airport transfer: {flight}"   # 120 + NRT hop
    rail = _arrival_transport_minutes(leg, "leg-1", "tokyo", mins, {"leg-1": "rail"})
    assert rail == 120, f"rail arrival must NOT add a phantom airport leg: {rail}"
    assert _arrival_transport_minutes(leg, "leg-0", "tokyo", {}, {}) == 0        # origin / no edge
    assert _arrival_transport_minutes(leg, "leg-1", "tokyo", {"leg-1": 0}, {"leg-1": "flight"}) == 0  # 0 intercity
    print("[TRAVELDAY] PASS — airport transfer gated to flight arrivals only\n")


if __name__ == "__main__":
    test_no_condition_passthrough()
    test_compliance_fires_in_society()
    test_health_fires_in_society()
    test_fraud_fires_in_society()
    test_health_compliance_interlock()
    test_insurance_fires_in_society()
    test_insurance_exc_unrest1_composes_on_bookable_civil_unrest_trip()
    test_arrival_minutes_map_wiring()
    test_arrival_transport_minutes_flight_only_gate()
    print("ALL COMPOSED SMOKE TESTS PASSED")
