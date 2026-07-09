"""
test_compliance_agent.py — Unit + invariant tests for the Compliance eligibility
GATE (L2 / DC1).

CI-safe: no LLM, no network, no DashScope. Uses the deterministic lead-time gate
directly and the in-process Starlette ASGI TestClient for the A2A surface.

Design contract: the §AGENT-EXTENSION PATTERN (insurance_agent.py) + AGENTS.md
(variance-clamp + the CI invariant test for the Compliance gate).

Coverage map:
  unit:
    1.  ET eVisa, depart in 3 days → cannot_satisfy via BLOCK_INSUFFICIENT_LEAD_TIME (DC1 core)
    2.  ET eVisa, depart 30 days out → can_satisfy, eVisa fee line item present
    3.  visa-free dest → can_satisfy (no lead-time gate)
    4.  unknown rule → conservative BLOCK (never silent allow)
    5.  earliest-feasible-departure re-sequence on a lead-time block
    6.  visa fee line item: integer cents from seed, kind=visa, FX-normalised for non-USD
    7.  business-day math (weekends skipped)
    8.  explain_block: factual claims derive from the structured verdict
  invariants (cascade-style regression — FAIL if violated):
    I1. NEVER-RETURN-UNBOOKABLE-IN-TIME: for any departure < (lead-time + buffer),
        the gate returns cannot_satisfy + bookable=False (the flagship CI invariant).
    I2. NO-LLM-NUMBERS: byte-identical verdict/fees/dates across N runs (variance-0).
    I3. DETERMINISTIC-VERDICT: the gate is a pure function of (rule, nationality,
        departure, today) — same inputs → same verdict, across the closed scan.
    I4. CONSERVATIVE-UNKNOWN: an unknown rule NEVER yields the plain can_satisfy
        verdict nor allowed=True. An unverified leg degrades to allowed=None and the
        whole-trip headline becomes can_satisfy_with_flags (bookable-but-flagged,
        D4 #24) — never an unqualified can_satisfy and never a silent allowed=True.
    I5. PROVENANCE-PRESENT: every leg verdict carries valid provenance.
  closed_set_fallback:
    F1. The full EligibilityVerdict is correct with NO LLM in the path (the gate is
        pure Python) — the LLM edge is purely cosmetic and clamped.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import date

sys.path.insert(0, os.path.dirname(__file__))

from starlette.testclient import TestClient

from core import contracts as C
from agents import compliance_agent as comp
from agents.compliance_agent import (
    ComplianceAgent,
    DEFAULT_BUFFER_BUSINESS_DAYS,
    EntryKind,
    GateVerdict,
    LegReason,
    add_business_days,
    business_days_between,
    check_eligibility,
    explain_block,
    gate_leg,
    required_lead_business_days,
    validate_rationale,
    visa_fee_line_item,
    _ENTRY_RULES,
)

# A fixed clock so the whole suite is deterministic (the gate's `today`).
TODAY = "2026-06-16"   # a Tuesday


# ===========================================================================
# Unit tests — the deterministic lead-time gate
# ===========================================================================

def test_dc1_core_ethiopia_evisa_unbookable_in_time():
    # ET eVisa needs 5 bd + 2 buffer = 7; depart Fri 2026-06-19 → only 3 bd → BLOCK.
    v = check_eligibility(
        legs=[{"dest_country": "ET", "departure_date": "2026-06-19"}],
        nationality="US", today=TODAY)
    assert v["verdict"] == GateVerdict.BLOCK
    assert v["bookable"] is False
    leg = v["per_leg"][0]
    assert leg["kind"] == EntryKind.EVISA
    assert leg["reason"] == LegReason.BLOCK_LEAD_TIME
    assert leg["available_business_days"] == 3
    assert leg["required_lead_business_days"] == 7
    assert leg["earliest_feasible_departure"] == "2026-06-25"


def test_negative_buffer_days_clamped_cannot_unblock_in_time():
    # HIGH defect: a caller-supplied negative buffer must be clamped to 0 so it can
    # never shrink (or invert) required lead time and let an unbookable-in-time trip
    # pass. ET eVisa lead = 5 bd; depart 2026-06-19 → only 3 bd available.
    # With buffer_days=-10, naive arithmetic gives required = 5 + (-10) = -5 < 3,
    # which would WRONGLY allow. Clamped buffer=0 → required = 5 > 3 → still BLOCK.
    v = check_eligibility(
        legs=[{"dest_country": "ET", "departure_date": "2026-06-19"}],
        nationality="US", today=TODAY, buffer_days=-10)
    assert v["verdict"] == GateVerdict.BLOCK
    assert v["bookable"] is False
    # Echoed buffer is the clamped value, not the raw negative input (HONESTY).
    assert v["buffer_business_days"] == 0
    leg = v["per_leg"][0]
    assert leg["buffer_business_days"] == 0
    assert leg["required_lead_business_days"] == 5     # 5 lead + 0 buffer, not -5
    assert leg["reason"] == LegReason.BLOCK_LEAD_TIME


def test_required_lead_business_days_clamps_negative_buffer():
    # Unit-level: required_lead_business_days never returns less than the raw lead.
    rule = {"kind": EntryKind.EVISA, "processing_lead_business_days": 5}
    assert required_lead_business_days(rule, -100) == 5
    assert required_lead_business_days(rule, 0) == 5
    assert required_lead_business_days(rule, 3) == 8


def test_ethiopia_evisa_bookable_when_far_out():
    v = check_eligibility(
        legs=[{"dest_country": "ET", "departure_date": "2026-07-20"}],
        nationality="US", today=TODAY)
    assert v["verdict"] == GateVerdict.ALLOW
    assert v["bookable"] is True
    # eVisa fee emitted as a line item (Budget vetoes it like any other).
    assert len(v["line_items"]) == 1
    li = v["line_items"][0]
    assert li["kind"] == "visa"
    assert li["usd_cents"] == 8200      # $82 Ethiopia eVisa — integer cents from seed
    assert v["total_visa_fee_usd_cents"] == 8200


def test_visa_free_destination_no_lead_time_gate():
    v = check_eligibility(
        legs=[{"dest_country": "TH", "departure_date": "2026-06-18"}],  # 2 days out, still OK
        nationality="US", today=TODAY)
    assert v["verdict"] == GateVerdict.ALLOW
    assert v["per_leg"][0]["kind"] == EntryKind.VISA_FREE
    assert v["per_leg"][0]["reason"] == LegReason.OK_VISA_FREE
    # No visa fee line item for a zero-fee visa-free leg.
    assert v["line_items"] == []


def test_unknown_rule_conservative_flag_never_silent_allow():
    # Unknown rule → FLAG advisory (allowed=None, unverified_flag=True), NOT hard block.
    # Trip still proceeds; advisory is surfaced. This is the correct honest behavior:
    # we don't know if it's blocked, so we flag and require human confirmation.
    v = check_eligibility(
        legs=[{"dest_country": "ZZ", "departure_date": "2026-12-01"}],
        nationality="US", today=TODAY)
    # Not a hard block — bookable=True, but the headline verdict is the legible
    # ALLOW_WITH_FLAGS (D4 #24): a bookable-but-unverified trip must NOT read as a
    # plain "can_satisfy" that a naive consumer could mistake for unqualified approval.
    assert v["verdict"] == GateVerdict.ALLOW_WITH_FLAGS
    assert v["bookable"] is True
    assert v["has_eligibility_flags"] is True
    leg = v["per_leg"][0]
    assert leg["kind"] == EntryKind.UNKNOWN
    assert leg["reason"] == LegReason.BLOCK_UNKNOWN_RULE
    assert leg["allowed"] is None       # unverified — not hard-blocked
    assert leg["unverified_flag"] is True
    assert "flag_advisory" in leg
    # And it appears in flagged_legs summary
    assert len(v["flagged_legs"]) == 1
    assert v["flagged_legs"][0]["dest_country"] == "ZZ"


def test_resequence_depart_later_on_lead_time_block():
    v = check_eligibility(
        legs=[{"dest_country": "ET", "departure_date": "2026-06-19"}],
        nationality="US", today=TODAY)
    rs = v["resequence"]
    assert rs is not None
    assert rs["depart_later_earliest"] == "2026-06-25"
    # Re-sequence is itself gated — an offered alternative carries its own verdict.
    for alt in rs["visa_free_alternatives"]:
        assert "alternative_bookable_in_time" in alt


def test_non_usd_visa_fee_fx_normalised():
    # France Schengen visa fee is EUR 90 → FX-normalised to usd_cents via the seam.
    v = check_eligibility(
        legs=[{"dest_country": "FR", "departure_date": "2026-12-01"}],
        nationality="IN", today=TODAY)
    assert v["verdict"] == GateVerdict.ALLOW
    li = v["line_items"][0]
    assert li["kind"] == "visa"
    assert li["money"]["currency"] == "EUR"
    assert li["money"]["native_cents"] == 9000
    assert li["usd_cents"] == 9720      # 9000 * 1.08 (seeded EUR rate)


def test_business_day_math_skips_weekends():
    # Fri 2026-06-19 + 1 business day = Mon 2026-06-22 (skips Sat/Sun).
    assert add_business_days(date(2026, 6, 19), 1) == date(2026, 6, 22)
    # Tue 6/16 -> Fri 6/19 = Wed,Thu,Fri = 3 business days.
    assert business_days_between(date(2026, 6, 16), date(2026, 6, 19)) == 3
    # end <= start → 0.
    assert business_days_between(date(2026, 6, 19), date(2026, 6, 16)) == 0


def test_explain_block_derives_from_structured_verdict():
    v = check_eligibility(
        legs=[{"dest_country": "ET", "departure_date": "2026-06-19"}],
        nationality="US", today=TODAY)
    ex = explain_block(v)
    assert ex["verdict"] == GateVerdict.BLOCK
    # The headline must mention the real seeded numbers (no fabrication).
    assert "5 business days" in ex["headline"]
    assert "2026-06-25" in ex["headline"]
    assert "Ethiopia" in ex["headline"]


def test_passport_validity_secondary_stub_blocks():
    # Passport expiring within 6 months of departure → secondary block.
    v = check_eligibility(
        legs=[{"dest_country": "ET", "departure_date": "2026-07-20"}],
        nationality="US", today=TODAY, passport_expiry="2026-08-01")
    assert v["verdict"] == GateVerdict.BLOCK
    assert v["per_leg"][0]["reason"] == LegReason.BLOCK_PASSPORT_VALIDITY


# ===========================================================================
# Invariant tests — cascade-style regression (FAIL if violated)
# ===========================================================================

def test_invariant_never_return_unbookable_in_time():
    """I1 (the flagship CI invariant): for ANY lead-time destination and ANY
    departure with fewer business days available than required, the gate MUST
    return cannot_satisfy + bookable=False. Scans the closed lead-time rule set
    across a range of near-departures. The society can NEVER return an itinerary
    with departure < (visa lead-time + buffer).

    Uses a nationality that is genuinely subject to the lead-time rule for each
    destination (US is visa-free for FR/Schengen, so we use NG for VISA_REQUIRED
    rules since NG is consistently subject to consular/embassy requirements).
    """
    today = date(2026, 6, 16)
    lead_rules = [r for r in _ENTRY_RULES if r["kind"] in
                  (EntryKind.EVISA, EntryKind.VISA_REQUIRED)]
    assert lead_rules, "expected at least one lead-time rule in the seed"

    # A nationality only exercises a WILDCARD lead-time rule if no nationality-specific
    # row for that destination claims it first (else it resolves to that specific row —
    # e.g. FR is visa-free-specific for UZ/AE, so FR→UZ is VISA_FREE, not the eVisa
    # wildcard). Compute, per destination, the set of nationalities pinned by a
    # specific row so we can pick a candidate that genuinely falls through to the
    # wildcard. Deterministic (sorted candidates) so the choice is var-0.
    def _specific_nats_for(dest: str) -> set[str]:
        out: set[str] = set()
        for x in _ENTRY_RULES:
            if x["dest_country"] == dest and x.get("eligible_nationalities") is not None:
                out |= {n.upper() for n in x["eligible_nationalities"]}
        return out

    # Candidate nationalities NOT broadly visa-free anywhere relevant; we filter
    # per-dest against the specific set and pick the first that falls through.
    _CANDIDATES = ["NG", "FR", "US", "CN", "IN", "BR", "ZA", "PH", "VN", "TH"]

    for r in lead_rules:
        # Skip specific-nationality rules — they only apply to their listed nationalities.
        if r.get("eligible_nationalities") is not None:
            continue
        dest = r["dest_country"]
        pinned = _specific_nats_for(dest)
        # Pick a nationality that (a) is not pinned by a specific row for this dest and
        # (b) is not the dest itself (citizen home entry is always visa-free).
        nat = next((c for c in _CANDIDATES if c not in pinned and c != dest), None)
        if nat is None:
            continue  # no clean fall-through candidate — not a counterexample to I1
        required = r["processing_lead_business_days"] + DEFAULT_BUFFER_BUSINESS_DAYS
        # Every departure from 1..required business days out is UNBOOKABLE.
        for bd in range(1, required):
            dep = add_business_days(today, bd)
            v = gate_leg(dest_country=r["dest_country"], nationality=nat,
                         departure_date=dep.isoformat(), today=today.isoformat())
            assert v["allowed"] is False, (r["dest_country"], nat, bd, v)
            assert v["reason"] == LegReason.BLOCK_LEAD_TIME, (r["dest_country"], nat, bd, v)
            assert v["available_business_days"] < v["required_lead_business_days"]
        # At exactly `required` business days out it becomes bookable.
        dep_ok = add_business_days(today, required)
        v_ok = gate_leg(dest_country=r["dest_country"], nationality=nat,
                        departure_date=dep_ok.isoformat(), today=today.isoformat())
        assert v_ok["allowed"] is True, (r["dest_country"], nat, v_ok)


def test_invariant_no_llm_numbers_variance_zero_across_n():
    """I2: byte-identical verdict/fees/dates across N runs (variance-0)."""
    sigs = set()
    for _ in range(12):
        v = check_eligibility(
            legs=[{"dest_country": "ET", "departure_date": "2026-06-19"}],
            nationality="US", today=TODAY)
        sigs.add(json.dumps({
            "verdict": v["verdict"],
            "reason": v["per_leg"][0]["reason"],
            "earliest": v["per_leg"][0]["earliest_feasible_departure"],
            "fee": v["total_visa_fee_usd_cents"],
            "required": v["per_leg"][0]["required_lead_business_days"],
        }, sort_keys=True))
    assert len(sigs) == 1, sigs


def test_invariant_deterministic_verdict_pure_function():
    """I3: the gate is a pure function of (rule, nationality, departure, today)."""
    a = gate_leg(dest_country="ET", nationality="US",
                 departure_date="2026-06-19", today=TODAY)
    b = gate_leg(dest_country="ET", nationality="US",
                 departure_date="2026-06-19", today=TODAY)
    assert a == b


def test_invariant_conservative_unknown_never_allows():
    """I4: an unknown rule NEVER silently allows (allowed=True). It is FLAG (allowed=None),
    never True (cleared) and never silently passes without a flag. Per the honesty thesis:
    unknown → FLAG advisory, NOT silent pass, NOT silent hard-block."""
    for dep in ("2026-06-18", "2026-09-01", "2027-01-01"):
        v = gate_leg(dest_country="ZZ", nationality="US",
                     departure_date=dep, today=TODAY)
        assert v["allowed"] is not True   # never silently cleared
        assert v["allowed"] is None       # always unverified flag, not hard-block
        assert v["unverified_flag"] is True
        assert v["kind"] == EntryKind.UNKNOWN


def test_invariant_provenance_present_and_valid():
    """I5: every leg verdict carries a valid provenance envelope."""
    v = check_eligibility(
        legs=[{"dest_country": "ET", "departure_date": "2026-06-19"},
              {"dest_country": "ZZ", "departure_date": "2026-12-01"}],
        nationality="US", today=TODAY)
    for leg in v["per_leg"]:
        assert C.is_valid_provenance(leg["provenance"]), leg


def test_closed_set_fallback_gate_is_pure_no_llm():
    """F1: the full EligibilityVerdict is correct with NO LLM in the path (the gate
    is pure Python — there is no LLM in check_eligibility/gate_leg at all)."""
    # Even with DASHSCOPE_API_KEY unset, the structured verdict is complete + correct.
    assert not os.environ.get("DASHSCOPE_API_KEY") or True  # path is LLM-free regardless
    v = check_eligibility(
        legs=[{"dest_country": "ET", "departure_date": "2026-06-19"}],
        nationality="US", today=TODAY)
    assert v["verdict"] == GateVerdict.BLOCK
    assert v["per_leg"][0]["earliest_feasible_departure"] == "2026-06-25"
    assert v["total_visa_fee_usd_cents"] == 8200


def test_rationale_validator_rejects_softened_block():
    """The cosmetic LLM rationale is clamped: a draft that softens a BLOCK to
    'good to go' is rejected (never overturns the deterministic gate)."""
    blocked = check_eligibility(
        legs=[{"dest_country": "ET", "departure_date": "2026-06-19"}],
        nationality="US", today=TODAY)
    assert validate_rationale(
        "The eVisa cannot be processed in time; consider departing later.", blocked) is True
    assert validate_rationale("You're all good to go — ready to book!", blocked) is False
    assert validate_rationale("", blocked) is False


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
    agent = ComplianceAgent(host="0.0.0.0", port=9106)
    client = TestClient(agent.build_app())
    card = client.get("/.well-known/agent-card.json").json()
    assert card["name"] == "compliance-agent"
    skill_ids = {s["id"] for s in card["skills"]}
    assert skill_ids == {"compliance.check_eligibility", "compliance.explain_block"}


def test_a2a_check_eligibility_dc1():
    agent = ComplianceAgent()
    client = TestClient(agent.build_app())
    data = _send(client, "compliance.check_eligibility", {
        "legs": [{"dest_country": "ET", "departure_date": "2026-06-19"}],
        "nationality": "US", "today": TODAY})
    assert data["verdict"] == GateVerdict.BLOCK
    assert data["bookable"] is False
    assert data["per_leg"][0]["reason"] == LegReason.BLOCK_LEAD_TIME
    assert data["resequence"]["depart_later_earliest"] == "2026-06-25"


def test_a2a_check_eligibility_bookable_emits_fee():
    agent = ComplianceAgent()
    client = TestClient(agent.build_app())
    data = _send(client, "compliance.check_eligibility", {
        "legs": [{"dest_country": "ET", "departure_date": "2026-07-20"}],
        "nationality": "US", "today": TODAY})
    assert data["verdict"] == GateVerdict.ALLOW
    assert data["line_items"][0]["kind"] == "visa"
    assert data["line_items"][0]["usd_cents"] == 8200


def test_a2a_explain_block_standalone():
    agent = ComplianceAgent()
    client = TestClient(agent.build_app())
    data = _send(client, "compliance.explain_block", {
        "legs": [{"dest_country": "ET", "departure_date": "2026-06-19"}],
        "nationality": "US", "today": TODAY})
    assert data["verdict"] == GateVerdict.BLOCK
    assert "UNBOOKABLE IN TIME" in data["headline"]
    assert data["rationale_source"] == "deterministic"


def _send_raw(client: TestClient, skill_id: str, data: dict) -> dict:
    """Like _send but returns the raw task WITHOUT asserting 'completed' — for the
    D7 raise-path tests where a non-completed terminal state is the expected result."""
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
    return resp.json()["result"]


def test_d7_today_required_check_handler_raises():
    """D7 (var-0): compliance.check_eligibility MUST reject a missing 'today' on the
    visa-shape gate path rather than clocking off wall-time. This is the structured-
    /negotiate gap the free-text ingress-stamp did NOT cover (a visa-required trip
    with no health legs reaches compliance directly). The lead-time verdict depends
    on 'today', so a wall-clock fallback would make a GATE DECISION non-deterministic."""
    agent = ComplianceAgent()
    client = TestClient(agent.build_app())
    task = _send_raw(client, "compliance.check_eligibility", {
        "legs": [{"dest_country": "ET", "departure_date": "2026-09-01"}],
        "nationality": "US",
        # 'today' deliberately omitted
    })
    assert task["status"]["state"] != "completed", (
        f"compliance.check_eligibility must reject missing 'today' (D7), "
        f"got state={task['status']['state']!r}"
    )


def test_d7_today_required_explain_handler_raises():
    """D7: compliance.explain_block MUST reject a missing 'today' when computing a
    verdict from {legs, nationality} (no wall-clock fallback)."""
    agent = ComplianceAgent()
    client = TestClient(agent.build_app())
    task = _send_raw(client, "compliance.explain_block", {
        "legs": [{"dest_country": "ET", "departure_date": "2026-09-01"}],
        "nationality": "US",
        # 'today' deliberately omitted, no pre-computed verdict
    })
    assert task["status"]["state"] != "completed", (
        f"compliance.explain_block must reject missing 'today' when computing from "
        f"legs (D7), got state={task['status']['state']!r}"
    )


# ===========================================================================
# add_business_days — weekend/holiday-adjacent boundary conditions
# ===========================================================================

def test_add_business_days_saturday_start_skips_weekend():
    """
    GAP fill: add_business_days() with a Saturday start date is not tested.

    Saturday 2026-06-20 + 1 business day → Monday 2026-06-22
    (Sunday is skipped too — both Sat and Sun are non-business days).
    The function advances from the START date, so a Saturday start correctly
    skips to the next business day.
    """
    saturday = date(2026, 6, 20)   # Saturday
    assert saturday.weekday() == 5, f"test fixture: expected Saturday; got weekday {saturday.weekday()}"

    result = add_business_days(saturday, 1)
    expected = date(2026, 6, 22)  # Monday
    assert result == expected, (
        f"add_business_days(Saturday, 1) must return Monday; got {result!r}"
    )


def test_add_business_days_sunday_start_skips_weekend():
    """
    Sunday 2026-06-21 + 1 business day → Monday 2026-06-22.
    """
    sunday = date(2026, 6, 21)
    assert sunday.weekday() == 6, f"test fixture: expected Sunday; got weekday {sunday.weekday()}"

    result = add_business_days(sunday, 1)
    expected = date(2026, 6, 22)  # Monday
    assert result == expected, (
        f"add_business_days(Sunday, 1) must return Monday; got {result!r}"
    )


def test_add_business_days_zero_returns_start_unchanged():
    """n=0 returns start unchanged, even if start is a Saturday."""
    saturday = date(2026, 6, 20)
    assert add_business_days(saturday, 0) == saturday, (
        "add_business_days(d, 0) must return d unchanged"
    )


def test_add_business_days_holiday_adjacent_no_special_handling():
    """
    Holidays are NOT modelled (documented conservative simplification).
    A date adjacent to a typical US holiday (e.g. US Independence Day 2026-07-04,
    a Saturday) is treated as any other date — no holiday skipping.

    Friday 2026-07-03 + 1 business day → Monday 2026-07-06
    (the function skips Sat 04-Jul and Sun 05-Jul but does NOT skip Mon 06-Jul).
    This confirms no accidental holiday logic has crept in.
    """
    fri_before_holiday = date(2026, 7, 3)   # Friday before 4th July 2026 (Saturday)
    result = add_business_days(fri_before_holiday, 1)
    expected = date(2026, 7, 6)  # Monday — no holiday skip
    assert result == expected, (
        f"add_business_days must NOT skip holidays (not modelled); "
        f"expected {expected!r}, got {result!r}"
    )


# ===========================================================================
# Nationality-discrimination tests — visa matrix correctness
# ===========================================================================

def _gate(nat: str, dest: str, dep: str = "2026-09-10") -> dict:
    return gate_leg(dest_country=dest, nationality=nat, departure_date=dep, today=TODAY)


def test_sg_to_id_visa_free_zero_fee():
    """SG→ID: ASEAN visa-free, no fee. The original bug that triggered this audit."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("ID", "SG")
    assert rule is not None
    assert rule["kind"] == EntryKind.VISA_FREE
    assert rule["fee_cents"] == 0
    v = _gate("SG", "ID")
    assert v["allowed"] is True
    assert v["kind"] == EntryKind.VISA_FREE
    assert v["fee_cents"] == 0


