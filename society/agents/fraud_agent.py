"""
fraud_agent.py — FRAUD / COUNTERPARTY-TRUST GATE (Travel Guild, L2 / DC-fraud).

═══════════════════════════════════════════════════════════════════════════════
WHAT THIS IS
═══════════════════════════════════════════════════════════════════════════════
The Fraud agent is a **deterministic counterparty-SOLVENCY GATE** — the SIXTH
roadmap specialist built on the §AGENT-EXTENSION PATTERN (insurance_agent.py 9105
→ compliance_agent.py 9106 → risk_agent.py 9107 → health_agent.py 9108 → THIS
9109). It is a money-path GATE in the spirit of Compliance/Health: it does not
emit a fee, but it CONSTRAINS which counterparties Budget may commit against.

It pre-empts fault class **N1 — SUPPLIER INSOLVENCY**: a carrier / OTA / lodging
operator that ceases operations after a ticket is sold. The ticket goes VOID with
NO counterparty to rebook against (distinct from a sold-out hotel — that has a
substitute; an insolvent supplier's paper is worthless). The World-Simulator can
flip a counterparty insolvent (sim.go `sim_set_counterparty` → bookings VOID), so
the DC3 scenario can DEMONSTRATE the very fault this gate pre-empts.

THE ONE DECISION (a pure function over closed sets — NO-LLM-NUMBERS)
-------------------------------------------------------------------
Per-counterparty (catalog id == counterparty id, the Phase-0 §19.1 identity
contract) a **solvency risk_band ∈ {clear, elevated, blocked, unknown}** is
resolved from a SEEDED solvency table (NO LLM in the number / decision path):

  clear     — solvency score high, normal commit.
  elevated   — solvency score moderate; surfaced as an advisory (committable, flag).
  blocked    — solvency score low / on a distress watchlist; NEVER booked without
               an explicit consent token (the gate).
  unknown    — NO seeded solvency profile → CONSERVATIVE BLOCK (never silent-OK).

GATE RULE (the invariant the whole build defends):
  A **blocked** OR **unknown** counterparty is NEVER committable without an
  explicit consent token (a non-empty string supplied by the caller). UNKNOWN
  degrades to a conservative block — it is NEVER silently treated as clear. The
  risk_band is a PURE function of seeded fields; the LLM is cosmetic-only behind
  a validator.

SCOPE — LODGING SOLVENCY (finding #18):
  The seeded table covers CARRIERS and OTA RESELLERS where insolvency risk is
  highest (ticket VOIDs with no counterparty to rebook). For the bulk hotel
  catalog (~14.8k hotels), lodging solvency is intentionally OUT-OF-SCOPE at
  this phase: a hotel going under typically has a physical asset and substitute
  supply (unlike a carrier ticket going VOID). The Critic's commit-time re-check
  gates any hotel_id that IS in the seed table but leaves bare/unseeded hotel_ids
  ungated by design. To gate uniformly, counterparty_ids must be supplied per-leg
  by the orchestrator for every hotel leg (a future hardening step). This design
  decision is explicit and intentional, not a scale accident.

CONSENT TOKEN CONTRACT (HIGH C1(a) — binding + consistency, fail-conservative):
  A consent token overrides a BLOCKED/UNKNOWN gate ONLY if it VALIDATES — mere
  non-empty presence is NOT enough. The token is a structured, deterministic string

      "consent:{counterparty_id}:{risk_band}:{nonce}"

  and it overrides the gate for a given counterparty ONLY when ALL of:
    (1) it parses to the 4-field structured form (scheme == "consent"), AND
    (2) its counterparty_id field == the canonical counterparty_id being vetted
        (BINDING — a token minted for carrier-A cannot release carrier-B), AND
    (3) its risk_band field == the OBSERVED risk_band the gate just derived from the
        seeded score (CONSISTENCY — a token claiming "elevated" cannot release a
        counterparty the seed says is "blocked"; a stale token that no longer
        matches the current band does NOT override), AND
    (4) the nonce field is non-empty (a structured, caller-issued grant marker).
  An unrecognised / garbage / unbound / band-mismatched token NEVER overrides
  (fail-conservative — HONESTY/var-0). Validation is a PURE deterministic string
  check (NO wall-clock / NO random / NO network); the Critic delegates the SAME
  check to vet_counterparty so the binding rule is enforced once, at one authority.

INTERLOCK (defense-in-depth — checked twice)
--------------------------------------------
  1. FRAUD (advisory, pre-commit): the orchestrator calls ``fraud.vet`` to learn
     which counterparties are committable; Budget only commits the cleared set.
  2. CRITIC (commit-time re-check): the Critic INDEPENDENTLY re-derives each
     leg's counterparty risk_band from the SAME seeded table at verify time, so
     a MISSED fraud verdict (Fraud bypassed / a slip) can NEVER let an insolvent
     supplier through to commit. (critic_agent.py COUNTERPARTY_BLOCKED gate.)

THE §AGENT-EXTENSION PATTERN (mirrors compliance_agent.py / health_agent.py)
---------------------------------------------------------------------------
  1. NEW FILE on a NEW PORT (9109), subclass A2AAgent, _build_card +
     _register_skills (fraud.vet, fraud.explain).
  2. COMPOSE against Phase-0 contracts: make_counterparty / CounterpartyKind (the
     §19.1 counterparty↔catalog identity — counterparty_id == catalog_id) +
     make_provenance / SourceTier on every seeded solvency fact. NO parallel
     shapes; the catalog id IS the join key (no second namespace).
  3. VARIANCE CLAMP: NO-LLM-NUMBERS — every solvency score is a seeded integer;
     the risk_band DECISION is a PURE threshold function over closed sets; UNKNOWN
     → conservative BLOCK (never silent-OK); the LLM is cosmetic only, validated
     by validate_rationale, falling back to deterministic advisory text.
  4. SEEDED DATA, provenance-tagged: a per-counterparty seed table with an honest
     Tier-3 SEEDED_FALLBACK behind fetch_solvency_profile(); a live credit /
     distress feed — a D&B Failure Score, agency credit ratings (Moody's/S&P/Fitch),
     an Altman-Z from public filings, or market-implied distance-to-default (CDS/bond
     yields) — is a Tier-2 swap, NOT a re-architecture. (NOT the IATA BSP feed: BSP is a
     settlement/reconciliation clearinghouse, not a credit-risk predictor.)
  5. WIRING — APPEND-ONLY orchestrator hook (fraud_url/fraud_client +
     _call_fraud(), defaulting to None so every existing call site + test is
     unchanged) + the Critic commit-time re-check. Registration IS the AgentCard +
     the optional hook. S1–S5 stay byte-identical var-0.
  6. VERIFY HARD: test_fraud_agent.py (CI-safe ASGI TestClient) with the CI-
     enforced invariants (deterministic risk_band; numbers from seed not LLM;
     UNKNOWN never silent-OK; blocked/unknown never committable without consent;
     checked at BOTH Fraud and Critic) + the DC-fraud fair baseline
     (baseline_fraud.py + run_dc_fraud_bench.py) measuring the gain.

Runnable service: HOST / PORT env, defaults 0.0.0.0:9109.

Security: no secrets; the only network is the OPTIONAL cosmetic DashScope call
(behind a key check + a validator); all numbers are seeded.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import uvicorn

from agents.a2a_agent import (
    A2AAgent,
    _data_part,
    _new_artifact,
)
from core.contracts import (
    CounterpartyKind,
    SourceTier,
    make_counterparty,
    make_provenance,
)

logger = logging.getLogger("fraud_agent")


# ===========================================================================
# 0. RISK BAND — the closed output vocabulary. The risk_band is a PURE
#    deterministic function of the seeded solvency score (or its ABSENCE).
# ===========================================================================

class RiskBand:
    """The closed set of per-counterparty solvency risk bands."""

    CLEAR = "clear"        # high solvency — normal commit
    ELEVATED = "elevated"  # moderate solvency — advisory flag, committable
    BLOCKED = "blocked"    # low solvency / distress watchlist — gate (consent token required)
    UNKNOWN = "unknown"    # NO seeded profile — CONSERVATIVE BLOCK (never silent-OK)


_RISK_BANDS: frozenset[str] = frozenset({
    RiskBand.CLEAR, RiskBand.ELEVATED, RiskBand.BLOCKED, RiskBand.UNKNOWN,
})

# The bands that are NEVER committable without an explicit consent token (the GATE).
_NON_COMMITTABLE_BANDS: frozenset[str] = frozenset({RiskBand.BLOCKED, RiskBand.UNKNOWN})


def is_committable_band(band: str) -> bool:
    """
    True iff a counterparty in `band` may be committed WITHOUT a consent token.

    PURE. The gate: blocked / unknown → False (consent required). Any band string
    not in the closed set is treated CONSERVATIVELY as non-committable.
    """
    if band not in _RISK_BANDS:
        return False
    return band not in _NON_COMMITTABLE_BANDS


# ===========================================================================
# 1. DETERMINISTIC THRESHOLDS (pure constants; the DECISION is a pure threshold
#    function over these — NEVER the LLM). Solvency score is a seeded integer
#    0–100 (HIGH = more solvent). The band is a step function of the score.
# ===========================================================================

SOLVENCY_CLEAR_MIN = 70    # score >= 70  → clear
SOLVENCY_ELEVATED_MIN = 45  # 45 <= score < 70 → elevated (advisory)
# score < 45 → blocked. No seeded score at all → unknown (conservative block).


def band_for_score(score: int | None) -> str:
    """
    Map a seeded solvency score (0–100) to a risk_band. PURE + DETERMINISTIC.

    None (no seeded profile) → UNKNOWN (conservative block — never silent clear).
    """
    if score is None:
        return RiskBand.UNKNOWN
    if score >= SOLVENCY_CLEAR_MIN:
        return RiskBand.CLEAR
    if score >= SOLVENCY_ELEVATED_MIN:
        return RiskBand.ELEVATED
    return RiskBand.BLOCKED


# ===========================================================================
# 2. SEEDED SOLVENCY TABLE (Tier-1 cache, provenance-tagged). NO-LLM-NUMBERS:
#    every score below is a fixed seeded integer. A live credit / distress feed
#    (D&B Failure Score, agency credit ratings, Altman-Z from filings, OTA financial filings) becomes a Tier-2 adapter
#    swap behind fetch_solvency_profile() — NOT a re-architecture.
#
#    Keyed by counterparty_id, which == catalog_id by the §19.1 identity contract
#    (counterparty_id == catalog_id; the catalog id IS the join key). Lodging rows
#    mirror catalog.go hotel ids; carrier / OTA rows are the supplier counterparties
#    a leg can be committed against.
#
#    D8 — CounterpartyKind.OTA ("ota"): OTA/reseller kind. The contracts.py enum
#    carries CounterpartyKind.OTA = "ota" (added by the D8 contracts patch). Until
#    that patch is live, we hold the string literal here so the seed table is
#    forward-compatible and does not misclassify an OTA as a carrier/transport.
# ===========================================================================

_SEED_FETCHED_AT = "2026-06-10"
_SEED_TTL_DAYS = 30  # solvency moves faster than climatology → shorter TTL
_SEED_TTL_SECONDS = _SEED_TTL_DAYS * 24 * 3600

# D8 — the "ota" string will equal CounterpartyKind.OTA.value once contracts.py is
# patched.  We define it here so this file is self-contained and forward-compatible.
_KIND_OTA: str = "ota"


def _row(score: int, kind: str, source: str, note: str = "") -> dict[str, Any]:
    return {"solvency_score": score, "kind": kind, "source": source, "note": note}


# The seeded solvency profiles. score 0–100 (HIGH = solvent). NOT fabricated at
# call time — these are reproducible seed constants, swappable at the Tier-2 seam.
_SOLVENCY_BY_COUNTERPARTY: dict[str, dict[str, Any]] = {
    # ---- CARRIERS (the N1 supplier counterparties a leg commits against) ---------
    # Flag carrier — strong balance sheet, clear.
    "carrier-garuda-indonesia": _row(82, CounterpartyKind.TRANSPORT.value,
                                      "seed:simulated-solvency-2026"),
    "carrier-singapore-airlines": _row(91, CounterpartyKind.TRANSPORT.value,
                                       "seed:simulated-solvency-2026"),
    "carrier-qantas": _row(85, CounterpartyKind.TRANSPORT.value,
                           "seed:simulated-solvency-2026"),
    "carrier-ethiopian-airlines": _row(74, CounterpartyKind.TRANSPORT.value,
                                       "seed:simulated-solvency-2026"),
    # Moderate — elevated advisory but committable (a discount LCC under pressure).
    "carrier-valuair-lcc": _row(52, CounterpartyKind.TRANSPORT.value,
                                "seed:simulated-solvency-2026",
                                "thin margins; on a soft watchlist"),
    # DISTRESSED — the DC3 cheapest-carrier trap. Low solvency, distress watchlist.
    # If committed and the World-Sim flips it insolvent, the ticket VOIDs with no
    # counterparty to rebook against (fault class N1).
    "carrier-skylark-budget-air": _row(28, CounterpartyKind.TRANSPORT.value,
                                       "seed:simulated-solvency-2026",
                                       "simulated distress watchlist (demo seed); deferred-remittance flag"),

    # ---- OTA RESELLERS (D8 — kind=OTA, not TRANSPORT; holds funds, not a carrier)
    # A failing OTA reseller (sells someone else's inventory but holds the funds).
    # D8: kind must be "ota" (CounterpartyKind.OTA.value) to distinguish a
    # funds-holding reseller from an operating carrier.
    "ota-cheapfly-reseller": _row(31, _KIND_OTA,
                                  "seed:ota-financial-filings-2026",
                                  "negative working capital; chargeback spike"),

    # ---- LODGING counterparties (mirror catalog.go hotel ids; HOTEL kind) -------
    # Bali (the S/demo corpus) — established operators, clear.
    # NOTE (finding #18): The seeded lodging table is intentionally limited to the
    # demo/DC corpus. Lodging solvency for the bulk catalog (~14.8k hotels) is
    # OUT-OF-SCOPE at this phase (see module-level SCOPE comment). Only lodging
    # counterparties explicitly listed here are gated; all others resolve to
    # band=UNKNOWN and are left un-gated by the Critic per the design decision
    # in the SCOPE block above.
    "bali-alila-seminyak": _row(80, CounterpartyKind.HOTEL.value,
                                "seed:lodging-operator-solvency-2026"),
    "bali-como-canggu": _row(83, CounterpartyKind.HOTEL.value,
                             "seed:lodging-operator-solvency-2026"),
    "bali-alaya-ubud": _row(78, CounterpartyKind.HOTEL.value,
                            "seed:lodging-operator-solvency-2026"),
    "bali-kuta-paradiso": _row(72, CounterpartyKind.HOTEL.value,
                               "seed:lodging-operator-solvency-2026"),
    "bali-legian-beach": _row(71, CounterpartyKind.HOTEL.value,
                              "seed:lodging-operator-solvency-2026"),
    "bali-ubud-garden": _row(58, CounterpartyKind.HOTEL.value,
                             "seed:lodging-operator-solvency-2026",
                             "small operator; thin reserves"),
}


# ===========================================================================
# Module-load self-checks on the seed table (fail loud at import).
# ===========================================================================

def _assert_seed_table_valid() -> None:
    # D8: include _KIND_OTA ("ota") in valid kinds. CounterpartyKind.OTA will be
    # added to contracts.py as part of the D8 contracts patch; until then we
    # accept the string directly so the seed table validates correctly.
    _kinds = frozenset(c.value for c in CounterpartyKind) | {_KIND_OTA}
    for cid, row in _SOLVENCY_BY_COUNTERPARTY.items():
        score = row.get("solvency_score")
        if not isinstance(score, int) or not (0 <= score <= 100):
            raise AssertionError(
                f"fraud seed: solvency_score {score!r} for {cid!r} out of 0–100"
            )
        if row.get("kind") not in _kinds:
            raise AssertionError(
                f"fraud seed: kind {row.get('kind')!r} for {cid!r} not in {sorted(_kinds)}"
            )
        if not row.get("source"):
            raise AssertionError(f"fraud seed: counterparty {cid!r} has no provenance source")


_assert_seed_table_valid()


# ===========================================================================
# Resolution + the deterministic decision (PURE functions; NO LLM, NO network).
# ===========================================================================

def canonical_counterparty_id(raw: str | None) -> str | None:
    """
    Normalise a raw counterparty / catalog id to the seeded key. Deterministic,
    lower-cased + stripped. Returns None for an empty input. (The §19.1 contract:
    counterparty_id == catalog_id; the catalog id IS the join key, no second
    namespace — so this is a pure normalisation, not a remap.)
    """
    if not raw:
        return None
    return str(raw).strip().lower()


def fetch_solvency_profile(
    counterparty_id: str | None,
    *,
    simulate_source_unreachable: bool = False,
) -> tuple[dict[str, Any] | None, str]:
    """
    Resolve the seeded solvency profile for a counterparty (Tier-1 cache today).

    Returns (profile_or_None, source_freshness). profile is
    {solvency_score, kind, source, note}. `simulate_source_unreachable=True`
    exercises the Tier-3 exhausted path → the seeded values tagged SEEDED_FALLBACK
    (NOT fabricated), or None if the counterparty is unknown.
    """
    cid = canonical_counterparty_id(counterparty_id)
    if cid is None or cid not in _SOLVENCY_BY_COUNTERPARTY:
        return None, "SEEDED"
    freshness = "SEEDED_FALLBACK" if simulate_source_unreachable else "SEEDED"
    if simulate_source_unreachable:
        logger.warning(
            "fraud: solvency source unreachable (Tier-3 exhausted) → SEEDED_FALLBACK "
            "for counterparty %s (values NOT fabricated)", cid,
        )
    return dict(_SOLVENCY_BY_COUNTERPARTY[cid]), freshness


def _provenance(source: str, tier: str = SourceTier.SEEDED.value) -> dict[str, Any]:
    """Canonical provenance envelope for a seeded solvency fact."""
    return make_provenance(
        source=source,
        fetched_at=_SEED_FETCHED_AT,
        tier=tier,
        source_url=None,
        ttl=_SEED_TTL_SECONDS,
    )


def solvency_profile_public(counterparty_id: str) -> dict[str, Any] | None:
    """
    PUBLIC seeded accessor (the fair-baseline tool uses this — IDENTICAL data).

    Returns the SAME facts the society's gate resolves: {counterparty_id,
    catalog_id, kind, solvency_score, risk_band, committable, source}. Returns
    None for an UNKNOWN counterparty (the baseline sees the same gap the society
    does — no fabrication). Deterministic.
    """
    cid = canonical_counterparty_id(counterparty_id)
    if cid is None:
        return None
    profile, _ = fetch_solvency_profile(cid)
    if profile is None:
        return None
    band = band_for_score(profile["solvency_score"])
    return {
        "counterparty_id": cid,
        "catalog_id": cid,
        "kind": profile["kind"],
        "solvency_score": profile["solvency_score"],
        "risk_band": band,
        "committable_without_consent": is_committable_band(band),
        "note": profile.get("note", ""),
        "source": profile["source"],
    }


def solvency_table_public() -> list[dict[str, Any]]:
    """
    PUBLIC seeded accessor: the FULL seeded solvency table (so the fair baseline
    can see every counterparty exactly as the society's seed). Deterministic,
    stable order (sorted by counterparty_id).
    """
    return [
        solvency_profile_public(cid)
        for cid in sorted(_SOLVENCY_BY_COUNTERPARTY)
    ]


# ===========================================================================
# CONSENT TOKEN VALIDATION (HIGH C1(a)). A consent token overrides a
# BLOCKED/UNKNOWN gate ONLY if it VALIDATES — bound to the counterparty_id AND
# consistent with the observed band. Mere non-empty presence is NOT enough; an
# unrecognised/garbage/unbound/band-mismatched token NEVER overrides
# (fail-conservative). PURE + DETERMINISTIC (no wall-clock / random / network).
# ===========================================================================

# The structured grant form: "consent:{counterparty_id}:{risk_band}:{nonce}".
# The counterparty_id may itself contain ':' (it should not, per §19.1 ids, but we
# do not assume) — so we split into AT MOST 4 fields from the LEFT for scheme and
# from the structure we reconstruct deterministically.
CONSENT_SCHEME = "consent"
_CONSENT_FIELDS = 4


def validate_consent_token(
    consent_token: str | None,
    *,
    counterparty_id: str | None,
    observed_band: str,
) -> bool:
    """
    True iff `consent_token` is a VALID grant that may override the gate for this
    counterparty in its OBSERVED band. PURE + DETERMINISTIC; fail-conservative.

    A valid token is exactly "consent:{counterparty_id}:{risk_band}:{nonce}" where:
      - scheme        == "consent"
      - counterparty  == the canonical counterparty_id being vetted (BINDING)
      - risk_band     == observed_band (CONSISTENCY with the seeded-score verdict)
      - nonce         is non-empty (a caller-issued grant marker)
    Anything else (empty, whitespace, wrong shape, wrong scheme, unbound to this
    counterparty, or band-mismatched) → False. NEVER raises on bad input.
    """
    if not isinstance(consent_token, str):
        return False
    raw = consent_token.strip()
    if not raw:
        return False
    # Only override gated (non-committable) bands; an unknown observed band string
    # is itself conservative (no override path is meaningful).
    if observed_band not in _NON_COMMITTABLE_BANDS:
        return False
    cid = canonical_counterparty_id(counterparty_id)
    if cid is None:
        return False
    parts = raw.split(":")
    if len(parts) != _CONSENT_FIELDS:
        return False
    scheme, tok_cid, tok_band, nonce = parts
    if scheme.strip().lower() != CONSENT_SCHEME:
        return False
    # BINDING: token's counterparty must canonicalise to the one being vetted.
    if canonical_counterparty_id(tok_cid) != cid:
        return False
    # CONSISTENCY: token's claimed band must equal the observed (seed-derived) band.
    if tok_band.strip().lower() != observed_band:
        return False
    # A grant marker must be present (structured, caller-issued).
    if not nonce.strip():
        return False
    return True


def vet_counterparty(
    counterparty_id: str | None,
    *,
    kind: str | None = None,
    consent_token: str | None = None,
    simulate_source_unreachable: bool = False,
) -> dict[str, Any]:
    """
    Vet ONE counterparty → its risk_band + the deterministic commit DECISION.

    PURE + DETERMINISTIC (same inputs → byte-identical output; NO-LLM-NUMBERS).
    The GATE: a blocked / unknown counterparty is NOT committable unless a VALID
    `consent_token` is supplied. UNKNOWN (no seeded profile) → conservative block,
    NEVER a silent clear.

    CONSENT TOKEN (HIGH C1(a)): the token overrides the gate ONLY if it VALIDATES
    via validate_consent_token — bound to THIS counterparty_id AND consistent with
    the OBSERVED risk_band ("consent:{counterparty_id}:{risk_band}:{nonce}"). Mere
    non-empty presence is NOT enough; an unrecognised / garbage / unbound /
    band-mismatched token NEVER overrides (fail-conservative — HONESTY/var-0). See
    the module-level CONSENT TOKEN CONTRACT.
    """
    cid = canonical_counterparty_id(counterparty_id)
    profile, freshness = fetch_solvency_profile(
        cid, simulate_source_unreachable=simulate_source_unreachable
    )

    if profile is None:
        band = RiskBand.UNKNOWN
        score = None
        src = "seed:fraud-no-profile"
        kind_out = kind or "unknown"
        note = "no seeded solvency profile"
    else:
        score = int(profile["solvency_score"])
        band = band_for_score(score)
        src = profile["source"]
        kind_out = profile["kind"]
        note = profile.get("note", "")

    base_committable = is_committable_band(band)
    # HIGH C1(a): a consent token overrides the gate ONLY if it VALIDATES — bound to
    # this counterparty_id AND consistent with the OBSERVED band. Mere non-empty
    # presence is NOT enough; garbage/unbound/band-mismatched → NO override
    # (fail-conservative). Validation is meaningful only for a gated band.
    has_consent = (not base_committable) and validate_consent_token(
        consent_token, counterparty_id=cid, observed_band=band,
    )
    # The gate: blocked/unknown becomes committable ONLY when a VALID consent token
    # is supplied for it.
    committable = base_committable or has_consent
    consent_required = (not base_committable)
    gate_reason = None
    if not base_committable:
        if has_consent:
            gate_reason = (
                f"BLOCK_OVERRIDDEN_BY_CONSENT: counterparty {cid!r} is {band} "
                f"(solvency {'unknown' if score is None else score}); committed only "
                f"because an explicit consent token was supplied."
            )
        else:
            gate_reason = (
                f"BLOCK_INSOLVENCY_RISK: counterparty {cid!r} is {band} "
                f"(solvency {'unknown' if score is None else score}) — NEVER booked "
                f"without an explicit consent token (pre-empts N1 supplier-insolvency VOID)."
            )

    tier = SourceTier.SEEDED.value if freshness != "SEEDED_FALLBACK" else SourceTier.EXHAUSTED.value
    return {
        "counterparty": make_counterparty(cid or "", kind_out)
        if cid else {"counterparty_id": "", "catalog_id": "", "kind": kind_out},
        "counterparty_id": cid,
        "solvency_score": score,
        "risk_band": band,
        "committable": committable,
        "consent_required": consent_required,
        "consent_supplied": has_consent,
        "gate_reason": gate_reason,
        "note": note,
        "source_freshness": freshness,
        "provenance": _provenance(src, tier),
    }


def vet(
    counterparties: list[dict[str, Any]],
    *,
    consent_tokens: dict[str, str] | None = None,
    simulate_source_unreachable: bool = False,
) -> dict[str, Any]:
    """
    Vet a list of counterparties → per-counterparty verdicts + a trip-level
    roll-up Budget consumes to learn which counterparties it may commit.

    Each counterparty: {counterparty_id | catalog_id, kind?}. `consent_tokens`
    maps counterparty_id → a non-empty consent token (overrides the gate for that
    one — see CONSENT TOKEN CONTRACT in the module docstring). PURE + DETERMINISTIC.
    Roll-up: {all_committable, committable_ids[], blocked_ids[],
    requires_consent_ids[], risk_bands{}}.
    """
    consent_tokens = consent_tokens or {}
    per_counterparty: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cp in counterparties or []:
        raw_id = cp.get("counterparty_id") or cp.get("catalog_id") or cp.get("hotel_id")
        cid = canonical_counterparty_id(raw_id)
        # De-dup on the canonical id (a leg may name the same carrier twice).
        if cid is not None and cid in seen:
            continue
        if cid is not None:
            seen.add(cid)
        verdict = vet_counterparty(
            cid,
            kind=cp.get("kind"),
            consent_token=consent_tokens.get(cid or ""),
            simulate_source_unreachable=simulate_source_unreachable,
        )
        verdict["leg_id"] = cp.get("leg_id")
        per_counterparty.append(verdict)

    committable_ids = [v["counterparty_id"] for v in per_counterparty if v["committable"]]
    blocked_ids = [
        v["counterparty_id"] for v in per_counterparty
        if not v["committable"]
    ]
    requires_consent_ids = [
        v["counterparty_id"] for v in per_counterparty if v["consent_required"]
    ]
    risk_bands = {v["counterparty_id"]: v["risk_band"] for v in per_counterparty}

    return {
        "per_counterparty": per_counterparty,
        "rollup": {
            "all_committable": all(v["committable"] for v in per_counterparty),
            "n_counterparties": len(per_counterparty),
            "committable_ids": committable_ids,
            "blocked_ids": blocked_ids,
            "requires_consent_ids": requires_consent_ids,
            "risk_bands": risk_bands,
        },
    }


# ===========================================================================
# Optional cosmetic LLM rationale — COSMETIC ONLY, behind a validator. NEVER
# changes a band or a commit decision (NO-LLM-NUMBERS).
# ===========================================================================

def validate_rationale(text: str, result: dict[str, Any]) -> bool:
    """
    True iff `text` is a faithful restatement of the structured vetting result:
    if ANY counterparty is non-committable (blocked/unknown without consent), the
    prose must NOT call the booking safe / clear / fine. Conservative: any
    "safe to book / all clear / no risk" softening when something is blocked →
    reject (never let prose overturn the gate).
    """
    if not isinstance(text, str) or not text.strip():
        return False
    rollup = result.get("rollup", {})
    blocked = bool(rollup.get("blocked_ids")) or not rollup.get("all_committable", True)
    if blocked:
        low = text.lower()
        soften = [
            "safe to book", "all clear", "no risk", "no concerns", "good to go",
            "perfectly fine", "nothing to worry", "risk-free", "go ahead and book",
            "solvent and safe",
        ]
        if any(s in low for s in soften):
            return False
    return True


def _llm_rationale(result: dict[str, Any]) -> str | None:
    """
    Optional cosmetic one-paragraph restatement via DashScope. Returns None if no
    key, on any error, or if the draft fails validate_rationale (→ deterministic
    fallback). NEVER changes the structured result / any band.
    """
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        return None
    try:
        import httpx

        rollup = result.get("rollup", {})
        facts = json.dumps({
            "all_committable": rollup.get("all_committable"),
            "blocked_ids": rollup.get("blocked_ids"),
            "requires_consent_ids": rollup.get("requires_consent_ids"),
            "risk_bands": rollup.get("risk_bands"),
        }, sort_keys=True)
        prompt = (
            "You are a counterparty-trust briefer. In ONE short paragraph, restate "
            "this deterministic counterparty-solvency vetting for the traveler. Use "
            "ONLY these facts; do NOT add numbers, do NOT call a blocked counterparty "
            "safe to book.\n" + facts
        )
        resp = httpx.post(
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"enable_thinking": False, "model": "qwen3-max", "messages": [{"role": "user", "content": prompt}]},
            timeout=30.0,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        return text if validate_rationale(text, result) else None
    except Exception:
        return None


def explain(result: dict[str, Any]) -> dict[str, Any]:
    """
    Honest human-readable summary built ONLY from the structured vetting result.
    Deterministic (no LLM in this path).
    """
    rollup = result.get("rollup", {})
    blocked = rollup.get("blocked_ids", [])
    if rollup.get("all_committable", False):
        headline = (
            f"All {rollup.get('n_counterparties', 0)} counterparties are committable "
            f"(solvency clear/elevated, or consent supplied)."
        )
    else:
        headline = (
            f"{len(blocked)} counterparty/ies are NOT committable without an explicit "
            f"consent token (blocked/unknown solvency): {blocked}. Pre-empts N1 "
            f"supplier-insolvency VOID."
        )
    return {"headline": headline, "blocked_ids": blocked}


# ===========================================================================
# The A2A agent
# ===========================================================================


class FraudAgent(A2AAgent):
    """
    Fraud / counterparty-trust GATE (Travel Guild, Track-3, L2 / DC-fraud).

    Implements ``fraud.vet`` — per-counterparty solvency risk_band + the
    deterministic commit DECISION (blocked/unknown never committable without
    an explicit consent token), and ``fraud.explain``. A money-path GATE: it
    constrains which counterparties Budget may commit (it emits NO fee).
    Fully deterministic (NO-LLM-NUMBERS); the optional cosmetic rationale is
    clamped behind validate_rationale.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 9109) -> None:
        self._host = host
        self._port = port
        super().__init__()

    def _build_card(self) -> dict:
        url = f"http://{self._host}:{self._port}"
        return {
            "name": "fraud-agent",
            "description": (
                "Fraud / counterparty-trust GATE — a deterministic counterparty-"
                "SOLVENCY gate that pre-empts fault class N1 (supplier insolvency: a "
                "ticket goes VOID with no counterparty to rebook against). Per "
                "counterparty (carrier/OTA/lodging; counterparty_id == catalog_id per "
                "the §19.1 identity contract) it resolves a seeded solvency risk_band "
                "in {clear, elevated, blocked, unknown} and the commit DECISION: a "
                "blocked OR unknown counterparty is NEVER booked without an explicit "
                "consent token (UNKNOWN → conservative block, never silent-OK). It CONSTRAINS "
                "which counterparties Budget may commit, and the Critic re-checks the "
                "same seeded band at commit time so a missed verdict can't slip an "
                "insolvent supplier through. NO-LLM-NUMBERS (every solvency score is a "
                "seeded constant; the band is a pure threshold function over closed "
                "sets; the LLM only drafts validated cosmetic prose). Implements "
                "'fraud.vet' + 'fraud.explain'. Part of the Travel Guild multi-agent "
                "pipeline (Track 3, L2 money-path gate)."
            ),
            "url": url,
            "version": "1.0.0",
            "protocolVersion": "0.3.0",
            "preferredTransport": "JSONRPC",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["data"],
            "defaultOutputModes": ["data"],
            "skills": [
                {
                    "id": "fraud.vet",
                    "name": "Vet counterparty solvency (the N1 insolvency gate)",
                    "description": (
                        "Given a trip's counterparties (carrier/OTA/lodging catalog "
                        "ids), return per-counterparty solvency risk_band "
                        "(clear/elevated/blocked/unknown) + the deterministic commit "
                        "decision (blocked/unknown never committable without an explicit "
                        "consent token) and a trip roll-up Budget consumes. Deterministic; "
                        "seeded numbers; UNKNOWN → conservative block."
                    ),
                    "tags": [
                        "fraud", "counterparty", "solvency", "insolvency", "N1",
                        "gate", "deterministic", "L2", "money-path",
                    ],
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "counterparties": {"type": "array", "items": {"type": "object"}},
                            "consent_tokens": {"type": "object"},
                            "use_llm": {"type": "boolean"},
                            "simulate_source_unreachable": {"type": "boolean"},
                        },
                        "required": ["counterparties"],
                    },
                    "examples": [
                        json.dumps({
                            "counterparties": [
                                {"counterparty_id": "carrier-skylark-budget-air",
                                 "kind": "transport", "leg_id": "leg-0"}
                            ]
                        }),
                    ],
                },
                {
                    "id": "fraud.explain",
                    "name": "Explain the counterparty vetting (honest summary)",
                    "description": (
                        "Return an honest human-readable summary built ONLY from a "
                        "structured fraud.vet result (no LLM in this path)."
                    ),
                    "tags": ["fraud", "explain", "deterministic"],
                    "inputSchema": {
                        "type": "object",
                        "properties": {"result": {"type": "object"}},
                        "required": ["result"],
                    },
                },
            ],
        }

    def _register_skills(self) -> None:
        self.register_skill("fraud.vet", self._vet_handler)
        self.register_skill("fraud.explain", self._explain_handler)

    async def _vet_handler(self, message: dict, task: dict) -> dict:
        payload = self._extract_payload(message)
        if not isinstance(payload, dict):
            raise ValueError(
                "fraud.vet requires a data part with a JSON object "
                "{counterparties[], consent_tokens?, use_llm?, simulate_source_unreachable?}"
            )
        counterparties = payload.get("counterparties")
        if not isinstance(counterparties, list) or not counterparties:
            raise ValueError("fraud.vet: 'counterparties' must be a non-empty list")

        result = vet(
            counterparties,
            consent_tokens=payload.get("consent_tokens") or {},
            simulate_source_unreachable=bool(payload.get("simulate_source_unreachable", False)),
        )

        if payload.get("use_llm"):
            rationale = _llm_rationale(result)
            result["llm_rationale"] = rationale
            result["rationale_source"] = "llm" if rationale else "deterministic"
        else:
            result["rationale_source"] = "deterministic"

        rollup = result["rollup"]
        logger.info(
            "fraud.vet: counterparties=%d all_committable=%s blocked=%s",
            rollup["n_counterparties"], rollup["all_committable"], rollup["blocked_ids"],
        )
        return _new_artifact(
            name="fraud.vet.result",
            parts=[_data_part(result)],
        )

    async def _explain_handler(self, message: dict, task: dict) -> dict:
        payload = self._extract_payload(message)
        if not isinstance(payload, dict) or "result" not in payload:
            raise ValueError("fraud.explain requires a data part {result: <fraud.vet result>}")
        explanation = explain(payload["result"])
        return _new_artifact(
            name="fraud.explain.result",
            parts=[_data_part(explanation)],
        )

    @staticmethod
    def _extract_payload(message: dict) -> Any:
        """Extract the JSON payload from the first data or text part."""
        for part in message.get("parts", []):
            if part.get("kind") == "data":
                data = part.get("data")
                if isinstance(data, (dict, list)):
                    return data
                if isinstance(data, str):
                    try:
                        return json.loads(data)
                    except Exception:
                        pass
            elif part.get("kind") == "text":
                text = part.get("text", "")
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, (dict, list)):
                        return parsed
                except Exception:
                    pass
        return None


