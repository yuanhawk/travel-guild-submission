"""
coverage_gap.py — Insurance coverage-GAP analyzer over the USER's own policy (#45).

WHAT THIS IS (and is NOT)
-------------------------
A deterministic cross-check that takes the trip's PERILS (from Risk via the
canonical peril crosswalk) and the TRAVELER's OWN declared policy, and reports —
peril by peril — where the user is LEFT EXPOSED. It is an INFORMATIONAL gap
analysis, NOT insurance advice, NOT a quote, and it recommends NO vendor / plan /
brand. The output recommendations are GENERIC coverage-TYPE phrases drawn ONLY
from the closed ``_PERIL_TO_COVERAGE_TYPE`` table below.

This is DISTINCT from insurance_agent.py: that agent assesses a SEEDED VENDOR
catalog (it sells / proposes a premium). This module never touches the catalog,
never mints a premium, and works entirely off the user's declarations. The two
do not share state; insurance_agent.py is UNCHANGED by this feature.

NON-NEGOTIABLE INVARIANTS
-------------------------
* var-0 (DETERMINISTIC): integer cents only, first-seen ordering, no set-iteration
  on output, no wall-clock, no random, NO LLM on the gap path. ``analyze_coverage_gap``
  is a pure function of its arguments.
* HONESTY / FAIL-CONSERVATIVE:
    - Every GapReport carries a FIXED disclaimer.
    - An UNDECLARED peril → UNDETERMINED → a gap ("confirm with your insurer").
      We NEVER assume covered for a peril the user did not declare.
    - CONDITIONAL / SUB_LIMITED → a PARTIAL gap (covered only if the condition is
      met / only up to a sub-limit), never silently "covered".
    - NO vendor / plan / brand names — recommendations come ONLY from the closed
      generic-TYPE table.
* OPT-IN: this module does NOTHING unless the caller passes a ``user_policy``.
  The orchestrator only calls it when ``trip_request['user_policy']`` is present,
  so a request without it is byte-identical to today.
* IMPORT-LIGHT: depends only on ``contracts`` (Peril / is_valid_peril) +
  insurance_agent's CoverageStatus vocab. No catalog / OSM / network load.

The OPTIONAL ``parse_user_policy_text`` (free-text → structured policy) is the
ONLY place an LLM may appear, and it is FIREWALLED: gated behind DASHSCOPE_API_KEY,
off the deterministic path, and its output is re-validated FIELD-BY-FIELD through
the same structured validator before any use. The gap algorithm itself NEVER
calls it.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from core.contracts import Peril, is_valid_peril

logger = logging.getLogger(__name__)


# ===========================================================================
# Coverage-status vocabulary — REUSE insurance_agent's closed set (single source).
# ===========================================================================
# Importing keeps ONE vocabulary across both modules. If the import ever fails
# (e.g. import-graph reshuffle), mirror the SAME closed constants so the gap path
# stays self-contained and the values stay byte-identical.
try:  # pragma: no cover - exercised by the real import in the suite
    from agents.insurance_agent import CoverageStatus
except Exception:  # pragma: no cover - defensive mirror, identical literals
    class CoverageStatus:  # type: ignore[no-redef]
        COVERED = "COVERED"
        EXCLUDED = "EXCLUDED"
        SUB_LIMITED = "SUB_LIMITED"
        CONDITIONAL = "CONDITIONAL"
        UNDETERMINED = "UNDETERMINED"


# The closed set of user-declarable stances (the INPUT vocabulary), each mapping
# to exactly one CoverageStatus (the OUTPUT vocabulary). Out-of-set stance →
# UNDETERMINED (never silent COVERED).
_STANCE_TO_STATUS: dict[str, str] = {
    "covered": CoverageStatus.COVERED,
    "excluded": CoverageStatus.EXCLUDED,
    "sub_limited": CoverageStatus.SUB_LIMITED,
    "conditional": CoverageStatus.CONDITIONAL,
    "unknown": CoverageStatus.UNDETERMINED,
}


# ===========================================================================
# FIXED disclaimer — present on EVERY GapReport (HONESTY contract).
# ===========================================================================
DISCLAIMER = (
    "Informational coverage-gap analysis only — not insurance advice, not an "
    "insurer. Verify all terms with your insurer."
)

SOURCE = "coverage_gap: user-declared policy cross-check (#45)"

# Parity with EXC-WAR-2 semantics (insurance_agent.py): an armed-conflict /
# terrorism / civil-unrest exclusion is STANDARD and ABSOLUTE across the market.
_ABSOLUTE_EXCLUSION_NOTE = (
    "armed-conflict/terrorism exclusions are standard and absolute"
)
_ABSOLUTE_EXCLUSION_PERILS: frozenset[str] = frozenset({
    Peril.WAR.value,
    Peril.TERRORISM.value,
    Peril.CIVIL_UNREST.value,
})


# ===========================================================================
# Closed _PERIL_TO_COVERAGE_TYPE — GENERIC coverage-TYPE phrases ONLY.
# ===========================================================================
# Recommendations are derived ONLY from this table. Every value is a generic
# coverage-TYPE description; there are NO vendor / plan / brand names. (Enforced
# by test_coverage_gap.py scanning these values against insurance_agent's catalog
# ids/names + a brand regex.) A peril absent from this table yields a generic
# "confirm appropriate cover for <peril> with your insurer" fallback (still no brand).
_PERIL_TO_COVERAGE_TYPE: dict[str, str] = {
    Peril.MEDICAL.value:
        "higher emergency-medical & evacuation limit",
    Peril.MEDICAL_EVACUATION.value:
        "dedicated emergency medical-evacuation cover",
    Peril.TRIP_CANCELLATION.value:
        "trip-cancellation cover for pre-departure losses",
    Peril.TRIP_INTERRUPTION.value:
        "trip-interruption/curtailment cover",
    Peril.TRAVEL_DELAY.value:
        "travel-delay & missed-connection cover",
    Peril.BAGGAGE_LOSS.value:
        "baggage loss/delay cover",
    Peril.ADVENTURE_ACTIVITY.value:
        "adventure-activity / high-risk-sports add-on cover",
    Peril.NATURAL_DISASTER.value:
        "weather/natural-disaster trip-interruption cover",
    Peril.PANDEMIC.value:
        "epidemic/pandemic disruption cover",
    Peril.SUPPLIER_INSOLVENCY.value:
        "supplier-insolvency / financial-failure cover",
    Peril.PERSONAL_LIABILITY.value:
        "personal-liability / legal-expenses cover",
    Peril.WAR.value:
        "specialist conflict cover rarely available — treat as uninsurable, plan contingency",
    Peril.TERRORISM.value:
        "specialist conflict cover rarely available — treat as uninsurable, plan contingency",
    Peril.CIVIL_UNREST.value:
        "specialist conflict cover rarely available — treat as uninsurable, plan contingency",
}


def _coverage_type_for(peril: str) -> str:
    """Generic coverage-TYPE phrase for a peril (closed table; no brand names)."""
    return _PERIL_TO_COVERAGE_TYPE.get(
        peril,
        f"confirm appropriate cover for {peril.replace('_', ' ')} with your insurer",
    )


# ===========================================================================
# UserPolicy structured schema + validators (the DETERMINISTIC trust boundary).
# ===========================================================================
# Every value that enters the gap algorithm passes through validate_user_policy
# FIRST — including any LLM-parsed policy (laundered field-by-field here before use).


class PolicyValidationError(ValueError):
    """A user_policy that cannot be safely interpreted (e.g. non-USD currency)."""


def _coerce_int_or_none(value: Any) -> int | None:
    """Coerce a limit to a pure int (cents). None stays None; bad → None."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def validate_user_policy(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """
    Validate + normalize a raw user_policy dict into the structured schema.

    Returns ``(policy, warnings)``. Conservative by construction:
      - currency must be "USD" (v1) — anything else REJECTS (we never mint FX).
      - every declaration.peril_class runs through contracts.is_valid_peril; an
        out-of-set peril is DROPPED from declarations and recorded as a warning
        (it will surface as UNDETERMINED in the report, never as covered).
      - stance out of the closed set → coerced to "unknown" (→ UNDETERMINED).
      - limit_cents is required iff stance == "sub_limited"; coerced to int.
      - global_exclusions are filtered to valid perils (invalid ones warned).

    Raises PolicyValidationError ONLY for a non-recoverable input (non-USD currency
    or a non-dict policy) — those cannot be made safe by dropping a field.
    """
    warnings: list[str] = []
    if not isinstance(raw, dict):
        raise PolicyValidationError("user_policy must be a JSON object")

    currency = raw.get("currency", "USD")
    if currency != "USD":
        raise PolicyValidationError(
            f"user_policy currency {currency!r} unsupported — USD only v1 "
            "(we never mint an FX conversion)"
        )

    policy_label = str(raw.get("policy_label", "") or "")

    declarations: list[dict[str, Any]] = []
    raw_decls = raw.get("declarations") or []
    if not isinstance(raw_decls, list):
        warnings.append("declarations is not a list — ignored")
        raw_decls = []
    for d in raw_decls:
        if not isinstance(d, dict):
            warnings.append("declaration is not an object — skipped")
            continue
        peril_class = d.get("peril_class")
        if not is_valid_peril(peril_class):
            warnings.append(
                f"declaration peril_class {peril_class!r} not in canonical Peril set "
                "— dropped (will be treated as UNDETERMINED, never covered)"
            )
            continue
        stance = d.get("stance")
        if stance not in _STANCE_TO_STATUS:
            warnings.append(
                f"declaration stance {stance!r} for peril {peril_class!r} not in "
                "closed stance set — coerced to 'unknown' (UNDETERMINED)"
            )
            stance = "unknown"
        limit_cents = _coerce_int_or_none(d.get("limit_cents"))
        if stance == "sub_limited" and limit_cents is None:
            warnings.append(
                f"declaration peril {peril_class!r} is sub_limited but has no valid "
                "limit_cents — treated as sub-limited with unknown limit"
            )
        condition_text = d.get("condition_text")
        condition_text = str(condition_text) if condition_text is not None else None
        declarations.append({
            "peril_class": peril_class,
            "stance": stance,
            "limit_cents": limit_cents,
            "condition_text": condition_text,
        })

    global_exclusions: list[str] = []
    raw_excl = raw.get("global_exclusions") or []
    if not isinstance(raw_excl, list):
        warnings.append("global_exclusions is not a list — ignored")
        raw_excl = []
    for p in raw_excl:
        if not is_valid_peril(p):
            warnings.append(
                f"global_exclusion {p!r} not in canonical Peril set — dropped"
            )
            continue
        if p not in global_exclusions:  # de-dup, first-seen
            global_exclusions.append(p)

    policy = {
        "policy_label": policy_label,
        "currency": "USD",
        "declarations": declarations,
        "global_exclusions": global_exclusions,
    }
    return policy, warnings


# ===========================================================================
# Status resolution — EXCLUDE-first, then MOST-CONSERVATIVE wins (deterministic).
# ===========================================================================
# Precedence for duplicate / conflicting declarations of the SAME peril is NOT
# last-write-wins. It is a fixed conservatism ordering: an EXCLUDE anywhere wins
# outright; otherwise the most-conservative remaining stance wins. This makes the
# result independent of declaration ORDER (deterministic) AND fail-conservative.
#
#   EXCLUDED (0)  >  UNDETERMINED (1)  >  SUB_LIMITED (2)  >  CONDITIONAL (3)  >  COVERED (4)
#
# (Lower rank = more conservative = wins.)
_STATUS_CONSERVATISM_RANK: dict[str, int] = {
    CoverageStatus.EXCLUDED: 0,
    CoverageStatus.UNDETERMINED: 1,
    CoverageStatus.SUB_LIMITED: 2,
    CoverageStatus.CONDITIONAL: 3,
    CoverageStatus.COVERED: 4,
}


def _dedup_first_seen(items: list[Any]) -> list[str]:
    """De-dup a peril list preserving FIRST-SEEN order (deterministic)."""
    out: list[str] = []
    for it in items:
        s = it.value if hasattr(it, "value") else str(it)
        if s not in out:
            out.append(s)
    return out


def analyze_coverage_gap(
    *,
    peril_set: list[Any],
    user_policy: Any,
    insured_trip_cost_cents: int = 0,
) -> dict[str, Any]:
    """
    Cross-check the trip's perils against the user's declared policy → GapReport.

    Pure + deterministic (var-0): no LLM, no clock, no random, integer cents only,
    first-seen ordering throughout.

    For each trip peril (de-duped, first-seen):
      * EXCLUDED      → GAP. war/terrorism/civil_unrest also attach the absolute-
                        exclusion note (parity with EXC-WAR-2 semantics).
      * UNDETERMINED  → GAP ("unverified — confirm with your insurer"). This is the
                        status for any peril the user did NOT declare (never covered).
      * SUB_LIMITED   → PARTIAL gap. If insured_trip_cost_cents > limit_cents emit
                        shortfall_cents = insured - limit (pure int); else note the
                        limit may be adequate but must be verified.
      * CONDITIONAL   → PARTIAL gap (covered only if the condition is met — conservative).
      * COVERED       → covered[].

    Recommendations are derived from GAP perils ONLY, in first-seen order, via the
    closed generic-TYPE table (no vendor / brand names).
    """
    policy, warnings = validate_user_policy(user_policy)
    insured = int(insured_trip_cost_cents or 0)

    trip_perils = _dedup_first_seen(list(peril_set or []))

    # Build {peril: (status, limit_cents, condition_text)} from declarations +
    # global_exclusions, applying EXCLUDE-first / most-conservative precedence.
    # We keep the limit/condition associated with the WINNING status for that peril.
    resolved: dict[str, dict[str, Any]] = {}

    def _consider(peril: str, status: str, *, limit_cents: int | None = None,
                  condition_text: str | None = None) -> None:
        cur = resolved.get(peril)
        new_rank = _STATUS_CONSERVATISM_RANK.get(status, _STATUS_CONSERVATISM_RANK[CoverageStatus.UNDETERMINED])
        if cur is None or new_rank < _STATUS_CONSERVATISM_RANK[cur["status"]]:
            resolved[peril] = {
                "status": status,
                "limit_cents": limit_cents,
                "condition_text": condition_text,
            }

    # global_exclusions first (EXCLUDED is the most-conservative rank anyway).
    for p in policy["global_exclusions"]:
        _consider(p, CoverageStatus.EXCLUDED)
    for d in policy["declarations"]:
        status = _STANCE_TO_STATUS.get(d["stance"], CoverageStatus.UNDETERMINED)
        _consider(
            d["peril_class"], status,
            limit_cents=d.get("limit_cents"),
            condition_text=d.get("condition_text"),
        )

    covered: list[str] = []
    gaps: list[dict[str, Any]] = []
    gap_perils_order: list[str] = []  # first-seen gap perils → recommendations

    for peril in trip_perils:
        rec = resolved.get(peril)
        status = rec["status"] if rec else CoverageStatus.UNDETERMINED

        if status == CoverageStatus.COVERED:
            if peril not in covered:
                covered.append(peril)
            continue

        gap: dict[str, Any] = {"peril_class": peril, "status": status}

        if status == CoverageStatus.EXCLUDED:
            gap["why_not_covered"] = "policy EXCLUDES this peril — pays nothing"
            gap["risk_left"] = "full loss for this peril is uninsured"
            if peril in _ABSOLUTE_EXCLUSION_PERILS:
                gap["note"] = _ABSOLUTE_EXCLUSION_NOTE
        elif status == CoverageStatus.UNDETERMINED:
            gap["why_not_covered"] = (
                "not declared / unverified — confirm with your insurer"
            )
            gap["risk_left"] = (
                "treated as NOT covered until confirmed (never assumed covered)"
            )
        elif status == CoverageStatus.SUB_LIMITED:
            limit = rec.get("limit_cents") if rec else None
            if limit is not None and insured > int(limit):
                shortfall = insured - int(limit)
                gap["why_not_covered"] = (
                    "covered only up to a sub-limit below the insured trip cost"
                )
                gap["risk_left"] = "insured cost exceeds the sub-limit — partial cover"
                gap["shortfall_cents"] = shortfall
            else:
                gap["why_not_covered"] = (
                    "covered subject to a sub-limit — limit may be adequate, verify"
                )
                gap["risk_left"] = "partial cover up to the sub-limit; verify the limit"
        elif status == CoverageStatus.CONDITIONAL:
            gap["why_not_covered"] = (
                "covered ONLY if a stated condition is met — conditional, not assured"
            )
            gap["risk_left"] = "no payout if the condition is unmet — treat as partial"
            cond = rec.get("condition_text") if rec else None
            if cond:
                gap["condition_text"] = cond
        else:  # defensive — any unknown status is a conservative gap
            gap["why_not_covered"] = "coverage status unknown — confirm with your insurer"
            gap["risk_left"] = "treated as NOT covered until confirmed"

        gaps.append(gap)
        if peril not in gap_perils_order:
            gap_perils_order.append(peril)

    recommendations: list[str] = []
    for peril in gap_perils_order:
        phrase = _coverage_type_for(peril)
        if phrase not in recommendations:  # de-dup, first-seen
            recommendations.append(phrase)

    report: dict[str, Any] = {
        "policy_label": policy["policy_label"],
        "covered": covered,
        "gaps": gaps,
        "recommendations": recommendations,
        "insured_trip_cost_cents": insured,
        "disclaimer": DISCLAIMER,
        "source": SOURCE,
    }
    if warnings:
        report["warnings"] = warnings
    return report


# ===========================================================================
# OPTIONAL, FIREWALLED LLM free-text parse — OFF the deterministic path.
# ===========================================================================
# The gap algorithm above NEVER calls this. It is a convenience for turning a
# traveler's free-text policy summary into the structured schema. It is gated
# behind DASHSCOPE_API_KEY (mirrors insurance_agent's LLM-disabled fallback) and
# its output is LAUNDERED field-by-field through validate_user_policy before use,
# so an LLM can introduce NO value the structured validator would not accept.

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
MODEL = os.environ.get("SOCIETY_LLM_MODEL", "qwen3-max")  # cheap model for test sweeps

# PUBLIC-EXPORT NOTE: this is a simplified stand-in for the prompt actually used in
# production. The real one is iteratively tuned against a private evaluation corpus
# (disambiguation heuristics for vague/partial coverage language, sub-limit vs.
# conditional wording cues, etc.) — that tuning is the product's work, not something
# this showcase repo hands out verbatim. This version keeps the JSON contract the
# rest of the pipeline depends on (validate_user_policy below, which launders every
# field regardless) so the code still runs end-to-end, but the actual wording here
# is intentionally unrefined.
_PARSE_SYSTEM_PROMPT = (
    "You convert a traveler's free-text description of THEIR OWN existing travel-"
    "insurance policy into a strict JSON object. Schema: {\"policy_label\": str, "
    "\"currency\": \"USD\", \"declarations\": [{\"peril_class\": <one of: "
    + ", ".join(sorted(p.value for p in Peril)) + ">, \"stance\": <one of: covered, "
    "excluded, sub_limited, conditional, unknown>, \"limit_cents\": int|null, "
    "\"condition_text\": str|null}], \"global_exclusions\": [<peril_class>...]}. "
    "Use ONLY perils from the listed set. If unsure whether a peril is covered, use "
    "\"unknown\". Never name an insurer or plan. Return ONLY the JSON object."
)


def parse_user_policy_text(text: str) -> dict[str, Any] | None:
    """
    OPTIONAL free-text → structured UserPolicy via DashScope. FIREWALLED:
      - returns None if DASHSCOPE_API_KEY is unset (deterministic path unaffected),
      - returns None on ANY network / parse failure,
      - the LLM output is re-validated FIELD-BY-FIELD through validate_user_policy;
        a non-USD currency or any non-recoverable validation error → None.

    The gap algorithm never calls this. It exists only so a UI can offer free-text
    entry; the resulting structured policy is then fed to analyze_coverage_gap like
    any hand-authored one.
    """
    if not DASHSCOPE_API_KEY:
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        from utils.model_router import dashscope_chat
    except ImportError:
        try:
            from model_router import dashscope_chat  # type: ignore[no-redef]
        except ImportError:
            return None  # router unavailable — treat as LLM-off
    try:
        import json as _json
        data = dashscope_chat(
            "default",
            {"enable_thinking": False,
             "messages": [{"role": "system", "content": _PARSE_SYSTEM_PROMPT},
                          {"role": "user", "content": text}],
             "response_format": {"type": "json_object"}},
            timeout=30.0,
        )
        content = data["choices"][0]["message"]["content"]
        parsed = _json.loads(content) if isinstance(content, str) else content
        if not isinstance(parsed, dict):
            return None
        # LAUNDER through the structured validator — the LLM gets NO trust.
        policy, warnings = validate_user_policy(parsed)
        if warnings:
            logger.info("coverage_gap: LLM-parsed policy validated with warnings: %s", warnings)
        return policy
    except PolicyValidationError as e:
        logger.info("coverage_gap: LLM-parsed policy rejected by validator (%s)", e)
        return None
    except Exception as e:  # noqa: BLE001
        logger.info("coverage_gap: LLM policy parse unavailable (%s) — None", e)
        return None
