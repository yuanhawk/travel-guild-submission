"""
insurance_agent.py — Insurance / residual-risk coverage specialist (Travel Guild, L2).

Design contract: the internal design spec §17.1 (Insurance specialist),
§17.3 (DC0 demo-contrast), §16.6.1 (insurance EXCLUSIONS sharpen L2), §19 (build
plan: "Insurance (DC0, L2) — money-path but NO-LLM-NUMBERS clamped"), and the
"Insurance" entry of agent-blueprints-raw.json.

WHAT THIS AGENT OWNS (one decision, EXCLUSION-FIRST)
---------------------------------------------------
Given a trip's perils (from Risk, via the Phase-0 peril_crosswalk) and a candidate
policy's seeded terms: does coverage ACTUALLY pay out for the peril most likely to
strand this traveler? It computes a per-peril coverage status
(COVERED / EXCLUDED / SUB_LIMITED / CONDITIONAL / UNDETERMINED) and surfaces
exclusions as FIRST-CLASS output. It NEVER asserts "covered" without enumerating
the matching exclusion clauses. It is a THIN reasoner: it consumes seeded/sourced
policy terms + a peril signal; it does not price policies itself nor scrape insurers.

It PROPOSES a coverage line item (the premium); it does NOT enforce budget (Budget
vetoes the premium like any other line item) and does NOT verify itself (Critic
checks source-authority/freshness + the never-cover-excluded invariant).

THE VARIANCE CLAMP (non-negotiable — money-path agent)
------------------------------------------------------
  NO-LLM-NUMBERS:     premium_cents + every numeric field are integer cents straight
                      from the seed (policy_catalog / policy_clause). The LLM never
                      mints a number. Identical inputs → byte-identical numbers.
  DETERMINISTIC-STATUS: per-peril status is a PURE function of (peril, clause set);
                      see assess_coverage() / _status_for_peril(). Zero variance.
  CLOSED-SET TAXONOMY: the peril universe is contracts.Peril (the §19.1 canonical
                      enum). An unmatched peril degrades to UNDETERMINED — NEVER a
                      silent COVERED. This is the single most important guard.
  LLM IS COSMETIC ONLY: the LLM only drafts the human-readable rationale, and it is
                      validated to be a derivation of the matched clause text. With
                      the LLM disabled the agent still produces the full, correct
                      CoverageAssessment (the closed-set fallback) — proving the LLM
                      edge is purely cosmetic and clamped.

A2A skills
----------
  ``insurance.assess_coverage``  — per-peril coverage status + matched clauses +
                                   premium line item + residual-EV note.
  ``insurance.explain_exclusion`` — DC0 headline built ONLY from matched clause text.

Runnable service: HOST / PORT env, defaults 0.0.0.0:9105 (next free port after
Transport's 9104 / Critic's 9103 / Destination's 9104-demo — see §AGENT-EXTENSION
PATTERN at the bottom of this file).

Determinism: the clause-matcher + line-item emission are pure Python over closed
sets; no LLM, no network in the assessment path. The only optional network call is
the cosmetic rationale-writer (DashScope), fully behind the clamp.
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
    Peril,
    SourceTier,
    all_perils,
    is_valid_peril,
    make_money,
    make_provenance,
)
from utils.line_item_assembler import LineItemAssembler, LineItemKind, PACKAGE_LEG

logger = logging.getLogger(__name__)


# ===========================================================================
# 0. Per-peril coverage STATUS — the closed output vocabulary.
# ===========================================================================
# Status is a PURE deterministic function of (peril, clause set). UNDETERMINED is
# the safe default for any peril with no matching clause (NEVER silent COVERED).

class CoverageStatus:
    """The closed set of per-peril coverage statuses (string constants)."""

    COVERED = "COVERED"          # a COVER clause matches, no EXCLUDE overrides it
    EXCLUDED = "EXCLUDED"        # an EXCLUDE clause matches — pays nothing (DC0 core)
    SUB_LIMITED = "SUB_LIMITED"  # covered subject to a sub-limit (a SUBLIMIT clause applies)
    CONDITIONAL = "CONDITIONAL"  # covered only if a CONDITION is met
    UNDETERMINED = "UNDETERMINED"  # no matching clause — the closed-set default


_STATUS_VALUES = frozenset({
    CoverageStatus.COVERED,
    CoverageStatus.EXCLUDED,
    CoverageStatus.SUB_LIMITED,
    CoverageStatus.CONDITIONAL,
    CoverageStatus.UNDETERMINED,
})


# Clause TYPEs (closed set; mirrors the blueprint policy_clause.clause_type).
class ClauseType:
    COVER = "COVER"
    EXCLUDE = "EXCLUDE"
    SUBLIMIT = "SUBLIMIT"
    CONDITION = "CONDITION"


# ===========================================================================
# 1. SEEDED DATA — policy_catalog, policy_clause, peril taxonomy.
# ===========================================================================
# Provenance-tagged per contracts.py (§13 reproducible). Tier-1 SEEDED today; a
# live insurer/PDS feed is a Tier-2 adapter swap (deferred — see §18 note below),
# NOT a re-architecture. peril_class on every clause is constrained to the canonical
# contracts.Peril closed set so the matcher can never drift.
#
# Premium MODEL (grounded, §12.3 "Premium ≈ 6-9% of insured trip cost"): a policy
# carries a base premium + a trip-cost percentage; the seeded premium = base +
# round(trip_cost_cents * pct). ALL integer cents, NO LLM (NO-LLM-NUMBERS).

# The fixed capture date of the seeded snapshot (deterministic → variance-0, §13).
_SEED_FETCHED_AT = "2026-06-10"
_SEED_TTL_DAYS = 90
_SEED_TTL_SECONDS = _SEED_TTL_DAYS * 24 * 3600


# --- policy_catalog --------------------------------------------------------
# (policy_id, name, tier, base_premium_cents, currency, trip_cost_bps, provenance, source_url)
# trip_cost_bps: integer basis points (100 bps = 1%). Pure integer math for NO-LLM-NUMBERS.
# 6.0% = 600 bps; 9.5% = 950 bps.
_POLICY_CATALOG: dict[str, dict[str, Any]] = {
    "P-STD-01": {
        "policy_id": "P-STD-01",
        "name": "Standard Traveler",
        "tier": "basic",
        "base_premium_cents": 4500,        # $45 base
        "currency": "USD",
        "trip_cost_bps": 600,              # 600 bps = 6% of insured trip cost (§12.3 basic ~6%)
        "provenance": "seed:AFCA-determination-corpus",
        "source_url": "https://www.afca.org.au/",
    },
    "P-PREM-02": {
        "policy_id": "P-PREM-02",
        "name": "Premier Plus",
        "tier": "premium",
        "base_premium_cents": 12000,       # $120 base
        "currency": "USD",
        "trip_cost_bps": 950,              # 950 bps = 9.5% (§12.3 premium ~9.5%)
        "provenance": "seed:insurer-PDS-sample",
        "source_url": "https://example-insurer.test/pds",
    },
}

# The default policy the agent assesses when none is named (the DC0 anchor: a
# traveler "just grabs" a standard policy).
DEFAULT_POLICY_ID = "P-STD-01"


# --- policy_clause ---------------------------------------------------------
# Each clause's peril_class is a canonical contracts.Peril value (closed set).
# clause_type ∈ {COVER, EXCLUDE, SUBLIMIT, CONDITION}. EXCLUDE always wins over
# COVER for the same peril (exclusion-first) — see _status_for_peril().
#
# P-STD-01 is engineered to BE the DC0 contrast: it COVERS trip_delay but EXCLUDES
# war / terrorism / civil_unrest / pandemic (the AFCA war-exclusion lesson), and
# SUB-LIMITS medical. That is precisely "covered for delay, NOT for the conflict
# most likely to strand you."
#
# D5 clause structure: EXC-WAR-2 keys ONLY to Peril.WAR (+Peril.TERRORISM as
# EXC-WAR-2C). A separate EXC-UNREST-1 clause covers Peril.CIVIL_UNREST.
# This ensures only genuine armed-conflict/terrorism signals trigger EXC-WAR-2;
# civil-unrest perils are excluded by their own clause.
_POLICY_CLAUSES: list[dict[str, Any]] = [
    # ---- P-STD-01 (Standard Traveler) — the DC0 anchor policy ----
    # EXC-WAR-2: keyed to Peril.WAR — armed conflict / invasion exclusion (D5).
    {
        "clause_id": "EXC-WAR-2", "policy_id": "P-STD-01", "clause_type": ClauseType.EXCLUDE,
        "peril_class": Peril.WAR.value, "sub_limit_cents": None, "condition_text": None,
        "clause_text": ("No benefit is payable for any claim arising from war, invasion, "
                        "civil unrest, riot or terrorism."),
        "provenance": "seed:AFCA-war-exclusion-upheld",
        "source_url": "https://www.afca.org.au/what-to-expect/determinations",
    },
    # EXC-WAR-2C: same AFCA clause text, keyed to Peril.TERRORISM.
    {
        "clause_id": "EXC-WAR-2C", "policy_id": "P-STD-01", "clause_type": ClauseType.EXCLUDE,
        "peril_class": Peril.TERRORISM.value, "sub_limit_cents": None, "condition_text": None,
        "clause_text": ("No benefit is payable for any claim arising from war, invasion, "
                        "civil unrest, riot or terrorism."),
        "provenance": "seed:AFCA-war-exclusion-upheld",
        "source_url": "https://www.afca.org.au/what-to-expect/determinations",
    },
    # EXC-UNREST-1: civil unrest exclusion — separate clause so that generic elevated
    # advisories (D5: ADVISORY_ELEVATED → personal_liability, not civil_unrest) do NOT
    # inadvertently reach the war-exclusion group.
    {
        "clause_id": "EXC-UNREST-1", "policy_id": "P-STD-01", "clause_type": ClauseType.EXCLUDE,
        "peril_class": Peril.CIVIL_UNREST.value, "sub_limit_cents": None, "condition_text": None,
        "clause_text": ("No benefit is payable for any claim arising from war, invasion, "
                        "civil unrest, riot or terrorism."),
        "provenance": "seed:AFCA-war-exclusion-upheld",
        "source_url": "https://www.afca.org.au/what-to-expect/determinations",
    },
    {
        "clause_id": "EXC-PANDEMIC-1", "policy_id": "P-STD-01", "clause_type": ClauseType.EXCLUDE,
        "peril_class": Peril.PANDEMIC.value, "sub_limit_cents": None, "condition_text": None,
        "clause_text": ("Claims arising from an epidemic or pandemic declared by the WHO "
                        "are excluded."),
        "provenance": "seed:PDS-sample", "source_url": "https://example-insurer.test/pds",
    },
    {
        "clause_id": "COV-DELAY-1", "policy_id": "P-STD-01", "clause_type": ClauseType.COVER,
        "peril_class": Peril.TRAVEL_DELAY.value, "sub_limit_cents": None, "condition_text": None,
        "clause_text": "Trip delay benefit up to the policy limit for delays over 6 hours.",
        "provenance": "seed:PDS-sample", "source_url": "https://example-insurer.test/pds",
    },
    {
        "clause_id": "COV-CANCEL-1", "policy_id": "P-STD-01", "clause_type": ClauseType.COVER,
        "peril_class": Peril.TRIP_CANCELLATION.value, "sub_limit_cents": None, "condition_text": None,
        "clause_text": "Cancellation of non-refundable prepaid travel costs is covered for listed reasons.",
        "provenance": "seed:PDS-sample", "source_url": "https://example-insurer.test/pds",
    },
    {
        "clause_id": "SUB-MED-1", "policy_id": "P-STD-01", "clause_type": ClauseType.SUBLIMIT,
        "peril_class": Peril.MEDICAL.value, "sub_limit_cents": 5_000_000, "condition_text": None,
        "clause_text": "Overseas emergency medical expenses are limited to USD 50,000.",
        "provenance": "seed:PDS-sample", "source_url": "https://example-insurer.test/pds",
    },
    {
        "clause_id": "COV-BAG-1", "policy_id": "P-STD-01", "clause_type": ClauseType.COVER,
        "peril_class": Peril.BAGGAGE_LOSS.value, "sub_limit_cents": None, "condition_text": None,
        "clause_text": "Lost or delayed baggage is covered up to the policy limit.",
        "provenance": "seed:PDS-sample", "source_url": "https://example-insurer.test/pds",
    },
    {
        "clause_id": "CON-ADV-1", "policy_id": "P-STD-01", "clause_type": ClauseType.CONDITION,
        "peril_class": Peril.ADVENTURE_ACTIVITY.value, "sub_limit_cents": None,
        "condition_text": "Requires the optional adventure-activity add-on to be purchased.",
        "clause_text": ("Adventure and high-risk activities are covered only if the "
                        "adventure-activity add-on has been purchased."),
        "provenance": "seed:PDS-sample", "source_url": "https://example-insurer.test/pds",
    },

    # ---- P-PREM-02 (Premier Plus) — broader cover, fewer exclusions ----
    {
        "clause_id": "PCOV-DELAY-1", "policy_id": "P-PREM-02", "clause_type": ClauseType.COVER,
        "peril_class": Peril.TRAVEL_DELAY.value, "sub_limit_cents": None, "condition_text": None,
        "clause_text": "Trip delay benefit up to the policy limit for delays over 3 hours.",
        "provenance": "seed:insurer-PDS-sample", "source_url": "https://example-insurer.test/pds",
    },
    {
        "clause_id": "PCOV-CANCEL-1", "policy_id": "P-PREM-02", "clause_type": ClauseType.COVER,
        "peril_class": Peril.TRIP_CANCELLATION.value, "sub_limit_cents": None, "condition_text": None,
        "clause_text": "Cancellation of non-refundable prepaid travel costs is covered for listed reasons.",
        "provenance": "seed:insurer-PDS-sample", "source_url": "https://example-insurer.test/pds",
    },
    {
        "clause_id": "PSUB-MED-1", "policy_id": "P-PREM-02", "clause_type": ClauseType.SUBLIMIT,
        "peril_class": Peril.MEDICAL.value, "sub_limit_cents": 25_000_000, "condition_text": None,
        "clause_text": "Overseas emergency medical expenses are limited to USD 250,000.",
        "provenance": "seed:insurer-PDS-sample", "source_url": "https://example-insurer.test/pds",
    },
    {
        "clause_id": "PEXC-WAR-1", "policy_id": "P-PREM-02", "clause_type": ClauseType.EXCLUDE,
        "peril_class": Peril.WAR.value, "sub_limit_cents": None, "condition_text": None,
        "clause_text": ("No benefit is payable for any claim arising directly from full-scale "
                        "war or invasion between recognised states."),
        "provenance": "seed:insurer-PDS-sample", "source_url": "https://example-insurer.test/pds",
    },
]


def _clauses_for_policy(policy_id: str) -> list[dict[str, Any]]:
    """All seeded clauses for a policy, in stable seed order (deterministic)."""
    return [c for c in _POLICY_CLAUSES if c["policy_id"] == policy_id]


def policy_catalog_public() -> list[dict[str, Any]]:
    """
    The seeded policy catalog as plain rows (id/name/tier/base premium/currency).

    Exposed so the FAIR DC0 baseline (benchmark/baseline_insurance.py) can offer
    the SAME policies the society's Insurance specialist works with — the §10.10
    fairness contract (identical data). Read-only copy; no LLM, no network.
    """
    return [
        {
            "policy_id": p["policy_id"],
            "name": p["name"],
            "tier": p["tier"],
            "base_premium_cents": p["base_premium_cents"],
            "currency": p["currency"],
        }
        for p in _POLICY_CATALOG.values()
    ]


def policy_clauses_public(policy_id: str) -> list[dict[str, Any]]:
    """
    The FULL seeded clause corpus for a policy (cover/exclude/sublimit/condition),
    each with clause text + provenance — the IDENTICAL data the society's
    deterministic clause-matcher matches against.

    Exposed as the FAIR DC0 baseline's tool payload so the single agent gets the
    same terms (including EXC-WAR-2 for war, EXC-WAR-2C for terrorism, and
    EXC-UNREST-1 for civil unrest). Whether it then PROACTIVELY surfaces the
    exclusions is exactly what DC0 measures — the data is not withheld; only the
    tip-off is. Read-only copies; deterministic order.
    """
    return [
        {
            "clause_id": c["clause_id"],
            "clause_type": c["clause_type"],
            "peril_class": c["peril_class"],
            "clause_text": c["clause_text"],
            "sub_limit_cents": c.get("sub_limit_cents"),
            "condition_text": c.get("condition_text"),
            "provenance": c["provenance"],
            "source_url": c["source_url"],
        }
        for c in _clauses_for_policy(policy_id)
    ]


# --- peril taxonomy --------------------------------------------------------
# The CLOSED SET bounding the matcher IS contracts.Peril (the §19.1 canonical
# enum — Risk owns the signal, Insurance keys coverage to it via peril_crosswalk).
# default_status_when_unmatched is ALWAYS UNDETERMINED (never silent COVERED).
PERIL_TAXONOMY: dict[str, dict[str, str]] = {
    p: {
        "peril_class": p,
        "canonical_label": p.replace("_", " ").title(),
        "default_status_when_unmatched": CoverageStatus.UNDETERMINED,
    }
    for p in all_perils()
}


def _policy_provenance(policy_id: str, tier: str = SourceTier.SEEDED.value) -> dict[str, Any]:
    """The canonical provenance envelope for a seeded policy's terms."""
    pol = _POLICY_CATALOG[policy_id]
    return make_provenance(
        source=pol["provenance"],
        fetched_at=_SEED_FETCHED_AT,
        tier=tier,
        source_url=pol["source_url"],
        ttl=_SEED_TTL_SECONDS,
    )


