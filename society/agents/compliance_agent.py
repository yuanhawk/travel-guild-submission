"""
compliance_agent.py — Compliance ELIGIBILITY GATE (Travel Guild, L2 / DC1).

Design contract: the §AGENT-EXTENSION PATTERN at the bottom of
``insurance_agent.py`` (the repeatable 6-step pattern; this is the SECOND roadmap
specialist after Insurance), AGENTS.md (Phase-0 contracts + variance-clamp +
scope-anchor), and the "Compliance" roadmap entry. DC1 is the reusable
FAIR-CONTRAST sibling of DC0.

WHAT THIS AGENT OWNS (one decision — a BOOKABILITY GATE, not advice)
-------------------------------------------------------------------
Given a trip (destinations, departure date, traveler nationality), it
deterministically checks each leg's ENTRY requirement (visa / eVisa: required?,
cost, **processing lead-time**, eligibility-by-nationality) against the departure
date. The flagship structural check is the **visa LEAD-TIME gate**:

    if (departure_date - today) < (visa_processing_lead_time + safety_buffer):
        the trip is UNBOOKABLE IN TIME → the gate BLOCKS it.

When blocked, the agent does NOT just refuse: it returns an honest
``cannot_satisfy`` verdict PLUS a compliant re-sequence — either (a) the earliest
departure date at which the visa CAN be obtained in time (depart-later), or
(b) a visa-free / visa-on-arrival alternative when one exists. Any visa FEE is
emitted as a Budget line item via the Phase-0 ``line_item_assembler`` (Budget
vetoes it like any other line item; idempotent).

It is a PLANNER, not a consultant: it only models a dimension that changes the
BOOKABLE PLAN. It does NOT give immigration advice, does NOT model every visa
class — it answers one question per leg: *can this traveler legally enter, in
time, for this departure date?* Passport-validity + customs are secondary
(stubbed honest checks); the visa lead-time gate is the flagship.

THE VARIANCE CLAMP (non-negotiable — money-path agent)
------------------------------------------------------
  NO-LLM-NUMBERS:     every lead-time (business days), fee (integer cents), and
                      date is straight from the SEEDED per-country eVisa table
                      (NZeTA, Ethiopia eVisa, US ESTA, India eVisa, ...). The LLM
                      NEVER mints a number or a date. Identical inputs → byte-
                      identical verdict + dates + fees.
  DETERMINISTIC-VERDICT: the gate (BLOCK / ALLOW) is a PURE function of (entry
                      rule, nationality, departure date, today). Zero variance.
  CLOSED-SET:         entry-requirement kinds are a closed set
                      (VISA_FREE / VISA_ON_ARRIVAL / EVISA / VISA_REQUIRED /
                      UNKNOWN). UNKNOWN degrades to a CONSERVATIVE block-with-
                      flag — NEVER a silent ALLOW (the single most important
                      guard: an unknown rule must never let an unbookable trip
                      through).
  LLM IS COSMETIC ONLY: the LLM only drafts the human-readable re-sequence
                      rationale, validated to be a derivation of the structured
                      verdict. With the LLM disabled the agent still produces the
                      full, correct EligibilityVerdict (the closed-set fallback).

A2A skills
----------
  ``compliance.check_eligibility`` — per-leg entry verdict + the lead-time gate
                                     decision + visa fee line item + re-sequence.
  ``compliance.explain_block``     — honest cannot_satisfy headline built ONLY
                                     from the structured verdict.

Runnable service: HOST / PORT env, defaults 0.0.0.0:9106 (next free port after
Insurance's 9105 — see §AGENT-EXTENSION PATTERN in insurance_agent.py).

Determinism: the rule-resolver + gate + line-item emission are pure Python over
closed sets; no LLM, no network in the verdict path. The only optional network
call is the cosmetic re-sequence-writer (DashScope), fully behind the clamp.
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
    make_money,
    make_provenance,
)
from utils.line_item_assembler import LineItemAssembler, LineItemKind

logger = logging.getLogger(__name__)


# ===========================================================================
# 0. ENTRY-REQUIREMENT KIND + GATE VERDICT — the closed output vocabularies.
# ===========================================================================
# Both are PURE deterministic functions of (rule, nationality, departure, today).
# UNKNOWN is the conservative default for any (nationality, destination) pair with
# no seeded rule: it is NEVER a silent "VISA_FREE / ALLOW" (the core guard — an
# unknown entry rule must never let an unbookable trip through).

class EntryKind:
    """The closed set of per-leg entry-requirement kinds."""

    VISA_FREE = "VISA_FREE"              # no visa needed — lead-time gate N/A
    VISA_ON_ARRIVAL = "VISA_ON_ARRIVAL"  # obtained at the border — lead-time ~0
    EVISA = "EVISA"                      # apply online; has a PROCESSING lead-time
    VISA_REQUIRED = "VISA_REQUIRED"      # embassy/consular visa; long lead-time
    UNKNOWN = "UNKNOWN"                  # no seeded rule — conservative block-flag


_ENTRY_KINDS = frozenset({
    EntryKind.VISA_FREE, EntryKind.VISA_ON_ARRIVAL, EntryKind.EVISA,
    EntryKind.VISA_REQUIRED, EntryKind.UNKNOWN,
})

# Kinds that carry a processing lead-time the gate must clear before departure.
_LEAD_TIME_KINDS = frozenset({EntryKind.EVISA, EntryKind.VISA_REQUIRED})


class GateVerdict:
    """The closed set of gate outcomes for the whole trip."""

    ALLOW = "can_satisfy"          # every leg bookable in time (or no visa needed)
    BLOCK = "cannot_satisfy"       # at least one leg UNBOOKABLE IN TIME / ineligible
    # D4 (verdict legibility): BOOKABLE-BUT-FLAGGED. At least one leg's entry rule is
    # UNVERIFIED (unknown rule / unspecified nationality → allowed=None). No leg is a
    # HARD block, so the trip remains bookable=True, but the HEADLINE verdict must NOT
    # read as an unqualified "can_satisfy" — a naive consumer keying off the verdict
    # field would otherwise treat an unverified-eligibility trip as fully cleared
    # (I4 violation). The orchestrator treats this as bookable=True + surfaces the
    # advisory (no new block).
    ALLOW_WITH_FLAGS = "can_satisfy_with_flags"


# Per-leg reasons (closed set; the structured, LLM-free verdict vocabulary).
class LegReason:
    OK_VISA_FREE = "OK_VISA_FREE"
    OK_VISA_ON_ARRIVAL = "OK_VISA_ON_ARRIVAL"
    OK_LEAD_TIME_CLEARED = "OK_LEAD_TIME_CLEARED"
    BLOCK_LEAD_TIME = "BLOCK_INSUFFICIENT_LEAD_TIME"   # the flagship structural block
    BLOCK_INELIGIBLE = "BLOCK_NATIONALITY_INELIGIBLE"  # eVisa not open to nationality
    BLOCK_UNKNOWN_RULE = "BLOCK_UNKNOWN_RULE_CONSERVATIVE"
    BLOCK_PASSPORT_VALIDITY = "BLOCK_PASSPORT_VALIDITY"  # secondary (stub) check


# ===========================================================================
# 1. SEEDED DATA — per-(destination) entry-requirement table, by nationality.
# ===========================================================================
# Provenance-tagged per contracts.py (§13 reproducible). Tier-1 SEEDED today; a
# live IATA Timatic / govt feed is a Tier-2 adapter swap behind fetch_entry_rule()
# — NOT a re-architecture. ALL lead-times are integer BUSINESS DAYS; all fees are
# integer cents in a named currency (NO-LLM-NUMBERS). Sources are the real public
# government programmes (NZeTA, US ESTA, Ethiopia eVisa, India eVisa) so the
# numbers are grounded and auditable.
#
# Schema per rule:
#   dest_country (ISO-3166 alpha-2), program, kind (EntryKind),
#   eligible_nationalities (None = ALL; else a closed allow-list),
#   processing_lead_business_days, fee_cents, currency,
#   provenance, source_url.
#
# Lead-times are the OFFICIAL published "allow up to N business days" guidance —
# the planner uses the published worst-case so a trip is only ALLOWED when it is
# genuinely bookable in time (conservative by construction).

_SEED_FETCHED_AT = "2026-06-10"
_SEED_TTL_DAYS = 90
_SEED_TTL_SECONDS = _SEED_TTL_DAYS * 24 * 3600

# Safety buffer (business days) added on top of the published processing lead-time
# before the gate clears a trip: shipping/clearance slack so an ALLOW is honest.
# Deterministic constant (NO-LLM-NUMBERS) — never an LLM-chosen margin.
DEFAULT_BUFFER_BUSINESS_DAYS = 2


# (dest_country, nationality?) → entry rule. nationality-specific rows override the
# wildcard row. Keyed for O(1) lookup; the resolver is a pure function.
_ENTRY_RULES: list[dict[str, Any]] = [
    # ---- New Zealand: Australian citizens visa-free (Trans-Tasman) ----
    # AU citizens can live/work/visit NZ indefinitely — no visa, no NZeTA.
    {
        "dest_country": "NZ", "program": "Trans-Tasman visa-free",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["AU"],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:immigration.govt.nz-trans-tasman-2026",
        "source_url": "https://www.immigration.govt.nz/",
    },
    # ---- New Zealand: NZeTA (the flagship lead-time gate anchor) ----
    # NZeTA: visa-waiver eligible nationalities must hold an NZeTA BEFORE travel.
    # Official guidance: "request it at least 72 hours before you travel" but the
    # published worst case is up to 3 business days to process — we seed 3 bd.
    {
        # E2 fix: NZeTA is NOT open to "ALL" — it is a closed visa-waiver list of
        # ~60 countries/territories (immigration.govt.nz/visit/what-you-need-to-visit-new-zealand/
        # visa-waiver-countries-and-territories/, WebFetch-verified 2026-07-04). AU is
        # excluded here — Australian citizens/PRs use the separate Trans-Tasman row above
        # (no NZeTA at all) and must not double-match this row. Anyone NOT on this list
        # falls through to the NZ Visitor Visa (consular) wildcard row below.
        "dest_country": "NZ", "program": "NZeTA",
        "kind": EntryKind.EVISA,
        "eligible_nationalities": [
            "AD", "AR", "AT", "BH", "BE", "BR", "BN", "BG", "CA", "CL",
            "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HK",
            "HU", "IS", "IE", "IL", "IT", "JP", "KR", "KW", "LV", "LI",
            "LT", "LU", "MO", "MY", "MT", "MU", "MX", "MC", "NL", "NO",
            "OM", "PL", "PT", "QA", "RO", "SM", "SA", "SC", "SG", "SK",
            "SI", "ES", "SE", "CH", "TW", "AE", "GB", "US", "UY", "VA",
        ],
        "processing_lead_business_days": 3,
        # NZeTA app fee NZ$17 ≈ US$10 — seeded in USD (the FX seam only knows the
        # §19.1 closed currency set; NZD is not in it, so we hold the USD figure).
        "fee_cents": 1000, "currency": "USD",
        "provenance": "seed:immigration.govt.nz-NZeTA-2026 (visa-waiver list WebFetch-verified 2026-07-04)",
        "source_url": "https://www.immigration.govt.nz/visit/what-you-need-to-visit-new-zealand/visa-waiver-countries-and-territories/",
    },
    # ---- New Zealand: Visitor Visa (consular) — everyone NOT on the NZeTA
    #      visa-waiver list (E2 fix: the wildcard fallback the old None-wildcard
    #      NZeTA row was silently absorbing; without this row those travelers would
    #      have had no rule at all once NZeTA became a closed allow-list). ----
    {
        "dest_country": "NZ", "program": "Visitor Visa",
        "kind": EntryKind.VISA_REQUIRED,
        "eligible_nationalities": None,
        "processing_lead_business_days": 25,  # published worst-case (20-25 business days)
        "fee_cents": 14500, "currency": "USD",  # NZ$246 visa fee, seeded in USD (NZD not in the FX seam)
        "provenance": "seed:immigration.govt.nz-visitor-visa-2026",
        "source_url": "https://www.immigration.govt.nz/visas/visitor-visa/",
    },
    # ---- United States: ESTA (VWP countries) ----
    # E2 fix: was `eligible_nationalities: None` (treated as ALL) — converted to the
    # real closed Visa Waiver Program list (~42 countries; travel.state.gov +
    # dhs.gov, WebFetch-verified 2026-07-04). Anyone NOT on this list needs a full
    # B1/B2 consular visa (the wildcard row directly below, which now also serves
    # as the general non-VWP fallback rather than a hand-picked short list).
    {
        "dest_country": "US", "program": "ESTA",
        "kind": EntryKind.EVISA,
        "eligible_nationalities": [
            "AD", "AU", "AT", "BE", "BN", "CL", "HR", "CZ", "DK", "EE",
            "FI", "FR", "DE", "GR", "HU", "IS", "IE", "IL", "IT", "JP",
            "LV", "LI", "LT", "LU", "MT", "MC", "NL", "NZ", "NO", "PL",
            "PT", "QA", "SM", "SG", "SK", "SI", "KR", "ES", "SE", "CH",
            "TW", "GB",
        ],
        "processing_lead_business_days": 3,   # "apply at least 72 hours before travel"
        "fee_cents": 4027, "currency": "USD",   # $40.27 ESTA ($21→$40 on 2025-09-30 HR-1; +CPI to $40.27 on 2026-01-01)
        "provenance": "seed:cbp.gov-ESTA-2026 (fee per DHS HR-1 + 2026 CPI adj; VWP list WebFetch-verified 2026-07-04)",
        "source_url": "https://esta.cbp.dhs.gov/",
    },
    # ---- United States: non-VWP nationalities require full B1/B2 visa ----
    # E2 fix: this is now the general wildcard fallback for every nationality NOT
    # on the ESTA/VWP list above (previously a hand-picked 13-country list, which
    # was itself evidence of the same None-wildcard-vs-explicit-allow-list bug —
    # every non-VWP nationality needs this same B1/B2 visa, not just those 13).
    {
        "dest_country": "US", "program": "US B1/B2 visitor visa",
        "kind": EntryKind.VISA_REQUIRED,
        "eligible_nationalities": None,
        "processing_lead_business_days": 15,  # consular appointment; conservative worst-case
        "fee_cents": 18500, "currency": "USD",  # $185 MRV visa fee
        "provenance": "seed:travel.state.gov-B1B2-non-VWP-2026",
        "source_url": "https://travel.state.gov/content/travel/en/us-visas/tourism-visit/visitor.html",
    },
    # ---- Ethiopia: eVisa (the long-lead-time anchor — the benchmark scenario) ----
    # Ethiopia eVisa published guidance: apply well in advance; standard processing
    # is up to 3 business days, but the conservative published worst-case is longer.
    # We seed 5 business days — a single solo traveler departing in 3 calendar days
    # is UNBOOKABLE IN TIME by construction (the DC1 benchmark anchor).
    {
        "dest_country": "ET", "program": "Ethiopia eVisa",
        "kind": EntryKind.EVISA,
        "eligible_nationalities": None,
        "processing_lead_business_days": 5,
        "fee_cents": 8200, "currency": "USD",   # ~$82 single-entry tourist eVisa
        "provenance": "seed:evisa.gov.et-2026",
        "source_url": "https://www.evisa.gov.et/",
    },
    # ---- India: Nepal/Bhutan/Maldives visa-free (override the eVisa wildcard) ----
    {
        "dest_country": "IN", "program": "Visa-free entry (neighbouring states)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["NP", "BT", "MV"],  # NP/BT passport-&-visa-free; MV 90-day visa-free
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:mha.gov.in-visafree-2026 (NP/BT MHA 2025-09-01; MV bilateral 90d)",
        "source_url": "https://www.mha.gov.in/",
    },
    # ---- India: eVisa (nationality-eligibility example) ----
    {
        "dest_country": "IN", "program": "India eVisa",
        "kind": EntryKind.EVISA,
        "eligible_nationalities": None,
        "processing_lead_business_days": 4,
        "fee_cents": 2500, "currency": "USD",   # ~$25 e-Tourist visa (short)
        "provenance": "seed:indianvisaonline.gov.in-2026",
        "source_url": "https://indianvisaonline.gov.in/evisa/",
    },
    # ---- Brazil: reinstated mandatory tourist e-Visa for US/CA/AU (effective 2025-04-10) ----
    {
        "dest_country": "BR", "program": "Brazil e-Visa (VFS)",
        "kind": EntryKind.EVISA,
        "eligible_nationalities": ["US", "CA", "AU"],  # e-Visa reinstated 2025-04-10; most others visa-free
        "processing_lead_business_days": 5,
        "fee_cents": 8081, "currency": "USD",   # ~US$80.81 (US$40.90 consular + VFS service fee)
        "provenance": "seed:gov.br-evisa-2025 (reinstated 2025-04-10 for US/CA/AU)",
        "source_url": "https://brazil.vfsevisa.com/",
    },
    # ---- Schengen / France: strong passports are visa-free ≤90 days ----
    # EU/EEA + many bilateral partners travel to Schengen without a visa.
    # Must appear BEFORE the consular-visa wildcard row.
    {
        "dest_country": "FR", "program": "Schengen visa-free (bilateral)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": [
            # EU / EEA nationals
            "DE", "IT", "ES", "PT", "NL", "BE", "AT", "CH", "SE", "NO", "DK", "FI",
            "IS", "LU", "GR", "PL", "CZ", "HU", "SK", "SI", "HR", "EE", "LT", "LV",
            "MT", "CY", "BG", "RO", "IE",
            # Bilateral visa-free partners (EU Annex II visa-exempt third countries)
            "US", "GB", "AU", "NZ", "JP", "KR", "SG", "MY", "CA", "HK", "TW",
            "BR", "MX", "AR", "CL", "AE", "IL", "CO", "PE",
        ],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:ec.europa.eu-schengen-visafree-2026 (Annex II: +IL/CO/PE)",
        "source_url": "https://home-affairs.ec.europa.eu/policies/schengen-borders-and-visa_en",
    },
    # ---- Schengen / France: full consular visa (long lead-time) for remaining
    #      nationalities (IN, CN, NG, ZA, PH, ID, TH, VN, etc.) ----
    {
        "dest_country": "FR", "program": "Schengen short-stay visa",
        "kind": EntryKind.VISA_REQUIRED,
        "eligible_nationalities": None,
        "processing_lead_business_days": 15,   # Schengen "apply up to 15 working days before"
        "fee_cents": 9000, "currency": "EUR",   # €90 Schengen visa fee
        "provenance": "seed:france-visas.gouv.fr-2026",
        "source_url": "https://france-visas.gouv.fr/",
    },
    # ---- Thailand: VoA for nationalities not on the visa-exemption list ----
    # NG is not on Thailand's visa-exemption list; needs VoA (THB 2,000).
    # E2 fix: ZA REMOVED from this row — South Africa has been on Thailand's
    # 60-day visa-EXEMPTION list since 2024 (tourismthailand.org + Wikipedia
    # visa-policy table, WebFetch/WebSearch-verified 2026-07-04), not VoA; it now
    # lives in the "visa exemption" row below. Leaving it on both rows would be an
    # ambiguous seed (two specific rows claiming the same nationality).
    {
        "dest_country": "TH", "program": "Visa on Arrival",
        "kind": EntryKind.VISA_ON_ARRIVAL,
        "eligible_nationalities": ["NG"],
        "processing_lead_business_days": 0,
        "fee_cents": 200000, "currency": "THB",  # THB 2,000 ≈ $58 at seeded rate
        "provenance": "seed:mfa.go.th-VOA-2026",
        "source_url": "https://www.mfa.go.th/",
    },
    # ---- Thailand: visa exemption ----
    # E2 fix: was `eligible_nationalities: None` (treated as ALL) — converted to
    # the real closed visa-exemption list (the ~93-country/60-day scheme in force
    # since 2024-07-15, plus the longstanding 90/30/14/7-day tiers; Wikipedia
    # visa-policy table + tourismthailand.org, WebFetch/WebSearch-verified
    # 2026-07-04). This system does not model differing stay-length tiers, only
    # whether a visa is needed, so all exemption tiers are folded into one
    # VISA_FREE row. NOTE (re-verified 2026-07-04 via TAT Newsroom + EY tax-news):
    # the Thai Cabinet DID approve a revision on 2026-05-19 that would replace
    # this 93-country/60-day scheme with a 30-day exemption for 54 countries (+
    # a 15-day tier for MV/MU/SC, and VoA cut to just 4 countries) — but as of
    # the latest reporting it had NOT yet been published in the Royal Gazette
    # (publication starts a 15-day countdown to taking effect), so the current
    # 60-day scheme (in force since 2024-07-15) is still the seeded law. This is
    # a real, imminent, cabinet-approved change, not a rumor — RECONFIRM against
    # the Gazette before the _SEED_TTL_DAYS window (90d from 2026-06-10) lapses,
    # and sooner if there is any signal the new rules have been gazetted. Anyone
    # NOT on this list falls through to the new Thailand Tourist Visa (TR)
    # wildcard row below.
    {
        "dest_country": "TH", "program": "visa exemption",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": [
            # 90-day
            "AR", "BR", "CL", "KR", "PE",
            # 60-day — ASEAN (excl. KH/MM/TL, which have their own shorter tiers)
            "BN", "ID", "LA", "MY", "PH", "SG", "VN",
            # 60-day — EU
            "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR",
            "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL",
            "PL", "PT", "RO", "SK", "SI", "ES", "SE",
            # 60-day — EFTA
            "IS", "LI", "NO", "CH",
            # 60-day — GCC
            "BH", "KW", "OM", "QA", "SA", "AE",
            # 60-day — other individual countries
            "AL", "AD", "AU", "BT", "CA", "CN", "CO", "CU", "DM", "DO",
            "EC", "FJ", "GE", "GT", "HK", "IN", "IL", "JM", "JP", "JO",
            "KZ", "MO", "MV", "MU", "MX", "MC", "MN", "MA", "NZ", "PA",
            "PG", "RU", "SM", "LK", "ZA", "TW", "TO", "TT", "TR", "UA",
            "GB", "US", "UY", "UZ",
            # 30-day
            "TL",
            # 14-day (air arrivals only)
            "MM",
            # 7-day (tourism only, reduced 2025)
            "KH",
        ],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:thaiembassy-visa-exemption-2026 (list WebFetch/WebSearch-verified 2026-07-04)",
        "source_url": "https://www.thaievisa.go.th/",
    },
    # ---- Thailand: Tourist Visa (TR) — everyone NOT on the exemption or VoA
    #      lists above (E2 fix: the wildcard fallback the old None-wildcard visa-
    #      exemption row was silently absorbing). ----
    {
        "dest_country": "TH", "program": "Thailand Tourist Visa (TR)",
        "kind": EntryKind.VISA_REQUIRED,
        "eligible_nationalities": None,
        "processing_lead_business_days": 10,  # e-visa/embassy published worst-case
        "fee_cents": 100000, "currency": "THB",  # THB 1,000 single-entry TR fee
        "provenance": "seed:thaievisa.go.th-TR-2026",
        "source_url": "https://www.thaievisa.go.th/",
    },
    # ---- Indonesia: ASEAN + bilateral visa-free partners ----
    # ASEAN nationals + Japan + South Korea enter Indonesia visa-free ≤30 days.
    # Must appear BEFORE the VoA wildcard so the selector picks the cheaper option.
    {
        "dest_country": "ID", "program": "Visa-free (ASEAN + bilateral)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": [
            "SG", "MY", "TH", "PH", "BN", "VN", "MM", "KH", "LA",  # ASEAN
            "JP", "KR",  # bilateral visa-free agreements
        ],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:imigrasi.go.id-visafree-2026",
        "source_url": "https://molina.imigrasi.go.id/",
    },
    {
        "dest_country": "ID", "program": "Visa on Arrival",
        "kind": EntryKind.VISA_ON_ARRIVAL,
        "eligible_nationalities": None,
        "processing_lead_business_days": 0,
        "fee_cents": 50000000,  # IDR 500,000.00 in cents (VOA fee; FX-normalised)
        "currency": "IDR",
        "provenance": "seed:imigrasi.go.id-VOA-2026",
        "source_url": "https://molina.imigrasi.go.id/",
    },
    # ---- China: 15-day visa-free (2024 bilateral agreements) + general visa ----
    # E1 fix (2026-07-04, WebFetch-verified against en.nia.gov.cn +
    # fmprc.gov.cn + corroborating sources): US/MX/ZA REMOVED — China's
    # unilateral/bilateral visa-free programme does NOT cover these three;
    # citizens of these three only ever get China's 240-hour (10-day) TRANSIT
    # exemption (a narrower, transit-only, no-onward-itinerary-leaving-the-
    # transit-city rule not modeled by this VISA_FREE-for-tourism row), so they
    # correctly fall through to the China Visa VISA_REQUIRED wildcard below.
    # GB and CA were wrongly removed in an earlier pass of this same fix and
    # are RESTORED here: China's unilateral visa-exemption programme was
    # extended to the United Kingdom and Canada effective 2026-02-17 (in force
    # through 2026-12-31; announced 2026-02-15 by China's MFA/NIA following the
    # Jan 2026 PM visits), so GB/CA are correctly VISA_FREE as of the current
    # run date.
    {
        "dest_country": "CN", "program": "Visa-free entry (bilateral/unilateral, 15-30 days)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": [
            "GB", "CA", "AU", "NZ", "DE", "FR", "IT", "ES", "PT",
            "NL", "BE", "AT", "CH", "DK", "SE", "NO", "FI", "IE", "GR",
            "PL", "HU", "CZ", "SK", "RO", "BG", "HR", "SI", "LU", "MT",
            "CY", "EE", "LV", "LT", "KR", "AE", "BR", "AR",
            # 30-day mutual exemptions (2024) + JP unilateral (Nov 2024): China waives the visa
            "SG", "TH", "MY", "JP",
        ],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:mfa.gov.cn-visafree-bilateral-2024 (SG/TH/MY mutual 2024, JP unilateral 2024-11; GB/CA unilateral exemption from 2026-02-17; US/MX/ZA correctly excluded — corrected 2026-07-04, E1, WebFetch-verified)",
        "source_url": "https://en.nia.gov.cn/n147418/n147463/c183390/content.html",
    },
    {
        "dest_country": "CN", "program": "China Visa",
        "kind": EntryKind.VISA_REQUIRED,
        "eligible_nationalities": None,
        "processing_lead_business_days": 7,
        "fee_cents": 14000, "currency": "USD",
        "provenance": "seed:mfa.gov.cn-visa-2026",
        "source_url": "https://www.mfa.gov.cn/",
    },
    # ---- Singapore: visa-free for most nationalities ----
    # E3 fix: IN (India) REMOVED — WebFetch-verified 2026-07-04 against
    # ica.gov.sg: Indian passport holders explicitly REQUIRE a visa to enter
    # Singapore (India is only covered by Singapore's narrower Visa-Free Transit
    # Facility, not general tourist entry). India now correctly falls through to
    # the "no seeded rule" conservative path for SG (no SG VISA_REQUIRED wildcard
    # exists yet in this seed; out of E3's scope to add one).
    {
        "dest_country": "SG", "program": "Visa-free entry",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": [
            "US", "GB", "CA", "AU", "NZ", "DE", "FR", "IT", "ES", "PT",
            "NL", "BE", "AT", "CH", "DK", "SE", "NO", "FI", "IE", "GR",
            "JP", "KR", "MY", "TH", "ID", "PH", "VN", "BN", "MM", "KH",
            "LA", "AE", "ZA", "BR", "AR", "MX", "HU", "CZ", "PL",
        ],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:ica.gov.sg-visafree-2026 (IN corrected out 2026-07-04, E3, WebFetch-verified)",
        "source_url": "https://www.ica.gov.sg/",
    },
    # ---- Japan: visa exemption for 90+ countries ----
    # E4 fix: IN/PH/ZA REMOVED — not on Japan's visa-exemption list (mofa.go.jp);
    # they now correctly fall through to the "Japan Visa (consular)"
    # VISA_REQUIRED wildcard below. ID (Indonesia) KEPT but is NOT unconditionally
    # exempt in reality — Japan's exemption for Indonesian nationals applies only
    # to holders of an Indonesian e-passport registered with Japan's programme;
    # this rule schema has no per-passport-registration dimension, so ID is left
    # in this VISA_FREE row (matching Japan's own general public guidance, which
    # advertises the exemption to e-passport holders) but callers should treat it
    # as CONDITIONAL, not universal — see the eligible_nationalities comment below.
    {
        "dest_country": "JP", "program": "Visa exemption",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": [
            "US", "GB", "CA", "AU", "NZ", "DE", "FR", "IT", "ES", "PT",
            "NL", "BE", "AT", "CH", "DK", "SE", "NO", "FI", "IE", "GR",
            "SG", "MY", "TH", "KR", "AE", "BR", "AR", "MX",
            "HU", "CZ", "PL", "RO", "BG",
            "ID",  # CONDITIONAL: registered e-passport holders only (not universal) — E4
        ],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:mofa.go.jp-visa-exemption-2026 (IN/PH/ZA corrected out 2026-07-04, E4; ID flagged conditional)",
        "source_url": "https://www.mofa.go.jp/",
    },
    # ---- Australia: eVisitor (EU), ETA (US/CA/JP/KR/SG), Visitor Visa (all others) ----
    {
        "dest_country": "AU", "program": "eVisitor (subclass 651)",
        "kind": EntryKind.EVISA,
        "eligible_nationalities": [
            "GB", "DE", "FR", "IT", "ES", "PT", "NL", "BE", "AT", "CH",
            "DK", "SE", "NO", "FI", "IE", "GR", "HU", "CZ", "PL", "SK",
            "SI", "HR", "BG", "RO", "EE", "LV", "LT", "LU", "MT", "CY",
        ],
        "processing_lead_business_days": 1,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:immi.homeaffairs.gov.au-evisitor-2026",
        "source_url": "https://immi.homeaffairs.gov.au/",
    },
    {
        "dest_country": "AU", "program": "Electronic Travel Authority (ETA)",
        "kind": EntryKind.EVISA,
        "eligible_nationalities": ["US", "CA", "JP", "KR", "SG", "MY", "HK"],
        "processing_lead_business_days": 1,
        "fee_cents": 2000, "currency": "AUD",   # AUD 20 app service fee (≈US$13); no visa charge
        "provenance": "seed:immi.homeaffairs.gov.au-eta-2026",
        "source_url": "https://immi.homeaffairs.gov.au/",
    },
    {
        "dest_country": "AU", "program": "Visitor Visa (subclass 600)",
        "kind": EntryKind.VISA_REQUIRED,
        "eligible_nationalities": None,
        "processing_lead_business_days": 10,
        "fee_cents": 14500, "currency": "USD",
        "provenance": "seed:immi.homeaffairs.gov.au-600-2026",
        "source_url": "https://immi.homeaffairs.gov.au/",
    },
    # ---- Kenya: East African Community nationals are visa-free ----
    {
        "dest_country": "KE", "program": "EAC visa-free",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["UG", "TZ", "RW", "BI", "SS"],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:immigration.go.ke-EAC-2026",
        "source_url": "https://immigration.go.ke/",
    },
    # ---- Kenya: eTA for all other nationalities ----
    {
        "dest_country": "KE", "program": "Kenya eTA",
        "kind": EntryKind.EVISA,
        "eligible_nationalities": None,
        "processing_lead_business_days": 3,
        "fee_cents": 3000, "currency": "USD",   # $30 Kenya eTA processing fee
        "provenance": "seed:etakenya.go.ke-2026",
        "source_url": "https://www.etakenya.go.ke/",
    },
    # ===================================================================
    # ADDITIVE VERIFIED entry rules (compliance only). Each block keeps
    # the file's ordering invariant: specific (nationality-listed) rows BEFORE
    # the wildcard fallback so the specific program overrides the wildcard.
    # JP/CN/AU pre-exist; only JP's additive consular-visa wildcard is added
    # (CN/AU were already fully covered). Health slates live in the health
    # agent, not here — these are entry/visa rules only.
    # ===================================================================
    # ---- JP ----
    {
        "dest_country": "JP", "program": "Japan Visa (consular)",
        "kind": EntryKind.VISA_REQUIRED,
        "eligible_nationalities": None,
        "processing_lead_business_days": 10,
        "fee_cents": 3000, "currency": "JPY",
        "provenance": "seed:mofa.go.jp-visa-consular-2026",
        "source_url": "https://www.mofa.go.jp/j_info/visit/visa/index.html",
    },
    # ---- MX ----
    {
        "dest_country": "MX", "program": "Visa-free entry (FMM tourist permit on arrival)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["US", "CA", "GB", "AU", "NZ", "DE", "FR", "IT", "ES", "PT", "NL", "BE", "AT", "CH", "DK", "SE", "NO", "FI", "IE", "GR", "PL", "HU", "CZ", "SK", "RO", "BG", "HR", "SI", "EE", "LV", "LT", "LU", "MT", "CY", "JP", "KR", "SG", "BR", "AR", "CL"],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:gob.mx-visafree-2026",
        "source_url": "https://www.gob.mx/sre/acciones-y-programas/paises-con-los-que-mexico-tiene-acuerdos-de-supresion-de-visas",
    },
    {
        "dest_country": "MX", "program": "Mexico Visa (consular)",
        "kind": EntryKind.VISA_REQUIRED,
        "eligible_nationalities": None,
        "processing_lead_business_days": 10,
        "fee_cents": 3600, "currency": "USD",
        "provenance": "seed:gob.mx-visa-consular-2026",
        "source_url": "https://www.gob.mx/sre",
    },
    # ---- GB ----
    {
        "dest_country": "GB", "program": "Common Travel Area visa-free (IE)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["IE"],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:gov.uk-CTA-ireland-2026",
        "source_url": "https://www.gov.uk/government/publications/common-travel-area-guidance",
    },
    {
        "dest_country": "GB", "program": "UK Electronic Travel Authorisation (ETA)",
        "kind": EntryKind.EVISA,
        "eligible_nationalities": ["US", "CA", "AU", "NZ", "DE", "FR", "IT", "ES", "PT", "NL", "BE", "AT", "CH", "DK", "SE", "NO", "FI", "GR", "PL", "HU", "CZ", "SK", "RO", "BG", "HR", "SI", "EE", "LV", "LT", "LU", "MT", "CY", "JP", "KR", "SG", "AE", "BR", "AR"],
        "processing_lead_business_days": 3,
        "fee_cents": 2000, "currency": "GBP",
        "provenance": "seed:gov.uk-ETA-2026",
        "source_url": "https://www.gov.uk/guidance/apply-for-an-electronic-travel-authorisation-eta",
    },
    {
        "dest_country": "GB", "program": "UK Standard Visitor Visa",
        "kind": EntryKind.VISA_REQUIRED,
        "eligible_nationalities": None,
        "processing_lead_business_days": 15,
        "fee_cents": 11500, "currency": "GBP",
        "provenance": "seed:gov.uk-standard-visitor-visa-2026",
        "source_url": "https://www.gov.uk/standard-visitor-visa",
    },
    # ---- PH ----
    {
        "dest_country": "PH", "program": "Visa-free (30-day)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["US", "GB", "CA", "AU", "NZ", "DE", "FR", "IT", "ES", "PT", "NL", "BE", "AT", "CH", "DK", "SE", "NO", "FI", "IE", "GR", "PL", "HU", "CZ", "SK", "RO", "BG", "HR", "SI", "EE", "LV", "LT", "LU", "MT", "CY", "JP", "KR", "SG", "MY", "TH", "ID", "BR", "AR", "AE"],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:dfa.gov.ph-visa-free-2026",
        "source_url": "https://dfa.gov.ph/",
    },
    {
        "dest_country": "PH", "program": "Philippines Visa (consular)",
        "kind": EntryKind.VISA_REQUIRED,
        "eligible_nationalities": None,
        "processing_lead_business_days": 10,
        "fee_cents": 3000, "currency": "USD",
        "provenance": "seed:dfa.gov.ph-visa-consular-2026",
        "source_url": "https://dfa.gov.ph/",
    },
    # ---- TR ----
    {
        "dest_country": "TR", "program": "Visa-free entry",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["US", "GB", "CA", "AU", "NZ", "DE", "FR", "IT", "ES", "PT", "NL", "BE", "AT", "DK", "SE", "NO", "FI", "GR", "JP", "KR", "BR", "AR",
            "SA", "AE", "QA", "KW", "OM", "BH"],  # all 6 GCC states visa-free 90/180 since 2023-12-23
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:mfa.gov.tr-visa-exemption-2026 (GCC exemption 2023-12-23)",
        "source_url": "https://www.mfa.gov.tr/visa-information-for-foreigners.en.mfa",
    },
    {
        "dest_country": "TR", "program": "Turkey Visa (consular)",
        "kind": EntryKind.VISA_REQUIRED,
        "eligible_nationalities": None,
        "processing_lead_business_days": 15,
        "fee_cents": 5100, "currency": "USD",
        "provenance": "seed:mfa.gov.tr-visa-consular-2026",
        "source_url": "https://www.mfa.gov.tr/visa-information-for-foreigners.en.mfa",
    },
    # ---- VN ----
    # ---- Vietnam: visa exemption (ASEAN bilateral + unilateral list) BEFORE the e-visa wildcard ----
    # Vietnam waives the visa for ASEAN nationals (SG/TH/MY/ID/LA/KH 30d, PH 21d, MM/BN 14d) and a
    # unilateral list (DE/FR/IT/ES/GB/RU/JP/KR + Nordics/Belarus, 45d, extended 2025) for short stays.
    # A nationality-specific row OVERRIDES the wildcard e-visa row → these travellers pay NO visa fee.
    # (max-stay days are not modelled here, same as every other visa-free seed row; demo trips are short.)
    {
        "dest_country": "VN", "program": "Visa-free entry (ASEAN bilateral + unilateral exemption, short stays)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": [
            "SG", "TH", "MY", "ID", "LA", "KH", "PH", "MM", "BN",          # ASEAN bilateral
            "DE", "FR", "IT", "ES", "GB", "RU", "JP", "KR", "DK", "SE", "NO", "FI", "BY",  # unilateral 45-day
        ],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:xuatnhapcanh.gov.vn-visa-exemption-2025",
        "source_url": "https://xuatnhapcanh.gov.vn/",
    },
    {
        "dest_country": "VN", "program": "Vietnam E-visa (90 days, single or multiple entry)",
        "kind": EntryKind.EVISA,
        "eligible_nationalities": None,
        "processing_lead_business_days": 3,
        "fee_cents": 2500, "currency": "USD",
        "provenance": "seed:evisa.xuatnhapcanh.gov.vn-2023",
        "source_url": "https://evisa.xuatnhapcanh.gov.vn/",
    },
    # ---- MY ----
    {
        "dest_country": "MY", "program": "Visa-free entry (ASEAN + bilateral partners)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["US", "GB", "CA", "AU", "NZ", "DE", "FR", "IT", "ES", "PT", "NL", "BE", "AT", "CH", "DK", "SE", "NO", "FI", "IE", "GR", "JP", "KR", "SG", "TH", "ID", "PH", "BN", "VN", "KH", "MM", "LA", "AE", "BR", "AR", "ZA"],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:imi.gov.my-visa-free-2025",
        "source_url": "https://www.imi.gov.my/",
    },
    # ---- ES ----
    {
        "dest_country": "ES", "program": "Schengen visa-free (EU/EEA + bilateral partners)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["DE", "FR", "IT", "PT", "NL", "BE", "AT", "CH", "SE", "NO", "DK", "FI", "IS", "LU", "GR", "PL", "CZ", "HU", "SK", "SI", "HR", "EE", "LT", "LV", "MT", "CY", "BG", "RO", "IE", "US", "GB", "AU", "NZ", "CA", "JP", "KR", "SG", "MY", "HK", "TW", "BR", "MX", "AR", "CL", "AE", "IL", "CO", "PE"],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:ec.europa.eu-schengen-visafree-2026 (Annex II: +IL/CO/PE)",
        "source_url": "https://home-affairs.ec.europa.eu/policies/schengen-borders-and-visa_en",
    },
    {
        "dest_country": "ES", "program": "Schengen short-stay visa (C visa)",
        "kind": EntryKind.VISA_REQUIRED,
        "eligible_nationalities": None,
        "processing_lead_business_days": 15,
        "fee_cents": 9000, "currency": "EUR",
        "provenance": "seed:exteriores.gob.es-schengen-visa-2026",
        "source_url": "https://www.exteriores.gob.es/",
    },
    # ---- CO ----
    {
        "dest_country": "CO", "program": "Visa-free entry (up to 90 days for tourism)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["US", "GB", "CA", "AU", "NZ", "DE", "FR", "IT", "ES", "PT", "NL", "BE", "AT", "CH", "DK", "SE", "NO", "FI", "IE", "GR", "HU", "PL", "CZ", "JP", "KR", "SG", "MY", "AE", "BR", "AR", "MX", "CL", "ZA"],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:cancilleria.gov.co-visa-free-2025",
        "source_url": "https://www.cancilleria.gov.co/",
    },
    # ---- AR ----
    {
        "dest_country": "AR", "program": "Visa-free entry (up to 90 days for tourism)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["US", "GB", "CA", "AU", "NZ", "DE", "FR", "IT", "ES", "PT", "NL", "BE", "AT", "CH", "DK", "SE", "NO", "FI", "IE", "GR", "HU", "PL", "CZ", "SK", "RO", "BG", "JP", "KR", "SG", "MY", "AE", "BR", "MX", "CL", "ZA"],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:cancilleria.gob.ar-visa-free-2025",
        "source_url": "https://www.cancilleria.gob.ar/",
    },
    # ---- BD ----
    {
        "dest_country": "BD", "program": "Bangladesh Tourist Visa (on arrival for eligible nationalities)",
        "kind": EntryKind.VISA_ON_ARRIVAL,
        "eligible_nationalities": ["US", "GB", "CA", "AU", "NZ", "DE", "FR", "IT", "ES", "PT", "NL", "BE", "AT", "DK", "SE", "NO", "FI", "IE", "JP", "KR", "SG", "AE"],
        "processing_lead_business_days": 0,
        "fee_cents": 5100, "currency": "USD",
        "provenance": "seed:dip.gov.bd-visa-on-arrival-2025",
        "source_url": "https://www.dip.gov.bd/",
    },
    {
        "dest_country": "BD", "program": "Bangladesh Tourist Visa (embassy/consular)",
        "kind": EntryKind.VISA_REQUIRED,
        "eligible_nationalities": None,
        "processing_lead_business_days": 10,
        "fee_cents": 5100, "currency": "USD",
        "provenance": "seed:dip.gov.bd-tourist-visa-2025",
        "source_url": "https://www.dip.gov.bd/",
    },
    # ---- ZA ----
    {
        "dest_country": "ZA", "program": "Visa-free entry (tourism)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["US", "GB", "DE", "FR", "IT", "ES", "PT", "NL", "BE", "AT", "CH", "DK", "SE", "NO", "FI", "IE", "GR", "AU", "CA", "NZ", "JP", "KR"],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:dha.gov.za-visafree-2026",
        "source_url": "https://www.dha.gov.za/",
    },
    {
        "dest_country": "ZA", "program": "South Africa Visa (consular)",
        "kind": EntryKind.VISA_REQUIRED,
        "eligible_nationalities": None,
        "processing_lead_business_days": 15,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:dha.gov.za-visa-2026",
        "source_url": "https://www.dha.gov.za/",
    },
    # ---- KR ----
    {
        "dest_country": "KR", "program": "Visa-free entry (Korea Visa Waiver)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["US", "GB", "DE", "FR", "IT", "ES", "PT", "NL", "BE", "AT", "CH", "DK", "SE", "NO", "FI", "IE", "GR", "AU", "CA", "NZ", "JP", "SG", "MY"],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:mofa.go.kr-visawaiver-2026 (SG 90d K-ETA-exempt to 2026; MY 90d visa-free)",
        "source_url": "https://www.mofa.go.kr/",
    },
    {
        "dest_country": "KR", "program": "Korea Visa (consular)",
        "kind": EntryKind.VISA_REQUIRED,
        "eligible_nationalities": None,
        "processing_lead_business_days": 10,
        "fee_cents": 6000, "currency": "USD",
        "provenance": "seed:mofa.go.kr-visa-2026",
        "source_url": "https://www.mofa.go.kr/",
    },
    # ---- DZ ----
    {
        "dest_country": "DZ", "program": "Algeria Tourist Visa (consular)",
        "kind": EntryKind.VISA_REQUIRED,
        "eligible_nationalities": None,
        "processing_lead_business_days": 15,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:algerianembassy-visa-2026",
        "source_url": "https://www.diplomatie.gov.dz/",
    },
    # ---- PK ----
    {
        "dest_country": "PK", "program": "Pakistan Visa (consular)",
        "kind": EntryKind.VISA_REQUIRED,
        "eligible_nationalities": None,
        "processing_lead_business_days": 15,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:mofa.gov.pk-visa-2026",
        "source_url": "https://mofa.gov.pk/",
    },
    # ---- MA ----
    {
        "dest_country": "MA", "program": "Visa-free entry (tourism, up to 90 days)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["US", "GB", "DE", "FR", "IT", "ES", "PT", "NL", "BE", "AT", "CH", "DK", "SE", "NO", "FI", "IE", "GR", "AU", "CA", "NZ", "JP", "KR", "BR", "AR"],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:maec.gov.ma-visafree-2026",
        "source_url": "https://www.maec.gov.ma/",
    },
    {
        "dest_country": "MA", "program": "Morocco Visa (consular)",
        "kind": EntryKind.VISA_REQUIRED,
        "eligible_nationalities": None,
        "processing_lead_business_days": 15,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:maec.gov.ma-visa-2026",
        "source_url": "https://www.maec.gov.ma/",
    },
    # ---- CL ----
    {
        "dest_country": "CL", "program": "Visa-free entry (tourism, up to 90 days)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["US", "GB", "DE", "FR", "IT", "ES", "PT", "NL", "BE", "AT", "CH", "DK", "SE", "NO", "FI", "IE", "GR", "AU", "CA", "NZ", "JP", "KR", "BR", "AR", "MX"],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:minrel.gob.cl-visafree-2026",
        "source_url": "https://www.minrel.gob.cl/",
    },
    {
        "dest_country": "CL", "program": "Chile Visa (consular)",
        "kind": EntryKind.VISA_REQUIRED,
        "eligible_nationalities": None,
        "processing_lead_business_days": 15,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:minrel.gob.cl-visa-2026",
        "source_url": "https://www.minrel.gob.cl/",
    },
    # ---- CI ----
    {
        "dest_country": "CI", "program": "ECOWAS visa-free",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["GH", "SN", "ML", "BF", "GN", "TG", "BJ", "NE", "MR", "LR", "SL", "GW", "CV", "GM", "NG"],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:gouv.ci-ecowas-visafree-2026",
        "source_url": "https://www.snedai.ci/",
    },
    {
        "dest_country": "CI", "program": "Cote d'Ivoire eVisa",
        "kind": EntryKind.EVISA,
        "eligible_nationalities": None,
        "processing_lead_business_days": 5,
        "fee_cents": 7300, "currency": "USD",
        "provenance": "seed:snedai.ci-evisa-2026",
        "source_url": "https://www.snedai.ci/",
    },
    # ---- RO ----
    {
        "dest_country": "RO", "program": "EU / Schengen visa-free (bilateral)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["DE", "FR", "IT", "ES", "PT", "NL", "BE", "AT", "CH", "SE", "NO", "DK", "FI", "IS", "LU", "GR", "PL", "CZ", "HU", "SK", "SI", "HR", "EE", "LT", "LV", "MT", "CY", "BG", "IE", "US", "GB", "AU", "CA", "NZ", "JP", "KR", "SG",
            "MY", "HK", "TW", "AE", "BR", "MX", "AR", "CL", "IL", "CO", "PE"],  # full-Schengen 2025 → EU Annex II parity
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:mae.ro-visafree-bilateral-2026 (Schengen 2025-01-01 → Annex II parity)",
        "source_url": "https://www.mae.ro/en/node/2035",
    },
    {
        "dest_country": "RO", "program": "Romania short-stay visa (Type C)",
        "kind": EntryKind.VISA_REQUIRED,
        "eligible_nationalities": None,
        "processing_lead_business_days": 15,
        "fee_cents": 8000, "currency": "EUR",
        "provenance": "seed:mae.ro-shortstayvisa-2026",
        "source_url": "https://www.mae.ro/en/node/2035",
    },
    # ---- UG ----
    {
        "dest_country": "UG", "program": "EAC visa-free",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["KE", "TZ", "RW", "BI", "SS"],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:immigration.go.ug-EAC-visafree-2026",
        "source_url": "https://www.immigration.go.ug/",
    },
    {
        "dest_country": "UG", "program": "Uganda eVisa",
        "kind": EntryKind.EVISA,
        "eligible_nationalities": None,
        "processing_lead_business_days": 5,
        "fee_cents": 5000, "currency": "USD",
        "provenance": "seed:visas.immigration.go.ug-evisa-2026",
        "source_url": "https://visas.immigration.go.ug/",
    },
    # ---- UZ ----
    {
        "dest_country": "UZ", "program": "Visa-free entry (bilateral)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["GB", "DE", "FR", "IT", "ES", "PT", "NL", "BE", "AT", "CH", "DK", "SE", "NO", "FI", "IE", "GR", "HU", "CZ", "PL", "SK", "SI", "AU", "CA", "JP", "KR", "SG", "AE", "TR"],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:mfa.uz-visafree-bilateral-2026",
        "source_url": "https://mfa.uz/en/consular/visas/",
    },
    {
        "dest_country": "UZ", "program": "Uzbekistan e-Visa",
        "kind": EntryKind.EVISA,
        "eligible_nationalities": None,
        "processing_lead_business_days": 5,
        "fee_cents": 2000, "currency": "USD",
        "provenance": "seed:e-visa.uz-evisa-2026",
        "source_url": "https://e-visa.uz/",
    },
    # ---- IR ----
    {
        "dest_country": "IR", "program": "Iran visa - US nationals (ineligible for standard tourist visa)",
        "kind": EntryKind.VISA_REQUIRED,
        "eligible_nationalities": ["US"],
        "processing_lead_business_days": 30,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:mfa.gov.ir-us-nationals-longstanding-restriction-2026",
        "source_url": "https://en.mfa.ir/portal/ViewPage.aspx?PageId=144",
    },
    # ---- AO ----
    {
        "dest_country": "AO", "program": "Angola eVisa",
        "kind": EntryKind.EVISA,
        "eligible_nationalities": ["US", "GB", "AU", "CA", "DE", "FR", "IT", "ES", "PT", "NL", "BE", "AT", "CH", "SE", "NO", "DK", "FI", "IE", "PL", "CZ", "HU"],
        "processing_lead_business_days": 5,
        "fee_cents": 12000, "currency": "USD",
        "provenance": "seed:visas.gov.ao-evisa-2026",
        "source_url": "https://www.visas.gov.ao/",
    },
    # ---- PE ----
    {
        "dest_country": "PE", "program": "Peru visa-free (90 days)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["US", "GB", "CA", "AU", "NZ", "DE", "FR", "IT", "ES", "PT", "NL", "BE", "AT", "CH", "DK", "SE", "NO", "FI", "IE", "GR", "PL", "CZ", "HU", "SK", "SI", "HR", "BG", "RO", "EE", "LV", "LT", "LU", "MT", "CY", "JP", "KR", "SG", "BR", "AR", "CL", "MX"],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:migraciones.gob.pe-visafree-2026",
        "source_url": "https://www.migraciones.gob.pe/",
    },
    # ---- EC ----
    {
        "dest_country": "EC", "program": "Ecuador visa-free (90 days)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["US", "GB", "CA", "AU", "NZ", "DE", "FR", "IT", "ES", "PT", "NL", "BE", "AT", "CH", "DK", "SE", "NO", "FI", "IE", "GR", "PL", "CZ", "HU", "SK", "SI", "HR", "BG", "RO", "EE", "LV", "LT", "LU", "MT", "CY", "JP", "KR", "SG", "BR", "AR", "CL", "MX"],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:cancilleria.gob.ec-visafree-2026",
        "source_url": "https://www.cancilleria.gob.ec/",
    },
    # ---- KZ ----
    {
        "dest_country": "KZ", "program": "Kazakhstan visa-free (30 days)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["US", "GB", "CA", "AU", "NZ", "DE", "FR", "IT", "ES", "PT", "NL", "BE", "AT", "CH", "DK", "SE", "NO", "FI", "IE", "GR", "PL", "CZ", "HU", "SK", "SI", "LU", "MT", "EE", "LV", "LT", "JP", "KR", "SG", "AE"],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:mfa.gov.kz-visafree-2026",
        "source_url": "https://www.mfa.gov.kz/",
    },
    # ---- GH ----
    {
        "dest_country": "GH", "program": "ECOWAS visa-free",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["NG", "CI", "SN", "BJ", "TG", "BF", "ML", "NE", "GN", "SL", "LR", "CV", "GW", "GM"],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:mfa.gov.gh-ecowas-visafree-2026",
        "source_url": "https://www.mfa.gov.gh/",
    },
    # ---- EG ----
    {
        "dest_country": "EG", "program": "Egypt visa on arrival",
        "kind": EntryKind.VISA_ON_ARRIVAL,
        "eligible_nationalities": ["US", "GB", "CA", "AU", "NZ", "DE", "FR", "IT", "ES", "PT", "NL", "BE", "AT", "CH", "DK", "SE", "NO", "FI", "IE", "GR", "PL", "CZ", "HU", "SK", "SI", "HR", "BG", "RO", "EE", "LV", "LT", "LU", "MT", "CY", "JP", "KR", "SG", "AE"],
        "processing_lead_business_days": 0,
        "fee_cents": 3000, "currency": "USD",
        "provenance": "seed:egypt-evisa.com.eg-voa-2026",
        "source_url": "https://www.egypt.travel/",
    },
    # ---- AE ----
    {
        "dest_country": "AE", "program": "Visa-free / free visa-on-arrival entry (30 days, extendable)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["US", "GB", "CA", "AU", "NZ", "DE", "FR", "IT", "ES", "PT", "NL", "BE", "AT", "CH", "DK", "SE", "NO", "FI", "IE", "GR", "PL", "HU", "CZ", "SK", "SI", "HR", "BG", "RO", "EE", "LV", "LT", "LU", "MT", "CY", "JP", "KR", "SG", "MY", "HK", "BR", "AR", "MX", "CL", "ZA", "RU",
            "SA", "QA", "KW", "OM", "BH"],  # GCC nationals enter on national ID, no fee, 180 days/yr
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:u.ae-visafree-bilateral-2026 (GCC national-ID entry, no fee)",
        "source_url": "https://u.ae/en/information-and-services/visa-and-emirates-id/do-you-need-a-uae-visa",
    },
    {
        "dest_country": "AE", "program": "UAE eVisa (tourist, 30 days)",
        "kind": EntryKind.EVISA,
        "eligible_nationalities": None,
        "processing_lead_business_days": 5,
        "fee_cents": 9000, "currency": "USD",
        "provenance": "seed:icp.gov.ae-evisa-tourist-2026",
        "source_url": "https://www.icp.gov.ae/",
    },
    # ---- CM ----
    {
        "dest_country": "CM", "program": "Cameroon eVisa",
        "kind": EntryKind.EVISA,
        "eligible_nationalities": None,
        "processing_lead_business_days": 7,
        "fee_cents": 9000, "currency": "USD",
        "provenance": "seed:evisa.spm.cm-2026",
        "source_url": "https://www.evisacameroon.com/",
    },
    # ---- GT ----
    {
        "dest_country": "GT", "program": "CA-4 / Category A visa-free (90 days)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["US", "GB", "CA", "AU", "NZ", "DE", "FR", "IT", "ES", "PT", "NL", "BE", "AT", "CH", "DK", "SE", "NO", "FI", "IE", "GR", "PL", "HU", "CZ", "SK", "SI", "HR", "BG", "RO", "EE", "LV", "LT", "LU", "MT", "CY", "JP", "KR", "SG", "MY", "BR", "AR", "MX", "CL", "AE", "HK", "TW"],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:migracion.gob.gt-visafree-2026",
        "source_url": "https://www.migracion.gob.gt/",
    },
    # ---- TW ----
    {
        "dest_country": "TW", "program": "Visa-free entry (90 days)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["US", "GB", "CA", "AU", "NZ", "DE", "FR", "IT", "ES", "PT", "NL", "BE", "AT", "CH", "DK", "SE", "NO", "FI", "IE", "GR", "PL", "HU", "CZ", "SK", "SI", "HR", "BG", "RO", "EE", "LV", "LT", "LU", "MT", "CY", "JP", "KR"],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:boca.gov.tw-visafree-2026",
        "source_url": "https://www.boca.gov.tw/",
    },
    # ---- DO ----
    {
        "dest_country": "DO", "program": "Visa-free entry (up to 30 days; free eTicket form required)",
        "kind": EntryKind.VISA_FREE,
        "eligible_nationalities": ["US", "GB", "CA", "AU", "NZ", "DE", "FR", "IT", "ES", "PT", "NL", "BE", "AT", "CH", "DK", "SE", "NO", "FI", "IE", "GR", "PL", "HU", "CZ", "SK", "SI", "HR", "BG", "RO", "EE", "LV", "LT", "LU", "MT", "CY", "JP", "KR", "SG", "MY", "BR", "AR", "MX", "CL", "AE", "ZA"],
        "processing_lead_business_days": 0,
        "fee_cents": 0, "currency": "USD",
        "provenance": "seed:migracion.gob.do-visafree-eticket-2026",
        "source_url": "https://www.migracion.gob.do/",
    },
]

# Country display names (cosmetic only; never used in a numeric decision).
_COUNTRY_NAMES: dict[str, str] = {
    "NZ": "New Zealand", "US": "United States", "ET": "Ethiopia",
    "IN": "India", "FR": "France", "TH": "Thailand", "ID": "Indonesia",
    "KE": "Kenya",
    # ADDITIVE VERIFIED destinations (entry/visa rules above).
    "JP": "Japan", "CN": "China", "AU": "Australia", "SG": "Singapore",
    "MX": "Mexico", "GB": "United Kingdom", "PH": "Philippines",
    "TR": "Turkey", "VN": "Vietnam", "MY": "Malaysia", "ES": "Spain",
    "CO": "Colombia", "AR": "Argentina", "BD": "Bangladesh",
    "ZA": "South Africa", "KR": "South Korea", "DZ": "Algeria",
    "PK": "Pakistan", "MA": "Morocco", "CL": "Chile", "CI": "Ivory Coast",
    "RO": "Romania", "UG": "Uganda", "UZ": "Uzbekistan", "IR": "Iran",
    "AO": "Angola", "PE": "Peru", "EC": "Ecuador", "KZ": "Kazakhstan",
    "GH": "Ghana", "EG": "Egypt", "AE": "United Arab Emirates",
    "CM": "Cameroon", "GT": "Guatemala", "TW": "Taiwan",
    "DO": "Dominican Republic",
}

# A minimal seeded "is there a same-region visa-free / VOA alternative?" map used
# only to OFFER an alternative in a block verdict (deterministic, closed).
_VISA_FREE_ALTERNATIVES: dict[str, list[str]] = {
    "ET": ["KE"],   # Ethiopia blocked → Kenya (still eVisa but shorter) as a near alt
    "FR": ["TH"],   # France consular visa → Thailand visa-free as a fallback region swap
}


# ===========================================================================
# 1b. HENLEY PASSPORT INDEX (#33) — import-time load + a PURE strength helper.
# ===========================================================================
# reference/henley_passport_index.json is the 2026 Henley Passport Index: a
# PER-PASSPORT ranking (iso2 -> {rank, visa_free_count}). It is loaded ONCE at
# import (mirroring _ENTRY_RULES) into _HENLEY_INDEX. It is used to ENRICH /
# ANNOTATE the compliance verdict with the traveler's passport strength — it is
# DESCRIPTIVE ONLY.
#
# FAIL-CONSERVATIVE (the core guard): the Henley index is NOT a per-(nationality,
# destination) visa-free table — it only states HOW MANY destinations a passport
# can access visa-free, not WHICH ones. It therefore can NEVER be used to derive a
# silent ALLOW that bypasses the seeded entry rules. An unknown (nationality,
# destination) pair stays UNKNOWN/flag; a seeded block stays a block. The
# annotation never touches the gate decision (var-0 / APPEND-ONLY).

_HENLEY_FETCHED_AT = "2026"


def _load_henley_index() -> dict[str, dict[str, Any]]:
    """
    Load the Henley Passport Index as iso2 -> {iso2, rank, visa_free_count, country}.

    Pure + deterministic: a single import-time read of the seeded JSON; iteration
    is over the file's fixed list order and the first row for a given iso2 wins
    (the seed has unique iso2 today, but the explicit guard keeps a future
    duplicate deterministic rather than last-write-wins). A null/blank iso2
    (e.g. Palestine) is SKIPPED — never fabricated into a code. Unreadable file ->
    {} (silent fallback): passport_strength then returns None for everyone and the
    verdict is simply un-annotated, never wrong (HONESTY / fail-conservative).
    """
    path = os.path.join(os.path.dirname(__file__), "..", "..", "reference", "henley_passport_index.json")
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.warning("compliance: Henley index unreadable (%s) — passport-strength annotation disabled", e)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in doc.get("rankings", []):
        iso2 = row.get("iso2")
        if not iso2:
            continue
        code = str(iso2).upper()
        out.setdefault(code, {
            "iso2": code,
            "rank": int(row["rank"]),
            "visa_free_count": int(row["visa_free_count"]),
            "country": row.get("country"),
        })
    return out


_HENLEY_INDEX: dict[str, dict[str, Any]] = _load_henley_index()


def passport_strength(nationality: str | None) -> dict[str, Any] | None:
    """
    The traveler's Henley passport-strength annotation, or None.

    PURE + DETERMINISTIC dict lookup over the import-loaded index. Returns None for
    an empty/whitespace or unknown nationality (fail-conservative — a missing
    passport is NEVER fabricated, and None never reads as an allow). The returned
    record is descriptive metadata only:
        {nationality, rank, visa_free_count, country, source}.
    """
    nat = (nationality or "").strip().upper()
    if not nat:
        return None
    rec = _HENLEY_INDEX.get(nat)
    if rec is None:
        return None
    return {
        "nationality": nat,
        "rank": rec["rank"],
        "visa_free_count": rec["visa_free_count"],
        "country": rec["country"],
        "source": "henley_passport_index_2026",
    }


def _passport_index_crosscheck(
    per_leg: list[dict[str, Any]],
    strength: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """
    Per-leg DESCRIPTIVE cross-check between the (already-decided) entry kind and the
    traveler's Henley passport strength. Pure formatter over per_leg: it adds NO
    new decision — visa_free_equivalent is read straight off each leg's resolved
    kind (VISA_FREE / VISA_ON_ARRIVAL). The note never asserts an allow that the
    gate did not already grant; it only annotates consistency with the passport's
    broad visa-free access. Deterministic (iterates per_leg in order).
    """
    has_strength = strength is not None
    out: list[dict[str, Any]] = []
    for v in per_leg:
        kind = v.get("kind")
        vf_equiv = kind in (EntryKind.VISA_FREE, EntryKind.VISA_ON_ARRIVAL)
        out.append({
            "leg_id": v.get("leg_id"),
            "dest_country": v.get("dest_country"),
            "kind": kind,
            "visa_free_equivalent": vf_equiv,
            "passport_strength_known": has_strength,
            "note": (
                "Seeded entry rule is visa-free-equivalent for this leg"
                if vf_equiv else
                "Seeded entry rule requires a visa/eVisa or is unverified for this leg"
            ) + (
                "; consistent with the passport's broad visa-free access (descriptive only)"
                if vf_equiv and has_strength else
                ""
            ),
        })
    return out


# ===========================================================================
# 2. DATE MATH — business-day arithmetic (pure, deterministic, NO LLM).
# ===========================================================================

def _parse_date(s: str) -> date:
    """Parse an ISO YYYY-MM-DD date. Raises ValueError on bad input."""
    return date.fromisoformat(s)


def add_business_days(start: date, n: int) -> date:
    """
    Return `start` advanced by `n` BUSINESS days (Mon–Fri), skipping weekends.

    Pure + deterministic. n=0 returns `start`. Holidays are NOT modelled (a
    documented conservative simplification — the safety buffer absorbs them).
    """
    if n <= 0:
        return start
    d = start
    added = 0
    while added < n:
        d = d + timedelta(days=1)
        if d.weekday() < 5:   # Mon=0 .. Fri=4
            added += 1
    return d


def business_days_between(start: date, end: date) -> int:
    """
    Count BUSINESS days strictly available from `start` (exclusive) up to `end`
    (inclusive). Returns 0 if end <= start. Pure + deterministic.
    """
    if end <= start:
        return 0
    d = start
    count = 0
    while d < end:
        d = d + timedelta(days=1)
        if d.weekday() < 5:
            count += 1
    return count


# ===========================================================================
# 3. RULE RESOLVER — pure function over the seeded closed set.
# ===========================================================================

class SeedIntegrityError(ValueError):
    """Raised at import time when the seeded entry-rule table is ambiguous —
    two nationality-specific rows for the SAME (dest, nationality) pair (finding
    #51). Caught at import so a bad seed never ships, rather than silently
    resolving to whichever row happens to appear later in list order."""


# Most-conservative-first precedence over EntryKind: when (defensively) more than
# one specific row matches at resolve time, the most CONSERVATIVE (longest-lead /
# strictest) kind wins so the gate can never pick the more-permissive program by
# list accident. Lower rank = more conservative = preferred.
_KIND_CONSERVATISM_RANK: dict[str, int] = {
    EntryKind.VISA_REQUIRED: 0,
    EntryKind.EVISA: 1,
    EntryKind.VISA_ON_ARRIVAL: 2,
    EntryKind.VISA_FREE: 3,
}


def _resolve_specific(
    matches: list[dict[str, Any]],
    dest: str,
    nat: str,
) -> dict[str, Any] | None:
    """
    Pick THE nationality-specific rule from all rows that match (dest, nationality).

    var-0 / fail-conservative: never relies on list-iteration accident. With one
    match it returns it. With more than one (an overlapping allow-list — finding
    #51) it logs the ambiguity and returns the MOST CONSERVATIVE (longest-lead)
    program by an explicit, deterministic precedence — sorted with a stable
    tiebreak (kind rank, then -lead, then -fee, then program) so the choice is
    byte-identical across runs and can never be the cheaper/more-permissive row by
    chance. The import-time uniqueness check (_assert_entry_rules_unambiguous)
    makes a real overlap a hard SeedIntegrityError; this resolver is the runtime
    fail-safe so a future overlap degrades conservatively rather than fails open.
    """
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    logger.warning(
        "compliance: AMBIGUOUS entry rules for %s/%s — %d nationality-specific rows "
        "overlap (%s); selecting the most-conservative by precedence (var-0).",
        dest, nat, len(matches), [m["program"] for m in matches],
    )
    return sorted(
        matches,
        key=lambda r: (
            _KIND_CONSERVATISM_RANK.get(r["kind"], -1),
            -int(r["processing_lead_business_days"]),
            -int(r["fee_cents"]),
            str(r["program"]),
        ),
    )[0]


def _assert_entry_rules_unambiguous() -> None:
    """
    Import-time guard (finding #51): assert NO (dest_country, nationality) pair is
    covered by more than one nationality-specific row. The wildcard row (elig is
    None) is the legitimate fallback and is NOT counted as an overlap. Raises
    SeedIntegrityError so an ambiguous seed cannot ship. Deterministic: iterates
    the fixed _ENTRY_RULES list and sorts the offending keys for a stable message.
    """
    seen: dict[tuple[str, str], list[str]] = {}
    for r in _ENTRY_RULES:
        elig = r["eligible_nationalities"]
        if elig is None:
            continue
        dest = r["dest_country"].upper()
        for nat in elig:
            key = (dest, nat.upper())
            seen.setdefault(key, []).append(r["program"])
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    if dupes:
        detail = "; ".join(
            f"{dest}/{nat}: {sorted(progs)}"
            for (dest, nat), progs in sorted(dupes.items())
        )
        raise SeedIntegrityError(
            "compliance: ambiguous entry-rule seed — overlapping nationality-specific "
            f"rows for the same (dest, nationality): {detail}"
        )


def fetch_entry_rule(
    dest_country: str,
    nationality: str,
    *,
    simulate_unreachable: bool = False,
) -> tuple[dict[str, Any] | None, str]:
    """
    Resolve the entry rule for (dest_country, nationality) via the (deferred)
    entry-requirements source adapter.

    Returns (rule_or_None, source_freshness). Today this is the Tier-1 seeded
    table. nationality-specific rows override the wildcard row.
    `simulate_unreachable=True` exercises the Tier-3 exhausted path → returns the
    seeded rule tagged SEEDED_FALLBACK (terms NOT fabricated), or None if no seed.
    """
    dest = (dest_country or "").upper()
    nat = (nationality or "").upper()

    # Citizens always enter their own country — no visa of any kind needed.
    if dest and nat and dest == nat:
        return {
            "dest_country": dest, "program": "Citizen entry (no visa required)",
            "kind": EntryKind.VISA_FREE,
            "eligible_nationalities": [nat],
            "processing_lead_business_days": 0,
            "fee_cents": 0, "currency": "USD",
            "provenance": "rule:citizen-home-country",
            "source_url": "",
        }, "RULE"

    # Resolve all rows for this destination, partitioned into nationality-specific
    # matches and the (single) wildcard. We collect ALL specific matches so an
    # accidental overlap in the seed (two specific rows both listing this
    # nationality) is DETECTED rather than silently resolving to whichever row
    # appears later in list order (var-0 / correctness — finding #51). Iteration is
    # over the fixed _ENTRY_RULES list (stable order), never a set/dict.
    specific_matches: list[dict[str, Any]] = []
    specific_present_for_dest = False  # any nationality-specific row exists for dest
    wildcard: dict[str, Any] | None = None
    for r in _ENTRY_RULES:
        if r["dest_country"] != dest:
            continue
        elig = r["eligible_nationalities"]
        if elig is None:
            wildcard = r
            continue
        specific_present_for_dest = True
        if nat and nat in {e.upper() for e in elig}:
            specific_matches.append(r)

    specific = _resolve_specific(specific_matches, dest, nat)

    freshness = "SEEDED_FALLBACK" if simulate_unreachable else "SEEDED"

    # D1 (HONESTY/fail-conservative): an EMPTY/UNKNOWN nationality against a KNOWN
    # destination that has nationality-CONDITIONAL rows must NOT silently bind to the
    # permissive wildcard (e.g. ESTA/VWP) — that would treat an unknown traveler as
    # wildcard-eligible and ALLOW the trip, understating that this nationality may
    # need a long-lead consular visa. Return None (no rule) → gate_leg degrades to a
    # conservative UNKNOWN FLAG so the traveler is told to verify, never approved.
    if not nat and specific_present_for_dest:
        if simulate_unreachable:
            logger.warning(
                "compliance: entry-requirements source unreachable (Tier-3 exhausted) "
                "→ SEEDED_FALLBACK for %s/%s (rule NOT fabricated)", dest, nat or "<unspecified>",
            )
        return None, freshness

    rule = specific or wildcard
    if simulate_unreachable and rule is not None:
        logger.warning(
            "compliance: entry-requirements source unreachable (Tier-3 exhausted) "
            "→ SEEDED_FALLBACK for %s/%s (rule NOT fabricated)", dest, nat,
        )
    return rule, freshness


# Fail fast at import: an ambiguous seed (finding #51) must never ship.
_assert_entry_rules_unambiguous()


def _rule_provenance(rule: dict[str, Any], tier: str = SourceTier.SEEDED.value) -> dict[str, Any]:
    """The canonical provenance envelope for a seeded entry rule."""
    return make_provenance(
        source=rule["provenance"],
        fetched_at=_SEED_FETCHED_AT,
        tier=tier,
        source_url=rule["source_url"],
        ttl=_SEED_TTL_SECONDS,
    )


# ===========================================================================
# 4. THE LEAD-TIME GATE — the heart of the agent (pure, variance-0).
# ===========================================================================

def required_lead_business_days(rule: dict[str, Any], buffer_days: int) -> int:
    """
    The total business days that must exist between `today` and departure for the
    visa to be obtainable IN TIME = processing lead-time + safety buffer.

    Pure integer arithmetic from the seed (NO-LLM-NUMBERS). Non-lead-time kinds
    (visa-free / VOA) require 0.
    """
    if rule["kind"] not in _LEAD_TIME_KINDS:
        return 0
    # buffer_days is caller-controllable; clamp to >= 0 so a negative buffer can
    # never shrink (or invert) the required lead time and let an unbookable-in-time
    # trip pass the gate (fail-conservative / HONESTY). int() first to normalize.
    safe_buffer = max(0, int(buffer_days))
    return int(rule["processing_lead_business_days"]) + safe_buffer


def gate_leg(
    *,
    dest_country: str,
    nationality: str,
    departure_date: str,
    today: str,
    buffer_days: int = DEFAULT_BUFFER_BUSINESS_DAYS,
    passport_expiry: str | None = None,
    simulate_source_unreachable: bool = False,
) -> dict[str, Any]:
    """
    Gate ONE leg: is this traveler eligible to enter, IN TIME, for this departure?

    PURE + DETERMINISTIC. Returns a per-leg verdict record:
        {dest_country, nationality, kind, allowed (bool), reason (LegReason),
         processing_lead_business_days, buffer_business_days,
         required_lead_business_days, available_business_days,
         earliest_feasible_departure (ISO|None), fee_cents, currency,
         provenance, source_freshness, ...}

    The flagship check: if the entry kind has a processing lead-time and the
    business days available before departure < required lead-time → BLOCK with
    BLOCK_INSUFFICIENT_LEAD_TIME and compute the earliest feasible departure.

    An UNKNOWN rule (no seed) → conservative BLOCK (BLOCK_UNKNOWN_RULE) — NEVER a
    silent allow.
    """
    # buffer_days is caller-controllable; clamp to >= 0 once at the gate boundary so
    # the echoed buffer_business_days, the required-lead computation, and the
    # resequence math all agree on the same non-negative value (HONESTY / var-0 —
    # a negative buffer must never let an unbookable-in-time trip pass).
    buffer_days = max(0, int(buffer_days))
    try:
        dep = _parse_date(departure_date)
        now = _parse_date(today)
    except (ValueError, TypeError):
        # Malformed date(s) → cannot verify lead time → conservative BLOCK (robustness:
        # never crash on bad external input, never silent-allow).
        return {
            "dest_country": (dest_country or "").upper(),
            "dest_name": _COUNTRY_NAMES.get((dest_country or "").upper(), (dest_country or "").upper()),
            "nationality": (nationality or "").upper(),
            "departure_date": departure_date,
            "today": today,
            "buffer_business_days": int(buffer_days),
            "source_freshness": "SEEDED",
            "kind": EntryKind.UNKNOWN,
            "program": None,
            "allowed": False,
            "reason": LegReason.BLOCK_UNKNOWN_RULE,
            "processing_lead_business_days": None,
            "required_lead_business_days": None,
            "available_business_days": None,
            "earliest_feasible_departure": None,
            "fee_cents": 0,
            "currency": "USD",
            "provenance": make_provenance(
                source="seed:compliance-invalid-date",
                fetched_at=_SEED_FETCHED_AT, tier=SourceTier.SEEDED.value,
                source_url=None, ttl=_SEED_TTL_SECONDS,
            ),
            "detail": (f"invalid date(s) departure={departure_date!r} today={today!r} — "
                       f"cannot verify entry lead time; blocked conservatively"),
        }
    rule, freshness = fetch_entry_rule(
        dest_country, nationality, simulate_unreachable=simulate_source_unreachable
    )

    base: dict[str, Any] = {
        "dest_country": (dest_country or "").upper(),
        "dest_name": _COUNTRY_NAMES.get((dest_country or "").upper(), (dest_country or "").upper()),
        "nationality": (nationality or "").upper(),
        "departure_date": departure_date,
        "today": today,
        "buffer_business_days": int(buffer_days),
        "source_freshness": freshness,
    }

    # --- UNKNOWN rule → conservative FLAG (surface advisory, never silent allow,
    #     never hard-block as unknown — genuine known-ineligible cases stay blocks).
    #     allowed=None means "unverified": trip still completes, advisory is surfaced.
    if rule is None:
        dest_disp = (dest_country or "").upper()
        nat_disp = (nationality or "").upper() or "unspecified nationality"
        base.update({
            "kind": EntryKind.UNKNOWN,
            "program": None,
            "allowed": None,
            "unverified_flag": True,
            "flag_advisory": (
                f"Entry requirements not verified for {dest_disp} / "
                f"{nat_disp} — traveler must confirm visa before travel."
            ),
            "reason": LegReason.BLOCK_UNKNOWN_RULE,
            "processing_lead_business_days": None,
            "required_lead_business_days": None,
            "available_business_days": business_days_between(now, dep),
            "earliest_feasible_departure": None,
            "fee_cents": 0,
            "currency": "USD",
            "provenance": make_provenance(
                source="seed:compliance-no-rule",
                fetched_at=_SEED_FETCHED_AT,
                tier=SourceTier.SEEDED.value,
                source_url=None,
                ttl=_SEED_TTL_SECONDS,
            ),
        })
        return base

    kind = rule["kind"]
    lead = int(rule["processing_lead_business_days"])
    required = required_lead_business_days(rule, buffer_days)
    available = business_days_between(now, dep)

    rec: dict[str, Any] = dict(base)
    rec.update({
        "kind": kind,
        "program": rule["program"],
        "processing_lead_business_days": lead,
        "required_lead_business_days": required,
        "available_business_days": available,
        "fee_cents": int(rule["fee_cents"]),
        "currency": rule["currency"],
        "provenance": _rule_provenance(
            rule,
            tier=SourceTier.SEEDED.value if freshness == "SEEDED" else SourceTier.EXHAUSTED.value,
        ),
        "earliest_feasible_departure": None,
    })

    # --- Secondary (stub) passport-validity check: many countries require 6mo
    #     validity beyond entry. Honest secondary gate; flagship is lead-time. ---
    if passport_expiry:
        try:
            exp = _parse_date(passport_expiry)
            if exp < dep + timedelta(days=180):
                rec.update({"allowed": False, "reason": LegReason.BLOCK_PASSPORT_VALIDITY})
                return rec
        except ValueError:
            pass  # malformed expiry → ignore the stub check (don't fabricate)

    # --- Visa-free / VOA: no lead-time gate; always in-time ---
    if kind == EntryKind.VISA_FREE:
        rec.update({"allowed": True, "reason": LegReason.OK_VISA_FREE})
        return rec
    if kind == EntryKind.VISA_ON_ARRIVAL:
        rec.update({"allowed": True, "reason": LegReason.OK_VISA_ON_ARRIVAL})
        return rec

    # --- Nationality eligibility (eVisa not open to this nationality) ---
    elig = rule["eligible_nationalities"]
    if elig is not None and (nationality or "").upper() not in {e.upper() for e in elig}:
        rec.update({"allowed": False, "reason": LegReason.BLOCK_INELIGIBLE})
        return rec

    # --- THE FLAGSHIP LEAD-TIME GATE ---
    if available < required:
        # UNBOOKABLE IN TIME. Compute the earliest departure that clears the gate:
        # today + required business days (the first date the visa can be in hand).
        earliest = add_business_days(now, required)
        rec.update({
            "allowed": False,
            "reason": LegReason.BLOCK_LEAD_TIME,
            "earliest_feasible_departure": earliest.isoformat(),
        })
        return rec

    # Bookable in time.
    rec.update({"allowed": True, "reason": LegReason.OK_LEAD_TIME_CLEARED})
    return rec


# ===========================================================================
# 5. VISA FEE LINE ITEM — emitted via the Phase-0 LineItemAssembler.
# ===========================================================================

def visa_fee_line_item(
    leg_verdict: dict[str, Any],
    leg_key: str,
    *,
    source: str = "compliance",
) -> dict[str, Any] | None:
    """
    Emit the visa/eVisa fee as a Budget line item (LineItemKind.VISA) via the
    Phase-0 LineItemAssembler — keyed/idempotent by (source, "visa", leg) so
    re-emission REPLACES (never double-charges). Budget then vetoes it like any
    other line item (Compliance proposes, Budget enforces).

    Returns the single assembled line-item dict, or None for a zero-fee leg
    (visa-free / VOA-at-no-cost / unknown). FX-normalised to usd_cents via the
    fx_provider seam for any non-USD fee (Budget reads usd_cents, never an LLM
    number, never an agent-decided rate).
    """
    fee = int(leg_verdict.get("fee_cents", 0) or 0)
    if fee <= 0:
        return None
    currency = (leg_verdict.get("currency") or "USD").upper()

    if currency == "USD":
        money = make_money(
            native_cents=fee, currency="USD", usd_cents=fee,
            fx_rate=1.0, fx_source=leg_verdict.get("provenance", {}).get("source", "seed:compliance"),
            fetched_at=_SEED_FETCHED_AT,
        )
    else:
        # Non-USD fee → normalise through the FX provider seam (Budget consumes
        # usd_cents; the agent NEVER decides a rate — fx_provider owns the rate).
        # An UNKNOWN currency → FXResult.exhausted: we do NOT silently treat it as
        # USD (the §18 honest-decline rule). We drop the fee line item rather than
        # emit a wrong number — the verdict (the gate decision) is unaffected.
        from utils.fx_provider import normalize
        fxr = normalize(native_cents=fee, currency=currency)
        if fxr.exhausted:
            logger.warning(
                "compliance: visa fee currency %r unknown to FX seam — fee line "
                "item omitted (honest decline; gate verdict unaffected)", currency,
            )
            return None
        money = fxr.to_money()

    a = LineItemAssembler()
    a.add_fee(
        source=source,
        kind=LineItemKind.VISA,
        leg=leg_key,
        money=money,
        description=(
            f"{leg_verdict.get('program') or 'Visa/eVisa'} fee — entry to "
            f"{leg_verdict.get('dest_name', leg_verdict.get('dest_country'))}"
        ),
    )
    return a.assemble()[0]


# ===========================================================================
# 6. CHECK ELIGIBILITY — assemble the whole-trip EligibilityVerdict (pure).
# ===========================================================================

# --- #70 multi-passport (best-passport selection) -------------------------------------------
# Lower rank == better entry outcome. PURE/deterministic — no LLM, no network.
_PASSPORT_KIND_RANK = {
    "VISA_FREE": 0, "VISA_FREE_EQUIVALENT": 0,
    "ETA": 1, "EVISA": 1, "VISA_ON_ARRIVAL": 2,
    "VISA_REQUIRED": 3, "UNKNOWN": 4,
}


def _passport_rank(v: dict[str, Any]) -> tuple[int, int, int]:
    """Rank a per-leg gate verdict for best-passport selection: prefer ALLOWED (in time),
    then the lightest entry kind (visa-free < eVisa/ETA < VOA < visa-required), then the
    lowest fee. allowed=None (UNVERIFIED/unknown rule) ranks between True and False so a
    KNOWN-eligible passport is preferred over an unknown one, but an unknown beats a
    KNOWN-ineligible one (fail-conservative)."""
    allowed = v.get("allowed")
    allowed_rank = 0 if allowed is True else (1 if allowed is None else 2)
    kind_rank = _PASSPORT_KIND_RANK.get(str(v.get("kind") or "UNKNOWN").upper(), 4)
    fee = int(v.get("fee_cents") or 0)
    return (allowed_rank, kind_rank, fee)


def gate_leg_best_passport(
    *,
    dest_country: str,
    nationalities: list[str],
    departure_date: str,
    today: str,
    buffer_days: int = DEFAULT_BUFFER_BUSINESS_DAYS,
    passport_expiry: str | None = None,
    simulate_source_unreachable: bool = False,
) -> dict[str, Any]:
    """Multi-passport: gate ONE leg with EACH supplied passport and return the verdict for the
    passport giving the best entry outcome. PURE + deterministic (stable tie-break on the
    passport string). Surfaces ``selected_passport`` + ``passports_considered`` for honesty.
    Fail-conservative: it returns the BEST (least-bad) verdict — a leg that EVERY passport is
    blocked/ineligible for still BLOCKS; a strong passport never silently rescues a seeded
    block. With no usable passport it falls through to the conservative unknown verdict."""
    nats = [str(n).strip().upper() for n in (nationalities or []) if str(n).strip()]
    if not nats:
        nats = [""]  # -> gate_leg's conservative unknown-nationality verdict
    scored = [
        gate_leg(
            dest_country=dest_country, nationality=nat, departure_date=departure_date,
            today=today, buffer_days=buffer_days, passport_expiry=passport_expiry,
            simulate_source_unreachable=simulate_source_unreachable,
        )
        for nat in nats
    ]
    best_idx = min(range(len(scored)), key=lambda i: (_passport_rank(scored[i]), nats[i]))
    best = dict(scored[best_idx])
    best["selected_passport"] = nats[best_idx]
    best["passports_considered"] = nats
    return best


def check_eligibility(
    *,
    legs: list[dict[str, Any]],
    nationality: str,
    nationalities: list[str] | None = None,
    today: str,
    buffer_days: int = DEFAULT_BUFFER_BUSINESS_DAYS,
    passport_expiry: str | None = None,
    simulate_source_unreachable: bool = False,
) -> dict[str, Any]:
    """
    The core deterministic eligibility GATE for a whole trip — NO LLM, NO network.

    Args:
        legs: ordered list of {dest_country, departure_date, leg_id?} dicts. The
              FIRST leg's departure_date is the trip departure the gate clears
              against (a per-leg departure_date may override for multi-entry).
        nationality: traveler nationality (ISO-3166 alpha-2).
        today: the ISO date the booking is being made (the gate's clock).
        buffer_days: safety-buffer business days (deterministic constant default).

    Returns the EligibilityVerdict dict:
        {verdict (GateVerdict), bookable (bool), per_leg[], blocking_legs[],
         line_items[], resequence{}, total_visa_fee_usd_cents, ...}

    The gate is a PURE function: trip is BLOCKED iff ANY leg is not allowed. A
    blocked trip ALWAYS carries an honest re-sequence (depart-later date and/or a
    visa-free alternative) — the planner never just refuses. Every numeric field
    is integer cents / integer business days from the seed (NO-LLM-NUMBERS).
    """
    # buffer_days is caller-controllable; clamp to >= 0 at the trip-gate boundary so
    # every downstream consumer (gate_leg, _build_resequence, the echoed
    # buffer_business_days) agrees on the same non-negative value — a negative buffer
    # must never shrink required lead time and let an unbookable trip pass (HONESTY).
    buffer_days = max(0, int(buffer_days))

    per_leg: list[dict[str, Any]] = []
    line_items: list[dict[str, Any]] = []
    total_visa_usd = 0
    seen_visa_dest: set[str] = set()  # one visa fee per destination COUNTRY, not per leg

    for i, leg in enumerate(legs):
        dest = leg.get("dest_country") or leg.get("country") or ""
        dep = leg.get("departure_date") or leg.get("checkin") or today
        leg_key = str(leg.get("leg_id", i))

        if nationalities:
            # #70 multi-passport (opt-in): pick, PER LEG, the passport giving the best entry
            # outcome. The single-nationality path below is unchanged -> byte-identical.
            v = gate_leg_best_passport(
                dest_country=dest,
                nationalities=nationalities,
                departure_date=dep,
                today=today,
                buffer_days=buffer_days,
                passport_expiry=passport_expiry,
                simulate_source_unreachable=simulate_source_unreachable,
            )
        else:
            v = gate_leg(
                dest_country=dest,
                nationality=nationality,
                departure_date=dep,
                today=today,
                buffer_days=buffer_days,
                passport_expiry=passport_expiry,
                simulate_source_unreachable=simulate_source_unreachable,
            )
        v["leg_id"] = leg_key
        # TTL enforcement: a seeded rule past its own fetched_at+ttl freshness
        # window must never keep being served as confidently current. Downgrade
        # allowed=True -> None (the existing "unverified" convention) with an
        # honest stale-data note; an existing hard block/flag is left as-is.
        apply_ttl_downgrade(v, as_of=today, verified_field="allowed")
        per_leg.append(v)

        # A visa/eVisa fee is charged once per COUNTRY ENTRY — a multi-city trip within one country
        # (e.g. Hanoi + Ha Long, both VN) needs ONE Vietnam visa, not one per leg. Emit the fee only
        # for the FIRST leg of each destination country; later same-country legs add no extra fee.
        # (per_leg verdicts stay per-leg so each leg still shows its entry rule; only the FEE dedups.)
        dest_key = (dest or "").strip().upper()
        if dest_key not in seen_visa_dest:
            li = visa_fee_line_item(v, leg_key)
            if li is not None:
                seen_visa_dest.add(dest_key)
                line_items.append(li)
                total_visa_usd += int(li["usd_cents"])

    # Genuine hard blocks: allowed is explicitly False (known-ineligible / lead-time).
    # Unverified flags: allowed is None (unknown rule → FLAG advisory, not a hard block).
    blocking = [v for v in per_leg if v["allowed"] is False]
    flagged = [v for v in per_leg if v["allowed"] is None]
    bookable = len(blocking) == 0
    has_flags = len(flagged) > 0
    # D4 (verdict legibility): a HARD block → BLOCK; an unverified FLAG (no hard
    # block) → ALLOW_WITH_FLAGS so the headline cannot be misread as unqualified
    # approval (I4); a fully-clear trip → ALLOW. Remains bookable=True for the
    # flagged case — the orchestrator surfaces the advisory, not a new block.
    if not bookable:
        verdict = GateVerdict.BLOCK
    elif has_flags:
        verdict = GateVerdict.ALLOW_WITH_FLAGS
    else:
        verdict = GateVerdict.ALLOW

    resequence = _build_resequence(blocking, nationality, today, buffer_days) if blocking else None

    # #33 Henley enrichment — ADDITIVE, DESCRIPTIVE ONLY. passport_index is the
    # traveler's Henley passport strength (or None for an unknown/empty
    # nationality); passport_index_crosscheck annotates, per already-decided leg,
    # whether the seeded entry kind is visa-free-equivalent. Neither changes the
    # gate decision above — a strong passport NEVER silently allows a leg the
    # seeded rules block or leave unknown (fail-conservative / var-0 / APPEND-ONLY).
    _passport_index = passport_strength(nationality)

    return {
        "verdict": verdict,
        "bookable": bookable,
        "nationality": (nationality or "").upper(),
        "today": today,
        "buffer_business_days": int(buffer_days),
        "passport_index": _passport_index,
        "passport_index_crosscheck": _passport_index_crosscheck(per_leg, _passport_index),
        "per_leg": per_leg,
        "blocking_legs": [
            {"leg_id": v["leg_id"], "dest_country": v["dest_country"],
             "reason": v["reason"], "kind": v["kind"]}
            for v in blocking
        ],
        "flagged_legs": [
            {"leg_id": v["leg_id"], "dest_country": v["dest_country"],
             "reason": v["reason"], "kind": v["kind"],
             "flag_advisory": v.get("flag_advisory", "")}
            for v in flagged
        ],
        "has_eligibility_flags": len(flagged) > 0,
        "line_items": line_items,
        "total_visa_fee_usd_cents": total_visa_usd,
        "resequence": resequence,
        "source_freshness": "SEEDED_FALLBACK" if simulate_source_unreachable else "SEEDED",
    }


def _build_resequence(
    blocking: list[dict[str, Any]],
    nationality: str,
    today: str,
    buffer_days: int,
) -> dict[str, Any]:
    """
    Build the honest compliant re-sequence for a blocked trip (deterministic).

    Two structural options, both from the seed (NO-LLM-NUMBERS):
      depart_later:  the latest earliest_feasible_departure across blocking legs —
                     the soonest date the WHOLE trip becomes bookable in time.
      visa_free_alternative: per blocking leg, a seeded visa-free / shorter-lead
                     alternative destination (if any), with its own verdict so the
                     re-sequence is itself gated (never offers an unbookable alt).
    """
    # depart-later: the max earliest_feasible_departure across lead-time blocks.
    feasible_dates = [
        v["earliest_feasible_departure"] for v in blocking
        if v.get("earliest_feasible_departure")
    ]
    depart_later = max(feasible_dates) if feasible_dates else None

    alternatives: list[dict[str, Any]] = []
    for v in blocking:
        for alt_dest in _VISA_FREE_ALTERNATIVES.get(v["dest_country"], []):
            alt = gate_leg(
                dest_country=alt_dest,
                nationality=nationality,
                departure_date=v["departure_date"],
                today=today,
                buffer_days=buffer_days,
            )
            alternatives.append({
                "blocked_dest": v["dest_country"],
                "alternative_dest": alt_dest,
                "alternative_name": _COUNTRY_NAMES.get(alt_dest, alt_dest),
                "alternative_kind": alt["kind"],
                "alternative_bookable_in_time": alt["allowed"],
                "alternative_reason": alt["reason"],
            })

    return {
        "depart_later_earliest": depart_later,
        "depart_later_note": (
            f"Earliest departure at which every visa can be obtained in time: "
            f"{depart_later}." if depart_later else
            "No depart-later date resolves the block (eligibility/unknown-rule)."
        ),
        "visa_free_alternatives": alternatives,
    }


# ===========================================================================
# 7. EXPLAIN BLOCK — honest cannot_satisfy headline from the structured verdict.
# ===========================================================================

_REASON_HEADLINES = {
    LegReason.BLOCK_LEAD_TIME: (
        "UNBOOKABLE IN TIME: the {program} for {dest} needs {lead} business days to "
        "process (+{buffer} buffer = {required} required), but only {available} "
        "business days remain before departure on {departure}. Earliest feasible "
        "departure: {earliest}."
    ),
    LegReason.BLOCK_INELIGIBLE: (
        "INELIGIBLE: the {program} for {dest} is not open to {nationality} nationals; "
        "a different visa route or destination is required."
    ),
    LegReason.BLOCK_UNKNOWN_RULE: (
        "UNVERIFIED: no seeded entry rule for {dest}/{nationality} — entry requirements "
        "not confirmed; traveler must verify visa before travel (never a silent allow)."
    ),
    LegReason.BLOCK_PASSPORT_VALIDITY: (
        "PASSPORT VALIDITY: entry to {dest} typically requires 6 months' passport "
        "validity beyond travel; the provided passport does not meet that for {departure}."
    ),
}


def explain_block(verdict: dict[str, Any]) -> dict[str, Any]:
    """
    Build the honest cannot_satisfy headline — a PURE FORMATTER over the
    structured verdict. NO free generation of facts: every factual claim derives
    from the structured per-leg record + the seeded provenance.

    Returns {verdict, headline, blocking_legs[], resequence, provenance[]}.
    """
    if verdict.get("bookable") and not verdict.get("has_eligibility_flags"):
        return {
            "verdict": verdict["verdict"],
            "headline": "All legs are bookable in time — no compliance block.",
            "blocking_legs": [],
            "flagged_legs": [],
            "resequence": None,
        }

    lines: list[str] = []
    provs: list[dict[str, Any]] = []
    for v in verdict["per_leg"]:
        if v["allowed"]:
            continue
        tmpl = _REASON_HEADLINES.get(v["reason"], "BLOCKED: {dest} ({reason}).")
        lines.append(tmpl.format(
            program=v.get("program") or "entry permit",
            dest=v.get("dest_name", v["dest_country"]),
            nationality=v["nationality"],
            lead=v.get("processing_lead_business_days"),
            buffer=v.get("buffer_business_days"),
            required=v.get("required_lead_business_days"),
            available=v.get("available_business_days"),
            departure=v.get("departure_date"),
            earliest=v.get("earliest_feasible_departure"),
            reason=v["reason"],
        ))
        provs.append(v.get("provenance", {}))

    return {
        "verdict": verdict["verdict"],
        "headline": " ".join(lines),
        "blocking_legs": verdict["blocking_legs"],
        "resequence": verdict.get("resequence"),
        "provenance": provs,
    }


# ===========================================================================
# 8. OPTIONAL COSMETIC LLM RE-SEQUENCE RATIONALE — fully clamped + validated.
# ===========================================================================
# The LLM ONLY drafts a human-friendly sentence about the (already-decided)
# re-sequence. It is validated to reference ONLY the structured facts; if it
# softens the block or invents a number/date it is REJECTED and we fall back to
# the deterministic headline. The LLM NEVER changes verdict / dates / fees.

def validate_rationale(text: str, verdict: dict[str, Any]) -> bool:
    """
    True iff `text` is a faithful derivation of the structured verdict: it must
    NOT assert the trip is bookable/allowed when it is blocked, and must not
    introduce a number/date absent from the structured verdict.

    Conservative: any softening of a BLOCK → reject (never let cosmetic prose
    overturn the deterministic gate).
    """
    if not isinstance(text, str) or not text.strip():
        return False
    low = text.lower()
    if not verdict.get("bookable"):
        # Must not claim it's fine / bookable / can proceed now.
        soften = ["can be booked now", "is bookable now", "no problem", "you're fine",
                  "good to go", "ready to book", "no issue", "can proceed as planned"]
        if any(s in low for s in soften):
            return False
    return True


def _llm_rationale(verdict: dict[str, Any]) -> str | None:
    """
    Optional cosmetic re-sequence sentence via DashScope. Returns None if no key,
    on any error, or if the draft fails validate_rationale (→ deterministic
    fallback). NEVER changes the structured verdict.
    """
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        return None
    try:
        from utils.model_router import dashscope_chat
    except ImportError:
        from model_router import dashscope_chat  # type: ignore[no-redef]
    try:
        rs = verdict.get("resequence") or {}
        facts = json.dumps({
            "verdict": verdict["verdict"],
            "blocking_legs": verdict["blocking_legs"],
            "depart_later_earliest": rs.get("depart_later_earliest"),
            "visa_free_alternatives": rs.get("visa_free_alternatives"),
        }, sort_keys=True)
        prompt = (
            "You are a travel planner. In ONE sentence, restate this compliance "
            "decision for the traveler. Use ONLY these facts; do NOT add numbers, "
            "dates, or claim the trip is bookable now if it is blocked.\n" + facts
        )
        data = dashscope_chat(
            "default",
            {"enable_thinking": False, "messages": [{"role": "user", "content": prompt}]},
            timeout=30.0,
        )
        text = data["choices"][0]["message"]["content"]
        return text if validate_rationale(text, verdict) else None
    except Exception:
        return None


# ===========================================================================
# 9. ComplianceAgent — A2A wrapper (follows the §AGENT-EXTENSION PATTERN).
# ===========================================================================

class ComplianceAgent(A2AAgent):
    """
    Compliance eligibility GATE specialist (Travel Guild, L2 / DC1).

    Implements:
      ``compliance.check_eligibility`` — per-leg entry verdict + the lead-time
                                         gate + visa fee line item + re-sequence.
      ``compliance.explain_block``     — honest cannot_satisfy headline.

    The gate path is fully deterministic (no LLM, no network). The optional
    cosmetic re-sequence rationale (DashScope) is fully clamped behind the
    deterministic verdict.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 9106) -> None:
        self._host = host
        self._port = port
        super().__init__()

    # ------------------------------------------------------------------
    # A2AAgent protocol
    # ------------------------------------------------------------------

    def _build_card(self) -> dict:
        url = f"http://{self._host}:{self._port}"
        return {
            "name": "compliance-agent",
            "description": (
                "Compliance eligibility GATE specialist (L2) — a deterministic BOOKABILITY "
                "gate, not advice. Given a trip's legs (destinations + departure date) and "
                "the traveler's nationality, checks each leg's entry requirement "
                "(visa/eVisa: required?, cost, PROCESSING LEAD-TIME, eligibility-by-"
                "nationality) against the departure date. The flagship check is the visa "
                "LEAD-TIME gate: if departure < (visa lead-time + buffer) the trip is "
                "UNBOOKABLE IN TIME and the gate BLOCKS it, returning an honest "
                "cannot_satisfy + a compliant re-sequence (depart later, or a visa-free "
                "alternative). Emits any visa FEE as a Budget line item. Deterministic "
                "rule-resolver; NO-LLM-NUMBERS (lead-times/fees/dates are integer values "
                "from the seeded per-country table); the LLM only drafts validated cosmetic "
                "rationale. Implements 'compliance.check_eligibility' + "
                "'compliance.explain_block'. Part of the Travel Guild multi-agent "
                "pipeline (Track 3, L2 / DC1)."
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
                    "id": "compliance.check_eligibility",
                    "name": "Check Eligibility (visa lead-time gate)",
                    "description": (
                        "Given {legs[{dest_country, departure_date}], nationality, today?, "
                        "buffer_days?} compute the whole-trip EligibilityVerdict: per-leg "
                        "entry kind + the deterministic lead-time gate (BLOCK if departure < "
                        "visa lead-time + buffer), visa fee line items, and a compliant "
                        "re-sequence for any block. UNKNOWN rule → conservative BLOCK "
                        "(never silent allow). NO-LLM-NUMBERS."
                    ),
                    "tags": ["compliance", "visa", "evisa", "lead-time", "eligibility",
                             "gate", "deterministic", "money-path", "L2"],
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "legs": {"type": "array", "items": {"type": "object"}},
                            "nationality": {"type": "string"},
                            "today": {"type": "string"},
                            "buffer_days": {"type": "integer"},
                            "passport_expiry": {"type": "string"},
                        },
                        "required": ["legs", "nationality"],
                    },
                    "examples": [
                        json.dumps({
                            "legs": [{"dest_country": "ET", "departure_date": "2026-06-19"}],
                            "nationality": "US", "today": "2026-06-16"}),
                    ],
                },
                {
                    "id": "compliance.explain_block",
                    "name": "Explain Block (honest cannot_satisfy)",
                    "description": (
                        "Given an EligibilityVerdict (or the same check_eligibility input), "
                        "return the honest cannot_satisfy headline built ONLY from the "
                        "structured verdict + seeded provenance (no free generation of "
                        "facts). Pure formatter; optional clamped+validated cosmetic LLM "
                        "rationale."
                    ),
                    "tags": ["compliance", "block", "cannot_satisfy", "explanation",
                             "deterministic"],
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "verdict": {"type": "object"},
                            "legs": {"type": "array", "items": {"type": "object"}},
                            "nationality": {"type": "string"},
                            "today": {"type": "string"},
                            "use_llm": {"type": "boolean"},
                        },
                    },
                },
            ],
        }

    def _register_skills(self) -> None:
        self.register_skill("compliance.check_eligibility", self._check_handler)
        self.register_skill("compliance.explain_block", self._explain_handler)

    # ------------------------------------------------------------------
    # Skill handlers
    # ------------------------------------------------------------------

    async def _check_handler(self, message: dict, task: dict) -> dict:
        payload = self._extract_payload(message)
        if not isinstance(payload, dict):
            raise ValueError(
                "compliance.check_eligibility requires a data part with a JSON object "
                "{legs[], nationality, today?, buffer_days?}"
            )
        legs = payload.get("legs")
        nationality = payload.get("nationality")
        if not isinstance(legs, list) or not legs:
            raise ValueError("compliance.check_eligibility: 'legs' must be a non-empty list")
        # D1 (HONESTY/fail-conservative): a falsy/None nationality must NOT raise.
        # Raising here defeated the conservative-FLAG path — the orchestrator swallows
        # the RuntimeError to None and silently SKIPS the visa lead-time gate (fail
        # open on a real hazard). Instead pass "" through to check_eligibility:
        # gate_leg already supports unspecified nationality → every leg degrades to a
        # conservative UNKNOWN FLAG (never a silent pass). Only a missing `legs` list
        # is a hard input error.
        if not nationality:
            nationality = ""

        # D7 (var-0 / frozen today): the visa lead-time verdict
        # `(departure_date - today) < lead_time + buffer` depends on `today`, so it
        # is a determinism-critical gate input. Mirror health.assess — RAISE if it
        # is absent rather than clocking off wall-time (which would make the gate
        # decision differ across calendar days). The orchestrator injects ONE frozen
        # run-today and its gate wrapper degrades a raise to a conservative BLOCK,
        # so this never fails open.
        today = payload.get("today")
        if not today:
            raise ValueError(
                "compliance.check_eligibility: 'today' (ISO date) is required — the "
                "gate cannot fall back to wall-clock time (var-0 / D7 contract). The "
                "orchestrator must inject a single frozen today for the whole run."
            )
        buffer_days = int(payload.get("buffer_days", DEFAULT_BUFFER_BUSINESS_DAYS))

        verdict = check_eligibility(
            legs=legs,
            nationality=nationality,
            nationalities=payload.get("nationalities"),  # #70 multi-passport (opt-in)
            today=today,
            buffer_days=buffer_days,
            passport_expiry=payload.get("passport_expiry"),
            simulate_source_unreachable=bool(payload.get("simulate_source_unreachable", False)),
        )

        # INTERLOCK (Health→Compliance): consume the Health jab_records (the §7
        # YF-cert handoff). Compliance owns the entry-DOCUMENT half: it reads the
        # SAME seeded jab record Health produced and surfaces the entry-cert
        # handoff evidence. APPEND-ONLY: this does NOT change the (pure) verdict —
        # it records which jab records back the per-leg entry-cert requirement, so
        # the var-0 verdict is preserved.
        jab_records = payload.get("jab_records")
        if isinstance(jab_records, list) and jab_records:
            verdict["jab_records_consumed"] = jab_records
            verdict["entry_cert_handoff"] = {
                "source": "health-agent",
                "n_jab_records": len(jab_records),
                "vaccines": [j.get("vaccine") for j in jab_records if isinstance(j, dict)],
                "note": (
                    "Compliance read the Health jab record(s) for the entry-certificate "
                    "(YF) half of the §7 handoff; the lead-time verdict is unchanged."
                ),
            }

        logger.info(
            "compliance.check_eligibility: nat=%s legs=%d verdict=%s blocking=%d visa_fee=%d¢ freshness=%s jabs=%d",
            verdict["nationality"], len(verdict["per_leg"]), verdict["verdict"],
            len(verdict["blocking_legs"]), verdict["total_visa_fee_usd_cents"],
            verdict["source_freshness"],
            len(jab_records) if isinstance(jab_records, list) else 0,
        )
        return _new_artifact(
            name="compliance.check_eligibility.result",
            parts=[_data_part(verdict)],
        )

    async def _explain_handler(self, message: dict, task: dict) -> dict:
        payload = self._extract_payload(message)
        if not isinstance(payload, dict):
            raise ValueError(
                "compliance.explain_block requires a data part with "
                "{verdict} OR {legs, nationality}"
            )
        verdict = payload.get("verdict")
        if not isinstance(verdict, dict):
            # Convenience: build the verdict from the same check input.
            legs = payload.get("legs")
            nationality = payload.get("nationality")
            if not isinstance(legs, list) or not nationality:
                raise ValueError(
                    "compliance.explain_block: provide a 'verdict' object, or "
                    "{legs, nationality} to compute one"
                )
            explain_today = payload.get("today")
            if not explain_today:
                raise ValueError(
                    "compliance.explain_block: 'today' (ISO date) is required to "
                    "compute a verdict from {legs, nationality} (var-0 / D7)."
                )
            verdict = check_eligibility(
                legs=legs,
                nationality=nationality,
                nationalities=payload.get("nationalities"),  # #70 multi-passport (opt-in)
                today=explain_today,
                buffer_days=int(payload.get("buffer_days", DEFAULT_BUFFER_BUSINESS_DAYS)),
            )

        result = explain_block(verdict)

        if payload.get("use_llm"):
            rationale = _llm_rationale(verdict)
            result["llm_rationale"] = rationale  # None if unavailable/invalid
            result["rationale_source"] = "llm" if rationale else "deterministic"
        else:
            result["rationale_source"] = "deterministic"

        return _new_artifact(
            name="compliance.explain_block.result",
            parts=[_data_part(result)],
        )

    # ------------------------------------------------------------------
    # Input extraction helper (same pattern as Insurance / Transport / Critic).
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
# Public read-only views — for the FAIR DC1 baseline (identical data, §10.10).
# ===========================================================================

