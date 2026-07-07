"""
test_inv_critic_counterparty_blocked.py — STRONG deterministic tests for the
CRITIC COUNTERPARTY_BLOCKED GATE (Gate 6b, critic_agent.py lines 842-900).

Money-safety invariant under test
----------------------------------
The Critic's commit-time fraud re-check (defense-in-depth, N1) independently
re-derives each leg's counterparty solvency band from the SAME seeded table
the Fraud agent uses.  A blocked or unknown counterparty MUST trigger a
COUNTERPARTY_BLOCKED violation so an insolvent supplier can never slip through
to the single irreversible human-consent commit — even when the orchestrator
missed or skipped the Fraud verdict.

Scoping rule (lines 883-886) under test
-----------------------------------------
(a) BLOCKED band (any leg, hotel_id or counterparty_id) → always gate.
(b) UNKNOWN band with an EXPLICIT counterparty_id → gate (conservative).
(c) UNKNOWN band with ONLY a hotel_id (no explicit counterparty_id) and no
    seeded profile → NOT gated (the design-decision scoping for the bulk
    hotel catalog; these legs resolve to UNKNOWN but the Critic leaves them
    alone — only a KNOWN-blocked band or an explicitly-declared counterparty
    triggers the gate).

Consent token rule (HIGH C1(a))
---------------------------------
A VALID structured consent token "consent:{cid}:{band}:{nonce}" overrides the
gate for that counterparty.  A garbage / unbound / band-mismatched token NEVER
overrides (fail-conservative).

Design contracts verified here
--------------------------------
- decision == "rejected" on ANY COUNTERPARTY_BLOCKED violation.
- The violation code is exactly COUNTERPARTY_BLOCKED (the right constant).
- route_to == "accommodation" (the accommodation agent must re-propose a
  solvent supplier — the planner/budget cannot fix an insolvent carrier).
- The violation detail embeds the band string and the N1 narrative.
- The gate_reason from vet_counterparty propagates into the violation detail.
- A VALID consent token lifts the block (no violation, decision "verified").
- A GARBAGE token (not structured-consent) does NOT lift the block.
- A CORRECTLY-STRUCTURED but WRONG-COUNTERPARTY token does NOT lift the block.
- A CORRECTLY-STRUCTURED, CORRECTLY-BOUND but WRONG-BAND token does NOT lift.
- A clear/solvent counterparty (score >= 70) is NEVER flagged.
- An elevated counterparty (45 <= score < 70) is NEVER flagged (committable).
- Multi-leg: only the blocked leg is flagged; the clear leg is untouched.
- An UNKNOWN carrier declared via explicit counterparty_id IS gated.
- An UNKNOWN hotel_id (bare, no explicit counterparty_id) is NOT gated.
- _fraud_vet_counterparty is wired into the Critic at module load (interlock).

CI-safe / var-0
----------------
- No LLM, no network, no paid API, no real merchant.
- Merchant HTTP calls intercepted by MockMerchantTransport (httpx.BaseTransport).
- All counterparty IDs are taken from the seeded solvency table or are
  deliberately absent from it (deterministic).
- No live booking_ref nonces or golden numbers are asserted.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from typing import Any

import httpx

sys.path.insert(0, os.path.dirname(__file__))

from starlette.testclient import TestClient

from agents import critic_agent as ca_mod
from agents.critic_agent import (
    COUNTERPARTY_BLOCKED,
    CriticAgent,
)
from agents.fraud_agent import RiskBand

# ---------------------------------------------------------------------------
# Seeded counterparty fixtures (from fraud_agent._SOLVENCY_BY_COUNTERPARTY).
# These are constants — changing them in the seed table is a regression.
# ---------------------------------------------------------------------------

# score=28 → BLOCKED (the DC3 cheapest-carrier trap; IATA BSP distress watchlist)
BLOCKED_CARRIER = "carrier-skylark-budget-air"

# score=31 → BLOCKED (failing OTA reseller; negative working capital)
BLOCKED_OTA = "ota-cheapfly-reseller"

# score=91 → CLEAR (Singapore Airlines; high solvency)
CLEAR_CARRIER = "carrier-singapore-airlines"

# score=52 → ELEVATED (committable advisory; a LCC under pressure)
ELEVATED_CARRIER = "carrier-valuair-lcc"

# No seeded profile → UNKNOWN (conservative block when explicitly declared)
UNKNOWN_CARRIER = "carrier-who-knows-airlines"

# A bare hotel_id that has NO seeded solvency profile.  Used to verify the
# scoping rule: UNKNOWN without explicit counterparty_id is NOT gated.
UNSEEN_HOTEL_ID = "hotel-unknown-no-profile-xyz"


# ---------------------------------------------------------------------------
# Consent token helpers (mirrors test_fraud_agent.py convention).
# ---------------------------------------------------------------------------

def _valid_consent(counterparty_id: str, band: str, nonce: str = "gate-n1") -> str:
    """Build a VALID structured consent grant for a counterparty at its band."""
    return f"consent:{counterparty_id}:{band}:{nonce}"


# ---------------------------------------------------------------------------
# Merchant mock transport (house pattern; all prices match proposals exactly
# so the only failing gate is COUNTERPARTY_BLOCKED, not PRICE_MISMATCH).
# ---------------------------------------------------------------------------

class _EchoMerchantTransport(httpx.BaseTransport):
    """
    Returns the proposal's own total_cents as the merchant price so that
    Gate 5 (price integrity) always passes.  The response is built from the
    request body's params.arguments so the Critic sees a price match.

    For tests that ONLY care about the COUNTERPARTY_BLOCKED gate we want zero
    noise from price mismatches.
    """

    def __init__(
        self,
        *,
        override_prices: dict[str, int] | None = None,
        review_score: float = 8.5,
        max_occupancy: int = 4,
    ) -> None:
        # Optional overrides: hotel_id → total_cents (for explicit price tests).
        self._overrides: dict[str, int] = override_prices or {}
        self._review_score = review_score
        self._max_occupancy = max_occupancy

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            body = json.loads(request.read())
        except Exception:
            return httpx.Response(400, text="bad json")

        args = (body.get("params") or {}).get("arguments") or {}
        hotel_id = args.get("hotel_id", "unknown")
        # Echo the request price back as the merchant price (no mismatch).
        # The Critic's proposal_cents comes from the leg, not the mock, so we
        # need to extract it from the ORIGINAL request body.  A simpler approach:
        # return a canonical stable price per hotel that the test knows about.
        # We use the override table if set, else a large benign value that the
        # tests keep inside budget.
        total_cents = self._overrides.get(hotel_id, 50_000)
        domain: dict[str, Any] = {
            "hotel": {
                "id": hotel_id,
                "title": f"Mock {hotel_id}",
                "review_score": self._review_score,
                "max_occupancy": self._max_occupancy,
            },
            "available": True,
            "total_cents": total_cents,
            "nights": 3,
            "source": "mock",
        }
        result = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "structuredContent": domain,
                "content": [{"type": "text", "text": json.dumps(domain)}],
            },
        }
        return httpx.Response(200, json=result)


# ---------------------------------------------------------------------------
# Helper factories.
# ---------------------------------------------------------------------------

def _make_critic(transport: httpx.BaseTransport | None = None) -> CriticAgent:
    return CriticAgent(merchant_transport=transport)


def _leg(
    *,
    leg_id: str = "leg-0",
    hotel_id: str | None = None,
    counterparty_id: str | None = None,
    total_cents: int = 50_000,
    provenance: str = "merchant",
    checkin: str = "2026-08-01",
    checkout: str = "2026-08-04",
) -> dict[str, Any]:
    """Build a minimal valid leg dict."""
    leg: dict[str, Any] = {
        "leg_id": leg_id,
        "city": "jakarta",
        "checkin": checkin,
        "checkout": checkout,
        "adults": 1,
        "total_cents": total_cents,
        "provenance": provenance,
    }
    if hotel_id is not None:
        leg["hotel_id"] = hotel_id
    if counterparty_id is not None:
        leg["counterparty_id"] = counterparty_id
    return leg


def _run(
    agent: CriticAgent,
    legs: list[dict],
    *,
    budget: int = 500_000,
    consent_tokens: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Call _verify_itinerary directly (deterministic, no HTTP transport needed for
    the fraud gate itself — the transport mock is needed for the price-integrity
    Gate 5 so that PRICE_MISMATCH does not pollute COUNTERPARTY_BLOCKED tests).
    """
    return agent._verify_itinerary(
        user_id="u-test",
        total_budget_cents=budget,
        legs=legs,
        counterparty_consent_tokens=consent_tokens,
    )