# ===========================================================================
# 2. PREMIUM — NO-LLM-NUMBERS. Pure integer-cents from seed.
# ===========================================================================
#
# #52 item 2 — KNOWN / ACCEPTED LIMITATION (documented, not silently swept under
# the rug): the premium below is a pure function of (policy_id, insured_trip_
# cost_cents) — it is flat regardless of HAZARD SEVERITY. A confirmed real
# example: a cyclone-season trip and an off-season trip to the SAME destination
# price identically, even though the traveler's actual loss exposure clearly
# differs.
#
# A genuine SEVERITY *signal* already exists elsewhere in this codebase —
# risk_agent.interruption_outlook() derives a closed-set likelihood_tier (low/
# moderate/elevated/high) from the SAME Risk roll-up Insurance already consumes
# via risk_reason_codes. But a signal is not a PRICING MODEL: nowhere in this
# codebase (or in the §12.3 AFCA/PDS sourcing this premium formula is grounded
# in) does a real, source-backed severity-to-premium MULTIPLIER exist. The
# base_premium_cents/trip_cost_bps figures above are grounded in the cited
# insurer tiers (§12.3 "Premium ≈ 6-9% of insured trip cost"); inventing a
# "moderate = +10%, high = +30%"-style surcharge table would be exactly the
# kind of ungrounded number NO-LLM-NUMBERS/DETERMINISTIC-STATUS exists to
# prevent an LLM from minting — doing it by hand in Python is no more honest.
#
# Building a REAL severity-rated premium model (actuarial loss tables per
# peril/tier, or a licensed rate-card feed) is a genuinely larger, separately-
# scoped effort — out of scope here. Flagging it explicitly (this comment +
# the module docstring's PREMIUM MODEL section) rather than shipping an
# invented multiplier is the deliberate call for this fix pass.
# ===========================================================================

