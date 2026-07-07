"""
contracts.py — PHASE-0 SHARED CONTRACTS for the Travel Guild (Track-3 A2A).

Design contract: AGENT-SOCIETY-A2A-DESIGN.md §18 (sourcing substrate;
reasoning ⊥ sourcing; LayeredProvider Tier1→Tier2→Tier3; provenance;
never-hallucinate), §19.1 (the Phase-0 shared-contract gap list), §19.5
(audit refinements).

WHY THIS FILE EXISTS
--------------------
The §19 reconcile proved the 9 roadmap specialist agents (Insurance, Risk,
Compliance, Health, Fraud, Loyalty, Connectivity, Pacing, Concierge) will NOT
integrate unless a single, coherent set of seams is locked FIRST. This module
is that foundation. It defines ONLY the seams the future agents compose
against — it deliberately contains NO domain logic (no coverage rules, no
visa tables, no advisory scoring). Each future agent owns its own MECHANISM
behind its own validator (§17); this file is the shared vocabulary + the
deterministic validators that keep correctness-variance ≈ 0 across them.

The thesis it serves (§10.11 / §13 / §18):
  - variance-clamped: deterministic validators + closed sets; numbers never
    from the LLM.
  - seeded / reproducible: every table here is a fixed, provenance-tagged
    snapshot; live feeds become a Tier-2 adapter swap, not a re-architecture.

CONTENTS (the §19.1 gap list, one coherent set)
  1.  Provenance envelope        — ONE {source, source_url, fetched_at, ttl, tier}
  2.  Source tier enum           — Tier-1 cache / Tier-2 live / Tier-3 exhausted
  3.  Currency-cents tagging      — {native_cents, currency, fx, usd_cents}
                                    (the FX PROVIDER SEAM lives in fx_provider.py;
                                     this is the shared SHAPE + validator)
  4.  Canonical peril enum        — ONE closed peril vocabulary
  5.  Risk advisory reason-codes  — ONE closed set Risk emits
      + Risk→Insurance crosswalk  — deterministic, total mapping (peril_crosswalk.py)
  6.  Critical-bag classifier     — the N4 single-classifier contract (Risk owns)
  7.  YF-cert handoff             — Health owns the JAB, Compliance owns the DOC
  8.  Frontier-event shape        — {frontier_class, enumerated_options[], tradeoffs[]}
  9.  Place keyspace              — canonical place key (reconcile node_id/area_id)
  10. Counterparty↔catalog id     — one identity for a bookable counterparty
  11. Traveler / party identity   — per-traveler, multi-nationality (N5)

Every shape ships a lightweight deterministic validator (``validate_*`` /
``is_valid_*``) so any agent can fail fast on a malformed envelope rather than
silently passing a bad fact downstream (the §18 "one validated truth vs N
independent guessers" discipline).

Security: no secrets, no network, no LLM calls. Pure deterministic Python.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

# ===========================================================================
# 2. SOURCE TIER ENUM (§18 LayeredProvider)
# ===========================================================================
# The §18 fail-plan: Tier-1 cache → Tier-2 live → Tier-3 exhausted. Mirrors
# travel_memory/retrieval.py's tier vocabulary so a provenance envelope from
# ANY provider speaks ONE tier language.


class SourceTier(str, Enum):
    """The §18 provider layer a fact was sourced from."""

    CACHE = "cache"        # Tier-1: seeded / cached snapshot (the submission default, §13)
    LIVE = "live"          # Tier-2: live fetch (deferred swap; same shape)
    EXHAUSTED = "exhausted"  # Tier-3: no data — honest stop, NEVER hallucinate
    SEEDED = "seeded"      # explicit seeded-constant tag (alias of cache for fixed tables)


# The full closed set of legal tier string values (for validators).
_TIER_VALUES: frozenset[str] = frozenset(t.value for t in SourceTier)


# ===========================================================================
# 1. PROVENANCE ENVELOPE (§19.1 "one provenance envelope")
# ===========================================================================
# ONE shape every sourced fact carries: {source, source_url, fetched_at, ttl,
# tier}. The Critic verifies freshness + authority against THIS shape (§18:
# "source-trust governance → the Critic"). `fetched_at` is an ISO-8601 string
# (seeded snapshots use a fixed date); `ttl` is seconds (None = no expiry for a
# fixed seeded constant). `source` is the authoritative origin name; `source_url`
# may be None for a purely-seeded internal table.

_PROVENANCE_REQUIRED: tuple[str, ...] = ("source", "source_url", "fetched_at", "ttl", "tier")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def make_provenance(
    source: str,
    fetched_at: str,
    tier: SourceTier | str,
    source_url: str | None = None,
    ttl: int | None = None,
) -> dict[str, Any]:
    """
    Build a canonical provenance envelope.

    Args:
        source:     Authoritative origin name (e.g. "MoneyHub NZ 2024-11-11",
                    "seeded-2026-01 FX snapshot"). Never empty.
        fetched_at: ISO-8601 date/datetime the fact was captured (seeded = fixed).
        tier:       SourceTier (cache/live/exhausted/seeded).
        source_url: Authoritative URL, or None for a purely-internal seeded table.
        ttl:        Freshness window in seconds, or None for a fixed seeded constant.

    Returns:
        {source, source_url, fetched_at, ttl, tier} — the ONE provenance shape.
    """
    tier_val = tier.value if isinstance(tier, SourceTier) else str(tier)
    return {
        "source": source,
        "source_url": source_url,
        "fetched_at": fetched_at,
        "ttl": ttl,
        "tier": tier_val,
    }


def validate_provenance(env: Any) -> list[str]:
    """
    Validate a provenance envelope. Returns a list of error strings (empty = OK).

    Deterministic: same input → same error list. Never raises.
    """
    errs: list[str] = []
    if not isinstance(env, dict):
        return ["provenance: not a dict"]
    for key in _PROVENANCE_REQUIRED:
        if key not in env:
            errs.append(f"provenance: missing required key {key!r}")
    if errs:
        return errs
    if not isinstance(env["source"], str) or not env["source"].strip():
        errs.append("provenance: source must be a non-empty string")
    if env["source_url"] is not None and not isinstance(env["source_url"], str):
        errs.append("provenance: source_url must be a string or None")
    if not isinstance(env["fetched_at"], str) or not _ISO_DATE_RE.match(env["fetched_at"]):
        errs.append("provenance: fetched_at must be an ISO-8601 date string (YYYY-MM-DD...)")
    if env["ttl"] is not None and (not isinstance(env["ttl"], int) or env["ttl"] < 0):
        errs.append("provenance: ttl must be a non-negative int or None")
    if env["tier"] not in _TIER_VALUES:
        errs.append(f"provenance: tier {env['tier']!r} not in {sorted(_TIER_VALUES)}")
    return errs


def is_valid_provenance(env: Any) -> bool:
    """True iff `env` is a well-formed provenance envelope."""
    return not validate_provenance(env)


# ---------------------------------------------------------------------------
# 1a. TTL ENFORCEMENT (fetched_at + ttl → stale downgrade)
# ---------------------------------------------------------------------------
# `_SEED_TTL_DAYS` is stamped into every envelope by every agent (compliance/
# health/risk/insurance/fraud) but, until now, nothing ever CHECKED it — a
# seeded rule kept being served as confidently-current forever, even long
# past its own stated freshness window. This is the single shared check every
# agent wires in at its own "ruling about to be returned" boundary.
#
# `as_of` is ALWAYS an explicit ISO-8601 date string threaded in by the
# caller (the same deterministic "today" already threaded through compliance/
# health/risk for D7 — the single frozen run-clock), NEVER a direct
# datetime.now()/date.today() read. This module is pure/deterministic (no
# network, no wall-clock) by design (see module docstring); a TTL check is
# the one place "the same inputs" must include "what day is it", so that
# input is passed in explicitly rather than smuggled in via a hidden clock
# read. `as_of=None` (no deterministic clock available) is treated as "not
# evaluated" (never expired) — conservative in the sense of "don't invent a
# clock", not in the sense of "hide staleness"; every real call site threads
# a real `today`.


def is_provenance_expired(env: Any, *, as_of: str | None) -> bool:
    """
    True iff `env` is a well-formed provenance envelope whose TTL window has
    elapsed as of `as_of` (an ISO-8601 date string).

    A `ttl` of None (no expiry — a fixed seeded constant with no freshness
    window by design) is NEVER expired. A malformed/invalid envelope is never
    reported expired here (validate it separately via validate_provenance).
    """
    if as_of is None:
        return False
    if not is_valid_provenance(env):
        return False
    ttl = env["ttl"]
    if ttl is None:
        return False
    try:
        fetched = datetime.strptime(env["fetched_at"][:10], "%Y-%m-%d")
        now = datetime.strptime(str(as_of)[:10], "%Y-%m-%d")
    except ValueError:
        return False
    return now >= fetched + timedelta(seconds=int(ttl))


def stale_provenance_note(env: dict[str, Any]) -> str:
    """Honest, user-facing note for a ruling whose provenance TTL has elapsed."""
    ttl = env.get("ttl")
    ttl_days = f"{int(ttl) / 86400:.0f}" if isinstance(ttl, (int, float)) else "its"
    source = env.get("source") or "unknown source"
    fetched_at = env.get("fetched_at") or "an unknown date"
    return (
        f"This data (source: {source}, fetched {fetched_at}) is past its "
        f"{ttl_days}-day freshness window — it may be stale. Please reconfirm "
        "before travel."
    )


def apply_ttl_downgrade(
    record: dict[str, Any],
    *,
    as_of: str | None,
    verified_field: str,
) -> bool:
    """
    Mutate `record` IN PLACE: if `record["provenance"]`'s TTL has elapsed as of
    `as_of`, stamp an honest stale-data note and, iff `record[verified_field]`
    is currently `True` (a confident "verified OK/allowed/complete"),
    downgrade it to `None` — reusing this codebase's OWN existing "unverified
    flag" convention (e.g. compliance's `allowed=None` / health's
    `can_complete=None`: the trip still proceeds, an advisory is surfaced,
    never a silent confident pass). A record that is ALREADY a hard block
    (False) or already unverified (None) is left as-is except for the stale
    note — staleness must never LOOSEN an existing block, only ever add
    honesty, never remove it.

    Returns True iff the envelope was found expired (a note was applied).
    """
    prov = record.get("provenance")
    if not is_provenance_expired(prov, as_of=as_of):
        return False
    record["provenance_stale"] = True
    record["stale_note"] = stale_provenance_note(prov)
    if record.get(verified_field) is True:
        record[verified_field] = None
        record["unverified_flag"] = True
        record["flag_advisory"] = record["stale_note"]
    return True


# ===========================================================================
# 3. CURRENCY-CENTS TAGGING (§19.1 "currency-cents tagging")
# ===========================================================================
# ONE shape for a money amount that may originate in a non-USD currency:
#   {native_cents, currency, fx, usd_cents}
# where `fx` is the FX-leg sub-shape {fx_rate, fx_source, fetched_at} carried
# from the FX PROVIDER SEAM (fx_provider.py). Budget consumes `usd_cents` for
# the merchant veto and NEVER decides a rate (§19.5: "Budget consumes usd_cents").
#
# This module owns the SHAPE + validator; fx_provider.py owns the seeded rates
# and the {native_cents → usd_cents} conversion (the real cross-cutting impl).

_MONEY_REQUIRED: tuple[str, ...] = ("native_cents", "currency", "fx", "usd_cents")
_FX_REQUIRED: tuple[str, ...] = ("fx_rate", "fx_source", "fetched_at")
_ISO_CCY_RE = re.compile(r"^[A-Z]{3}$")


def make_money(
    native_cents: int,
    currency: str,
    usd_cents: int,
    fx_rate: float,
    fx_source: str,
    fetched_at: str,
) -> dict[str, Any]:
    """
    Build a canonical currency-cents tag.

    `currency` is normalised to an upper-case ISO-4217 code. `usd_cents` is the
    value Budget feeds to the merchant veto; for USD inputs it equals
    native_cents and fx_rate is 1.0.
    """
    return {
        "native_cents": int(native_cents),
        "currency": str(currency).upper(),
        "usd_cents": int(usd_cents),
        "fx": {
            "fx_rate": float(fx_rate),
            "fx_source": str(fx_source),
            "fetched_at": str(fetched_at),
        },
    }


def validate_money(tag: Any) -> list[str]:
    """Validate a currency-cents tag. Returns error strings (empty = OK)."""
    errs: list[str] = []
    if not isinstance(tag, dict):
        return ["money: not a dict"]
    for key in _MONEY_REQUIRED:
        if key not in tag:
            errs.append(f"money: missing required key {key!r}")
    if errs:
        return errs
    if not isinstance(tag["native_cents"], int) or tag["native_cents"] < 0:
        errs.append("money: native_cents must be a non-negative int")
    if not isinstance(tag["usd_cents"], int) or tag["usd_cents"] < 0:
        errs.append("money: usd_cents must be a non-negative int")
    if not isinstance(tag["currency"], str) or not _ISO_CCY_RE.match(tag["currency"]):
        errs.append("money: currency must be an upper-case ISO-4217 code")
    fx = tag["fx"]
    if not isinstance(fx, dict):
        errs.append("money: fx must be a dict {fx_rate, fx_source, fetched_at}")
    else:
        for key in _FX_REQUIRED:
            if key not in fx:
                errs.append(f"money.fx: missing required key {key!r}")
        if "fx_rate" in fx and (
            not isinstance(fx["fx_rate"], (int, float)) or fx["fx_rate"] <= 0
        ):
            errs.append("money.fx: fx_rate must be a positive number")
        if "fx_source" in fx and (not isinstance(fx["fx_source"], str) or not fx["fx_source"]):
            errs.append("money.fx: fx_source must be a non-empty string")
    return errs


def is_valid_money(tag: Any) -> bool:
    """True iff `tag` is a well-formed currency-cents tag."""
    return not validate_money(tag)


# ===========================================================================
# 4. CANONICAL PERIL ENUM (§19.1 "ONE canonical peril enum")
# ===========================================================================
# The single closed peril vocabulary the whole society shares. Insurance maps
# a peril to COVERAGE; Risk maps its advisory signal to a peril (the crosswalk
# below). DC0 depends on civil_unrest flowing Risk→Insurance.
#
# Grounded against the §12.3 coverage matrix (medical, cancellation,
# interruption, baggage, adventure, delay, war/unrest exclusion) and the §16/
# §16.11 fault classes. This is a CATEGORY-level vocabulary (§12.3 "BUILD at
# category level"); finer covered-reasons lists are a future refinement, NOT
# Phase-0.


class Peril(str, Enum):
    """The ONE closed peril vocabulary (Insurance coverage keys to it)."""

    MEDICAL = "medical"                  # emergency medical / medevac (§12.3 #1 risk)
    TRIP_CANCELLATION = "trip_cancellation"   # pre-departure cancellation
    TRIP_INTERRUPTION = "trip_interruption"   # curtailment mid-trip
    TRAVEL_DELAY = "travel_delay"        # missed connection / delay (§16.12 gap)
    BAGGAGE_LOSS = "baggage_loss"        # lost/delayed baggage (ties N4 critical-bag)
    ADVENTURE_ACTIVITY = "adventure_activity"  # ski/scuba/surf high-risk (add-on)
    NATURAL_DISASTER = "natural_disaster"      # weather/volcanic/fire (§12.1)
    CIVIL_UNREST = "civil_unrest"        # riots/unrest — DC0 exclusion peril
    WAR = "war"                          # war/invasion — classic exclusion (DC0)
    TERRORISM = "terrorism"              # terrorism peril
    PANDEMIC = "pandemic"                # epidemic/pandemic disruption
    SUPPLIER_INSOLVENCY = "supplier_insolvency"  # N1 counterparty default
    PERSONAL_LIABILITY = "personal_liability"    # liability / legal defence
    MEDICAL_EVACUATION = "medical_evacuation"    # emergency medical evacuation (medevac)


_PERIL_VALUES: frozenset[str] = frozenset(p.value for p in Peril)


def is_valid_peril(value: Any) -> bool:
    """True iff `value` is a member of the canonical peril vocabulary."""
    return isinstance(value, str) and value in _PERIL_VALUES


def all_perils() -> tuple[str, ...]:
    """The full closed peril set as a stable, sorted tuple (deterministic)."""
    return tuple(sorted(_PERIL_VALUES))


# ===========================================================================
# 5a. RISK ADVISORY REASON-CODES (§19.1: "Risk owns the threat SIGNAL")
# ===========================================================================
# The closed set of reason-codes the Risk agent emits with its advisory_level.
# Risk OWNS this signal; Insurance does not re-derive it — it consumes it via
# the crosswalk (peril_crosswalk.py). One reason-code → one peril_class.


class RiskReasonCode(str, Enum):
    """The closed set of advisory reason-codes Risk emits (the threat SIGNAL)."""

    CIVIL_UNREST = "civil_unrest"            # protests / riots → CIVIL_UNREST peril
    ARMED_CONFLICT = "armed_conflict"        # war/invasion → WAR peril (DC0)
    TERRORISM = "terrorism"                  # terror threat → TERRORISM peril
    NATURAL_DISASTER = "natural_disaster"    # weather/volcanic/fire → NATURAL_DISASTER
    FLOOD = "flood"                          # seasonal/riverine/pluvial flood → NATURAL_DISASTER
    DISEASE_OUTBREAK = "disease_outbreak"    # epidemic → PANDEMIC peril
    CRIME = "crime"                          # elevated crime → PERSONAL_LIABILITY proxy
    TRANSPORT_DISRUPTION = "transport_disruption"  # strike/closure → TRAVEL_DELAY
    HEALTH_HAZARD = "health_hazard"          # altitude/medical hazard → MEDICAL
    ADVISORY_ELEVATED = "advisory_elevated"  # generic elevated advisory (no specific cause) → PERSONAL_LIABILITY proxy, NOT war


_RISK_REASON_VALUES: frozenset[str] = frozenset(r.value for r in RiskReasonCode)


def is_valid_risk_reason(value: Any) -> bool:
    """True iff `value` is a member of the closed Risk reason-code set."""
    return isinstance(value, str) and value in _RISK_REASON_VALUES


# ===========================================================================
# 5b. DO-NOT-RECOMMEND COUNTRY SET (armed-conflict → uninsurable → DECLINE)
# ===========================================================================
# THE single source of truth for the armed-conflict DO-NOT-RECOMMEND set. This
# is DISTINCT from an ordinary L3/L4 advisory FLAG (advisory-level design: L3/L4
# = FLAG + guide recommendation, still bookable). Membership here is a HARD,
# honest, non-bookable TERMINAL: an active armed conflict makes the trip
# UNINSURABLE under the EXC-WAR-2 war exclusion (peril WAR / reason-code
# ARMED_CONFLICT), so the society DECLINES rather than booking a trip it cannot
# stand behind (HONESTY / fail-conservative thesis).
#
# DECISION 2026-06-19 (armed conflict → uninsurable + decline): the 7 ISO-3166
# alpha-2 codes below are the ratified set. Any agent that must reason about the
# decline-vs-flag boundary reads THIS frozenset — it is THE authority; do NOT
# re-derive a parallel list elsewhere.
#
# DELIBERATELY EXCLUDED: Russia (RU) is NOT a member. RU stays an ordinary
# advisory FLAG and remains BOOKABLE (a high advisory level is not, by itself,
# an active-armed-conflict decline). Do not add RU here.
#
# var-0: a frozenset of fixed upper-case ISO2 codes; any message that enumerates
# the set MUST iterate sorted() for a deterministic string.
DO_NOT_RECOMMEND_COUNTRIES: frozenset[str] = frozenset({
    "CD",  # DR Congo
    "MM",  # Myanmar
    "SD",  # Sudan
    "IQ",  # Iraq
    "AF",  # Afghanistan
    "YE",  # Yemen
    "UA",  # Ukraine — active war (added 2026-06-19; has catalog hotels, still declines)
    # --- Conflict / state-collapse / no-go states (user decision 2026-06-19): all
    # L4 do-not-travel; declined + war/conflict-risk uninsurable (EXC-WAR-2). NOT a
    # catalog-harvest target — explicit exclusion, not bookable hotels.
    "SY",  # Syria — armed conflict
    "LY",  # Libya — armed conflict
    "SO",  # Somalia — armed conflict / insurgency
    "SS",  # South Sudan — armed conflict
    "ML",  # Mali — Sahel insurgency
    "NE",  # Niger — Sahel insurgency
    "BF",  # Burkina Faso — Sahel insurgency
    "CF",  # Central African Republic — armed conflict
    "KP",  # North Korea — armistice-only (technically at war) / no independent tourism
    "HT",  # Haiti — armed gang control / state collapse
})


def is_do_not_recommend_country(code: Any) -> bool:
    """
    True iff `code` is a member of the armed-conflict DO-NOT-RECOMMEND set.

    Membership is decided on the UPPER-CASED ISO-3166 alpha-2 code (fail-
    conservative: a non-string / unknown input is simply not a member → False,
    it never short-circuits the decline of a genuine set member elsewhere).
    """
    return isinstance(code, str) and code.strip().upper() in DO_NOT_RECOMMEND_COUNTRIES


# ===========================================================================
# 6. N4 CRITICAL-BAG CLASSIFIER CONTRACT (§19.1: "single classifier")
# ===========================================================================
# Risk OWNS critical-bag classification (one classifier). Accommodation/Planner
# (cabin-only constraint) and Health (med legality) CONSUME the classification;
# they do NOT re-classify. This is the contract shape Risk emits and the others
# read.
#
# bag_class is a closed set; cabin_only is the enforced hard-constraint signal.


class BagClass(str, Enum):
    """Closed set for the N4 single-classifier (Risk owns)."""

    CRITICAL_MEDICAL = "critical_medical"      # insulin / CPAP — medical dependency
    CRITICAL_DOCUMENT = "critical_document"    # passport / visa docs
    CRITICAL_VALUABLE = "critical_valuable"    # high-value / irreplaceable
    STANDARD = "standard"                      # ordinary checked baggage


_BAG_CLASS_VALUES: frozenset[str] = frozenset(b.value for b in BagClass)

_BAG_CLASSIFICATION_REQUIRED: tuple[str, ...] = (
    "item",          # free-text item label (e.g. "insulin")
    "bag_class",     # one of BagClass
    "cabin_only",    # bool: hard constraint that the item must travel in cabin
    "classified_by",  # MUST be "risk" — the single classifier (§19.1)
)


def make_bag_classification(
    item: str,
    bag_class: BagClass | str,
    cabin_only: bool,
) -> dict[str, Any]:
    """
    Build the N4 critical-bag classification record (Risk is the sole emitter).

    `classified_by` is hard-coded to "risk" so consumers can assert provenance
    of the classification (the single-classifier invariant).
    """
    bc = bag_class.value if isinstance(bag_class, BagClass) else str(bag_class)
    return {
        "item": str(item),
        "bag_class": bc,
        "cabin_only": bool(cabin_only),
        "classified_by": "risk",
    }


def validate_bag_classification(rec: Any) -> list[str]:
    """Validate an N4 bag-classification record. Returns error strings."""
    errs: list[str] = []
    if not isinstance(rec, dict):
        return ["bag_classification: not a dict"]
    for key in _BAG_CLASSIFICATION_REQUIRED:
        if key not in rec:
            errs.append(f"bag_classification: missing required key {key!r}")
    if errs:
        return errs
    if not isinstance(rec["item"], str) or not rec["item"].strip():
        errs.append("bag_classification: item must be a non-empty string")
    if rec["bag_class"] not in _BAG_CLASS_VALUES:
        errs.append(
            f"bag_classification: bag_class {rec['bag_class']!r} not in "
            f"{sorted(_BAG_CLASS_VALUES)}"
        )
    if not isinstance(rec["cabin_only"], bool):
        errs.append("bag_classification: cabin_only must be a bool")
    if rec["classified_by"] != "risk":
        errs.append(
            "bag_classification: classified_by must be 'risk' (single-classifier "
            "invariant §19.1 — only Risk classifies)"
        )
    return errs


def is_valid_bag_classification(rec: Any) -> bool:
    """True iff `rec` is a well-formed N4 bag-classification record."""
    return not validate_bag_classification(rec)


# ===========================================================================
# 7. YF-CERT HEALTH↔COMPLIANCE HANDOFF (§19.1: YF-cert ownership handoff)
# ===========================================================================
# Yellow-fever (and similar mandatory-cert) handoff. Health OWNS the JAB
# (cost/lead-time/validity); Compliance OWNS the ENTRY DOCUMENT (denied entry
# without it). The handoff is explicit so neither re-owns the other's half
# (the DC2 interlock).
#
#   Health emits   -> the JAB half  (make_jab_record)
#   Compliance reads the jab half and emits the DOC half (make_entry_doc_requirement)
# The two compose into a YF-cert handoff envelope keyed by vaccine + place.

_JAB_REQUIRED: tuple[str, ...] = (
    "vaccine",            # e.g. "yellow_fever"
    "cost",               # currency-cents tag (make_money) — Budget line item
    "lead_time_days",     # int: days before departure the jab must be taken
    "validity_years",     # int|None: cert validity (YF ~10yr); None = single-trip
    "owned_by",           # MUST be "health"
)

_ENTRY_DOC_REQUIRED: tuple[str, ...] = (
    "document",           # e.g. "yellow_fever_certificate"
    "vaccine",            # the jab this doc certifies (links to the Health half)
    "mandatory_for_entry",  # bool: denied entry without it
    "place_key",          # canonical place key the rule applies to
    "owned_by",           # MUST be "compliance"
)


def make_jab_record(
    vaccine: str,
    cost: dict[str, Any],
    lead_time_days: int,
    validity_years: int | None,
) -> dict[str, Any]:
    """Health's half of the YF-cert handoff: the JAB (cost/lead-time/validity)."""
    return {
        "vaccine": str(vaccine),
        "cost": cost,
        "lead_time_days": int(lead_time_days),
        "validity_years": validity_years,
        "owned_by": "health",
    }


