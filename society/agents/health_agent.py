"""
health_agent.py — Health VACCINATION / ENTRY-HEALTH GATE (Travel Guild, L2 / DC-health).

Design contract: the §AGENT-EXTENSION PATTERN at the bottom of
``insurance_agent.py`` (the repeatable 6-step pattern; this is the FOURTH roadmap
specialist after Insurance/Compliance/Risk), AGENTS.md (Phase-0 contracts +
variance-clamp + scope-anchor), the §7 YF-CERT HEALTH↔COMPLIANCE HANDOFF contract
in ``contracts.py`` (Health owns the JAB; Compliance owns the ENTRY DOCUMENT), and
the "Health" roadmap entry. DC-health is the FAIR-CONTRAST sibling of DC0/DC1/DC2.

WHAT THIS AGENT OWNS (one decision — a BOOKABILITY GATE + a budget correction)
-----------------------------------------------------------------------------
Given a trip (destinations, departure date), Health deterministically resolves
each destination's vaccination / prophylaxis slate (grounded in the CDC
destination guidance, e.g. the Ethiopia slate) and produces THREE plan-changing
outputs — and nothing else (it is a PLANNER, not a clinician):

  (a) BUDGET CORRECTION — the UPFRONT vaccine cost as a Budget line item via the
      Phase-0 ``line_item_assembler`` (LineItemKind.VACCINE). This is the "$1k the
      single agent forgets": a trip budget that looks sufficient but omits the
      required-vaccine spend is materially wrong. Budget vetoes it like any other
      line item; FX-normalised through the fx_provider seam for the non-USD (SGD)
      Ethiopia slate (Budget reads usd_cents, never an LLM number).

  (b) ENTRY-CERT GATE — the determinism win (Compliance-like). If a MANDATORY
      entry-health certificate (yellow-fever) cannot be completed in time
      (dosing lead-time > days-to-departure) the trip is flagged UNBOOKABLE /
      cannot_complete with an honest reason. A destination whose entry-health rule
      is UNKNOWN degrades to a CONSERVATIVE flag — NEVER a silent OK.

  (c) YF-CERT INTERLOCK — Health emits the JAB half of the §7 handoff
      (make_jab_record: cost / lead-time / validity) for the mandatory-cert
      vaccine; Compliance reads it and owns the ENTRY-DOCUMENT half
      (make_entry_doc_requirement). Health NEVER re-owns the entry document; it
      surfaces the jab. The two compose into the YF-cert handoff envelope.

SCOPE ANCHOR (AGENTS.md, critical)
----------------------------------
Health models ONLY what changes the BOOKABLE PLAN:
  vaccine COST → budget; dosing LEAD-TIME → timeline; mandatory CERT → entry gate.
It SURFACES but does NOT own traveler judgment (fitness, "is your body able"),
and PER-USER ALLERGY is explicitly OUT OF SCOPE. Altitude / medication-legality
are LIGHT SECONDARY SIGNALS only (surfaced, never a hard gate, never a number).

THE VARIANCE CLAMP (non-negotiable — money-path agent)
------------------------------------------------------
  NO-LLM-NUMBERS:      every cost (integer cents), dosing lead-time (days), and
                       cert validity is straight from the SEEDED per-destination
                       CDC slate. The LLM NEVER mints a number or a date.
  DETERMINISTIC-VERDICT: the entry-cert gate (CAN_COMPLETE / CANNOT_COMPLETE) is a
                       PURE function of (slate, departure date, today). Zero variance.
  CLOSED-SET:          requirement kinds are a closed set (REQUIRED_FOR_ENTRY /
                       CDC_RECOMMENDED / SITUATIONAL); an UNKNOWN destination
                       degrades to a CONSERVATIVE flag — NEVER a silent OK.
  LLM IS COSMETIC ONLY: the LLM only drafts a human-readable summary, validated to
                       be a derivation of the structured verdict; with the LLM
                       disabled the agent still produces the full, correct
                       HealthVerdict (the closed-set fallback).

A2A skills
----------
  ``health.assess``        — per-destination vaccine slate + the entry-cert gate +
                             vaccine cost line item + the JAB handoff half.
  ``health.explain``       — honest summary built ONLY from the structured verdict.

Runnable service: HOST / PORT env, defaults 0.0.0.0:9108 (next free port after
Insurance 9105 / Compliance 9106 / Risk 9107 — see §AGENT-EXTENSION PATTERN).

Determinism: the slate resolver + gate + line-item emission + jab handoff are pure
Python over closed sets; no LLM, no network in the verdict path. The only optional
network call is the cosmetic summary writer (DashScope), fully behind the clamp.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta
from typing import Any

import uvicorn

from agents.a2a_agent import (
    A2AAgent,
    _data_part,
    _new_artifact,
)
from core.contracts import (
    SourceTier,
    apply_ttl_downgrade,
    canonical_place_key,
    make_jab_record,
    make_money,
    make_provenance,
)
from utils.line_item_assembler import LineItemAssembler, LineItemKind

logger = logging.getLogger(__name__)


# ===========================================================================
# 0. REQUIREMENT KIND + GATE VERDICT — the closed output vocabularies.
# ===========================================================================
# All are PURE deterministic functions of (slate, departure, today). UNKNOWN is
# the conservative default for any destination with no seeded slate: it is NEVER a
# silent "no vaccines needed / OK" (the core guard — an unknown entry-health rule
# must never let a possibly-unbookable trip through as fine).

class RequirementKind:
    """The closed set of per-vaccine requirement kinds."""

    REQUIRED_FOR_ENTRY = "REQUIRED_FOR_ENTRY"  # mandatory cert (yellow-fever) — the GATE
    CDC_RECOMMENDED = "CDC_RECOMMENDED"        # CDC-recommended; cost + lead-time, not a gate
    SITUATIONAL = "SITUATIONAL"                # recommended for some travelers (light signal)


_REQUIREMENT_KINDS = frozenset({
    RequirementKind.REQUIRED_FOR_ENTRY,
    RequirementKind.CDC_RECOMMENDED,
    RequirementKind.SITUATIONAL,
})


class GateVerdict:
    """The closed set of entry-health gate outcomes for the whole trip.

    Values:
        CAN_COMPLETE            — every mandatory cert obtainable in time (or none); no flags.
        CAN_COMPLETE_WITH_FLAGS — all mandatory certs obtainable (or none), BUT one or more
                                  destinations have an UNKNOWN health slate (unverified flag).
                                  Trip is BOOKABLE (bookable=True) but consumers MUST surface
                                  the advisory; the headline verdict MUST NOT be read as
                                  unqualified approval.  (D4 contract.)
        CANNOT_COMPLETE         — a mandatory entry cert UNBOOKABLE IN TIME; trip is blocked.
    """

    CAN_COMPLETE = "can_complete"                      # no flags, all clear
    CAN_COMPLETE_WITH_FLAGS = "can_complete_with_flags"  # bookable + unverified flags (D4)
    CANNOT_COMPLETE = "cannot_complete"                # hard block on cert lead time


class GateReason:
    """Per-destination gate reasons (closed set; the LLM-free verdict vocabulary)."""

    OK_NO_MANDATORY = "OK_NO_MANDATORY_CERT"             # slate has no entry cert
    OK_CERT_LEAD_TIME_CLEARED = "OK_CERT_LEAD_TIME_CLEARED"  # mandatory cert obtainable in time
    BLOCK_CERT_LEAD_TIME = "BLOCK_CERT_INSUFFICIENT_LEAD_TIME"  # the flagship structural block
    BLOCK_UNKNOWN_SLATE = "BLOCK_UNKNOWN_SLATE_CONSERVATIVE"    # no seed → conservative flag


# ===========================================================================
# 1. SEEDED DATA — per-destination CDC vaccination / prophylaxis slate.
# ===========================================================================
# Provenance-tagged per contracts.py (§13 reproducible). Tier-1 SEEDED today; a
# live CDC Travelers' Health feed is a Tier-2 adapter swap behind fetch_slate()
# — NOT a re-architecture. ALL dosing lead-times are integer CALENDAR DAYS; all
# costs are integer cents in a named currency (NO-LLM-NUMBERS). The slates are the
# real public CDC destination guidance so the numbers are grounded + auditable.
#
# The Ethiopia slate is the benchmark anchor: yellow-fever is REQUIRED_FOR_ENTRY
# (a mandatory cert — the GATE), plus the CDC-recommended set (Hep A, Typhoid,
# Rabies, Meningococcal) and malaria prophylaxis. The summed UPFRONT cost is a
# ~4-figure SGD line item — the "$1k the single agent forgets".
#
# Costs are seeded in SGD (the Ethiopia slate is priced at a Singapore travel
# clinic — the §10.10 grounding) and FX-normalised to usd_cents through the
# fx_provider seam (Budget reads usd_cents; the agent NEVER decides a rate).
#
# Per-vaccine schema:
#   vaccine (canonical id), label, kind (RequirementKind),
#   cost_cents, currency, dosing_lead_days (days before departure the FULL course /
#   cert validity must be complete), validity_years (cert validity; None=trip-only),
#   note.

_SEED_FETCHED_AT = "2026-06-10"
_SEED_TTL_DAYS = 90
_SEED_TTL_SECONDS = _SEED_TTL_DAYS * 24 * 3600

# Safety buffer (calendar days) added on top of the published dosing lead-time
# before the gate clears a mandatory cert: clinic-availability + cert-validity slack
# so a CAN_COMPLETE is honest. Deterministic constant (NO-LLM-NUMBERS).
DEFAULT_BUFFER_DAYS = 3

# Yellow-fever cert becomes valid 10 days after vaccination (IHR rule) — the dosing
# lead-time encodes this so the gate clears only when the cert is genuinely valid by
# departure. Seeded as the published worst-case lead.


# place_key -> {country, vaccines[...]}. Keyed by canonical place key (slug).
_HEALTH_SLATES: dict[str, dict[str, Any]] = {
    # ---- Ethiopia: the CDC slate (the benchmark anchor) ----
    "ethiopia": {
        "country": "Ethiopia",
        "source": "seed:cdc.gov-travel-ethiopia-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/ethiopia",
        "medical_access_tier": "C",
        "evacuation_recommended": True,
        "vaccines": [
            {
                "vaccine": "yellow_fever",
                "label": "Yellow fever (entry certificate)",
                "kind": RequirementKind.REQUIRED_FOR_ENTRY,
                # Single travel-clinic dose; SGD ~ $150 at a Singapore clinic.
                "cost_cents": 15000, "currency": "SGD",
                # IHR: the cert is valid 10 days after the jab — the gate must clear
                # 10 days dosing lead before departure for a valid entry cert.
                "dosing_lead_days": 10,
                "validity_years": 10,   # YF cert validity (lifetime per IHR 2016; 10yr conservative)
                "note": "Mandatory yellow-fever certificate for entry; valid 10 days after vaccination.",
            },
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000, "currency": "SGD",   # 2 doses ~ S$240
                "dosing_lead_days": 14,   # first dose ≥2 weeks before travel for protection
                "validity_years": None,
                "note": "CDC-recommended; first dose at least 2 weeks before travel.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000, "currency": "SGD",    # ~ S$60
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for Ethiopia (food/water exposure).",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure, 2-dose)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000, "currency": "SGD",   # ~ S$360 (2-dose series)
                "dosing_lead_days": 21,
                "validity_years": None,
                "note": "Situational (rural / animal exposure); multi-week series.",
            },
            {
                "vaccine": "meningococcal",
                "label": "Meningococcal (ACWY)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 12000, "currency": "SGD",   # ~ S$120
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended (meningitis belt seasonality).",
            },
            {
                "vaccine": "malaria_prophylaxis",
                "label": "Malaria prophylaxis (course)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 9000, "currency": "SGD",    # ~ S$90 course
                # Prophylaxis must START before travel and CONTINUE after; the start
                # lead encodes the multi-week dosing window.
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended; prophylaxis must start before travel.",
            },
        ],
    },
    # ---- Kenya: the ET re-sequence sibling (also YF entry cert) ----
    "kenya": {
        "country": "Kenya",
        "source": "seed:cdc.gov-travel-kenya-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/kenya",
        "vaccines": [
            {
                "vaccine": "yellow_fever",
                "label": "Yellow fever (entry certificate)",
                "kind": RequirementKind.REQUIRED_FOR_ENTRY,
                "cost_cents": 15000, "currency": "SGD",
                "dosing_lead_days": 10,
                "validity_years": 10,
                "note": "Mandatory yellow-fever certificate for entry.",
            },
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000, "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000, "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended.",
            },
        ],
    },
    # ---- Thailand: a no-mandatory-cert slate (the OK control) ----
    "thailand": {
        "country": "Thailand",
        "source": "seed:cdc.gov-travel-thailand-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/thailand",
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000, "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended; no mandatory entry vaccine.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000, "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended.",
            },
        ],
    },
    # ---- Bali / Indonesia: a no-mandatory-cert slate (used by the WA/SEA corpus) ----
    "bali": {
        "country": "Indonesia (Bali)",
        "source": "seed:cdc.gov-travel-indonesia-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/indonesia",
        "medical_access_tier": "B",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000, "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended; no mandatory entry vaccine.",
            },
        ],
    },
    # ---- Stage D additions — scraped from CDC travel health pages 2026-06 ----
    "nepal": {
        "country": "Nepal",
        "source": "seed:cdc.gov-travel-nepal-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/nepal",
        "medical_access_tier": "C",
        "evacuation_recommended": True,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for unvaccinated travelers age 1+, especially those visiting rural areas or staying with relatives.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for unvaccinated travelers under 60 years old.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for most travelers, especially those staying with friends or relatives or visiting smaller cities or rural areas.",
            },
            {
                "vaccine": "malaria_prophylaxis",
                "label": "Malaria prophylaxis",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 9000, "currency": "SGD",
                "dosing_lead_days": 7,
                "validity_years": None,
                "note": "Malaria risk in Sudurpashchim and Karnali provinces below 2,000m elevation. Chemoprophylaxis recommended (atovaquone-proguanil, doxycycline, mefloquine, or tafenoquine). No risk in Kathmandu, Pokhara, or typical Himalayan treks. Primary species: P. vivax; P. falciparum <10%.",
            },
            {
                "vaccine": "japanese_encephalitis",
                "label": "Japanese Encephalitis",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 18000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for travelers spending less than 1 month doing high-risk activities (rural visits, hiking, camping, unscreened lodging). Not recommended for short-term urban travel.",
            },
            {
                "vaccine": "cholera",
                "label": "Cholera",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 4000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "May be considered; active cholera transmission is widespread in Nepal but cases remain rare in travelers.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider pre-exposure vaccination; dogs with rabies are commonly found in Nepal.",
            },
        ],
    },
    "peru": {
        "country": "Peru",
        "source": "seed:cdc.gov-travel-peru-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/peru",
        "medical_access_tier": "C",
        "evacuation_recommended": True,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000, "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "Recommended for unvaccinated travelers age 1+.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for unvaccinated travelers under 60 years old.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for most travelers, especially those staying with friends or relatives or visiting smaller cities or rural areas.",
            },
            {
                "vaccine": "yellow_fever",
                "label": "Yellow Fever",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 15000, "currency": "SGD",
                "dosing_lead_days": 10,
                "validity_years": 10,
                "note": "Recommended for areas below 2,300m elevation in specific Amazonian regions; not required for entry.",
            },
            {
                "vaccine": "malaria_prophylaxis",
                "label": "Malaria prophylaxis",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 9000, "currency": "SGD",
                "dosing_lead_days": 7,
                "validity_years": None,
                "note": "Recommended for all areas below 2,500m elevation east of the Andes. Medications: atovaquone-proguanil, doxycycline, mefloquine, or tafenoquine. Lower-risk transmission areas require only insect bite precautions.",
            },
            {
                "vaccine": "chikungunya",
                "label": "Chikungunya",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 0, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "May be considered for people planning to stay for an extended period (6 months or more).",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for travelers engaging in activities increasing animal exposure risk.",
            },
        ],
    },
    "cambodia": {
        "country": "Cambodia",
        "source": "seed:cdc.gov-travel-cambodia-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/cambodia",
        "medical_access_tier": "C",
        "evacuation_recommended": True,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000, "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "Recommended for ages 1+. Infants 6-11 months get special dosing; unvaccinated 40+ with <2 weeks before departure can receive initial dose plus immune globulin.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for travelers under 60 years; those 60+ may choose vaccination.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended especially for rural/remote areas, smaller cities, or staying with locals.",
            },
            {
                "vaccine": "malaria_prophylaxis",
                "label": "Malaria prophylaxis",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 9000, "currency": "SGD",
                "dosing_lead_days": 7,
                "validity_years": None,
                "note": "Primarily isolated to pockets in rural and forested areas. No/negligible transmission in Phnom Penh, Siem Reap, or Angkor Wat. Drug-resistant strains (chloroquine, mefloquine) documented. Recommended chemoprophylaxis for rural/forested areas: atovaquone-proguanil, doxycycline, primaquine, tafenoquine.",
            },
            {
                "vaccine": "japanese_encephalitis",
                "label": "Japanese Encephalitis",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 18000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider if moving to affected areas, staying 1+ month, or frequent rural visits/hiking/camping/unscreened accommodations.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for occupational/recreational activities with animal exposure risk or limited access to post-exposure prophylaxis.",
            },
            {
                "vaccine": "chikungunya",
                "label": "Chikungunya",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 0, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Generally not recommended; vaccination available for those considering it.",
            },
        ],
    },
    "jordan": {
        "country": "Jordan",
        "source": "seed:cdc.gov-travel-jordan-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/jordan",
        "medical_access_tier": "B",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for unvaccinated travelers one year old or older.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for unvaccinated travelers younger than 60 years old.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for most travelers, especially those visiting smaller cities or rural areas.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for occupational or recreational activities that increase risk of exposure to potentially rabid animals.",
            },
        ],
    },
    "tanzania": {
        "country": "Tanzania",
        "source": "seed:cdc.gov-travel-tanzania-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/tanzania",
        "medical_access_tier": "C",
        "evacuation_recommended": True,
        "vaccines": [
            {
                "vaccine": "yellow_fever",
                "label": "Yellow Fever (entry certificate)",
                "kind": RequirementKind.REQUIRED_FOR_ENTRY,
                "cost_cents": 15000, "currency": "SGD",
                "dosing_lead_days": 10,
                "validity_years": 10,
                "note": "Required only if arriving from countries with yellow fever transmission risk; not required for direct US travel.",
            },
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Strongly advised for unvaccinated travelers age 1+.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Suggested for unvaccinated travelers under 60.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended, especially for rural areas or staying with relatives.",
            },
            {
                "vaccine": "polio",
                "label": "Polio booster",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 3000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Adults need single booster; unvaccinated require full series.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 36000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider if activities involve animal contact risk.",
            },
            {
                "vaccine": "malaria_prophylaxis",
                "label": "Malaria prophylaxis",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 9000, "currency": "SGD",
                "dosing_lead_days": 7,
                "validity_years": None,
                "note": "All zones below 1,800m elevation. Drug-resistant strains present. Recommended medications: atovaquone-proguanil, doxycycline, mefloquine, or tafenoquine. Start multiple days before travel, continue during and after trip.",
            },
            {
                "vaccine": "cholera",
                "label": "Cholera",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 4000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "May be considered given widespread active transmission.",
            },
            {
                "vaccine": "monkeypox",
                "label": "Monkeypox (Mpox)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 0, "currency": "SGD",
                "dosing_lead_days": 42,
                "validity_years": None,
                "note": "Recommended for those engaging in high-risk activities.",
            },
        ],
    },
    "india": {
        "country": "India",
        "source": "seed:cdc.gov-travel-india-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/india",
        "medical_access_tier": "B",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000, "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "Recommended for unvaccinated travelers one year old or older.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for unvaccinated travelers under 60 years old.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000, "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "Recommended for most travelers, especially those staying with friends/relatives or visiting smaller cities/rural areas.",
            },
            {
                "vaccine": "malaria_prophylaxis",
                "label": "Malaria prophylaxis",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 9000, "currency": "SGD",
                "dosing_lead_days": 7,
                "validity_years": None,
                "note": "Malaria transmission occurs throughout India including major cities (Mumbai, New Delhi) except areas above 2,000m elevation. Medications: atovaquone-proguanil, doxycycline, mefloquine, or tafenoquine.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000, "currency": "SGD",
                "dosing_lead_days": 28,
                "validity_years": None,
                "note": "Consider for those with higher exposure risk from occupational or recreational activities with animal contact.",
            },
            {
                "vaccine": "japanese_encephalitis",
                "label": "Japanese Encephalitis",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 18000, "currency": "SGD",
                "dosing_lead_days": 28,
                "validity_years": None,
                "note": "Consider for extended stays (1+ month) or shorter stays involving rural activities, hiking, camping.",
            },
            {
                "vaccine": "chikungunya",
                "label": "Chikungunya",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 0, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for extended stays (6+ months or longer).",
            },
            {
                "vaccine": "cholera",
                "label": "Cholera",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 4000, "currency": "SGD",
                "dosing_lead_days": 7,
                "validity_years": None,
                "note": "May be considered for certain travelers in active transmission areas.",
            },
        ],
    },
    "morocco": {
        "country": "Morocco",
        "source": "seed:cdc.gov-travel-morocco-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/morocco",
        "medical_access_tier": "B",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for unvaccinated travelers one year old or older.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for unvaccinated travelers under 60 years old.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for most travelers, especially those staying with friends or relatives or visiting smaller cities or rural areas.",
            },
            {
                "vaccine": "measles_mmr",
                "label": "Measles (MMR)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0, "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "All international travelers should be fully vaccinated against measles, ideally 2+ weeks before departure.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for occupational or recreational activities that increase risk for exposure to animals; dogs infected with rabies are commonly found in Morocco.",
            },
        ],
    },
    "ecuador": {
        "country": "Ecuador",
        "source": "seed:cdc.gov-travel-ecuador-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/ecuador",
        "medical_access_tier": "C",
        "evacuation_recommended": True,
        "vaccines": [
            {
                "vaccine": "yellow_fever",
                "label": "Yellow Fever (entry certificate)",
                "kind": RequirementKind.REQUIRED_FOR_ENTRY,
                "cost_cents": 15000, "currency": "SGD",
                "dosing_lead_days": 10,
                "validity_years": 10,
                "note": "Required for travelers aged 1-60 who spent 10+ days in YF-endemic South American countries, or from DRC/Uganda with >12-hour layovers.",
            },
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000, "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "For unvaccinated persons aged 1+.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "For unvaccinated travelers under 60 years old.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for most travelers, especially those visiting rural areas.",
            },
            {
                "vaccine": "yellow_fever_recommended",
                "label": "Yellow Fever (Amazon regions)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 15000, "currency": "SGD",
                "dosing_lead_days": 10,
                "validity_years": 10,
                "note": "Recommended for areas below 2,300m elevation east of the Andes (Morona-Santiago, Napo, Orellana, Pastaza, Sucumbios, Tungurahua, Zamora-Chinchipe). Not recommended above 2,300m, Quito, Guayaquil, or Galapagos.",
            },
            {
                "vaccine": "malaria_prophylaxis",
                "label": "Malaria prophylaxis",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 9000, "currency": "SGD",
                "dosing_lead_days": 7,
                "validity_years": None,
                "note": "Required in high-risk provinces below 1,500m elevation (Cotopaxi, Esmeraldas, Morona-Santiago, Orellana, Pastaza, Sucumbios). Options: atovaquone-proguanil, doxycycline, mefloquine, tafenoquine. No risk in Quito, Guayaquil, or Galapagos Islands.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for occupational/recreational activities with animal exposure risk.",
            },
            {
                "vaccine": "chikungunya",
                "label": "Chikungunya",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 0, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Not generally recommended; vaccination available for those who wish to consider it.",
            },
        ],
    },
    "vietnam": {
        "country": "Vietnam",
        "source": "seed:cdc.gov-travel-vietnam-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/vietnam",
        "medical_access_tier": "B",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for unvaccinated travelers one year old or older.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Suggested for all unvaccinated travelers.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for most travelers, especially those staying with friends/relatives or visiting smaller cities/rural areas.",
            },
            {
                "vaccine": "malaria_prophylaxis",
                "label": "Malaria prophylaxis",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 9000, "currency": "SGD",
                "dosing_lead_days": 7,
                "validity_years": None,
                "note": "Risk in rural regions only; no prophylaxis needed in major cities (Hanoi, Ho Chi Minh City, Da Nang, Hai Phong, Nha Trang, Quy Nhon). High-risk provinces: atovaquone-proguanil, doxycycline, or tafenoquine. Other endemic areas: atovaquone-proguanil, doxycycline, mefloquine, or tafenoquine. Mekong and Red River Deltas: insect precautions only.",
            },
            {
                "vaccine": "japanese_encephalitis",
                "label": "Japanese Encephalitis",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 18000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for long-term residents, staying 1+ months in endemic areas, or high-risk activities.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "For those with occupational or recreational exposure risk.",
            },
        ],
    },
    "sri-lanka": {
        "country": "Sri Lanka",
        "source": "seed:cdc.gov-travel-sri-lanka-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/sri-lanka",
        "medical_access_tier": "B",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for unvaccinated travelers 1 year or older.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for unvaccinated travelers under 60 years old.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for most travelers, especially those staying with friends/relatives or visiting rural areas.",
            },
            # NOTE: No malaria prophylaxis — Sri Lanka has been WHO-certified
            # malaria-free since September 2016.
            {
                "vaccine": "japanese_encephalitis",
                "label": "Japanese Encephalitis",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 18000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for travelers spending 1+ month in endemic areas or doing high-risk activities like hiking/camping in rural regions.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for those with occupational or recreational activities increasing animal exposure risk.",
            },
            {
                "vaccine": "chikungunya",
                "label": "Chikungunya",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 0, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Generally not recommended but can be considered.",
            },
        ],
    },
    "philippines": {
        "country": "Philippines",
        "source": "seed:cdc.gov-travel-philippines-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/philippines",
        "medical_access_tier": "B",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for unvaccinated travelers one year old or older.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for all unvaccinated travelers.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for most travelers, especially those staying with friends or relatives.",
            },
            {
                "vaccine": "malaria_prophylaxis",
                "label": "Malaria prophylaxis",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 9000, "currency": "SGD",
                "dosing_lead_days": 7,
                "validity_years": None,
                "note": "Prophylaxis recommended for Palawan province and Mindanao Island group; no transmission in Manila and urban areas. Chloroquine-resistant strains present; use atovaquone-proguanil, doxycycline, mefloquine, or tafenoquine.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider if activities increase exposure risk to potentially rabid animals.",
            },
            {
                "vaccine": "japanese_encephalitis",
                "label": "Japanese Encephalitis",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 18000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for extended stays (1+ month) or high-risk activities in rural areas.",
            },
            {
                "vaccine": "chikungunya",
                "label": "Chikungunya",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 0, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "May be considered for extended period stays (6 months or more).",
            },
            {
                "vaccine": "cholera",
                "label": "Cholera",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 4000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Vaccination may be considered for those visiting active transmission areas.",
            },
        ],
    },
    "zimbabwe": {
        "country": "Zimbabwe",
        "source": "seed:cdc.gov-travel-zimbabwe-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/zimbabwe",
        "medical_access_tier": "C",
        "evacuation_recommended": True,
        "vaccines": [
            {
                "vaccine": "yellow_fever",
                "label": "Yellow Fever (entry certificate)",
                "kind": RequirementKind.REQUIRED_FOR_ENTRY,
                "cost_cents": 15000, "currency": "SGD",
                "dosing_lead_days": 10,
                "validity_years": 10,
                "note": "Required only if arriving from countries with YF transmission risk; not required for direct US travel.",
            },
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000, "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "Recommended for unvaccinated travelers one year old or older.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0, "currency": "SGD",
                "dosing_lead_days": 21,
                "validity_years": None,
                "note": "Recommended for all unvaccinated travelers.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000, "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "Recommended for most travelers, especially those staying with friends/relatives or visiting rural areas.",
            },
            {
                "vaccine": "malaria_prophylaxis",
                "label": "Malaria prophylaxis",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 9000, "currency": "SGD",
                "dosing_lead_days": 7,
                "validity_years": None,
                "note": "Transmission occurs country-wide. CDC recommends prescription malaria prophylaxis for all travelers. Recommended medications: atovaquone-proguanil, doxycycline, mefloquine, or tafenoquine. Begin multiple days before departure; continue during and after travel per physician instructions. Risk extends up to 1 year after return if fever develops.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000, "currency": "SGD",
                "dosing_lead_days": 21,
                "validity_years": None,
                "note": "Pre-exposure vaccination may be recommended based on risk factors; dogs infected with rabies are commonly found in Zimbabwe. Warning: counterfeit ABHAYRAB rabies vaccine circulating — verify authenticity.",
            },
            {
                "vaccine": "cholera",
                "label": "Cholera",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 4000, "currency": "SGD",
                "dosing_lead_days": 7,
                "validity_years": None,
                "note": "Vaccination may be considered for children and adults in areas of active transmission.",
            },
        ],
    },
    # ---- Tier A destinations: world-class facilities, no evacuation recommended ----
    "singapore": {
        "country": "Singapore",
        "source": "seed:cdc.gov-travel-singapore-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/singapore",
        "medical_access_tier": "A",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000, "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended; world-class medical facilities available.",
            },
        ],
    },
    "tokyo": {
        "country": "Japan (Tokyo)",
        "source": "seed:cdc.gov-travel-japan-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/japan",
        "medical_access_tier": "A",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000, "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended; excellent medical infrastructure.",
            },
        ],
    },
    "kyoto": {
        "country": "Japan (Kyoto)",
        "source": "seed:cdc.gov-travel-japan-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/japan",
        "medical_access_tier": "A",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000, "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended; excellent medical infrastructure.",
            },
        ],
    },
    "osaka": {
        "country": "Japan (Osaka)",
        "source": "seed:cdc.gov-travel-japan-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/japan",
        "medical_access_tier": "A",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000, "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended; excellent medical infrastructure.",
            },
        ],
    },
    "london": {
        "country": "United Kingdom (London)",
        "source": "seed:cdc.gov-travel-united-kingdom-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/united-kingdom",
        "medical_access_tier": "A",
        "evacuation_recommended": False,
        "vaccines": [],
    },
    "paris": {
        "country": "France (Paris)",
        "source": "seed:cdc.gov-travel-france-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/france",
        "medical_access_tier": "A",
        "evacuation_recommended": False,
        "vaccines": [],
    },
    "dubai": {
        "country": "United Arab Emirates (Dubai)",
        "source": "seed:cdc.gov-travel-united-arab-emirates-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/united-arab-emirates",
        "medical_access_tier": "A",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000, "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended; world-class medical facilities available.",
            },
        ],
    },
    "sydney": {
        "country": "Australia (Sydney)",
        "source": "seed:cdc.gov-travel-australia-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/australia",
        "medical_access_tier": "A",
        "evacuation_recommended": False,
        "vaccines": [],
    },
    "melbourne": {
        "country": "Australia (Melbourne)",
        "source": "seed:cdc.gov-travel-australia-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/australia",
        "medical_access_tier": "A",
        "evacuation_recommended": False,
        "vaccines": [],
    },
    # ---- Tier D destinations: remote/minimal facilities, evacuation required ----
    "namche bazaar": {
        "country": "Nepal (Namche Bazaar)",
        "source": "seed:cdc.gov-travel-nepal-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/nepal",
        "medical_access_tier": "D",
        "evacuation_recommended": True,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Remote high-altitude location; nearest adequate medical facility requires evacuation.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000, "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Strongly recommended for remote trekking; post-exposure care unavailable locally.",
            },
        ],
    },
    "yulara": {
        "country": "Australia (Yulara / Uluru)",
        "source": "seed:cdc.gov-travel-australia-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/australia",
        "medical_access_tier": "D",
        "evacuation_recommended": True,
        "vaccines": [],
    },
    "longyearbyen": {
        "country": "Norway (Svalbard / Longyearbyen)",
        "source": "seed:cdc.gov-travel-norway-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/norway",
        "medical_access_tier": "D",
        "evacuation_recommended": True,
        "vaccines": [],
    },
    "mcmurdo station": {
        "country": "Antarctica (McMurdo Station)",
        "source": "seed:cdc.gov-travel-antarctica-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/antarctica",
        "medical_access_tier": "D",
        "evacuation_recommended": True,
        "vaccines": [],
    },
    "lord howe island": {
        "country": "Australia (Lord Howe Island)",
        "source": "seed:cdc.gov-travel-australia-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/australia",
        "medical_access_tier": "D",
        "evacuation_recommended": True,
        "vaccines": [],
    },
    "talkeetna": {
        "country": "United States (Talkeetna, Alaska)",
        "source": "seed:cdc.gov-travel-united-states-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/united-states",
        "medical_access_tier": "D",
        "evacuation_recommended": True,
        "vaccines": [],
    },
    # ---- Verified CDC health slates (additive batch, 2026-06) ----
    # Added ADDITIVELY; no existing slate modified. Same field set as the
    # 'ethiopia' benchmark entry; kind uses RequirementKind enum members.
    "china": {
        "country": "China",
        "source": "seed:cdc.gov-travel-china-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/china",
        "medical_access_tier": "B",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers; food/water-borne risk especially outside major cities.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for unvaccinated travelers under 60 years old.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended, especially for travelers visiting smaller cities or rural areas.",
            },
            # NOTE: No malaria prophylaxis — China has been WHO-certified
            # malaria-free since June 2021.
            {
                "vaccine": "japanese_encephalitis",
                "label": "Japanese Encephalitis",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 18000,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for stays of 1+ month in rural/agricultural areas, or high-risk activities. Not recommended for short urban travel.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for occupational or recreational activities with animal exposure risk.",
            },
        ],
    },
    "mexico": {
        "country": "Mexico",
        "source": "seed:cdc.gov-travel-mexico-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/mexico",
        "medical_access_tier": "B",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers one year old or older.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for unvaccinated travelers under 60 years old.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for most travelers, especially those visiting smaller cities or rural areas.",
            },
            {
                "vaccine": "malaria_prophylaxis",
                "label": "Malaria prophylaxis",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 9000,
                "currency": "SGD",
                "dosing_lead_days": 7,
                "validity_years": None,
                "note": "Risk in rural areas of certain Pacific-coast and southern states (Chiapas, Oaxaca, Sinaloa, Nayarit, Chihuahua). No risk in major tourist cities (Mexico City, Cancun, Los Cabos, Puerto Vallarta). Predominantly P. vivax; chloroquine prophylaxis or atovaquone-proguanil.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for occupational or recreational activities with animal exposure risk.",
            },
        ],
    },
    "turkey": {
        "country": "Turkey",
        "source": "seed:cdc.gov-travel-turkey-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/turkey",
        "medical_access_tier": "B",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers one year old or older.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for unvaccinated travelers under 60 years old.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "Recommended for most travelers, especially those visiting smaller cities or rural areas.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for occupational or recreational activities with animal exposure risk.",
            },
        ],
    },
    "malaysia": {
        "country": "Malaysia",
        "source": "seed:cdc.gov-travel-malaysia-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/malaysia",
        "medical_access_tier": "B",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers; food and water precautions advised especially outside major cities.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers under 60 years old.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for most travelers, especially those eating outside tourist restaurants or visiting rural areas.",
            },
            {
                "vaccine": "malaria_prophylaxis",
                "label": "Malaria prophylaxis",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 9000,
                "currency": "SGD",
                "dosing_lead_days": 7,
                "validity_years": None,
                "note": "Malaria risk present in rural and forested areas, including parts of Sabah and Sarawak (Malaysian Borneo). No risk in Kuala Lumpur, Penang, or major cities. Recommended medications: atovaquone-proguanil, doxycycline, mefloquine, or tafenoquine.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for travelers with occupational or recreational activities that increase animal exposure risk.",
            },
            {
                "vaccine": "japanese_encephalitis",
                "label": "Japanese Encephalitis",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 18000,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for travelers spending extended time (1+ month) in rural or agricultural areas, or engaging in high-risk outdoor activities. Low risk for typical urban and resort travel.",
            },
        ],
    },
    "spain": {
        "country": "Spain",
        "source": "seed:cdc.gov-travel-spain-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/spain",
        "medical_access_tier": "A",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended as routine; excellent medical facilities available throughout Spain.",
            },
        ],
    },
    "colombia": {
        "country": "Colombia",
        "source": "seed:cdc.gov-travel-colombia-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/colombia",
        "medical_access_tier": "B",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers 1 year and older.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers under 60 years old.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for most travelers, especially those visiting smaller cities or rural areas.",
            },
            {
                "vaccine": "yellow_fever",
                "label": "Yellow Fever (Amazon/Orinoco regions)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 15000,
                "currency": "SGD",
                "dosing_lead_days": 10,
                "validity_years": 10,
                "note": "CDC-recommended for travelers visiting endemic areas (departments of Amazonas, Arauca, Casanare, Guaviare, Meta, Putumayo, Vaupes, Vichada, and parts of Antioquia, Boyaca, Cundinamarca). NOT required for entry and NOT recommended for Bogota, the Andean highlands above 2,300m, or tourist coastal areas (Cartagena, Santa Marta).",
            },
            {
                "vaccine": "malaria_prophylaxis",
                "label": "Malaria prophylaxis",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 9000,
                "currency": "SGD",
                "dosing_lead_days": 7,
                "validity_years": None,
                "note": "Malaria risk in rural areas below 1,700m elevation, particularly eastern lowlands and Pacific coast. No risk in Bogota, the Andean highlands, or Cartagena. Recommended medications: atovaquone-proguanil, doxycycline, mefloquine, or tafenoquine.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for travelers with occupational or recreational activities increasing animal exposure risk.",
            },
        ],
    },
    "argentina": {
        "country": "Argentina",
        "source": "seed:cdc.gov-travel-argentina-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/argentina",
        "medical_access_tier": "B",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers 1 year and older.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers under 60 years old.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for travelers visiting areas outside major cities or eating in non-tourist establishments.",
            },
            {
                "vaccine": "yellow_fever",
                "label": "Yellow Fever (northern jungle provinces)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 15000,
                "currency": "SGD",
                "dosing_lead_days": 10,
                "validity_years": 10,
                "note": "CDC-recommended for travelers visiting Misiones province or the forested areas of Corrientes and Formosa. NOT required for entry to Argentina and NOT recommended for Buenos Aires, Patagonia, the Pampas, or altiplano regions above 2,300m.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for travelers with recreational or occupational activities increasing animal exposure risk.",
            },
        ],
    },
    "bangladesh": {
        "country": "Bangladesh",
        "source": "seed:cdc.gov-travel-bangladesh-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/bangladesh",
        "medical_access_tier": "C",
        "evacuation_recommended": True,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers 1 year and older; food and waterborne illness risk is high.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers under 60 years old.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for most travelers, especially those visiting rural areas or eating outside tourist establishments.",
            },
            {
                "vaccine": "malaria_prophylaxis",
                "label": "Malaria prophylaxis",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 9000,
                "currency": "SGD",
                "dosing_lead_days": 7,
                "validity_years": None,
                "note": "Malaria risk primarily in the Chittagong Hill Tracts (Bandarban, Khagrachhari, Rangamati districts). No significant risk in Dhaka, Chittagong city, or the Cox's Bazar beach area. Recommended medications: atovaquone-proguanil, doxycycline, or tafenoquine.",
            },
            {
                "vaccine": "japanese_encephalitis",
                "label": "Japanese Encephalitis",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 18000,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for travelers spending extended time (1+ month) in rural or agricultural areas; low risk for short-term urban travel.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for travelers with activities that increase exposure risk to potentially rabid animals; dogs with rabies are common in Bangladesh.",
            },
            {
                "vaccine": "cholera",
                "label": "Cholera",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 4000,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "May be considered; active cholera transmission occurs in Bangladesh, particularly during flooding and monsoon seasons.",
            },
        ],
    },
    "south-africa": {
        "country": "South Africa",
        "source": "seed:cdc.gov-travel-south-africa-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/south-africa",
        "medical_access_tier": "B",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "yellow_fever",
                "label": "Yellow Fever (entry certificate)",
                "kind": RequirementKind.REQUIRED_FOR_ENTRY,
                "cost_cents": 15000,
                "currency": "SGD",
                "dosing_lead_days": 10,
                "validity_years": 10,
                "note": "Required ONLY if arriving from or transiting (12+ hrs) a country with yellow fever transmission risk; NOT required for direct travel from US/UK/EU/AU/CA. Cert valid from 10 days after vaccination.",
            },
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended, especially for travelers visiting smaller cities or rural areas.",
            },
            {
                "vaccine": "malaria_prophylaxis",
                "label": "Malaria prophylaxis",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 9000,
                "currency": "SGD",
                "dosing_lead_days": 7,
                "validity_years": None,
                "note": "Endemic in northeastern South Africa: Limpopo, Mpumalanga lowveld (Kruger area), northern KwaZulu-Natal. Not present in Cape Town, Johannesburg, or the Garden Route. Options: atovaquone-proguanil, doxycycline, mefloquine, tafenoquine.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for travelers with occupational or recreational animal exposure risk.",
            },
        ],
    },
    "south-korea": {
        "country": "South Korea",
        "source": "seed:cdc.gov-travel-south-korea-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/south-korea",
        "medical_access_tier": "A",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers; world-class medical facilities available.",
            },
            {
                "vaccine": "japanese_encephalitis",
                "label": "Japanese Encephalitis",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 18000,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for travelers spending time in rural agricultural areas, particularly during transmission season (May-October). Not recommended for short-term urban travel to Seoul.",
            },
        ],
    },
    "algeria": {
        "country": "Algeria",
        "source": "seed:cdc.gov-travel-algeria-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/algeria",
        "medical_access_tier": "C",
        "evacuation_recommended": True,
        "vaccines": [
            {
                "vaccine": "yellow_fever",
                "label": "Yellow Fever (entry certificate)",
                "kind": RequirementKind.REQUIRED_FOR_ENTRY,
                "cost_cents": 15000,
                "currency": "SGD",
                "dosing_lead_days": 10,
                "validity_years": 10,
                "note": "Required ONLY if arriving from or transiting a country with yellow fever transmission risk.",
            },
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for unvaccinated travelers under 60 years old.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended, especially for travelers visiting smaller cities or rural areas.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for occupational or recreational activities with animal exposure risk.",
            },
        ],
    },
    "pakistan": {
        "country": "Pakistan",
        "source": "seed:cdc.gov-travel-pakistan-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/pakistan",
        "medical_access_tier": "C",
        "evacuation_recommended": True,
        "vaccines": [
            {
                "vaccine": "yellow_fever",
                "label": "Yellow Fever (entry certificate)",
                "kind": RequirementKind.REQUIRED_FOR_ENTRY,
                "cost_cents": 15000,
                "currency": "SGD",
                "dosing_lead_days": 10,
                "validity_years": 10,
                "note": "Required ONLY if arriving from or transiting a country with yellow fever transmission risk.",
            },
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for unvaccinated travelers under 60 years old.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended; extensively drug-resistant (XDR) typhoid strains documented in Pakistan.",
            },
            {
                "vaccine": "polio",
                "label": "Polio booster",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 3000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "Pakistan is one of the remaining polio-endemic countries. All travelers should ensure polio vaccination is up to date; adults who completed the series need a single lifetime booster.",
            },
            {
                "vaccine": "malaria_prophylaxis",
                "label": "Malaria prophylaxis",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 9000,
                "currency": "SGD",
                "dosing_lead_days": 7,
                "validity_years": None,
                "note": "Malaria transmission throughout Pakistan below 2,000m, including some urban areas. No risk in Islamabad (but present in Rawalpindi). Options: atovaquone-proguanil, doxycycline, mefloquine, tafenoquine.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for travelers with occupational or recreational animal exposure risk.",
            },
        ],
    },
    "chile": {
        "country": "Chile",
        "source": "seed:cdc.gov-travel-chile-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/chile",
        "medical_access_tier": "B",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended, especially for travelers visiting smaller cities or rural areas.",
            },
        ],
    },
    "ivory-coast": {
        "country": "Ivory Coast (Cote d'Ivoire)",
        "source": "seed:cdc.gov-travel-ivory-coast-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/ivory-coast",
        "medical_access_tier": "C",
        "evacuation_recommended": True,
        "vaccines": [
            {
                "vaccine": "yellow_fever",
                "label": "Yellow fever (entry certificate)",
                "kind": RequirementKind.REQUIRED_FOR_ENTRY,
                "cost_cents": 15000,
                "currency": "SGD",
                "dosing_lead_days": 10,
                "validity_years": 10,
                "note": "Mandatory yellow-fever certificate required for entry for all travellers aged 1+; Cote d'Ivoire is a yellow-fever endemic country.",
            },
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travellers aged 1+.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended; food and water risk.",
            },
            {
                "vaccine": "malaria_prophylaxis",
                "label": "Malaria prophylaxis (course)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 9000,
                "currency": "SGD",
                "dosing_lead_days": 7,
                "validity_years": None,
                "note": "Malaria is holoendemic throughout Cote d'Ivoire including Abidjan; Plasmodium falciparum predominates. CDC recommends prescription prophylaxis.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travellers under 60.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000,
                "currency": "SGD",
                "dosing_lead_days": 21,
                "validity_years": None,
                "note": "Consider for those with occupational or recreational exposure risk; post-exposure care may be unavailable outside major cities.",
            },
        ],
    },
    "romania": {
        "country": "Romania",
        "source": "seed:cdc.gov-travel-romania-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/romania",
        "medical_access_tier": "B",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travellers aged 1+.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travellers under 60.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended especially for those visiting rural areas or staying with locals.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000,
                "currency": "SGD",
                "dosing_lead_days": 21,
                "validity_years": None,
                "note": "Consider for those with animal-exposure risk; Romania has documented rabies in wildlife.",
            },
        ],
    },
    "uganda": {
        "country": "Uganda",
        "source": "seed:cdc.gov-travel-uganda-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/uganda",
        "medical_access_tier": "C",
        "evacuation_recommended": True,
        "vaccines": [
            {
                "vaccine": "yellow_fever",
                "label": "Yellow fever (entry certificate)",
                "kind": RequirementKind.REQUIRED_FOR_ENTRY,
                "cost_cents": 15000,
                "currency": "SGD",
                "dosing_lead_days": 10,
                "validity_years": 10,
                "note": "Mandatory yellow-fever certificate required for all travellers aged 1+; Uganda is an endemic country.",
            },
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travellers aged 1+.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended; food and water risk.",
            },
            {
                "vaccine": "malaria_prophylaxis",
                "label": "Malaria prophylaxis (course)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 9000,
                "currency": "SGD",
                "dosing_lead_days": 7,
                "validity_years": None,
                "note": "Malaria is holoendemic throughout Uganda including Kampala; Plasmodium falciparum predominates. CDC recommends prescription prophylaxis.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travellers under 60.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000,
                "currency": "SGD",
                "dosing_lead_days": 21,
                "validity_years": None,
                "note": "Consider for those with animal-exposure risk; post-exposure care may be unavailable outside Kampala.",
            },
            {
                "vaccine": "meningococcal",
                "label": "Meningococcal (ACWY)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 12000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended; Uganda is in the meningitis belt.",
            },
        ],
    },
    "uzbekistan": {
        "country": "Uzbekistan",
        "source": "seed:cdc.gov-travel-uzbekistan-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/uzbekistan",
        "medical_access_tier": "B",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travellers aged 1+; food and water risk.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended, especially for those visiting rural areas or eating outside tourist facilities.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travellers under 60.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000,
                "currency": "SGD",
                "dosing_lead_days": 21,
                "validity_years": None,
                "note": "Consider for those with animal-exposure risk in rural areas.",
            },
        ],
    },
    "iran": {
        "country": "Iran",
        "source": "seed:cdc.gov-travel-iran-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/iran",
        "medical_access_tier": "B",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travellers aged 1+.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended, especially for those visiting rural areas or smaller cities.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travellers under 60.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000,
                "currency": "SGD",
                "dosing_lead_days": 21,
                "validity_years": None,
                "note": "Consider for those with animal-exposure risk, particularly in rural areas.",
            },
        ],
    },
    "angola": {
        "country": "Angola",
        "source": "seed:cdc.gov-travel-angola-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/angola",
        "medical_access_tier": "C",
        "evacuation_recommended": True,
        "vaccines": [
            {
                "vaccine": "yellow_fever",
                "label": "Yellow fever (entry certificate)",
                "kind": RequirementKind.REQUIRED_FOR_ENTRY,
                "cost_cents": 15000,
                "currency": "SGD",
                "dosing_lead_days": 10,
                "validity_years": 10,
                "note": "Mandatory YF cert; Angola YF-endemic, all >=9mo; valid 10d after vax.",
            },
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended; food/water risk.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended; food/water risk.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for unvaccinated under 60.",
            },
            {
                "vaccine": "malaria_prophylaxis",
                "label": "Malaria prophylaxis",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 9000,
                "currency": "SGD",
                "dosing_lead_days": 7,
                "validity_years": None,
                "note": "Throughout Angola incl urban; drug-resistant P. falciparum; start before travel.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for animal-exposure activities.",
            },
        ],
    },
    "kazakhstan": {
        "country": "Kazakhstan",
        "source": "seed:cdc.gov-travel-kazakhstan-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/kazakhstan",
        "medical_access_tier": "B",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended; food/water risk.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for unvaccinated under 60.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended, esp rural.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for animal-exposure activities.",
            },
        ],
    },
    "ghana": {
        "country": "Ghana",
        "source": "seed:cdc.gov-travel-ghana-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/ghana",
        "medical_access_tier": "C",
        "evacuation_recommended": True,
        "vaccines": [
            {
                "vaccine": "yellow_fever",
                "label": "Yellow fever (entry certificate)",
                "kind": RequirementKind.REQUIRED_FOR_ENTRY,
                "cost_cents": 15000,
                "currency": "SGD",
                "dosing_lead_days": 10,
                "validity_years": 10,
                "note": "Mandatory YF cert all >=9mo; Ghana YF-endemic; valid 10d after vax.",
            },
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended; food/water risk.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended; food/water risk.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for unvaccinated under 60.",
            },
            {
                "vaccine": "malaria_prophylaxis",
                "label": "Malaria prophylaxis",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 9000,
                "currency": "SGD",
                "dosing_lead_days": 7,
                "validity_years": None,
                "note": "Throughout Ghana; chloroquine-resistant P. falciparum; start before travel.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for animal-exposure activities.",
            },
        ],
    },
    "egypt": {
        "country": "Egypt",
        "source": "seed:cdc.gov-travel-egypt-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/egypt",
        "medical_access_tier": "B",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended; food/water risk.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Recommended for unvaccinated under 60.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended, esp rural.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for animal-exposure activities.",
            },
        ],
    },
    "cameroon": {
        "country": "Cameroon",
        "source": "seed:cdc.gov-travel-cameroon-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/cameroon",
        "medical_access_tier": "C",
        "evacuation_recommended": True,
        "vaccines": [
            {
                "vaccine": "yellow_fever",
                "label": "Yellow fever (entry certificate)",
                "kind": RequirementKind.REQUIRED_FOR_ENTRY,
                "cost_cents": 15000,
                "currency": "SGD",
                "dosing_lead_days": 10,
                "validity_years": 10,
                "note": "Mandatory yellow-fever certificate for all travelers entering Cameroon (endemic zone, enforced at entry); valid 10 days after vaccination.",
            },
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers; food and water risk throughout.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended; food and water exposure risk.",
            },
            {
                "vaccine": "malaria_prophylaxis",
                "label": "Malaria prophylaxis (course)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 9000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "Malaria hyperendemic throughout Cameroon; prophylaxis strongly recommended for all travelers (atovaquone-proguanil, doxycycline, mefloquine, or tafenoquine).",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers under 60.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for travelers with occupational or recreational animal exposure risk.",
            },
        ],
    },
    "cuba": {
        "country": "Cuba",
        "source": "seed:cdc.gov-travel-cuba-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/cuba",
        "medical_access_tier": "B",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers; food and water exposure risk.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended, especially outside resort areas or when staying with relatives.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers under 60.",
            },
        ],
    },
    "guatemala": {
        "country": "Guatemala",
        "source": "seed:cdc.gov-travel-guatemala-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/guatemala",
        "medical_access_tier": "C",
        "evacuation_recommended": True,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers; food and water exposure risk.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended, especially outside tourist areas or when staying with relatives.",
            },
            {
                "vaccine": "malaria_prophylaxis",
                "label": "Malaria prophylaxis (course)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 9000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "Malaria risk in rural lowland areas below 1,500m; no risk in Guatemala City or Antigua. Chloroquine-sensitive P. vivax predominates (atovaquone-proguanil, chloroquine, doxycycline, mefloquine, or tafenoquine).",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers under 60.",
            },
            {
                "vaccine": "rabies_preexposure",
                "label": "Rabies (pre-exposure)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 36000,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "Consider for travelers with animal exposure risk in rural areas.",
            },
        ],
    },
    "taiwan": {
        "country": "Taiwan",
        "source": "seed:cdc.gov-travel-taiwan-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/taiwan",
        "medical_access_tier": "A",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers; excellent medical infrastructure available.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers under 60.",
            },
        ],
    },
    "dominican-republic": {
        "country": "Dominican Republic",
        "source": "seed:cdc.gov-travel-dominican-republic-2026",
        "source_url": "https://wwwnc.cdc.gov/travel/destinations/traveler/none/dominican-republic",
        "medical_access_tier": "B",
        "evacuation_recommended": False,
        "vaccines": [
            {
                "vaccine": "hepatitis_a",
                "label": "Hepatitis A (2-dose series)",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 24000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers; food and water exposure risk outside resort areas.",
            },
            {
                "vaccine": "typhoid",
                "label": "Typhoid",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 6000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "CDC-recommended, especially when visiting outside resort areas or staying with relatives.",
            },
            {
                "vaccine": "hepatitis_b",
                "label": "Hepatitis B",
                "kind": RequirementKind.CDC_RECOMMENDED,
                "cost_cents": 0,
                "currency": "SGD",
                "dosing_lead_days": 30,
                "validity_years": None,
                "note": "CDC-recommended for unvaccinated travelers under 60.",
            },
            {
                "vaccine": "malaria_prophylaxis",
                "label": "Malaria prophylaxis (course)",
                "kind": RequirementKind.SITUATIONAL,
                "cost_cents": 9000,
                "currency": "SGD",
                "dosing_lead_days": 14,
                "validity_years": None,
                "note": "P. falciparum (chloroquine-sensitive). CDC recommends chemoprophylaxis for several provinces INCLUDING La Altagracia (Punta Cana), Santo Domingo, Distrito Nacional, Azua, San Juan and Elias Pina; risk is highest near the Haiti border. Consider prophylaxis even for resort stays in affected provinces.",
            },
        ],
    },
}

# Light SECONDARY SIGNALS (surfaced, NOT a gate, NO numbers). Altitude / med-legality
# only — per the scope anchor these never block and never carry a decision number.
_SECONDARY_SIGNALS: dict[str, list[dict[str, str]]] = {
    "ethiopia": [
        {"signal": "altitude",
         "note": "Addis Ababa sits ~2,355 m — mild altitude; allow acclimatisation. "
                 "SURFACED only (traveler-fitness judgment is out of Health's scope)."},
        {"signal": "medication_legality",
         "note": "Some common medications are restricted; verify before travel. "
                 "Light signal only."},
    ],
}


# Country-level fallback slate lookup: ISO-3166 alpha-2 and lowercase country names
# → best-match slate key. Enables health gate to engage for expansion cities whose
# exact city slug isn't seeded yet (returns the country-level slate rather than
# BLOCK_UNKNOWN_SLATE). Deterministic; data-driven from the seeded slate set.
_COUNTRY_ISO2_TO_SLATE_KEY: dict[str, str] = {
    "ET": "ethiopia",
    "KE": "kenya",
    "TH": "thailand",
    "ID": "bali",      # Indonesia → Bali slate (the seeded Indonesia slate)
    "IN": "india",
    "NP": "nepal",
    "PH": "philippines",
    "VN": "vietnam",
    "KH": "cambodia",
    "MA": "morocco",
    "JO": "jordan",
    "TZ": "tanzania",
    # PG/MW/MZ removed: borrowing a *different* country's CDC slate fabricates a
    # confident vaccine verdict — honest behavior is to fall through to the
    # unverified FLAG. (Same-country mappings like ID→bali, ZW→zimbabwe stay.)
    "PE": "peru",
    "EC": "ecuador",
    "ZW": "zimbabwe",
    "LK": "sri-lanka",
    "SG": "singapore",
    "JP": "tokyo",
    "GB": "london",
    "FR": "paris",
    "AE": "dubai",
    "AU": "sydney",
    # ---- Verified additive batch (2026-06): each maps to its OWN seeded slate ----
    "CN": "china",
    "MX": "mexico",
    "TR": "turkey",
    "MY": "malaysia",
    "ES": "spain",
    "CO": "colombia",
    "AR": "argentina",
    "BD": "bangladesh",
    "ZA": "south-africa",
    "KR": "south-korea",
    "DZ": "algeria",
    "PK": "pakistan",
    "CL": "chile",
    "CI": "ivory-coast",
    "RO": "romania",
    "UG": "uganda",
    "UZ": "uzbekistan",
    "IR": "iran",
    "AO": "angola",
    "KZ": "kazakhstan",
    "GH": "ghana",
    "EG": "egypt",
    "CM": "cameroon",
    "CU": "cuba",
    "GT": "guatemala",
    "TW": "taiwan",
    "DO": "dominican-republic",
}

_COUNTRY_NAME_TO_SLATE_KEY: dict[str, str] = {
    "ethiopia": "ethiopia",
    "kenya": "kenya",
    "thailand": "thailand",
    "indonesia": "bali",
    "india": "india",
    "nepal": "nepal",
    "philippines": "philippines",
    "vietnam": "vietnam",
    "cambodia": "cambodia",
    "morocco": "morocco",
    "jordan": "jordan",
    "tanzania": "tanzania",
    "peru": "peru",
    "ecuador": "ecuador",
    "zimbabwe": "zimbabwe",
    "sri lanka": "sri-lanka",
    "singapore": "singapore",
    "japan": "tokyo",
    "united kingdom": "london",
    "england": "london",
    "scotland": "london",
    "wales": "london",
    "northern ireland": "london",
    "france": "paris",
    "united arab emirates": "dubai",
    "australia": "sydney",
    # ---- Verified additive batch (2026-06): lowercased country name -> slate key ----
    "china": "china",
    "mexico": "mexico",
    "turkey": "turkey",
    "malaysia": "malaysia",
    "spain": "spain",
    "colombia": "colombia",
    "argentina": "argentina",
    "bangladesh": "bangladesh",
    "south africa": "south-africa",
    "south korea": "south-korea",
    "algeria": "algeria",
    "pakistan": "pakistan",
    "chile": "chile",
    "ivory coast": "ivory-coast",
    "cote d'ivoire": "ivory-coast",
    "romania": "romania",
    "uganda": "uganda",
    "uzbekistan": "uzbekistan",
    "iran": "iran",
    "angola": "angola",
    "kazakhstan": "kazakhstan",
    "ghana": "ghana",
    "egypt": "egypt",
    "cameroon": "cameroon",
    "cuba": "cuba",
    "guatemala": "guatemala",
    "taiwan": "taiwan",
    "dominican republic": "dominican-republic",
}


def _build_city_to_slate_key() -> dict[str, str]:
    """
    Build a deterministic city (lowercase canonical) → slate key map from the catalog,
    using the country-name fallback tables above. Loaded once at module init.

    Enables health gate to engage for expansion cities (e.g. 'marrakech' → 'morocco')
    that aren't individually seeded in _HEALTH_SLATES.
    """
    catalog_path = os.path.join(os.path.dirname(__file__), "..", "..", "ucp-merchant", "catalog.json")
    result: dict[str, str] = {}
    try:
        with open(catalog_path, encoding="utf-8") as f:
            catalog = json.load(f)
    except (OSError, json.JSONDecodeError):
        return result
    for hotel in catalog:
        city = canonical_place_key(str(hotel.get("city", "")).strip())
        if not city:
            continue
        if city in _HEALTH_SLATES:
            continue  # already has a direct slate — no need to map
        country_name = str(hotel.get("country", "")).strip().lower()
        slate_key = _COUNTRY_NAME_TO_SLATE_KEY.get(country_name)
        if slate_key:
            result.setdefault(city, slate_key)
    return result


_CITY_TO_SLATE_KEY: dict[str, str] = _build_city_to_slate_key()


# ===========================================================================
# 2. DATE MATH — calendar-day arithmetic (pure, deterministic, NO LLM).
# ===========================================================================

def _parse_date(s: str) -> date:
    """Parse an ISO YYYY-MM-DD date. Raises ValueError on bad input."""
    return date.fromisoformat(s)


def days_between(start: date, end: date) -> int:
    """
    Calendar days strictly available from `start` (exclusive) to `end` (inclusive).
    Returns 0 if end <= start. Pure + deterministic.
    """
    if end <= start:
        return 0
    return (end - start).days


# ===========================================================================
# 3. SLATE RESOLVER — pure function over the seeded closed set.
# ===========================================================================

def fetch_slate(
    place_key: str,
    *,
    simulate_unreachable: bool = False,
) -> tuple[dict[str, Any] | None, str]:
    """
    Resolve the CDC slate for a destination via the (deferred) CDC source adapter.

    Lookup order (deterministic):
      1. Exact city-level key in _HEALTH_SLATES (primary).
      2. Country-name fallback via _COUNTRY_NAME_TO_SLATE_KEY (expansion cities).
      3. ISO-3166 alpha-2 fallback via _COUNTRY_ISO2_TO_SLATE_KEY (dest_country leg field).

    Returns (slate_or_None, source_freshness). `simulate_unreachable=True` exercises
    the Tier-3 exhausted path → returns the seeded slate tagged SEEDED_FALLBACK
    (terms NOT fabricated), or None if no seed for the destination.
    """
    key = canonical_place_key(place_key or "")
    freshness = "SEEDED_FALLBACK" if simulate_unreachable else "SEEDED"

    # 1. City-level exact lookup
    slate = _HEALTH_SLATES.get(key)
    if slate is None:
        # 2. Catalog-driven city→country-slate lookup (expansion cities like 'marrakech' → 'morocco')
        mapped_city = _CITY_TO_SLATE_KEY.get(key)
        if mapped_city:
            slate = _HEALTH_SLATES.get(mapped_city)
            if slate is not None:
                logger.info("health: slate for %r resolved via city-catalog fallback → %r", key, mapped_city)
    if slate is None:
        # 3. Country-name / ISO2 passed directly as place_key
        mapped = _COUNTRY_NAME_TO_SLATE_KEY.get(key)
        if mapped:
            slate = _HEALTH_SLATES.get(mapped)
            if slate is not None:
                logger.info("health: slate for %r resolved via country-name fallback → %r", key, mapped)
    if slate is None:
        # 4. ISO-3166 alpha-2 fallback
        mapped_iso = _COUNTRY_ISO2_TO_SLATE_KEY.get(key.upper())
        if mapped_iso:
            slate = _HEALTH_SLATES.get(mapped_iso)
            if slate is not None:
                logger.info("health: slate for %r resolved via ISO2 fallback → %r", key, mapped_iso)

    if simulate_unreachable and slate is not None:
        logger.warning(
            "health: CDC source unreachable (Tier-3 exhausted) → SEEDED_FALLBACK "
            "for %s (slate NOT fabricated)", key,
        )
    return slate, freshness


def _slate_provenance(slate: dict[str, Any], tier: str = SourceTier.SEEDED.value) -> dict[str, Any]:
    """The canonical provenance envelope for a seeded CDC slate."""
    return make_provenance(
        source=slate["source"],
        fetched_at=_SEED_FETCHED_AT,
        tier=tier,
        source_url=slate["source_url"],
        ttl=_SEED_TTL_SECONDS,
    )


# ===========================================================================
# 4. VACCINE COST — UPFRONT cost as a Budget line item (the $1k correction).
# ===========================================================================

def _vaccine_money(cost_cents: int, currency: str, source: str) -> dict[str, Any] | None:
    """
    Build the canonical money tag for a vaccine cost, FX-normalised to usd_cents.

    USD → trivial; non-USD (the SGD slate) → through the fx_provider seam (Budget
    reads usd_cents, never an LLM number, never an agent-decided rate). An unknown
    currency → honest decline (None) — never a silent treat-as-USD.
    """
    cur = (currency or "USD").upper()
    if cost_cents <= 0:
        return None
    if cur == "USD":
        return make_money(
            native_cents=cost_cents, currency="USD", usd_cents=cost_cents,
            fx_rate=1.0, fx_source=source, fetched_at=_SEED_FETCHED_AT,
        )
    from utils.fx_provider import normalize
    fxr = normalize(native_cents=cost_cents, currency=cur)
    if fxr.exhausted:
        logger.warning(
            "health: vaccine cost currency %r unknown to FX seam — cost omitted "
            "(honest decline; gate verdict unaffected)", cur,
        )
        return None
    return fxr.to_money()


def vaccine_cost_line_item(
    verdict: dict[str, Any],
    leg_key: str,
    *,
    source: str = "health",
    _already_costed: set[str] | None = None,
) -> dict[str, Any] | None:
    """
    Emit the UPFRONT total vaccine cost for a destination as a single Budget line
    item (LineItemKind.VACCINE) via the Phase-0 LineItemAssembler — keyed/
    idempotent by (source, "vaccine", leg) so re-emission REPLACES (never double-
    charges). Budget then vetoes it like any other line item (Health proposes,
    Budget enforces).

    The cost is the SUM of every vaccine's usd_cents in the destination's slate
    (the materially-wrong-by-$1k correction the single agent forgets). Returns the
    single assembled line-item dict, or None for a zero-cost / unknown slate.

    WARNING (Finding #52): this standalone function does NOT dedup cross-leg vaccines.
    Calling it once per destination on a multi-leg trip will DOUBLE-CHARGE any vaccine
    shared across legs (the exact bug assess() was written to prevent).  For all live
    billing paths use assess() directly — it performs the incremental-dedup billing
    internally.  Only call this function when you are certain the `verdict` comes from
    a single-destination assess_destination() call and you manage the already-costed
    set externally (pass it via `_already_costed` to skip already-seen vaccine ids).
    """
    # If an external dedup set was provided, compute only the incremental cost.
    if _already_costed is not None:
        incremental = 0
        for vrec in verdict.get("vaccines", []):
            vax_id = vrec.get("vaccine", "")
            if vax_id not in _already_costed:
                _already_costed.add(vax_id)
                incremental += int(vrec.get("usd_cents") or 0)
        total = incremental
    else:
        total = int(verdict.get("total_vaccine_cost_usd_cents", 0) or 0)
    if total <= 0:
        return None
    money = make_money(
        native_cents=total, currency="USD", usd_cents=total,
        fx_rate=1.0, fx_source="seed:health-aggregate-usd",
        fetched_at=_SEED_FETCHED_AT,
    )
    a = LineItemAssembler()
    a.add_fee(
        source=source,
        kind=LineItemKind.VACCINE,
        leg=leg_key,
        money=money,
        description=(
            f"Required + recommended vaccines / prophylaxis for "
            f"{verdict.get('country', verdict.get('place_key'))} (CDC slate, upfront)"
        ),
    )
    return a.assemble()[0]


# ===========================================================================
# 5. THE ENTRY-CERT GATE — the heart of the agent (pure, variance-0).
# ===========================================================================

def assess_destination(
    *,
    place_key: str,
    departure_date: str,
    today: str,
    buffer_days: int = DEFAULT_BUFFER_DAYS,
    simulate_source_unreachable: bool = False,
    held_vaccine_certs: "frozenset[str] | set[str] | None" = None,
) -> dict[str, Any]:
    """
    Assess ONE destination: resolve the CDC slate, run the mandatory-cert gate,
    sum the upfront vaccine cost, and emit the JAB handoff half for any
    mandatory-cert vaccine.

    PURE + DETERMINISTIC. Returns a per-destination verdict record:
        {place_key, country, gate_reason, can_complete (bool),
         vaccines[{vaccine, label, kind, cost_cents, currency, usd_cents,
                   dosing_lead_days, validity_years, required_lead_days,
                   available_days, obtainable_in_time, earliest_feasible_departure}],
         mandatory_certs[], total_vaccine_cost_usd_cents, jab_records[],
         secondary_signals[], provenance, source_freshness}

    The flagship check: for any REQUIRED_FOR_ENTRY vaccine, if the days available
    before departure < (dosing lead-time + buffer) → CANNOT_COMPLETE with
    BLOCK_CERT_INSUFFICIENT_LEAD_TIME and the earliest feasible departure.

    An UNKNOWN slate (no seed) → conservative CANNOT_COMPLETE
    (BLOCK_UNKNOWN_SLATE) — NEVER a silent OK.
    """
    key = canonical_place_key(place_key or "")
    try:
        dep = _parse_date(departure_date)
        now = _parse_date(today)
    except (ValueError, TypeError):
        # Malformed date(s) → cannot verify cert lead time → conservative CANNOT_COMPLETE
        # (robustness: never crash on bad external input, never silent OK).
        return {
            "place_key": key, "departure_date": departure_date, "today": today,
            "buffer_days": int(buffer_days), "available_days": None, "source_freshness": "SEEDED",
            "country": key, "can_complete": False, "gate_reason": GateReason.BLOCK_UNKNOWN_SLATE,
            "vaccines": [], "mandatory_certs": [], "total_vaccine_cost_usd_cents": 0,
            "jab_records": [], "secondary_signals": [], "earliest_feasible_departure": None,
            "provenance": make_provenance(
                source="seed:health-invalid-date", fetched_at=_SEED_FETCHED_AT,
                tier=SourceTier.SEEDED.value, source_url=None, ttl=_SEED_TTL_SECONDS,
            ),
            "detail": (f"invalid date(s) departure={departure_date!r} today={today!r} — "
                       f"cannot verify vaccination/cert lead time; blocked conservatively"),
        }
    available = days_between(now, dep)
    slate, freshness = fetch_slate(key, simulate_unreachable=simulate_source_unreachable)

    base: dict[str, Any] = {
        "place_key": key,
        "departure_date": departure_date,
        "today": today,
        "buffer_days": int(buffer_days),
        "available_days": available,
        "source_freshness": freshness,
    }

    # --- UNKNOWN slate → conservative FLAG advisory (never silent OK, never hard-block).
    #     can_complete=None means "unverified": trip still proceeds, advisory is surfaced.
    if slate is None:
        base.update({
            "country": key,
            "can_complete": None,
            "unverified_flag": True,
            "flag_advisory": (
                f"Health/vaccination requirements unverified for {key!r} — "
                f"check CDC/WHO before travel."
            ),
            "gate_reason": GateReason.BLOCK_UNKNOWN_SLATE,
            "vaccines": [],
            "mandatory_certs": [],
            "total_vaccine_cost_usd_cents": 0,
            "jab_records": [],
            "secondary_signals": [],
            "earliest_feasible_departure": None,
            "provenance": make_provenance(
                source="seed:health-no-slate",
                fetched_at=_SEED_FETCHED_AT,
                tier=SourceTier.SEEDED.value,
                source_url=None,
                ttl=_SEED_TTL_SECONDS,
            ),
        })
        return base

    tier = SourceTier.SEEDED.value if freshness == "SEEDED" else SourceTier.EXHAUSTED.value
    prov = _slate_provenance(slate, tier=tier)

    vaccines: list[dict[str, Any]] = []
    held = held_vaccine_certs or frozenset()
    total_usd = 0          # FULL slate sum (frozen I1 contract — NOT changed by held certs)
    billed_usd = 0         # #70: held-adjusted billed sum (a held cert costs 0 — already obtained)
    mandatory_certs: list[str] = []
    held_certs: list[str] = []   # #70: REQUIRED_FOR_ENTRY vaccines the traveler already holds
    jab_records: list[dict[str, Any]] = []
    blocking_lead_dates: list[date] = []
    can_complete = True

    for vrow in slate["vaccines"]:
        lead = int(vrow["dosing_lead_days"])
        required = lead + int(buffer_days)
        money = _vaccine_money(int(vrow["cost_cents"] or 0), vrow["currency"], slate["source"])
        usd = int(money["usd_cents"]) if money else 0
        total_usd += usd

        is_mandatory = vrow["kind"] == RequirementKind.REQUIRED_FOR_ENTRY
        is_held = vrow["vaccine"] in held   # #70: traveler already holds this cert
        billed_usd += 0 if is_held else usd
        obtainable = available >= required
        earliest = (now + timedelta(days=required)).isoformat()

        vrec = {
            "vaccine": vrow["vaccine"],
            "label": vrow["label"],
            "kind": vrow["kind"],
            "cost_cents": int(vrow["cost_cents"]),
            "currency": vrow["currency"],
            "usd_cents": usd,
            "held": is_held,                              # #70
            "billed_usd_cents": 0 if is_held else usd,    # #70: held -> not billed
            "money": money,
            "dosing_lead_days": lead,
            "required_lead_days": required,
            "available_days": available,
            "obtainable_in_time": obtainable,
            "validity_years": vrow["validity_years"],
            "earliest_feasible_departure": earliest,
            "note": vrow.get("note", ""),
        }
        vaccines.append(vrec)

        if is_mandatory:
            # #70: a HELD entry-required cert is ALREADY satisfied — no cost, no lead-time
            # gate, never a block. Record it as held (not as a cert-to-obtain).
            if is_held:
                held_certs.append(vrow["vaccine"])
            else:
                mandatory_certs.append(vrow["vaccine"])
            # Health's half of the §7 YF-cert handoff: the JAB (cost/lead-time/validity).
            # Compliance reads this and owns the entry-DOCUMENT half.
            # Finding #43 (D4): emit jab_record for EVERY REQUIRED_FOR_ENTRY vaccine
            # regardless of cost — the handoff is about cert/lead-time/validity, not price.
            # A HELD cert is emitted zero-cost + tagged held.
            jab_cost = money if (money is not None and not is_held) else make_money(
                native_cents=0, currency="USD", usd_cents=0,
                fx_rate=1.0, fx_source=slate["source"], fetched_at=_SEED_FETCHED_AT,
            )
            jr = make_jab_record(
                vaccine=vrow["vaccine"],
                cost=jab_cost,
                lead_time_days=lead,
                validity_years=vrow["validity_years"],
            )
            if is_held and isinstance(jr, dict):
                jr["held"] = True
            jab_records.append(jr)
            # The GATE: a mandatory cert that can't be completed in time blocks the trip —
            # UNLESS already held (#70: a held cert satisfies the gate regardless of lead time).
            if not is_held and not obtainable:
                can_complete = False
                blocking_lead_dates.append(now + timedelta(days=required))

    if not can_complete:
        gate_reason = GateReason.BLOCK_CERT_LEAD_TIME
        earliest_feasible = max(blocking_lead_dates).isoformat() if blocking_lead_dates else None
    elif mandatory_certs:
        gate_reason = GateReason.OK_CERT_LEAD_TIME_CLEARED
        earliest_feasible = None
    else:
        gate_reason = GateReason.OK_NO_MANDATORY
        earliest_feasible = None

    # ---- #39 TIER SPLIT: mandatory (budget) vs optional (opt-in estimate) -------
    # MANDATORY tier = REQUIRED_FOR_ENTRY only — the budget-affecting spend (the
    # cert vaccines a trip MUST carry). OPTIONAL tier = CDC_RECOMMENDED / SITUATIONAL
    # jabs — a SEPARATE, opt-in estimate that is NEVER folded into the trip budget.
    # NOTE: `total_vaccine_cost_usd_cents` (and the Budget line item) remains the
    # FULL slate sum — that contract is frozen (I1). The tier fields are ADDITIVE:
    # they expose the breakdown without changing any existing value. Iteration is
    # over the already-built, order-preserving `vaccines` list → deterministic.
    mandatory_cost_usd = sum(
        int(v["usd_cents"]) for v in vaccines
        if v["kind"] == RequirementKind.REQUIRED_FOR_ENTRY and not v.get("held")
    )
    optional_items = [
        {
            "vaccine": v["vaccine"],
            "label": v["label"],
            "kind": v["kind"],
            "usd_cents": int(v["usd_cents"]),
        }
        for v in vaccines
        if v["kind"] in (RequirementKind.CDC_RECOMMENDED, RequirementKind.SITUATIONAL)
    ]

    base.update({
        "country": slate["country"],
        "can_complete": can_complete,
        "gate_reason": gate_reason,
        "vaccines": vaccines,
        "mandatory_certs": mandatory_certs,
        "total_vaccine_cost_usd_cents": total_usd,
        "mandatory_vaccine_cost_usd_cents": mandatory_cost_usd,
        "billed_vaccine_cost_usd_cents": billed_usd,   # #70: held-adjusted (held certs = 0)
        "held_certs": held_certs,                       # #70: REQUIRED_FOR_ENTRY already held
        "jab_records": jab_records,
        "secondary_signals": _SECONDARY_SIGNALS.get(key, []),
        "earliest_feasible_departure": earliest_feasible,
        "provenance": prov,
        "medical_access_tier": slate.get("medical_access_tier"),
        "evacuation_recommended": slate.get("evacuation_recommended"),
    })
    # OPT-IN: surface the optional-jab estimate ONLY when the slate actually has
    # optional jabs (additive key ABSENT otherwise — never a noisy empty estimate).
    if optional_items:
        base["optional_vaccine_estimate"] = {
            "total_usd_cents": sum(it["usd_cents"] for it in optional_items),
            "items": optional_items,
            "not_in_budget": True,
            "note": (
                "CDC-recommended / situational jabs — an OPT-IN estimate, NOT folded "
                "into the trip budget. Discuss with a travel clinic."
            ),
        }
    return base


# ===========================================================================
# 6. HEALTH ASSESS — assemble the whole-trip HealthVerdict (pure).
# ===========================================================================

def assess(
    *,
    legs: list[dict[str, Any]],
    today: str,
    buffer_days: int = DEFAULT_BUFFER_DAYS,
    simulate_source_unreachable: bool = False,
    held_vaccine_certs: list[str] | None = None,
) -> dict[str, Any]:
    """
    The core deterministic Health assessment for a whole trip — NO LLM, NO network.

    Args:
        legs: ordered list of {place_key|city|country, departure_date|checkin} dicts.
        today: REQUIRED — the ISO date the booking is being made (the gate's clock).
               MUST be supplied by the caller; raises ValueError if absent.
               (D7 contract: no wall-clock fallback on the determinism-critical path.)
        buffer_days: safety-buffer calendar days (deterministic constant default).

    Returns the HealthVerdict dict:
        {verdict (GateVerdict), bookable (bool), per_destination[], blocking[],
         flagged_destinations[], line_items[], total_vaccine_cost_usd_cents,
         jab_records[], ...}

    Verdict values (D4 contract):
        CAN_COMPLETE            — all mandatory certs obtainable in time; no unknown flags.
        CAN_COMPLETE_WITH_FLAGS — all certs obtainable (or none mandatory), but ≥1 destination
                                  has an UNKNOWN slate (advisory flagged).  bookable=True;
                                  orchestrator must surface has_health_flags advisory.
        CANNOT_COMPLETE         — ≥1 mandatory entry cert unbookable in time; bookable=False.

    The trip is CANNOT_COMPLETE iff ANY destination cannot complete a mandatory
    entry cert in time. UNKNOWN slates produce CAN_COMPLETE_WITH_FLAGS (bookable=True
    + advisory) rather than a hard block. The vaccine cost is ALWAYS summed and
    emitted as a Budget line item (the $1k correction) regardless of the gate outcome.
    Every numeric field is integer cents / days from the seed (NO-LLM-NUMBERS).
    """
    per_destination: list[dict[str, Any]] = []
    line_items: list[dict[str, Any]] = []
    all_jab_records: list[dict[str, Any]] = []
    total_usd = 0

    # DEDUP: track which vaccine ids have already been costed on an earlier leg.
    # Each physical vaccine jab is administered ONCE per traveler for the whole trip,
    # so its cost must only appear once regardless of how many destinations need it.
    # Strategy: INCREMENTAL PER-LEG — each leg's line item carries ONLY the cost of
    # vaccines first seen on that leg (new vaccines not seen in prior legs).
    # The per-destination verdict's `vaccines` list and `total_vaccine_cost_usd_cents`
    # remain full/unchanged (complete per-destination clinical data + gate logic);
    # only the trip-level billing (line_items, total_vaccine_cost_usd_cents) dedups.
    # SAFETY: the per-leg ENTRY-CERT GATE (can_complete / lead-time / mandatory_certs)
    # is computed inside assess_destination and is NEVER touched here — dedup is
    # billing-only, not gate logic.
    already_costed: set[str] = set()
    already_costed_mandatory: set[str] = set()      # #70: dedup for the ENFORCED mandatory line
    mandatory_line_items: list[dict[str, Any]] = []  # #70: entry-required-and-NOT-held only
    held = frozenset(held_vaccine_certs or [])       # #70: certs the traveler already holds

    for i, leg in enumerate(legs):
        place = (leg.get("place_key") or leg.get("city") or leg.get("country")
                 or leg.get("dest_country") or "")
        dep = (leg.get("departure_date") or leg.get("checkin") or today)
        leg_key = str(leg.get("leg_id", i))

        d = assess_destination(
            place_key=place,
            departure_date=dep,
            today=today,
            buffer_days=buffer_days,
            simulate_source_unreachable=simulate_source_unreachable,
            held_vaccine_certs=held,
        )
        d["leg_id"] = leg_key
        # TTL enforcement: a seeded CDC/WHO slate past its own fetched_at+ttl
        # freshness window must never keep being served as confidently current.
        # Downgrade can_complete=True -> None (the existing "unverified"
        # convention) with an honest stale-data note; an existing hard block
        # (cert lead-time) or existing flag is left as-is.
        apply_ttl_downgrade(d, as_of=today, verified_field="can_complete")
        per_destination.append(d)

        # Compute the INCREMENTAL cost for this leg: only vaccines not yet costed.
        incremental_usd = 0
        for vrec in d.get("vaccines", []):
            vax_id = vrec["vaccine"]
            if vax_id not in already_costed:
                already_costed.add(vax_id)
                incremental_usd += int(vrec.get("usd_cents") or 0)

        # Emit a line item only if this leg adds new vaccine cost.
        if incremental_usd > 0:
            inc_money = make_money(
                native_cents=incremental_usd, currency="USD", usd_cents=incremental_usd,
                fx_rate=1.0, fx_source="seed:health-aggregate-usd",
                fetched_at=_SEED_FETCHED_AT,
            )
            a = LineItemAssembler()
            a.add_fee(
                source="health",
                kind=LineItemKind.VACCINE,
                leg=leg_key,
                money=inc_money,
                description=(
                    f"Required + recommended vaccines / prophylaxis for "
                    f"{d.get('country', d.get('place_key'))} "
                    f"(CDC slate, upfront — incremental new vaccines for this leg)"
                ),
            )
            li = a.assemble()[0]
            line_items.append(li)
            total_usd += incremental_usd

        # #70 ENFORCED tier: a SECOND incremental line over REQUIRED_FOR_ENTRY-and-NOT-held
        # vaccines only — the budget-enforced vaccine cost (mandatory certs still to obtain).
        # Recommended + already-held vaccines are excluded (surfaced separately, not in budget).
        incremental_mandatory_usd = 0
        for vrec in d.get("vaccines", []):
            if vrec["kind"] == RequirementKind.REQUIRED_FOR_ENTRY and not vrec.get("held"):
                vax_id = vrec["vaccine"]
                if vax_id not in already_costed_mandatory:
                    already_costed_mandatory.add(vax_id)
                    incremental_mandatory_usd += int(vrec.get("usd_cents") or 0)
        if incremental_mandatory_usd > 0:
            m_money = make_money(
                native_cents=incremental_mandatory_usd, currency="USD",
                usd_cents=incremental_mandatory_usd, fx_rate=1.0,
                fx_source="seed:health-aggregate-usd", fetched_at=_SEED_FETCHED_AT,
            )
            ma = LineItemAssembler()
            ma.add_fee(
                source="health", kind=LineItemKind.VACCINE, leg=leg_key, money=m_money,
                description=(
                    f"Entry-REQUIRED vaccines (mandatory cert) for "
                    f"{d.get('country', d.get('place_key'))} — enforced in budget"
                ),
            )
            mandatory_line_items.append(ma.assemble()[0])

        all_jab_records.extend(d.get("jab_records", []))

    # Genuine hard blocks: can_complete is explicitly False (cert lead-time insufficient).
    # Unverified flags: can_complete is None (unknown slate → FLAG advisory, not hard block).
    blocking = [d for d in per_destination if d["can_complete"] is False]
    flagged = [d for d in per_destination if d["can_complete"] is None]
    bookable = len(blocking) == 0
    # D4 contract: emit can_complete_with_flags when bookable but has unknown-slate flags,
    # so the headline verdict cannot be misread as unqualified approval by any consumer.
    if not bookable:
        verdict = GateVerdict.CANNOT_COMPLETE
    elif flagged:
        verdict = GateVerdict.CAN_COMPLETE_WITH_FLAGS
    else:
        verdict = GateVerdict.CAN_COMPLETE

    # #39 trip-level optional estimate: aggregate the per-destination optional jabs,
    # deduped by vaccine id (a jab is administered ONCE per traveler), in first-seen
    # leg/slate order (deterministic). NEVER added to total_usd / line_items.
    trip_optional_items: list[dict[str, Any]] = []
    _seen_optional: set[str] = set()
    trip_mandatory_usd = 0
    _seen_mandatory: set[str] = set()
    trip_held_certs: list[str] = []          # #70: entry-required certs already held (deduped)
    _seen_held: set[str] = set()
    for d in per_destination:
        est = d.get("optional_vaccine_estimate")
        if est:
            for it in est["items"]:
                if it["vaccine"] not in _seen_optional:
                    _seen_optional.add(it["vaccine"])
                    trip_optional_items.append(it)
        for hc in d.get("held_certs", []):    # #70: traveler-held entry-required certs
            if hc not in _seen_held:
                _seen_held.add(hc)
                trip_held_certs.append(hc)
        for vrec in d.get("vaccines", []):
            if vrec["kind"] == RequirementKind.REQUIRED_FOR_ENTRY \
                    and not vrec.get("held") \
                    and vrec["vaccine"] not in _seen_mandatory:
                _seen_mandatory.add(vrec["vaccine"])
                trip_mandatory_usd += int(vrec.get("usd_cents") or 0)
    trip_optional_estimate = (
        {
            "total_usd_cents": sum(it["usd_cents"] for it in trip_optional_items),
            "items": trip_optional_items,
            "not_in_budget": True,
            "note": (
                "CDC-recommended / situational jabs across the trip — an OPT-IN "
                "estimate, NOT folded into the trip budget."
            ),
        }
        if trip_optional_items else None
    )

    result_verdict = {
        "verdict": verdict,
        "bookable": bookable,
        "today": today,
        "buffer_days": int(buffer_days),
        "per_destination": per_destination,
        "blocking": [
            {"leg_id": d["leg_id"], "place_key": d["place_key"],
             "gate_reason": d["gate_reason"],
             "earliest_feasible_departure": d.get("earliest_feasible_departure")}
            for d in blocking
        ],
        "flagged_destinations": [
            {"leg_id": d["leg_id"], "place_key": d["place_key"],
             "gate_reason": d["gate_reason"],
             "flag_advisory": d.get("flag_advisory", "")}
            for d in flagged
        ],
        "has_health_flags": len(flagged) > 0,
        "line_items": line_items,
        "mandatory_line_items": mandatory_line_items,   # #70: ENFORCED (entry-required, NOT held)
        "total_vaccine_cost_usd_cents": total_usd,
        # The Health half of the §7 YF-cert handoff (Compliance reads these and
        # owns the entry-DOCUMENT half). Surfaced for the interlock.
        "jab_records": all_jab_records,
        "mandatory_vaccine_cost_usd_cents": trip_mandatory_usd,
        "held_certs": trip_held_certs,                   # #70: entry-required certs already held
        "source_freshness": "SEEDED_FALLBACK" if simulate_source_unreachable else "SEEDED",
    }
    if trip_optional_estimate is not None:
        result_verdict["optional_vaccine_estimate"] = trip_optional_estimate
    return result_verdict


# ===========================================================================
# 7. EXPLAIN — honest summary from the structured verdict (pure formatter).
# ===========================================================================

_REASON_HEADLINES = {
    GateReason.BLOCK_CERT_LEAD_TIME: (
        "ENTRY-CERT UNBOOKABLE IN TIME: {country} requires a mandatory "
        "{certs} certificate; the dosing/validity lead-time is {required} days "
        "(incl. {buffer} buffer) but only {available} days remain before departure "
        "on {departure}. Earliest feasible departure: {earliest}."
    ),
    GateReason.BLOCK_UNKNOWN_SLATE: (
        "CANNOT CONFIRM: no seeded CDC slate for {country} — Health conservatively "
        "FLAGS rather than risk a silent OK on an entry-health requirement (never a "
        "silent pass)."
    ),
}


def explain(verdict: dict[str, Any]) -> dict[str, Any]:
    """
    Build the honest Health summary — a PURE FORMATTER over the structured verdict.
    NO free generation of facts: every factual claim derives from the structured
    per-destination record + the seeded provenance.

    Returns {verdict, headline, blocking[], vaccine_cost_usd_cents,
             mandatory_certs[], provenance[]}.
    """
    lines: list[str] = []
    provs: list[dict[str, Any]] = []
    all_mandatory: list[str] = []

    for d in verdict["per_destination"]:
        all_mandatory.extend(d.get("mandatory_certs", []))
        provs.append(d.get("provenance", {}))
        if d["can_complete"]:
            continue
        tmpl = _REASON_HEADLINES.get(d["gate_reason"], "FLAGGED: {country} ({reason}).")
        lines.append(tmpl.format(
            country=d.get("country", d["place_key"]),
            certs=", ".join(d.get("mandatory_certs", [])) or "entry",
            required=max((v["required_lead_days"] for v in d.get("vaccines", [])
                          if v["kind"] == RequirementKind.REQUIRED_FOR_ENTRY), default=None),
            buffer=d.get("buffer_days"),
            available=d.get("available_days"),
            departure=d.get("departure_date"),
            earliest=d.get("earliest_feasible_departure"),
            reason=d["gate_reason"],
        ))

    if verdict["bookable"]:
        cost = verdict["total_vaccine_cost_usd_cents"]
        # D4 contract: when bookable but the slate is CAN_COMPLETE_WITH_FLAGS (>=1
        # UNKNOWN-slate destination) the headline must NOT read as unqualified
        # approval. Lead with the FLAGGED state + the conservative-flag lines so no
        # consumer can misread "all certs obtainable" as a clean pass.
        flagged = verdict.get("verdict") == GateVerdict.CAN_COMPLETE_WITH_FLAGS \
            or verdict.get("has_health_flags")
        if flagged:
            cost_clause = (
                f" Required + recommended vaccines / prophylaxis cost "
                f"${cost/100:.0f} upfront (added to the budget as a line item)."
                if cost > 0 else ""
            )
            flag_detail = (" " + " ".join(lines)) if lines else ""
            headline = (
                "BOOKABLE WITH HEALTH FLAGS: entry-health certificates for the "
                "seeded destinations are obtainable in time, but one or more "
                "destinations have an UNVERIFIED health slate that must be reviewed "
                "before travel (not an unqualified approval)."
                + flag_detail + cost_clause
            )
        else:
            headline = (
                f"All entry-health certificates obtainable in time. Required + "
                f"recommended vaccines / prophylaxis cost ${cost/100:.0f} upfront "
                f"(added to the budget as a line item)."
                if cost > 0 else
                "No mandatory entry-health certificate or vaccine cost for this trip."
            )
    else:
        headline = " ".join(lines)

    return {
        "verdict": verdict["verdict"],
        "headline": headline,
        "blocking": verdict["blocking"],
        "vaccine_cost_usd_cents": verdict["total_vaccine_cost_usd_cents"],
        "mandatory_certs": sorted(set(all_mandatory)),
        "provenance": provs,
    }


# ===========================================================================
# 8. OPTIONAL COSMETIC LLM SUMMARY — fully clamped + validated.
# ===========================================================================
# The LLM ONLY drafts a human-friendly sentence about the (already-decided)
# verdict. It is validated to reference ONLY the structured facts; if it softens a
# block or invents a number it is REJECTED and we fall back to the deterministic
# headline. The LLM NEVER changes verdict / costs / dates.

def validate_summary(text: str, verdict: dict[str, Any]) -> bool:
    """
    True iff `text` is a faithful derivation of the structured verdict.

    Finding #64 (fail-closed, corrected): for a CANNOT_COMPLETE verdict the prior
    implementation FAILED OPEN — it merely *required* the summary to contain a block
    keyword, so a softened draft that asserts the trip is fine yet happens to mention
    "certificate" (a block keyword) sailed through and overturned the deterministic
    gate.  The honest fail-closed rule rejects on BOTH counts:

      (1) SOFTEN GUARD (the real fail-closed check, matching the compliance/fraud/risk
          siblings): if the draft contains ANY phrase that softens the block into an
          approval ("all set", "is fine", "good to go", "can book", "no issue",
          "obtainable in time", …) it is REJECTED outright — even if it also mentions
          a block keyword.  A soften phrase can never be redeemed by a block keyword.
      (2) POSITIVE BLOCK REFERENCE: the draft must also actually reference the block
          (cannot/unable/blocked/cert/place name/earliest feasible date); a draft that
          neither softens NOR names the block (e.g. "requires advance preparation") is
          still rejected as non-derivative.

    An empty or non-string summary is always rejected.

    For CAN_COMPLETE / CAN_COMPLETE_WITH_FLAGS the check is structural-only (non-empty
    string passes unless obviously empty).
    """
    if not isinstance(text, str) or not text.strip():
        return False
    low = text.lower()
    if not verdict.get("bookable"):
        # (1) SOFTEN GUARD — fail CLOSED: any approval/softening of a hard block is
        # rejected regardless of any block keyword also present (closes Finding #64's
        # fail-open hole). Mirrors compliance_agent / fraud_agent / risk_agent.
        soften = [
            "all set", "all clear", "is fine", "you're fine", "you are fine",
            "good to go", "no problem", "no issue", "no concerns", "no risk",
            "can book", "can be booked", "is bookable", "ready to book",
            "safe to book", "can travel", "able to travel", "can proceed",
            "you can go", "good to travel", "obtainable in time", "no mandatory",
        ]
        if any(s in low for s in soften):
            return False
        # (2) POSITIVE BLOCK REFERENCE — the draft must actually name the block, so a
        # vague non-derivative sentence is rejected too.
        block_signals: list[str] = ["cannot", "can not", "unable", "blocked",
                                     "unbookable", "certificate", "cert",
                                     "depart later", "earliest", "lead time",
                                     "lead-time", "not be completed", "not obtainable"]
        # Add the earliest feasible departure date + place keys from the structured verdict.
        for d in verdict.get("blocking", []):
            efd = d.get("earliest_feasible_departure")
            if efd:
                block_signals.append(str(efd))
            place = d.get("place_key", "")
            if place:
                block_signals.append(place.lower())
        # Also gather country names from per_destination.
        for d in verdict.get("per_destination", []):
            country = d.get("country", "")
            if country:
                block_signals.append(country.lower())
        if not any(sig in low for sig in block_signals):
            return False
    return True


def _llm_summary(verdict: dict[str, Any]) -> str | None:
    """
    Optional cosmetic summary sentence via DashScope. Returns None if no key, on
    any error, or if the draft fails validate_summary (→ deterministic fallback).
    NEVER changes the structured verdict.
    """
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        return None
    try:
        import httpx
        facts = json.dumps({
            "verdict": verdict["verdict"],
            "blocking": verdict["blocking"],
            "total_vaccine_cost_usd_cents": verdict["total_vaccine_cost_usd_cents"],
        }, sort_keys=True)
        prompt = (
            "You are a travel planner. In ONE sentence, restate this health "
            "decision for the traveler. Use ONLY these facts; do NOT add numbers "
            "or claim the trip is fine if a mandatory entry certificate is "
            "unbookable in time.\n" + facts
        )
        resp = httpx.post(
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"enable_thinking": False, "model": "qwen3-max",
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30.0,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        return text if validate_summary(text, verdict) else None
    except Exception:
        return None


# ===========================================================================
# 9. HealthAgent — A2A wrapper (follows the §AGENT-EXTENSION PATTERN).
# ===========================================================================

class HealthAgent(A2AAgent):
    """
    Health vaccination / entry-health GATE specialist (Travel Guild, L2 / DC-health).

    Implements:
      ``health.assess``  — per-destination CDC slate + the mandatory-cert gate +
                           vaccine cost line item + the JAB handoff half.
      ``health.explain`` — honest summary built ONLY from the structured verdict.

    The gate path is fully deterministic (no LLM, no network). The optional
    cosmetic summary (DashScope) is fully clamped behind the deterministic verdict.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 9108) -> None:
        self._host = host
        self._port = port
        super().__init__()

    # ------------------------------------------------------------------
    # A2AAgent protocol
    # ------------------------------------------------------------------

    def _build_card(self) -> dict:
        url = f"http://{self._host}:{self._port}"
        return {
            "name": "health-agent",
            "description": (
                "Health vaccination / entry-health GATE specialist (L2) — a deterministic "
                "BOOKABILITY gate + budget correction, not clinical advice. Given a trip's "
                "destinations + departure date, resolves each destination's CDC vaccination / "
                "prophylaxis slate and produces three plan-changing outputs: (a) the UPFRONT "
                "vaccine COST as a Budget line item (the '$1k the single agent forgets'); "
                "(b) the ENTRY-CERT GATE — if a MANDATORY entry certificate (yellow-fever) "
                "cannot be completed in time (dosing lead-time > days-to-departure) the trip "
                "is flagged cannot_complete with an honest reason + earliest feasible "
                "departure; UNKNOWN slate → conservative flag, never a silent OK; and (c) the "
                "YF-cert INTERLOCK — Health owns the JAB (cost/lead-time/validity) and emits "
                "the jab half of the handoff for Compliance (which owns the entry DOCUMENT). "
                "Deterministic slate-resolver; NO-LLM-NUMBERS (costs/lead-times/validity are "
                "integer values from the seeded CDC slate); the LLM only drafts validated "
                "cosmetic summary. Per-user allergy / fitness are OUT OF SCOPE (surfaced, not "
                "owned). Implements 'health.assess' + 'health.explain'. Part of the Travel "
                "Society multi-agent pipeline (Track 3, L2 / DC-health)."
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
                    "id": "health.assess",
                    "name": "Assess Health (vaccine cost + entry-cert gate)",
                    "description": (
                        "Given {legs[{place_key|city, departure_date}], today?, buffer_days?} "
                        "compute the whole-trip HealthVerdict: per-destination CDC slate, the "
                        "deterministic entry-cert gate (cannot_complete if a mandatory cert's "
                        "dosing lead-time exceeds days-to-departure), the UPFRONT vaccine cost "
                        "line item (Budget vetoes it), and the JAB handoff half for Compliance. "
                        "UNKNOWN slate → conservative flag (never silent OK). NO-LLM-NUMBERS."
                    ),
                    "tags": ["health", "vaccine", "vaccination", "prophylaxis", "yellow-fever",
                             "entry-cert", "lead-time", "gate", "deterministic", "money-path", "L2"],
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "legs": {"type": "array", "items": {"type": "object"}},
                            "today": {"type": "string"},
                            "buffer_days": {"type": "integer"},
                            "held_vaccine_certs": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["legs"],
                    },
                    "examples": [
                        json.dumps({
                            "legs": [{"place_key": "ethiopia", "departure_date": "2026-06-30"}],
                            "today": "2026-06-16"}),
                    ],
                },
                {
                    "id": "health.explain",
                    "name": "Explain Health (honest summary)",
                    "description": (
                        "Given a HealthVerdict (or the same assess input), return the honest "
                        "summary built ONLY from the structured verdict + seeded provenance "
                        "(no free generation of facts). Pure formatter; optional clamped+"
                        "validated cosmetic LLM summary."
                    ),
                    "tags": ["health", "vaccine", "summary", "explanation", "deterministic"],
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "verdict": {"type": "object"},
                            "legs": {"type": "array", "items": {"type": "object"}},
                            "today": {"type": "string"},
                            "use_llm": {"type": "boolean"},
                        },
                    },
                },
            ],
        }

    def _register_skills(self) -> None:
        self.register_skill("health.assess", self._assess_handler)
        self.register_skill("health.explain", self._explain_handler)

    # ------------------------------------------------------------------
    # Skill handlers
    # ------------------------------------------------------------------

    async def _assess_handler(self, message: dict, task: dict) -> dict:
        payload = self._extract_payload(message)
        if not isinstance(payload, dict):
            raise ValueError(
                "health.assess requires a data part with a JSON object "
                "{legs[], today?, buffer_days?}"
            )
        legs = payload.get("legs")
        if not isinstance(legs, list) or not legs:
            raise ValueError("health.assess: 'legs' must be a non-empty list")

        # D7 contract: 'today' MUST be supplied; no wall-clock fallback on the
        # determinism-critical path (byte-identical reruns require a frozen clock).
        today = payload.get("today")
        if not today:
            raise ValueError(
                "health.assess: 'today' (ISO date) is required — "
                "the gate cannot fall back to wall-clock time (var-0 / D7 contract). "
                "The orchestrator must inject a single frozen today for the whole run."
            )
        buffer_days = int(payload.get("buffer_days", DEFAULT_BUFFER_DAYS))

        _held = payload.get("held_vaccine_certs")
        verdict = assess(
            legs=legs,
            today=today,
            buffer_days=buffer_days,
            simulate_source_unreachable=bool(payload.get("simulate_source_unreachable", False)),
            held_vaccine_certs=_held if isinstance(_held, list) else None,
        )

        logger.info(
            "health.assess: legs=%d verdict=%s blocking=%d vaccine_cost=%d¢ freshness=%s",
            len(verdict["per_destination"]), verdict["verdict"],
            len(verdict["blocking"]), verdict["total_vaccine_cost_usd_cents"],
            verdict["source_freshness"],
        )
        return _new_artifact(
            name="health.assess.result",
            parts=[_data_part(verdict)],
        )

    async def _explain_handler(self, message: dict, task: dict) -> dict:
        payload = self._extract_payload(message)
        if not isinstance(payload, dict):
            raise ValueError(
                "health.explain requires a data part with {verdict} OR {legs}"
            )
        verdict = payload.get("verdict")
        if not isinstance(verdict, dict):
            legs = payload.get("legs")
            if not isinstance(legs, list) or not legs:
                raise ValueError(
                    "health.explain: provide a 'verdict' object, or {legs} to compute one"
                )
            # D7 contract: 'today' MUST be supplied; no wall-clock fallback.
            today = payload.get("today")
            if not today:
                raise ValueError(
                    "health.explain: 'today' (ISO date) is required when computing a "
                    "verdict from legs — no wall-clock fallback (var-0 / D7 contract)."
                )
            verdict = assess(
                legs=legs,
                today=today,
                buffer_days=int(payload.get("buffer_days", DEFAULT_BUFFER_DAYS)),
            )

        result = explain(verdict)

        if payload.get("use_llm"):
            summary = _llm_summary(verdict)
            result["llm_summary"] = summary  # None if unavailable/invalid
            result["summary_source"] = "llm" if summary else "deterministic"
        else:
            result["summary_source"] = "deterministic"

        return _new_artifact(
            name="health.explain.result",
            parts=[_data_part(result)],
        )

    # ------------------------------------------------------------------
    # Input extraction helper (same pattern as Compliance / Insurance / Risk).
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
# Public read-only views — for the FAIR DC-health baseline (identical data, §10.10).
# ===========================================================================