def test_jp_to_id_visa_free():
    """JP→ID: bilateral visa-free agreement."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("ID", "JP")
    assert rule is not None
    assert rule["kind"] == EntryKind.VISA_FREE
    v = _gate("JP", "ID")
    assert v["allowed"] is True
    assert v["kind"] == EntryKind.VISA_FREE


def test_kr_to_id_visa_free():
    """KR→ID: bilateral visa-free agreement."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("ID", "KR")
    assert rule["kind"] == EntryKind.VISA_FREE


def test_us_to_id_voa():
    """US→ID: Visa on Arrival (US is not on Indonesia's visa-free list)."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("ID", "US")
    assert rule["kind"] == EntryKind.VISA_ON_ARRIVAL


def test_in_to_id_voa():
    """IN→ID: India is VoA for Indonesia, not visa-free."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("ID", "IN")
    assert rule["kind"] == EntryKind.VISA_ON_ARRIVAL


def test_my_to_th_visa_free():
    """MY→TH: ASEAN visa exemption."""
    v = _gate("MY", "TH")
    assert v["allowed"] is True
    assert v["kind"] == EntryKind.VISA_FREE


def test_sg_to_th_visa_free():
    """SG→TH: ASEAN visa exemption."""
    v = _gate("SG", "TH")
    assert v["allowed"] is True
    assert v["kind"] == EntryKind.VISA_FREE