def _violation_codes(result: dict) -> list[str]:
    return [v["code"] for v in result.get("violations", [])]


def _find_violation(result: dict, code: str) -> dict[str, Any]:
    for v in result.get("violations", []):
        if v["code"] == code:
            return v
    raise AssertionError(
        f"Violation {code!r} not found in result; violations: "
        f"{_violation_codes(result)}"
    )


# ===========================================================================
# Prerequisite: the fraud gate IS wired into the Critic module
# ===========================================================================

def test_fraud_vet_wired_into_critic_module() -> None:
    """
    ca_mod._fraud_vet_counterparty must not be None at module load — the
    interlock depends on it.  If it is None, every test in this file would
    be silently vacuous (the gate becomes a no-op).
    """
    assert ca_mod._fraud_vet_counterparty is not None, (
        "critic_agent._fraud_vet_counterparty is None — the fraud interlock "
        "is NOT wired and the COUNTERPARTY_BLOCKED gate is a no-op.  "
        "This is a money-safety regression."
    )
    assert ca_mod._FraudRiskBand is not None, (
        "critic_agent._FraudRiskBand is None — RiskBand constants cannot be "
        "compared and the gate is broken."
    )


# ===========================================================================
# 1. BLOCKED band via hotel_id → COUNTERPARTY_BLOCKED, decision=rejected
# ===========================================================================

