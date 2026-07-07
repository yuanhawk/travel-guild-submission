"""
test_inv_fraud_consent_override.py — STRONG deterministic invariant test for the
FRAUD CONSENT-OVERRIDE e2e invariant (Travel Guild, fraud_agent.py).

INVARIANT UNDER TEST
────────────────────
  A VALID consent token MUST flip vet_counterparty(...) committable False → True
  with gate_reason starting with "BLOCK_OVERRIDDEN_BY_CONSENT".

  An UNKNOWN counterparty (no seeded profile) MUST conservatively BLOCK
  (committable=False) without a valid consent token.

WHY A SEPARATE FILE
───────────────────
  validate_consent_token() is already exercised in isolation by test_fraud_agent_cov2.py.
  What is NOT yet tested end-to-end is the FULL FLIP via vet_counterparty():

    Step 1 — same counterparty, NO token    → committable=False, gate_reason contains
                                              "BLOCK_INSOLVENCY_RISK"
    Step 2 — same counterparty, VALID token → committable=True,  gate_reason starts
                                              with "BLOCK_OVERRIDDEN_BY_CONSENT"

  These assertions would CATCH a regression where:
    - the consent path is short-circuited (token accepted but committable not set),
    - the gate_reason string changes or is omitted,
    - validate_consent_token is called but its return value is ignored,
    - or the blocked→committable logic is wired incorrectly.

DESIGN RULES
────────────
  - Direct calls only — no ASGI/TestClient, no A2A surface (the FLIP lives inside
    vet_counterparty(); HTTP wrapping is tested elsewhere).
  - No mocking of the unit under test. The only mock-able seam (httpx.BaseTransport /
    DashScope) is never reached in these paths because no DASHSCOPE_API_KEY is set and
    vet_counterparty() does not call the LLM at all.
  - No live network, no LLM, no paid API, no nonce / booking_ref golden values.
  - Deterministic: same inputs → byte-identical outputs across repeated calls.
"""

from __future__ import annotations

import os
import sys

# conftest.py inserts society/ into sys.path; this ensures the module is importable
# when the file is run standalone via `python3 -m pytest`.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.fraud_agent import (
    RiskBand,
    vet_counterparty,
    validate_consent_token,
)

# ── Seeded fixtures (scores from _SOLVENCY_BY_COUNTERPARTY) ──────────────────
# BLOCKED counterparty: carrier-skylark-budget-air, seeded score 28 (< 45 → blocked).
# This is the DC3 cheapest-carrier trap seeded to demonstrate the N1 fault class.
BLOCKED_CP = "carrier-skylark-budget-air"

# UNKNOWN counterparty: not present in the seed table at all.
# The gate must conservatively block this — NEVER silently clear.
UNKNOWN_CP = "carrier-totally-unknown-xyz-9999"


def _valid_consent(counterparty_id: str, band: str, nonce: str = "grant-2026-test") -> str:
    """Build a VALID structured consent token bound to this counterparty and band."""
    return f"consent:{counterparty_id}:{band}:{nonce}"


# ─────────────────────────────────────────────────────────────────────────────
# PART A — The FLIP: blocked counterparty False → True via valid consent token
# ─────────────────────────────────────────────────────────────────────────────

def test_blocked_without_consent_is_not_committable():
    """
    STEP 1 of the FLIP: same counterparty, no consent token.
    The gate must block committable=False.
    """
    v = vet_counterparty(BLOCKED_CP)

    assert v["risk_band"] == RiskBand.BLOCKED, (
        f"Expected risk_band=blocked for {BLOCKED_CP!r}; got {v['risk_band']!r}. "
        "If this changes, the seeded score for this counterparty was altered."
    )
    assert v["committable"] is False, (
        f"BLOCKED counterparty {BLOCKED_CP!r} must NOT be committable without a "
        "consent token; got committable=True (gate is open — regression)."
    )
    assert v["consent_required"] is True, (
        "consent_required must be True when committable=False due to solvency block."
    )
    assert v["consent_supplied"] is False, (
        "consent_supplied must be False when no token was passed."
    )
    assert v["gate_reason"] is not None, (
        "gate_reason must be set (non-None) when the gate blocks a counterparty."
    )
    assert "BLOCK_INSOLVENCY_RISK" in v["gate_reason"], (
        f"gate_reason must contain 'BLOCK_INSOLVENCY_RISK' before consent is "
        f"supplied; got: {v['gate_reason']!r}"
    )