def make_entry_doc_requirement(
    document: str,
    vaccine: str,
    mandatory_for_entry: bool,
    place_key: str,
) -> dict[str, Any]:
    """Compliance's half: the ENTRY DOCUMENT (denied entry without it)."""
    return {
        "document": str(document),
        "vaccine": str(vaccine),
        "mandatory_for_entry": bool(mandatory_for_entry),
        "place_key": str(place_key),
        "owned_by": "compliance",
    }


def validate_jab_record(rec: Any) -> list[str]:
    """Validate Health's JAB half of the YF-cert handoff."""
    errs: list[str] = []
    if not isinstance(rec, dict):
        return ["jab_record: not a dict"]
    for key in _JAB_REQUIRED:
        if key not in rec:
            errs.append(f"jab_record: missing required key {key!r}")
    if errs:
        return errs
    if not isinstance(rec["vaccine"], str) or not rec["vaccine"].strip():
        errs.append("jab_record: vaccine must be a non-empty string")
    errs.extend(f"jab_record.cost.{e}" for e in validate_money(rec["cost"]))
    if not isinstance(rec["lead_time_days"], int) or rec["lead_time_days"] < 0:
        errs.append("jab_record: lead_time_days must be a non-negative int")
    if rec["validity_years"] is not None and (
        not isinstance(rec["validity_years"], int) or rec["validity_years"] <= 0
    ):
        errs.append("jab_record: validity_years must be a positive int or None")
    if rec["owned_by"] != "health":
        errs.append("jab_record: owned_by must be 'health' (Health owns the jab §19.1)")
    return errs