def test_blocked_carrier_hotel_id_triggers_counterparty_blocked() -> None:
    """
    Gate 6b condition (a): a leg whose hotel_id maps to a BLOCKED band in the
    seeded solvency table must produce COUNTERPARTY_BLOCKED regardless of whether
    the leg uses 'hotel_id' or 'counterparty_id' — the BLOCKED band alone gates.

    Here we use hotel_id (the common case: an orchestrator that filled in the
    carrier ID as the hotel_id without an explicit counterparty_id field).

    The mock transport returns a matching price so Gate 5 passes; the ONLY gate
    that fires is COUNTERPARTY_BLOCKED.
    """
    agent = _make_critic(
        _EchoMerchantTransport(
            override_prices={BLOCKED_CARRIER: 50_000}
        )
    )
    legs = [_leg(hotel_id=BLOCKED_CARRIER, total_cents=50_000)]
    result = _run(agent, legs)

    assert result["decision"] == "rejected", (
        f"A BLOCKED counterparty must cause rejection: {result}"
    )
    codes = _violation_codes(result)
    assert COUNTERPARTY_BLOCKED in codes, (
        f"COUNTERPARTY_BLOCKED must be in violations for BLOCKED band: {codes}"
    )
    v = _find_violation(result, COUNTERPARTY_BLOCKED)
    # route_to must be "accommodation" (§4.3 conflict routing)
    assert v["route_to"] == "accommodation", (
        f"COUNTERPARTY_BLOCKED must route to accommodation, got {v['route_to']!r}"
    )
    # Detail must surface the band and the N1 narrative
    detail = v["detail"]
    assert "blocked" in detail.lower(), (
        f"Detail must contain the band 'blocked': {detail!r}"
    )
    assert "N1" in detail, (
        f"Detail must reference fault class N1 (supplier-insolvency VOID): {detail!r}"
    )
    # The leg_id must be populated correctly
    assert v["leg_id"] == "leg-0", (
        f"leg_id must be 'leg-0', got {v['leg_id']!r}"
    )
    print(
        f"PASS: test_blocked_carrier_hotel_id_triggers_counterparty_blocked "
        f"[band=blocked, detail={detail[:60]!r}]"
    )


# ===========================================================================
# 2. BLOCKED band via explicit counterparty_id → COUNTERPARTY_BLOCKED
# ===========================================================================

def test_blocked_carrier_explicit_counterparty_id_triggers_gate() -> None:
    """
    Gate 6b: when a leg declares counterparty_id explicitly, condition (b) also
    applies (any non-committable band on an explicit counterparty → gate).  For
    a BLOCKED band the gate fires via BOTH condition (a) and (b).

    We verify that the violation's detail embeds the carrier id and the gate_reason
    from vet_counterparty (which includes "BLOCK_INSOLVENCY_RISK").
    """
    agent = _make_critic(
        _EchoMerchantTransport(
            override_prices={BLOCKED_CARRIER: 50_000}
        )
    )
    legs = [
        _leg(
            hotel_id=BLOCKED_CARRIER,
            counterparty_id=BLOCKED_CARRIER,
            total_cents=50_000,
        )
    ]
    result = _run(agent, legs)

    assert result["decision"] == "rejected", result
    v = _find_violation(result, COUNTERPARTY_BLOCKED)
    detail = v["detail"]
    assert BLOCKED_CARRIER in detail, (
        f"Detail must embed the counterparty id: {detail!r}"
    )
    assert "BLOCK_INSOLVENCY_RISK" in detail, (
        f"gate_reason 'BLOCK_INSOLVENCY_RISK' must appear in detail: {detail!r}"
    )
    print(
        f"PASS: test_blocked_carrier_explicit_counterparty_id_triggers_gate "
        f"[detail={detail[:80]!r}]"
    )