def compute_premium_cents(policy_id: str, insured_trip_cost_cents: int) -> int:
    """
    Deterministic premium = base_premium_cents + (trip_cost * trip_cost_bps + 5000) // 10000.

    ALL integer cents straight from the seeded policy_catalog (NO-LLM-NUMBERS).
    Uses integer basis-point arithmetic (100 bps = 1%) to eliminate float and
    banker's-rounding from the money path.  Identical inputs → byte-identical output.
    Negative trip cost clamps to 0.

    Formula: bps_component = (trip * bps + 5000) // 10000
      e.g. trip=120000, bps=600: (120000*600 + 5000) // 10000 = 72005000//10000 = 7200
      e.g. trip=120000, bps=950: (120000*950 + 5000) // 10000 = 114005000//10000 = 11400

    KNOWN LIMITATION (#52 item 2, see the section banner above): this premium
    does NOT vary with hazard severity — a real signal exists (risk_agent.
    interruption_outlook's likelihood_tier) but no source-grounded severity-to-
    premium mapping exists to wire it to; documented rather than invented.
    """
    pol = _POLICY_CATALOG.get(policy_id)
    if pol is None:
        # policy_id is an INTERNAL contract (agent-selected from the catalog); an
        # unknown one is a programming error → fail loud with a TYPED error (not KeyError).
        raise ValueError(f"insurance: unknown policy_id {policy_id!r}")
    trip = max(0, int(insured_trip_cost_cents))
    bps = int(pol["trip_cost_bps"])
    bps_component = (trip * bps + 5000) // 10000
    return int(pol["base_premium_cents"]) + bps_component


# ===========================================================================
# 3. DETERMINISTIC CLAUSE-MATCHER — the heart of the agent (variance-0).
# ===========================================================================
# Per-peril status is a PURE function of (peril_class, clause set). The ONLY
# precedence rule (exclusion-first): an EXCLUDE clause for a peril ALWAYS wins.
# Then SUBLIMIT, then CONDITION, then COVER. No matching clause → UNDETERMINED.

# Precedence order for selecting the governing clause among matches.
_CLAUSE_PRECEDENCE = {
    ClauseType.EXCLUDE: 0,    # exclusion-first — always wins
    ClauseType.SUBLIMIT: 1,
    ClauseType.CONDITION: 2,
    ClauseType.COVER: 3,
}