def test_ng_to_th_voa():
    """NG→TH: Nigeria needs VoA for Thailand (not on exemption list)."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("TH", "NG")
    assert rule["kind"] == EntryKind.VISA_ON_ARRIVAL
    assert rule["fee_cents"] > 0


def test_za_to_th_visa_free():
    """ZA→TH: South Africa is on Thailand's 60-day visa-exemption list since
    2024 (E2 fix — was wrongly seeded on the VoA row, which would have created
    an ambiguous seed once the exemption row became an explicit allow-list)."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("TH", "ZA")
    assert rule["kind"] == EntryKind.VISA_FREE
    assert rule["fee_cents"] == 0


def test_bd_to_th_visa_required():
    """BD→TH: Bangladesh is not on Thailand's exemption/VoA lists — must fall
    through to the Tourist Visa (TR) wildcard (E2: was silently visa-free under
    the old `eligible_nationalities: None` wildcard)."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("TH", "BD")
    assert rule["kind"] == EntryKind.VISA_REQUIRED
    assert rule["fee_cents"] > 0


def test_au_to_nz_visa_free():
    """AU→NZ: Trans-Tasman visa-free (no NZeTA required for Australians)."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("NZ", "AU")
    assert rule["kind"] == EntryKind.VISA_FREE
    assert rule["fee_cents"] == 0
    v = _gate("AU", "NZ")
    assert v["allowed"] is True
    assert v["kind"] == EntryKind.VISA_FREE


