"""
peril_crosswalk.py — PHASE-0 Risk→Insurance peril crosswalk (§19.1, §19.5).

Design contract: AGENT-SOCIETY-A2A-DESIGN.md §19.1 ("Risk→Insurance peril
crosswalk missing — GAP; DC0 depends on it: civil_unrest must flow
Risk→Insurance. Lock ONE canonical peril enum; Risk owns the threat SIGNAL,
Insurance maps it to COVERAGE."), §19.5 ("Risk→Insurance peril crosswalk is a
PHASE-0 CONTRACT — Insurance/DC0 needs it before the Risk agent exists").

WHAT THIS IS
------------
A deterministic, TOTAL crosswalk mapping every Risk advisory reason-code
(contracts.RiskReasonCode — the threat SIGNAL Risk owns) to a canonical
insurance peril class (contracts.Peril — the coverage key Insurance owns).

  Risk emits reason_codes  ──[this crosswalk]──>  Insurance peril_class

"TOTAL" means: every RiskReasonCode has exactly one peril mapping, asserted by
a module-load self-check and a unit test. This guarantees Insurance (DC0) can be
built and verified BEFORE the Risk agent exists (§19.5 build order), because the
contract between them is locked here.

Determinism: a pure dict lookup over closed sets → zero variance. No LLM, no
network, no domain scoring (Risk decides the advisory_level; Insurance decides
coverage/exclusion; this module only TRANSLATES the vocabulary between them).

DC0 anchor: RiskReasonCode.CIVIL_UNREST → Peril.CIVIL_UNREST and
RiskReasonCode.ARMED_CONFLICT → Peril.WAR are the exclusion perils DC0 relies
on (the society must never report "covered" for a war/unrest-excluded peril).
"""

from __future__ import annotations

from core.contracts import Peril, RiskReasonCode, is_valid_peril, is_valid_risk_reason

# ---------------------------------------------------------------------------
# The crosswalk — ONE deterministic, TOTAL Risk-reason → Insurance-peril map.
# Keyed by RiskReasonCode.value → Peril.value. Every reason-code MUST appear
# (enforced at module load + by test_peril_crosswalk).
# ---------------------------------------------------------------------------
_RISK_TO_PERIL: dict[str, str] = {
    RiskReasonCode.CIVIL_UNREST.value:         Peril.CIVIL_UNREST.value,    # DC0
    RiskReasonCode.ARMED_CONFLICT.value:       Peril.WAR.value,             # DC0
    RiskReasonCode.TERRORISM.value:            Peril.TERRORISM.value,
    RiskReasonCode.NATURAL_DISASTER.value:     Peril.NATURAL_DISASTER.value,
    RiskReasonCode.FLOOD.value:                Peril.NATURAL_DISASTER.value,
    RiskReasonCode.DISEASE_OUTBREAK.value:     Peril.PANDEMIC.value,
    RiskReasonCode.CRIME.value:                Peril.PERSONAL_LIABILITY.value,
    RiskReasonCode.TRANSPORT_DISRUPTION.value: Peril.TRAVEL_DELAY.value,
    RiskReasonCode.HEALTH_HAZARD.value:        Peril.MEDICAL.value,
    # Generic elevated advisory with no specific cause: benign PERSONAL_LIABILITY
    # proxy, NEVER WAR — only ARMED_CONFLICT may reach WAR/EXC-WAR-2 (D5).
    RiskReasonCode.ADVISORY_ELEVATED.value:    Peril.PERSONAL_LIABILITY.value,
}


def _assert_total_and_valid() -> None:
    """
    Module-load self-check: the crosswalk is TOTAL over RiskReasonCode and every
    value is a valid canonical Peril. Fails loud at import if a reason-code was
    added without a mapping (so the gap can never reach integration).
    """
    reason_codes = {r.value for r in RiskReasonCode}
    mapped = set(_RISK_TO_PERIL)
    missing = reason_codes - mapped
    extra = mapped - reason_codes
    if missing:
        raise AssertionError(
            f"peril_crosswalk: NOT TOTAL — RiskReasonCode(s) without a peril mapping: "
            f"{sorted(missing)}"
        )
    if extra:
        raise AssertionError(
            f"peril_crosswalk: maps unknown reason-code(s) not in RiskReasonCode: "
            f"{sorted(extra)}"
        )
    for rc, peril in _RISK_TO_PERIL.items():
        if not is_valid_peril(peril):
            raise AssertionError(
                f"peril_crosswalk: reason-code {rc!r} maps to non-canonical peril {peril!r}"
            )


_assert_total_and_valid()


def risk_reason_to_peril(reason_code: str) -> str:
    """
    Translate a Risk advisory reason-code → canonical insurance peril class.

    Deterministic + total over the closed RiskReasonCode set. Raises KeyError on
    an unknown reason-code (a caller passing an out-of-set code is a contract
    violation, not a recoverable input — fail fast rather than guess coverage).
    """
    if not is_valid_risk_reason(reason_code):
        raise KeyError(
            f"peril_crosswalk: {reason_code!r} is not a valid RiskReasonCode "
            f"(closed set: {sorted(r.value for r in RiskReasonCode)})"
        )
    return _RISK_TO_PERIL[reason_code]


def risk_reasons_to_perils(reason_codes: list[str]) -> list[str]:
    """
    Map a list of Risk reason-codes → de-duplicated, STABLY-ORDERED peril list.

    Order is preserved by first appearance (deterministic). Used by Insurance to
    turn a Risk advisory's reason_codes[] into the set of perils to assess.
    """
    out: list[str] = []
    for rc in reason_codes:
        peril = risk_reason_to_peril(rc)
        if peril not in out:
            out.append(peril)
    return out


def crosswalk_table() -> dict[str, str]:
    """Return a copy of the full crosswalk (for inspection / the seeded table)."""
    return dict(_RISK_TO_PERIL)