_CLAUSE_TYPE_TO_STATUS = {
    ClauseType.EXCLUDE: CoverageStatus.EXCLUDED,
    ClauseType.SUBLIMIT: CoverageStatus.SUB_LIMITED,
    ClauseType.CONDITION: CoverageStatus.CONDITIONAL,
    ClauseType.COVER: CoverageStatus.COVERED,
}


def _status_for_peril(peril_class: str, clauses: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Resolve ONE peril's coverage status against a policy's clause set.

    Pure + deterministic. Returns a per-peril record:
        {peril_class, status, matched_clause_ids[], sub_limit_cents?, condition?}

    Precedence: EXCLUDE > SUBLIMIT > CONDITION > COVER. matched_clause_ids lists
    EVERY clause that touches the peril (so the EXCLUDE is always enumerated, even
    when a COVER also exists — the never-mask-exclusion guarantee). No match →
    UNDETERMINED with an empty clause list.
    """
    matches = [c for c in clauses if c["peril_class"] == peril_class]
    # Stable, deterministic ordering of ALL matched clause ids (by precedence then id).
    matched_sorted = sorted(
        matches, key=lambda c: (_CLAUSE_PRECEDENCE[c["clause_type"]], c["clause_id"])
    )
    matched_clause_ids = [c["clause_id"] for c in matched_sorted]

    rec: dict[str, Any] = {
        "peril_class": peril_class,
        "status": PERIL_TAXONOMY[peril_class]["default_status_when_unmatched"]
        if peril_class in PERIL_TAXONOMY else CoverageStatus.UNDETERMINED,
        "matched_clause_ids": matched_clause_ids,
        "sub_limit_cents": None,
        "condition": None,
    }
    if not matched_sorted:
        return rec  # UNDETERMINED — closed-set default, NEVER silent COVERED.

    # The governing clause is the highest-precedence match (exclusion-first).
    governing = matched_sorted[0]
    rec["status"] = _CLAUSE_TYPE_TO_STATUS[governing["clause_type"]]
    if governing["clause_type"] == ClauseType.SUBLIMIT:
        rec["sub_limit_cents"] = governing["sub_limit_cents"]
    elif governing["clause_type"] == ClauseType.CONDITION:
        rec["condition"] = governing["condition_text"]
    return rec


def matched_clause_text(clause_ids: list[str]) -> list[dict[str, str]]:
    """
    Return [{clause_id, clause_text, provenance, source_url}] for the given ids.

    Used by explain_exclusion + the LLM-rationale validator: the LLM may ONLY
    reference facts that appear in THIS text (no free generation of facts).
    """
    by_id = {c["clause_id"]: c for c in _POLICY_CLAUSES}
    out: list[dict[str, str]] = []
    for cid in clause_ids:
        c = by_id.get(cid)
        if c is not None:
            out.append({
                "clause_id": c["clause_id"],
                "clause_text": c["clause_text"],
                "provenance": c["provenance"],
                "source_url": c["source_url"],
            })
    return out


# ===========================================================================
# 4. ASSESS COVERAGE — assemble the full CoverageAssessment (pure / deterministic).
# ===========================================================================

def assess_coverage(
    *,
    peril_set: list[str],
    policy_id: str = DEFAULT_POLICY_ID,
    insured_trip_cost_cents: int = 0,
    leg: str = PACKAGE_LEG,
    source_freshness: str = "SEEDED",
    health_assessment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    The core deterministic assessment — NO LLM, NO network.

    Args:
        peril_set:   list of canonical contracts.Peril values (from Risk via the
                     peril_crosswalk). Invalid/unknown perils degrade to
                     UNDETERMINED (closed-set guard), never silent COVERED.
        policy_id:   the candidate policy (default P-STD-01, the DC0 anchor).
        insured_trip_cost_cents: integer cents the premium % is computed against.
        leg:         line-item leg key (default PACKAGE_LEG for a single trip premium).
        source_freshness: "SEEDED" (Tier-1) or "SEEDED_FALLBACK" (Tier-3 exhausted
                     → fell back to the seeded closed-set template, never fabricated).

    Returns the CoverageAssessment dict (see module docstring / blueprint interface).
    Every numeric field is integer cents from the seed (NO-LLM-NUMBERS). status is a
    pure function of (peril, clause set) (DETERMINISTIC-STATUS). Provenance present
    on the premium + each matched clause (PROVENANCE-PRESENT).
    """
    if policy_id not in _POLICY_CATALOG:
        raise KeyError(f"insurance: unknown policy_id {policy_id!r}")
    clauses = _clauses_for_policy(policy_id)

    # Additive: if the health_assessment signals evacuation_recommended, include
    # MEDICAL_EVACUATION in the peril set so it is surfaced in the assessment.
    # This is purely additive — existing callers that omit health_assessment are
    # unaffected (the default is None, so the block is skipped).
    peril_set = list(peril_set)  # copy — do not mutate caller's list
    if isinstance(health_assessment, dict):
        # Check at the whole-trip level (any per_destination with evacuation_recommended)
        # or a single-destination health result with evacuation_recommended at the top.
        needs_evac = bool(health_assessment.get("evacuation_recommended"))
        if not needs_evac:
            for dest in health_assessment.get("per_destination", []):
                if isinstance(dest, dict) and dest.get("evacuation_recommended"):
                    needs_evac = True
                    break
        if needs_evac:
            evac_peril = Peril.MEDICAL_EVACUATION.value
            if evac_peril not in peril_set:
                peril_set.append(evac_peril)

    # De-dup the peril set preserving first-seen order (deterministic).
    seen: set[str] = set()
    ordered_perils: list[str] = []
    for p in peril_set:
        if p not in seen:
            ordered_perils.append(p)
            seen.add(p)

    per_peril: list[dict[str, Any]] = []
    for p in ordered_perils:
        if not is_valid_peril(p):
            # Out-of-closed-set peril → UNDETERMINED, NEVER silent COVERED.
            logger.warning(
                "insurance.assess_coverage: peril %r not in canonical closed set "
                "→ UNDETERMINED (closed-set guard)", p,
            )
            per_peril.append({
                "peril_class": str(p),
                "status": CoverageStatus.UNDETERMINED,
                "matched_clause_ids": [],
                "sub_limit_cents": None,
                "condition": None,
            })
            continue
        per_peril.append(_status_for_peril(p, clauses))

    # Exclusions are FIRST-CLASS output (the never-mask-exclusion guarantee).
    excluded = [
        {"peril_class": r["peril_class"], "matched_clause_ids": r["matched_clause_ids"]}
        for r in per_peril if r["status"] == CoverageStatus.EXCLUDED
    ]
    sub_limited = [r["peril_class"] for r in per_peril if r["status"] == CoverageStatus.SUB_LIMITED]
    undetermined = [r["peril_class"] for r in per_peril if r["status"] == CoverageStatus.UNDETERMINED]
    covered = [r["peril_class"] for r in per_peril if r["status"] == CoverageStatus.COVERED]
    # CONDITIONAL perils are covered ONLY if a condition is met — they are NOT
    # unconditionally covered. Surface them explicitly so no CONDITIONAL peril
    # silently vanishes from both covered_perils and the residual set.
    conditional = [
        {"peril_class": r["peril_class"], "condition": r.get("condition"),
         "matched_clause_ids": r["matched_clause_ids"]}
        for r in per_peril if r["status"] == CoverageStatus.CONDITIONAL
    ]

    # Residual (uncovered) risk = EXCLUDED ∪ UNDETERMINED ∪ SUB_LIMITED ∪ CONDITIONAL
    # — the perils the policy will NOT unconditionally/fully pay out for.
    # CONDITIONAL is included because if the required condition is not met, the
    # peril is effectively uncovered (fail-conservative / HONESTY). NO numbers minted here.
    uncovered = [
        r["peril_class"] for r in per_peril
        if r["status"] in (
            CoverageStatus.EXCLUDED,
            CoverageStatus.UNDETERMINED,
            CoverageStatus.SUB_LIMITED,
            CoverageStatus.CONDITIONAL,
        )
    ]

    premium_cents = compute_premium_cents(policy_id, insured_trip_cost_cents)
    pol = _POLICY_CATALOG[policy_id]

    # The premium as the canonical money tag (USD seeded → fx_rate 1.0). Budget
    # reads usd_cents from this; the agent NEVER decides an FX rate.
    premium_money = make_money(
        native_cents=premium_cents,
        currency=pol["currency"],
        usd_cents=premium_cents,  # seeded USD policy → identity
        fx_rate=1.0,
        fx_source=pol["provenance"],
        fetched_at=_SEED_FETCHED_AT,
    )

    return {
        "policy_id": policy_id,
        "policy_name": pol["name"],
        "policy_tier": pol["tier"],
        "per_peril": per_peril,
        "excluded_perils_summary": excluded,
        "sub_limited_perils": sub_limited,
        "undetermined_perils": undetermined,
        "covered_perils": covered,
        # CONDITIONAL perils: covered only if a stated condition is met. Surfaced
        # explicitly so callers can present the condition to the traveler honestly.
        # Also included in residual_ev.uncovered_perils (fail-conservative: if the
        # condition is not met the peril is effectively uncovered).
        "conditional_perils": conditional,
        "residual_ev": {
            "uncovered_perils": uncovered,
            "note": (
                "Residual (uncovered) risk: the policy will not unconditionally/fully "
                "pay out for these perils (EXCLUDED ∪ UNDETERMINED ∪ SUB_LIMITED ∪ "
                "CONDITIONAL). EV figures require a seeded loss table (deferred); "
                "uncovered_perils is the honest residual signal today."
            ),
        },
        "premium_cents": premium_cents,
        "premium_money": premium_money,
        "premium_provenance": _policy_provenance(policy_id),
        "insured_trip_cost_cents": max(0, int(insured_trip_cost_cents)),
        "leg": leg,
        "confidence": "high" if source_freshness == "SEEDED" else "seeded_fallback",
        "source_freshness": source_freshness,
    }


def premium_line_item(assessment: dict[str, Any], source: str = "insurance") -> dict[str, Any]:
    """
    Emit the premium as a Budget line item via the Phase-0 LineItemAssembler.

    Keyed/idempotent by (source, "premium", leg) — re-emission REPLACES, never
    double-charges (the §16.3 invariant at fee level). Budget then applies its
    veto/ceiling to it like ANY other line item (Insurance proposes, Budget
    enforces). Returns the single assembled line-item dict.
    """
    a = LineItemAssembler()
    a.add_fee(
        source=source,
        kind=LineItemKind.PREMIUM,
        leg=assessment.get("leg", PACKAGE_LEG),
        money=assessment["premium_money"],
        description=f"Travel insurance premium — {assessment['policy_name']} ({assessment['policy_id']})",
    )
    return a.assemble()[0]


# ===========================================================================
# 5. EXPLAIN EXCLUSION — DC0 headline built ONLY from matched clause text.
# ===========================================================================

def explain_exclusion(
    assessment: dict[str, Any],
    peril_class: str,
    region: str | None = None,
) -> dict[str, Any]:
    """
    Build the DC0 headline for a peril — a PURE FORMATTER over validated structured
    data. NO free generation of facts: every factual claim is a derivation of the
    matched clause text (the never-cover-excluded surfacing).

    Returns {peril_class, status, headline, matched_clauses[], region}.
    """
    rec = next((r for r in assessment["per_peril"] if r["peril_class"] == peril_class), None)
    if rec is None:
        return {
            "peril_class": peril_class,
            "status": CoverageStatus.UNDETERMINED,
            "headline": (
                f"Peril '{peril_class}' was not assessed against policy "
                f"{assessment.get('policy_id', '?')}."
            ),
            "matched_clauses": [],
            "region": region,
        }

    clauses = matched_clause_text(rec["matched_clause_ids"])
    policy_id = assessment.get("policy_id", "?")
    where = f" in {region}" if region else ""

    if rec["status"] == CoverageStatus.EXCLUDED and clauses:
        clause = clauses[0]
        # Surface the contrasting covered peril if any (the "covered for delay" beat).
        covered = assessment.get("covered_perils", [])
        contrast = ""
        if covered:
            human = covered[0].replace("_", " ")
            contrast = f"you are covered for {human}, NOT for the {peril_class.replace('_', ' ')} most likely to strand you{where}. "
        headline = (
            f"Policy {policy_id} EXCLUDES {peril_class.replace('_', ' ')} "
            f"(clause {clause['clause_id']}): {contrast}"
            f"\"{clause['clause_text']}\""
        )
    elif rec["status"] == CoverageStatus.SUB_LIMITED and clauses:
        clause = clauses[0]
        cap = rec.get("sub_limit_cents")
        cap_str = f" (capped at {cap} cents)" if cap is not None else ""
        headline = (
            f"Policy {policy_id} SUB-LIMITS {peril_class.replace('_', ' ')}{cap_str} "
            f"(clause {clause['clause_id']}): \"{clause['clause_text']}\""
        )
    elif rec["status"] == CoverageStatus.CONDITIONAL and clauses:
        clause = clauses[0]
        headline = (
            f"Policy {policy_id} covers {peril_class.replace('_', ' ')} ONLY CONDITIONALLY "
            f"(clause {clause['clause_id']}): \"{clause['clause_text']}\""
        )
    elif rec["status"] == CoverageStatus.COVERED and clauses:
        clause = clauses[0]
        headline = (
            f"Policy {policy_id} covers {peril_class.replace('_', ' ')} "
            f"(clause {clause['clause_id']})."
        )
    elif rec["status"] == CoverageStatus.EXCLUDED:
        # Clause text unavailable but status is authoritative — preserve EXCLUDED in headline.
        headline = (
            f"Policy {policy_id} EXCLUDES {peril_class.replace('_', ' ')} "
            f"(clause text unavailable — do NOT assume covered)."
        )
    elif rec["status"] == CoverageStatus.SUB_LIMITED:
        cap = rec.get("sub_limit_cents")
        cap_str = f" (capped at {cap} cents)" if cap is not None else ""
        headline = (
            f"Policy {policy_id} SUB-LIMITS {peril_class.replace('_', ' ')}{cap_str} "
            f"(clause text unavailable)."
        )
    elif rec["status"] == CoverageStatus.CONDITIONAL:
        headline = (
            f"Policy {policy_id} covers {peril_class.replace('_', ' ')} ONLY CONDITIONALLY "
            f"(clause text unavailable — conditions unspecified)."
        )
    else:
        # Status is UNDETERMINED or COVERED with no clause text.
        headline = (
            f"Policy {policy_id} has no clause addressing {peril_class.replace('_', ' ')} "
            f"— coverage is UNDETERMINED (not assumed covered)."
        )

    return {
        "peril_class": peril_class,
        "status": rec["status"],
        "headline": headline,
        "matched_clauses": clauses,
        "region": region,
    }


# ===========================================================================
# 6. LLM RATIONALE WRITER — COSMETIC ONLY, validated against matched clause text.
# ===========================================================================
# The ONLY LLM surface in the agent. It drafts a human-readable rationale; it is
# then VALIDATED to be a derivation of the matched clause text (it may introduce no
# new factual token outside the structured result). If the key is missing, the call
# fails, or validation fails → we fall back to the deterministic explain_exclusion
# headline. The assessment NUMBERS + STATUS never depend on it (NO-LLM-NUMBERS /
# DETERMINISTIC-STATUS). This is why the closed-set fallback test proves the LLM is
# cosmetic.

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
MODEL = os.environ.get("SOCIETY_LLM_MODEL", "qwen3-max")  # cheap model for test sweeps

# PUBLIC-EXPORT NOTE: this is a simplified stand-in for the prompt actually used in
# production. The real one is iteratively tuned against a private evaluation corpus
# (clause-grounding disambiguation, wording for each CoverageStatus, edge-case
# phrasing guidance, etc.) — that tuning is the product's work, not something this
# showcase repo hands out verbatim. This version keeps the JSON contract the rest
# of the pipeline depends on (the "rationale" key read in _llm_rationale above, and
# validated by validate_rationale) so the code still runs end-to-end, but the
# actual wording here is intentionally unrefined.
_LLM_SYSTEM_PROMPT = (
    "You are an insurance-coverage assistant. You will be given a peril, a coverage "
    "status, and matched policy clause text. Write one plain-English sentence "
    "explaining the coverage status to a traveler, using only the facts given — do "
    "not add anything not present in the clause text or status. Return JSON: "
    "{\"rationale\": \"...\"}."
)


def _llm_rationale(peril_class: str, status: str, clauses: list[dict[str, str]]) -> str | None:
    """
    Optional cosmetic rationale via DashScope. Returns None on any failure (caller
    falls back to the deterministic headline). NEVER affects numbers or status.
    """
    if not DASHSCOPE_API_KEY:
        return None
    try:
        from utils.model_router import dashscope_chat
    except ImportError:
        try:
            from model_router import dashscope_chat  # type: ignore[no-redef]
        except ImportError:
            return None  # router unavailable — treat as LLM-off
    clause_text = " ".join(c["clause_text"] for c in clauses)
    user = json.dumps({
        "peril_class": peril_class,
        "status": status,
        "matched_clause_text": clause_text,
    })
    try:
        data = dashscope_chat(
            "default",
            {"enable_thinking": False,
             "messages": [{"role": "system", "content": _LLM_SYSTEM_PROMPT},
                          {"role": "user", "content": user}],
             "response_format": {"type": "json_object"}},
            timeout=30.0,
        )
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content) if isinstance(content, str) else content
        rationale = parsed.get("rationale") if isinstance(parsed, dict) else None
        if not isinstance(rationale, str) or not rationale.strip():
            return None
        if not validate_rationale(rationale, status, clauses):
            logger.warning(
                "insurance: LLM rationale failed clause-derivation validation "
                "→ dropping (clamp held). peril=%s status=%s", peril_class, status,
            )
            return None
        return rationale.strip()
    except Exception as e:
        logger.info("insurance: LLM rationale unavailable (%s) — using deterministic headline", e)
        return None