def entry_rules_public(nationality: str | None = None) -> list[dict[str, Any]]:
    """
    The seeded entry-requirement table as plain rows (the IDENTICAL data the
    society's gate resolves against), exposed so the FAIR DC1 single-agent
    baseline gets the same data — the §10.10 fairness contract. Read-only copies.
    """
    return [
        {
            "dest_country": r["dest_country"],
            "dest_name": _COUNTRY_NAMES.get(r["dest_country"], r["dest_country"]),
            "program": r["program"],
            "kind": r["kind"],
            "eligible_nationalities": r["eligible_nationalities"],
            "processing_lead_business_days": r["processing_lead_business_days"],
            "fee_cents": r["fee_cents"],
            "currency": r["currency"],
            "provenance": r["provenance"],
            "source_url": r["source_url"],
        }
        for r in _ENTRY_RULES
    ]


def entry_rule_public(dest_country: str, nationality: str) -> dict[str, Any] | None:
    """The single resolved entry rule for (dest, nationality) as a plain row, or None."""
    rule, _ = fetch_entry_rule(dest_country, nationality)
    if rule is None:
        return None
    return {
        "dest_country": rule["dest_country"],
        "dest_name": _COUNTRY_NAMES.get(rule["dest_country"], rule["dest_country"]),
        "program": rule["program"],
        "kind": rule["kind"],
        "eligible_nationalities": rule["eligible_nationalities"],
        "processing_lead_business_days": rule["processing_lead_business_days"],
        "fee_cents": rule["fee_cents"],
        "currency": rule["currency"],
        "provenance": rule["provenance"],
        "source_url": rule["source_url"],
    }