def test_blocked_with_valid_consent_flips_committable_to_true():
    """
    STEP 2 of the FLIP: same counterparty, VALID consent token supplied.
    committable must flip from False to True, and gate_reason must begin with
    BLOCK_OVERRIDDEN_BY_CONSENT — the exact string the spec defines.
    """
    token = _valid_consent(BLOCKED_CP, RiskBand.BLOCKED)
    v = vet_counterparty(BLOCKED_CP, consent_token=token)

    # The band is derived from the seeded score and MUST NOT change when consent
    # is supplied — the consent does not alter the risk assessment, only the
    # commit decision.
    assert v["risk_band"] == RiskBand.BLOCKED, (
        "Supplying a consent token must NOT change the risk_band — the seeded score "
        f"still puts {BLOCKED_CP!r} at 'blocked'; got {v['risk_band']!r}."
    )

    # THE FLIP: committable must now be True.
    assert v["committable"] is True, (
        f"BLOCKED counterparty {BLOCKED_CP!r} with a VALID consent token must flip "
        "committable to True; got committable=False (consent path is broken)."
    )

    # Consent was recognised and consumed.
    assert v["consent_supplied"] is True, (
        "consent_supplied must be True when a valid token was accepted and "
        "the committable flip was applied."
    )

    # consent_required stays True even after consent is supplied — it records that
    # a token WAS required; supplying one satisfies the requirement, not removes it.
    assert v["consent_required"] is True, (
        "consent_required must remain True even when consent is supplied "
        "(it records that a token was required, not that none is needed)."
    )

    # gate_reason must be set and must start with the canonical override prefix.
    assert v["gate_reason"] is not None, (
        "gate_reason must be set (non-None) when the gate was overridden by consent "
        "(the override must be auditable, not silent)."
    )
    assert v["gate_reason"].startswith("BLOCK_OVERRIDDEN_BY_CONSENT"), (
        f"gate_reason must start with 'BLOCK_OVERRIDDEN_BY_CONSENT' after a valid "
        f"consent token flips the gate; got: {v['gate_reason']!r}"
    )


def test_flip_is_a_state_transition_not_a_side_effect():
    """
    The flip must be repeatable and deterministic (not a one-shot side-effect):
    calling vet_counterparty WITHOUT consent always returns committable=False,
    and calling WITH consent always returns committable=True.
    Interleave the calls to confirm independence.
    """
    token = _valid_consent(BLOCKED_CP, RiskBand.BLOCKED)

    for _ in range(3):
        v_no = vet_counterparty(BLOCKED_CP)
        assert v_no["committable"] is False, (
            "Without consent, committable must remain False on repeated calls."
        )
        v_yes = vet_counterparty(BLOCKED_CP, consent_token=token)
        assert v_yes["committable"] is True, (
            "With consent, committable must remain True on repeated calls."
        )


def test_gate_reason_prefix_exact_match_not_just_substring():
    """
    The gate_reason must START WITH 'BLOCK_OVERRIDDEN_BY_CONSENT' (not just contain
    it as a substring elsewhere). A regression that swaps the prefix order would only
    be caught by this stricter check.
    """
    token = _valid_consent(BLOCKED_CP, RiskBand.BLOCKED)
    v = vet_counterparty(BLOCKED_CP, consent_token=token)

    reason = v["gate_reason"]
    assert isinstance(reason, str) and reason.startswith("BLOCK_OVERRIDDEN_BY_CONSENT"), (
        f"gate_reason must start with 'BLOCK_OVERRIDDEN_BY_CONSENT' (not just contain "
        f"it); got: {reason!r}"
    )
    # And it must name the counterparty (auditable override — human readable).
    assert BLOCKED_CP in reason, (
        f"gate_reason should identify the counterparty {BLOCKED_CP!r}; "
        f"got: {reason!r}"
    )