def validate_entry_doc_requirement(rec: Any) -> list[str]:
    """Validate Compliance's ENTRY-DOCUMENT half of the YF-cert handoff."""
    errs: list[str] = []
    if not isinstance(rec, dict):
        return ["entry_doc: not a dict"]
    for key in _ENTRY_DOC_REQUIRED:
        if key not in rec:
            errs.append(f"entry_doc: missing required key {key!r}")
    if errs:
        return errs
    if not isinstance(rec["document"], str) or not rec["document"].strip():
        errs.append("entry_doc: document must be a non-empty string")
    if not isinstance(rec["vaccine"], str) or not rec["vaccine"].strip():
        errs.append("entry_doc: vaccine must be a non-empty string")
    if not isinstance(rec["mandatory_for_entry"], bool):
        errs.append("entry_doc: mandatory_for_entry must be a bool")
    if not is_valid_place_key(rec["place_key"]):
        errs.append("entry_doc: place_key must be a valid canonical place key")
    if rec["owned_by"] != "compliance":
        errs.append(
            "entry_doc: owned_by must be 'compliance' (Compliance owns the entry doc §19.1)"
        )
    return errs


def is_valid_jab_record(rec: Any) -> bool:
    """True iff `rec` is a well-formed Health JAB record."""
    return not validate_jab_record(rec)