# ===========================================================================
# Entry point
# ===========================================================================

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )
    port = int(os.environ.get("PORT", 9109))
    host = os.environ.get("AGENT_BIND_HOST", "127.0.0.1")

    agent = FraudAgent(host=host, port=port)
    app = agent.build_app()

    logger.info("Fraud agent starting on %s:%d", host, port)
    logger.info("Agent Card: http://%s:%d/.well-known/agent-card.json", host, port)
    logger.info("RPC endpoint: http://%s:%d/", host, port)

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()


# ===========================================================================
# §AGENT-EXTENSION NOTE — Fraud is the SIXTH specialist on the pattern, and the
# LAST roster candidate. It is a money-path GATE (like Compliance/Health) that
# constrains the commit set rather than emitting a fee.
# ===========================================================================
# Built per the §AGENT-EXTENSION PATTERN documented at the bottom of
# insurance_agent.py. Recap of what this file did so the pattern stays legible:
#   1. NEW FILE on a NEW PORT (9109), subclass A2AAgent, _build_card +
#      _register_skills (fraud.vet, fraud.explain).
#   2. COMPOSE against Phase-0 contracts: make_counterparty / CounterpartyKind
#      (the §19.1 counterparty↔catalog identity — counterparty_id == catalog_id) +
#      make_provenance / SourceTier on every seeded solvency fact. NO parallel
#      shapes; the catalog id IS the join key (no second namespace).
#   3. VARIANCE CLAMP: NO-LLM-NUMBERS (every solvency score is a seeded integer);
#      the risk_band DECISION is a PURE threshold function over closed sets;
#      UNKNOWN → conservative BLOCK (never silent-OK); the LLM is cosmetic only,
#      validated by validate_rationale.
#   4. SEEDED DATA, provenance-tagged: a per-counterparty solvency table with an
#      honest Tier-3 SEEDED_FALLBACK behind fetch_solvency_profile(); a live
#      credit/distress feed (D&B Failure Score / credit ratings / Altman-Z) is a Tier-2 swap.
#   5. WIRING — APPEND-ONLY orchestrator hook (fraud_url/fraud_client +
#      _call_fraud(), defaulting to None) + the Critic commit-time re-check that
#      INDEPENDENTLY re-derives each leg's band from this SAME seeded table so a
#      missed Fraud verdict can't slip an insolvent supplier through. Registration
#      IS the AgentCard + the hook. S1–S5 stay byte-identical var-0.
#   6. VERIFY HARD: test_fraud_agent.py (CI-safe ASGI TestClient) with the CI-
#      enforced invariants (deterministic band; numbers from seed not LLM; UNKNOWN
#      never silent-OK; blocked/unknown never committable without consent; checked
#      at BOTH Fraud and Critic) + the DC-fraud fair baseline (baseline_fraud.py +
#      run_dc_fraud_bench.py) measuring the gain.