# ===========================================================================
# 3. BLOCKED OTA reseller → COUNTERPARTY_BLOCKED
# ===========================================================================

def test_blocked_ota_reseller_triggers_gate() -> None:
    """
    The solvency table includes OTA resellers (kind=ota).  An OTA with a BLOCKED
    band (score=31, 'ota-cheapfly-reseller') must also trigger COUNTERPARTY_BLOCKED.
    This verifies the gate is not limited to carriers.
    """
    agent = _make_critic(
        _EchoMerchantTransport(override_prices={BLOCKED_OTA: 50_000})
    )
    legs = [_leg(hotel_id=BLOCKED_OTA, total_cents=50_000)]
    result = _run(agent, legs)

    assert result["decision"] == "rejected", result
    codes = _violation_codes(result)
    assert COUNTERPARTY_BLOCKED in codes, (
        f"BLOCKED OTA must trigger COUNTERPARTY_BLOCKED: {codes}"
    )
    v = _find_violation(result, COUNTERPARTY_BLOCKED)
    assert v["route_to"] == "accommodation"
    assert "blocked" in v["detail"].lower()
    print(
        f"PASS: test_blocked_ota_reseller_triggers_gate "
        f"[band=blocked, ota={BLOCKED_OTA}]"
    )


# ===========================================================================
# 4. CLEAR counterparty → NO COUNTERPARTY_BLOCKED, decision=verified
# ===========================================================================

def test_clear_counterparty_not_flagged() -> None:
    """
    A counterparty with score=91 (CLEAR band) must NEVER produce a
    COUNTERPARTY_BLOCKED violation.  This is the regression anchor: if the gate
    fires on clear counterparties the system becomes unusable.
    """
    agent = _make_critic(
        _EchoMerchantTransport(override_prices={CLEAR_CARRIER: 50_000})
    )
    legs = [_leg(hotel_id=CLEAR_CARRIER, total_cents=50_000)]
    result = _run(agent, legs)

    codes = _violation_codes(result)
    assert COUNTERPARTY_BLOCKED not in codes, (
        f"CLEAR counterparty must NOT be flagged: {codes}"
    )
    assert result["decision"] == "verified", result
    print(
        f"PASS: test_clear_counterparty_not_flagged [band=clear]"
    )


# ===========================================================================
# 5. ELEVATED counterparty → committable, NOT flagged
# ===========================================================================

def test_elevated_counterparty_not_flagged() -> None:
    """
    An ELEVATED counterparty (score=52, committable=True, advisory) must NOT
    trigger COUNTERPARTY_BLOCKED.  ELEVATED is in the committable set — only
    BLOCKED and UNKNOWN are gated.
    """
    agent = _make_critic(
        _EchoMerchantTransport(override_prices={ELEVATED_CARRIER: 50_000})
    )
    legs = [_leg(hotel_id=ELEVATED_CARRIER, total_cents=50_000)]
    result = _run(agent, legs)

    codes = _violation_codes(result)
    assert COUNTERPARTY_BLOCKED not in codes, (
        f"ELEVATED counterparty must NOT be flagged (committable): {codes}"
    )
    assert result["decision"] == "verified", result
    print(
        f"PASS: test_elevated_counterparty_not_flagged [band=elevated]"
    )


# ===========================================================================
# 6. Valid consent token overrides BLOCKED gate → no violation
# ===========================================================================