def is_valid_entry_doc_requirement(rec: Any) -> bool:
    """True iff `rec` is a well-formed Compliance entry-doc requirement."""
    return not validate_entry_doc_requirement(rec)


# ===========================================================================
# 8. FRONTIER-EVENT ENUMERATION SHAPE (§16.9 / §19.1)
# ===========================================================================
# The {frontier_class, enumerated_options[], tradeoffs[]} contract that
# Concierge.escalate consumes. At the §16.9 Accountability Frontier the
# society's job flips from DECIDING to PRESERVING OPTIONALITY + SURFACING
# TRADEOFFS to the one human. This is the SHAPE; WHO populates it is recorded
# below.
#
# WHO POPULATES (§16.9 / §19.1): the Orchestrator COMPOSES the option set at
# the frontier from specialist inputs (Risk plan_b_ref, Insurance exclusions,
# Transport reroutes); Concierge PRESENTS it (never decides). The frontier_class
# is one of the 4 irreducible limit classes of §16.9.


class FrontierClass(str, Enum):
    """The 4 irreducible limit classes of the §16.9 Accountability Frontier."""

    PREDICTION_LIMIT = "prediction_limit"        # can react, cannot predict timing
    GROUND_TRUTH_LIMIT = "ground_truth_limit"    # no reliable real-time feed / connectivity
    HUMAN_AGENCY_LIMIT = "human_agency_limit"    # irreducibly human risk tradeoff
    ABSTRACTION_LIMIT = "abstraction_limit"      # un-transactable / un-sensible (embassy, injury)