def health_slates_public() -> list[dict[str, Any]]:
    """
    The seeded CDC slates as plain rows (the IDENTICAL data the society's gate
    resolves against), exposed so the FAIR DC-health single-agent baseline gets the
    same data — the §10.10 fairness contract. Read-only copies.
    """
    out: list[dict[str, Any]] = []
    for key, slate in _HEALTH_SLATES.items():
        out.append({
            "place_key": key,
            "country": slate["country"],
            "source": slate["source"],
            "source_url": slate["source_url"],
            "vaccines": [dict(v) for v in slate["vaccines"]],
        })
    return out


def health_slate_public(place_key: str) -> dict[str, Any] | None:
    """The single resolved CDC slate for a destination as a plain row, or None."""
    slate, _ = fetch_slate(place_key)
    if slate is None:
        return None
    key = canonical_place_key(place_key or "")
    return {
        "place_key": key,
        "country": slate["country"],
        "source": slate["source"],
        "source_url": slate["source_url"],
        "vaccines": [dict(v) for v in slate["vaccines"]],
    }


# ===========================================================================
# Entry point
# ===========================================================================

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )
    port = int(os.environ.get("PORT", 9108))
    host = os.environ.get("AGENT_BIND_HOST", "127.0.0.1")

    agent = HealthAgent(host=host, port=port)
    app = agent.build_app()

    logger.info("Health agent starting on %s:%d", host, port)
    logger.info("Agent Card: http://%s:%d/.well-known/agent-card.json", host, port)
    logger.info("RPC endpoint: http://%s:%d/", host, port)

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()