def validate_rationale(rationale: str, status: str, clauses: list[dict[str, str]]) -> bool:
    """
    VARIANCE CLAMP for the cosmetic rationale: it must be a derivation of the
    matched clause text, and it must NOT contradict an EXCLUDED status.

    Conservative checks (false-negatives only drop a cosmetic string — safe):
      - an EXCLUDED status must not be described as 'covered'/'will pay';
      - no digit may appear unless that digit-run appears in the clause text
        (the LLM may not mint a number — NO-LLM-NUMBERS even for prose).
    """
    low = rationale.lower()
    if status == CoverageStatus.EXCLUDED:
        for bad in ("is covered", "are covered", "will pay", "you're covered", "you are covered"):
            if bad in low:
                return False
    # No fabricated numbers: every digit run in the rationale must appear in the clause text.
    import re as _re
    clause_blob = " ".join(c["clause_text"] for c in clauses)
    clause_digits = set(_re.findall(r"\d+", clause_blob))
    for run in _re.findall(r"\d+", rationale):
        if run not in clause_digits:
            return False
    return True


# ===========================================================================
# 7. SOURCE-ADAPTER STUB — Tier-1 seeded; Tier-3 honest fallback (§18).
# ===========================================================================
# Submission runs ENTIRELY on Tier-1 seed (reproducible). A live insurer/PDS feed
# is a Tier-2 adapter swap behind this seam, NOT a re-architecture. On an honest
# "source-unreachable" the agent marks source_freshness='SEEDED_FALLBACK' and uses
# the seeded closed-set template — it NEVER fabricates terms.