def test_valid_consent_token_overrides_blocked_gate() -> None:
    """
    A VALID structured consent token ("consent:{cid}:{band}:{nonce}" bound to
    the counterparty and consistent with its observed band) must lift the
    COUNTERPARTY_BLOCKED gate and allow a "verified" result.

    This tests HIGH C1(a): the consent-override path.
    """
    agent = _make_critic(
        _EchoMerchantTransport(override_prices={BLOCKED_CARRIER: 50_000})
    )
    legs = [_leg(hotel_id=BLOCKED_CARRIER, total_cents=50_000)]
    valid_token = _valid_consent(BLOCKED_CARRIER, RiskBand.BLOCKED)
    result = _run(
        agent, legs,
        consent_tokens={BLOCKED_CARRIER: valid_token},
    )

    codes = _violation_codes(result)
    assert COUNTERPARTY_BLOCKED not in codes, (
        f"A valid consent token must lift the BLOCKED gate: {codes}"
    )
    assert result["decision"] == "verified", (
        f"Valid consent token must produce 'verified': {result}"
    )
    print(
        f"PASS: test_valid_consent_token_overrides_blocked_gate "
        f"[token={valid_token!r}]"
    )


# ===========================================================================
# 7. Garbage token does NOT override — COUNTERPARTY_BLOCKED still fires
# ===========================================================================

def test_garbage_consent_token_does_not_override() -> None:
    """
    A random string (not the structured "consent:{cid}:{band}:{nonce}" form)
    must NOT override the gate.  The gate remains blocked.

    HIGH C1(a) fail-conservative: mere non-empty presence is NOT enough.
    """
    agent = _make_critic(
        _EchoMerchantTransport(override_prices={BLOCKED_CARRIER: 50_000})
    )
    legs = [_leg(hotel_id=BLOCKED_CARRIER, total_cents=50_000)]
    result = _run(
        agent, legs,
        consent_tokens={BLOCKED_CARRIER: "fresh-xyz-garbage"},
    )

    codes = _violation_codes(result)
    assert COUNTERPARTY_BLOCKED in codes, (
        f"A garbage token must NOT lift the gate: {codes}"
    )
    assert result["decision"] == "rejected", result
    print(
        f"PASS: test_garbage_consent_token_does_not_override "
        f"[token='fresh-xyz-garbage']"
    )


# ===========================================================================
# 8. UNBOUND token (right form, wrong counterparty) does NOT override
# ===========================================================================

def test_unbound_consent_token_does_not_override() -> None:
    """
    A token "consent:{other_cid}:{band}:{nonce}" — correctly structured but
    bound to a DIFFERENT counterparty — must not override the gate for this
    counterparty.

    HIGH C1(a) BINDING rule: a token minted for carrier-A cannot release carrier-B.
    """
    agent = _make_critic(
        _EchoMerchantTransport(override_prices={BLOCKED_CARRIER: 50_000})
    )
    legs = [_leg(hotel_id=BLOCKED_CARRIER, total_cents=50_000)]
    # Token claims to consent for a DIFFERENT counterparty (the OTA).
    unbound_token = _valid_consent(BLOCKED_OTA, RiskBand.BLOCKED)
    result = _run(
        agent, legs,
        consent_tokens={BLOCKED_CARRIER: unbound_token},
    )

    codes = _violation_codes(result)
    assert COUNTERPARTY_BLOCKED in codes, (
        f"A token bound to a different counterparty must NOT override: {codes}"
    )
    print(
        f"PASS: test_unbound_consent_token_does_not_override "
        f"[token_for={BLOCKED_OTA!r}, vetted={BLOCKED_CARRIER!r}]"
    )


# ===========================================================================
# 9. Band-mismatched token does NOT override
# ===========================================================================

def test_band_mismatched_token_does_not_override() -> None:
    """
    A token "consent:{cid}:elevated:{nonce}" claiming band=elevated for a
    counterparty the seed says is BLOCKED must not override (stale token).

    HIGH C1(a) CONSISTENCY rule: the token's claimed band must match the
    OBSERVED band the gate just derived from the seeded score.
    """
    agent = _make_critic(
        _EchoMerchantTransport(override_prices={BLOCKED_CARRIER: 50_000})
    )
    legs = [_leg(hotel_id=BLOCKED_CARRIER, total_cents=50_000)]
    # Stale token that claims band=elevated for a counterparty that is BLOCKED.
    stale_token = _valid_consent(BLOCKED_CARRIER, RiskBand.ELEVATED)
    result = _run(
        agent, legs,
        consent_tokens={BLOCKED_CARRIER: stale_token},
    )

    codes = _violation_codes(result)
    assert COUNTERPARTY_BLOCKED in codes, (
        f"A band-mismatched (stale) token must NOT override: {codes}"
    )
    print(
        f"PASS: test_band_mismatched_token_does_not_override "
        f"[token_band=elevated, actual_band=blocked]"
    )