def test_solvency_score_unchanged_across_the_flip():
    """
    The seeded solvency score must be identical before and after supplying consent.
    If the score changes, the gate is deriving the number from something other than
    the seed table (NO-LLM-NUMBERS invariant violation).
    """
    token = _valid_consent(BLOCKED_CP, RiskBand.BLOCKED)
    v_before = vet_counterparty(BLOCKED_CP)
    v_after = vet_counterparty(BLOCKED_CP, consent_token=token)

    assert v_before["solvency_score"] == v_after["solvency_score"], (
        "solvency_score must be identical before and after supplying consent — "
        "the score is a seeded constant, not computed at consent time."
    )
    assert v_before["solvency_score"] is not None, (
        f"BLOCKED counterparty {BLOCKED_CP!r} must have a non-None seeded solvency score."
    )


# ─────────────────────────────────────────────────────────────────────────────
# PART B — UNKNOWN counterparty conservative block
# ─────────────────────────────────────────────────────────────────────────────

def test_unknown_counterparty_is_conservatively_blocked():
    """
    An UNKNOWN counterparty (no seeded solvency profile) MUST conservatively BLOCK.
    It must NEVER be silently treated as clear/committable — UNKNOWN is non-committable
    by spec (the gate: unknown → conservative block).
    """
    v = vet_counterparty(UNKNOWN_CP)

    assert v["risk_band"] == RiskBand.UNKNOWN, (
        f"A counterparty with no seeded profile must have risk_band=unknown; "
        f"got {v['risk_band']!r}. If 'clear' or 'elevated', the gate is silently "
        "approving unknown counterparties — a money-safety regression."
    )
    assert v["committable"] is False, (
        f"UNKNOWN counterparty {UNKNOWN_CP!r} must NOT be committable (conservative "
        "block — NEVER silent-OK); got committable=True."
    )
    assert v["consent_required"] is True, (
        "An unknown counterparty requires consent (it is gated), so consent_required "
        "must be True."
    )
    assert v["solvency_score"] is None, (
        "An UNKNOWN counterparty has no seeded score; solvency_score must be None."
    )
    assert v["consent_supplied"] is False, (
        "consent_supplied must be False when no token was passed."
    )


def test_unknown_with_valid_consent_also_flips():
    """
    An UNKNOWN counterparty with a consent token bound to 'unknown' band MUST also
    flip to committable=True — the consent-override path must work for UNKNOWN
    just as it does for BLOCKED.
    """
    token = _valid_consent(UNKNOWN_CP, RiskBand.UNKNOWN)
    v = vet_counterparty(UNKNOWN_CP, consent_token=token)

    assert v["risk_band"] == RiskBand.UNKNOWN, (
        "risk_band must remain 'unknown' even when consent is supplied."
    )
    assert v["committable"] is True, (
        f"UNKNOWN counterparty {UNKNOWN_CP!r} with a VALID consent token must flip "
        "committable to True; got committable=False."
    )
    assert v["consent_supplied"] is True
    assert v["gate_reason"] is not None
    assert v["gate_reason"].startswith("BLOCK_OVERRIDDEN_BY_CONSENT"), (
        f"gate_reason for UNKNOWN+consent must start with 'BLOCK_OVERRIDDEN_BY_CONSENT'; "
        f"got: {v['gate_reason']!r}"
    )


def test_unknown_without_consent_is_not_silently_committable():
    """
    Regression guard: an UNKNOWN counterparty must NEVER become committable
    without consent regardless of how many times the call is repeated.
    (Catches any stateful or probabilistic path in the gate.)
    """
    for _ in range(5):
        v = vet_counterparty(UNKNOWN_CP)
        assert v["committable"] is False, (
            f"UNKNOWN counterparty {UNKNOWN_CP!r} became committable without consent "
            "on a repeated call — the gate is non-deterministic or has been opened."
        )
        assert v["risk_band"] == RiskBand.UNKNOWN


# ─────────────────────────────────────────────────────────────────────────────
# PART C — Negative cases: tokens that must NOT flip the gate
# These ensure the flip is tightly gated (not presence-only).
# ─────────────────────────────────────────────────────────────────────────────

def test_garbage_token_does_not_flip_blocked():
    """
    A non-empty but unstructured/garbage token must NOT flip the gate.
    Mere non-empty presence is not enough (HIGH C1(a) — fail-conservative).
    """
    garbage_tokens = [
        "any-truthy-string",
        "yes-i-consent",
        "consent_token_here",
        "1",
        "true",
    ]
    for tok in garbage_tokens:
        v = vet_counterparty(BLOCKED_CP, consent_token=tok)
        assert v["committable"] is False, (
            f"Garbage token {tok!r} must NOT flip BLOCKED counterparty to committable; "
            "got committable=True (presence-only check is leaking)."
        )
        assert v["consent_supplied"] is False, (
            f"consent_supplied must be False for garbage token {tok!r}."
        )