def fetch_policy_terms(policy_id: str, *, simulate_unreachable: bool = False) -> tuple[str, str]:
    """
    Resolve policy terms via the (deferred) insurance-terms source adapter.

    Returns (policy_id, source_freshness). Today this is the Tier-1 seeded cache.
    `simulate_unreachable=True` exercises the Tier-3 exhausted path → the agent
    falls back to the seeded template and tags SEEDED_FALLBACK (never fabricates).
    """
    if policy_id not in _POLICY_CATALOG:
        raise KeyError(f"insurance: unknown policy_id {policy_id!r}")
    if simulate_unreachable:
        logger.warning(
            "insurance: insurance-terms source unreachable (Tier-3 exhausted) "
            "→ SEEDED_FALLBACK for %s (terms NOT fabricated)", policy_id,
        )
        return policy_id, "SEEDED_FALLBACK"
    return policy_id, "SEEDED"


# ===========================================================================
# 8. InsuranceAgent — A2A wrapper (follows the §AGENT-EXTENSION PATTERN below).
# ===========================================================================

class InsuranceAgent(A2AAgent):
    """
    Insurance / residual-risk coverage specialist (Travel Guild, L2).

    Implements:
      ``insurance.assess_coverage``  — per-peril status + matched clauses + premium.
      ``insurance.explain_exclusion`` — DC0 headline from matched clause text.

    The assessment path is fully deterministic (no LLM, no network). The optional
    cosmetic rationale (DashScope) is fully clamped behind the deterministic result.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 9105) -> None:
        self._host = host
        self._port = port
        super().__init__()

    # ------------------------------------------------------------------
    # A2AAgent protocol
    # ------------------------------------------------------------------

    def _build_card(self) -> dict:
        url = f"http://{self._host}:{self._port}"
        return {
            "name": "insurance-agent",
            "description": (
                "Insurance / residual-risk coverage specialist (L2) — EXCLUSION-FIRST. "
                "Given a trip's perils (from Risk, via the canonical peril crosswalk) and a "
                "candidate policy's seeded terms, computes per-peril coverage status "
                "(COVERED/EXCLUDED/SUB_LIMITED/CONDITIONAL/UNDETERMINED) with the matching "
                "exclusion clauses as first-class output, and proposes the premium as a Budget "
                "line item. Deterministic clause-matcher; NO-LLM-NUMBERS (premium is integer "
                "cents from seed); the LLM only drafts validated cosmetic rationale. "
                "Implements 'insurance.assess_coverage' + 'insurance.explain_exclusion'. "
                "Part of the Travel Guild multi-agent pipeline (Track 3, L2 / DC0)."
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
                    "id": "insurance.assess_coverage",
                    "name": "Assess Coverage (exclusion-first)",
                    "description": (
                        "Given {peril_set[], policy_id?, insured_trip_cost_cents?} compute the "
                        "per-peril CoverageAssessment + premium line item. Status is a pure "
                        "deterministic function of (peril, clause set); EXCLUDE always wins. "
                        "Unmatched perils → UNDETERMINED (never silent COVERED). NO-LLM-NUMBERS."
                    ),
                    "tags": ["insurance", "coverage", "exclusion", "residual-risk",
                             "deterministic", "money-path", "L2"],
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "peril_set": {"type": "array", "items": {"type": "string"}},
                            "policy_id": {"type": "string"},
                            "insured_trip_cost_cents": {"type": "integer"},
                            "leg": {"type": "string"},
                            "region": {"type": "string"},
                            "risk_reason_codes": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "examples": [
                        json.dumps({"peril_set": ["civil_unrest", "travel_delay"],
                                    "policy_id": "P-STD-01",
                                    "insured_trip_cost_cents": 120000}),
                    ],
                },
                {
                    "id": "insurance.explain_exclusion",
                    "name": "Explain Exclusion (DC0 headline)",
                    "description": (
                        "Given a CoverageAssessment + a peril_class, return a headline built "
                        "ONLY from the matched clause text (no free generation of facts). "
                        "Pure formatter over validated structured data."
                    ),
                    "tags": ["insurance", "exclusion", "explanation", "DC0", "deterministic"],
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "assessment": {"type": "object"},
                            "peril_class": {"type": "string"},
                            "region": {"type": "string"},
                            "use_llm": {"type": "boolean"},
                        },
                        "required": ["peril_class"],
                    },
                },
                {
                    # #45 — coverage-GAP analyzer over the USER's OWN declared policy.
                    # DISTINCT from assess_coverage (which works the SEEDED VENDOR
                    # catalog): this recommends NO vendor, mints no premium, and only
                    # reports where the user is LEFT EXPOSED. Deterministic; no LLM on
                    # the gap path. OPT-IN — only runs when a user_policy is supplied.
                    "id": "insurance.coverage_gap",
                    "name": "Coverage-Gap Analyzer (user policy cross-check)",
                    "description": (
                        "Given {peril_set[]? OR risk_reason_codes[]?, user_policy{}, "
                        "insured_trip_cost_cents?} cross-check the trip's perils against "
                        "the TRAVELER's own declared policy and return a GapReport: per-"
                        "peril gaps (EXCLUDED / UNDETERMINED / SUB_LIMITED / CONDITIONAL), "
                        "generic coverage-TYPE recommendations (NO vendor/brand names), and "
                        "a fixed disclaimer. Undeclared peril → UNDETERMINED gap (never "
                        "assumed covered). Informational only — not insurance advice."
                    ),
                    "tags": ["insurance", "coverage-gap", "user-policy", "deterministic",
                             "informational", "no-vendor", "L2"],
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "peril_set": {"type": "array", "items": {"type": "string"}},
                            "risk_reason_codes": {"type": "array", "items": {"type": "string"}},
                            "user_policy": {"type": "object"},
                            "insured_trip_cost_cents": {"type": "integer"},
                        },
                        "required": ["user_policy"],
                    },
                    "examples": [
                        json.dumps({
                            "peril_set": ["medical", "war"],
                            "user_policy": {
                                "policy_label": "my existing policy",
                                "currency": "USD",
                                "declarations": [
                                    {"peril_class": "medical", "stance": "sub_limited",
                                     "limit_cents": 5000000},
                                ],
                                "global_exclusions": ["war"],
                            },
                            "insured_trip_cost_cents": 8000000,
                        }),
                    ],
                },
            ],
        }

    def _register_skills(self) -> None:
        self.register_skill("insurance.assess_coverage", self._assess_handler)
        self.register_skill("insurance.explain_exclusion", self._explain_handler)
        # #45 — coverage-gap analyzer (user policy). APPEND-ONLY; vendor path untouched.
        self.register_skill("insurance.coverage_gap", self._coverage_gap_handler)

    # ------------------------------------------------------------------
    # Skill handlers
    # ------------------------------------------------------------------

    async def _assess_handler(self, message: dict, task: dict) -> dict:
        payload = self._extract_payload(message)
        if not isinstance(payload, dict):
            raise ValueError(
                "insurance.assess_coverage requires a data part with a JSON object "
                "{peril_set[], policy_id?, insured_trip_cost_cents?}"
            )

        # Accept perils directly OR derive from Risk reason_codes via the Phase-0
        # crosswalk (Risk owns the SIGNAL; Insurance maps it to coverage).
        peril_set = list(payload.get("peril_set") or [])
        reason_codes = payload.get("risk_reason_codes")
        if reason_codes:
            from utils.peril_crosswalk import risk_reasons_to_perils
            crosswalked = risk_reasons_to_perils([str(r) for r in reason_codes])
            for p in crosswalked:
                if p not in peril_set:
                    peril_set.append(p)

        policy_id = payload.get("policy_id", DEFAULT_POLICY_ID)
        trip_cost = int(payload.get("insured_trip_cost_cents", 0) or 0)
        leg = payload.get("leg", PACKAGE_LEG)

        # Source-adapter resolution (Tier-1 seeded today; honest Tier-3 fallback).
        _, freshness = fetch_policy_terms(
            policy_id, simulate_unreachable=bool(payload.get("simulate_source_unreachable", False))
        )

        assessment = assess_coverage(
            peril_set=peril_set,
            policy_id=policy_id,
            insured_trip_cost_cents=trip_cost,
            leg=leg,
            source_freshness=freshness,
        )
        assessment["line_item"] = premium_line_item(assessment)

        logger.info(
            "insurance.assess_coverage: policy=%s perils=%d excluded=%d premium=%d¢ freshness=%s",
            policy_id, len(assessment["per_peril"]),
            len(assessment["excluded_perils_summary"]),
            assessment["premium_cents"], freshness,
        )
        return _new_artifact(
            name="insurance.assess_coverage.result",
            parts=[_data_part(assessment)],
        )

    async def _coverage_gap_handler(self, message: dict, task: dict) -> dict:
        """
        #45 — coverage-GAP analysis over the USER's OWN declared policy.

        DISTINCT from _assess_handler: that works the SEEDED VENDOR catalog; this
        consumes the traveler's policy and recommends NO vendor. Deterministic;
        the gap algorithm never calls the LLM. Accepts peril_set[] directly OR
        risk_reason_codes[] (crosswalked exactly like _assess_handler).
        """
        payload = self._extract_payload(message)
        if not isinstance(payload, dict):
            raise ValueError(
                "insurance.coverage_gap requires a data part with a JSON object "
                "{peril_set[]? OR risk_reason_codes[]?, user_policy{}, "
                "insured_trip_cost_cents?}"
            )

        user_policy = payload.get("user_policy")
        if not isinstance(user_policy, dict):
            raise ValueError(
                "insurance.coverage_gap: user_policy (object) is required"
            )

        # Accept perils directly OR derive from Risk reason_codes via the Phase-0
        # crosswalk (same path as _assess_handler — Risk owns the SIGNAL).
        peril_set = list(payload.get("peril_set") or [])
        reason_codes = payload.get("risk_reason_codes")
        if reason_codes:
            from utils.peril_crosswalk import risk_reasons_to_perils
            crosswalked = risk_reasons_to_perils([str(r) for r in reason_codes])
            for p in crosswalked:
                if p not in peril_set:
                    peril_set.append(p)

        trip_cost = int(payload.get("insured_trip_cost_cents", 0) or 0)

        # Import-light: coverage_gap depends only on contracts + this module's vocab.
        from utils.coverage_gap import analyze_coverage_gap

        report = analyze_coverage_gap(
            peril_set=peril_set,
            user_policy=user_policy,
            insured_trip_cost_cents=trip_cost,
        )

        logger.info(
            "insurance.coverage_gap: perils=%d gaps=%d recs=%d insured=%d¢",
            len(peril_set), len(report["gaps"]),
            len(report["recommendations"]), report["insured_trip_cost_cents"],
        )
        return _new_artifact(
            name="insurance.coverage_gap.result",
            parts=[_data_part(report)],
        )

    async def _explain_handler(self, message: dict, task: dict) -> dict:
        payload = self._extract_payload(message)
        if not isinstance(payload, dict):
            raise ValueError(
                "insurance.explain_exclusion requires a data part with "
                "{assessment, peril_class, region?}"
            )
        peril_class = payload.get("peril_class")
        if not peril_class:
            raise ValueError("insurance.explain_exclusion: peril_class is required")

        assessment = payload.get("assessment")
        # Convenience: if no assessment supplied, build one for the single peril so
        # explain_exclusion is usable standalone (DC0 demo / Concierge surfacing).
        if not isinstance(assessment, dict):
            assessment = assess_coverage(
                peril_set=[peril_class],
                policy_id=payload.get("policy_id", DEFAULT_POLICY_ID),
                insured_trip_cost_cents=int(payload.get("insured_trip_cost_cents", 0) or 0),
            )

        region = payload.get("region")
        result = explain_exclusion(assessment, peril_class, region=region)

        # Optional cosmetic LLM rationale — clamped + validated; falls back to the
        # deterministic headline. NEVER changes status/numbers.
        if payload.get("use_llm"):
            rationale = _llm_rationale(peril_class, result["status"], result["matched_clauses"])
            result["llm_rationale"] = rationale  # None if unavailable/invalid
            result["rationale_source"] = "llm" if rationale else "deterministic"
        else:
            result["rationale_source"] = "deterministic"

        return _new_artifact(
            name="insurance.explain_exclusion.result",
            parts=[_data_part(result)],
        )

    # ------------------------------------------------------------------
    # Input extraction helper (same pattern as Transport / Critic / Planner).
    # ------------------------------------------------------------------

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
    port = int(os.environ.get("PORT", 9105))
    host = os.environ.get("AGENT_BIND_HOST", "127.0.0.1")

    agent = InsuranceAgent(host=host, port=port)
    app = agent.build_app()

    logger.info("Insurance agent starting on %s:%d", host, port)
    logger.info("Agent Card: http://%s:%d/.well-known/agent-card.json", host, port)
    logger.info("RPC endpoint: http://%s:%d/", host, port)

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()


# ===========================================================================
# §AGENT-EXTENSION PATTERN — how this agent wired in, and how the NEXT
# roadmap specialists (Risk, Compliance, Health, Fraud, Loyalty, ...) follow it.
# ===========================================================================
#
# This is the FIRST roadmap specialist built on the Phase-0 contracts, so it
# DOCUMENTS the repeatable extension pattern for clean SERIAL integration. The
# guiding principle: a new specialist is a NEW FILE + (optionally) an APPEND-ONLY
# registry/wiring add. Shared files are touched minimally and only by appending.
#
# THE 6-STEP PATTERN (what Insurance did; what the next agent repeats):
#
#   1. NEW AGENT FILE: `<name>_agent.py`, subclass `a2a_agent.A2AAgent`.
#      Override `_build_card()` (camelCase AgentCard: name/description/url/skills[])
#      and `_register_skills()` (one `register_skill("<name>.<verb>", handler)` per
#      skill). Add a `main()` on a NEW PORT (echo 9100, budget 9101, planner 9102,
#      critic 9103, transport/destination 9104, INSURANCE 9105 — next agent: 9106).
#
#   2. COMPOSE AGAINST PHASE-0 CONTRACTS — do NOT invent parallel shapes:
#        - money:      contracts.make_money() / validate_money() (Budget reads usd_cents).
#        - provenance: contracts.make_provenance() on every sourced fact (Critic verifies).
#        - perils:     contracts.Peril (the ONE closed enum) — never a private taxonomy.
#        - Risk→peril: peril_crosswalk.risk_reasons_to_perils() (Risk owns the signal).
#        - fees:       line_item_assembler.LineItemAssembler.add_fee(kind=...) — keyed,
#                      idempotent → Budget vetoes it like any other line item.
#        - FX:         fx_provider.normalize() if the fee is non-USD.
#
#   3. VARIANCE CLAMP (mandatory for money-path agents): NO-LLM-NUMBERS — every
#      numeric field is integer cents from seed/adapter; the decision (here: status)
#      is a PURE function over closed sets; the LLM is COSMETIC ONLY and validated
#      against the structured result. Prove it with a closed-set fallback test that
#      runs with the LLM disabled and still produces the correct structured output.
#
#   4. SEEDED DATA, provenance-tagged: keep a `<name>`-local seed table (Tier-1) +
#      a source-adapter stub with an honest Tier-3 fallback (mark SEEDED_FALLBACK,
#      never fabricate). The live feed is a Tier-2 swap behind the seam — NOT a
#      re-architecture.
#
#   5. WIRING — APPEND-ONLY, minimal: the orchestrator already takes optional
#      `<agent>_url` / `<agent>_client` constructor params + a `_call_<agent>()`
#      helper (see orchestrator.py budget/critic/transport). A new agent ADDS one
#      such optional param + helper (defaulting to None so every existing call site
#      and test is unchanged — backward-compatible by construction). The demo
#      instantiates the agent on its port. There is NO central registry file to edit
#      — registration IS the AgentCard + the orchestrator's optional hook. (For the
#      submission-feasible DC0 demo, Insurance is also driven directly by its own
#      recorder benchmark/run_dc0_bench.py, mirroring S6/S7's standalone recorders.)
#
#   6. VERIFY HARD: a unit suite (test_<name>_agent.py, CI-safe, in-process ASGI
#      TestClient) + a CASCADE-STYLE invariant regression test that FAILS if the
#      agent's core invariant is ever violated or a number drifts across N runs.
#      Add the suite to the Makefile `test` target. No regression in S1–S7 + demos.
#
# Following these 6 steps, each subsequent specialist integrates as an isolated,
# append-only change — clean serial integration with zero shared-file churn beyond
# one optional orchestrator hook and one Makefile line.