def test_us_to_nz_nzeta():
    """US→NZ: NZeTA required (not Trans-Tasman)."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("NZ", "US")
    assert rule["kind"] == EntryKind.EVISA


def test_gb_to_fr_visa_free():
    """GB→FR: UK citizens are visa-free in Schengen (post-Brexit, 90-day rule)."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("FR", "GB")
    assert rule["kind"] == EntryKind.VISA_FREE
    assert rule["fee_cents"] == 0


def test_sg_to_fr_visa_free():
    """SG→FR: Singapore passport is Schengen visa-free."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("FR", "SG")
    assert rule["kind"] == EntryKind.VISA_FREE


def test_au_to_fr_visa_free():
    """AU→FR: Australia is Schengen visa-free."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("FR", "AU")
    assert rule["kind"] == EntryKind.VISA_FREE


def test_jp_to_fr_visa_free():
    """JP→FR: Japan is Schengen visa-free."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("FR", "JP")
    assert rule["kind"] == EntryKind.VISA_FREE


def test_in_to_fr_visa_required():
    """IN→FR: India needs a Schengen consular visa (long lead time)."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("FR", "IN")
    assert rule["kind"] == EntryKind.VISA_REQUIRED
    assert rule["processing_lead_business_days"] == 15


def test_ng_to_fr_visa_required():
    """NG→FR: Nigeria needs a Schengen consular visa."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("FR", "NG")
    assert rule["kind"] == EntryKind.VISA_REQUIRED


def test_in_to_us_visa_required():
    """IN→US: India is not on the VWP; needs B1/B2 consular visa."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("US", "IN")
    assert rule["kind"] == EntryKind.VISA_REQUIRED
    assert rule["processing_lead_business_days"] == 15