# ===========================================================================
# §AGENT-EXTENSION NOTE — Health is the FOURTH specialist on the pattern.
# ===========================================================================
# Built per the §AGENT-EXTENSION PATTERN documented at the bottom of
# insurance_agent.py (the 6-step pattern Insurance set; Compliance + Risk followed).
# Recap of what this file did so the NEXT specialist (Fraud, Loyalty, ...) mirrors it:
#   1. NEW FILE on a NEW PORT (9108), subclass A2AAgent, _build_card +
#      _register_skills (health.assess / health.explain).
#   2. COMPOSE against Phase-0 contracts: make_money / make_provenance / SourceTier
#      + line_item_assembler.add_fee(kind=VACCINE) (Budget vetoes the vaccine cost
#      like any line item) + fx_provider.normalize() for the non-USD (SGD) slate +
#      contracts.make_jab_record for the §7 YF-cert handoff (Health owns the JAB;
#      Compliance owns the entry DOCUMENT). NO parallel shapes.
#   3. VARIANCE CLAMP: NO-LLM-NUMBERS (costs/lead-times/validity are integer seed
#      values); the entry-cert gate (CAN/CANNOT_COMPLETE) is a PURE function over a
#      closed requirement-kind set; UNKNOWN slate → conservative flag (never a
#      silent OK); the LLM is cosmetic only, validated by validate_summary + falls
#      back to the deterministic headline.
#   4. SEEDED DATA, provenance-tagged: the per-destination CDC slate (Ethiopia,
#      Kenya, Thailand, Bali) with an honest Tier-3 SEEDED_FALLBACK behind
#      fetch_slate(); a live CDC Travelers' Health feed is a Tier-2 swap, not a
#      re-architecture.
#   5. WIRING — APPEND-ONLY orchestrator hook (health_url/health_client +
#      _call_health(), defaulting to None so every existing call site is unchanged).
#      Registration IS the AgentCard + the optional hook.
#   6. VERIFY HARD: test_health_agent.py (CI-safe ASGI TestClient) with the
#      CI-enforced invariants (budget ALWAYS includes the required-vaccine cost;
#      the society NEVER returns a plan missing a mandatory entry-cert; deterministic;
#      numbers from seed not LLM; UNKNOWN never silent-OK) + the DC-health fair
#      baseline (baseline_health.py + run_dc_health_bench.py) measuring the gain.
#
# SCOPE ANCHOR (AGENTS.md): Health models ONLY what changes the BOOKABLE PLAN —
# vaccine COST (→ budget), dosing LEAD-TIME (→ timeline), mandatory CERT (→ entry
# gate). It SURFACES but does NOT own traveler-judgment (fitness); PER-USER ALLERGY
# is explicitly OUT OF SCOPE; altitude / med-legality are LIGHT SECONDARY SIGNALS
# only (no number, no gate).