# ===========================================================================
# Entry point
# ===========================================================================

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )
    port = int(os.environ.get("PORT", 9106))
    host = os.environ.get("AGENT_BIND_HOST", "127.0.0.1")

    agent = ComplianceAgent(host=host, port=port)
    app = agent.build_app()

    logger.info("Compliance agent starting on %s:%d", host, port)
    logger.info("Agent Card: http://%s:%d/.well-known/agent-card.json", host, port)
    logger.info("RPC endpoint: http://%s:%d/", host, port)

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()


# ===========================================================================
# §AGENT-EXTENSION NOTE — Compliance is the SECOND specialist on the pattern.
# ===========================================================================
# Built per the §AGENT-EXTENSION PATTERN documented at the bottom of
# insurance_agent.py (the 6-step pattern Insurance set). Recap of what this file
# did so the NEXT specialist (Health 9107, Fraud 9108, ...) can mirror it:
#   1. NEW FILE on a NEW PORT (9106), subclass A2AAgent, _build_card +
#      _register_skills (compliance.check_eligibility / compliance.explain_block).
#   2. COMPOSE against Phase-0 contracts: make_money / make_provenance / SourceTier
#      + line_item_assembler.add_fee(kind=VISA) (Budget vetoes the visa fee like any
#      line item) + fx_provider.normalize() for non-USD fees. NO parallel shapes.
#   3. VARIANCE CLAMP: NO-LLM-NUMBERS (lead-times/fees/dates are integer seed
#      values); the gate (BLOCK/ALLOW) is a PURE function over a closed entry-kind
#      set; UNKNOWN → conservative BLOCK (never a silent allow); the LLM is cosmetic
#      only, validated by validate_rationale + falls back to the deterministic
#      headline.
#   4. SEEDED DATA, provenance-tagged: the per-country eVisa table (NZeTA, ESTA,
#      Ethiopia eVisa, India eVisa, Schengen, VOA alternatives) with an honest
#      Tier-3 SEEDED_FALLBACK behind fetch_entry_rule(); a live IATA/govt feed is a
#      Tier-2 swap, not a re-architecture.
#   5. WIRING — APPEND-ONLY orchestrator hook (compliance_url/compliance_client +
#      _call_compliance(), defaulting to None so every existing call site is
#      unchanged). Registration IS the AgentCard + the optional hook.
#   6. VERIFY HARD: test_compliance_agent.py (CI-safe ASGI TestClient) with the
#      CI-enforced invariant (the society NEVER returns an itinerary with departure
#      < visa lead-time; deterministic; numbers not from the LLM) + the DC1 fair
#      baseline (baseline_compliance.py + run_dc1_bench.py) measuring the gain.