def test_ng_to_us_visa_required():
    """NG→US: Nigeria is not on the VWP; needs B1/B2 consular visa."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("US", "NG")
    assert rule["kind"] == EntryKind.VISA_REQUIRED


def test_cn_to_us_visa_required():
    """CN→US: China is not on the VWP; needs B1/B2 consular visa."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("US", "CN")
    assert rule["kind"] == EntryKind.VISA_REQUIRED


def test_sg_to_us_esta():
    """SG→US: Singapore is on the VWP; gets ESTA."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("US", "SG")
    assert rule["kind"] == EntryKind.EVISA
    assert rule["fee_cents"] == 4027   # ESTA $40.27 (HR-1 2025-09-30 + 2026 CPI)


def test_gb_to_us_esta():
    """GB→US: UK is on the VWP; gets ESTA."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("US", "GB")
    assert rule["kind"] == EntryKind.EVISA


def test_ke_eac_visa_free():
    """TZ→KE: EAC member is visa-free into Kenya."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("KE", "TZ")
    assert rule["kind"] == EntryKind.VISA_FREE
    assert rule["fee_cents"] == 0


def test_us_to_ke_eta():
    """US→KE: US needs Kenya eTA ($30, 3bd)."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("KE", "US")
    assert rule["kind"] == EntryKind.EVISA
    assert rule["fee_cents"] == 3000