# ===========================================================================
# 10. Multi-leg: only the blocked leg is flagged; the clear leg is untouched
# ===========================================================================

def test_multi_leg_only_blocked_leg_flagged() -> None:
    """
    Two legs: one with a CLEAR counterparty, one with a BLOCKED counterparty.
    Only the blocked leg should produce a COUNTERPARTY_BLOCKED violation; the
    clear leg must not appear in violations.

    Also verifies that the leg_id in the violation identifies the correct leg.
    """
    agent = _make_critic(
        _EchoMerchantTransport(
            override_prices={
                CLEAR_CARRIER: 30_000,
                BLOCKED_CARRIER: 30_000,
            }
        )
    )
    legs = [
        _leg(leg_id="leg-clear", hotel_id=CLEAR_CARRIER, total_cents=30_000),
        _leg(
            leg_id="leg-blocked",
            hotel_id=BLOCKED_CARRIER,
            checkin="2026-08-04",
            checkout="2026-08-07",
            total_cents=30_000,
        ),
    ]
    result = _run(agent, legs, budget=500_000)

    codes = _violation_codes(result)
    assert COUNTERPARTY_BLOCKED in codes, (
        f"Blocked leg must produce COUNTERPARTY_BLOCKED: {codes}"
    )
    cb_violations = [v for v in result["violations"] if v["code"] == COUNTERPARTY_BLOCKED]
    assert len(cb_violations) == 1, (
        f"Exactly one COUNTERPARTY_BLOCKED violation expected: {cb_violations}"
    )
    assert cb_violations[0]["leg_id"] == "leg-blocked", (
        f"Violation leg_id must be 'leg-blocked', got {cb_violations[0]['leg_id']!r}"
    )
    # The clear leg must not appear in any violation
    all_leg_ids_in_violations = {
        v["leg_id"] for v in result["violations"]
        if v["code"] == COUNTERPARTY_BLOCKED
    }
    assert "leg-clear" not in all_leg_ids_in_violations, (
        f"Clear leg must not be in COUNTERPARTY_BLOCKED violations: "
        f"{all_leg_ids_in_violations}"
    )
    print(
        f"PASS: test_multi_leg_only_blocked_leg_flagged "
        f"[blocked_leg_id={cb_violations[0]['leg_id']!r}]"
    )


# ===========================================================================
# 11. UNKNOWN carrier via explicit counterparty_id → COUNTERPARTY_BLOCKED
# ===========================================================================

def test_unknown_explicit_counterparty_id_triggers_gate() -> None:
    """
    Gate 6b condition (b): an UNKNOWN counterparty (no seeded profile) declared
    via an explicit counterparty_id field in the leg is NEVER silently cleared —
    it triggers COUNTERPARTY_BLOCKED (conservative block, fail-conservative).

    This models a new carrier not yet in the seed table but explicitly named by
    the orchestrator as the vetted supplier.
    """
    agent = _make_critic(
        _EchoMerchantTransport(
            override_prices={UNKNOWN_CARRIER: 50_000}
        )
    )
    # Leg with BOTH hotel_id and counterparty_id set to the unknown carrier.
    legs = [
        _leg(
            hotel_id=UNKNOWN_CARRIER,
            counterparty_id=UNKNOWN_CARRIER,
            total_cents=50_000,
        )
    ]
    result = _run(agent, legs)

    codes = _violation_codes(result)
    assert COUNTERPARTY_BLOCKED in codes, (
        f"UNKNOWN counterparty with explicit counterparty_id must be gated: {codes}"
    )
    v = _find_violation(result, COUNTERPARTY_BLOCKED)
    detail = v["detail"]
    assert "unknown" in detail.lower(), (
        f"Detail must contain the band 'unknown': {detail!r}"
    )
    print(
        f"PASS: test_unknown_explicit_counterparty_id_triggers_gate "
        f"[cp={UNKNOWN_CARRIER!r}, band=unknown]"
    )


# ===========================================================================
# 12. Scoping rule: UNKNOWN hotel_id WITHOUT explicit counterparty_id — NOT gated
# ===========================================================================