_FRONTIER_VALUES: frozenset[str] = frozenset(f.value for f in FrontierClass)

_FRONTIER_REQUIRED: tuple[str, ...] = (
    "frontier_class",       # one of FrontierClass
    "enumerated_options",   # list of {option_id, summary, ...}
    "tradeoffs",            # list of {option_id, pros[], cons[]}
    "composed_by",          # MUST be "orchestrator" (§16.9: Orchestrator composes)
)

_OPTION_REQUIRED: tuple[str, ...] = ("option_id", "summary")
_TRADEOFF_REQUIRED: tuple[str, ...] = ("option_id", "pros", "cons")


def make_frontier_event(
    frontier_class: FrontierClass | str,
    enumerated_options: list[dict[str, Any]],
    tradeoffs: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Build a frontier-event for Concierge.escalate.

    `composed_by` is hard-coded "orchestrator": the Orchestrator composes the
    option set; Concierge only presents it (never decides) — the §16.9 boundary.
    """
    fc = frontier_class.value if isinstance(frontier_class, FrontierClass) else str(frontier_class)
    return {
        "frontier_class": fc,
        "enumerated_options": list(enumerated_options),
        "tradeoffs": list(tradeoffs),
        "composed_by": "orchestrator",
    }


def validate_frontier_event(ev: Any) -> list[str]:
    """Validate a frontier-event. Returns error strings (empty = OK)."""
    errs: list[str] = []
    if not isinstance(ev, dict):
        return ["frontier_event: not a dict"]
    for key in _FRONTIER_REQUIRED:
        if key not in ev:
            errs.append(f"frontier_event: missing required key {key!r}")
    if errs:
        return errs
    if ev["frontier_class"] not in _FRONTIER_VALUES:
        errs.append(
            f"frontier_event: frontier_class {ev['frontier_class']!r} not in "
            f"{sorted(_FRONTIER_VALUES)}"
        )
    if ev["composed_by"] != "orchestrator":
        errs.append(
            "frontier_event: composed_by must be 'orchestrator' (§16.9 — the "
            "Orchestrator composes the option set; Concierge only presents)"
        )
    opts = ev["enumerated_options"]
    tos = ev["tradeoffs"]
    if not isinstance(opts, list) or not opts:
        errs.append("frontier_event: enumerated_options must be a non-empty list")
        opts = []
    if not isinstance(tos, list):
        errs.append("frontier_event: tradeoffs must be a list")
        tos = []
    opt_ids: list[str] = []
    for i, opt in enumerate(opts):
        if not isinstance(opt, dict):
            errs.append(f"frontier_event: option[{i}] not a dict")
            continue
        for key in _OPTION_REQUIRED:
            if key not in opt:
                errs.append(f"frontier_event: option[{i}] missing {key!r}")
        if "option_id" in opt:
            opt_ids.append(opt["option_id"])
    # Every tradeoff must reference a declared option (deterministic cross-check).
    for i, to in enumerate(tos):
        if not isinstance(to, dict):
            errs.append(f"frontier_event: tradeoff[{i}] not a dict")
            continue
        for key in _TRADEOFF_REQUIRED:
            if key not in to:
                errs.append(f"frontier_event: tradeoff[{i}] missing {key!r}")
        if "option_id" in to and to["option_id"] not in opt_ids:
            errs.append(
                f"frontier_event: tradeoff[{i}] option_id {to['option_id']!r} "
                "does not match any enumerated option"
            )
    return errs


def is_valid_frontier_event(ev: Any) -> bool:
    """True iff `ev` is a well-formed frontier-event."""
    return not validate_frontier_event(ev)


# ===========================================================================
# 9. PLACE KEYSPACE (§19.1: reconcile node_id / area_id / node)
# ===========================================================================
# ONE canonical place key. The society uses single-area "cities" (city == area
# for the WA/Ethiopia corpus) and slug forms elsewhere (intent_parser
# CITY_SLUG_MAP). A canonical place key is the lower-case, hyphen-slug form of
# the place name — the SAME slug intent_parser emits ("margaret-river"). This
# reconciles node_id (graph), area_id (catalog), and node (transport) onto one
# string keyspace so every agent keys facts the same way.

_PLACE_KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def canonical_place_key(name: str) -> str:
    """
    Normalise a place name to the canonical place key (lower-case hyphen-slug).

    "Margaret River" -> "margaret-river"; "Bali" -> "bali"; "kuala lumpur" ->
    "kuala-lumpur". Deterministic; idempotent on an already-canonical key.
    Matches intent_parser's slug convention so node_id/area_id/node unify.
    """
    s = str(name).strip().lower()
    s = re.sub(r"[\s_]+", "-", s)        # spaces / underscores → hyphen
    s = re.sub(r"[^a-z0-9-]", "", s)     # drop anything not slug-safe
    s = re.sub(r"-+", "-", s).strip("-")  # collapse + trim hyphens
    return s


def is_valid_place_key(key: Any) -> bool:
    """True iff `key` is already a canonical place key (slug form)."""
    return isinstance(key, str) and bool(_PLACE_KEY_RE.match(key))


# ===========================================================================
# 10. COUNTERPARTY ↔ CATALOG IDENTITY (§19.1)
# ===========================================================================
# ONE identity for a bookable counterparty. The merchant keys line items by
# `hotel_id` (a catalog id, checkout.go). A counterparty identity wraps that
# catalog id with the counterparty TYPE so Fraud (counterparty.vet, N1) and
# Budget (dead-counterparty loss-track) speak the same id as the catalog. The
# catalog_id IS the join key; counterparty_id == catalog_id by construction
# (no second namespace).

_COUNTERPARTY_REQUIRED: tuple[str, ...] = ("counterparty_id", "catalog_id", "kind")


class CounterpartyKind(str, Enum):
    """Closed set of bookable counterparty kinds (keys the catalog/checkout)."""

    HOTEL = "hotel"          # lodging — the current catalog kind (hotel_id)
    TRANSPORT = "transport"  # carrier / transfer operator
    INSURER = "insurer"      # insurance underwriter
    ACTIVITY = "activity"    # tours / activity operator
    OTA = "ota"              # reseller / online travel agency (funds-custody, not the carrier)


_COUNTERPARTY_KIND_VALUES: frozenset[str] = frozenset(c.value for c in CounterpartyKind)


def make_counterparty(catalog_id: str, kind: CounterpartyKind | str) -> dict[str, Any]:
    """
    Build a counterparty identity. counterparty_id == catalog_id (single
    namespace — the catalog id IS the join key the merchant already uses).
    """
    k = kind.value if isinstance(kind, CounterpartyKind) else str(kind)
    return {
        "counterparty_id": str(catalog_id),
        "catalog_id": str(catalog_id),
        "kind": k,
    }


def validate_counterparty(rec: Any) -> list[str]:
    """Validate a counterparty identity. Returns error strings."""
    errs: list[str] = []
    if not isinstance(rec, dict):
        return ["counterparty: not a dict"]
    for key in _COUNTERPARTY_REQUIRED:
        if key not in rec:
            errs.append(f"counterparty: missing required key {key!r}")
    if errs:
        return errs
    if not isinstance(rec["catalog_id"], str) or not rec["catalog_id"].strip():
        errs.append("counterparty: catalog_id must be a non-empty string")
    if rec["counterparty_id"] != rec["catalog_id"]:
        errs.append(
            "counterparty: counterparty_id must equal catalog_id (single "
            "namespace — the catalog id IS the join key §19.1)"
        )
    if rec["kind"] not in _COUNTERPARTY_KIND_VALUES:
        errs.append(
            f"counterparty: kind {rec['kind']!r} not in {sorted(_COUNTERPARTY_KIND_VALUES)}"
        )
    return errs


def is_valid_counterparty(rec: Any) -> bool:
    """True iff `rec` is a well-formed counterparty identity."""
    return not validate_counterparty(rec)


# ===========================================================================
# 11. TRAVELER / PARTY IDENTITY (§19.1: per-traveler, multi-nationality N5)
# ===========================================================================
# The identity model the society reasons over. A PARTY is a set of TRAVELERS;
# each traveler carries their OWN nationality LIST (multi-nationality → N5).
# Compliance keys eVisa rules per-traveler-per-nationality; this shape makes
# that explicit (vs the old per-user-with-one-nationality assumption).

_TRAVELER_REQUIRED: tuple[str, ...] = ("traveler_id", "nationalities")
_PARTY_REQUIRED: tuple[str, ...] = ("party_id", "travelers")


def make_traveler(traveler_id: str, nationalities: list[str]) -> dict[str, Any]:
    """
    Build a traveler identity with an ORDERED list of nationalities (ISO-3166
    alpha-2/alpha-3 country codes, upper-cased). Multi-nationality → N5; the
    list preserves the traveler's stated preference order.
    """
    return {
        "traveler_id": str(traveler_id),
        "nationalities": [str(n).upper() for n in nationalities],
    }


def make_party(party_id: str, travelers: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a party (a set of travelers) — the unit a trip is booked for."""
    return {
        "party_id": str(party_id),
        "travelers": list(travelers),
    }


def validate_traveler(rec: Any) -> list[str]:
    """Validate a traveler identity. Returns error strings."""
    errs: list[str] = []
    if not isinstance(rec, dict):
        return ["traveler: not a dict"]
    for key in _TRAVELER_REQUIRED:
        if key not in rec:
            errs.append(f"traveler: missing required key {key!r}")
    if errs:
        return errs
    if not isinstance(rec["traveler_id"], str) or not rec["traveler_id"].strip():
        errs.append("traveler: traveler_id must be a non-empty string")
    nats = rec["nationalities"]
    if not isinstance(nats, list) or not nats:
        errs.append("traveler: nationalities must be a non-empty list (>=1 nationality)")
    else:
        for n in nats:
            if not isinstance(n, str) or not re.match(r"^[A-Z]{2,3}$", n):
                errs.append(
                    f"traveler: nationality {n!r} must be an ISO-3166 country code (2-3 letters)"
                )
    return errs


def validate_party(rec: Any) -> list[str]:
    """Validate a party identity (and every traveler within). Returns errors."""
    errs: list[str] = []
    if not isinstance(rec, dict):
        return ["party: not a dict"]
    for key in _PARTY_REQUIRED:
        if key not in rec:
            errs.append(f"party: missing required key {key!r}")
    if errs:
        return errs
    if not isinstance(rec["party_id"], str) or not rec["party_id"].strip():
        errs.append("party: party_id must be a non-empty string")
    travelers = rec["travelers"]
    if not isinstance(travelers, list) or not travelers:
        errs.append("party: travelers must be a non-empty list")
        travelers = []
    seen: set[str] = set()
    for i, t in enumerate(travelers):
        for e in validate_traveler(t):
            errs.append(f"party.travelers[{i}]: {e}")
        if isinstance(t, dict) and isinstance(t.get("traveler_id"), str):
            if t["traveler_id"] in seen:
                errs.append(f"party: duplicate traveler_id {t['traveler_id']!r}")
            seen.add(t["traveler_id"])
    return errs


def is_valid_traveler(rec: Any) -> bool:
    """True iff `rec` is a well-formed traveler identity."""
    return not validate_traveler(rec)


def is_valid_party(rec: Any) -> bool:
    """True iff `rec` is a well-formed party identity."""
    return not validate_party(rec)


def is_multi_national(traveler: dict[str, Any]) -> bool:
    """True iff the traveler holds >1 nationality (the N5 trigger)."""
    return is_valid_traveler(traveler) and len(traveler["nationalities"]) > 1