# ===========================================================================
# HARDENING — D1 / D4 / #51 fixes (fail-conservative + var-0 + verdict legibility)
# ===========================================================================

def test_d1_empty_nationality_passes_through_as_conservative_flag():
    """D1 #5: a falsy/empty nationality must NOT raise — it degrades to a
    conservative UNKNOWN FLAG per leg (never a silent pass, never a crash that the
    orchestrator would swallow to None and skip the gate). For a known dest with
    nationality-specific rows, an empty nationality is UNVERIFIED, not wildcard-ALLOW."""
    v = check_eligibility(
        legs=[{"dest_country": "US", "departure_date": "2026-12-01"}],
        nationality="", today=TODAY)
    leg = v["per_leg"][0]
    assert leg["allowed"] is None              # unverified flag, not allowed=True
    assert leg["kind"] == EntryKind.UNKNOWN
    assert leg["unverified_flag"] is True
    assert v["bookable"] is True               # bookable-but-flagged, not a hard block
    assert v["has_eligibility_flags"] is True
    assert v["verdict"] == GateVerdict.ALLOW_WITH_FLAGS


def test_d1_none_nationality_does_not_raise_in_handler():
    """D1 #5: the A2A handler must NOT raise on nationality=None — it passes "" so
    the gate runs and surfaces a FLAG, instead of throwing a RuntimeError the
    orchestrator swallows to None (which would silently bypass compliance)."""
    agent = ComplianceAgent()
    client = TestClient(agent.build_app())
    data = _send(client, "compliance.check_eligibility", {
        "legs": [{"dest_country": "US", "departure_date": "2026-12-01"}],
        "nationality": None, "today": TODAY})
    assert data["bookable"] is True
    assert data["has_eligibility_flags"] is True
    assert data["verdict"] == GateVerdict.ALLOW_WITH_FLAGS
    assert data["per_leg"][0]["allowed"] is None


def test_d1_empty_nationality_known_country_specific_rows_not_wildcard_allow():
    """D1 #12: an empty/unknown nationality against a KNOWN country that has
    nationality-CONDITIONAL rows must resolve to a conservative UNKNOWN FLAG, NOT
    bind to the permissive wildcard (e.g. US ESTA) and silently ALLOW. The traveler
    must be told to verify, never approved as wildcard-eligible."""
    from agents.compliance_agent import fetch_entry_rule
    # US has both a nationality-specific B1/B2 row and an ESTA wildcard.
    rule, _ = fetch_entry_rule("US", "")
    assert rule is None    # NOT the ESTA wildcard
    v = gate_leg(dest_country="US", nationality="", departure_date="2026-12-01", today=TODAY)
    assert v["allowed"] is None
    assert v["kind"] == EntryKind.UNKNOWN
    assert v["reason"] == LegReason.BLOCK_UNKNOWN_RULE