def test_unknown_hotel_id_only_not_gated() -> None:
    """
    Gate 6b scoping rule (finding #18 / design decision): a bare hotel_id with
    no seeded solvency profile AND no explicit counterparty_id is NOT gated.

    The bulk hotel catalog (~14.8k hotels) is intentionally out-of-scope for
    lodging solvency at this phase.  An UNKNOWN band on a mere hotel_id (with no
    explicit counterparty_id declaring a vetted supplier) must NOT block — only
    KNOWN-blocked or explicitly-declared counterparties trigger the gate.

    A regression here would break virtually every ordinary itinerary.
    """
    agent = _make_critic(
        _EchoMerchantTransport(
            override_prices={UNSEEN_HOTEL_ID: 50_000}
        )
    )
    legs = [
        _leg(
            hotel_id=UNSEEN_HOTEL_ID,
            # NO counterparty_id field — only hotel_id
            total_cents=50_000,
        )
    ]
    result = _run(agent, legs)

    codes = _violation_codes(result)
    assert COUNTERPARTY_BLOCKED not in codes, (
        f"An UNKNOWN bare hotel_id (no explicit counterparty_id) must NOT "
        f"trigger COUNTERPARTY_BLOCKED (scoping rule): {codes}"
    )
    assert result["decision"] == "verified", result
    print(
        f"PASS: test_unknown_hotel_id_only_not_gated "
        f"[hotel_id={UNSEEN_HOTEL_ID!r}]"
    )


# ===========================================================================
# 13. Solvency score embedded in detail (regression guard)
# ===========================================================================

def test_counterparty_blocked_detail_contains_solvency_score() -> None:
    """
    The violation detail (lines 891-895) is templated as:
      "Leg {leg_id!r} counterparty {cp_id!r}: solvency risk_band {band!r} (score
       {fv.get('solvency_score')}) — NOT committable ..."

    Assert that the solvency score from the seed table IS present in the detail
    so that a future change that strips the score (or omits it) is caught.  The
    score for BLOCKED_CARRIER is 28.
    """
    agent = _make_critic(
        _EchoMerchantTransport(override_prices={BLOCKED_CARRIER: 50_000})
    )
    legs = [_leg(hotel_id=BLOCKED_CARRIER, total_cents=50_000)]
    result = _run(agent, legs)

    v = _find_violation(result, COUNTERPARTY_BLOCKED)
    detail = v["detail"]
    # The seeded score for BLOCKED_CARRIER is 28 (from fraud_agent.py seed table).
    assert "28" in detail, (
        f"Solvency score (28) must appear in COUNTERPARTY_BLOCKED detail: {detail!r}"
    )
    print(
        f"PASS: test_counterparty_blocked_detail_contains_solvency_score "
        f"[score=28 found in detail]"
    )


# ===========================================================================
# 14. Via ASGI TestClient (end-to-end through the A2A message/send surface)
# ===========================================================================

def test_counterparty_blocked_via_asgi_surface() -> None:
    """
    Full end-to-end path through the Starlette ASGI app / JSON-RPC message/send
    surface (the same path the orchestrator uses).

    The counterparty_consent_tokens can be included in the payload dict, and the
    fraud gate must fire exactly as it does via the _verify_itinerary call.

    This pins the payload-extraction → gate → output-artifact chain.
    """
    agent = _make_critic(
        _EchoMerchantTransport(override_prices={BLOCKED_CARRIER: 50_000})
    )
    client = TestClient(agent.build_app(), raise_server_exceptions=True)

    payload: dict[str, Any] = {
        "user_id": "u-asgi-test",
        "total_budget_cents": 500_000,
        "legs": [
            {
                "leg_id": "leg-0",
                "city": "jakarta",
                "checkin": "2026-08-01",
                "checkout": "2026-08-04",
                "adults": 1,
                "hotel_id": BLOCKED_CARRIER,
                "total_cents": 50_000,
                "provenance": "merchant",
            }
        ],
        # No consent token → gate must fire.
    }
    msg = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": {
                "kind": "message",
                "messageId": str(uuid.uuid4()),
                "role": "user",
                "parts": [{"kind": "data", "data": payload}],
                "metadata": {"skillId": "itinerary.verify"},
            }
        },
    }
    resp = client.post("/", json=msg)
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
    rpc = resp.json()
    assert "error" not in rpc, f"RPC error: {rpc.get('error')}"
    task = rpc["result"]

    # Extract the CriticResult from the artifact
    result: dict[str, Any] | None = None
    for artifact in task.get("artifacts", []):
        for part in artifact.get("parts", []):
            if part.get("kind") == "data":
                result = part["data"]
    assert result is not None, f"No data part in task: {task}"

    assert result["decision"] == "rejected", result
    codes = _violation_codes(result)
    assert COUNTERPARTY_BLOCKED in codes, (
        f"COUNTERPARTY_BLOCKED must appear via the ASGI surface: {codes}"
    )
    v = _find_violation(result, COUNTERPARTY_BLOCKED)
    assert v["route_to"] == "accommodation"
    print(
        f"PASS: test_counterparty_blocked_via_asgi_surface "
        f"[decision=rejected, code=COUNTERPARTY_BLOCKED]"
    )