def test_unbound_token_does_not_flip_blocked():
    """
    A token bound to a DIFFERENT counterparty must NOT flip this blocked counterparty.
    (BINDING rule: a token minted for carrier-A cannot release carrier-B.)
    """
    # Build a valid token but bound to a different counterparty id.
    other_cp = "carrier-garuda-indonesia"
    wrong_binding = _valid_consent(other_cp, RiskBand.BLOCKED)

    v = vet_counterparty(BLOCKED_CP, consent_token=wrong_binding)
    assert v["committable"] is False, (
        f"A token bound to {other_cp!r} must NOT release {BLOCKED_CP!r}; "
        "got committable=True (BINDING rule is violated)."
    )
    assert v["consent_supplied"] is False


def test_band_mismatched_token_does_not_flip_blocked():
    """
    A token claiming a band that does not match the observed (seed-derived) band
    must NOT flip the gate. (CONSISTENCY rule.)
    """
    # Token claims the band is 'unknown' but the seed says 'blocked'.
    mismatched = _valid_consent(BLOCKED_CP, RiskBand.UNKNOWN)
    v = vet_counterparty(BLOCKED_CP, consent_token=mismatched)
    assert v["committable"] is False, (
        "A band-mismatched token (claims 'unknown', observed 'blocked') must NOT "
        "flip the gate; got committable=True (CONSISTENCY rule is violated)."
    )
    assert v["consent_supplied"] is False


def test_empty_and_whitespace_token_does_not_flip_blocked():
    """
    Empty string and whitespace-only tokens must not flip the gate.
    """
    for empty in ("", "   ", "\t", "\n"):
        v = vet_counterparty(BLOCKED_CP, consent_token=empty)
        assert v["committable"] is False, (
            f"Whitespace/empty token {empty!r} must NOT flip BLOCKED to committable."
        )
        assert v["consent_supplied"] is False


def test_none_token_does_not_flip_blocked():
    """None (default) must not flip the gate."""
    v = vet_counterparty(BLOCKED_CP, consent_token=None)
    assert v["committable"] is False
    assert v["consent_supplied"] is False


# ─────────────────────────────────────────────────────────────────────────────
# PART D — Validate_consent_token isolation cross-check
# The token validator must agree with what vet_counterparty just did.
# ─────────────────────────────────────────────────────────────────────────────

def test_validate_consent_token_isolation_agrees_with_flip():
    """
    The validate_consent_token function (isolated) must return True for exactly
    the same token that causes vet_counterparty to set committable=True.
    If the two disagree, the gate is using a different validation path than the
    exported function — a hidden logic split.
    """
    token = _valid_consent(BLOCKED_CP, RiskBand.BLOCKED)

    # Isolated validation
    is_valid = validate_consent_token(
        token,
        counterparty_id=BLOCKED_CP,
        observed_band=RiskBand.BLOCKED,
    )
    assert is_valid is True, (
        "validate_consent_token must return True for the same token that should "
        "flip vet_counterparty to committable=True."
    )

    # End-to-end flip
    v = vet_counterparty(BLOCKED_CP, consent_token=token)
    assert v["committable"] is True, (
        "vet_counterparty must flip committable to True for the same token that "
        "validate_consent_token accepted — the two paths must agree."
    )


def test_validate_consent_token_isolation_rejects_same_garbage():
    """
    A token that validate_consent_token rejects (garbage) must also leave
    vet_counterparty with committable=False (the two must stay in sync).
    """
    garbage = "not-a-real-consent-token"

    is_valid = validate_consent_token(
        garbage,
        counterparty_id=BLOCKED_CP,
        observed_band=RiskBand.BLOCKED,
    )
    assert is_valid is False

    v = vet_counterparty(BLOCKED_CP, consent_token=garbage)
    assert v["committable"] is False, (
        "vet_counterparty must NOT flip committable to True for a token that "
        "validate_consent_token rejected."
    )


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