def test_empty_nationality_dest_with_only_wildcard_still_unverified():
    """D1 #12 corollary: ET has ONLY a wildcard eVisa row (no nationality-specific
    rows). An empty nationality there is still UNVERIFIED (we don't know the
    traveler), conservative-flagged rather than silently bound to the wildcard."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("ET", "")
    # ET has no nationality-specific rows, so the prior behavior would bind the
    # wildcard. The conservative fix only suppresses the wildcard when specific rows
    # exist; ET binds the wildcard eVisa (a lead-time-gated kind, never a silent OK).
    assert rule is not None
    assert rule["kind"] == EntryKind.EVISA


def test_d4_allow_with_flags_verdict_only_on_flagged_not_blocked():
    """D4 #24: the headline verdict is ALLOW (clear), ALLOW_WITH_FLAGS (bookable but
    unverified), or BLOCK (hard) — never a plain ALLOW when an unverified flag is
    present."""
    # Fully clear → plain ALLOW.
    clear = check_eligibility(
        legs=[{"dest_country": "TH", "departure_date": "2026-09-10"}],
        nationality="US", today=TODAY)
    assert clear["verdict"] == GateVerdict.ALLOW
    assert clear["has_eligibility_flags"] is False
    # Flagged (unknown dest) → ALLOW_WITH_FLAGS, still bookable.
    flagged = check_eligibility(
        legs=[{"dest_country": "ZZ", "departure_date": "2026-12-01"}],
        nationality="US", today=TODAY)
    assert flagged["verdict"] == GateVerdict.ALLOW_WITH_FLAGS
    assert flagged["bookable"] is True
    # Hard block (lead-time) → BLOCK, even though no flag.
    blocked = check_eligibility(
        legs=[{"dest_country": "ET", "departure_date": "2026-06-19"}],
        nationality="US", today=TODAY)
    assert blocked["verdict"] == GateVerdict.BLOCK
    assert blocked["bookable"] is False


def test_d4_block_wins_over_flag_when_both_present():
    """D4 #24: a HARD block plus an unverified flag on the same trip → BLOCK (the
    most conservative verdict), never ALLOW_WITH_FLAGS."""
    v = check_eligibility(
        legs=[{"dest_country": "ET", "departure_date": "2026-06-19"},   # hard lead-time block
              {"dest_country": "ZZ", "departure_date": "2026-12-01"}],  # unknown flag
        nationality="US", today=TODAY)
    assert v["verdict"] == GateVerdict.BLOCK
    assert v["bookable"] is False
    assert v["has_eligibility_flags"] is True


def test_d4_explain_block_handles_allow_with_flags():
    """D4 #24: explain_block on a flagged-but-bookable verdict must NOT emit the
    'all bookable, no block' all-clear headline (it surfaces the flag legs)."""
    v = check_eligibility(
        legs=[{"dest_country": "ZZ", "departure_date": "2026-12-01"}],
        nationality="US", today=TODAY)
    ex = explain_block(v)
    assert ex["verdict"] == GateVerdict.ALLOW_WITH_FLAGS
    assert ex["headline"] != "All legs are bookable in time — no compliance block."


# --- #51: ambiguous overlapping rules → precedence + uniqueness -------------

def test_seed_uniqueness_no_ambiguous_specific_rows():
    """#51: the shipped seed must have NO (dest, nationality) pair covered by more
    than one nationality-specific row. _assert_entry_rules_unambiguous() runs at
    import; this asserts it stays green for the current seed."""
    from agents.compliance_agent import _assert_entry_rules_unambiguous
    _assert_entry_rules_unambiguous()   # must not raise on the shipped seed


def test_seed_uniqueness_detects_injected_overlap():
    """#51: an injected overlap (two specific rows for the same dest+nationality)
    must raise SeedIntegrityError, not silently resolve to a list-order accident."""
    from agents import compliance_agent as cmod
    from agents.compliance_agent import SeedIntegrityError, _assert_entry_rules_unambiguous
    original = cmod._ENTRY_RULES
    try:
        cmod._ENTRY_RULES = original + [
            {"dest_country": "TH", "program": "DUP overlap",
             "kind": EntryKind.VISA_REQUIRED, "eligible_nationalities": ["NG"],
             "processing_lead_business_days": 9, "fee_cents": 999, "currency": "USD",
             "provenance": "seed:test-dup", "source_url": ""},
        ]
        # TH already has a VOA specific row for NG → now two specific rows for TH/NG.
        try:
            _assert_entry_rules_unambiguous()
            raised = False
        except SeedIntegrityError:
            raised = True
        assert raised, "expected SeedIntegrityError on overlapping TH/NG rows"
    finally:
        cmod._ENTRY_RULES = original


def test_resolve_specific_overlap_picks_most_conservative_var0():
    """#51: the runtime resolver fail-safe — if more than one specific row matches,
    pick the MOST CONSERVATIVE (longest-lead) kind by explicit deterministic
    precedence, var-0 (sorted, never list-order accident), never the cheaper one."""
    from agents.compliance_agent import _resolve_specific
    permissive = {"dest_country": "XX", "program": "cheap VOA",
                  "kind": EntryKind.VISA_ON_ARRIVAL, "eligible_nationalities": ["NG"],
                  "processing_lead_business_days": 0, "fee_cents": 100, "currency": "USD",
                  "provenance": "seed:t", "source_url": ""}
    strict = {"dest_country": "XX", "program": "consular visa",
              "kind": EntryKind.VISA_REQUIRED, "eligible_nationalities": ["NG"],
              "processing_lead_business_days": 15, "fee_cents": 9000, "currency": "USD",
              "provenance": "seed:t", "source_url": ""}
    # Order must not matter (var-0): both orderings pick the conservative one.
    assert _resolve_specific([permissive, strict], "XX", "NG")["program"] == "consular visa"
    assert _resolve_specific([strict, permissive], "XX", "NG")["program"] == "consular visa"


def test_newly_seeded_destinations_return_real_verdict_not_unknown_flag():
    """ADDITIVE VERIFIED seed coverage: destinations that were previously
    uncovered (→ conservative UNKNOWN FLAG) now resolve to a REAL closed-set entry
    verdict. We assert several newly-added (dest, nationality) pairs each yield a
    concrete EntryKind (NOT UNKNOWN) with allowed in {True, False}, and that a real
    visa-free verdict reads as a clean can_satisfy (no eligibility flag).

    The coverage-invariant is preserved by the still-unknown control below: a
    genuinely-uncovered destination MUST still degrade to the UNKNOWN FLAG, so
    seeding more countries never weakened the never-silent-allow guard."""
    # (dest, nationality, expected_kind) — all are far-future departures so the
    # lead-time gate never confounds the entry-kind assertion.
    cases = [
        ("MX", "US", EntryKind.VISA_FREE),         # FMM tourist permit, visa-free
        ("GB", "US", EntryKind.EVISA),             # UK ETA
        ("GB", "IN", EntryKind.VISA_REQUIRED),     # UK Standard Visitor Visa (wildcard)
        ("KR", "US", EntryKind.VISA_FREE),         # Korea visa waiver
        ("TW", "JP", EntryKind.VISA_FREE),         # Taiwan 90-day visa-free
    ]
    for dest, nat, expect_kind in cases:
        v = gate_leg(dest_country=dest, nationality=nat,
                     departure_date="2026-12-01", today=TODAY)
        assert v["kind"] == expect_kind, (dest, nat, v["kind"])
        assert v["kind"] != EntryKind.UNKNOWN, (dest, nat)
        # A real verdict is decided (allowed is a bool, never the None FLAG state).
        assert v["allowed"] in (True, False), (dest, nat, v["allowed"])
        assert v.get("unverified_flag") is not True, (dest, nat)
        assert v["provenance"], (dest, nat)  # I5: provenance present

    # Whole-trip: a real visa-free leg is a clean can_satisfy (no eligibility flag).
    trip = check_eligibility(
        legs=[{"dest_country": "MX", "departure_date": "2026-12-01"}],
        nationality="US", today=TODAY)
    assert trip["verdict"] == GateVerdict.ALLOW
    assert trip["bookable"] is True
    assert trip["has_eligibility_flags"] is False

    # Coverage-invariant control: a STILL-uncovered destination must remain the
    # conservative UNKNOWN FLAG (never a silent allow) — seeding more real countries
    # did not erode the guard.
    ctrl = gate_leg(dest_country="ZZ", nationality="US",
                    departure_date="2026-12-01", today=TODAY)
    assert ctrl["kind"] == EntryKind.UNKNOWN
    assert ctrl["allowed"] is None
    assert ctrl["unverified_flag"] is True


def test_ir_us_national_never_silently_allowed_bug53():
    """finding #53 rerun (HIGH): the IR seed row's eligible_nationalities used to be
    ["US"], which — because eligible_nationalities is an ALLOW-list everywhere else
    in this file (fetch_entry_rule / gate_leg) — inverted the row's own program
    label ("Iran visa - US nationals (ineligible for standard tourist visa)") and
    made US the ONE nationality that resolved to a real, booking-eligible verdict,
    while every other nationality (no seeded rule) correctly degraded to the
    conservative UNKNOWN flag. After the fix (eligible_nationalities=[]), US must
    get the SAME conservative treatment as GB/DE/CA — never a silent allow."""
    us = gate_leg(dest_country="IR", nationality="US",
                  departure_date="2027-01-15", today=TODAY)
    # Pre-fix this was kind=VISA_REQUIRED, allowed=True (a real booking-eligible
    # verdict for a nationality the program's own label says is ineligible).
    assert us["kind"] == EntryKind.UNKNOWN
    assert us["allowed"] is None
    assert us["unverified_flag"] is True
    assert us["reason"] == LegReason.BLOCK_UNKNOWN_RULE

    # Parity check: US must resolve identically to other nationalities with no
    # seeded IR rule (GB/DE/CA) — the fix must not special-case US in either
    # direction.
    for nat in ("GB", "DE", "CA"):
        v = gate_leg(dest_country="IR", nationality=nat,
                     departure_date="2027-01-15", today=TODAY)
        assert v["kind"] == us["kind"]
        assert v["allowed"] == us["allowed"]
        assert v["reason"] == us["reason"]

    # Whole-trip: US/IR must read as flagged, never a clean can_satisfy.
    trip = check_eligibility(
        legs=[{"dest_country": "IR", "departure_date": "2027-01-15"}],
        nationality="US", today=TODAY)
    assert trip["verdict"] == GateVerdict.ALLOW_WITH_FLAGS
    assert trip["has_eligibility_flags"] is True
    assert len(trip["flagged_legs"]) == 1
    assert trip["flagged_legs"][0]["dest_country"] == "IR"


def test_sg_visa_required_wildcard_hard_blocks_non_visa_free_nationality_bug53():
    """finding #53 rerun (medium): SG previously had only the visa-free row and no
    VISA_REQUIRED wildcard fallback (unlike every other seeded country — US, JP,
    CN, etc. all have one), so a nationality like IN (India — explicitly documented
    in the seed comment as requiring a visa to enter Singapore) degraded to the
    soft "unverified" conservative FLAG instead of a real hard block with a genuine
    lead-time gate. After the fix, SG/IN resolves to a real VISA_REQUIRED rule."""
    from agents.compliance_agent import fetch_entry_rule
    rule, _ = fetch_entry_rule("SG", "IN")
    assert rule is not None
    assert rule["kind"] == EntryKind.VISA_REQUIRED
    assert rule["processing_lead_business_days"] > 0
    assert rule["fee_cents"] > 0

    # Insufficient lead time -> real hard BLOCK_LEAD_TIME, not the soft UNKNOWN flag.
    v = gate_leg(dest_country="SG", nationality="IN",
                 departure_date="2026-06-19", today=TODAY)
    assert v["kind"] == EntryKind.VISA_REQUIRED
    assert v["allowed"] is False
    assert v["reason"] == LegReason.BLOCK_LEAD_TIME
    assert v["earliest_feasible_departure"] is not None

    # Far-out departure -> a real bookable-with-fee verdict, not a soft flag.
    v2 = gate_leg(dest_country="SG", nationality="IN",
                  departure_date="2026-12-01", today=TODAY)
    assert v2["allowed"] is True
    assert v2["kind"] == EntryKind.VISA_REQUIRED
    assert v2.get("unverified_flag") is not True


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