# ===========================================================================
# 15. Consent token in payload dict (ASGI path) lifts the gate
# ===========================================================================

def test_consent_token_in_payload_lifts_gate_via_asgi() -> None:
    """
    When counterparty_consent_tokens is supplied as a key in the JSON payload
    (the A2A message path), a valid token must lift the gate end-to-end.

    This verifies the payload-extraction path (lines 480-482 in critic_agent.py)
    correctly feeds consent_tokens into _verify_itinerary.
    """
    agent = _make_critic(
        _EchoMerchantTransport(override_prices={BLOCKED_CARRIER: 50_000})
    )
    client = TestClient(agent.build_app(), raise_server_exceptions=True)

    valid_token = _valid_consent(BLOCKED_CARRIER, RiskBand.BLOCKED)
    payload: dict[str, Any] = {
        "user_id": "u-asgi-consent-test",
        "total_budget_cents": 500_000,
        "legs": [
            {
                "leg_id": "leg-0",
                "city": "jakarta",
                "checkin": "2026-08-01",
                "checkout": "2026-08-04",
                "adults": 1,
                "hotel_id": BLOCKED_CARRIER,
                "total_cents": 50_000,
                "provenance": "merchant",
            }
        ],
        # Valid consent token supplied in the payload.
        "counterparty_consent_tokens": {BLOCKED_CARRIER: valid_token},
    }
    msg = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": {
                "kind": "message",
                "messageId": str(uuid.uuid4()),
                "role": "user",
                "parts": [{"kind": "data", "data": payload}],
                "metadata": {"skillId": "itinerary.verify"},
            }
        },
    }
    resp = client.post("/", json=msg)
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
    task = resp.json()["result"]

    result_data: dict[str, Any] | None = None
    for artifact in task.get("artifacts", []):
        for part in artifact.get("parts", []):
            if part.get("kind") == "data":
                result_data = part["data"]
    assert result_data is not None

    codes = _violation_codes(result_data)
    assert COUNTERPARTY_BLOCKED not in codes, (
        f"Valid consent token in payload must lift the gate: {codes}"
    )
    assert result_data["decision"] == "verified", (
        f"Valid consent must yield 'verified': {result_data}"
    )
    print(
        f"PASS: test_consent_token_in_payload_lifts_gate_via_asgi "
        f"[token={valid_token!r}]"
    )


# ===========================================================================
# Test runner (standalone, mirrors the house convention)
# ===========================================================================

TESTS = [
    test_fraud_vet_wired_into_critic_module,
    test_blocked_carrier_hotel_id_triggers_counterparty_blocked,
    test_blocked_carrier_explicit_counterparty_id_triggers_gate,
    test_blocked_ota_reseller_triggers_gate,
    test_clear_counterparty_not_flagged,
    test_elevated_counterparty_not_flagged,
    test_valid_consent_token_overrides_blocked_gate,
    test_garbage_consent_token_does_not_override,
    test_unbound_consent_token_does_not_override,
    test_band_mismatched_token_does_not_override,
    test_multi_leg_only_blocked_leg_flagged,
    test_unknown_explicit_counterparty_id_triggers_gate,
    test_unknown_hotel_id_only_not_gated,
    test_counterparty_blocked_detail_contains_solvency_score,
    test_counterparty_blocked_via_asgi_surface,
    test_consent_token_in_payload_lifts_gate_via_asgi,
]


def main() -> None:
    import traceback

    passed = failed = 0
    print(f"\n{'=' * 72}")
    print("Travel Guild — Critic COUNTERPARTY_BLOCKED Gate (money-safety)")
    print(f"{'=' * 72}\n")
    for fn in TESTS:
        try:
            fn()
            passed += 1
        except Exception as exc:
            failed += 1
            print(f"FAIL: {fn.__name__} — {exc}")
            traceback.print_exc()
    print(f"\n{'=' * 72}")
    print(f"Results: {passed} passed, {failed} failed of {len(TESTS)}")
    print(f"{'=' * 72}\n")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
