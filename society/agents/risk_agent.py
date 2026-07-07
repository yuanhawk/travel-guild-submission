"""
risk_agent.py — RISK specialist (Travel Guild, Track-3 A2A / L1 proactive).

═══════════════════════════════════════════════════════════════════════════════
WHAT THIS IS
═══════════════════════════════════════════════════════════════════════════════
The Risk agent is the **L1 proactive signal CONSOLIDATOR** of the Travel
Guild. It is the THIRD roadmap specialist built on the §AGENT-EXTENSION
PATTERN (insurance_agent.py 9105 → compliance_agent.py 9106 → THIS 9107).

Unlike Insurance/Compliance (money-path GATES), Risk is **OFF the money path**:
it emits PLANNING-INPUT signals only — no checkout, no mandate, no fee. It does
NOT book and it does NOT decide traveler-judgment (fitness/allergy etc., per the
AGENTS.md scope anchor). It provides SIGNALS that change the *bookable plan*
(avoid/buffer/flag a window) and surfaces info the Planner/Critic may use.

Three seeded, deterministic, provenance-tagged PLANNING-INPUT profiles
(NO-LLM-NUMBERS — every number is a seeded constant, never minted by the LLM):

  (a) CYCLONE / TYPHOON likelihood  by region × month   (the flagship)
  (b) MEDIAN DELAY (minutes)        by region × mode     (flight / rail / bus)
  (c) SEISMIC RESILIENCE            by region            (expected-IMPACT weight;
        occurrence is unforecastable, but expected impact is knowable — Japan
        high resilience vs Türkiye/Ethiopia low — the §scope-anchor "weight-but-
        can't-forecast hazard").

Skill: ``risk.assess`` → per-leg risk signals + deterministic advisory flags:
  {cyclone_likelihood, median_delay_min, seismic_resilience, advisory[],
   reason_codes[], decisions{avoid_window, buffer_connection_min, flag}}.

═══════════════════════════════════════════════════════════════════════════════
THE §AGENT-EXTENSION PATTERN (mirrors compliance_agent.py / insurance_agent.py)
═══════════════════════════════════════════════════════════════════════════════
  1. NEW FILE on a NEW PORT (9107), subclass A2AAgent, _build_card +
     _register_skills (risk.assess).
  2. COMPOSE against Phase-0 contracts: make_provenance / SourceTier (provenance
     on every sourced signal) + RiskReasonCode (the closed reason-code set Risk
     OWNS, mapped to perils by peril_crosswalk for Insurance). NO parallel shapes.
     Risk is off the money path → it emits NO money line item (no make_money /
     LineItemAssembler call), only signals.
  3. VARIANCE CLAMP: NO-LLM-NUMBERS — likelihoods/medians/resilience are integer/
     float seed constants; the avoid/buffer/flag DECISION is a PURE threshold
     function over closed sets; UNKNOWN region → conservative FLAG (never a silent
     "safe"); the LLM is cosmetic only, validated by validate_rationale + falls
     back to the deterministic advisory text.
  4. SEEDED DATA, provenance-tagged: per-region seed tables with an honest Tier-3
     SEEDED_FALLBACK behind fetch_region_profile(); a live hazard feed
     (JTWC/GDACS/OAG) is a Tier-2 swap, not a re-architecture.
  5. WIRING — APPEND-ONLY orchestrator hook (risk_url/risk_client +
     _call_risk(), defaulting to None so every existing call site + test is
     unchanged). The Planner CONSUMES the signals additively: it only changes
     scenarios that CARRY a cyclone/delay condition — S1–S5 stay byte-identical
     var-0. Registration IS the AgentCard + the optional hook.
  6. VERIFY HARD: test_risk_agent.py (CI-safe ASGI TestClient) with the CI-
     enforced invariants (deterministic signals; numbers from seed not LLM;
     UNKNOWN never silent-safe; avoid/buffer thresholds pure) + the cyclone-window
     fair baseline (baseline_risk.py + run_risk_bench.py) measuring the gain.

Runnable service: HOST / PORT env, defaults 0.0.0.0:9107 (next free port after
Compliance's 9106 — see §AGENT-EXTENSION PATTERN in insurance_agent.py).

Security: no secrets; the only network is the OPTIONAL cosmetic DashScope call
(behind a key check + a validator); all numbers are seeded.
"""

from __future__ import annotations

import json
import logging
import os
import unicodedata
from typing import Any

import uvicorn

from agents.a2a_agent import (
    A2AAgent,
    _data_part,
    _new_artifact,
)
from core.contracts import (
    DO_NOT_RECOMMEND_COUNTRIES,
    RiskReasonCode,
    SourceTier,
    is_provenance_expired,
    is_valid_risk_reason,
    make_provenance,
    stale_provenance_note,
)

logger = logging.getLogger("risk_agent")


# ---------------------------------------------------------------------------
# D5 advisory-categorization reason-code resolution.
#
# The State-Dept advisory→reason_code mapping (assess_leg) must use the
# advisory's CAUSE, not a blanket CIVIL_UNREST: ARMED_CONFLICT for war/active
# conflict (the ONLY category that reaches WAR/EXC-WAR-2 via the crosswalk),
# CRIME for crime/kidnapping advisories, and a GENERIC `advisory_elevated`
# signal for a high advisory with no specific seeded cause.
#
# `RiskReasonCode.ADVISORY_ELEVATED` is added to contracts.py (D5) by the
# contracts owner; the peril_crosswalk owner maps it to PERSONAL_LIABILITY (a
# benign proxy, NOT WAR). We resolve the enum's *value* defensively so this
# module stays importable regardless of the parallel landing order — falling
# back to the exact spec literal "advisory_elevated" until the enum lands.
_ADVISORY_ELEVATED_CODE: str = getattr(
    RiskReasonCode, "ADVISORY_ELEVATED",
    type("‹x›", (), {"value": "advisory_elevated"})(),
).value
_ARMED_CONFLICT_CODE: str = RiskReasonCode.ARMED_CONFLICT.value
_CRIME_CODE: str = RiskReasonCode.CRIME.value
_CIVIL_UNREST_CODE: str = RiskReasonCode.CIVIL_UNREST.value


# ===========================================================================
# Seed metadata (Tier-1 cache, provenance-tagged). NO-LLM-NUMBERS: every value
# below is a fixed seeded constant. A live hazard feed becomes a Tier-2 adapter
# swap behind fetch_region_profile() — NOT a re-architecture.
# ===========================================================================

_SEED_FETCHED_AT = "2026-06-10"
_SEED_TTL_DAYS = 90
_SEED_TTL_SECONDS = _SEED_TTL_DAYS * 24 * 3600

# --- Deterministic decision thresholds (pure constants; the DECISION is a pure
#     threshold function over these — never the LLM). -------------------------
# Cyclone likelihood is a seeded probability in BASIS POINTS (0–10000) so the
# number is an exact integer (no float drift across runs / machines).
CYCLONE_AVOID_BP = 3000      # >= 30% monthly likelihood → AVOID the window
CYCLONE_FLAG_BP = 1000       # >= 10% monthly likelihood → FLAG (surface, don't avoid)
# Seasonal flood-risk thresholds (same 0–10000 index scale). FLAG bar is HIGHER
# than cyclone's: flood seasonality is noisier and the seed is a within-region
# index, so we require a clearer in-season signal before surfacing.
FLOOD_AVOID_BP = 3000        # >= peak flood season → AVOID the window
FLOOD_FLAG_BP = 1500         # >= elevated flood season → FLAG (surface, don't avoid)
# Seasonal WILDFIRE-likelihood thresholds (same 0–10000 index scale). Mirrors
# cyclone exactly (AVOID 3000 / FLAG 1000): a peak fire-season month surfaces an
# AVOID, a shoulder month a FLAG. The signal is a SEASONAL fire-weather/likelihood
# index, NOT a live active-fire detection (that is the documented EFFIS/GWIS
# Tier-2 swap) — so the advisory is precaution-oriented, never an evacuation order.
WILDFIRE_AVOID_BP = 3000     # >= peak fire season → AVOID the window
WILDFIRE_FLAG_BP = 1000      # >= elevated fire season → FLAG (surface, don't avoid)
# Seasonal DROUGHT index threshold. Drought is ADVISORY-ONLY (no AVOID): a drought
# never makes a destination do-not-travel, it surfaces planning impacts (water
# restrictions, river-cruise/ferry curtailment, compounding fire risk, food
# prices). The bar is HIGH — only a genuinely SEVERE drought season surfaces.
DROUGHT_FLAG_BP = 4000       # >= severe drought season → FLAG (advisory only)
# Median delay (minutes) at/above which the Planner should BUFFER connections.
DELAY_BUFFER_THRESHOLD_MIN = 45
# Seismic resilience is a seeded score 0–100 (expected-IMPACT weight; HIGH =
# resilient). Below this → surface a resilience advisory (prefer resilient stock).
SEISMIC_LOW_RESILIENCE = 50

# UNKNOWN-region conservative defaults (never a silent "safe").
_UNKNOWN_BUFFER_MIN = DELAY_BUFFER_THRESHOLD_MIN  # buffer conservatively
_UNKNOWN_SEISMIC = 50                              # neither resilient nor fragile


# ---------------------------------------------------------------------------
# (a) CYCLONE / TYPHOON likelihood — region × month (the flagship).
# Likelihood is the seeded MONTHLY chance of a cyclone/typhoon AFFECTING the
# region, in basis points (0–10000). Months are 1–12; an absent month = 0 bp
# (negligible). Seeded from public climatology (cyclone seasonality), tagged as
# SEEDED — a live JTWC/BoM/JMA feed is the Tier-2 swap.
# ---------------------------------------------------------------------------
_CYCLONE_BY_REGION_MONTH: dict[str, dict[int, int]] = {
    # NW-Australia / Top End — Austral cyclone season Nov–Apr, peak Jan–Mar.
    "au-northwest": {11: 1500, 12: 2800, 1: 4200, 2: 4500, 3: 3500, 4: 1500},
    # NE-Australia / Coral Sea (Cairns/Townsville) — peak Jan–Mar.
    "au-northeast": {11: 1200, 12: 2500, 1: 3800, 2: 4200, 3: 3200, 4: 1200},
    # Philippines (Luzon/Visayas) — NW-Pacific typhoon belt, peak Jul–Nov.
    "ph-luzon": {6: 1500, 7: 2800, 8: 3500, 9: 4000, 10: 4200, 11: 3000, 12: 1500},
    # Okinawa / SW-Japan — NW-Pacific, peak Aug–Sep.
    "jp-okinawa": {6: 800, 7: 2000, 8: 3500, 9: 3800, 10: 1800},
    # US Gulf Coast (Atlantic hurricane season Jun–Nov, peak Aug–Oct).
    "us-gulf": {6: 800, 7: 1200, 8: 2800, 9: 3800, 10: 2200, 11: 800},
    # Fiji / South Pacific — Austral season Nov–Apr, peak Jan–Mar.
    "fj-southpacific": {11: 1500, 12: 2500, 1: 3500, 2: 3800, 3: 3000, 4: 1500},
}

# ---------------------------------------------------------------------------
# (a.2) SEASONAL FLOOD-RISK INDEX — region × month (basis points, 0–10000).
# DERIVED from the Global Flood Monitor event database (de Bruijn et al., Nature
# Scientific Data 2019; GFM 2014-07..2023-03, CC-BY-4.0), resolved to admin-1
# (province) and normalized WITHIN-region (peak month → ~4000). This encodes the
# SEASONAL PATTERN (which months flood), NOT an absolute probability — absolute
# calibration is the documented Tier-2 swap to GloFAS/Copernicus EWDS river
# discharge + return-period products. Province granularity is deliberate: Bali's
# wet season ≠ national Indonesia, so an off-season (e.g. June) Bali trip does not
# false-alarm. CONTEXTUAL + ADDITIVE: a region absent here → 0 bp → no flood
# signal, so every region not listed stays byte-identical (var-0 preserved).
_FLOOD_BY_REGION_MONTH: dict[str, dict[int, int]] = {
    # Bali (ID) — NW-monsoon wet season Nov–Mar, peak Dec–Jan; dry Apr–Oct.
    "id-bali":    {11: 2333, 12: 4000, 1: 3000, 2: 2667, 3: 2333},
    # Bangkok / Central Thailand — SW-monsoon late season Aug–Oct, peak Sep–Oct.
    "th-bangkok": {8: 1600, 9: 4000, 10: 3200},
    # Japan (Honshu/Kanto) — baiu rains Jun–Jul + typhoon Sep–Oct, peak Sep–Oct.
    "jp":         {7: 2667, 8: 2667, 9: 4000, 10: 4000},
    # Türkiye (Marmara/Black-Sea) — summer convective flash floods Jun–Oct, peak Aug.
    "tr":         {6: 2545, 7: 2182, 8: 4000, 9: 3273, 10: 1818},
}

# (a.2-bis) #51 — FLOOD SEED-COMPLETENESS for typhoon/monsoon sub-regions that
# already carry a CYCLONE/wet season but were MISSING the flood channel the same
# rains produce (caught by reference/flood_seismic_audit.py — Manila/Luzon, Naha/
# Okinawa, tropical-N Australia, Vienna/Danube). HONEST under-warning fix, NOT an
# over-warn: each pattern follows the region's DOCUMENTED flood season —
#   • tropical regions copy their country parent's GFM pattern (season matches);
#   • temperate Victoria gets its own COOL-season pattern (its summer-peaked parent
#     would be wrong-season → under/over-warn);
#   • ARID au-uluru / au-kangaroo are DELIBERATELY EXCLUDED (the Red Centre does not
#     flood seasonally — auto-inheriting the national pattern there would over-warn,
#     the same trap that ruled out seismic proximity-inference).
# CONTEXTUAL + ADDITIVE (a city outside these months stays byte-identical, var-0).
# NOTE (audit NIT): "arid-excluded" below means au-uluru/au-kangaroo specifically.
# au-sydney / au-tassie are NOT excluded — they ALREADY carry (modest) flood seeds
# (au-sydney 1200, au-tassie 1000); whether to RAISE Sydney given the 2021/22
# Hawkesbury-Nepean floods is a magnitude revisit deferred to #22 seed-reconciliation.
_FLOOD_BY_REGION_MONTH.update({
    # Luzon/Manila — SW-monsoon + NW-Pacific typhoon rains Jun–Dec (mirrors the
    # country-level 'ph' flood seed).
    "ph-luzon":    {1: 300, 2: 400, 3: 200, 5: 800, 6: 2000, 7: 2500, 8: 2800,
                    9: 2200, 10: 2000, 11: 1800, 12: 1000},
    # Okinawa/SW-Japan — baiu + typhoon flooding Jul–Oct (mirrors the 'jp' seed).
    "jp-okinawa":  {7: 2667, 8: 2667, 9: 4000, 10: 4000},
    # NE-Australia / Coral-Sea (Cairns/Townsville) — tropical wet season Dec–Apr.
    "au-northeast": {1: 2200, 2: 2500, 3: 1800, 4: 1200, 12: 1000},
    # NW-Australia / Kimberley-Pilbara — monsoon wet season Dec–Mar.
    "au-northwest": {1: 2200, 2: 2500, 3: 1800, 4: 1200, 12: 1000},
    # Victoria (Melbourne/Geelong) — temperate: riverine floods late winter–spring,
    # peak Oct (2022 event). Hand-authored cool-season pattern (NOT the northern
    # summer-monsoon peak, which is wrong for the temperate south).
    "au-vic":      {8: 1200, 9: 1600, 10: 2200, 11: 1800},
    # Vienna / Austria — Danube spring–summer snowmelt + rain (mirrors the
    # country-level 'at' flood seed).
    "at-vienna":   {4: 800, 5: 1400, 6: 1800, 7: 1700, 8: 1300},
    # #22 — Northern Italy / Po Valley (Milan/Turin/Venice/Bologna…). NEW sub-region
    # so the humid-continental north is NOT over-warned with central-Italy summer
    # WILDFIRE (it-north carries NO wildfire seed — that is the over-warn fix). Po
    # basin floods autumn (Oct–Nov peak) + spring Alpine snowmelt (Apr–May); Venice
    # acqua-alta autumn. Flood-dominant, no Mediterranean-summer fire.
    "it-north":    {1: 1500, 2: 1200, 4: 1200, 5: 1400, 10: 2500, 11: 4000, 12: 2200},
})

# ---------------------------------------------------------------------------
# (a.3) SEASONAL WILDFIRE-LIKELIHOOD INDEX — region × month (basis points,
# 0–10000). Encodes the FIRE SEASON (which months carry elevated fire-weather /
# burned-area likelihood) for regions with a pronounced, well-documented season.
# Seeded from public fire climatology (Northern/Southern Hemisphere fire seasons;
# Mediterranean summer, California autumn-peak, Austral summer, Amazon dry-season,
# Canadian boreal summer). Calibration is the documented Tier-2 swap to Copernicus
# EFFIS (European Forest Fire Information System) / GWIS (Global Wildfire
# Information System) burned-area + fire-danger products — NOT a runtime dep.
# Mirrors cyclone EXACTLY: CONTEXTUAL + ADDITIVE — a region absent here → 0 bp →
# NO wildfire signal, so every unseeded region stays byte-identical (var-0). Peak
# months ~3000–4500 (AVOID), shoulder months ~1000–2000 (FLAG). Months 1–12.
_WILDFIRE_BY_REGION_MONTH: dict[str, dict[int, int]] = {
    # --- Austral summer fire season (Dec–Feb peak; SW Australia / Victoria) ---
    # SW Western Australia — hot dry summer, peak Jan–Feb (Black-summer style).
    "au-southwest": {12: 2000, 1: 4000, 2: 4000, 3: 2500, 11: 1200},
    # Victoria (Melbourne/Geelong) — peak Jan–Feb (2009 Black Saturday region).
    "au-vic":       {12: 1800, 1: 3800, 2: 4000, 3: 2200},
    # --- US West / California — fire season Jun–Oct, AUTUMN peak Sep–Oct -------
    "us-west":      {6: 1500, 7: 2500, 8: 3500, 9: 4500, 10: 4500, 11: 2000},
    # US Southwest (AZ/NM) — pre-monsoon peak May–Jun, eases with Jul monsoon.
    "us-southwest": {5: 3500, 6: 4000, 7: 2000, 8: 1200},
    # --- Mediterranean Europe — hot dry summer Jun–Sep, peak Jul–Aug ----------
    "es-east":      {6: 2000, 7: 4000, 8: 4500, 9: 2500},
    "gr-attica":    {6: 2500, 7: 4000, 8: 4500, 9: 3000},
    "it-central":   {6: 1800, 7: 3500, 8: 4000, 9: 2200},
    "tr":           {6: 1800, 7: 3500, 8: 4000, 9: 2500},
    "pt-alentejo":  {6: 2200, 7: 4000, 8: 4500, 9: 3000},
    "fr-south":     {6: 1500, 7: 3000, 8: 3800, 9: 2000},
    # --- Amazon / Brazil dry season — Aug–Oct burning peak --------------------
    "br-southeast": {8: 2500, 9: 4000, 10: 3500},
    # --- Canadian boreal — fire season Jun–Aug, peak Jul–Aug -----------------
    "ca-west":      {6: 2000, 7: 4000, 8: 4000, 9: 2000},
}

# ---------------------------------------------------------------------------
# (a.4) SEASONAL DROUGHT INDEX — region × month (basis points, 0–10000).
# Encodes the pronounced DRY SEASON for regions with a documented, recurring
# drought exposure. ADVISORY-ONLY: drought never triggers AVOID — it surfaces
# planning impacts (water restrictions, river-cruise/ferry curtailment,
# compounding wildfire risk, food prices). Conservative + only where defensible:
# peaks ~4000–5000 in the driest months. Calibration is the documented Tier-2 swap
# to the Copernicus GDO (Global Drought Observatory) / Combined Drought Indicator
# — NOT a runtime dep. CONTEXTUAL + ADDITIVE: a region absent here → 0 bp → NO
# drought signal → byte-identical (var-0) for every unseeded region. Months 1–12.
_DROUGHT_BY_REGION_MONTH: dict[str, dict[int, int]] = {
    # SW Western Australia — Mediterranean climate; severe dry summer Dec–Mar.
    "au-southwest": {12: 4000, 1: 5000, 2: 5000, 3: 4000},
    # US Southwest (AZ/NM) — chronic aridity; driest pre-monsoon May–Jun.
    "us-southwest": {5: 4500, 6: 5000, 7: 4000},
    # California / US West — dry-season deficit peak Jul–Sep (recurrent drought).
    "us-west":      {7: 4000, 8: 4500, 9: 4500, 10: 4000},
    # Eastern Spain — summer rainfall deficit, peak Jul–Aug (water restrictions).
    "es-east":      {7: 4500, 8: 5000, 9: 4000},
    # Kenya coast — long-dry season Jan–Mar (recurrent drought exposure).
    "ke-coast":     {1: 4000, 2: 4500, 3: 4000},
}

# #51 CALIBRATION — fold the Copernicus FWI calibration into the wildfire seed.
# society/wildfire_calibration.json (built by reference/apply_fire_calibration.py
# from cems-fire-historical-v1 FWI, 2018–2022, season-extension max-merge) is
# MAX-merged here: it can only RAISE or EXTEND a fire season, NEVER LOWER a hand
# value (fail-conservative) — so FWI anchor artefacts (e.g. coastal-Melbourne) can
# never erase the inland hand seed, and the desert (us-southwest) stays bounded to
# a real season, not a year-round flag. DROUGHT is deliberately NOT calibrated from
# Copernicus SPEI (an anomaly vs 1991–2020, not the recurring dry-season
# CLIMATOLOGY the hand seed encodes) — the hand drought seed stands. Absent file →
# hand seed unchanged (var-0). Regenerate: reference/apply_fire_calibration.py.
try:
    _wfc_path = os.path.join(os.path.dirname(__file__), "..", "wildfire_calibration.json")
    with open(_wfc_path, encoding="utf-8") as _wf:
        for _rg, _mm in json.load(_wf).get("wildfire", {}).items():
            _tgt = _WILDFIRE_BY_REGION_MONTH.setdefault(_rg, {})
            for _mo, _bp in _mm.items():
                if int(_bp) > _tgt.get(int(_mo), 0):
                    _tgt[int(_mo)] = int(_bp)
except (FileNotFoundError, json.JSONDecodeError, OSError):
    pass  # NIT#2: a truncated/partial JSON fails CONSERVATIVE (hand seed stands), never crashes import

# ---------------------------------------------------------------------------
# (#51-a) ENSO (El Niño / La Niña) SEASONAL MODULATION — a capped MULTIPLIER on
# the already-firing cyclone/flood/drought channels, NEVER a standalone hazard.
#
# WHY: ENSO modulates a WHOLE SEASON of hydromet hazard across basins — a La Niña
# year worsens Atlantic hurricanes and wettens the SE-Asia / Australian monsoon;
# an El Niño shifts the W-Pacific typhoon belt and drives Indonesian / E-Australian
# DROUGHT and Peru-coast flooding. Without it the static seasonal seeds silently
# UNDER-state in the active phase and the traveler gets a confidently-wrong window.
#
# DESIGN (fail-conservative, var-0-preserving — mirrors the wildfire FWI max-merge):
#   • A multiplier in BASIS POINTS where 10000 == 1.0x (the IDENTITY). Every seeded
#     value is >= 10000 — ENSO may only AMPLIFY a signal, NEVER cut a hand seed.
#   • It applies ONLY to a month that ALREADY carries a base signal (base 0 stays 0)
#     and ONLY within the documented basin+phase window → it can never invent an
#     off-season hazard.
#   • Gated by ONE seeded current-phase constant (ENSO_CURRENT_PHASE), sourced +
#     as-of-dated below — NOT wall-clock (bump it deliberately like the content
#     calendar). Shipped "el_nino" (2026 forecast): the channel amplifies the
#     el_nino-seeded months and is the IDENTITY everywhere else. Still APPEND-ONLY
#     and fully deterministic (var-0); a "neutral" phase makes it a complete no-op.
#   • Folded into the EXISTING NATURAL_DISASTER / FLOOD / ADVISORY_ELEVATED reason
#     codes — NO new RiskReasonCode, so peril_crosswalk stays TOTAL untouched.
#
# A live NOAA ONI / IRI-ENSO-plume fetch is the documented Tier-2 swap (like the
# EFFIS/GWIS wildfire swap): it would only update ENSO_CURRENT_PHASE, not re-arch.
# ---------------------------------------------------------------------------
# Current ENSO phase — SEEDED, as-of 2026-06-22 (NOAA CPC ENSO advisory + IRI
# plume: El Niño PRESENT and STRENGTHENING into NH winter 2026-27; P(El Niño) ≥97%
# JJA–SON 2026; peak SON 2026/DJF 2026-27. Niño-3.4 magnitude unsettled across
# reports (~+0.7°C CPC / ~+1.7°C IRI) → "moderate, strengthening". A FORECAST, not a
# live observation — bumped DELIBERATELY (like the content calendar), still var-0
# (a module constant, never wall-clock). Single phase: no within-2026 transition is
# forecast. "neutral" | "el_nino" | "la_nina". The live NOAA-ONI swap (opt-in,
# RISK_ENSO_LIVE) overrides this only off the deterministic path — see
# _resolve_enso_phase. NB: el_nino activates the el_nino seeds across ALL basins
# (australian/se_asia DRY, e_pacific_coast WET, nw_pacific late-season WET) — a
# deliberate, deterministic seed change, NOT byte-identical to the neutral build.
ENSO_CURRENT_PHASE: str = "el_nino"

# Region → ENSO basin (the teleconnection region a place belongs to). A region
# absent here has NO basin → ENSO is the identity for it (additive / var-0).
_REGION_ENSO_BASIN: dict[str, str] = {
    # Australian / Coral-Sea basin (La Niña ↑ cyclones+floods; El Niño ↑ drought)
    "au-northwest": "australian", "au-northeast": "australian",
    "au-southwest": "australian", "au-vic": "australian",
    # SE-Asia / maritime-continent monsoon (La Niña wetter; El Niño drier).
    # vn-* + th-* + id-bali are monsoon-dominated → kept on se_asia. (The Philippines
    # is a typhoon country → BOTH its regions live in nw_pacific below, not here.)
    "id-bali": "se_asia",
    "vn-central": "se_asia", "vn-north": "se_asia", "vn-south": "se_asia",
    "th-bangkok": "se_asia", "th-south": "se_asia",
    # NW-Pacific typhoon basin (La Niña ↑ mid-season Jul–Oct western landfalls;
    # El Niño ↑ late-season Oct–Dec). Fires the WET family on each region's existing
    # cyclone/flood seed — every region here carries a cyclone and/or flood seed, so
    # all fire under the relevant phase (NOT inert). BOTH Philippine regions are here
    # (ph = Cebu/Visayas/Mindanao via "cebu"→"ph"; ph-luzon = Manila) so the whole
    # typhoon-exposed country gets one consistent typhoon-ENSO treatment.
    "jp": "nw_pacific", "jp-okinawa": "nw_pacific",
    "tw": "nw_pacific", "kr": "nw_pacific",
    "cn-main": "nw_pacific", "hk": "nw_pacific", "mo": "nw_pacific",
    "ph": "nw_pacific", "ph-luzon": "nw_pacific",
    # Atlantic hurricane basin (La Niña ↑ Atlantic season; El Niño ↓ — we only ↑)
    "us-gulf": "atlantic", "us-northeast": "atlantic",
    "mx-caribbean": "atlantic", "bs": "atlantic", "cu": "atlantic",
    # E-Pacific / Peru-Ecuador coast (El Niño ↑ coastal flooding)
    "pe-coast": "e_pacific_coast", "mx-pacific": "e_pacific_coast",
}

# Basin → phase → hazard-family ("wet" | "dry") → {month: multiplier_bp}.
# 10000 == 1.0x (the IDENTITY). Hand-seeded from public ENSO teleconnection
# climatology (NOAA ONI composites). Only AMPLIFYING (>= 10000) entries appear; a
# phase/month with no documented teleconnection is simply absent → identity. The
# "wet" family amplifies the WET hazards (cyclone + flood); the "dry" family
# amplifies DROUGHT. Splitting by family is the HONEST encoding: El Niño SUPPRESSES
# Australian cyclones while driving Australian drought, so its seed lives under
# "dry" only and can NEVER amplify the cyclone channel (a single phase dict would
# wrongly raise cyclones — this split prevents that).
_ENSO_BASIN_SENSITIVITY: dict[str, dict[str, dict[str, dict[int, int]]]] = {
    # Australian basin — La Niña amplifies the Nov–Apr cyclone/flood (WET) season;
    # El Niño suppresses cyclones but drives summer DROUGHT (DRY only).
    "australian": {
        "la_nina": {
            "wet": {11: 11500, 12: 12500, 1: 13000, 2: 13000, 3: 12500, 4: 11500},
        },
        "el_nino": {
            "dry": {12: 13000, 1: 14000, 2: 14000, 3: 13000},
        },
    },
    # SE-Asia / maritime-continent monsoon — La Niña wetter (flood ↑); El Niño
    # drier (drought ↑).
    "se_asia": {
        "la_nina": {
            "wet": {10: 11500, 11: 13000, 12: 13000, 1: 12500, 2: 11500},
        },
        "el_nino": {
            "dry": {6: 13000, 7: 13500, 8: 14000, 9: 13500},
        },
    },
    # Atlantic hurricane basin — La Niña amplifies the Aug–Oct WET peak.
    "atlantic": {
        "la_nina": {
            "wet": {8: 12000, 9: 13000, 10: 12500, 11: 11500},
        },
    },
    # E-Pacific / Peru-Ecuador coast — El Niño drives coastal flooding (WET).
    "e_pacific_coast": {
        "el_nino": {
            "wet": {12: 12500, 1: 14000, 2: 14500, 3: 14000, 4: 12500},
        },
    },
    # NW-Pacific typhoon basin — La Niña shifts genesis WEST → more/earlier
    # mid-season (Jul–Oct) western landfalls (Philippines/Vietnam/S-China/Taiwan/
    # S-Japan/Okinawa); El Niño shifts genesis east + makes storms stronger/
    # longer-lived → the season EXTENDS LATE (Oct–Dec). WET family only: NW-Pac is
    # not a drought basin here. Amplify-only means we deliberately do NOT encode El
    # Niño's SUPPRESSION of early western landfalls (we never cut a hand seed) — an
    # honest under-model, not an invented cut; the seasonal Jul–Oct vs Oct–Dec split
    # still captures the first-order shift. Assigned regions carry cyclone and/or
    # flood seeds (most carry BOTH — jp/tw/kr/hk/jp-okinawa/cn-main/ph/ph-luzon all
    # fire via cyclone AND flood), so every channel actually fires. The El Niño
    # Nov–Dec entries DO bite (e.g. ph
    # cyclone Nov 2800→3640, cn-main cyclone Nov 600→780, tw cyclone Nov 800→1040) —
    # not latent. Refs: Wang & Chan 2002; PRIMAVERA WNP-TC/ENSO 2023.
    "nw_pacific": {
        "la_nina": {
            "wet": {7: 11500, 8: 12500, 9: 13000, 10: 12000},
        },
        "el_nino": {
            "wet": {10: 12000, 11: 13000, 12: 12500},
        },
    },
}

# Which hazard FAMILY each channel belongs to: cyclone + flood are WET hazards;
# drought is the DRY hazard. ENSO amplifies a channel only via its family's seed.
_ENSO_CHANNEL_FAMILY: dict[str, str] = {
    "cyclone": "wet", "flood": "wet", "drought": "dry",
}


# ISO-code basin sets for the cyclone NOUN — bare 2-letter region codes the prefix
# tests miss. Audited 2026-06-26: Caribbean/Central-American codes (cu/jm/do/bs/…)
# silently defaulted to "Cyclone" → should be "Hurricane"; China/Macau/Cambodia/
# Micronesia/bare-`ph` (incl. Tacloban) defaulted to "Cyclone" → should be "Typhoon".
_TYPHOON_ISO = frozenset({"gu", "mp", "hk", "mo", "kh", "fm", "mh"})
_ATLANTIC_ISO = frozenset({
    "mx", "bs", "bz", "cu", "cr", "dm", "gt", "ni", "pa", "pr", "sv", "jm", "hn",
    "do", "co", "ag", "bb", "gd", "kn", "lc", "vc", "tt", "cv", "ht", "ky", "tc", "bm",
})


def _cyclone_basin_noun(src: str, region: str) -> str:
    """Basin-correct noun for the tropical-cyclone advisory (same phenomenon, different basin name).

    The same storm is a HURRICANE in the N-Atlantic / NE-Pacific (US/Mexico/Caribbean/Gulf), a
    TYPHOON in the NW-Pacific (Philippines/Japan/Taiwan/Vietnam/Korea/China/Guam), and a CYCLONE in
    the Indian Ocean / S-Pacific / Australia. Prefers the seed-source basin tag (pagasa/jma=typhoon,
    noaa/nhc=hurricane, bom/fms=cyclone); falls back to region geography (prefix OR bare-ISO set)
    when the source is a generic advisory. Pure function of (deterministic) inputs -> var-0 preserved.
    """
    s = (src or "").lower()
    r = (region or "").lower()
    base = r.split("-")[0]
    if ("typhoon" in s or r.startswith(("ph", "jp", "tw", "vn", "kr", "cn", "th"))
            or base in _TYPHOON_ISO):
        return "Typhoon"
    if ("nhc" in s or "hurricane" in s or r.startswith(("us-", "mx-"))
            or "caribbean" in r or "gulf" in r or base in _ATLANTIC_ISO):
        return "Hurricane"
    return "Cyclone"  # Indian Ocean / S-Pacific / Australia (and umbrella default)


# Tier-2 LIVE ONI swap — injected resolver, mirrors the #51 emergency-overlay
# firewall. None → the seeded ENSO_CURRENT_PHASE is used (the deterministic / var-0
# default). A live phase is consulted ONLY when RISK_ENSO_LIVE is set AND a client
# is injected; any failure degrades to the seed (never fabricates a phase, never
# raises). The live value is NEVER added to _request_digest — it stays off the
# deterministic path entirely. Provenance: "live:noaa-cpc-oni" vs the seeded
# "seed:noaa-cpc-oni-2026-06-22".
_enso_phase_client = None  # callable[[], str] | None — injected; None → seeded phase


def _resolve_enso_phase() -> str:
    """Seeded ENSO_CURRENT_PHASE by default (deterministic, var-0). Returns a LIVE
    phase only when RISK_ENSO_LIVE is set AND a client is injected; any failure (or
    an unrecognised value) degrades to the seed. Never raises, never fabricates."""
    if not os.environ.get("RISK_ENSO_LIVE") or _enso_phase_client is None:
        return ENSO_CURRENT_PHASE
    try:
        live = _enso_phase_client()
        return live if live in ("el_nino", "la_nina", "neutral") else ENSO_CURRENT_PHASE
    except Exception:
        return ENSO_CURRENT_PHASE


def _enso_modulate(
    base_bp: int,
    region: str | None,
    months: list[int],
    channel: str,
    *,
    phase: str | None = None,
) -> int:
    """ENSO-modulated channel bp (cyclone/flood/drought). PURE + deterministic.

    Returns ``base_bp`` UNCHANGED (the identity) when: the resolved phase is neutral
    (whole channel is a no-op, var-0), the region has no
    basin, the basin has no seed for the current phase + the channel's hazard
    family, or no spanned month carries a multiplier. Otherwise scales ``base_bp``
    by the MAX seeded multiplier over the spanned months, clamped to
    [base_bp, 10000] — it can only RAISE (never lower a hand seed) and never exceeds
    the 0–10000 bp ceiling. A ``base_bp`` of 0 (off-season) stays 0: ENSO never
    invents a hazard. The wet/dry family split means an El Niño phase can amplify
    Australian DROUGHT without ever wrongly amplifying Australian CYCLONES."""
    cur = _resolve_enso_phase() if phase is None else phase
    if cur == "neutral" or base_bp <= 0 or region is None:
        return base_bp
    basin = _REGION_ENSO_BASIN.get(region)
    if basin is None:
        return base_bp
    family = _ENSO_CHANNEL_FAMILY.get(channel)
    if family is None:
        return base_bp
    by_month = _ENSO_BASIN_SENSITIVITY.get(basin, {}).get(cur, {}).get(family)
    if not by_month:
        return base_bp
    mult_bp = max((by_month.get(m, 10000) for m in months), default=10000)
    if mult_bp <= 10000:
        return base_bp
    # NIT#3 (audit) — fail-conservative ceiling pin: every seeded bp is <= 10000,
    # so a base already at/above the ceiling is returned UNCHANGED. This guarantees
    # ENSO can NEVER lower a hand seed even if a future seed were mis-authored above
    # 10000 (the final min(...,10000) below would otherwise cap such a base down).
    if base_bp >= 10000:
        return base_bp
    scaled = (int(base_bp) * int(mult_bp)) // 10000
    return min(max(scaled, base_bp), 10000)


# ---------------------------------------------------------------------------
# (#51-c) RISK SCOPE-DISCLOSURE — HONESTY: name the peril classes the model does
# NOT cover, so a traveler never reads SILENCE as safety. Static, hand-authored,
# deterministic + stable-ordered. Adds NO number, NO reason_code, NO decision —
# it is a pure disclosure surface a caller/UI MAY attach; it does NOT touch the
# always-on assess_leg/assess output (so every plain trip stays byte-identical).
# ---------------------------------------------------------------------------
_RISK_SCOPE_NOT_COVERED: tuple[dict[str, str], ...] = (
    {
        "peril": "earthquake_occurrence_forecast",
        "why_not": (
            "Earthquake OCCURRENCE is not predictable; the model weights expected "
            "seismic IMPACT (building-code resilience) only, never a quake forecast."
        ),
        "check_instead": "USGS earthquake hazard + local building-code/retrofit status.",
    },
    {
        "peril": "tsunami",
        "why_not": (
            "Tsunami inundation depends on a specific offshore rupture and local "
            "bathymetry the seasonal seeds do not model."
        ),
        "check_instead": "NOAA/UNESCO-IOC tsunami warnings + local evacuation-zone maps.",
    },
    {
        "peril": "volcano",
        "why_not": (
            "Volcanic unrest/eruption + ashfall are not in the seeded hydromet model; "
            "ash can ground flights with little notice."
        ),
        "check_instead": "Smithsonian GVP + the relevant VAAC ash advisories.",
    },
    {
        "peril": "flash_flood_street_precision",
        "why_not": (
            "The flood channel is a SEASONAL within-region index, not a street-level "
            "flash-flood nowcast."
        ),
        "check_instead": "Local meteorological-service flash-flood warnings near travel dates.",
    },
    {
        "peril": "terrorism_attack_prediction",
        "why_not": (
            "Specific attacks are unpredictable; the model surfaces only STANDING "
            "government advisory levels, not an attack forecast."
        ),
        "check_instead": "Current government travel advisories (e.g. travel.state.gov).",
    },
    {
        "peril": "air_quality_realtime",
        "why_not": (
            "Real-time air quality (wildfire smoke, dust, pollution) is not modeled; "
            "only the seasonal wildfire LIKELIHOOD is."
        ),
        "check_instead": "A live AQI source (e.g. local agency / WAQI) near travel dates.",
    },
)


def risk_scope_disclosure() -> dict[str, Any]:
    """Honest, deterministic SCOPE disclosure: the peril classes the Risk model
    does NOT cover (so silence is never read as safety). Stable-ordered by peril;
    returns a fresh copy each call (callers cannot mutate the seed)."""
    items = sorted((dict(d) for d in _RISK_SCOPE_NOT_COVERED), key=lambda d: d["peril"])
    return {
        "disclosure": (
            "This risk model covers SEASONAL hydromet/transport/advisory hazards and "
            "expected seismic IMPACT only. The perils below are NOT modeled — their "
            "absence is NOT a safety signal; check the named source before travel."
        ),
        "not_covered": items,
        "source": "seed:risk-scope-disclosure-2026-06",
    }


# ---------------------------------------------------------------------------
# (#51-d) HAZARD → INTERRUPTION → INSURANCE / REBOOKING signal. A PURE function of
# the already-deterministic rollup, OFF the money path: it mints NO premium and
# changes NO total (Insurance still owns coverage via the existing _apply_insurance
# path). It turns the rollup into a human-facing interruption TIER + concrete
# rebooking guidance, making the safe-commerce reaction (hazard → react) explicit.
# Display-only by default → it adds no avoid and cannot regress a plain trip (var-0).
# ---------------------------------------------------------------------------
# Reason-code → the interruption DRIVER family + the rebooking line it implies.
_INTERRUPTION_REBOOKING_BY_REASON: dict[str, str] = {
    RiskReasonCode.NATURAL_DISASTER.value: (
        "Hold flexible-cancellation / changeable fares; keep a rebooking buffer day "
        "after the hazard window and confirm the carrier's storm/weather waiver policy."
    ),
    RiskReasonCode.FLOOD.value: (
        "Prefer lodging on higher floors away from low-lying/riverbank areas and book "
        "refundable rates; flooding can sever ground transfers at short notice."
    ),
    RiskReasonCode.TRANSPORT_DISRUPTION.value: (
        "Add the recommended connection buffer, avoid same-day tight connections, and "
        "prefer carriers with free same-day rebooking."
    ),
    RiskReasonCode.CIVIL_UNREST.value: (
        "Keep itineraries flexible, avoid protest hotspots, and confirm your policy's "
        "civil-unrest position before committing non-refundable spend."
    ),
    RiskReasonCode.ARMED_CONFLICT.value: (
        "Do not commit non-refundable travel into an armed-conflict advisory; coverage "
        "is typically excluded (EXC-WAR-2). Reconsider the destination/dates."
    ),
    RiskReasonCode.ADVISORY_ELEVATED.value: (
        "Recheck the government advisory before departure and prefer "
        "flexible-cancellation facilities."
    ),
    RiskReasonCode.CRIME.value: (
        "Use reputable local guides/transport and refundable bookings; keep plans "
        "adaptable around higher-crime areas."
    ),
}


def interruption_outlook(rollup: dict[str, Any] | None) -> dict[str, Any]:
    """Hazard → trip-interruption likelihood TIER + rebooking guidance.

    PURE + deterministic function of the Risk ROLL-UP (assess()'s rollup). OFF the
    money path: no premium, no total, no fee — Insurance owns coverage. Tiers:
      • "low"      — no flagged leg, no buffer, no avoid (a clean trip).
      • "moderate" — a flagged leg or a connection buffer, but no avoid window.
      • "elevated" — an avoid window present (a do-not-travel-window hazard).
      • "high"     — an avoid window AND an armed-conflict driver.
    Returns {likelihood_tier, drivers[], rebooking_guidance[],
    prefer_flexible_cancellation}. A clean trip → lowest tier, empty guidance,
    prefer_flexible_cancellation False (adds no avoid → byte-identical plain trip)."""
    rollup = rollup or {}
    any_avoid = bool(rollup.get("any_avoid_window"))
    buffer_min = int(rollup.get("max_buffer_connection_min", 0) or 0)
    flagged = list(rollup.get("flagged_legs", []) or [])
    # De-dup reason codes, STABLE order (deterministic; mirrors assess()).
    drivers: list[str] = []
    seen: set[str] = set()
    for rc in (rollup.get("all_reason_codes", []) or []):
        if rc not in seen:
            seen.add(rc)
            drivers.append(rc)

    if any_avoid:
        tier = "high" if RiskReasonCode.ARMED_CONFLICT.value in seen else "elevated"
    elif flagged or buffer_min > 0:
        tier = "moderate"
    else:
        tier = "low"

    # Rebooking guidance: the stable-ordered lines implied by the present drivers
    # (only when there is a real condition — a clean trip yields none).
    guidance: list[str] = []
    if tier != "low":
        for rc in drivers:
            line = _INTERRUPTION_REBOOKING_BY_REASON.get(rc)
            if line and line not in guidance:
                guidance.append(line)

    return {
        "likelihood_tier": tier,
        "drivers": drivers,
        "rebooking_guidance": guidance,
        # Mirrors the existing _risk_planning_directives flexible-cancellation hint:
        # an avoid window prefers flexible cancellation. Display-only (no avoid added).
        "prefer_flexible_cancellation": any_avoid,
        "source": "seed:risk-interruption-outlook-2026-06",
    }


# ---------------------------------------------------------------------------
# (b) MEDIAN DELAY (minutes) — region × mode {flight, rail, bus}.
# Seeded median-delay-on-day-of-travel snapshots: Japan rail famously low; EU/US
# higher; emerging-market road higher still. A live OAG/punctuality feed is the
# Tier-2 swap.
# ---------------------------------------------------------------------------
_DELAY_BY_REGION_MODE: dict[str, dict[str, int]] = {
    "jp":        {"flight": 12, "rail": 3,  "bus": 10},   # Japan — low
    "jp-okinawa": {"flight": 18, "rail": 5,  "bus": 14},
    "eu":        {"flight": 35, "rail": 18, "bus": 25},   # EU — moderate
    "us":        {"flight": 52, "rail": 28, "bus": 40},   # US — higher
    "us-gulf":   {"flight": 58, "rail": 30, "bus": 45},
    "au-northwest": {"flight": 40, "rail": 0,  "bus": 35},  # remote; no rail → 0 = N/A
    "au-northeast": {"flight": 38, "rail": 22, "bus": 33},
    "tr":        {"flight": 48, "rail": 25, "bus": 42},   # Türkiye
    "et":        {"flight": 65, "rail": 30, "bus": 70},   # Ethiopia — high
    "ph-luzon":  {"flight": 55, "rail": 20, "bus": 60},   # Philippines
    "fj-southpacific": {"flight": 45, "rail": 0, "bus": 38},
    "pg-portmoresby": {"flight": 50, "rail": 0, "bus": 55},  # PNG — no rail
}

# ---------------------------------------------------------------------------
# (c) SEISMIC RESILIENCE — region → expected-IMPACT weight (0–100, HIGH = more
# resilient). Occurrence is unforecastable; expected IMPACT (building codes,
# preparedness) is knowable (§scope-anchor "weight-but-can't-forecast"). Japan
# high; Türkiye/Ethiopia low. A live USGS/building-code feed is the Tier-2 swap.
# ---------------------------------------------------------------------------
_SEISMIC_RESILIENCE_BY_REGION: dict[str, int] = {
    "jp": 90, "jp-okinawa": 88,        # Japan — world-leading seismic codes
    "us": 80, "us-gulf": 78,
    "eu": 75,
    "au-northwest": 70, "au-northeast": 70,
    "fj-southpacific": 55,
    "ph-luzon": 45,                    # active seismicity, mixed codes
    "tr": 35,                          # Türkiye — elevated impact (2023 quakes)
    "et": 30,                          # Ethiopia — low resilience
    "id-bali": 35,                     # Indonesia/Bali — Ring of Fire, mixed codes
    "pg-portmoresby": 55,              # PNG — moderate (no seismic advisory noise)
    # th-bangkok is a fully-seeded flood/advisory region that previously lacked a
    # seismic entry and fell back to the neutral _UNKNOWN_SEISMIC (50) by accident
    # (audit #5). Central Thailand has low base hazard but soft-soil amplification
    # + mixed codes (cf. the 2025 Bangkok high-rise collapse from a distant quake):
    # a true low-to-moderate resilience, seeded just below the LOW threshold so the
    # resilience advisory surfaces rather than a silent neutral.
    "th-bangkok": 48,
}

# ---------------------------------------------------------------------------
# (d) CIVIL-UNREST advisory level — region → seeded unrest-advisory level in
# BASIS POINTS (0–10000), the deterministic likelihood that the travel window
# overlaps an ACTIVE civil-unrest advisory (protests / riots). HIGH = elevated.
# This is the §scope-anchor "weight-but-can't-forecast" signal: Risk does NOT
# predict a riot, it surfaces a STANDING government-advisory level the Planner
# weights and (via the peril crosswalk) Insurance maps to the CIVIL_UNREST
# coverage key. A live FCDO/State-Dept/Smartraveller advisory feed is the Tier-2
# swap. ADDITIVE + CONTEXTUAL: only regions with a SEEDED unrest level appear
# here; every other region is absent → 0 bp → NO civil_unrest signal (so the
# existing S1–S6 regions are untouched and stay var-0).
# ---------------------------------------------------------------------------
_CIVIL_UNREST_BY_REGION: dict[str, int] = {
    # Papua New Guinea / Port Moresby — Jan-2024 riots; standing Smartraveller
    # "reconsider your need to travel" unrest advisory. The seeded test region for
    # the Risk→Insurance civil_unrest→EXC-UNREST-1 composition (a BOOKABLE catalog
    # city, see ucp-merchant/catalog.go). NOT used by any S/DC scenario.
    "pg-portmoresby": 4500,
}

# Civil-unrest advisory thresholds (basis points; same scale as cyclone).
CIVIL_UNREST_FLAG_BP = 1000    # >= 10% standing-advisory level → FLAG (surface)
CIVIL_UNREST_ELEVATED_BP = 3000  # >= 30% → ELEVATED (high-severity advisory)

# Per-region provenance source labels (one source per region keyspace).
_REGION_SOURCE: dict[str, str] = {
    "au-northwest": "seed:bom-cyclone-climatology-2026",
    "au-northeast": "seed:bom-cyclone-climatology-2026",
    "ph-luzon": "seed:pagasa-typhoon-climatology-2026",
    "jp-okinawa": "seed:jma-typhoon-climatology-2026",
    "us-gulf": "seed:noaa-nhc-climatology-2026",
    "fj-southpacific": "seed:fms-cyclone-climatology-2026",
    "jp": "seed:society-region-profile-2026",
    "eu": "seed:society-region-profile-2026",
    "us": "seed:society-region-profile-2026",
    "tr": "seed:society-region-profile-2026",
    "et": "seed:society-region-profile-2026",
    "pg-portmoresby": "seed:smartraveller-png-advisory-2026",
    "id-bali": "seed:gfm-flood-climatology-2014-2023",
    "th-bangkok": "seed:gfm-flood-climatology-2014-2023",
}

# All seeded regions (union of the tables) — the closed keyspace.
_ALL_REGIONS: frozenset[str] = frozenset(
    set(_CYCLONE_BY_REGION_MONTH)
    | set(_FLOOD_BY_REGION_MONTH)
    | set(_DELAY_BY_REGION_MODE)
    | set(_SEISMIC_RESILIENCE_BY_REGION)
    | set(_CIVIL_UNREST_BY_REGION)
)

# City → region keyspace (the place-key reconcile; deterministic, lower-cased).
# A city not here resolves to UNKNOWN → conservative flag (never silent safe).
_CITY_TO_REGION: dict[str, str] = {
    # Australia
    "darwin": "au-northwest", "broome": "au-northwest", "exmouth": "au-northwest",
    "cairns": "au-northeast", "townsville": "au-northeast", "port douglas": "au-northeast",
    # Philippines / Japan / Pacific
    "manila": "ph-luzon", "cebu": "ph",
    "naha": "jp-okinawa", "okinawa": "jp-okinawa",
    "tokyo": "jp", "osaka": "jp", "kyoto": "jp",
    "suva": "fj-southpacific", "nadi": "fj-southpacific",
    # Flood-seasonality regions (GFM-derived; see _FLOOD_BY_REGION_MONTH).
    "bali": "id-bali", "denpasar": "id-bali", "kuta": "id-bali", "ubud": "id-bali",
    "bangkok": "th-bangkok",
    # US / EU
    "new orleans": "us-gulf", "houston": "us-gulf", "tampa": "us-gulf", "miami": "us-gulf",
    "new york": "us", "san francisco": "us", "los angeles": "us",
    "paris": "eu", "berlin": "eu", "rome": "eu", "amsterdam": "eu",
    # low-resilience anchors
    "istanbul": "tr", "ankara": "tr",
    "addis ababa": "et", "addis": "et",
    # civil-unrest test region (BOOKABLE catalog city; see catalog.go). ADDITIVE:
    # the only region carrying a SEEDED civil_unrest advisory → Risk emits
    # civil_unrest → peril_crosswalk → Insurance EXC-UNREST-1 (the composition).
    "port moresby": "pg-portmoresby", "port-moresby": "pg-portmoresby",
}

# ===========================================================================
# REGION EXPANSION (research-backed, 2026-06-17). Web-grounded seasonal
# climatology (NOAA/JMA/BoM/USGS/GEM/national met services) + State Dept
# advisory levels. Additive + CONTEXTUAL: each new region/month fires only
# its own signal; unseeded regions stay 0 -> var-0 preserved. civil_unrest is
# now ADVISORY-DRIVEN (L4->4000, L3->2000) generalizing the prior PNG-only seed
# (pg-portmoresby kept at its fixture value). See benchmark research artifact.
# ===========================================================================
_CYCLONE_BY_REGION_MONTH.update({
    "vn-central": {8: 800, 9: 2000, 10: 4000, 11: 3800, 12: 600},
    "vn-north": {6: 800, 7: 800, 8: 2500, 9: 4000, 10: 2000},
    "th-south": {1: 1000, 10: 800, 11: 2000, 12: 2500},
    "ws-samoa": {1: 4000, 2: 4200, 3: 3500, 4: 1500, 11: 1200, 12: 2500},
    "nz-north": {1: 1800, 2: 2500, 3: 2800, 4: 2000, 11: 800, 12: 1200},
    "mx-caribbean": {6: 700, 7: 1200, 8: 3000, 9: 4500, 10: 4000, 11: 1500},
    "mx-pacific": {6: 800, 7: 2500, 8: 4000, 9: 4500, 10: 3000, 11: 1200},
    "gt-highlands": {9: 800, 10: 1200},
    "cr-central": {8: 700, 9: 1200, 10: 1500, 11: 800},
    "us-northeast": {8: 1000, 9: 2500, 10: 1500},
    "us-hawaii": {6: 500, 7: 2500, 8: 4500, 9: 3500, 10: 1500, 11: 500},
})
_FLOOD_BY_REGION_MONTH.update({
    "vn-central": {9: 1500, 10: 2500, 11: 4500, 12: 3500},
    "vn-north": {6: 1500, 7: 1200, 8: 4000, 9: 3000, 10: 1000},
    "vn-south": {5: 1500, 6: 2000, 7: 2500, 8: 2500, 9: 3500, 10: 4000, 11: 2000},
    "th-south": {5: 1500, 6: 2000, 7: 2000, 8: 2000, 9: 2500, 10: 3500, 11: 4000, 12: 2500},
    "ws-samoa": {1: 2800, 2: 2800, 3: 2200, 4: 1200, 11: 1000, 12: 2000},
    "nz-north": {1: 2000, 2: 2500, 3: 2800, 4: 2500, 5: 1500, 11: 1000, 12: 1200},
    "nz-south": {5: 1000, 6: 1200, 7: 1200, 8: 1000, 10: 1200, 11: 1000},
    "mx-central": {6: 1500, 7: 3500, 8: 4000, 9: 3800, 10: 2000},
    "mx-caribbean": {6: 1000, 7: 1500, 8: 2000, 9: 2500, 10: 2000, 11: 800},
    "mx-pacific": {6: 800, 7: 2000, 8: 2500, 9: 4000, 10: 2500, 11: 700},
    "br-southeast": {1: 4500, 2: 4500, 3: 3500, 4: 1500, 11: 1500, 12: 3000},
    "gt-highlands": {5: 1500, 6: 3000, 7: 3500, 8: 3500, 9: 4000, 10: 3500, 11: 900},   # ERA5: Nov is the dry-season transition (short rivers, no cyclone)
    "cr-central": {5: 1500, 6: 2500, 7: 2000, 8: 2500, 9: 3500, 10: 4000, 11: 3500, 12: 1500},   # Dec kept FLAG: ERA5 shows wet S-Pacific (Quepos ~14mm/day) — err on safety (audit)
    "co-andes": {3: 800, 4: 3000, 5: 3500, 6: 1000, 7: 700, 8: 700, 9: 1500, 10: 4000, 11: 3500, 12: 1500},
    "pe-coast": {1: 4000, 2: 4500, 3: 4000, 4: 2000, 11: 800, 12: 2000},
    "cl-central": {6: 2500, 7: 3500, 8: 3000, 9: 1500},
    "ar-pampas": {1: 1500, 2: 1500, 3: 2000, 4: 2000, 10: 1500, 11: 1500},
    "it-central": {1: 2800, 2: 1800, 3: 1000, 9: 800, 10: 2500, 11: 4000, 12: 3500},
    "gr-attica": {1: 1500, 9: 1000, 10: 2200, 11: 3800, 12: 2500},
    "es-east": {8: 600, 9: 1200, 10: 4000, 11: 3500, 12: 1500},
    "fr-south": {1: 1200, 8: 700, 9: 2000, 10: 4200, 11: 3800, 12: 2500},
    "fr-north": {1: 3200, 2: 3500, 3: 2500, 4: 1500, 5: 1000, 11: 1000, 12: 1800},
    "de-west": {1: 2800, 2: 3500, 3: 4000, 4: 2500, 5: 1200, 12: 1500},
    "uk-south": {1: 3500, 2: 3800, 3: 2500, 4: 1200, 10: 800, 11: 1500, 12: 2500},
    "nl-randstad": {1: 2000, 2: 2000, 3: 1200, 11: 800, 12: 1500},
    "us-west": {1: 4500, 2: 4000, 3: 3000, 4: 1500, 11: 1500, 12: 3500},
    "us-pacnw": {1: 4000, 2: 3000, 3: 2000, 4: 1500, 10: 1500, 11: 3500, 12: 4500},
    "us-northeast": {1: 2500, 2: 2500, 3: 3000, 4: 2000, 9: 1500, 10: 1500, 12: 2000},
    "ca-east": {3: 1500, 4: 3500, 5: 4000, 6: 2500, 7: 1500},
    "ca-west": {1: 3500, 2: 3000, 3: 2500, 4: 2000, 5: 1500, 11: 2000, 12: 3000},
    "us-hawaii": {1: 3000, 2: 2500, 3: 2000, 11: 1500, 12: 2500},
})
_SEISMIC_RESILIENCE_BY_REGION.update({
    "vn-central": 42,
    "vn-north": 42,
    "vn-south": 42,
    "th-south": 48,
    "ws-samoa": 25,
    "nz-north": 60,
    "nz-south": 55,
    "mx-central": 45,
    "mx-caribbean": 80,
    "mx-pacific": 45,
    "br-southeast": 75,
    "gt-highlands": 30,
    "cr-central": 50,
    "co-andes": 45,
    "pe-coast": 35,
    "cl-central": 75,
    "ar-pampas": 72,
    "it-central": 48,
    "it-north": 48,  # #22 Po Valley/Alps — match it-central (stay on the warn side;
                     # Friuli is active) rather than suppress the low-resilience advisory
    "gr-attica": 45,
    "es-east": 65,
    "fr-south": 62,
    "fr-north": 78,
    "de-west": 80,
    "uk-south": 85,
    "nl-randstad": 82,
    "us-west": 65,
    "us-pacnw": 55,
    "us-northeast": 80,
    "ca-east": 82,
    "ca-west": 58,
    "us-hawaii": 60,
})
# GEOPOLITICAL is ADVISORY-DRIVEN. Each region carries its country's CURRENT US
# State Dept advisory level (1-4). The advisory SIGNAL is derived from that level
# at runtime (L3 reconsider -> 2000bp, L4 do-not-travel -> 4000bp), but D5 #38
# routes it by CAUSE (see _ADVISORY_CATEGORY_BY_REGION / _advisory_reason_code):
# ARMED_CONFLICT for war (the only code that reaches WAR/EXC-WAR-2), CRIME for
# crime/kidnapping, generic ADVISORY_ELEVATED otherwise — NOT a blanket
# civil_unrest. Only a genuine civil-unrest CAUSE feeds the CIVIL_UNREST signal
# (the legacy _CIVIL_UNREST_BY_REGION seed, e.g. pg-portmoresby's 4500 fixture).
# Below-threshold restraint is REAL: an L1/L2 region is ASSESSED (level known) and
# correctly emits no advisory signal -- restraint by judgment, not absence of data.
_ADVISORY_LEVEL_BY_REGION: dict[str, int] = {  # US State Dept level 1-4 (current); seed:state-dept-advisories-2026-06
    "ar-pampas": 1, "au-northeast": 1, "au-northwest": 1, "br-southeast": 2,
    "ca-east": 1, "ca-west": 1, "cl-central": 2, "co-andes": 3, "cr-central": 1,
    "de-west": 2, "es-east": 2, "et": 3, "eu": 1, "fj-southpacific": 2,
    "fr-north": 2, "fr-south": 2, "gr-attica": 1, "gt-highlands": 3, "id-bali": 2,
    "it-central": 2, "jp": 1, "jp-okinawa": 1, "mx-caribbean": 2, "mx-central": 2,
    "mx-pacific": 2, "nl-randstad": 1, "nz-north": 1, "nz-south": 1, "pe-coast": 2,
    "pg-portmoresby": 3, "ph-luzon": 2, "th-bangkok": 2, "th-south": 2, "tr": 2,
    "uk-south": 2, "us": 1, "us-gulf": 1, "us-hawaii": 1, "us-northeast": 1,
    "us-pacnw": 1, "us-west": 1, "vn-central": 1, "vn-north": 1, "vn-south": 1,
    "ws-samoa": 1,
}


# ---------------------------------------------------------------------------
# D5 #38 — per-region advisory CATEGORY (the CAUSE of the L3/L4 advisory), so
# the derived reason-code is honest. A State-Dept L3/L4 is rarely about civil
# unrest: it is usually CRIME/kidnapping, ARMED CONFLICT, terrorism, health, or
# natural disaster. We carry the cause explicitly here and DEFAULT any
# uncategorized L3/L4 region to the GENERIC `advisory_elevated` signal — NEVER a
# blanket `civil_unrest` (which is reserved for actual protests/riots, seeded in
# _CIVIL_UNREST_BY_REGION) and NEVER `armed_conflict` (which is the ONLY category
# that reaches WAR/EXC-WAR-2 via the crosswalk).
#
# Categories (closed set, values are RiskReasonCode .value strings):
#   "civil_unrest"  — actual protests/riots driving the advisory
#   "armed_conflict"— war / active armed conflict (→ WAR / EXC-WAR-2)
#   "crime"         — crime / kidnapping advisory (e.g. Sabah ESSZONE)
#   <absent>        — generic high advisory, no specific seeded cause → ADVISORY_ELEVATED
# Conservative-by-default: an unlisted L3/L4 region degrades to ADVISORY_ELEVATED
# (a real flag), so we never silently drop the advisory and never over-claim WAR.
#
# C2 (single-authority): contracts.DO_NOT_RECOMMEND_COUNTRIES is the ONLY
# armed-conflict / WAR-decline authority. The "armed_conflict" CAUSE category —
# the ONLY one that reaches WAR/EXC-WAR-2 via the crosswalk and that implies a
# decline — is therefore RESERVED for regions whose country is a member of that
# set. A conflict-ADJACENT but BOOKABLE country (BY/IL/IR/LB) must NOT be marked
# armed_conflict: it degrades to the GENERIC `advisory_elevated` signal (a real
# flag, still insurable), so it never wrongly reaches WAR / EXC-WAR-2 nor implies
# a decline. `_advisory_category` enforces this at the boundary (defense in
# depth) and `_assert_seed_tables_valid` rejects any armed_conflict seed for a
# non-member country at import (so the invariant can never silently drift).
_ADVISORY_CATEGORY_BY_REGION: dict[str, str] = {
    # --- ARMED CONFLICT (war / active conflict) → ARMED_CONFLICT → WAR/EXC-WAR-2.
    #     RESERVED for DO_NOT_RECOMMEND_COUNTRIES members ONLY (see C2 above).
    #     BY/IL/IR/LB are conflict-adjacent but BOOKABLE → they are NOT seeded
    #     here; their L3/L4 LEVEL still flags them as generic advisory_elevated
    #     (flag + insurable), never armed_conflict / WAR / EXC-WAR-2.
    # --- CRIME / kidnapping advisories → CRIME → PERSONAL_LIABILITY (benign proxy).
    "my-sabah": "crime",               # Sabah ESSZONE — standing kidnapping advisory
    "hn": "crime",                     # Honduras — violent crime
    "ni": "crime",                     # Nicaragua — crime + arbitrary enforcement
    "gt": "crime", "gt-highlands": "crime",   # Guatemala — violent crime
    "co": "crime", "co-andes": "crime",       # Colombia — crime/kidnapping
    "zw-zimbabwe": "crime",            # Zimbabwe — crime
    "gy": "crime",                     # Guyana — crime
    "mr": "crime",                     # Mauritania — crime + terrorism
    "pg-portmoresby": "civil_unrest",  # PNG/Port Moresby — 2024 riots (also seeded unrest)
    "pg-highlands": "crime",           # PNG highlands — tribal violence / crime
    # --- everything else (ao, az, bi, cg-congo, cm, et*, mz, pk, rw, td, tz,
    #     ug*, gw …) has no specific seeded cause → GENERIC advisory_elevated.
}


def _region_iso2(region: str | None) -> str:
    """The ISO-3166 alpha-2 country code a region key belongs to, UPPER-cased.

    Region keys encode the country as the segment before the first '-' (e.g.
    'my-sabah' → 'MY', 'co-andes' → 'CO', 'ua' → 'UA'). Deterministic, no I/O.
    Returns '' for an empty/None region (→ never a DO_NOT_RECOMMEND member)."""
    return (region or "").split("-", 1)[0].upper()


def _advisory_category(region: str | None) -> str | None:
    """The seeded CAUSE category of a region's advisory, or None (→ generic).

    C2 single-authority guard: the ONLY category that reaches WAR/EXC-WAR-2 and
    implies a decline is "armed_conflict", and it is RESERVED for regions whose
    country is a member of contracts.DO_NOT_RECOMMEND_COUNTRIES. A seeded
    armed_conflict for a non-member (a bookable, conflict-adjacent country)
    degrades here to the generic `advisory_elevated` signal (None ⇒ generic),
    so a bookable country can never wrongly reach WAR / EXC-WAR-2."""
    cat = _ADVISORY_CATEGORY_BY_REGION.get(region or "")
    if cat == "armed_conflict" and _region_iso2(region) not in DO_NOT_RECOMMEND_COUNTRIES:
        return None
    return cat


def _advisory_level_bp(region: str | None) -> int:
    """State Dept advisory LEVEL -> basis points (deterministic, cause-agnostic).
    L4 (do-not-travel) -> 4000 (ELEVATED), L3 (reconsider) -> 2000 (FLAG), else 0.
    This is the raw LEVEL signal; the CAUSE (category) decides the reason-code."""
    return {4: 4000, 3: 2000}.get(_ADVISORY_LEVEL_BY_REGION.get(region or "", 1), 0)


def _advisory_reason_code(region: str | None) -> str:
    """Map a region's advisory CAUSE → the honest RiskReasonCode .value.
    Generic (no seeded cause) → ADVISORY_ELEVATED, never blanket CIVIL_UNREST."""
    cat = _advisory_category(region)
    if cat == "armed_conflict":
        return _ARMED_CONFLICT_CODE
    if cat == "crime":
        return _CRIME_CODE
    if cat == "civil_unrest":
        return _CIVIL_UNREST_CODE
    return _ADVISORY_ELEVATED_CODE


def _advisory_civil_unrest_bp(region: str | None) -> int:
    """LEGACY accessor (kept for benchmark/stress_test.py compatibility): the
    advisory-derived basis points that flow to the CIVIL_UNREST signal.

    Post-D5 this returns the level bp ONLY for regions whose advisory CAUSE is
    actually civil unrest (so the stress-test prediction `cu = max(seed, this)`
    keeps predicting `civil_unrest` exactly for the civil-unrest-category regions
    and 0 elsewhere). Crime/armed-conflict/generic advisories now flow through
    their own reason-codes via _advisory_reason_code, NOT civil_unrest."""
    if _advisory_category(region) == "civil_unrest":
        return _advisory_level_bp(region)
    return 0


_CITY_TO_REGION.update({
    "acapulco": "mx-pacific",
    "alicante": "es-east",
    "amsterdam": "nl-randstad",
    "antigua": "gt-highlands",
    "apia": "ws-samoa",
    "arequipa": "pe-coast",
    "athens": "gr-attica",
    "athina": "gr-attica",
    "attica": "gr-attica",
    "auckland": "nz-north",
    "baja california sur": "mx-pacific",
    "barcelona": "es-east",
    "bellingham": "us-pacnw",
    "berlin": "de-west",
    "bogota": "co-andes",
    "bogotá": "co-andes",
    "bonn": "de-west",
    "bordeaux": "fr-north",
    "boston": "us-northeast",
    "brighton": "uk-south",
    "bristol": "uk-south",
    "buenos aires": "ar-pampas",
    "burnaby": "ca-west",
    "cabo san lucas": "mx-pacific",
    "cali": "co-andes",
    "callao": "pe-coast",
    "cambridge": "uk-south",
    "can tho": "vn-south",
    "cancun": "mx-caribbean",
    "cartagena": "co-andes",
    "cdmx": "mx-central",
    "christchurch": "nz-south",
    "ciudad de guatemala": "gt-highlands",
    "ciudad de mexico": "mx-central",
    "cologne": "de-west",
    "concepcion": "cl-central",
    "cordoba": "ar-pampas",
    "costa rica": "cr-central",
    "cote d'azur": "fr-south",
    "cozumel": "mx-caribbean",
    "cusco": "pe-coast",
    "cuzco": "pe-coast",
    "da nang": "vn-central",
    "danang": "vn-central",
    "den haag": "nl-randstad",
    "dunedin": "nz-south",
    "dusseldorf": "de-west",
    "eugene": "us-pacnw",
    "firenze": "it-central",
    "florence": "it-central",
    "frankfurt": "de-west",
    "guatemala city": "gt-highlands",
    "ha long": "vn-north",
    "ha noi": "vn-north",
    "hai phong": "vn-north",
    "halong": "vn-north",
    "hamburg": "de-west",
    "hamilton": "ca-east",
    "hanoi": "vn-north",
    "hartford": "us-northeast",
    "hawke's bay": "nz-north",
    "hawkes bay": "nz-north",
    "hcmc": "vn-south",
    "hilo": "us-hawaii",
    "ho chi minh": "vn-south",
    "ho chi minh city": "vn-south",
    "hoi an": "vn-central",
    "honolulu": "us-hawaii",
    "huatulco": "mx-pacific",
    "hue": "vn-central",
    "ile-de-france": "fr-north",
    "invercargill": "nz-south",
    "jaco": "cr-central",
    "kauai": "us-hawaii",
    "kelowna": "ca-west",
    "khao lak": "th-south",
    "ko pha ngan": "th-south",
    "ko samui": "th-south",
    "koh phangan": "th-south",
    "koh samui": "th-south",
    "koln": "de-west",
    "krabi": "th-south",
    "la paz": "mx-pacific",
    "la plata": "ar-pampas",
    "lahaina": "us-hawaii",
    "liberia": "cr-central",
    "lima": "pe-coast",
    "limon": "cr-central",
    "london": "uk-south",
    "london ontario": "ca-east",
    "los angeles": "us-west",
    "los cabos": "mx-pacific",
    "lyon": "fr-north",
    "machu picchu": "pe-coast",
    "manzanillo": "mx-pacific",
    "marseille": "fr-south",
    "maui": "us-hawaii",
    "mazatlan": "mx-pacific",
    "medellin": "co-andes",
    "medellín": "co-andes",
    "mekong delta": "vn-south",
    "mendoza": "ar-pampas",
    "merida": "mx-caribbean",
    "mexico city": "mx-central",
    "mexico df": "mx-central",
    "montpellier": "fr-south",
    "montreal": "ca-east",
    "nelson": "nz-south",
    "new york": "us-northeast",
    "new york city": "us-northeast",
    "newark": "us-northeast",
    "nice": "fr-south",
    "nimes": "fr-south",
    "ninh binh": "vn-north",
    "north island": "nz-north",
    "northland": "nz-north",
    "nyc": "us-northeast",
    "oahu": "us-hawaii",
    "oakland": "us-west",
    "olympia": "us-pacnw",
    "ottawa": "ca-east",
    "oxford": "uk-south",
    "paris": "fr-north",
    "petropolis": "br-southeast",
    "phang nga": "th-south",
    "philadelphia": "us-northeast",
    "phuket": "th-south",
    "playa del carmen": "mx-caribbean",
    "portland": "us-pacnw",
    "provence": "fr-south",
    "providence": "us-northeast",
    "puerto vallarta": "mx-pacific",
    "puno": "pe-coast",
    "quang nam": "vn-central",
    "quebec city": "ca-east",
    "queenstown": "nz-south",
    "quepos": "cr-central",
    "quetzaltenango": "gt-highlands",
    "quintana roo": "mx-caribbean",
    "richmond bc": "ca-west",
    "rio": "br-southeast",
    "rio de janeiro": "br-southeast",
    "roma": "it-central",
    "rome": "it-central",
    "rosario": "ar-pampas",
    "rotterdam": "nl-randstad",
    "sacramento": "us-west",
    "saigon": "vn-south",
    "samoa": "ws-samoa",
    "san diego": "us-west",
    "san francisco": "us-west",
    "san jose": "us-west",
    "santiago": "cl-central",
    "santos": "br-southeast",
    "sao paulo": "br-southeast",
    "seattle": "us-pacnw",
    "south island": "nz-south",
    "southampton": "uk-south",
    "surat thani": "th-south",
    "surrey": "ca-west",
    "tacoma": "us-pacnw",
    "tarragona": "es-east",
    "the hague": "nl-randstad",
    "thua thien hue": "vn-central",
    "toronto": "ca-east",
    "tulum": "mx-caribbean",
    "upolu": "ws-samoa",
    "utrecht": "nl-randstad",
    "valencia": "es-east",
    "valparaiso": "cl-central",
    "vancouver": "ca-west",
    "venezia": "it-north",
    "venice": "it-north",
    "victoria": "ca-west",
    "vina del mar": "cl-central",
    "vung tau": "vn-south",
    "waikiki": "us-hawaii",
    "wellington": "nz-north",
    "yucatan": "mx-caribbean",
    "zihuatanejo": "mx-pacific",
})
_REGION_SOURCE.update({
    "vn-central": "seed:research-climatology-2026-06",
    "vn-north": "seed:research-climatology-2026-06",
    "vn-south": "seed:research-climatology-2026-06",
    "th-south": "seed:research-climatology-2026-06",
    "ws-samoa": "seed:research-climatology-2026-06",
    "nz-north": "seed:research-climatology-2026-06",
    "nz-south": "seed:research-climatology-2026-06",
    "mx-central": "seed:research-climatology-2026-06",
    "mx-caribbean": "seed:research-climatology-2026-06",
    "mx-pacific": "seed:research-climatology-2026-06",
    "br-southeast": "seed:research-climatology-2026-06",
    "gt-highlands": "seed:research-climatology-2026-06",
    "cr-central": "seed:research-climatology-2026-06",
    "co-andes": "seed:research-climatology-2026-06",
    "pe-coast": "seed:research-climatology-2026-06",
    "cl-central": "seed:research-climatology-2026-06",
    "ar-pampas": "seed:research-climatology-2026-06",
    "it-central": "seed:research-climatology-2026-06",
    "it-north": "seed:research-climatology-2026-06",  # #22 Po Valley (no summer fire)
    "gr-attica": "seed:research-climatology-2026-06",
    "es-east": "seed:research-climatology-2026-06",
    "fr-south": "seed:research-climatology-2026-06",
    "fr-north": "seed:research-climatology-2026-06",
    "de-west": "seed:research-climatology-2026-06",
    "uk-south": "seed:research-climatology-2026-06",
    "nl-randstad": "seed:research-climatology-2026-06",
    "us-west": "seed:research-climatology-2026-06",
    "us-pacnw": "seed:research-climatology-2026-06",
    "us-northeast": "seed:research-climatology-2026-06",
    "ca-east": "seed:research-climatology-2026-06",
    "ca-west": "seed:research-climatology-2026-06",
    "us-hawaii": "seed:research-climatology-2026-06",
})
_ALL_REGIONS = frozenset(
    set(_CYCLONE_BY_REGION_MONTH) | set(_FLOOD_BY_REGION_MONTH)
    | set(_DELAY_BY_REGION_MODE) | set(_SEISMIC_RESILIENCE_BY_REGION)
    | set(_CIVIL_UNREST_BY_REGION)
)

# ===========================================================================
# Stage D — 84 LP region climate/risk profiles (LP500 coverage, 2026-06-17).
# Cyclone/flood/seismic/advisory seeds + city→region mappings for LP catalog.
# Every new region is provenance-tagged in _REGION_SOURCE (required by
# _assert_seed_tables_valid). _ALL_REGIONS is recomputed after this block.
# ===========================================================================
_CYCLONE_BY_REGION_MONTH.update({
    'ae': {5: 600, 6: 800, 10: 700, 11: 900},
    'bs': {6: 800, 7: 1200, 8: 3000, 9: 4200, 10: 2500, 11: 700},
    'bz': {6: 800, 7: 1200, 8: 2800, 9: 3800, 10: 3500, 11: 1500},
    'cn': {6: 1000, 7: 2000, 8: 3500, 9: 3000, 10: 800},
    'cr': {7: 800, 8: 1200, 9: 1800, 10: 1500, 11: 600},
    'cu': {6: 1000, 7: 1500, 8: 2500, 9: 4000, 10: 3500, 11: 1500},
    'dj': {5: 300},
    'dm': {6: 700, 7: 1100, 8: 3000, 9: 4000, 10: 2500, 11: 600},
    'gt': {6: 800, 7: 1800, 8: 2800, 9: 3800, 10: 3000, 11: 1500},
    'in': {5: 1200, 6: 1000, 10: 1800, 11: 3500, 12: 2000},
    'kh': {9: 800, 10: 2000, 11: 2500, 12: 1200},
    'kr': {7: 1500, 8: 3000, 9: 3500, 10: 1500},
    'lk': {1: 800, 10: 1200, 11: 2500, 12: 2000},
    'mg': {1: 4000, 2: 4200, 3: 3500, 4: 1800, 12: 1500},
    'mv': {1: 1300, 10: 1200, 11: 1800, 12: 1500},
    'mw': {},  # #67: landlocked — no direct cyclone strike; remnant-flood already AVOID in _FLOOD (re-channel, not under-warn)
    'mz': {1: 3500, 2: 4000, 3: 3500, 4: 1500, 11: 1200, 12: 2500},
    'ni': {6: 800, 7: 1200, 8: 2000, 9: 3500, 10: 4000, 11: 2500},
    'om': {5: 1800, 6: 2200, 10: 1600, 11: 2000},
    'pa': {10: 600, 11: 900},
    'pf': {1: 3000, 2: 4000, 3: 3500, 4: 1500, 11: 800, 12: 1800},
    'pk': {5: 700, 6: 900, 10: 800, 11: 700},
    'pr': {6: 700, 7: 1100, 8: 3200, 9: 4500, 10: 2200, 11: 600},
    'sa': {5: 800, 6: 1000, 10: 700, 11: 900},
    'sc': {1: 1500, 2: 2000, 3: 1800, 11: 1200, 12: 1600},
    'tw': {6: 1000, 7: 2500, 8: 4200, 9: 4000, 10: 2000, 11: 800},
    'tz': {1: 800, 2: 1000, 3: 1200, 4: 900},
    'zw': {},  # #67: landlocked — no direct cyclone strike; remnant-flood already AVOID in _FLOOD (re-channel, not under-warn)
})  # Stage D

_FLOOD_BY_REGION_MONTH.update({
    'ae': {1: 500, 2: 600, 3: 700},
    'al': {1: 1800, 2: 1200, 10: 1000, 11: 1800, 12: 2000},
    'aw': {10: 800, 11: 1200, 12: 900},
    'az': {4: 1500, 5: 2500, 6: 2000, 10: 1200},
    'ba': {3: 1000, 4: 1800, 5: 2400, 6: 1800},
    'be': {6: 800, 7: 2000, 8: 1500, 11: 1000, 12: 800},
    'bo': {1: 1800, 2: 2200, 3: 1600, 4: 800, 12: 1200},
    'bs': {6: 600, 7: 800, 8: 1500, 9: 2000, 10: 1200},
    'bt': {6: 1500, 7: 2800, 8: 2500, 9: 1200},
    'bw': {1: 1400, 2: 1600, 3: 1200, 4: 500, 12: 900},
    'bz': {6: 800, 7: 1200, 8: 1500, 9: 1800, 10: 1600, 11: 800},
    'cg': {3: 1400, 4: 1800, 10: 1600, 11: 2000, 12: 1800},
    'ch': {5: 900, 6: 1500, 7: 1200, 8: 1000},
    'cn': {5: 800, 6: 1800, 7: 3000, 8: 2800, 9: 1600},
    'cr': {5: 1200, 6: 2000, 7: 1800, 8: 2200, 9: 2800, 10: 2500, 11: 1500},
    'cu': {5: 600, 6: 900, 7: 1200, 8: 1500, 9: 2000, 10: 1800},
    'cy': {1: 3000, 2: 2500, 3: 1800, 10: 1200, 11: 2000, 12: 2800},
    'cz': {5: 800, 6: 1500, 7: 2000, 8: 2500, 9: 1200},
    'dj': {10: 1200, 11: 1800},
    'dk': {1: 1500, 2: 1200, 10: 600, 11: 1200, 12: 1500},
    'dm': {6: 800, 7: 1200, 8: 1500, 9: 1800, 10: 2000, 11: 1000},
    'dz': {1: 1000, 9: 1000, 10: 1800, 11: 1600, 12: 1200},
    'ec': {1: 1200, 2: 1800, 3: 2200, 4: 1800, 5: 900, 10: 600, 11: 900, 12: 1100},
    'ee': {3: 800, 4: 1200, 5: 700},
    'eg': {1: 500, 2: 500, 11: 600, 12: 600},
    'fi': {4: 900, 5: 1200, 6: 600},
    'ge': {4: 1200, 5: 2500, 6: 2000, 7: 1200},
    'gh': {4: 700, 5: 1200, 6: 1800, 7: 1000, 9: 900, 10: 1200, 11: 700},
    'gl': {6: 600, 7: 900, 8: 700},
    'gt': {5: 1200, 6: 2000, 7: 2200, 8: 2200, 9: 2500, 10: 2000, 11: 1500},
    'hr': {3: 800, 4: 1200, 10: 1500, 11: 2000, 12: 1200},
    'hu': {4: 1000, 5: 1800, 6: 1500, 9: 1500, 10: 1000},
    'ie': {1: 1100, 2: 900, 10: 700, 11: 1000, 12: 1200},
    'in': {6: 1800, 7: 3000, 8: 3200, 9: 2800, 10: 1800},
    'ir': {1: 1000, 2: 1200, 3: 1800, 4: 2800, 5: 1800, 11: 800},
    'is': {4: 800, 5: 1000, 6: 600},
    'jo': {1: 1800, 2: 1800, 3: 1000, 11: 1200, 12: 1800},
    'ke': {3: 1500, 4: 2500, 5: 2200, 10: 1500, 11: 2000, 12: 1500},
    'kg': {4: 800, 5: 1500, 6: 1800, 7: 1200, 8: 900},
    'kh': {7: 1200, 8: 1800, 9: 2200, 10: 2500, 11: 1800},
    'kr': {6: 1000, 7: 2500, 8: 2800, 9: 1800},
    'kz': {3: 1500, 4: 2500, 5: 1500},
    'la': {6: 1000, 7: 2500, 8: 3000, 9: 2800, 10: 1500},
    'lb': {1: 1200, 2: 1000, 3: 700, 12: 900},
    'lk': {5: 1800, 6: 600, 10: 1500, 11: 2800, 12: 2000},   # ERA5: Jun dry in the NE/dry zone (SW wet-zone is lk-southwest)
    'ma': {1: 2000, 2: 1800, 3: 1200, 10: 1200, 11: 2000, 12: 2200},
    'md': {4: 600, 5: 1000, 6: 1400, 7: 1200, 8: 900},
    'me': {1: 1000, 2: 900, 3: 700, 11: 800, 12: 1200},
    'mg': {1: 2000, 2: 2200, 3: 1800, 4: 1000, 11: 800, 12: 1500},
    'mn': {7: 1500, 8: 1800, 9: 800},
    'mv': {5: 1200, 6: 1500, 7: 1400, 8: 1300, 9: 1200, 10: 1500, 11: 1400},
    'mw': {1: 3500, 2: 4000, 3: 3000, 4: 1500, 11: 1200, 12: 2500},
    'mz': {1: 3500, 2: 4500, 3: 4000, 4: 2000, 11: 1200, 12: 2500},
    'na': {1: 2000, 2: 3000, 3: 3500, 4: 2000, 5: 800},
    'ni': {5: 800, 6: 1500, 7: 2000, 8: 2500, 9: 3000, 10: 3500, 11: 2000},
    'no': {5: 1800, 6: 2000, 9: 1200, 10: 1500, 11: 1200},
    'np': {6: 1500, 7: 3000, 8: 3200, 9: 900, 10: 900},
    'om': {6: 600, 7: 1200, 8: 1400, 9: 800},
    'pa': {4: 700, 5: 1200, 6: 1400, 7: 1300, 8: 1400, 9: 1500, 10: 1600, 11: 1200},
    'pf': {1: 1500, 2: 2000, 3: 1800, 4: 1000, 12: 800},
    'pk': {7: 2500, 8: 3500, 9: 2800},
    'pl': {5: 1500, 6: 2000, 7: 1200, 9: 1500},
    'pr': {5: 400, 6: 700, 7: 900, 8: 1400, 9: 1800, 10: 1000, 11: 500},
    'pt': {1: 2000, 2: 2200, 3: 1800, 4: 1200, 11: 1200, 12: 1800},
    'ro': {4: 1200, 5: 1800, 6: 1500, 7: 1000},
    'rs': {3: 1000, 4: 1800, 5: 2200, 6: 1600},
    'rw': {3: 1500, 4: 2000, 5: 1200, 10: 1400, 11: 1800, 12: 1200},
    'sa': {1: 500, 2: 600, 3: 700, 11: 600, 12: 700},
    'sc': {1: 1500, 2: 1200, 3: 800, 11: 600, 12: 1200},
    'si': {6: 900, 7: 1000, 8: 900, 9: 1100, 10: 800, 11: 700},
    'sk': {3: 500, 4: 900, 5: 1200, 6: 1000, 7: 700},
    'sn': {7: 1200, 8: 2500, 9: 2000, 10: 800},
    'sz': {1: 1800, 2: 2000, 3: 1500, 4: 800, 11: 700, 12: 1200},
    'tj': {3: 800, 4: 2000, 5: 3500, 6: 4000, 7: 3000, 8: 1500},
    'tn': {1: 1000, 9: 900, 10: 1600, 11: 1800, 12: 1200},
    'tw': {5: 600, 6: 1200, 7: 2000, 8: 2500, 9: 2000, 10: 1000},
    'tz': {3: 1200, 4: 1800, 5: 1400, 10: 800, 11: 1200, 12: 1000},
    'ug': {3: 1500, 4: 2500, 5: 2000, 9: 1500, 10: 2200, 11: 1800},
    'uz': {3: 800, 4: 1500, 5: 1000},
    'za': {1: 2000, 2: 2000, 3: 2200, 4: 2500, 10: 1200, 11: 1500, 12: 1800},
    'zm': {1: 2800, 2: 2800, 3: 2200, 4: 1500, 11: 1200, 12: 2000},
    'zw': {1: 3000, 2: 3500, 3: 2500, 11: 1000, 12: 2000},
})  # Stage D

_SEISMIC_RESILIENCE_BY_REGION.update({
    'ae': 60,
    'al': 32,
    'aq': 60,
    'aw': 60,
    'az': 38,
    'ba': 33,
    'be': 72,
    'bo': 30,
    'bs': 55,
    'bt': 30,
    'bw': 55,
    'bz': 30,
    'cg': 22,
    'ch': 75,
    'cn': 52,
    'cr': 45,
    'cu': 38,
    'cy': 55,
    'cz': 72,
    'dj': 25,
    'dk': 80,
    'dm': 35,
    'dz': 35,
    'ec': 42,
    'ee': 72,
    'eg': 38,
    'fi': 85,
    'fo': 78,
    'ge': 30,
    'gh': 38,
    'gl': 75,
    'gt': 28,
    'hr': 52,
    'hu': 60,
    'ie': 70,
    'in': 38,
    'ir': 22,
    'is': 80,
    'jo': 40,
    'ke': 30,
    'kg': 28,
    'kh': 25,
    'kr': 65,
    'kz': 35,
    'la': 25,
    'lb': 25,
    'lk': 30,
    'ma': 30,
    'md': 30,
    'me': 40,
    'mg': 22,
    'mn': 33,
    'mv': 30,
    'mw': 25,
    'mz': 25,
    'na': 60,
    'ni': 35,
    'no': 72,
    'np': 35,
    'om': 50,
    'pa': 42,
    'pf': 50,
    'pk': 25,
    'pl': 70,
    'pr': 60,
    'pt': 45,
    'ro': 38,
    'rs': 45,
    'rw': 30,
    'sa': 52,
    'sc': 55,
    'si': 60,
    'sk': 62,
    'sn': 18,
    'sz': 22,
    'tj': 22,
    'tn': 38,
    'tw': 75,
    'tz': 28,
    'ug': 32,
    'uz': 30,
    'za': 58,
    'zm': 55,
    'zw': 55,
})  # Stage D

# Domain-correctness audit 2026-07 (E7/E8) — VERIFIED, NOT CHANGED. The audit
# flagged 'eg' (seeded 2) as stale-should-be-3, and 'ug'/'rw' (seeded 4/3) as
# stale-should-be-3/1. Live-checked travel.state.gov + WHO/CDC on 2026-07-04:
#   - Egypt: CURRENTLY Level 2 "Exercise Increased Caution" (confirmed by the
#     Cairo embassy's Apr 2026 alert) — the seeded 2 is correct AS OF TODAY.
#     The audit's "Level 3 continuously since ~2017" premise is out of date;
#     Egypt was downgraded to L2 at some point after that. No change made.
#   - Uganda: CURRENTLY Level 4 "Do Not Travel" (renewed 2026-06-04) — raised
#     from 3 to 4 on 2026-05-17 when WHO declared the DRC/Uganda Bundibugyo
#     Ebola outbreak a PHEIC. The seeded 4 is correct AS OF TODAY.
#   - Rwanda: CURRENTLY Level 3 "Reconsider Travel" (same 2026-05-17 Ebola
#     spillover-risk escalation, confirmed renewed 2026-06-04) — the seeded 3
#     is correct AS OF TODAY.
# The audit's "high confidence" facts predate this outbreak; applying them now
# would REVERT correct current data to a stale pre-outbreak baseline and
# under-warn travelers during an active PHEIC. Left unchanged; see 'ug-rift'
# below for the one genuine inconsistency this surfaced (a sub-region that
# WAS reconciled against the older, lower 'ug' baseline and never re-synced).
_ADVISORY_LEVEL_BY_REGION.update({
    'ae': 2,
    'al': 2,
    'aq': 2,
    'az': 3,
    'ba': 2,
    'be': 2,
    'bo': 2,
    'bs': 2,
    'bz': 2,
    'cg': 2,
    'cn': 2,
    'cr': 2,
    'cu': 2,
    'dj': 2,
    'dk': 2,
    'dz': 2,
    'ec': 2,
    'eg': 2,
    'fo': 2,
    'gh': 2,
    'gt': 3,
    'in': 2,
    'ir': 4,
    'jo': 2,
    'ke': 2,
    'kg': 2,
    'la': 2,
    'lb': 4,
    'lk': 2,
    'ma': 2,
    'md': 2,
    'me': 2,
    'mg': 2,
    'mw': 2,
    'mz': 2,
    'na': 2,
    'ni': 3,
    'om': 2,
    'pk': 3,
    'rs': 2,
    'rw': 3,
    'sa': 2,
    'sz': 2,
    'tj': 2,
    'tn': 2,
    'ug': 4,
    'uz': 2,
    'tz': 3,
    'za': 2,
    'zw': 2,
})  # Stage D

_REGION_SOURCE.update({
    'ae': 'seed:NCMS UAE (National Centre of Meteorology) climatology',
    'al': 'seed:lp-region-profile-2026',
    'aq': 'seed:lp-region-profile-2026',
    'aw': 'seed:lp-region-profile-2026',
    'az': 'seed:lp-region-profile-2026',
    'ba': 'seed:lp-region-profile-2026',
    'be': 'seed:lp-region-profile-2026',
    'bo': 'seed:US State Dept Travel Advisory Bolivia 2024',
    'bs': 'seed:lp-region-profile-2026',
    'bt': 'seed:NCHM Bhutan (National Centre for Hydrology and Meteorology) climatology',
    'bw': 'seed:US State Dept Travel Advisory Botswana 2024',
    'bz': 'seed:lp-region-profile-2026',
    'cg': 'seed:lp-region-profile-2026',
    'ch': 'seed:MeteoSwiss seasonal climatology',
    'cn': 'seed:US State Dept Travel Advisory China 2024',
    'cr': 'seed:IMN Costa Rica (national met service) seasonal normals',
    'cu': 'seed:US State Dept Travel Advisory Cuba 2024',
    'cy': 'seed:lp-region-profile-2026',
    'cz': 'seed:lp-region-profile-2026',
    'dj': 'seed:lp-region-profile-2026',
    'dk': 'seed:lp-region-profile-2026',
    'dm': 'seed:lp-region-profile-2026',
    'dz': 'seed:lp-region-profile-2026',
    'ec': 'seed:US State Dept Travel Advisory Ecuador 2024',
    'ee': 'seed:lp-region-profile-2026',
    'eg': 'seed:lp-region-profile-2026',
    'fi': 'seed:FMI Finnish Meteorological Institute seasonal normals',
    'fo': 'seed:lp-region-profile-2026',
    'ge': 'seed:lp-region-profile-2026',
    'gh': 'seed:lp-region-profile-2026',
    'gl': 'seed:DMI Danish Meteorological Institute Greenland climate data',
    'gt': 'seed:lp-region-profile-2026',
    'hr': 'seed:lp-region-profile-2026',
    'hu': 'seed:lp-region-profile-2026',
    'ie': 'seed:US State Dept Travel Advisory Ireland 2024',
    'in': 'seed:US State Dept Travel Advisory India 2024',
    'ir': 'seed:lp-region-profile-2026',
    'is': 'seed:IMO Iceland (Veðurstofa Íslands) seasonal normals and hazard data',
    'jo': 'seed:US State Dept Travel Advisory Jordan 2024',
    'ke': 'seed:lp-region-profile-2026',
    'kg': 'seed:lp-region-profile-2026',
    'kh': 'seed:US State Dept Travel Advisory Cambodia 2024',
    'kr': 'seed:lp-region-profile-2026',
    'kz': 'seed:lp-region-profile-2026',
    'la': 'seed:lp-region-profile-2026',
    'lb': 'seed:Lebanese Meteorological Department climatology',
    'lk': 'seed:Department of Meteorology Sri Lanka seasonal normals',
    'ma': 'seed:lp-region-profile-2026',
    'md': 'seed:lp-region-profile-2026',
    'me': 'seed:IHMS Montenegro meteorological normals',
    'mg': 'seed:lp-region-profile-2026',
    'mn': 'seed:lp-region-profile-2026',
    'mv': 'seed:lp-region-profile-2026',
    'mw': 'seed:lp-region-profile-2026',
    'mz': 'seed:lp-region-profile-2026',
    'na': 'seed:lp-region-profile-2026',
    'ni': 'seed:lp-region-profile-2026',
    'no': 'seed:lp-region-profile-2026',
    'np': 'seed:US State Dept Travel Advisory Nepal 2024',
    'om': 'seed:lp-region-profile-2026',
    'pa': 'seed:lp-region-profile-2026',
    'pf': 'seed:lp-region-profile-2026',
    'pk': 'seed:PMD Pakistan Meteorological Department monsoon climatology',
    'pl': 'seed:lp-region-profile-2026',
    'pr': 'seed:lp-region-profile-2026',
    'pt': 'seed:lp-region-profile-2026',
    'ro': 'seed:lp-region-profile-2026',
    'rs': 'seed:lp-region-profile-2026',
    'rw': 'seed:lp-region-profile-2026',
    'sa': 'seed:lp-region-profile-2026',
    'sc': 'seed:lp-region-profile-2026',
    'si': 'seed:US State Dept Travel Advisory Slovenia 2024',
    'sk': 'seed:lp-region-profile-2026',
    'sn': 'seed:lp-region-profile-2026',
    'sz': 'seed:lp-region-profile-2026',
    'tj': 'seed:lp-region-profile-2026',
    'tn': 'seed:lp-region-profile-2026',
    'tw': 'seed:lp-region-profile-2026',
    'tz': 'seed:US State Dept Travel Advisory Tanzania 2024',
    'ug': 'seed:lp-region-profile-2026',
    'uz': 'seed:Uzhydromet (Uzbekistan national hydro-met service) normals',
    'za': 'seed:lp-region-profile-2026',
    'zm': 'seed:lp-region-profile-2026',
    'zw': 'seed:lp-region-profile-2026',
})  # Stage D

_CITY_TO_REGION.update({
    'abu dhabi': 'ae',
    'abu simbel': 'eg',
    'agra': 'in',
    'agrigento': 'it-central',
    'akureyri': 'is',
    'alberobello': 'it-central',
    'almaty': 'kz',
    'amalfi': 'it-central',
    'amman': 'jo',
    'amritsar': 'in',
    'andalsnes': 'no',
    'aomori': 'jp',
    'aqaba': 'jo',
    'arles': 'fr-north',
    'arusha': 'tz',
    'aurangabad': 'in',
    'ayutthaya': 'th-bangkok',
    'baalbek': 'lb',
    'bacalar': 'mx-caribbean',
    'baddeck': 'ca-east',
    'baku': 'az',
    'balloch': 'uk-south',
    'banaue': 'ph-luzon',
    'bar harbor': 'us',
    'barreirinhas': 'br-southeast',
    'bath': 'uk-south',
    'batna': 'dz',
    'bayeux': 'fr-north',
    'beijing': 'cn',
    'belfast': 'uk-south',
    'belgrade': 'rs',
    'belize city': 'bz',
    'belmopan': 'bz',
    'bergen': 'no',
    'bergville': 'za',
    'billund': 'dk',
    'blantyre': 'mw',
    'bled': 'si',
    'bonito': 'br-southeast',
    'bora bora': 'pf',
    'bournemouth': 'uk-south',
    'brasilia': 'br-southeast',
    'brașov': 'ro',
    'brecon': 'uk-south',
    'brno': 'cz',
    'bruges': 'be',
    'brussels': 'be',
    'bucharest': 'ro',
    'budapest': 'hu',
    'budva': 'me',
    'bushmills': 'uk-south',
    'caernarfon': 'uk-south',
    'cairo': 'eg',
    'cancún': 'mx-caribbean',  # B1 audit: accent-collision under-warning — mx-central
    'cangas de onis': 'es-east',
    'cape coast': 'gh',
    'cape town': 'za',
    'carcassonne': 'fr-north',
    'cardiff': 'uk-south',
    'carrick': 'ie',
    'chamonix': 'fr-north',
    'champasak': 'la',
    'chandrapur': 'in',
    'chicago': 'us',
    'chișinău': 'md',
    'churchill': 'ca-east',
    'clifden': 'ie',
    'cochrane': 'cl-central',
    'como': 'it-north',
    'con son': 'vn-south',
    'coromandel': 'nz-north',
    'corrientes': 'ar-pampas',
    'cortez': 'us',
    'creel': 'mx-central',
    'crescent city': 'us',
    'cuiaba': 'br-southeast',
    'dakar': 'sn',
    'dalanzadgad': 'mn',
    'dambulla': 'lk',
    'dana': 'jo',
    'darchen': 'cn',
    'delhi': 'in',
    'dessau': 'de-west',
    'dijon': 'fr-north',
    'djibouti city': 'dj',
    'drogheda': 'ie',
    'drumheller': 'ca-east',
    'dublin': 'ie',
    'dubrovnik': 'hr',
    'dundee': 'uk-south',
    'dunhuang': 'cn',
    'eastbourne': 'uk-south',
    'edinburgh': 'uk-south',
    'egilsstaðir': 'is',
    'el calafate': 'ar-pampas',
    'el chaltén': 'ar-pampas',
    'el jem': 'tn',
    'el nido': 'ph-luzon',
    'embilipitiya': 'lk',
    'erdenet': 'mn',
    'esbjerg': 'dk',
    'fernando de noronha': 'br-southeast',
    'fethiye': 'tr',
    'fez': 'ma',
    'figueres': 'es-east',
    'flores': 'gt',
    'flåm': 'no',
    'fort macleod': 'ca-east',
    'fort william': 'uk-south',
    'freiburg': 'de-west',
    'furnace creek': 'us',
    # Füssen is in southern Bavaria (Ostallgäu, on the Austrian border, near
    # Neuschwanstein) -- de-bavaria, NOT de-west. This unaccented spelling used
    # to disagree with the accented 'füssen' -> 'de-bavaria' entry added later
    # in this file (Stage E); a caller normalizing/stripping accents before the
    # bare-key lookup (or simply typing the ASCII spelling) silently got the
    # wrong region. Task-1 sweep (2026-07-06), same bug class as #236/#240.
    'fussen': 'de-bavaria',
    'galway': 'ie',
    'gdansk': 'pl',
    'geneva': 'ch',
    'george town': 'bs',
    'gilgit': 'pk',
    'giverny': 'fr-north',
    'giza': 'eg',
    'glasgow': 'uk-south',
    'glencoe': 'uk-south',
    'granada': 'es-east',
    'grand canyon village': 'us',
    'guilin': 'cn',
    'göreme': 'tr',
    'halfmoon bay': 'nz-north',
    'halong city': 'vn-south',
    'hampi': 'in',
    'hanga roa': 'cl-central',
    'harbin': 'cn',
    'havana': 'cu',
    'henties bay': 'na',
    'heraklion': 'gr-attica',
    'hexham': 'uk-south',
    'himeji': 'jp',
    # Höfn is southeast Iceland (Hornafjörður, on the Ring Road near Vatnajökull)
    # -- is-south, NOT generic 'is'. This unaccented spelling used to disagree
    # with the accented 'höfn' -> 'is-south' entry added later (Stage E); an
    # accent-stripped or plain-ASCII query silently got the less-precise/wrong
    # sub-region. Task-1 sweep (2026-07-06), same bug class as #236/#240.
    'hofn': 'is-south',
    'homestead': 'us-gulf',  # B1 audit: SE-Florida hurricane coast (was inland 'us')
    'hong kong': 'cn',
    'hualien': 'tw',
    'huangshan': 'cn',
    'huaraz': 'pe-coast',
    'humlebæk': 'dk',
    'ihosy': 'mg',
    'ilulissat': 'gl',
    'inhambane': 'mz',
    'interlaken': 'ch',
    'inverness': 'uk-south',
    'isfahan': 'ir',
    'jackson': 'us',
    'jaipur': 'in',
    'jaisalmer': 'in',
    'jasper': 'ca-east',
    'johannesburg': 'za',
    'joshimath': 'in',
    'kabale': 'ug',
    'kaikoura': 'nz-north',
    'kalambaka': 'gr-attica',
    'kandy': 'lk',
    'karoi': 'zw',
    'kars': 'tr',
    'kathmandu': 'np',
    'kawaguchiko': 'jp',
    'kayenta': 'us',
    'khatgal': 'mn',
    'khiva': 'uz',
    'khorog': 'tj',
    'kigali': 'rw',
    'kigoma': 'tz',
    'killarney': 'ie',
    'kilwa masoko': 'tz',
    'kirkwall': 'uk-south',
    'kochi': 'in',
    'kotor': 'me',
    'koya': 'jp',
    'krakow': 'pl',
    'kraków': 'pl',
    'kutná hora': 'cz',
    'la digue': 'sc',
    'la macarena': 'co-andes',
    'labuan bajo': 'id-bali',
    'lake louise': 'ca-east',
    'lalomanu': 'ws-samoa',
    'lanquín': 'gt',
    'las vegas': 'us',
    'leh': 'in',
    'leshan': 'cn',
    'lhasa': 'cn',
    'lijiang': 'cn',
    'lisbon': 'pt',
    'liverpool': 'uk-south',
    'livingstone': 'zm',
    'longyearbyen': 'no',
    'luang namtha': 'la',
    'luquillo': 'pr',
    'lusaka': 'zm',
    'luxor': 'eg',
    'madrid': 'es',
    'magelang': 'id-bali',
    'malé': 'mv',
    'mangochi': 'mw',
    'manzini': 'sz',
    'marrakech': 'ma',
    'masvingo': 'zw',
    'matera': 'it-central',
    'matmata': 'tn',
    'maun': 'bw',
    'mcmurdo station': 'aq',
    'mecca': 'sa',
    'mekele': 'et',
    'mfuwe': 'zm',
    'mont-saint-michel': 'fr-north',
    'monteverde': 'cr',
    'moorea': 'pf',
    'morondava': 'mg',
    'moshi': 'tz',
    'mostar': 'ba',
    'motueka': 'nz-north',
    'mount cook village': 'nz-north',
    'musanze': 'rw',
    'málaga': 'es-east',
    'nagasaki': 'jp',
    'nairobi': 'ke',
    'namche bazaar': 'np',
    'nampula': 'mz',
    'naoshima': 'jp',
    'naples': 'it-central',
    'nara': 'jp',
    'narok': 'ke',
    'naryn': 'kg',
    'nazca': 'pe-coast',
    'neiva': 'co-andes',
    'niagara falls': 'ca-east',
    'nizwa': 'om',
    'novi pazar': 'rs',
    'nuwara eliya': 'lk',
    # Nîmes is in Gard/Occitanie, southern France (Mediterranean climate,
    # same cluster as Nice/Marseille/Montpellier) -- fr-south, NOT fr-north.
    # This accented spelling used to disagree with the unaccented 'nimes' ->
    # 'fr-south' entry set earlier in this file; whichever spelling a caller
    # passed decided which (sometimes wrong) region it got. Task-1 sweep
    # (2026-07-06), same bug class as #236/#240.
    'nîmes': 'fr-south',
    'oranjestad': 'aw',
    'orlando': 'us-gulf',  # B1 audit: FL hurricane-exposed (was inland 'us')
    # Oświęcim (Auschwitz) is ~50km from Kraków, Lesser Poland -- pl-krakow,
    # NOT generic 'pl'. This unaccented spelling used to disagree with the
    # accented 'oświęcim' -> 'pl-krakow' entry added later (Stage E). Task-1
    # sweep (2026-07-06), same bug class as #236/#240.
    'oswiecim': 'pl-krakow',
    'ouarzazate': 'ma',
    'ouesso': 'cg',
    'outjo': 'na',
    'paju': 'kr',
    'pakse': 'la',
    'palenque': 'mx-central',
    'panajachel': 'gt',
    'pangkalan bun': 'id',
    'paphos': 'cy',
    'paro': 'bt',
    'penrhyndeudraeth': 'uk-south',
    'pinhão': 'pt',
    'plitvice lakes': 'hr',
    'pokhara': 'np',
    'poprad': 'sk',
    'porthmadog': 'uk-south',
    'porto': 'pt',
    'postojna': 'si',
    'potsdam': 'de-west',
    'prague': 'cz',
    'preah vihear city': 'kh',
    'provincetown': 'us',
    'puerto ayora': 'ec',
    'puerto iguazú': 'br-southeast',
    # Puerto Jiménez is the hub town of the Osa Peninsula, south Pacific Costa
    # Rica (Corcovado NP / Golfo Dulce) -- cr-osa, NOT generic 'cr'. This
    # unaccented spelling used to disagree with the accented 'puerto jiménez'
    # -> 'cr-osa' entry added later (Stage E). Task-1 sweep (2026-07-06), same
    # bug class as #236/#240.
    'puerto jimenez': 'cr-osa',
    'puerto madryn': 'ar-pampas',
    'puerto natales': 'cl-central',
    'purmamarca': 'ar-pampas',
    'queen charlotte': 'ca-east',
    'quito': 'ec',
    'ravenna': 'it-central',
    'rivas': 'ni',
    'roseau': 'dm',
    'rotorua': 'nz-north',
    'rovaniemi': 'fi',
    'rurrenabaque': 'bo',
    'russell': 'nz-north',
    'sagres': 'pt',
    'saint john': 'ca-east',
    'saint-pierre': 'fr-north',
    'salisbury': 'uk-south',
    'sam neua': 'la',
    'samarkand': 'uz',
    'san ignacio': 'bz',
    'san juan': 'pr',
    'san pedro': 'bz',
    'san pedro de atacama': 'cl-central',
    'santa catalina': 'pa',
    'santa cruz de tenerife': 'es',
    'santa marta': 'co-andes',
    'santiago de compostela': 'es',
    'santorini': 'gr-attica',
    'sarajevo': 'ba',
    'saranda': 'al',
    'segovia': 'es-east',
    'selcuk': 'tr',
    'selfoss': 'is',
    'seoul': 'kr',
    'sesriem': 'na',
    'seville': 'es-east',
    'shanghai': 'cn',
    'shingu': 'jp',
    'shiraz': 'ir',
    'siem reap': 'kh',
    'siena': 'it-central',
    'sintra': 'pt',
    'skukuza': 'za',
    'split': 'hr',
    'squamish': 'ca-east',
    'st davids': 'uk-south',
    'stavanger': 'no',
    'stirling': 'uk-south',
    'stykkisholmur': 'is',
    'sur': 'om',
    'svolvaer': 'no',
    'swakopmund': 'na',
    'talkeetna': 'us',
    'tallinn': 'ee',
    'taos': 'us',
    'taveuni': 'fj-southpacific',
    'tbilisi': 'ge',
    'te anau': 'nz-north',
    'tefé': 'br-southeast',
    'tours': 'fr-north',
    'tryphena': 'nz-north',
    'tulcea': 'ro',
    'tunis': 'tn',
    'ulaanbaatar': 'mn',
    'uyuni': 'bo',
    'valladolid': 'mx-caribbean',
    'varanasi': 'in',
    'versailles': 'fr-north',
    'vestmanna': 'fo',
    'vinales': 'cu',
    'vis': 'hr',
    # Vík í Mýrdal is the quintessential South Iceland Ring-Road town (Reynisfjara
    # black-sand beach, Mýrdalsjökull/Katla) -- is-south, NOT generic 'is'. This
    # accented spelling used to disagree with the unaccented 'vik' -> 'is-south'
    # entry set later (Stage E); whichever spelling a caller passed decided
    # which (sometimes wrong) sub-region it got. Task-1 sweep (2026-07-06),
    # same bug class as #236/#240.
    'vík': 'is-south',
    'wadi musa': 'jo',
    'waisai': 'id',
    'waitomo': 'nz-north',
    'wanaka': 'nz-north',
    'washington': 'us',
    'windermere': 'uk-south',
    "xi'an": 'cn',
    'yazd': 'ir',
    'york': 'uk-south',
    'yosemite valley': 'us',
    'ypres': 'be',
    'zagora': 'ma',
    'zagreb': 'hr',
    'zermatt': 'ch',
    'zernez': 'ch',
    'zhangjiajie': 'cn',
    'çanakkale': 'tr',
    'ísafjörður': 'is',
})  # Stage D

_ALL_REGIONS = frozenset(
    set(_CYCLONE_BY_REGION_MONTH) | set(_FLOOD_BY_REGION_MONTH)
    | set(_DELAY_BY_REGION_MODE) | set(_SEISMIC_RESILIENCE_BY_REGION)
    | set(_CIVIL_UNREST_BY_REGION)
)

# ===========================================================================
# Stage E — 36 LP500 remaining region seeds (LP500 coverage, 2026-06-17).
# Fills the regions that were returning UNKNOWN conservative flag.
# Source: task brief + known seasonal/advisory data. Provenance-tagged.
# ===========================================================================

_CYCLONE_BY_REGION_MONTH.update({
    'fj-outer': {1: 3500, 2: 3800, 3: 3000, 11: 1500, 12: 2500},
    'mg-madagascar': {1: 3500, 2: 4000, 3: 3000},
    'mw-malawi': {},  # #67: landlocked sub-region — no direct cyclone strike; flood (already FLAG) carries the wet-season hazard
    'mz-mozambique': {1: 2000, 2: 3000, 3: 2000},
    'zw-zimbabwe': {},  # #67: landlocked sub-region — no direct cyclone strike (Victoria Falls); flood raised below to carry wet-season hazard
})  # Stage E

_FLOOD_BY_REGION_MONTH.update({
    'ao-angola': {2: 1000, 3: 1500, 4: 1000},
    'au-sydney': {10: 800, 11: 1000, 12: 1200},
    'au-tassie': {7: 800, 8: 1000},
    'br-pantanal': {1: 2000, 2: 2500, 3: 2000, 12: 1500},
    'ca-rockies': {5: 800, 6: 1200},
    'cg-congo': {3: 1000, 4: 1500, 10: 1200, 11: 1800},
    'cr-osa': {9: 2000, 10: 3000, 11: 2000},
    'de-bavaria': {6: 800, 7: 800},
    'es-cantabria': {1: 1200, 2: 1200, 10: 1000, 11: 1500, 12: 1200},
    'et-north': {7: 2000, 8: 3000, 9: 2000},
    'gh-ghana': {6: 1500, 7: 2000, 8: 1500},
    'is-north': {5: 800, 6: 1000, 9: 800},
    'is-south': {4: 800, 5: 1000, 10: 800},
    'ke-coast': {4: 1200, 5: 800, 11: 800},
    'mg-madagascar': {1: 2000, 2: 2500, 3: 2000},
    'mw-malawi': {1: 1500, 2: 2000, 3: 1500},
    'mz-mozambique': {1: 2000, 2: 2500, 3: 2000},
    'na-namibia': {1: 800, 2: 1200, 3: 800},
    'no-lofoten': {10: 800, 11: 1200, 12: 800},
    'pg-highlands': {3: 800, 4: 1200},
    'pl-krakow': {3: 800, 4: 1200, 5: 800},
    'pt-alentejo': {1: 1200, 2: 1500, 3: 1000, 11: 800, 12: 1200},
    'sg-city': {1: 1000, 11: 1000, 12: 1500},
    'sn-senegal': {8: 1500, 9: 2000, 10: 1500},
    'tz-mainland': {3: 800, 4: 1200, 11: 800},
    'ug-rift': {3: 1500, 4: 2000, 10: 1500, 11: 1800},
    'us-southwest': {7: 800, 8: 1200},
    'zm-zambia': {1: 800, 2: 1500, 3: 1200},
    'zw-zimbabwe': {1: 3000, 2: 3500, 3: 2500},  # #67: raised to AVOID/FLAG (mirrors country 'zw') so the cyclone re-channel PRESERVES the wet-season flood hazard — never drops it (Idai-class inland flooding)
})  # Stage E

_SEISMIC_RESILIENCE_BY_REGION.update({
    'ao-angola': 60,
    'at-vienna': 65,
    'au-kangaroo': 70,
    'au-sydney': 75,
    'au-tassie': 70,
    'au-uluru': 80,
    'au-vic': 70,
    'br-pantanal': 60,
    'ca-rockies': 70,
    'cg-congo': 55,
    'cr-osa': 50,
    'de-bavaria': 65,
    'es-cantabria': 65,
    'et-north': 25,
    'fj-outer': 55,
    'gh-ghana': 60,
    'is-north': 40,
    'is-south': 40,
    'ke-coast': 50,
    'mg-madagascar': 50,
    'mw-malawi': 55,
    'mz-mozambique': 55,
    'na-namibia': 65,
    'no-lofoten': 60,
    'nz-south': 45,
    'pg-highlands': 50,
    'pl-krakow': 70,
    'pt-alentejo': 60,
    'sc-seychelles': 65,
    'sg-city': 70,
    'sn-senegal': 65,
    'tz-mainland': 50,
    'ug-rift': 60,
    'us-southwest': 65,
    'zm-zambia': 60,
    'zw-zimbabwe': 55,
})  # Stage E

_ADVISORY_LEVEL_BY_REGION.update({
    'ao-angola': 3,
    'at-vienna': 1,
    'au-kangaroo': 1,
    'au-sydney': 1,
    'au-tassie': 1,
    'au-uluru': 1,
    'au-vic': 1,
    'br-pantanal': 1,
    'ca-rockies': 1,
    'cg-congo': 3,
    'cr-osa': 1,
    'de-bavaria': 1,
    'es-cantabria': 1,
    'et-north': 3,
    'fj-outer': 1,
    'gh-ghana': 2,
    'is-north': 1,
    'is-south': 1,
    'ke-coast': 2,
    'mg-madagascar': 2,
    'mw-malawi': 2,
    'mz-mozambique': 3,
    'na-namibia': 1,
    'no-lofoten': 1,
    'nz-south': 1,
    'pg-highlands': 3,
    'pl-krakow': 1,
    'pt-alentejo': 1,
    'sc-seychelles': 1,
    'sg-city': 1,
    'sn-senegal': 2,
    'tz-mainland': 2,
    'ug-rift': 4,  # E8 2026-07: reconciled against 'ug' (now 4, see note above) — was
                   # 3, stale against an older/lower 'ug' baseline; Kampala/Entebbe
                   # (this sub-region's only cities) are under the SAME nationwide
                   # Level-4 advisory, not a carved-out lower zone.
    'us-southwest': 1,
    'zm-zambia': 2,
    'zw-zimbabwe': 3,
})  # Stage E

_REGION_SOURCE.update({
    'ao-angola': 'seed:society-region-profile-2026',
    'at-vienna': 'seed:society-region-profile-2026',
    'au-kangaroo': 'seed:society-region-profile-2026',
    'au-sydney': 'seed:bom-australia-se-2026',
    'au-tassie': 'seed:society-region-profile-2026',
    'au-uluru': 'seed:bom-australia-central-2026',
    'au-vic': 'seed:society-region-profile-2026',
    'br-pantanal': 'seed:society-region-profile-2026',
    'ca-rockies': 'seed:ec.gc.ca-canada-2026',
    'cg-congo': 'seed:society-region-profile-2026',
    'cr-osa': 'seed:imn.ac.cr-costarica-2026',
    'de-bavaria': 'seed:dwd.de-germany-2026',
    'es-cantabria': 'seed:society-region-profile-2026',
    'et-north': 'seed:society-region-profile-2026',
    'fj-outer': 'seed:fms-cyclone-climatology-2026',
    'gh-ghana': 'seed:society-region-profile-2026',
    'is-north': 'seed:vedur.is-iceland-2026',
    'is-south': 'seed:vedur.is-iceland-2026',
    'ke-coast': 'seed:meteo.go.ke-kenya-2026',
    'mg-madagascar': 'seed:society-region-profile-2026',
    'mw-malawi': 'seed:society-region-profile-2026',
    'mz-mozambique': 'seed:inam.gov.mz-mozambique-2026',
    'na-namibia': 'seed:society-region-profile-2026',
    'no-lofoten': 'seed:met.no-norway-2026',
    'nz-south': 'seed:metservice.com-nz-2026',
    'pg-highlands': 'seed:society-region-profile-2026',
    'pl-krakow': 'seed:imgw-poland-2026',
    'pt-alentejo': 'seed:society-region-profile-2026',
    'sc-seychelles': 'seed:society-region-profile-2026',
    'sg-city': 'seed:society-region-profile-2026',
    'sn-senegal': 'seed:anacim.sn-senegal-2026',
    'tz-mainland': 'seed:tma.go.tz-tanzania-2026',
    'ug-rift': 'seed:society-region-profile-2026',
    'us-southwest': 'seed:nws-usa-southwest-2026',
    'zm-zambia': 'seed:zmd.gov.zm-zambia-2026',
    'zw-zimbabwe': 'seed:society-region-profile-2026',
})  # Stage E

_CITY_TO_REGION.update({
    'accra': 'gh-ghana',
    'alice springs': 'au-uluru',
    'banff': 'ca-rockies',
    'brazzaville': 'cg-congo',
    'corumba': 'br-pantanal',
    'corumbá': 'br-pantanal',
    'dar es salaam': 'tz-mainland',
    'entebbe': 'ug-rift',
    'evora': 'pt-alentejo',
    'füssen': 'de-bavaria',
    'geelong': 'au-vic',
    'gondar': 'et-north',
    'hobart': 'au-tassie',
    'husavik': 'is-north',
    'höfn': 'is-south',
    'húsavík': 'is-north',
    'kampala': 'ug-rift',
    'kingscote': 'au-kangaroo',
    'lalibela': 'et-north',
    'launceston': 'au-tassie',
    'luanda': 'ao-angola',
    'melbourne': 'au-vic',
    'mombasa': 'ke-coast',
    'munich': 'de-bavaria',
    'oviedo': 'es-cantabria',
    'oświęcim': 'pl-krakow',
    'penneshaw': 'au-kangaroo',
    'puerto jiménez': 'cr-osa',
    'santander': 'es-cantabria',
    'sedona': 'us-southwest',
    'singapore': 'sg-city',
    # Stykkishólmur is on the NORTH coast of the Snæfellsnes peninsula, WEST
    # Iceland (Breiðafjörður) -- not south. There is no 'is-west' sub-region;
    # this entry originally (wrongly) said 'is-south', disagreeing with the
    # unaccented 'stykkisholmur' -> 'is' entry set earlier in this file. Fixed
    # to the honest generic 'is' rather than a confidently-wrong south-Iceland
    # read. Task-1 sweep (2026-07-06), same bug class as #236/#240.
    'stykkishólmur': 'is',
    'svolvær': 'no-lofoten',
    'sydney': 'au-sydney',
    'victoria falls': 'zw-zimbabwe',
    'vienna': 'at-vienna',
    'vik': 'is-south',
    'yulara': 'au-uluru',
    # Åndalsnes is in Rauma, Møre og Romsdal -- western/central Norway (the
    # Romsdalsfjord/Trollstigen gateway), NOT the Lofoten archipelago (which is
    # far north in Nordland, ~750km away). This entry originally (wrongly)
    # said 'no-lofoten', disagreeing with the unaccented 'andalsnes' -> 'no'
    # entry set earlier in this file. Task-1 sweep (2026-07-06), same bug
    # class as #236/#240.
    'åndalsnes': 'no',
    'évora': 'pt-alentejo',
})  # Stage E

# ===========================================================================
# Region-gap fill — the 26 catalog cities (full A-Z scan, 2026-06-18) that
# resolved to UNKNOWN → conservative flag. Each is mapped to a region whose
# hazard profile MATCHES the place: SW Australia is low-cyclone (unlike the
# cyclone-prone au-northwest), KL is monsoon-flood but outside the typhoon belt,
# the Afar/Danakil is an active rift, Sabah's east coast carries the ESSZONE
# advisory. Existing regions are reused where geographically sound (Tasmania,
# Kangaroo Is., Victoria, NSW, Turkey, Vienna); 8 new regions are added where
# none existed. No cyclone entries — none of these sit in a cyclone belt (→ 0,
# never a silent "safe"). Below-threshold flood stays advisory-only by design.
# ===========================================================================
_FLOOD_BY_REGION_MONTH.update({
    'au-southwest': {6: 600, 7: 800, 8: 600},                 # SW WA winter rain (minor)
    'my-peninsular': {11: 1200, 12: 1500, 1: 1000, 3: 800},   # KL monsoon flash floods
    'my-sabah': {11: 1000, 12: 1200, 1: 1000},                # NE monsoon
    'bg': {3: 600, 4: 800, 5: 600},                           # spring snowmelt (minor)
    'se': {4: 600, 5: 800},                                   # spring melt (minor)
    'lu': {1: 600, 7: 800},                                   # winter/summer (minor)
    'mt': {10: 600, 11: 800},                                 # autumn Med rains (minor)
    # et-afar: Danakil desert — no seasonal flood.
})  # region-gap fill 2026-06-18

_DELAY_BY_REGION_MODE.update({
    'au-southwest': {"flight": 30, "rail": 0, "bus": 30},
    'my-peninsular': {"flight": 35, "rail": 20, "bus": 30},
    'my-sabah': {"flight": 40, "rail": 0, "bus": 35},
    'bg': {"flight": 30, "rail": 25, "bus": 30},
    'se': {"flight": 30, "rail": 15, "bus": 25},
    'et-afar': {"flight": 50, "rail": 0, "bus": 60},          # remote/harsh (bus≥45 → flags)
    'lu': {"flight": 25, "rail": 15, "bus": 20},
    'mt': {"flight": 30, "rail": 0, "bus": 30},
})  # region-gap fill 2026-06-18

_SEISMIC_RESILIENCE_BY_REGION.update({
    'au-southwest': 75,   # stable craton (minor Meckering-type only)
    'my-peninsular': 70,  # stable peninsula, outside the main belt
    'my-sabah': 55,       # more active (2015 Ranau quake)
    'bg': 60,
    'se': 80,             # Baltic shield, very stable
    'et-afar': 20,        # active rift + volcanism (Danakil) → low resilience
    'lu': 75,
    'mt': 60,
})  # region-gap fill 2026-06-18

_ADVISORY_LEVEL_BY_REGION.update({
    'au-southwest': 1,
    'my-peninsular': 1,
    'my-sabah': 3,        # ESSZONE — standing kidnapping advisory off Semporna
    'bg': 1,
    'se': 1,
    'et-afar': 3,         # Afar/Danakil — remote, harsh, conflict-adjacent
    'lu': 1,
    'mt': 1,
})  # region-gap fill 2026-06-18

_REGION_SOURCE.update({
    'au-southwest': 'seed:bom-australia-sw-2026',
    'my-peninsular': 'seed:met.gov.my-peninsular-2026',
    'my-sabah': 'seed:met.gov.my-sabah-2026',
    'bg': 'seed:weather.bg-bulgaria-2026',
    'se': 'seed:smhi.se-sweden-2026',
    'et-afar': 'seed:society-region-profile-2026',
    'lu': 'seed:meteolux-luxembourg-2026',
    'mt': 'seed:maltairport-met-2026',
})  # region-gap fill 2026-06-18

_CITY_TO_REGION.update({
    # Australia — SW WA loop (low-cyclone Mediterranean SW) → new au-southwest;
    # the rest reuse existing regions that match their location.
    'perth': 'au-southwest', 'margaret-river': 'au-southwest',
    'busselton': 'au-southwest', 'albany': 'au-southwest',
    'pemberton': 'au-southwest', 'northam': 'au-southwest',
    'esperance': 'au-southwest', 'ravensthorpe': 'au-southwest',
    'kalgoorlie': 'au-southwest',
    'coles bay': 'au-tassie',          # Freycinet, Tasmania
    'kangaroo island': 'au-kangaroo',  # South Australia
    'ararat': 'au-vic',                # Victoria
    'lord howe island': 'au-sydney',   # NSW (remote subtropical island)
    # Malaysia (no region existed before).
    'kuala lumpur': 'my-peninsular',
    'semporna': 'my-sabah',
    # Turkey (existing 'tr' — seismically active, seismic 35 → flags).
    'antalya': 'tr', 'goreme': 'tr',
    # Austria (existing at-vienna).
    'innsbruck': 'at-vienna', 'krems': 'at-vienna',
    # Sweden, Bulgaria, Ethiopia-Afar, Luxembourg, Malta (new regions).
    'stockholm': 'se', 'jukkasjärvi': 'se',
    'razgrad': 'bg', 'rila': 'bg',
    'semera': 'et-afar',
    'luxembourg city': 'lu',
    'mellieħa': 'mt',
})  # region-gap fill 2026-06-18

_CITY_TO_REGION.update({
    # Stage F batch 1 — Vietnam (GeoNames cities x OSM hotels, 2026-06-18).
    # vn-north/central/south assigned by latitude; regions already seeded.
    'an nhon': 'vn-central',
    'ba don': 'vn-central',
    'ba ria': 'vn-south',
    'ba vi': 'vn-north',
    'bac giang': 'vn-north',
    'bac lieu': 'vn-south',
    'bac ninh': 'vn-north',
    'bac quang': 'vn-north',
    'bao loc': 'vn-south',
    'ben cat': 'vn-south',
    'ben tre': 'vn-south',
    'bien hoa': 'vn-south',
    'binh thuy': 'vn-south',
    'buon ma thuot': 'vn-south',
    'ca mau': 'vn-south',
    'cai lay': 'vn-south',
    'cam pha': 'vn-north',
    'cam pha mines': 'vn-north',
    'cam ranh': 'vn-south',
    'can tho': 'vn-south',
    'cao lanh': 'vn-south',
    'chi linh': 'vn-north',
    'da lat': 'vn-south',
    'di an': 'vn-south',
    'dong ha': 'vn-central',
    'dong hoi': 'vn-central',
    'dong trieu': 'vn-north',
    'dong xoai': 'vn-south',
    'duc pho': 'vn-central',
    'duc thinh': 'vn-south',
    'duc trong': 'vn-south',
    'ha tien': 'vn-south',
    'ha tinh': 'vn-north',
    'hai chau': 'vn-central',
    'hai duong': 'vn-north',
    'haiphong': 'vn-north',
    'hoa binh': 'vn-north',
    'hoa thanh': 'vn-south',
    'hoang mai': 'vn-north',
    'hong ngu': 'vn-south',
    'hue': 'vn-central',
    'kien an': 'vn-north',
    'kon tum': 'vn-central',
    'ky anh': 'vn-north',
    'la gi': 'vn-south',
    'lang son': 'vn-north',
    'lao cai': 'vn-north',
    'long xuyen': 'vn-south',
    'mong cai': 'vn-north',
    'my hao': 'vn-north',
    'my tho': 'vn-south',
    'nam dinh': 'vn-north',
    'nha trang': 'vn-south',
    'o mon': 'vn-south',
    'phan rang-thap cham': 'vn-south',
    'phan thiet': 'vn-south',
    'pho yen': 'vn-north',
    'phu ly': 'vn-north',
    'phu my': 'vn-south',
    'phu quoc': 'vn-south',
    'phuc yen': 'vn-north',
    'pleiku': 'vn-central',
    'quang ngai': 'vn-central',
    'qui nhon': 'vn-central',
    'rach gia': 'vn-south',
    'sa dec': 'vn-south',
    'sam son': 'vn-north',
    'soc trang': 'vn-south',
    'son la': 'vn-north',
    'son tay': 'vn-north',
    'tam ky': 'vn-central',
    'tan an': 'vn-south',
    'tay ninh': 'vn-south',
    'thai nguyen': 'vn-north',
    'thanh hoa': 'vn-north',
    'thot not': 'vn-south',
    'thu dau mot': 'vn-south',
    'thu duc': 'vn-south',
    'thuan an': 'vn-south',
    'tra vinh': 'vn-south',
    'trang bang': 'vn-south',
    'tuy hoa': 'vn-south',
    'tuyen quang': 'vn-north',
    'viet tri': 'vn-north',
    'viet yen': 'vn-north',
    'vinh': 'vn-north',
    'vinh long': 'vn-south',
    'vinh yen': 'vn-north',
    'vung tau': 'vn-south',
    'xuan loc': 'vn-south',
    'yen bai': 'vn-north',
    'yen vinh': 'vn-north',
})  # Stage F: Vietnam

_CYCLONE_BY_REGION_MONTH.update({
    'nz': {1: 900, 2: 1300, 3: 1000, 4: 500, 12: 600},
})  # Stage F wave1
_FLOOD_BY_REGION_MONTH.update({
    'gr': {9: 1600, 10: 2000, 11: 1800, 12: 1500},
    'at': {4: 800, 5: 1400, 6: 1800, 7: 1700, 8: 1300},
    'nz': {1: 1200, 2: 1800, 3: 1600, 4: 1500, 5: 1500, 6: 1700, 7: 1700, 8: 1600, 9: 1400, 10: 1300, 11: 1200, 12: 1300},
})  # Stage F wave1
_DELAY_BY_REGION_MODE.update({
    'gr': {'flight': 45, 'rail': 35, 'bus': 20},
    'at': {'flight': 25, 'rail': 15, 'bus': 20},
    'nz': {'flight': 30, 'rail': 0, 'bus': 20},
})  # Stage F wave1
_SEISMIC_RESILIENCE_BY_REGION.update({
    'gr': 32,
    'at': 62,
    'nz': 38,
})  # Stage F wave1
_ADVISORY_LEVEL_BY_REGION.update({
    'gr': 1,
    'at': 1,
    'nz': 1,
})  # Stage F wave1
_REGION_SOURCE.update({
    'gr': 'seed:society-region-profile-2026',
    'at': 'seed:society-region-profile-2026',
    'nz': 'seed:society-region-profile-2026',
})  # Stage F wave1
_CITY_TO_REGION.update({
    'agadir': 'ma',
    'ait melloul': 'ma',
    'al fqih ben calah': 'ma',
    'al hoceima': 'ma',
    'antwerp': 'be',
    'as salt': 'jo',
    'beni mellal': 'ma',
    'berkane': 'ma',
    'berrechid': 'ma',
    'bharatpur': 'np',
    'biratnagar': 'np',
    'birendranagar': 'np',
    'birganj': 'np',
    'bizerte': 'tn',
    'bouskoura': 'ma',
    'braga': 'pt',
    'butwal': 'np',
    'casablanca': 'ma',
    'charleroi': 'be',
    'coimbra': 'pt',
    'colombo': 'lk',
    'cork': 'ie',
    'dar bouazza': 'ma',
    'dchira el jihadia': 'ma',
    'debrecen': 'hu',
    'dehiwala-mount lavinia': 'lk',
    'dhangadhi': 'np',
    'dharan': 'np',
    'east helsinki': 'fi',
    'el jadida': 'ma',
    'el kelaa des srarhna': 'ma',
    'el mourouj': 'tn',
    'errachidia': 'ma',
    'espoo': 'fi',
    'funchal': 'pt',
    'gabes': 'tn',
    'gent': 'be',
    'graz': 'at',
    'guelmim': 'ma',
    'gyor': 'hu',
    'helsinki': 'fi',
    'hetauda': 'np',
    'inezgane': 'ma',
    'irbid': 'jo',
    'jaffna': 'lk',
    'janakpur': 'np',
    'jyvaskyla': 'fi',
    'kairouan': 'tn',
    'kalmunai': 'lk',
    'kecskemet': 'hu',
    'kenitra': 'ma',
    'khenifra': 'ma',
    'khouribga': 'ma',
    'klagenfurt am worthersee': 'at',
    'ksar el kebir': 'ma',
    'kuopio': 'fi',
    'lahan': 'np',
    'lahti': 'fi',
    'larache': 'ma',
    'larisa': 'gr',
    'leiria': 'pt',
    'leuven': 'be',
    'liberec': 'cz',
    'liege': 'be',
    'limerick': 'ie',
    'linz': 'at',
    'lower hutt': 'nz',
    'maharagama': 'lk',
    'manukau city': 'nz',
    'meknes': 'ma',
    'miskolc': 'hu',
    'mohammedia': 'ma',
    'moratuwa': 'lk',
    'nador': 'ma',
    'namur': 'be',
    'negombo': 'lk',
    'nepalgunj': 'np',
    'nyiregyhaza': 'hu',
    'ostrava': 'cz',
    'oujda': 'ma',
    'oulu': 'fi',
    'patra': 'gr',
    'pecs': 'hu',
    'pilsen': 'cz',
    'pita kotte': 'lk',
    'rabat': 'ma',
    'rijeka': 'hr',
    'russeifa': 'jo',
    'safi': 'ma',
    'sale al jadida': 'ma',
    'salzburg': 'at',
    'settat': 'ma',
    'setubal': 'pt',
    'sfax': 'tn',
    'sousse': 'tn',
    'sri jayewardenepura kotte': 'lk',
    'szeged': 'hu',
    'szekesfehervar': 'hu',
    'tampere': 'fi',
    'tangier': 'ma',
    'taourirt': 'ma',
    'tauranga': 'nz',
    'taza': 'ma',
    'temara': 'ma',
    'tetouan': 'ma',
    'thessaloniki': 'gr',
    'trincomalee': 'lk',
    'turku': 'fi',
    'vantaa': 'fi',
    'viseu': 'pt',
    'zarqa': 'jo',
    '‘ajlun': 'jo',
})  # Stage F wave1

_CYCLONE_BY_REGION_MONTH.update({
    'cn-main': {7: 1500, 8: 2000, 9: 1800, 10: 1000},
    'hk': {7: 2000, 8: 2400, 9: 2200, 10: 1000},
    'mo': {7: 1800, 8: 2200, 9: 2000},
})  # Stage F China
_FLOOD_BY_REGION_MONTH.update({
    'cn-west': {6: 1200, 7: 1500, 8: 1200},
    'cn-main': {6: 1500, 7: 1600, 8: 1400},
    'hk': {6: 1200, 7: 1000},
    'mo': {6: 1000},
})  # Stage F China
_DELAY_BY_REGION_MODE.update({
    'cn-west': {'flight': 40, 'rail': 20, 'bus': 40},
    'cn-main': {'flight': 35, 'rail': 15, 'bus': 25},
    'hk': {'flight': 30, 'rail': 10, 'bus': 20},
    'mo': {'flight': 30, 'rail': 0, 'bus': 20},
})  # Stage F China
_SEISMIC_RESILIENCE_BY_REGION.update({
    'cn-west': 28,
    'cn-main': 55,
    'hk': 72,
    'mo': 70,
})  # Stage F China
_ADVISORY_LEVEL_BY_REGION.update({
    'cn-west': 2,
    'cn-main': 1,
    'hk': 1,
    'mo': 1,
})  # Stage F China
_REGION_SOURCE.update({
    'cn-west': 'seed:cma-china-west-2026',
    'cn-main': 'seed:cma-china-main-2026',
    'hk': 'seed:hko-hongkong-2026',
    'mo': 'seed:smg-macao-2026',
})  # Stage F China
_CITY_TO_REGION.update({
    'acheng': 'cn-main',
    'anbu': 'cn-main',
    'ankang': 'cn-main',
    'anning': 'cn-west',
    'anqing': 'cn-main',
    'anshan': 'cn-main',
    'anshun': 'cn-main',
    'anyang': 'cn-main',
    'baise': 'cn-main',
    'baishan': 'cn-main',
    'baiyin': 'cn-west',
    'banan': 'cn-main',
    "bao'an": 'cn-main',
    "bao'an centre": 'cn-main',
    'baoding': 'cn-main',
    'baoji': 'cn-main',
    'baoshan': 'cn-main',
    'baotou': 'cn-main',
    'basuo': 'cn-main',
    'bayan nur': 'cn-main',
    'bazhong': 'cn-main',
    'beibei': 'cn-main',
    'beihai': 'cn-main',
    'beining': 'cn-main',
    'bei’an': 'cn-main',
    'bengbu': 'cn-main',
    'benxi': 'cn-main',
    'bijie': 'cn-main',
    'binzhou': 'cn-main',
    'bishan': 'cn-main',
    'bole': 'cn-west',
    'boshan': 'cn-main',
    'changchun': 'cn-main',
    'changde': 'cn-main',
    'changji': 'cn-west',
    'changle': 'cn-main',
    'changsha': 'cn-main',
    'changshu': 'cn-main',
    'changyi': 'cn-main',
    'changzhi': 'cn-main',
    'changzhou': 'cn-main',
    'chaoyang': 'cn-main',
    'chaozhou': 'cn-main',
    'chengde': 'cn-main',
    'chengdu': 'cn-west',
    'chenggu': 'cn-main',
    'chenghua': 'cn-main',
    'chengqiao': 'cn-main',
    'chengtangcun': 'cn-main',
    'chenzhou': 'cn-main',
    'chifeng': 'cn-main',
    'chizhou': 'cn-main',
    'chongqing': 'cn-main',
    'chongzuo': 'cn-main',
    'chuxiong': 'cn-west',
    'chuzhou': 'cn-main',
    'cixi': 'cn-main',
    'dali': 'cn-west',
    'dalian': 'cn-main',
    'daliang': 'cn-main',
    'dandong': 'cn-main',
    'danshui': 'cn-main',
    'daqing': 'cn-main',
    'dasha': 'cn-main',
    'datong': 'cn-main',
    'datun': 'cn-main',
    'daxing': 'cn-main',
    'daye': 'cn-main',
    'dazhou': 'cn-main',
    'dengzhou': 'cn-main',
    'deyang': 'cn-west',
    'didao': 'cn-main',
    'dingxi': 'cn-west',
    'dingzhou': 'cn-main',
    'doilungdeqen': 'cn-west',
    'dongguan': 'cn-main',
    'donghai': 'cn-main',
    'dongyang': 'cn-main',
    'dongying': 'cn-main',
    'dunhua': 'cn-main',
    'encheng': 'cn-main',
    'enshi': 'cn-main',
    'e’zhou': 'cn-main',
    'fangchenggang': 'cn-main',
    'fanling': 'hk',
    'fenghuang': 'cn-main',
    'fengxiang': 'cn-main',
    'foshan': 'cn-main',
    'fushun': 'cn-main',
    'fuxin': 'cn-main',
    'fuyang': 'cn-main',
    'fuzhou': 'cn-main',
    'gangu chengguanzhen': 'cn-main',
    'ganzhou': 'cn-main',
    'gaozhou': 'cn-main',
    'ghulja': 'cn-west',
    'gongheyong': 'cn-main',
    'gongzhuling': 'cn-main',
    'guangyuan': 'cn-main',
    'guangzhou': 'cn-main',
    'guang’an': 'cn-main',
    'guankou': 'cn-main',
    'guigang': 'cn-main',
    'guiyang': 'cn-main',
    'gujangbagh': 'cn-west',
    'gunan': 'cn-main',
    'guyuan': 'cn-main',
    'haicheng': 'cn-main',
    'haikou': 'cn-main',
    'hami': 'cn-west',
    'handan': 'cn-main',
    'hanfeng': 'cn-main',
    'hangu': 'cn-main',
    'hangzhou': 'cn-main',
    'hanjia': 'cn-main',
    'hanzhong': 'cn-main',
    'hechi': 'cn-main',
    'hechuan': 'cn-main',
    'hedong': 'cn-main',
    'hefei': 'cn-main',
    'hegang': 'cn-main',
    'heihe': 'cn-main',
    'hejiang': 'cn-main',
    'hengshan': 'cn-main',
    'hengyang': 'cn-main',
    'hepu': 'cn-main',
    'heshan': 'cn-main',
    'heyuan': 'cn-main',
    'heze': 'cn-main',
    'hezhou': 'cn-main',
    'hohhot': 'cn-main',
    'huacheng': 'cn-main',
    "huai'an": 'cn-main',
    'huaibei': 'cn-main',
    'huaihua': 'cn-main',
    'huainan': 'cn-main',
    'huanggang': 'cn-main',
    'huangshi': 'cn-main',
    'huayin': 'cn-main',
    'huixing': 'cn-main',
    'huizhou': 'cn-main',
    'hulan': 'cn-main',
    'hulan ergi': 'cn-main',
    'huludao': 'cn-main',
    'hulunbuir': 'cn-main',
    'humen': 'cn-main',
    'huocheng': 'cn-west',
    'huzhou': 'cn-main',
    'jalai nur': 'cn-main',
    'jiading': 'cn-main',
    'jiamusi': 'cn-main',
    'jiangmen': 'cn-main',
    'jiangyin': 'cn-main',
    'jiaojiang': 'cn-main',
    'jiaozhou': 'cn-main',
    'jiashan': 'cn-main',
    'jiaxing': 'cn-main',
    'jiayuguan': 'cn-west',
    'jieshou': 'cn-main',
    'jieyang': 'cn-main',
    'jilin': 'cn-main',
    'jinan': 'cn-main',
    'jincheng': 'cn-main',
    'jingdezhen': 'cn-main',
    'jinghong': 'cn-west',
    'jingling': 'cn-main',
    'jingmen': 'cn-main',
    'jingzhou': 'cn-main',
    'jinhua': 'cn-main',
    'jining': 'cn-main',
    'jinjiang': 'cn-main',
    'jinshan': 'cn-main',
    'jinshanlu': 'cn-west',
    'jinzhong': 'cn-main',
    'jinzhou': 'cn-main',
    'jiujiang': 'cn-main',
    'jiuquan': 'cn-west',
    'jixi': 'cn-main',
    'jizhou': 'cn-main',
    'ji’an': 'cn-main',
    'kaifeng': 'cn-main',
    'kaili': 'cn-main',
    'kaiyuan': 'cn-west',
    'kangding': 'cn-west',
    'karamay': 'cn-west',
    'kashgar': 'cn-west',
    'korla': 'cn-west',
    'kunming': 'cn-west',
    'kunshan': 'cn-main',
    'laiwu': 'cn-main',
    'laixi': 'cn-main',
    'laizhou': 'cn-main',
    'langfang': 'cn-main',
    'lanzhou': 'cn-west',
    'lecheng': 'cn-main',
    'leiyang': 'cn-main',
    'lengshuijiang': 'cn-main',
    'lianjiang': 'cn-main',
    'lianyungang': 'cn-main',
    'liaocheng': 'cn-main',
    'liaoyang': 'cn-main',
    'lincang': 'cn-west',
    'linfen': 'cn-main',
    'linqu': 'cn-main',
    'linxi': 'cn-main',
    'linxia chengguanzhen': 'cn-west',
    'linyi': 'cn-main',
    'lishui': 'cn-main',
    'liuzhou': 'cn-main',
    'longfeng': 'cn-main',
    'longgang': 'cn-main',
    'longjing': 'cn-main',
    'longshan': 'cn-main',
    'longyan': 'cn-main',
    'loudi': 'cn-main',
    'luliang': 'cn-main',
    'luohe': 'cn-main',
    'luohu district': 'cn-main',
    'luoyang': 'cn-main',
    'lushui': 'cn-west',
    'luzhou': 'cn-main',
    'lu’an': 'cn-main',
    'ma on shan': 'hk',
    'maba': 'cn-main',
    'macau': 'mo',
    'majie': 'cn-west',
    'maoming': 'cn-main',
    'ma’anshan': 'cn-main',
    'meishan': 'cn-west',
    'meizhou': 'cn-main',
    'mengmao': 'cn-west',
    'mengzi': 'cn-west',
    'mentougou': 'cn-main',
    'mianyang': 'cn-west',
    'minhang': 'cn-main',
    'mudanjiang': 'cn-main',
    'nanchang': 'cn-main',
    'nanchong': 'cn-main',
    'nanjin': 'cn-main',
    'nanjing': 'cn-main',
    'nanning': 'cn-main',
    'nanqiao': 'cn-main',
    'nantong': 'cn-main',
    'nantou': 'cn-main',
    'nanyang': 'cn-main',
    'neijiang': 'cn-main',
    'new territories': 'hk',
    'ningbo': 'cn-main',
    'ningde': 'cn-main',
    'ning’er': 'cn-west',
    'nossa senhora de fatima': 'cn-main',
    'nyingchi': 'cn-west',
    'ordos': 'cn-main',
    'panjin': 'cn-main',
    'panshan': 'cn-main',
    'panzhihua': 'cn-west',
    'pingdingshan': 'cn-main',
    'pingliang': 'cn-main',
    'pingshan': 'cn-main',
    'pingxiang': 'cn-main',
    "pu'er": 'cn-west',
    'pulandian': 'cn-main',
    'puning': 'cn-main',
    'puqi': 'cn-main',
    'putian': 'cn-main',
    'puyang': 'cn-main',
    'puyang chengguanzhen': 'cn-main',
    'qianjiang': 'cn-main',
    'qibao': 'cn-main',
    'qingdao': 'cn-main',
    'qingnian': 'cn-main',
    'qingpu': 'cn-main',
    'qingyang': 'cn-main',
    'qingyuan': 'cn-main',
    'qingzhou': 'cn-main',
    'qinhuangdao': 'cn-main',
    'qinzhou': 'cn-main',
    'qionghai': 'cn-main',
    'qiqihar': 'cn-main',
    'qitaihe': 'cn-main',
    'quanzhou': 'cn-main',
    'quzhou': 'cn-main',
    'rizhao': 'cn-main',
    'rugao': 'cn-main',
    'rui’an': 'cn-main',
    'san tung chung hang': 'hk',
    'sanhe': 'cn-main',
    'sanming': 'cn-main',
    'sanshui': 'cn-main',
    'santo antonio': 'cn-main',
    'sanya': 'cn-main',
    'shache': 'cn-west',
    'shajing': 'cn-main',
    'shangqiu': 'cn-main',
    'shangrao': 'cn-main',
    'shangri-la': 'cn-west',
    'shangyu': 'cn-main',
    'shanhaiguan': 'cn-main',
    'shantou': 'cn-main',
    'shanwei': 'cn-main',
    'shaoguan': 'cn-main',
    'shaoshan': 'cn-main',
    'shaoxing': 'cn-main',
    'shaping': 'cn-main',
    'shenyang': 'cn-main',
    'shenzhen': 'cn-main',
    'shihezi': 'cn-west',
    'shijiazhuang': 'cn-main',
    'shijie': 'cn-main',
    'shilong': 'cn-main',
    'shiqi': 'cn-main',
    'shiqiao': 'cn-main',
    'shiyan': 'cn-main',
    'shouguang': 'cn-main',
    'shuangcheng': 'cn-main',
    'shuanglonghu': 'cn-main',
    'shuangyashan': 'cn-main',
    'shuifu': 'cn-west',
    'shuizhai': 'cn-main',
    'shunyi': 'cn-main',
    'songcheng': 'cn-main',
    'songjiang': 'cn-main',
    'suihua': 'cn-main',
    'suining': 'cn-main',
    'suizhou': 'cn-main',
    'sujiatun': 'cn-main',
    'suqian': 'cn-main',
    'suzhou': 'cn-main',
    'tacheng': 'cn-west',
    'tai po': 'hk',
    'taicang': 'cn-main',
    'taipa': 'mo',
    'taishan': 'cn-main',
    'taiyuan': 'cn-main',
    'taizhou': 'cn-main',
    'tai’an': 'cn-main',
    'tanggu': 'cn-main',
    'tangshan': 'cn-main',
    'tantou': 'cn-main',
    'tanzhou': 'cn-main',
    'tengyue': 'cn-west',
    'tianjin': 'cn-main',
    'tianshui': 'cn-main',
    'tieling': 'cn-main',
    'tin shui wai': 'hk',
    'tongchuan': 'cn-main',
    'tonghua': 'cn-main',
    'tongling': 'cn-main',
    'tongren': 'cn-west',
    'tongshan': 'cn-main',
    'tongzhou': 'cn-main',
    'tuen mun': 'hk',
    'tumxuk': 'cn-west',
    'tung chung': 'hk',
    'turpan': 'cn-west',
    'ulanhot': 'cn-main',
    'ulanqab': 'cn-main',
    'urumqi': 'cn-west',
    'wanning': 'cn-main',
    'wanxian': 'cn-main',
    'weifang': 'cn-main',
    'weihai': 'cn-main',
    'weinan': 'cn-main',
    'wenchang': 'cn-main',
    'wenshan city': 'cn-west',
    'wenzhou': 'cn-main',
    'wugang': 'cn-main',
    'wuhan': 'cn-main',
    'wuhu': 'cn-main',
    'wushan': 'cn-main',
    'wuwei': 'cn-west',
    'wuxi': 'cn-main',
    'wuzhou': 'cn-main',
    'xiamen': 'cn-main',
    'xiangcheng': 'cn-west',
    'xiangtan': 'cn-main',
    'xiangyang': 'cn-main',
    'xianyang': 'cn-main',
    'xiaogan': 'cn-main',
    'xiayang': 'cn-main',
    'xiazhen': 'cn-main',
    'xichang': 'cn-west',
    'xincheng': 'cn-west',
    'xinghua': 'cn-main',
    'xingning': 'cn-main',
    'xingtai': 'cn-main',
    'xingyi': 'cn-west',
    'xining': 'cn-west',
    'xinji': 'cn-main',
    'xinyang': 'cn-main',
    'xinyu': 'cn-main',
    'xinyuan': 'cn-west',
    'xinzhou': 'cn-main',
    'xiuying': 'cn-main',
    'xuancheng': 'cn-main',
    'xuchang': 'cn-main',
    "ya'an": 'cn-west',
    'yancheng': 'cn-main',
    'yangcheng': 'cn-main',
    'yangquan': 'cn-main',
    'yangshuo': 'cn-main',
    'yangzhou': 'cn-main',
    'yanji': 'cn-main',
    'yantai': 'cn-main',
    'yanzhou': 'cn-main',
    'yan’an': 'cn-main',
    'yezhou': 'cn-main',
    'yibin': 'cn-west',
    'yichang': 'cn-main',
    'yidu': 'cn-main',
    'yinchuan': 'cn-main',
    'yingkou': 'cn-main',
    'yiwu': 'cn-main',
    'yixing': 'cn-main',
    'yongchuan': 'cn-main',
    'yongzhou': 'cn-main',
    'yuci': 'cn-main',
    'yuen long kau hui': 'hk',
    'yueyang': 'cn-main',
    'yulin': 'cn-main',
    'yuncheng': 'cn-main',
    'yunfu': 'cn-main',
    'yunlong': 'cn-main',
    'yushu': 'cn-west',
    'yuxi': 'cn-west',
    'yuyao': 'cn-main',
    'zaozhuang': 'cn-main',
    'zhangjiagang': 'cn-main',
    'zhangjiakou': 'cn-main',
    'zhangye': 'cn-west',
    'zhangzhou': 'cn-main',
    'zhanjiang': 'cn-main',
    'zhaoqing': 'cn-main',
    'zhaotong': 'cn-west',
    'zhengding': 'cn-main',
    'zhengzhou': 'cn-main',
    'zhenjiang': 'cn-main',
    'zhenzhou': 'cn-main',
    'zhongshan': 'cn-main',
    'zhongwei': 'cn-main',
    'zhongxiang': 'cn-main',
    'zhoucun': 'cn-main',
    'zhoushan': 'cn-main',
    'zhu cheng city': 'cn-main',
    'zhucheng': 'cn-main',
    'zhuhai': 'cn-main',
    'zhuji': 'cn-main',
    'zhujing': 'cn-main',
    'zhuzhou': 'cn-main',
    'zibo': 'cn-main',
    'zigong': 'cn-west',
    'zunyi': 'cn-main',
})  # Stage F China

_CYCLONE_BY_REGION_MONTH.update({
    'mu': {1: 2200, 2: 2500, 3: 1800, 4: 800, 5: 200, 11: 500, 12: 1500},
    'gw': {8: 150, 9: 200, 10: 100},
    'cv': {7: 300, 8: 1300, 9: 1400, 10: 800, 11: 200},
    'tl': {1: 1800, 2: 2200, 3: 1600, 4: 600, 11: 400, 12: 900},
    'mr': {6: 100, 7: 150, 8: 250, 9: 300, 10: 200, 11: 100},
})  # Stage F wave2
_FLOOD_BY_REGION_MONTH.update({
    'gq': {3: 800, 4: 1200, 5: 1600, 6: 1800, 7: 1800, 8: 1900, 9: 2000, 10: 1700, 11: 1200, 12: 800},
    'mu': {1: 1800, 2: 2000, 3: 1600, 4: 800, 11: 500, 12: 1200},
    'gw': {6: 1200, 7: 2800, 8: 3500, 9: 2500, 10: 1000},
    'cv': {7: 600, 8: 1800, 9: 1600, 10: 900},
    'gy': {1: 1800, 2: 1600, 3: 800, 4: 800, 5: 2200, 6: 2800, 7: 2400, 8: 1800, 9: 800, 10: 800, 11: 2000, 12: 2200},
    'tl': {1: 2200, 2: 2400, 3: 1800, 4: 900, 11: 800, 12: 1600},
    'sr': {1: 1800, 4: 1800, 5: 2400, 6: 2200, 7: 1400, 8: 800, 11: 1600, 12: 2000},
    'tm': {2: 400, 3: 800, 4: 1200, 5: 900, 6: 400},
    'ga': {1: 1400, 2: 1400, 3: 800, 4: 1600, 5: 1200, 9: 1200, 10: 1700, 11: 1800, 12: 1000},
    'mk': {3: 600, 4: 900, 5: 1200, 6: 800, 10: 600, 11: 700, 12: 600},
    'mr': {7: 800, 8: 1800, 9: 1600, 10: 600},
})  # Stage F wave2
_DELAY_BY_REGION_MODE.update({
    'gq': {'flight': 55, 'rail': 0, 'bus': 70},
    'mu': {'flight': 30, 'rail': 0, 'bus': 20},
    'gw': {'flight': 55, 'rail': 0, 'bus': 90},
    'cv': {'flight': 52, 'rail': 0, 'bus': 20},
    'gy': {'flight': 50, 'rail': 0, 'bus': 65},
    'tl': {'flight': 55, 'rail': 0, 'bus': 60},
    'sr': {'flight': 35, 'rail': 0, 'bus': 55},
    'tm': {'flight': 55, 'rail': 35, 'bus': 60},
    'ga': {'flight': 55, 'rail': 30, 'bus': 60},
    'mk': {'flight': 28, 'rail': 55, 'bus': 35},
    'mr': {'flight': 55, 'rail': 0, 'bus': 90},
})  # Stage F wave2
_SEISMIC_RESILIENCE_BY_REGION.update({
    'gq': 38,
    'mu': 72,
    'gw': 52,
    'cv': 32,
    'gy': 68,
    'tl': 22,
    'sr': 72,
    'tm': 32,
    'ga': 72,
    'mk': 32,
    'mr': 68,
})  # Stage F wave2
_ADVISORY_LEVEL_BY_REGION.update({
    'gq': 2,
    'mu': 2,
    'gw': 3,
    'cv': 1,
    'gy': 3,
    'tl': 2,
    'sr': 1,
    'tm': 2,
    'ga': 2,
    'mk': 1,
    'mr': 3,
})  # Stage F wave2
_CIVIL_UNREST_BY_REGION.update({
    'gq': 800,
    'gw': 2000,
    'gy': 400,
    'tl': 400,
    'sr': 300,
    'tm': 300,
    'ga': 600,
    'mk': 600,
    'mr': 1200,
})  # Stage F wave2
_REGION_SOURCE.update({
    'gq': 'seed:travel-risk-domain-2026',
    'mu': 'seed:society-region-profile-2026',
    'gw': 'seed:society-region-profile-2026',
    'cv': 'WebSearch:NOAA/NHC+IFRC+GFDRR+StateDept+VolcanoDiscovery 2026-06-18',
    'gy': 'seed:society-region-profile-2026',
    'tl': 'seed:society-region-profile-2026',
    'sr': 'seed:society-region-profile-2026',
    'tm': 'seed:society-region-profile-2026',
    'ga': 'seed:society-region-profile-2026',
    'mk': 'seed:society-region-profile-2026',
    'mr': 'seed:society-region-profile-2026',
})  # Stage F wave2
_CITY_TO_REGION.update({
    'ashgabat': 'tm',
    'bata': 'gq',
    'beau bassin-rose hill': 'mu',
    'bissau': 'gw',
    'dasoguz': 'tm',
    'dili': 'tl',
    'franceville': 'ga',
    'georgetown': 'gy',
    'libreville': 'ga',
    'malabo': 'gq',
    'mary': 'tm',
    'nassau': 'bs',
    'nouadhibou': 'mr',
    'nouakchott': 'mr',
    'paramaribo': 'sr',
    'port louis': 'mu',
    'port-gentil': 'ga',
    'praia': 'cv',
    'skopje': 'mk',
    'turkmenabat': 'tm',
    'vacoas': 'mu',
})  # Stage F wave2

_CYCLONE_BY_REGION_MONTH.update({
    'sv': {6: 400, 7: 600, 8: 900, 9: 1800, 10: 1400, 11: 400},
    'jm': {6: 300, 7: 600, 8: 2500, 9: 3500, 10: 3500, 11: 500},
})  # Stage F wave3
_FLOOD_BY_REGION_MONTH.update({
    'er': {6: 800, 7: 1800, 8: 2000, 9: 1200},
    'sv': {5: 800, 6: 1800, 7: 2000, 8: 2200, 9: 2500, 10: 1600, 11: 600},
    'lr': {5: 1200, 6: 2800, 7: 3500, 8: 3800, 9: 3200, 10: 1800, 11: 900},
    'jm': {5: 1000, 6: 1400, 7: 1000, 8: 2000, 9: 2200, 10: 2400, 11: 900},
    'sl': {5: 800, 6: 2200, 7: 3500, 8: 3800, 9: 2500, 10: 1000, 11: 600},
    'bi': {3: 1800, 4: 2200, 9: 1000, 10: 1600, 11: 1600},
    'bj': {5: 1000, 6: 800, 7: 1800, 8: 2200, 9: 2000, 10: 1200},
})  # Stage F wave3
_DELAY_BY_REGION_MODE.update({
    'er': {'flight': 75, 'rail': 0, 'bus': 60},
    'sv': {'flight': 38, 'rail': 0, 'bus': 55},
    'lr': {'flight': 55, 'rail': 0, 'bus': 90},
    'jm': {'flight': 35, 'rail': 0, 'bus': 55},
    'sl': {'flight': 55, 'rail': 0, 'bus': 75},
    'bi': {'flight': 55, 'rail': 0, 'bus': 75},
    'bj': {'flight': 55, 'rail': 0, 'bus': 60},
})  # Stage F wave3
_SEISMIC_RESILIENCE_BY_REGION.update({
    'er': 16,
    'sv': 22,
    'lr': 50,
    'jm': 22,
    'sl': 50,
    'bi': 38,
    'bj': 72,
})  # Stage F wave3
_ADVISORY_LEVEL_BY_REGION.update({
    'er': 2,
    'sv': 1,
    'lr': 2,
    'jm': 2,
    'sl': 2,
    'bi': 3,
    'bj': 2,
})  # Stage F wave3
_CIVIL_UNREST_BY_REGION.update({
    'er': 2500,
    'sv': 300,
    'lr': 1200,
    'jm': 400,
    'sl': 800,
    'bi': 1800,
    'bj': 1200,
})  # Stage F wave3
_REGION_SOURCE.update({
    'er': 'seed:society-region-profile-2026',
    'sv': 'seed:society-region-profile-2026',
    'lr': 'seed:society-region-profile-2026',
    'jm': 'seed:society-region-profile-2026',
    'sl': 'seed:society-region-profile-2026',
    'bi': 'seed:society-region-profile-2026',
    'bj': 'seed:society-region-profile-2026',
})  # Stage F wave3
_CITY_TO_REGION.update({
    'abomey': 'bj',
    'abomey-calavi': 'bj',
    'asmara': 'er',
    'battambang': 'kh',
    'bo': 'sl',
    'bokhtar': 'tj',
    'bujumbura': 'bi',
    'cotonou': 'bj',
    'dolisie': 'cg',
    'dushanbe': 'tj',
    'freetown': 'sl',
    'ganja': 'az',
    'godome': 'bj',
    'isfara': 'tj',
    'istaravshan': 'tj',
    'juan diaz': 'pa',
    'kenema': 'sl',
    'khujand': 'tj',
    'kingston': 'jm',
    'koidu': 'sl',
    'konibodom': 'tj',
    'kulob': 'tj',
    'lankaran': 'az',
    'limassol': 'cy',
    'mejicanos': 'sv',
    'mingachevir': 'az',
    'monrovia': 'lr',
    'new kingston': 'jm',
    'nicosia': 'cy',
    'nkayi': 'cg',
    'panama city': 'pa',
    'parakou': 'bj',
    'phnom penh': 'kh',
    'podgorica': 'me',
    'pointe-noire': 'cg',
    'portmore': 'jm',
    'porto-novo': 'bj',
    'san miguel': 'sv',
    'san miguelito': 'pa',
    'san salvador': 'sv',
    'santa ana': 'sv',
    'santa tecla': 'sv',
    'soyapango': 'sv',
    'sumqayit': 'az',
    'tovuz': 'az',
    'yevlakh': 'az',
})  # Stage F wave3

_CYCLONE_BY_REGION_MONTH.update({
    'hn': {6: 400, 7: 800, 8: 1800, 9: 3200, 10: 3500, 11: 1200},
})  # Stage F wave4
_FLOOD_BY_REGION_MONTH.update({
    'am': {3: 800, 4: 1600, 5: 1800, 6: 1200, 7: 700, 8: 600},
    'uy': {1: 900, 2: 800, 3: 800, 4: 1200, 5: 900, 6: 800, 8: 700, 9: 900, 10: 1100},
    'tg': {3: 400, 4: 500, 5: 900, 6: 2000, 7: 2200, 8: 2400, 9: 2100, 10: 1200},
    'hn': {5: 800, 6: 1800, 7: 2000, 8: 2500, 9: 3000, 10: 3200, 11: 1600},
})  # Stage F wave4
_DELAY_BY_REGION_MODE.update({
    'am': {'flight': 35, 'rail': 55, 'bus': 50},
    'uy': {'flight': 28, 'rail': 0, 'bus': 22},
    'tg': {'flight': 55, 'rail': 0, 'bus': 60},
    'hn': {'flight': 55, 'rail': 0, 'bus': 70},
})  # Stage F wave4
_SEISMIC_RESILIENCE_BY_REGION.update({
    'am': 28,
    'uy': 78,
    'tg': 42,
    'hn': 35,
})  # Stage F wave4
_ADVISORY_LEVEL_BY_REGION.update({
    'am': 2,
    'uy': 2,
    'tg': 2,
    'hn': 3,
})  # Stage F wave4
_CIVIL_UNREST_BY_REGION.update({
    'am': 600,
    'tg': 900,
    'hn': 600,
})  # Stage F wave4
_REGION_SOURCE.update({
    'am': 'seed:society-region-profile-2026',
    'uy': 'seed:society-region-profile-2026',
    'tg': 'seed:society-region-profile-2026',
    'hn': 'seed:society-region-profile-2026',
})  # Stage F wave4
_CITY_TO_REGION.update({
    'ajapnyak': 'am',
    'arabkir': 'am',
    'balcon de la lisa': 'cu',
    'bayamo': 'cu',
    'bishkek': 'kg',
    'boyeros': 'cu',
    'camaguey': 'cu',
    'carolina': 'pr',
    'catacamas': 'hn',
    'chinandega': 'ni',
    'ciego de avila': 'cu',
    'cienfuegos': 'cu',
    'danli': 'hn',
    'durres': 'al',
    'el progreso': 'hn',
    'elbasan': 'al',
    'erebuni': 'am',
    'fontanar': 'cu',
    'gisenyi': 'rw',
    'guantanamo': 'cu',
    'gyumri': 'am',
    'holguin': 'cu',
    'kara': 'tg',
    'kentron': 'am',
    'la ceiba': 'hn',
    'las tunas': 'cu',
    'leon': 'ni',
    'lome': 'tg',
    'malatia-sebastia': 'am',
    'managua': 'ni',
    'manas': 'kg',
    'masaya': 'ni',
    'matagalpa': 'ni',
    'matanzas': 'cu',
    'montevideo': 'uy',
    'nor nork': 'am',
    'nyagatare': 'rw',
    'olanchito': 'hn',
    'osh': 'kg',
    'palma soriano': 'cu',
    'pinar del rio': 'cu',
    'ponce': 'pr',
    'puerto cortez': 'hn',
    'reykjavik': 'is',
    'san pedro sula': 'hn',
    'sancti spiritus': 'cu',
    'santa clara': 'cu',
    'santiago de cuba': 'cu',
    'savannakhet': 'la',
    'shengavit': 'am',
    'siguatepeque': 'hn',
    'sokode': 'tg',
    'tegucigalpa': 'hn',
    'tirana': 'al',
    'tocoa': 'hn',
    'vientiane': 'la',
    'vlore': 'al',
    'windhoek': 'na',
    'yerevan': 'am',
})  # Stage F wave4

_CYCLONE_BY_REGION_MONTH.update({
    'do': {6: 500, 7: 1200, 8: 2800, 9: 3500, 10: 2200, 11: 800},
})  # Stage F wave5
_FLOOD_BY_REGION_MONTH.update({
    'ao': {1: 2500, 2: 2800, 3: 2200, 4: 1200, 10: 800, 11: 1600, 12: 2000},
    'do': {5: 1200, 6: 1800, 7: 1600, 8: 1900, 9: 2100, 10: 1700, 11: 1000},
    'ci': {4: 800, 5: 1800, 6: 2500, 7: 2200, 8: 1900, 9: 2000, 10: 1600, 11: 800},
    'gm': {6: 500, 7: 1800, 8: 2800, 9: 2200, 10: 900},
    'gn': {5: 800, 6: 2200, 7: 3500, 8: 4000, 9: 3000, 10: 1200},
    'il': {1: 800, 2: 900, 3: 600, 9: 300, 10: 500, 11: 1200, 12: 1000},
    'ls': {1: 1800, 2: 2000, 3: 1600, 4: 700, 10: 600, 11: 800, 12: 1200},
})  # Stage F wave5
_DELAY_BY_REGION_MODE.update({
    'ao': {'flight': 55, 'rail': 0, 'bus': 75},
    'do': {'flight': 40, 'rail': 0, 'bus': 55},
    'ci': {'flight': 38, 'rail': 0, 'bus': 55},
    'gm': {'flight': 55, 'rail': 0, 'bus': 65},
    'gn': {'flight': 65, 'rail': 0, 'bus': 90},
    'il': {'flight': 35, 'rail': 15, 'bus': 20},
    'ls': {'flight': 40, 'rail': 0, 'bus': 55},
})  # Stage F wave5
_SEISMIC_RESILIENCE_BY_REGION.update({
    'ao': 72,
    'do': 20,
    'ci': 68,
    'gm': 72,
    'gn': 42,
    'il': 30,
    'ls': 62,
})  # Stage F wave5
_ADVISORY_LEVEL_BY_REGION.update({
    'ao': 3,
    'do': 2,
    'ci': 2,
    'gm': 2,
    'gn': 2,
    'il': 3,
    'ls': 2,
})  # Stage F wave5
_CIVIL_UNREST_BY_REGION.update({
    'ao': 600,
    'do': 400,
    'ci': 900,
    'gm': 600,
    'gn': 1700,
    'il': 2500,
    'ls': 600,
})  # Stage F wave5
_REGION_SOURCE.update({
    'ao': 'seed:society-region-profile-2026',
    'do': 'seed:society-region-profile-2026',
    'ci': 'seed:society-region-profile-2026',
    'gm': 'seed:society-region-profile-2026',
    'gn': 'seed:society-region-profile-2026',
    'il': 'seed:society-region-profile-2026',
    'ls': 'seed:society-region-profile-2026',
})  # Stage F wave5
_CITY_TO_REGION.update({
    'abengourou': 'ci',
    'abidjan': 'ci',
    'abobo': 'ci',
    'amanfrom': 'gh',
    'ambato': 'ec',
    'andijon': 'uz',
    'angren': 'uz',
    'anyama': 'ci',
    'ashaiman': 'gh',
    'ashdod': 'il',
    'ashkelon': 'il',
    'atsiaman': 'gh',
    'balti': 'md',
    'bat yam': 'il',
    'batumi': 'ge',
    'beersheba': 'il',
    'bella vista': 'do',
    'bender': 'md',
    'benfica': 'ao',
    'benguela': 'ao',
    'bnei brak': 'il',
    'bondoukou': 'ci',
    'bouafle': 'ci',
    'bouake': 'ci',
    'bukhara': 'uz',
    'cabinda': 'ao',
    'cacuaco': 'ao',
    'calumbo': 'ao',
    'camama': 'ao',
    'camayenne': 'gn',
    'chilanzar': 'uz',
    'chirchiq': 'uz',
    'chust': 'uz',
    'conakry': 'gn',
    'cuenca': 'ec',
    'cuito': 'ao',
    'dabou': 'ci',
    'daloa': 'ci',
    'daoukro': 'ci',
    'daule': 'ec',
    'diourbel': 'sn',
    'divo': 'ci',
    'dixinn': 'gn',
    'dubreka': 'gn',
    'duekoue': 'ci',
    'eloy alfaro': 'ec',
    'esmeraldas': 'ec',
    'fergana': 'uz',
    'francistown': 'bw',
    'gaborone': 'bw',
    'gagnoa': 'ci',
    'guayaquil': 'ec',
    'haifa': 'il',
    'ho': 'gh',
    'huambo': 'ao',
    'ibarra': 'ec',
    'ingombota': 'ao',
    'jaffa': 'il',
    'jerusalem': 'il',
    'jizzax': 'uz',
    'kamsar': 'gn',
    'kankan': 'gn',
    'kaolack': 'sn',
    'kfar saba': 'il',
    'kikolo': 'ao',
    'kindia': 'gn',
    'kissidougou': 'gn',
    'koforidua': 'gh',
    'kolda': 'sn',
    'korhogo': 'ci',
    'koumassi': 'ci',
    'kumasi': 'gh',
    'kutaisi': 'ge',
    'la romana': 'do',
    'la vega': 'do',
    'labe': 'gn',
    'latacunga': 'ec',
    'lobito': 'ao',
    'loja': 'ec',
    'louga': 'sn',
    'lubango': 'ao',
    'machala': 'ec',
    'maianga': 'ao',
    'malanje': 'ao',
    'man': 'ci',
    'maneah': 'gn',
    'manta': 'ec',
    'marcory': 'ci',
    'marg‘ilon': 'uz',
    'maseru': 'ls',
    'mbanza kongo': 'ao',
    'mbour': 'sn',
    'medina estates': 'gh',
    'menongue': 'ao',
    'milagro': 'ec',
    'mossamedes': 'ao',
    "n'dalatando": 'ao',
    'namangan': 'uz',
    'navegantes': 'ao',
    'navoiy': 'uz',
    'netanya': 'il',
    'nova vida': 'ao',
    'nukus': 'uz',
    'nzerekore': 'gn',
    'obuase': 'gh',
    'olmaliq': 'uz',
    'ondjiva': 'ao',
    'panguila': 'ao',
    'petah tiqva': 'il',
    'portoviejo': 'ec',
    'puerto plata': 'do',
    'punta cana': 'do',
    'qarshi': 'uz',
    'qo‘qon': 'uz',
    'quevedo': 'ec',
    'ramat gan': 'il',
    'rangel': 'ao',
    'rehovot': 'il',
    'riobamba': 'ec',
    'rishon letsiyyon': 'il',
    'rufisque': 'sn',
    'rufisque est': 'sn',
    'rustavi': 'ge',
    'saint-louis': 'sn',
    'salvaleon de higuey': 'do',
    'samba': 'ao',
    'sambizanga': 'ao',
    'san cristobal': 'do',
    'san francisco de macoris': 'do',
    'san pedro de macoris': 'do',
    'san-pedro': 'ci',
    'santiago de los caballeros': 'do',
    'santo domingo': 'do',
    'santo domingo de los colorados': 'ec',
    'santo domingo este': 'do',
    'santo domingo oeste': 'do',
    'saurimo': 'ao',
    'seguela': 'ci',
    'sekondi': 'gh',
    'sekondi-takoradi': 'gh',
    'serekunda': 'gm',
    'sergeli': 'uz',
    'shahrisabz': 'uz',
    'siguiri': 'gn',
    'sinfra': 'ci',
    'soubre': 'ci',
    'soyo': 'ao',
    'sumbe': 'ao',
    'takoradi': 'gh',
    'talatona': 'ao',
    'tamale': 'gh',
    'tambacounda': 'sn',
    'tashkent': 'uz',
    'tel aviv': 'il',
    'tema': 'gh',
    'teshi old town': 'gh',
    'thies': 'sn',
    'thies nones': 'sn',
    'tiebo': 'sn',
    'tiraspol': 'md',
    'tirmiz': 'uz',
    'tivaouane': 'sn',
    'twifu praso': 'gh',
    'urganch': 'uz',
    'viana': 'ao',
    'vila flor': 'ao',
    'west jerusalem': 'il',
    'yamoussoukro': 'ci',
    'yunusobod': 'uz',
    'ziguinchor': 'sn',
})  # Stage F wave5

_CYCLONE_BY_REGION_MONTH.update({
})  # Stage F wave6
_FLOOD_BY_REGION_MONTH.update({
    'td': {7: 1800, 8: 2800, 9: 2400, 10: 2200, 11: 1400},
    'lv': {1: 500, 2: 600, 3: 800, 4: 1200, 7: 600, 11: 600, 12: 700},
    'py': {1: 1800, 2: 1900, 3: 1600, 4: 1000, 5: 900, 10: 700, 11: 1200, 12: 1500},
    'lt': {1: 300, 2: 400, 3: 400, 4: 600, 5: 400, 11: 300, 12: 300},
    'cm': {4: 800, 5: 1600, 6: 2200, 7: 2800, 8: 3000, 9: 2600, 10: 1800, 11: 600},
})  # Stage F wave6
_DELAY_BY_REGION_MODE.update({
    'td': {'flight': 65, 'rail': 0, 'bus': 90},
    'lv': {'flight': 28, 'rail': 22, 'bus': 18},
    'py': {'flight': 35, 'rail': 0, 'bus': 50},
    'lt': {'flight': 28, 'rail': 22, 'bus': 18},
    'cm': {'flight': 55, 'rail': 40, 'bus': 90},
})  # Stage F wave6
_SEISMIC_RESILIENCE_BY_REGION.update({
    'td': 45,
    'lv': 78,
    'py': 68,
    'lt': 82,
    'cm': 42,
})  # Stage F wave6
_ADVISORY_LEVEL_BY_REGION.update({
    'td': 4,
    'lv': 1,
    'py': 2,
    'lt': 1,
    'cm': 3,
})  # Stage F wave6
_CIVIL_UNREST_BY_REGION.update({
    'td': 2500,
    'lv': 150,
    'py': 400,
    'lt': 150,
    'cm': 2200,
})  # Stage F wave6
_REGION_SOURCE.update({
    'td': 'seed:society-region-profile-2026',
    'lv': 'seed:society-region-profile-2026',
    'py': 'seed:society-region-profile-2026',
    'lt': 'seed:society-region-profile-2026',
    'cm': 'seed:society-region-profile-2026',
})  # Stage F wave6
_CITY_TO_REGION.update({
    'abbottabad': 'pk',
    'abeche': 'td',
    'adigrat': 'et',
    'ahmadpur east': 'pk',
    'aktau': 'kz',
    'aktobe': 'kz',
    'al fayyum': 'eg',
    'al hawamidiyah': 'eg',
    'al khusus': 'eg',
    'al mahallah al kubra': 'eg',
    'al mansurah': 'eg',
    'al qahirah al jadidah': 'eg',
    'al ‘ashir min ramadan': 'eg',
    'alexandria': 'eg',
    'arba minch': 'et',
    'arish': 'eg',
    'as sinbillawayn': 'eg',
    'asela': 'et',
    'ashmun': 'eg',
    'assiut': 'eg',
    'astana': 'kz',
    'asuncion': 'py',
    'aswan': 'eg',
    'atyrau': 'kz',
    'awasa': 'et',
    'bafoussam': 'cm',
    'bahawalpur': 'pk',
    'bahir dar': 'et',
    'bamenda': 'cm',
    'bani suwayf': 'eg',
    'banja luka': 'ba',
    'bannu': 'pk',
    'battagram': 'pk',
    'bertoua': 'cm',
    'bishoftu': 'et',
    'buea': 'cm',
    'bulawayo': 'zw',
    'burgas': 'bg',
    'capiata': 'py',
    'chichicastenango': 'gt',
    'chiniot': 'pk',
    'chiquimula': 'gt',
    'ciudad del este': 'py',
    'coatepeque': 'gt',
    'coban': 'gt',
    'cochabamba': 'bo',
    'damietta': 'eg',
    'debre birhan': 'et',
    'debre mark’os': 'et',
    'debre tabor': 'et',
    'dera ghazi khan': 'pk',
    'dera ismail khan': 'pk',
    'dessie': 'et',
    'dila': 'et',
    'dire dawa': 'et',
    'disuq': 'eg',
    'douala': 'cm',
    'ebolowa': 'cm',
    'edea': 'cm',
    'ekibastuz': 'kz',
    'escuintla': 'gt',
    'faisalabad': 'pk',
    'fernando de la mora': 'py',
    'foumban': 'cm',
    'gujranwala': 'pk',
    'gujrat': 'pk',
    'gweru': 'zw',
    'halwan': 'eg',
    'harar': 'et',
    'harare': 'zw',
    'hosa’ina': 'et',
    'hub': 'pk',
    'hurghada': 'eg',
    'hyderabad': 'pk',
    'inda silase': 'et',
    'islamabad': 'pk',
    'ismailia': 'eg',
    'jacobabad': 'pk',
    'jalapa': 'gt',
    'jhang sadr': 'pk',
    'jhelum': 'pk',
    'jijiga': 'et',
    'jimma': 'et',
    'jutiapa': 'gt',
    'kadoma': 'zw',
    'kafr ash shaykh': 'eg',
    'kamoke': 'pk',
    'karachi': 'pk',
    'karagandy': 'kz',
    'kaunas': 'lt',
    'khanpur': 'pk',
    'khushab': 'pk',
    'khuzdar': 'pk',
    'klaipeda': 'lt',
    'kokshetau': 'kz',
    'kombolcha': 'et',
    'kostanay': 'kz',
    'kousseri': 'cm',
    'kumba': 'cm',
    'kumbo': 'cm',
    'kwekwe': 'zw',
    'kyzylorda': 'kz',
    'lahore': 'pk',
    'lambare': 'py',
    'larkana': 'pk',
    'lilongwe': 'mw',
    'limbe': 'cm',
    'lodhran': 'pk',
    'malir cantonment': 'pk',
    'mandi bahauddin': 'pk',
    'maroua': 'cm',
    'marsa matruh': 'eg',
    'mianwali': 'pk',
    'mingora': 'pk',
    'minya': 'eg',
    'mirpur khas': 'pk',
    'mixco': 'gt',
    'model town': 'pk',
    'moundou': 'td',
    'multan': 'pk',
    'muridke': 'pk',
    'mutare': 'zw',
    'muzaffarabad': 'pk',
    'muzaffargarh': 'pk',
    'mzuzu': 'mw',
    "n'djamena": 'td',
    'narowal': 'pk',
    'nazret': 'et',
    'nek’emte': 'et',
    'new cairo': 'eg',
    'new mirpur city': 'pk',
    'ngaoundere': 'cm',
    'nkongsamba': 'cm',
    'oral': 'kz',
    'oruro': 'bo',
    'pavlodar': 'kz',
    'peshawar': 'pk',
    'petropavl': 'kz',
    'plovdiv': 'bg',
    'port said': 'eg',
    'potosi': 'bo',
    'puerto barrios': 'gt',
    'qalyub': 'eg',
    'qina': 'eg',
    'quetta': 'pk',
    'quillacollo': 'bo',
    'rahim yar khan': 'pk',
    'rawalpindi': 'pk',
    'riga': 'lv',
    'rudnyy': 'kz',
    'ruse': 'bg',
    'sacaba': 'bo',
    'san lorenzo': 'py',
    'santa cruz de la sierra': 'bo',
    'santa lucia cotzumalguapa': 'gt',
    'sargodha': 'pk',
    'sarh': 'td',
    'sebeta': 'et',
    'semey': 'kz',
    'shashamane': 'et',
    'shekhupura': 'pk',
    'shymkent': 'kz',
    'sialkot': 'pk',
    'sinnuris': 'eg',
    'skardu': 'pk',
    'sodo': 'et',
    'sofia': 'bg',
    'sohag': 'eg',
    'stara zagora': 'bg',
    'sucre': 'bo',
    'suez': 'eg',
    'sukkur': 'pk',
    'taldykorgan': 'kz',
    'talkha': 'eg',
    'tando allahyar': 'pk',
    'tanta': 'eg',
    'taraz': 'kz',
    'tarija': 'bo',
    'temirtau': 'kz',
    'toba tek singh': 'pk',
    'totonicapan': 'gt',
    'turkistan': 'kz',
    'tuzla': 'ba',
    'ust-kamenogorsk': 'kz',
    'varna': 'bg',
    'villa canales': 'gt',
    'villa nueva': 'gt',
    'vilnius': 'lt',
    'wah cantt': 'pk',
    'weldiya': 'et',
    'yaounde': 'cm',
    'zagazig': 'eg',
    'zefta': 'eg',
    'zenica': 'ba',
    'zhanaozen': 'kz',
    'zhezqazghan': 'kz',
    'zomba': 'mw',
})  # Stage F wave6

_CYCLONE_BY_REGION_MONTH.update({
    'my': {1: 200, 2: 100, 3: 50, 4: 50, 5: 100, 6: 150, 7: 200, 8: 300, 9: 400, 10: 600, 11: 900, 12: 1100},
    'co': {6: 200, 7: 200, 8: 400, 9: 600, 10: 700, 11: 600},
})  # Stage F wave7
_FLOOD_BY_REGION_MONTH.update({
    'pe': {1: 1800, 2: 2200, 3: 2000, 4: 1200, 5: 400, 6: 100, 7: 100, 8: 100, 9: 200, 10: 500, 11: 900, 12: 1400},
    'my': {1: 1800, 2: 1200, 3: 600, 4: 400, 5: 500, 6: 700, 7: 800, 8: 900, 9: 1000, 10: 1400, 11: 2200, 12: 2500},
    'qa': {1: 600, 2: 700, 3: 800, 10: 400, 11: 500, 12: 600},
    'bh': {1: 500, 2: 400, 3: 300, 4: 500, 5: 200, 11: 400, 12: 600},
    'kw': {1: 200, 2: 300, 3: 400, 4: 300, 10: 200, 11: 300, 12: 250},
    'co': {3: 800, 4: 1800, 5: 2200, 6: 1600, 7: 900, 8: 800, 9: 1400, 10: 2400, 11: 2600, 12: 1200},
})  # Stage F wave7
_DELAY_BY_REGION_MODE.update({
    'pe': {'flight': 35, 'rail': 0, 'bus': 60},
    'my': {'flight': 30, 'rail': 20, 'bus': 35},
    'qa': {'flight': 22, 'rail': 0, 'bus': 18},
    'bh': {'flight': 20, 'rail': 0, 'bus': 15},
    'kw': {'flight': 28, 'rail': 0, 'bus': 35},
    'co': {'flight': 35, 'rail': 0, 'bus': 55},
})  # Stage F wave7
_SEISMIC_RESILIENCE_BY_REGION.update({
    'pe': 22,
    'my': 48,
    'qa': 70,
    'bh': 68,
    'kw': 58,
    'co': 30,
})  # Stage F wave7
_ADVISORY_LEVEL_BY_REGION.update({
    'pe': 2,
    'my': 1,
    'qa': 1,
    'bh': 2,
    'kw': 2,
    'co': 3,
})  # Stage F wave7
_CIVIL_UNREST_BY_REGION.update({
    'pe': 1200,
    'bh': 800,
    'kw': 200,
    'co': 1500,
})  # Stage F wave7
_REGION_SOURCE.update({
    'pe': 'seed:society-region-profile-2026',
    'my': 'seed:society-region-profile-2026',
    'qa': 'seed:society-region-profile-2026',
    'bh': 'seed:travel-risk-domain-2026',
    'kw': 'seed:society-region-profile-2026',
    'co': 'seed:society-region-profile-2026',
})  # Stage F wave7
_CITY_TO_REGION.update({
    'abadan': 'ir',
    'ahar': 'ir',
    'ahvaz': 'ir',
    'ain beida': 'dz',
    'ajman': 'ae',
    'al ahmadi': 'kw',
    'al ain city': 'ae',
    'al majaz': 'ae',
    'al muharraq': 'bh',
    'algiers': 'dz',
    'alor setar': 'my',
    'amol': 'ir',
    'andimeshk': 'ir',
    'andisheh': 'ir',
    'andong': 'kr',
    'angoche': 'mz',
    'annaba': 'dz',
    'ansan-si': 'kr',
    'anyang-si': 'kr',
    'ar rayyan': 'qa',
    'ar rifa‘': 'bh',
    'arak': 'ir',
    'ardabil': 'ir',
    'armenia': 'co',
    'as salimiyah': 'kw',
    'ayacucho': 'pe',
    'azadshahr': 'ir',
    'bab ezzouar': 'dz',
    'babol': 'ir',
    'bandar abbas': 'ir',
    'bandar seri alam': 'my',
    'bandar sunway': 'my',
    'bandar tasik puteri': 'my',
    'bandar-e anzali': 'ir',
    'bandar-e mahshahr': 'ir',
    'baneh': 'ir',
    'baraki': 'dz',
    'barrancabermeja': 'co',
    'barranquilla': 'co',
    'batu pahat': 'my',
    'bawshar': 'om',
    'bayan lepas': 'my',
    'bechar': 'dz',
    'behbahan': 'ir',
    'beira': 'mz',
    'bejaia': 'dz',
    'bercham': 'my',
    'bintulu': 'my',
    'birjand': 'ir',
    'biskra': 'dz',
    'blida': 'dz',
    'bojnurd': 'ir',
    'borazjan': 'ir',
    'bordj bou arreridj': 'dz',
    'bordj el kiffan': 'dz',
    'borujerd': 'ir',
    'bou saada': 'dz',
    'bucaramanga': 'co',
    'bucheon-si': 'kr',
    'bukan': 'ir',
    'bukit mertajam': 'my',
    'busan': 'kr',
    'bushehr': 'ir',
    'business bay': 'ae',
    'butterworth': 'my',
    'cacak': 'rs',
    'cajamarca': 'pe',
    'cartago': 'co',
    'changwon': 'kr',
    'cheonan': 'kr',
    'cheongju-si': 'kr',
    'chia': 'co',
    'chiclayo': 'pe',
    'chililabombwe': 'zm',
    'chimbote': 'pe',
    'chimoio': 'mz',
    'chincha alta': 'pe',
    'chingola': 'zm',
    'chinju': 'kr',
    'chipata': 'zm',
    'chlef': 'dz',
    'chuncheon': 'kr',
    'chungju': 'kr',
    'constantine': 'dz',
    'cucuta': 'co',
    'daegu': 'kr',
    'daejeon': 'kr',
    'dayrah': 'ae',
    'dezful': 'ir',
    'djelfa': 'dz',
    'doha': 'qa',
    'donghae city': 'kr',
    'dorud': 'ir',
    'dosquebradas': 'co',
    'dubai': 'ae',
    'dubai investments park': 'ae',
    'dubai marina': 'ae',
    'el eulma': 'dz',
    'el oued': 'dz',
    'eslamshahr': 'ir',
    'facatativa': 'co',
    'florencia': 'co',
    'floridablanca': 'co',
    'fujairah': 'ae',
    'funza': 'co',
    'gangneung': 'kr',
    'geoje': 'kr',
    'ghardaia': 'dz',
    'gijang': 'kr',
    'gimcheon': 'kr',
    'gimpo-si': 'kr',
    'girardot city': 'co',
    'giron': 'co',
    'gonbad-e kavus': 'ir',
    'gorgan': 'ir',
    'goyang-si': 'kr',
    'guadalajara de buga': 'co',
    'guelma': 'dz',
    'gumi': 'kr',
    'gunpo': 'kr',
    'gunsan': 'kr',
    'guri-si': 'kr',
    'gurue': 'mz',
    'gwangju': 'kr',
    'gwangmyeong': 'kr',
    'gwangyang': 'kr',
    'gyeongju': 'kr',
    'gyeongsan-si': 'kr',
    'hamadan': 'ir',
    'hanam': 'kr',
    'hawalli': 'kw',
    'huancayo': 'pe',
    'huanuco': 'pe',
    'hwado': 'kr',
    'hwaseong-si': 'kr',
    'ibague': 'co',
    'ica': 'pe',
    'icheon-si': 'kr',
    'ijok': 'my',
    'iksan': 'kr',
    'ilam': 'ir',
    'incheon': 'kr',
    'international city': 'ae',
    'ipoh': 'my',
    'iquitos': 'pe',
    'iranshahr': 'ir',
    'iskandar puteri': 'my',
    'jahrom': 'ir',
    'jebel ali': 'ae',
    'jeju city': 'kr',
    'jeongeup': 'kr',
    'jeonju': 'kr',
    'jijel': 'dz',
    'jiroft': 'ir',
    'johor bahru': 'my',
    'juliaca': 'pe',
    'kabwe': 'zm',
    'kafue': 'zm',
    'kajang': 'my',
    'kamalshahr': 'ir',
    'kampong baharu cheras batu sebelas': 'my',
    'kampung baru subang': 'my',
    'kampung kangkar teberau': 'my',
    'kampung larkin lama': 'my',
    'kampung sungai ara': 'my',
    'kampung sungai glugur': 'my',
    'kapar': 'my',
    'karaj': 'ir',
    'kashan': 'ir',
    'kashmar': 'ir',
    'kennedy': 'co',
    'kerman': 'ir',
    'kermanshah': 'ir',
    'khenchela': 'dz',
    'khomeyni shahr': 'ir',
    'khorramabad': 'ir',
    'khorramshahr': 'ir',
    'khuy': 'ir',
    'kimhae': 'kr',
    'kitwe': 'zm',
    'klang': 'my',
    'kluang': 'my',
    'kota bharu': 'my',
    'kota damansara': 'my',
    'kota kinabalu': 'my',
    'kragujevac': 'rs',
    'kuala krai': 'my',
    'kuala kubu baharu': 'my',
    'kuala terengganu': 'my',
    'kuantan': 'my',
    'kuching': 'my',
    'kulim': 'my',
    'laghouat': 'dz',
    'lahad datu': 'my',
    'lahijan': 'ir',
    'lichinga': 'mz',
    'ljubljana': 'si',
    'lusail': 'qa',
    "m'sila": 'dz',
    'madinat hamad': 'bh',
    'magangue': 'co',
    'mahabad': 'ir',
    'maicao': 'co',
    'malacca': 'my',
    'malambo': 'co',
    'malayer': 'ir',
    'manama': 'bh',
    'mandimba': 'mz',
    'manizales': 'co',
    'mansa': 'zm',
    'maputo': 'mz',
    'maragheh': 'ir',
    'maran': 'my',
    'marand': 'ir',
    'marivan': 'ir',
    'marvdasht': 'ir',
    'masai': 'my',
    'masan': 'kr',
    'mascara': 'dz',
    'mashhad': 'ir',
    'masjed soleyman': 'ir',
    'matola': 'mz',
    'medea': 'dz',
    'miandoab': 'ir',
    'mianeh': 'ir',
    'miri': 'my',
    'mohammad shahr': 'ir',
    'mokpo': 'kr',
    'mongu': 'zm',
    'monteria': 'co',
    'mosquera': 'co',
    'mostaganem': 'dz',
    'muar': 'my',
    'mukim pulai': 'my',
    'musaffah': 'ae',
    'muscat': 'om',
    'nacala': 'mz',
    'najafabad': 'ir',
    'ndola': 'zm',
    'neyshabur': 'ir',
    'nis': 'rs',
    'novi sad': 'rs',
    'ocana': 'co',
    'oran': 'dz',
    'orumiyeh': 'ir',
    'osan': 'kr',
    'ouargla': 'dz',
    'palmira': 'co',
    'pasir gudang': 'my',
    'pasir puteh': 'my',
    'pasto': 'co',
    'paya terubong': 'my',
    'pelentong': 'my',
    'pemba': 'mz',
    'pereira': 'co',
    'perling': 'my',
    'piedecuesta': 'co',
    'piranshahr': 'ir',
    'pitalito': 'co',
    'piura': 'pe',
    'pohang': 'kr',
    'popayan': 'co',
    'port dickson': 'my',
    'pucallpa': 'pe',
    'puchong': 'my',
    'pyeongtaek': 'kr',
    'qarchak': 'ir',
    'qazvin': 'ir',
    'qa’em shahr': 'ir',
    'qods': 'ir',
    'qom': 'ir',
    'quchan': 'ir',
    'quelimane': 'mz',
    'quibdo': 'co',
    'rafsanjan': 'ir',
    'ras al khaimah': 'ae',
    'rasht': 'ir',
    'rawang': 'my',
    'relizane': 'dz',
    'riohacha': 'co',
    'rionegro': 'co',
    'robat karim': 'ir',
    'rouiba': 'dz',
    'rustaq': 'om',
    'sabah as salim': 'kw',
    'sabzevar': 'ir',
    'saida': 'dz',
    'salalah': 'om',
    'sanandaj': 'ir',
    'sandakan': 'my',
    'sangju': 'kr',
    'saqqez': 'ir',
    'sari': 'ir',
    'saveh': 'ir',
    'seeb': 'om',
    'sejong': 'kr',
    'selayang baru utara': 'my',
    'semnan': 'ir',
    'seogwipo': 'kr',
    'seongnam-si': 'kr',
    'sepang': 'my',
    'seremban': 'my',
    'seri kembangan': 'my',
    'seri manjung': 'my',
    'setia alam': 'my',
    'setif': 'dz',
    'shah alam': 'my',
    'shahin shahr': 'ir',
    'shahr-e kord': 'ir',
    'shahr-e sadra': 'ir',
    'shahreza': 'ir',
    'shahriar': 'ir',
    'shahrud': 'ir',
    'sharjah': 'ae',
    'shushtar': 'ir',
    'sibu': 'my',
    'sidi bel abbes': 'dz',
    'siheungdong': 'kr',
    'sincelejo': 'co',
    'sirjan': 'ir',
    'sitiawan': 'my',
    'skikda': 'dz',
    'skudai': 'my',
    'soacha': 'co',
    'sogamoso': 'co',
    'sohar': 'om',
    'soledad': 'co',
    'solwezi': 'zm',
    'souk ahras': 'dz',
    'subang jaya': 'my',
    'subotica': 'rs',
    'sullana': 'pe',
    'suncheon': 'kr',
    'sungai buloh': 'my',
    'sungai petani': 'my',
    'suwon': 'kr',
    'tabriz': 'ir',
    'tacna': 'pe',
    'taiping': 'my',
    'tasek glugor': 'my',
    'tawau': 'my',
    'tebessa': 'dz',
    'tehran': 'ir',
    'teluk intan': 'my',
    'tete': 'mz',
    'tiaret': 'dz',
    'tizi ouzou': 'dz',
    'tlemcen': 'dz',
    'torbat-e heydariyeh': 'ir',
    'touggourt': 'dz',
    'trujillo': 'pe',
    'tulua': 'co',
    'tunja': 'co',
    'uijeongbu-si': 'kr',
    'ulsan': 'kr',
    'valledupar': 'co',
    'varamin': 'ir',
    'villavicencio': 'co',
    'warisan': 'ae',
    'wonju': 'kr',
    'xai-xai': 'mz',
    'yangju': 'kr',
    'yangsan': 'kr',
    'yeoju': 'kr',
    'yeosu': 'kr',
    'yopal': 'co',
    'zabol': 'ir',
    'zahedan': 'ir',
    'zanjan': 'ir',
    'zipaquira': 'co',
    '‘ibri': 'om',
})  # Stage F wave7

_CYCLONE_BY_REGION_MONTH.update({
    'th': {1: 50, 2: 20, 3: 20, 4: 30, 5: 100, 6: 150, 7: 200, 8: 300, 9: 600, 10: 1800, 11: 2200, 12: 800},
    'bd': {1: 0, 2: 0, 3: 50, 4: 300, 5: 2800, 6: 800, 7: 200, 8: 200, 9: 600, 10: 2200, 11: 3500, 12: 200},
    'ph': {1: 100, 2: 50, 3: 50, 4: 100, 5: 300, 6: 800, 7: 2500, 8: 3500, 9: 3000, 10: 2500, 11: 2800, 12: 1200},
})  # Stage F wave8
_FLOOD_BY_REGION_MONTH.update({
    'th': {1: 100, 2: 50, 3: 50, 4: 100, 5: 400, 6: 800, 7: 1200, 8: 1800, 9: 2500, 10: 3000, 11: 2000, 12: 500},
    'cl': {5: 800, 6: 1600, 7: 1800, 8: 1600, 9: 900},
    'by': {3: 800, 4: 1600, 5: 1200, 6: 600},
    'bd': {1: 0, 2: 0, 3: 50, 4: 100, 5: 400, 6: 2500, 7: 4500, 8: 4800, 9: 3500, 10: 1800, 11: 300, 12: 0},
    'ar': {1: 1800, 2: 2000, 3: 1600, 4: 900, 5: 500, 6: 500, 10: 400, 11: 700, 12: 1200},
    'ph': {1: 300, 2: 400, 3: 200, 5: 800, 6: 2000, 7: 2500, 8: 2800, 9: 2200, 10: 2000, 11: 1800, 12: 1000},
})  # Stage F wave8
_DELAY_BY_REGION_MODE.update({
    'th': {'flight': 35, 'rail': 30, 'bus': 50},
    'cl': {'flight': 35, 'rail': 20, 'bus': 50},
    'by': {'flight': 28, 'rail': 22, 'bus': 35},
    'bd': {'flight': 55, 'rail': 65, 'bus': 75},
    'ar': {'flight': 35, 'rail': 25, 'bus': 30},
    'ph': {'flight': 55, 'rail': 0, 'bus': 60},
})  # Stage F wave8
_SEISMIC_RESILIENCE_BY_REGION.update({
    'th': 44,
    'cl': 30,
    'by': 78,
    'bd': 32,
    'ar': 40,
    'ph': 18,
})  # Stage F wave8
_ADVISORY_LEVEL_BY_REGION.update({
    'th': 2,
    'cl': 2,
    'by': 4,
    'bd': 2,
    'ar': 2,
    'ph': 2,
})  # Stage F wave8
_CIVIL_UNREST_BY_REGION.update({
    'th': 400,
    'cl': 500,
    'by': 1800,
    'bd': 1700,
    'ar': 600,
    'ph': 800,
})  # Stage F wave8
_REGION_SOURCE.update({
    'th': 'seed:travel-risk-domain-expert-2026',
    'cl': 'seed:society-region-profile-2026',
    'by': 'seed:society-region-profile-2026',
    'bd': 'seed:bd-hazard-profile-2026',
    'ar': 'seed:society-region-profile-2026',
    'ph': 'seed:society-region-profile-2026',
})  # Stage F wave8
_CITY_TO_REGION.update({
    'aalborg': 'dk',
    'alexandra': 'za',
    'alto hospicio': 'cl',
    'angeles city': 'ph',
    'angono': 'ph',
    'antananarivo': 'mg',
    'antipolo': 'ph',
    'antofagasta': 'cl',
    'antsirabe': 'mg',
    'antsiranana': 'mg',
    'apalit': 'ph',
    'arad': 'ro',
    'arhus': 'dk',
    'arica': 'cl',
    'ashuganj city': 'bd',
    'bacau': 'ro',
    'bacolod city': 'ph',
    'bacoor': 'ph',
    'bade': 'tw',
    'bagerhat': 'bd',
    'bagong silang': 'ph',
    'baguio': 'ph',
    'bahia blanca': 'ar',
    'baia mare': 'ro',
    'baliuag': 'ph',
    'ban khlong prawet': 'th',
    'ban samae dam': 'th',
    'bandarban': 'bd',
    'bang kapi': 'th',
    'bang khae': 'th',
    'bang khun thian': 'th',
    'banqiao': 'tw',
    'baranovichi': 'by',
    'barishal': 'bd',
    'barysaw': 'by',
    'basel': 'ch',
    'batangas': 'ph',
    'bayambang': 'ph',
    'bayawan': 'ph',
    'bayugan': 'ph',
    'benoni': 'za',
    'bern': 'ch',
    'bhairab bazar': 'bd',
    'bhatara': 'bd',
    'binan': 'ph',
    'binangonan': 'ph',
    'bloemfontein': 'za',
    'bobruysk': 'by',
    'bogra': 'bd',
    'boksburg': 'za',
    'braila': 'ro',
    'brakpan': 'za',
    'bratislava': 'sk',
    'brest': 'by',
    'bueng kum': 'th',
    'bulaon': 'ph',
    'bunamwaya': 'ug',
    'butuan': 'ph',
    'buzau': 'ro',
    'cabanatuan city': 'ph',
    'cadiz': 'ph',
    'cagayan de oro': 'ph',
    'cainta': 'ph',
    'calama': 'cl',
    'calamba': 'ph',
    'calasiao': 'ph',
    'calumpit': 'ph',
    'candelaria': 'ph',
    'capas': 'ph',
    'carmona': 'ph',
    'castelar': 'ar',
    'catamarca': 'ar',
    'catbalogan': 'ph',
    'cavite city': 'ph',
    'cebu city': 'ph',
    'centurion': 'za',
    'chang-hua': 'tw',
    'changhua': 'tw',
    'chattogram': 'bd',
    'chiang mai': 'th',
    'chiayi city': 'tw',
    'chillan': 'cl',
    'chon buri': 'th',
    'cluj-napoca': 'ro',
    'colina': 'cl',
    'comilla': 'bd',
    'commonwealth': 'ph',
    'comodoro rivadavia': 'ar',
    'concordia': 'ar',
    'constanta': 'ro',
    'copenhagen': 'dk',
    'copiapo': 'cl',
    'coquimbo': 'cl',
    'coronel': 'cl',
    'cotabato': 'ph',
    'cox’s bazar': 'bd',
    'craiova': 'ro',
    'curico': 'cl',
    'dagupan': 'ph',
    'dasmarinas': 'ph',
    'davao': 'ph',
    'dhaka': 'bd',
    'diepsloot': 'za',
    'digos': 'ph',
    'dinaig': 'ph',
    'dinajpur': 'bd',
    'douliu': 'tw',
    'dumaguete': 'ph',
    'durban': 'za',
    'east london': 'za',
    'eldoret': 'ke',
    'emalahleni': 'za',
    'ezeiza': 'ar',
    'faridpur': 'bd',
    'fengshan': 'tw',
    'fianarantsoa': 'mg',
    'formosa': 'ar',
    'galati': 'ro',
    'gapan': 'ph',
    'garissa': 'ke',
    'gazipur': 'bd',
    'general santos': 'ph',
    'george': 'za',
    'germiston': 'za',
    'gingoog': 'ph',
    'gqeberha': 'za',
    'guiguinto': 'ph',
    'gulu': 'ug',
    'guyong': 'ph',
    'hagonoy': 'ph',
    'hammanskraal': 'za',
    'hat yai': 'th',
    'hathazari': 'bd',
    'hoima': 'ug',
    "homyel'": 'by',
    'hrodna': 'by',
    'hsinchu': 'tw',
    'hua hin': 'th',
    'iasi': 'ro',
    'ibanda': 'ug',
    'ilagan': 'ph',
    'iligan': 'ph',
    'iligan city': 'ph',
    'iloilo': 'ph',
    'imus': 'ph',
    'iquique': 'cl',
    'iriga city': 'ph',
    'isulan': 'ph',
    'ituzaingo': 'ar',
    'ivory park': 'za',
    'jamalpur': 'bd',
    'jessore': 'bd',
    'jomvu': 'ke',
    'jose c. paz': 'ar',
    'juja': 'ke',
    'kabankalan': 'ph',
    'kabin buri': 'th',
    'kafrul': 'bd',
    'kajansi': 'ug',
    'kakamega': 'ke',
    'kaohsiung': 'tw',
    'kariega': 'za',
    'karuri': 'ke',
    'kasangati': 'ug',
    'kasese': 'ug',
    'katabi': 'ug',
    'keelung': 'tw',
    'khlong luang': 'th',
    'khon kaen': 'th',
    'khulna': 'bd',
    'khwisero': 'ke',
    'kiambu': 'ke',
    'kikuyu': 'ke',
    'kimberley': 'za',
    'kira': 'ug',
    'kisii': 'ke',
    'kisumu': 'ke',
    'kitale': 'ke',
    'kitengela': 'ke',
    'klerksdorp': 'za',
    'koronadal': 'ph',
    'kosice': 'sk',
    'krugersdorp': 'za',
    'kushtia': 'bd',
    'kwadukuza': 'za',
    'kyengera': 'ug',
    'la rioja': 'ar',
    'la serena': 'cl',
    'la trinidad': 'ph',
    'ladysmith': 'za',
    'lak si': 'th',
    'lampang': 'th',
    "lang'ata": 'ke',
    'laoag': 'ph',
    'lapu-lapu city': 'ph',
    'las pinas': 'ph',
    'lat krabang': 'th',
    'lat phrao': 'th',
    'latkrabang': 'th',
    'lausanne': 'ch',
    'legaspi': 'ph',
    'libertad': 'ph',
    'lida': 'by',
    'limuru': 'ke',
    'lipa city': 'ph',
    'lira': 'ug',
    'lomas de zamora': 'ar',
    'los banos': 'ph',
    'lucena': 'ph',
    'lugazi': 'ug',
    'mabalacat city': 'ph',
    'mabopane': 'za',
    'magugpo poblacion': 'ph',
    'mahajanga': 'mg',
    'mahilyow': 'by',
    'maijdi': 'bd',
    'maipu': 'cl',
    'malindi': 'ke',
    'malita': 'ph',
    'malolos': 'ph',
    'mandaue city': 'ph',
    'mandera': 'ke',
    'manolo fortich': 'ph',
    'mansilingan': 'ph',
    'mantampay': 'ph',
    'mar del plata': 'ar',
    'maramag': 'ph',
    'marawi city': 'ph',
    'marikina city': 'ph',
    'mariveles': 'ph',
    'masaka': 'ug',
    'masindi': 'ug',
    'mati': 'ph',
    'matuga': 'ke',
    'mazyr': 'by',
    'mbale': 'ug',
    'mbarara': 'ug',
    'mbombela': 'za',
    'mdantsane': 'za',
    'merlo': 'ar',
    'meycauayan': 'ph',
    'midsayap': 'ph',
    'minsk': 'by',
    'mirpur model thana': 'bd',
    'mityana': 'ug',
    'mlolongo': 'ke',
    'mokopane': 'za',
    'motijheel': 'bd',
    'mpumalanga': 'za',
    'mthatha': 'za',
    'mubende': 'ug',
    'mukono': 'ug',
    'muntinlupa': 'ph',
    'muricay': 'ph',
    'mymensingh': 'bd',
    'naga': 'ph',
    'nagar naluakot': 'bd',
    'naivasha': 'ke',
    'nakhon pathom': 'th',
    'nakhon ratchasima': 'th',
    'nakhon si thammarat': 'th',
    'nakuru': 'ke',
    'nansana': 'ug',
    'narayanganj': 'bd',
    'natore': 'bd',
    'nawabganj': 'bd',
    'neihu': 'tw',
    'neili': 'tw',
    'neuquen': 'ar',
    'new taipei city': 'tw',
    'newcastle': 'za',
    'ngong': 'ke',
    'nia valencia': 'ph',
    'njeru': 'ug',
    'nong khaem': 'th',
    'ntuzuma': 'za',
    'odense': 'dk',
    'olongapo': 'ph',
    'ongata rongai': 'ke',
    'oradea': 'ro',
    'ormoc': 'ph',
    'orsha': 'by',
    'osorno': 'cl',
    'paarl': 'za',
    'pabna': 'bd',
    'pagadian': 'ph',
    'pak kret': 'th',
    'pallabi': 'bd',
    'paltan': 'bd',
    'panalanoy': 'ph',
    'paniqui': 'ph',
    'par naogaon': 'bd',
    'parana': 'ar',
    'paranaque city': 'ph',
    'pattaya': 'th',
    'petrzalka': 'sk',
    'phalaborwa': 'za',
    'piatra neamt': 'ro',
    'pietermaritzburg': 'za',
    'pinetown': 'za',
    'pinsk': 'by',
    'pitesti': 'ro',
    'plaridel': 'ph',
    'ploiesti': 'ro',
    'poblacion': 'ph',
    'polokwane': 'za',
    'posadas': 'ar',
    'potchefstroom': 'za',
    'pretoria': 'za',
    'puente alto': 'cl',
    'puerto montt': 'cl',
    'puerto princesa': 'ph',
    'pulilan': 'ph',
    'pulong santa cruz': 'ph',
    'punta arenas': 'cl',
    'putatan': 'ph',
    'puthia': 'bd',
    'quezon': 'ph',
    'quilmes': 'ar',
    'quilpue': 'cl',
    'rajshahi': 'bd',
    'ramna maidan': 'bd',
    'rancagua': 'cl',
    'randburg': 'za',
    'rangamati': 'bd',
    'rangpur': 'bd',
    'rayong': 'th',
    'resistencia': 'ar',
    'richards bay': 'za',
    'rio cuarto': 'ar',
    'roodepoort': 'za',
    'roxas city': 'ph',
    'ruiru': 'ke',
    'rustenburg': 'za',
    'sai mai': 'th',
    'saidpur': 'bd',
    'salta': 'ar',
    'samut prakan': 'th',
    'san fernando': 'ph',
    'san jose del monte': 'ph',
    'san justo': 'ar',
    'san luis': 'ar',
    'san mateo': 'ph',
    'san miguel de tucuman': 'ar',
    'san nicolas de los arroyos': 'ar',
    'san pablo': 'ph',
    'san pedro de la paz': 'cl',
    'san rafael': 'ar',
    'san salvador de jujuy': 'ar',
    'santa fe': 'ar',
    'santa rosa': 'ph',
    'santiago del estero': 'ar',
    'santol': 'ph',
    'sanxia': 'tw',
    'satkhira': 'bd',
    'satu mare': 'ro',
    'savar': 'bd',
    'sherpur': 'bd',
    'shibganj': 'bd',
    'shulin': 'tw',
    'si maha phot': 'th',
    'si racha': 'th',
    'sibiu': 'ro',
    'silang': 'ph',
    'sirajganj': 'bd',
    'somerset west': 'za',
    'sorsogon': 'ph',
    'soweto': 'za',
    'springs': 'za',
    'suan luang': 'th',
    'sylhet': 'bd',
    'tabuk': 'ph',
    'tacloban': 'ph',
    'tacurong': 'ph',
    'taguig': 'ph',
    'taichung': 'tw',
    'tainan': 'tw',
    'taipei': 'tw',
    'taitung': 'tw',
    'talca': 'cl',
    'talcahuano': 'cl',
    'talisay': 'ph',
    'tandil': 'ar',
    'tangail': 'bd',
    'taoyuan': 'tw',
    'targu mures': 'ro',
    'tarlac city': 'ph',
    'tayabas': 'ph',
    'taytay': 'ph',
    'temuco': 'cl',
    'thembisa': 'za',
    'thika': 'ke',
    'thohoyandou': 'za',
    'thung khru': 'th',
    'timisoara': 'ro',
    'toamasina': 'mg',
    'tokoza': 'za',
    'toledo': 'ph',
    'toliara': 'mg',
    'toufen': 'tw',
    'tuguegarao': 'ph',
    'tungi': 'bd',
    'ubon ratchathani': 'th',
    'udon thani': 'th',
    'urdaneta': 'ph',
    'valdivia': 'cl',
    'vanderbijlpark': 'za',
    'vereeniging': 'za',
    'villa lugano': 'ar',
    'vitebsk': 'by',
    'vryheid': 'za',
    'wang thonglang': 'th',
    'welkom': 'za',
    'winterthur': 'ch',
    'worcester': 'za',
    'xizhi': 'tw',
    'yongkang': 'tw',
    'yuanlin': 'tw',
    'zamboanga': 'ph',
    'zhubei': 'tw',
    'zurich': 'ch',
})  # Stage F wave8

_CYCLONE_BY_REGION_MONTH.update({
    'mx': {5: 200, 6: 800, 7: 1200, 8: 2500, 9: 3500, 10: 2000, 11: 600},
})  # Stage F wave9
_FLOOD_BY_REGION_MONTH.update({
    'mx': {5: 500, 6: 1200, 7: 2000, 8: 2200, 9: 2500, 10: 1500, 11: 400},
})  # Stage F wave9
_DELAY_BY_REGION_MODE.update({
    'mx': {'flight': 38, 'rail': 15, 'bus': 55},
})  # Stage F wave9
_SEISMIC_RESILIENCE_BY_REGION.update({
    'mx': 28,
})  # Stage F wave9
_ADVISORY_LEVEL_BY_REGION.update({
    'mx': 2,
})  # Stage F wave9
_CIVIL_UNREST_BY_REGION.update({
    'mx': 1200,
})  # Stage F wave9
_REGION_SOURCE.update({
    'mx': 'seed:society-region-profile-2026',
})  # Stage F wave9
_CITY_TO_REGION.update({
    'acapulco de juarez': 'mx',
    'adana': 'tr',
    'adapazari': 'tr',
    'adiyaman': 'tr',
    'afyonkarahisar': 'tr',
    'agri': 'tr',
    'aguascalientes': 'mx',
    'aksaray': 'tr',
    'alanya': 'tr',
    'amasya': 'tr',
    'antakya': 'tr',
    'apatzingan': 'mx',
    'arnavutkoy': 'tr',
    'arsuz': 'tr',
    'atasehir': 'tr',
    'aydin': 'tr',
    'balikesir': 'tr',
    'bandirma': 'tr',
    'basaksehir': 'tr',
    'batman': 'tr',
    'beylikduzu': 'tr',
    'bingol': 'tr',
    'bolu': 'tr',
    'buenavista': 'mx',
    'bursa': 'tr',
    'buyukcekmece': 'tr',
    'campeche': 'mx',
    'celaya': 'mx',
    'chetumal': 'mx',
    'chicoloapan': 'mx',
    'chihuahua': 'mx',
    'chilpancingo': 'mx',
    'cholula': 'mx',
    'cigli': 'tr',
    'ciudad acuna': 'mx',
    'ciudad apodaca': 'mx',
    'ciudad benito juarez': 'mx',
    'ciudad de villa de alvarez': 'mx',
    'ciudad del carmen': 'mx',
    'ciudad delicias': 'mx',
    'ciudad guzman': 'mx',
    'ciudad juarez': 'mx',
    'ciudad lazaro cardenas': 'mx',
    'ciudad lopez mateos': 'mx',
    'ciudad madero': 'mx',
    'ciudad nezahualcoyotl': 'mx',
    'ciudad obregon': 'mx',
    'ciudad valles': 'mx',
    'ciudad victoria': 'mx',
    'cizre': 'tr',
    'coatzacoalcos': 'mx',
    'colima': 'mx',
    'comitan': 'mx',
    'corlu': 'tr',
    'corum': 'tr',
    'cuautitlan': 'mx',
    'cuautitlan izcalli': 'mx',
    'cuautla': 'mx',
    'cuernavaca': 'mx',
    'culiacan': 'mx',
    'delegacion cuajimalpa de morelos': 'mx',
    'denizli': 'tr',
    'derince': 'tr',
    'diyarbakir': 'tr',
    'duzce': 'tr',
    'ecatepec de morelos': 'mx',
    'edirne': 'tr',
    'elazig': 'tr',
    'ensenada': 'mx',
    'erzincan': 'tr',
    'erzurum': 'tr',
    'esenyurt': 'tr',
    'eskisehir': 'tr',
    'fresnillo': 'mx',
    'gaziantep': 'tr',
    'gebze': 'tr',
    'giresun': 'tr',
    'golbasi': 'tr',
    'gomez palacio': 'mx',
    'guadalajara': 'mx',
    'guadalupe': 'mx',
    'hermosillo': 'mx',
    'heroica guaymas': 'mx',
    'heroica matamoros': 'mx',
    'hidalgo del parral': 'mx',
    'igdir': 'tr',
    'iguala de la independencia': 'mx',
    'inegol': 'tr',
    'irapuato': 'mx',
    'iskenderun': 'tr',
    'isparta': 'tr',
    'ixtapaluca': 'mx',
    'izmir': 'tr',
    'izmit': 'tr',
    'jiutepec': 'mx',
    'kahramanmaras': 'tr',
    'karabaglar': 'tr',
    'karabuk': 'tr',
    'karaman': 'tr',
    'karsiyaka': 'tr',
    'kastamonu': 'tr',
    'kayseri': 'tr',
    'kilis': 'tr',
    'kirikkale': 'tr',
    'kirsehir': 'tr',
    'kiziltepe': 'tr',
    'konak': 'tr',
    'konya': 'tr',
    'kucukcekmece': 'tr',
    'kutahya': 'tr',
    'leon de los aldama': 'mx',
    'los mochis': 'mx',
    'magdalena contreras': 'mx',
    'malatya': 'tr',
    'maltepe': 'tr',
    'manisa': 'tr',
    'mardin': 'tr',
    'merkezefendi': 'tr',
    'mersin': 'tr',
    'mexicali': 'mx',
    'minatitlan': 'mx',
    'miramar': 'mx',
    'monclova': 'mx',
    'monterrey': 'mx',
    'morelia': 'mx',
    'naucalpan de juarez': 'mx',
    'navojoa': 'mx',
    'nazilli': 'tr',
    'nilufer': 'tr',
    'nogales': 'mx',
    'nuevo laredo': 'mx',
    'oaxaca': 'mx',
    'ojo de agua': 'mx',
    'ordu': 'tr',
    'orizaba': 'mx',
    'osmaniye': 'tr',
    'pachuca de soto': 'mx',
    'piedras negras': 'mx',
    'poza rica de hidalgo': 'mx',
    'puebla': 'mx',
    'reynosa': 'mx',
    'rize': 'tr',
    'rosarito': 'mx',
    'salihli': 'tr',
    'saltillo': 'mx',
    'samandag': 'tr',
    'samsun': 'tr',
    'san cristobal de las casas': 'mx',
    'san jose del cabo': 'mx',
    'san juan del rio': 'mx',
    'san luis potosi': 'mx',
    'san luis rio colorado': 'mx',
    'san martin texmelucan de labastida': 'mx',
    'san miguel de allende': 'mx',
    'san nicolas de los garza': 'mx',
    'san pedro garza garcia': 'mx',
    'sancaktepe': 'tr',
    'sanliurfa': 'tr',
    'santa catarina': 'mx',
    'santiago de queretaro': 'mx',
    'siirt': 'tr',
    'silifke': 'tr',
    'silopi': 'tr',
    'sivas': 'tr',
    'siverek': 'tr',
    'soledad de graciano sanchez': 'mx',
    'sultanbeyli': 'tr',
    'sultangazi': 'tr',
    'tampico': 'mx',
    'tapachula': 'mx',
    'tarsus': 'tr',
    'tehuacan': 'mx',
    'tekirdag': 'tr',
    'tepexpan': 'mx',
    'tepic': 'mx',
    'texcoco de mora': 'mx',
    'teziutlan': 'mx',
    'tijuana': 'mx',
    'tlalnepantla': 'mx',
    'tlalpan': 'mx',
    'tlaquepaque': 'mx',
    'tokat': 'tr',
    'toluca': 'mx',
    'tonala': 'mx',
    'torreon': 'mx',
    'trabzon': 'tr',
    'tulancingo': 'mx',
    'turgutlu': 'tr',
    'turhal': 'tr',
    'tuxtepec': 'mx',
    'tuxtla': 'mx',
    'umraniye': 'tr',
    'uruapan': 'mx',
    'usak': 'tr',
    'van': 'tr',
    'veracruz': 'mx',
    'victoria de durango': 'mx',
    'villahermosa': 'mx',
    'viransehir': 'tr',
    'xalapa de enriquez': 'mx',
    'xico': 'mx',
    'xochimilco': 'mx',
    'yautepec': 'mx',
    'zacatecas': 'mx',
    'zamora de hidalgo': 'mx',
    'zapopan': 'mx',
    'zonguldak': 'tr',
    'zumpango': 'mx',
})  # Stage F wave9

_CYCLONE_BY_REGION_MONTH.update({
    'au': {1: 2800, 2: 3200, 3: 2400, 4: 1200, 11: 600, 12: 1400},
    'id': {1: 800, 2: 900, 3: 700, 4: 300, 5: 150, 6: 250, 7: 350, 8: 350, 9: 300, 10: 300, 11: 600, 12: 900},
})  # Stage F giants1
_FLOOD_BY_REGION_MONTH.update({
    'gb': {1: 1300, 2: 1100, 3: 500, 10: 400, 11: 700, 12: 1200},
    'es': {1: 900, 2: 800, 3: 700, 9: 1200, 10: 1800, 11: 1600, 12: 900},
    'au': {1: 2200, 2: 2500, 3: 1800, 4: 1200, 5: 700, 6: 800, 7: 700, 8: 500, 12: 1000},
    'id': {1: 2800, 2: 3000, 3: 2200, 4: 1200, 5: 700, 6: 400, 7: 300, 8: 300, 9: 500, 10: 900, 11: 1800, 12: 2600},
})  # Stage F giants1
_DELAY_BY_REGION_MODE.update({
    'gb': {'flight': 28, 'rail': 18, 'bus': 12},
    'es': {'flight': 28, 'rail': 18, 'bus': 22},
    'au': {'flight': 28, 'rail': 22, 'bus': 18},
    'id': {'flight': 55, 'rail': 40, 'bus': 70},
})  # Stage F giants1
_SEISMIC_RESILIENCE_BY_REGION.update({
    'gb': 82,
    'es': 46,
    'au': 72,
    'id': 18,
})  # Stage F giants1
_ADVISORY_LEVEL_BY_REGION.update({
    'gb': 1,
    'es': 2,
    'au': 1,
    'id': 2,
})  # Stage F giants1
_CIVIL_UNREST_BY_REGION.update({
    'gb': 120,
    'es': 250,
    'au': 60,
    'id': 650,
})  # Stage F giants1
_REGION_SOURCE.update({
    'gb': 'seed:society-region-profile-2026',
    'es': 'seed:society-region-profile-2026',
    'au': 'seed:society-region-profile-2026',
    'id': 'seed:society-region-profile-2026',
})  # Stage F giants1
_CITY_TO_REGION.update({
    'a coruna': 'es',
    'aberdeen': 'gb',
    'abiko': 'jp',
    'adachi': 'jp',
    'adelaide': 'au-vic',
    'ageo': 'jp',
    'aihara': 'jp',
    'aizu-wakamatsu': 'jp',
    'akashi': 'jp',
    'akishima': 'jp',
    'akita': 'jp',
    'albacete': 'es',
    'alcala de henares': 'es',
    'alcobendas': 'es',
    'alcorcon': 'es',
    'algeciras': 'es',
    'almeria': 'es',
    'ambon': 'id',
    'anjo': 'jp',
    'asahikawa': 'jp',
    'asaka': 'jp',
    'ashikaga': 'jp',
    'atsugi': 'jp',
    'badajoz': 'es',
    'balikpapan': 'id',
    'ballarat': 'au-vic',
    'banda aceh': 'id',
    'bandar lampung': 'id',
    'bandung': 'id',
    'banjar': 'id',
    'banjaran': 'id',
    'banjarbaru': 'id',
    'banjarmasin': 'id',
    'banyuwangi': 'id',
    'barakaldo': 'es',
    'barking': 'gb',
    'basildon': 'gb',
    'basingstoke': 'gb',
    'batam': 'id',
    'batu': 'id',
    'baubau': 'id',
    'becontree': 'gb',
    'bedford': 'gb',
    'bekasi': 'id',
    'belawan': 'id',
    'bendigo': 'au-vic',
    'bengkulu': 'id',
    'beppu': 'jp',
    'bexley': 'gb',
    'bilbao': 'es',
    'bima': 'id',
    'binjai': 'id',
    'birmingham': 'gb',
    'bitung': 'id',
    'blackburn': 'gb',
    'blackpool': 'gb',
    'blitar': 'id',
    'bogor': 'id',
    'bolton': 'gb',
    'bontang': 'id',
    'bradford': 'gb',
    'brent': 'gb',
    'brisbane': 'au',
    'bukittinggi': 'id',
    'bunkyo': 'jp',
    'burgos': 'es',
    'burnley': 'gb',
    'burton upon trent': 'gb',
    'canberra': 'au',
    'castello de la plana': 'es',
    'central coast': 'au',
    'chelmsford': 'gb',
    'cheltenham': 'gb',
    'chesterfield': 'gb',
    'chiba': 'jp',
    'chigasaki': 'jp',
    'chikusei': 'jp',
    'chikushino-shi': 'jp',
    'chofu': 'jp',
    'ciamis': 'id',
    'ciampea': 'id',
    'cianjur': 'id',
    'cibinong': 'id',
    'cikampek': 'id',
    'cikarang': 'id',
    'cikupa': 'id',
    'cilacap': 'id',
    'cilegon': 'id',
    'cileungsir': 'id',
    'cileunyi': 'id',
    'cimahi': 'id',
    'ciputat': 'id',
    'cirebon': 'id',
    'city of port phillip': 'au',
    'colchester': 'gb',
    'coventry': 'gb',
    'crawley': 'gb',
    'croydon': 'gb',
    'curug': 'id',
    'dagenham': 'gb',
    'delicias': 'es',
    'depok': 'id',
    'derby': 'gb',
    'doncaster': 'gb',
    'donostia / san sebastian': 'es',
    'dos hermanas': 'es',
    'dudley': 'gb',
    'dumai': 'id',
    'ebetsu': 'jp',
    'edogawe': 'jp',
    'elche': 'es',
    'enfield town': 'gb',
    'exeter': 'gb',
    'fuchu': 'jp',
    'fuenlabrada': 'es',
    'fuji': 'jp',
    'fujieda': 'jp',
    'fujinomiya': 'jp',
    'fujisawa': 'jp',
    'fukayacho': 'jp',
    'fukui-shi': 'jp',
    'fukuoka': 'jp',
    'fukushima': 'jp',
    'fukuyama': 'jp',
    'garut': 'id',
    'gasteiz / vitoria': 'es',
    'getafe': 'es',
    'gifu': 'jp',
    'gijon': 'es',
    'gillingham': 'gb',
    'ginowan': 'jp',
    'girona': 'es',
    'gloucester': 'gb',
    'gold coast': 'au',
    'gorontalo': 'id',
    'grogol': 'id',
    'gunungsitoli': 'id',
    'habikino': 'jp',
    'hachinohe': 'jp',
    'hachioji': 'jp',
    'hadano': 'jp',
    'hakodate': 'jp',
    'hamamatsu': 'jp',
    'handa': 'jp',
    'harrow': 'gb',
    'hatsukaichi': 'jp',
    'higashihiroshima': 'jp',
    'higashikurume': 'jp',
    'higashimurayama': 'jp',
    'high wycombe': 'gb',
    'hikone': 'jp',
    'hino': 'jp',
    'hirakata': 'jp',
    'hiratsuka': 'jp',
    'hirosaki': 'jp',
    'hiroshima': 'jp',
    'hitachi': 'jp',
    'hitachi-naka': 'jp',
    'hofu': 'jp',
    'honcho': 'jp',
    'honmachi': 'jp',
    'huddersfield': 'gb',
    'huelva': 'es',
    'ibaraki': 'jp',
    'ichihara': 'jp',
    'ichikawa': 'jp',
    'ichinomiya': 'jp',
    'ichinoseki': 'jp',
    'iida': 'jp',
    'iizuka': 'jp',
    'ikeda': 'jp',
    'ilford': 'gb',
    'imabari': 'jp',
    'inazawa': 'jp',
    'inzai': 'jp',
    'ipswich': 'gb',
    'iruma': 'jp',
    'isahaya': 'jp',
    'ise': 'jp',
    'isehara': 'jp',
    'isesaki': 'jp',
    'ishinomaki': 'jp',
    'itami': 'jp',
    'iwaki': 'jp',
    'iwakuni': 'jp',
    'iwata': 'jp',
    'iwatsuki': 'jp',
    'izumi': 'jp',
    'izumisano': 'jp',
    'izumo': 'jp',
    'jaen': 'es',
    'jakarta': 'id',
    'jambi city': 'id',
    'jayapura': 'id',
    'jember': 'id',
    'jepara': 'id',
    'jerez de la frontera': 'es',
    'joetsu': 'jp',
    'jombang': 'id',
    'kagoshima': 'jp',
    'kakamigahara': 'jp',
    'kakegawa': 'jp',
    'kakogawacho-honmachi': 'jp',
    'kamagaya': 'jp',
    'kamakura': 'jp',
    'kamirenjaku': 'jp',
    'kanazawa': 'jp',
    'kani': 'jp',
    'kanoya': 'jp',
    'karatsu': 'jp',
    'karawang': 'id',
    'kariya': 'jp',
    'kashihara-shi': 'jp',
    'kashiwa': 'jp',
    'kashiwara': 'jp',
    'kasuga': 'jp',
    'kasugai': 'jp',
    'kasukabe': 'jp',
    'katsushika': 'jp',
    'katsuta': 'jp',
    'kawachi-nagano': 'jp',
    'kawagoe': 'jp',
    'kawaguchi': 'jp',
    'kawasaki': 'jp',
    'kazo': 'jp',
    'kediri': 'id',
    'kendari': 'id',
    'kingston upon hull': 'gb',
    'kirishima': 'jp',
    'kiryu': 'jp',
    'kisarazu': 'jp',
    'kishiwada': 'jp',
    'kitakyushu': 'jp',
    'kitami': 'jp',
    'klaten': 'id',
    'klungkung': 'id',
    'kobe': 'jp',
    'kofu': 'jp',
    'koga': 'jp',
    'koganei': 'jp',
    'kokubunji': 'jp',
    'komaki': 'jp',
    'komatsu': 'jp',
    'konosu': 'jp',
    'koriyama': 'jp',
    'koshigaya': 'jp',
    'koto': 'jp',
    'kresek': 'id',
    'kukichuo': 'jp',
    'kumagaya': 'jp',
    'kumamoto': 'jp',
    'kuningan': 'id',
    'kupang': 'id',
    'kurashiki': 'jp',
    'kure': 'jp',
    'kurume': 'jp',
    'kusatsu': 'jp',
    'kushiro': 'jp',
    'kuwana': 'jp',
    'langsa': 'id',
    'lawang': 'id',
    'leeds': 'gb',
    'leicester': 'gb',
    'lembang': 'id',
    'lhokseumawe': 'id',
    'lincoln': 'gb',
    'lleida': 'es',
    'loa janan': 'id',
    'logan city': 'au',
    'logrono': 'es',
    'lubuklinggau': 'id',
    'lumajang': 'id',
    'luton': 'gb',
    'machida': 'jp',
    'mackay': 'au',
    'madiun': 'id',
    'maebashi': 'jp',
    'maidstone': 'gb',
    'makassar': 'id',
    'malang': 'id',
    'manado': 'id',
    'manchester': 'gb',
    'manokwari': 'id',
    'mansfield': 'gb',
    'marbella': 'es',
    'marugame': 'jp',
    'mataram': 'id',
    'mataro': 'es',
    'matsubara': 'jp',
    'matsudo': 'jp',
    'matsue': 'jp',
    'matsumoto': 'jp',
    'matsusaka': 'jp',
    'matsuto': 'jp',
    'matsuyama': 'jp',
    'medan': 'id',
    'mendip': 'gb',
    'merauke': 'id',
    'metro': 'id',
    'middlesbrough': 'gb',
    'milton keynes': 'gb',
    'minamirinkan': 'jp',
    'minato': 'jp',
    'minoh': 'jp',
    'misato, saitama': 'jp',
    'mishima': 'jp',
    'mito': 'jp',
    'miyakonojo': 'jp',
    'miyazaki': 'jp',
    'mojokerto': 'id',
    'morioka': 'jp',
    'mostoles': 'es',
    'murcia': 'es',
    'musashino': 'jp',
    'nagahama': 'jp',
    'nagano': 'jp',
    'nagaoka': 'jp',
    'nagareyama': 'jp',
    'nagoya': 'jp',
    'narashino': 'jp',
    'narita': 'jp',
    'nasushiobara': 'jp',
    'negara': 'id',
    'newcastle under lyme': 'gb',
    'newcastle upon tyne': 'gb',
    'newport': 'gb',
    'neyagawa': 'jp',
    'niigata': 'jp',
    'niihama': 'jp',
    'niiza': 'jp',
    'nishi-tokyo-shi': 'jp',
    'nishinomiya': 'jp',
    'nishio': 'jp',
    'nobeoka': 'jp',
    'noda': 'jp',
    'northampton': 'gb',
    'norwich': 'gb',
    'nottingham': 'gb',
    'numazu': 'jp',
    'obihiro': 'jp',
    'odawara': 'jp',
    'ogaki': 'jp',
    'oita': 'jp',
    'okayama': 'jp',
    'okazaki': 'jp',
    'oldham': 'gb',
    'ome': 'jp',
    'omuta': 'jp',
    'onojo': 'jp',
    'onomichi': 'jp',
    'orihuela': 'es',
    'osaki': 'jp',
    'oshu': 'jp',
    'ota': 'jp',
    'otaru': 'jp',
    'ourense': 'es',
    'oyama': 'jp',
    'padalarang': 'id',
    'padang': 'id',
    'padangsidempuan': 'id',
    'palangkaraya': 'id',
    'palembang': 'id',
    'palma': 'es',
    'palopo': 'id',
    'palu': 'id',
    'pamplona': 'es',
    'pamulang': 'id',
    'pangkalpinang': 'id',
    'pare': 'id',
    'parepare': 'id',
    'parla': 'es',
    'parung': 'id',
    'pasuruan': 'id',
    'pati': 'id',
    'payakumbuh': 'id',
    'pekalongan': 'id',
    'pekanbaru': 'id',
    'pelabuhanratu': 'id',
    'pemalang': 'id',
    'pematangsiantar': 'id',
    'percut': 'id',
    'peterborough': 'gb',
    'plymouth': 'gb',
    'pontianak': 'id',
    'portsmouth': 'gb',
    'preston': 'gb',
    'probolinggo': 'id',
    'purwakarta': 'id',
    'purwodadi': 'id',
    'purwokerto': 'id',
    'rangkasbitung': 'id',
    'rantauprapat': 'id',
    'reading': 'gb',
    'reus': 'es',
    'rotherham': 'gb',
    'sabadell': 'es',
    'saga': 'jp',
    'sagamihara': 'jp',
    'saijo': 'jp',
    'saint peters': 'gb',
    'saitama': 'jp',
    'sakado': 'jp',
    'sakai': 'jp',
    'sakata': 'jp',
    'sakura': 'jp',
    'salatiga': 'id',
    'salford': 'gb',
    'samarinda': 'id',
    'sampit': 'id',
    'sandacho': 'jp',
    'sano': 'jp',
    'sapporo': 'jp',
    'sasebo': 'jp',
    'sayama': 'jp',
    'semarang': 'id',
    'sendai': 'jp',
    'serang': 'id',
    'seto': 'jp',
    'sheffield': 'gb',
    'shimonoseki': 'jp',
    'shimotoda': 'jp',
    'shinagawa': 'jp',
    'shizuoka': 'jp',
    'sidoarjo': 'id',
    'singaraja': 'id',
    'singkawang': 'id',
    'singosari': 'id',
    'situbondo': 'id',
    'slough': 'gb',
    'soka': 'jp',
    'solihull': 'gb',
    'soreang': 'id',
    'sorong': 'id',
    'south tangerang': 'id',
    'southend-on-sea': 'gb',
    'st helens': 'gb',
    'stockport': 'gb',
    'stoke-on-trent': 'gb',
    'subang': 'id',
    'suginami': 'jp',
    'sukabumi': 'id',
    'sukawati': 'id',
    'sumida': 'jp',
    'sunderland': 'gb',
    'sungai penuh': 'id',
    'sungailiat': 'id',
    'sunggal': 'id',
    'sunshine coast': 'au',
    'surabaya': 'id',
    'surakarta': 'id',
    'sutton': 'gb',
    'sutton coldfield': 'gb',
    'suzuka': 'jp',
    'swansea': 'gb',
    'swindon': 'gb',
    'tachikawa': 'jp',
    'tajimi': 'jp',
    'takamatsu': 'jp',
    'takaoka': 'jp',
    'takarazuka': 'jp',
    'takasaki': 'jp',
    'takatsuki': 'jp',
    'tama': 'jp',
    'tangerang': 'id',
    'tanjung pandan': 'id',
    'tanjung pinang': 'id',
    'tanjungbalai': 'id',
    'tarakan': 'id',
    'tasikmalaya': 'id',
    'tegal': 'id',
    'telford': 'gb',
    'teluknaga': 'id',
    'ternate': 'id',
    'terrassa': 'es',
    'tochigi': 'jp',
    'tokai': 'jp',
    'tokorozawa': 'jp',
    'tokushima': 'jp',
    'tokuyama': 'jp',
    'tomakomai': 'jp',
    'tondabayashicho': 'jp',
    'toowoomba': 'au',
    'toride': 'jp',
    'torrejon de ardoz': 'es',
    'tottori-shi': 'jp',
    'toyama': 'jp',
    'toyohashi': 'jp',
    'toyokawa': 'jp',
    'toyota': 'jp',
    'tsu': 'jp',
    'tsuchiura': 'jp',
    'tsukuba': 'jp',
    'tsuruoka': 'jp',
    'tsuyama': 'jp',
    'ube': 'jp',
    'ueda': 'jp',
    'uji': 'jp',
    'ungaran': 'id',
    'urasoe': 'jp',
    'urayasu': 'jp',
    'uruma': 'jp',
    'utsunomiya': 'jp',
    'vigo': 'es',
    'wakayama': 'jp',
    'wakefield': 'gb',
    'walsall': 'gb',
    'warrington': 'gb',
    'watampone': 'id',
    'watford': 'gb',
    'west bromwich': 'gb',
    'wigan': 'gb',
    'woking': 'gb',
    'wollongong': 'au',
    'wolverhampton': 'gb',
    'worthing': 'gb',
    'yachiyo': 'jp',
    'yaizu': 'jp',
    'yamagata': 'jp',
    'yamaguchi': 'jp',
    'yamato': 'jp',
    'yao': 'jp',
    'yogyakarta': 'id',
    'yokkaichi': 'jp',
    'yokohama': 'jp',
    'yokosuka': 'jp',
    'yonago': 'jp',
    'yono': 'jp',
    'youkaichi': 'jp',
    'zama': 'jp',
    'zaragoza': 'es',
})  # Stage F giants1

# --- Stage F batch-3 (RU/NG/UA/VE): country-level region profiles + city map.
# Additive — no existing sub-regions for these; detailed seasonal seeds deferred
# to reconciliation #22. Advisories are defensible STANDING pre-2026 US State-Dept
# baselines: VE L4 crime, NG L3 crime, UA L4 ARMED_CONFLICT (active war ->
# WAR/EXC-WAR-2), RU L4 advisory_elevated (do-not-travel; interior not a battlefield).
_SEISMIC_RESILIENCE_BY_REGION.update({"ve": 50, "ng": 70, "ua": 78, "ru": 55})
_REGION_SOURCE.update({
    "ve": "seed:society-region-profile-2026 (stage-f-batch3)",
    "ng": "seed:society-region-profile-2026 (stage-f-batch3)",
    "ua": "seed:society-region-profile-2026 (stage-f-batch3)",
    "ru": "seed:society-region-profile-2026 (stage-f-batch3)",
})
_ADVISORY_LEVEL_BY_REGION.update({"ve": 4, "ng": 3, "ua": 4, "ru": 4})
# RU: no category -> generic ADVISORY_ELEVATED reason code (L4 do-not-travel, no
# single seeded cause); only armed_conflict/civil_unrest/crime are explicit categories.
_ADVISORY_CATEGORY_BY_REGION.update({"ve": "crime", "ng": "crime", "ua": "armed_conflict"})
_CITY_TO_REGION.update({
    'aba': 'ng',
    'abakaliki': 'ng',
    'abakan': 'ru',
    'abeokuta': 'ng',
    'abuja': 'ng',
    'acarigua': 've',
    'achinsk': 'ru',
    'admiralteisky': 'ru',
    'ado-ekiti': 'ng',
    'ajegunle': 'ng',
    'akademicheskoe': 'ru',
    'akowonjo': 'ng',
    'akure': 'ng',
    'alchevsk': 'ua',
    'aliayabiagba': 'ng',
    'alimosho': 'ng',
    'alto barinas': 've',
    'al’met’yevsk': 'ru',
    'amaigbo': 'ng',
    'anaco': 've',
    'angarsk': 'ru',
    'araure': 've',
    'arkhangel’sk': 'ru',
    'armavir': 'ru',
    'artem': 'ru',
    'arzamas': 'ru',
    'astrakhan': 'ru',
    'avtozavodskyi': 'ua',
    'awka': 'ng',
    'balakovo': 'ru',
    'balashikha': 'ru',
    'barinas': 've',
    'barnaul': 'ru',
    'barquisimeto': 've',
    'baruta': 've',
    'bataysk': 'ru',
    'bauchi': 'ng',
    'belgorod': 'ru',
    'benin city': 'ng',
    'berdyansk': 'ua',
    'berezniki': 'ru',
    'bibirevo': 'ru',
    'bila tserkva': 'ua',
    'birnin kebbi': 'ng',
    'biryulevo': 'ru',
    'biysk': 'ru',
    'bogorodskoye': 'ru',
    'bohuniya': 'ua',
    'borshchahivka': 'ua',
    'brateyevo': 'ru',
    'bratsk': 'ru',
    'brovary': 'ua',
    'bryansk': 'ru',
    'cabimas': 've',
    'cabudare': 've',
    'cagua': 've',
    'calabar': 'ng',
    'calabozo': 've',
    'caracas': 've',
    'carora': 've',
    'carupano': 've',
    'catia la mar': 've',
    'centralniy': 'ru',
    'cheboksary': 'ru',
    'chelyabinsk': 'ru',
    'cheremushki': 'ru',
    'cheremushky': 'ua',
    'cherepovets': 'ru',
    'cherkasy': 'ua',
    'cherkessk': 'ru',
    'chernihiv': 'ua',
    'chernivtsi': 'ua',
    'chertanovo yuzhnoye': 'ru',
    'chita': 'ru',
    'ciudad bolivar': 've',
    'ciudad guayana': 've',
    'ciudad ojeda': 've',
    'coro': 've',
    'cumana': 've',
    'darnytsya': 'ua',
    'derbent': 'ru',
    'desna': 'ua',
    'desnyanskyi': 'ua',
    'dimitrovgrad': 'ru',
    'dnipro': 'ua',
    'dniprovskyi': 'ua',
    'donetsk': 'ua',
    'dzerzhinsk': 'ru',
    'ebute ikorodu': 'ng',
    'efon-alaaye': 'ng',
    'ejido': 've',
    'ejigbo': 'ng',
    'el limon': 've',
    'el tigre': 've',
    'el vigia': 've',
    'elektrostal’': 'ru',
    'elista': 'ru',
    'engels': 'ru',
    'enugu': 'ng',
    'fortechnyi': 'ua',
    'glazov': 'ru',
    'gol’yanovo': 'ru',
    'gombe': 'ng',
    'grozny': 'ru',
    'guacara': 've',
    'guanare': 've',
    'guatire': 've',
    'gusau': 'ng',
    'hirnytskyi': 'ua',
    'horlivka': 'ua',
    'ibadan': 'ng',
    'ikeja': 'ng',
    'ikot ekpene': 'ng',
    'ile-ife': 'ng',
    'ilobu': 'ng',
    'ilorin': 'ng',
    'irewe': 'ng',
    'irkutsk': 'ru',
    'ivano-frankivsk': 'ua',
    'ivanovo': 'ru',
    'ivanovskoye': 'ru',
    'iwo': 'ng',
    'izhevsk': 'ru',
    'izmaylovo': 'ru',
    'jos': 'ng',
    'kaduna': 'ng',
    'kaliningrad': 'ru',
    'kalininskiy': 'ru',
    'kalmiuskyi': 'ua',
    'kaluga': 'ru',
    'kalynivskyi': 'ua',
    'kamensk-ural’skiy': 'ru',
    'kamyanske': 'ua',
    'kamyshin': 'ru',
    'kano': 'ng',
    'kansk': 'ru',
    'katsina': 'ng',
    'kazan': 'ru',
    'kemerovo': 'ru',
    'kerch': 'ua',
    'khabarovsk': 'ru',
    'khabarovsk vtoroy': 'ru',
    'khanty-mansiysk': 'ru',
    'kharkiv': 'ua',
    'khasavyurt': 'ru',
    'khimki': 'ru',
    'khmelnytskyi': 'ua',
    'khoroshevo-mnevniki': 'ru',
    'kindrativskyi': 'ua',
    'kirov': 'ru',
    'kislovodsk': 'ru',
    'kolomna': 'ru',
    'kolpino': 'ru',
    'komsomolsk-on-amur': 'ru',
    'korolev': 'ru',
    'korolyov': 'ua',
    'kostroma': 'ru',
    'kovrov': 'ru',
    'kramatorsk': 'ua',
    'krasnodar': 'ru',
    'krasnogvargeisky': 'ru',
    'krasnoyarsk': 'ru',
    'kremenchuk': 'ua',
    'kropyvnytskyi': 'ua',
    'kryvyy rih': 'ua',
    'kuntsevo': 'ru',
    'kurgan': 'ru',
    'kursk': 'ru',
    'kuz’minki': 'ru',
    'kyiv': 'ua',
    'kyivskyi': 'ua',
    'kyzyl': 'ru',
    'la victoria': 've',
    'lagos': 'ng',
    'leninsk-kuznetsky': 'ru',
    'lipetsk': 'ru',
    'livoberezhnyi': 'ua',
    'los puertos de altagracia': 've',
    'los rastrojos': 've',
    'los teques': 've',
    'luhansk': 'ua',
    'lutsk': 'ua',
    'lviv': 'ua',
    'lyubertsy': 'ru',
    'lyublino': 'ru',
    'machiques': 've',
    'magnitogorsk': 'ru',
    'maiduguri': 'ng',
    'makhachkala': 'ru',
    'makurdi': 'ng',
    'maracaibo': 've',
    'maracay': 've',
    'mariara': 've',
    'mariupol': 'ua',
    'mar’ino': 'ru',
    'maturin': 've',
    'maykop': 'ru',
    'melitopol': 'ua',
    'mezhdurechensk': 'ru',
    'miass': 'ru',
    'minna': 'ng',
    'moscow': 'ru',
    'murmansk': 'ru',
    'murom': 'ru',
    'mushin': 'ng',
    'mykilska borshchahivka': 'ua',
    'mykolayiv': 'ua',
    'mytishchi': 'ru',
    'naberezhnyye chelny': 'ru',
    'nakhodka': 'ru',
    'nalchik': 'ru',
    'nazran': 'ru',
    'neftekamsk': 'ru',
    'nefteyugansk': 'ru',
    'nevinnomyssk': 'ru',
    'nikopol': 'ua',
    'nizhnekamsk': 'ru',
    'nizhnevartovsk': 'ru',
    'nizhniy novgorod': 'ru',
    'nizhny tagil': 'ru',
    'nnewi': 'ng',
    'noginsk': 'ru',
    'norilsk': 'ru',
    'novo-peredelkino': 'ru',
    'novocheboksarsk': 'ru',
    'novocherkassk': 'ru',
    'novokuybyshevsk': 'ru',
    'novokuznetsk': 'ru',
    'novomoskovsk': 'ru',
    'novorossiysk': 'ru',
    'novosibirsk': 'ru',
    'novotroitsk': 'ru',
    'novyye cheremushki': 'ru',
    'novyye kuz’minki': 'ru',
    'noyabrsk': 'ru',
    'nyzhnodniprovsk': 'ua',
    'obalende': 'ng',
    'obninsk': 'ru',
    'obolon': 'ua',
    'ochakovo-matveyevskoye': 'ru',
    'odesa': 'ua',
    'odintsovo': 'ru',
    'ogbomoso': 'ng',
    'okrika': 'ng',
    'oktyabrsky': 'ru',
    'oleksandrivskyi': 'ua',
    'oleksiyivka': 'ua',
    'omsk': 'ru',
    'onitsha': 'ng',
    'orekhovo-borisovo': 'ru',
    'orekhovo-borisovo severnoye': 'ru',
    'orekhovo-zuyevo': 'ru',
    'orel': 'ru',
    'orenburg': 'ru',
    'orsk': 'ru',
    'oshodi': 'ng',
    'osogbo': 'ng',
    'owerri': 'ng',
    'palo negro': 've',
    'pavlohrad': 'ua',
    'pechersk': 'ua',
    'penza': 'ru',
    'perm': 'ru',
    'pervouralsk': 'ru',
    'petare': 've',
    'petrogradka': 'ru',
    'petropavlovsk-kamchatsky': 'ru',
    'petrozavodsk': 'ru',
    'podolsk': 'ru',
    'poltava': 'ua',
    'porlamar': 've',
    'port harcourt': 'ng',
    'pozniaky': 'ua',
    'presnenskiy': 'ru',
    'prokop’yevsk': 'ru',
    'pskov': 'ru',
    'puerto ayacucho': 've',
    'puerto cabello': 've',
    'puerto la cruz': 've',
    'punta cardon': 've',
    'punto fijo': 've',
    'pushkino': 'ru',
    'pyatigorsk': 'ru',
    'ramenki': 'ru',
    'rayon ktz': 'ua',
    'rivne': 'ua',
    'rostov-on-don': 'ru',
    'rubtsovsk': 'ru',
    'rutchenkivskyi': 'ua',
    'ryazanskiy': 'ru',
    'ryazan’': 'ru',
    'rybinsk': 'ru',
    'saint petersburg': 'ru',
    'saki': 'ng',
    'salavat': 'ru',
    'saltivka': 'ua',
    'samara': 'ru',
    'san carlos': 've',
    'san carlos del zulia': 've',
    'san felipe': 've',
    'san fernando de apure': 've',
    'san juan de los morros': 've',
    'sapele': 'ng',
    'saransk': 'ru',
    'saratov': 'ru',
    'sergiyev posad': 'ru',
    'serpukhov': 'ru',
    'sevastopol': 'ua',
    'severnyy': 'ru',
    'severodvinsk': 'ru',
    'seversk': 'ru',
    'shagamu': 'ng',
    'shakhty': 'ru',
    'shchukino': 'ru',
    'shchyolkovo': 'ru',
    'shevchenkivskyi': 'ua',
    'shevchenko': 'ua',
    'shomolu': 'ng',
    'simferopol': 'ua',
    'skhidni kvartaly': 'ua',
    'slovyansk': 'ua',
    'smolensk': 'ru',
    'smolyanskyi': 'ua',
    'sochi': 'ru',
    'sokoto': 'ng',
    'solikamsk': 'ru',
    'solntsevo': 'ru',
    'staryy oskol': 'ru',
    'stavropol': 'ru',
    'sterlitamak': 'ru',
    'strogino': 'ru',
    'sumy': 'ua',
    'surgut': 'ru',
    'sykhiv': 'ua',
    'syktyvkar': 'ru',
    'syzran': 'ru',
    'taganrog': 'ru',
    'taganskiy': 'ru',
    'tambov': 'ru',
    'tariba': 've',
    'tekstil’shchiki': 'ru',
    'ternopil': 'ua',
    'tinaquillo': 've',
    'tobolsk': 'ru',
    'tolyatti': 'ru',
    'tomsk': 'ru',
    'troparevo': 'ru',
    'tsaritsyno': 'ru',
    'tsentralno-miskyi': 'ua',
    'tsentralnyi': 'ua',
    'tula': 'ru',
    'tver': 'ru',
    'tyoply stan': 'ru',
    'tyumen': 'ru',
    'ufa': 'ru',
    'ukhta': 'ru',
    'ulan-ude': 'ru',
    'ulyanovsk': 'ru',
    'upata': 've',
    'ussuriysk': 'ru',
    'ust’-ilimsk': 'ru',
    'uyo': 'ng',
    'uzhhorod': 'ua',
    'valera': 've',
    'valle de la pascua': 've',
    "vasyl'evsky ostrov": 'ru',
    'velikiy novgorod': 'ru',
    'velikiye luki': 'ru',
    'veshnyaki': 'ru',
    'vilkhivskyi': 'ua',
    'vinnytsya': 'ua',
    'vladikavkaz': 'ru',
    'vladimir': 'ru',
    'vladivostok': 'ru',
    'volgodonsk': 'ru',
    'volgograd': 'ru',
    'vologda': 'ru',
    'volzhsky': 'ru',
    'voronezh': 'ru',
    'vyhurivshchyna-troyeshchyna': 'ua',
    'vykhino-zhulebino': 'ru',
    'warri': 'ng',
    'yakutsk': 'ru',
    'yaroslavl': 'ru',
    'yasenevo': 'ru',
    'yekaterinburg': 'ru',
    'yelets': 'ru',
    'yenagoa': 'ng',
    'yevpatoriya': 'ua',
    'yoshkar-ola': 'ru',
    'yuzhno-sakhalinsk': 'ru',
    'zaporizhzhya': 'ua',
    'zaria': 'ng',
    'zarichnyi': 'ua',
    'zelenograd': 'ru',
    'zheleznodorozhnyy': 'ru',
    'zhulebino': 'ru',
    'zhytomyr': 'ua',
    'zlatoust': 'ru',
    'zyablikovo': 'ru',
    'zyuzino': 'ru',
})  # Stage F batch-3

# --- Microstates batch 1 (AD + Caribbean): additive country-level profiles.
# Caribbean islands sit in the Atlantic HURRICANE belt (Jun-Nov, peak Sep) -> a
# conservative cyclone profile flags the dominant hazard (finer per-island bp -> #22);
# Trinidad is south of the main belt (reduced). Andorra: landlocked Pyrenees, no cyclone,
# high seismic resilience. Advisory: TT L2 (crime); others L1 (safe).
# vc (St Vincent) = 45 (<50 flag threshold): the engine has no volcano channel, so
# active-volcano hazard is encoded by a sub-50 seismic resilience (cf. et-afar=20,
# id-bali=35, ph-luzon=45) — surfaces La Soufriere (active stratovolcano, 2021
# eruption + mass evacuation) instead of silently dropping it. (Audit fix.)
_SEISMIC_RESILIENCE_BY_REGION.update({'ad': 80, 'ag': 58, 'bb': 60, 'gd': 55, 'kn': 58, 'lc': 58, 'vc': 45, 'tt': 60})
_REGION_SOURCE.update({r: "seed:society-region-profile-2026 (microstates-1)" for r in ['ad', 'ag', 'bb', 'gd', 'kn', 'lc', 'tt', 'vc']})
_CYCLONE_BY_REGION_MONTH.update({
    'ag': {6: 300, 7: 600, 8: 1200, 9: 1800, 10: 1200, 11: 400},
    'bb': {6: 300, 7: 600, 8: 1200, 9: 1800, 10: 1200, 11: 400},
    'gd': {6: 300, 7: 600, 8: 1200, 9: 1800, 10: 1200, 11: 400},
    'kn': {6: 300, 7: 600, 8: 1200, 9: 1800, 10: 1200, 11: 400},
    'lc': {6: 300, 7: 600, 8: 1200, 9: 1800, 10: 1200, 11: 400},
    'tt': {8: 200, 9: 300, 10: 200},
    'vc': {6: 300, 7: 600, 8: 1200, 9: 1800, 10: 1200, 11: 400},
})
_ADVISORY_LEVEL_BY_REGION.update({"tt": 2})
_ADVISORY_CATEGORY_BY_REGION.update({"tt": "crime"})
_CITY_TO_REGION.update({
    'andorra la vella': 'ad',
    'arima': 'tt',
    'basseterre': 'kn',
    'bridgetown': 'bb',
    'calliaqua': 'vc',
    'castries': 'lc',
    'chaguanas': 'tt',
    'diego martin': 'tt',
    'gros islet': 'lc',
    'kingstown': 'vc',
    'laventille': 'tt',
    'les escaldes': 'ad',
    'marabella': 'tt',
    'mon repos': 'tt',
    'paradise': 'tt',
    'point fortin': 'tt',
    'port of spain': 'tt',
    "saint george's": 'gd',
    'saint john’s': 'ag',
    'sangre grande': 'tt',
    'scarborough': 'tt',
    'tunapuna': 'tt',
})  # Microstates batch 1

# --- Microstates batch 2 (Pacific + Brunei): additive country-level profiles.
# Ring-of-Fire seismic: Vanuatu/Solomon very active (40); Tonga 45 (Hunga Tonga
# volcano, 2022). Cyclone: South Pacific Nov-Apr (VU/SB/TO/TV), NW Pacific typhoon
# Jul-Nov (FM/MH); Kiribati/Nauru near-equator + Brunei (Borneo) -> none. All L1.
_SEISMIC_RESILIENCE_BY_REGION.update({'fm': 60, 'ki': 65, 'mh': 62, 'nr': 65, 'tv': 63, 'to': 45, 'vu': 40, 'sb': 40, 'bn': 70})
_REGION_SOURCE.update({r: "seed:society-region-profile-2026 (microstates-2)" for r in ['bn', 'fm', 'ki', 'mh', 'nr', 'sb', 'to', 'tv', 'vu']})
_CYCLONE_BY_REGION_MONTH.update({
    'fm': {7: 600, 8: 1000, 9: 1200, 10: 900, 11: 500},
    'mh': {7: 600, 8: 1000, 9: 1200, 10: 900, 11: 500},
    'sb': {1: 1400, 2: 1600, 3: 1200, 4: 500, 11: 400, 12: 900},
    'to': {1: 1400, 2: 1600, 3: 1200, 4: 500, 11: 400, 12: 900},
    'tv': {1: 1400, 2: 1600, 3: 1200, 4: 500, 11: 400, 12: 900},
    'vu': {1: 1400, 2: 1600, 3: 1200, 4: 500, 11: 400, 12: 900},
})

# ── Cyclone-seed recalibration — six-basin climatology audit 2026-06-26 (#67 follow-up) ──
# Applied LAST so it wins over every prior block. Over-warn trims DOWN, genuine
# under-warns UP, two new typhoon seeds (jp/vn-south: NAME the hazard + light the
# delay/connection channel WITHOUT raising the flood-driven tier — both peak < the
# region's flood peak and < the 3000 AVOID bar), and the 4 landlocked empty-dict
# stubs removed (latent max() crash; their remnant rain already lives in the FLOOD
# channel). Data-only + deterministic → var-0 preserved.
_CYCLONE_BY_REGION_MONTH.update({
    # NW-Pacific typhoon coverage gaps (were flood-only → typhoon invisible, no delay channel):
    'jp':         {7: 1200, 8: 2200, 9: 2800, 10: 2400},                     # Honshu/Kyushu — THE Tokyo bug; Sep 28% < flood 40%
    'vn-south':   {9: 700, 10: 1300, 11: 1800, 12: 900},                     # late-season SCS typhoons (Durian/Tembin), peak Nov
    # Under-warn raises (genuinely exposed; were flat placeholders / under-seeded):
    'cn-main':    {6: 800, 7: 2000, 8: 3300, 9: 3500, 10: 1800, 11: 600},    # SE-China coast + Hainan 20→35%
    'vu':         {11: 1200, 12: 2500, 1: 3800, 2: 4000, 3: 3500, 4: 1800},  # Vanuatu 16→40% — Pam/Harold Cat-5
    'to':         {11: 600, 12: 1500, 1: 2800, 2: 3200, 3: 2800, 4: 1200},   # Tonga 16→32% — Gita Cat-4
    'sb':         {11: 500, 12: 1200, 1: 2000, 2: 2200, 3: 1800, 4: 800},    # Solomon 16→22% — N-belt edge
    # Over-warn trims (DOWN, #67 conservative):
    'us-hawaii':  {6: 200, 7: 1100, 8: 2000, 9: 1500, 10: 700, 11: 200},     # Hawaii 45→20% — direct hits ~decadal
    'pf':         {12: 1000, 1: 1800, 2: 2200, 3: 1800, 4: 800},             # Fr.Polynesia 40→22% — mostly outside belt
    'dm':         {6: 500, 7: 800, 8: 1700, 9: 2300, 10: 1500, 11: 400},     # Dominica 40→23% — trim the Maria outlier, keep a high-exposure Lesser-Antilles level
    'gt':         {6: 600, 7: 1200, 8: 1800, 9: 2500, 10: 2000, 11: 1000},   # Guatemala 38→25% — short sheltered Caribbean coast
    'pr':         {6: 600, 7: 1000, 8: 2800, 9: 4000, 10: 2000, 11: 600},    # Puerto Rico 45→40%
    'ni':         {6: 700, 7: 1100, 8: 1800, 9: 3000, 10: 3500, 11: 2200},   # Nicaragua 40→35%
    'sv':         {8: 600, 9: 1000, 10: 800, 11: 300},                       # El Salvador 18→10% — no Caribbean coast (Pacific only)
    'mv':         {1: 1000, 10: 800, 11: 1000, 12: 1000},                    # Maldives 18→10% — near-equatorial cyclone-free belt
    'mx-pacific': {6: 700, 7: 2200, 8: 3500, 9: 3800, 10: 2600, 11: 1000},   # W-Mexico 45→38%
})
# Drop the 4 landlocked empty-dict cyclone stubs (no basin; remnant rain is a FLOOD hazard).
for _empty_cyc in ('mw', 'mw-malawi', 'zw', 'zw-zimbabwe'):
    _CYCLONE_BY_REGION_MONTH.pop(_empty_cyc, None)
# HK city bypassed its tailored region (resolved to 'cn' 35%); route to 'hk' (typhoon-named, tuned).
_CITY_TO_REGION.update({'hong kong': 'hk', 'hongkong': 'hk'})

_CITY_TO_REGION.update({
    'bandar seri begawan': 'bn',
    'dalap-uliga-dorrit': 'mh',
    'funafuti': 'tv',
    'honiara': 'sb',
    "kola'a": 'sb',
    'kuala belait': 'bn',
    'liang': 'bn',
    'majuro': 'mh',
    'mentiri': 'bn',
    'nuku‘alofa': 'to',
    'palikir': 'fm',
    'panatina': 'sb',
    'port-vila': 'vu',
    'sengkurong': 'bn',
    'serasa': 'bn',
    'seria': 'bn',
    'tarawa': 'ki',
    'tutong': 'bn',
    'yaren': 'nr',
})  # Microstates batch 2

# --- Microstates batch 3 (Europe-micro + Comoros + Sao Tome): country-level.
# Comoros km=45 (active Karthala volcano + 2018 Mayotte submarine event; sub-50
# surfaces, no volcano channel) + SW Indian Ocean cyclone Nov-Apr. San Marino
# sm=55 (seismic central-Italy Apennines). Sao Tome st=60 (volcanic origin, quiet;
# equatorial Atlantic -> no cyclone). Monaco mc=75 / Liechtenstein li=78 (low). All L1.
_SEISMIC_RESILIENCE_BY_REGION.update({'mc': 75, 'sm': 55, 'li': 78, 'km': 45, 'st': 60})
_REGION_SOURCE.update({r: "seed:society-region-profile-2026 (microstates-3)" for r in ['km', 'li', 'mc', 'sm', 'st']})
_CYCLONE_BY_REGION_MONTH.update({
    'km': {1: 1200, 2: 1400, 3: 1000, 4: 400, 11: 300, 12: 700},
})
_CITY_TO_REGION.update({
    'fomboni': 'km',
    'monaco': 'mc',
    'monte-carlo': 'mc',
    'moroni': 'km',
    'moutsamoudou': 'km',
    'san marino': 'sm',
    'sao tome': 'st',
    'vaduz': 'li',
})  # Microstates batch 3

# ---------------------------------------------------------------------------
# jp-north (Hokkaido + northern Tohoku / Aomori pref.) — split out of the national
# "jp" flood seed. Unlike Honshu/Kyushu, NORTHERN Japan has NO baiu rainy season and
# typhoons rarely reach it intact, so the national typhoon-flood pattern (which legitimately
# carries the Oct-Hagibis risk for Honshu) OVER-WARNS here — it was flagging Sapporo/
# Asahikawa/Aomori with an October flood AVOID that does not exist. Documented northern
# flood risk is LOW and confined to occasional late-summer (Aug–Sep) typhoon remnants, with
# NONE in October (winter transition). Both seeded months sit BELOW FLOOD_FLAG_BP, so the
# advisory simply does not raise a flood flag for these cities. Everything else (advisory L1,
# seismic resilience, transport delays) mirrors the national jp profile.
# Sources: JMA regional climatology (Hokkaido has no tsuyu); #67 over-warn calibration.
# ---------------------------------------------------------------------------
_FLOOD_BY_REGION_MONTH["jp-north"] = {8: 1100, 9: 1300}          # both < FLOOD_FLAG_BP; no Oct entry → 0
_DELAY_BY_REGION_MODE["jp-north"] = dict(_DELAY_BY_REGION_MODE["jp"])
_SEISMIC_RESILIENCE_BY_REGION["jp-north"] = _SEISMIC_RESILIENCE_BY_REGION["jp"]
_ADVISORY_LEVEL_BY_REGION["jp-north"] = _ADVISORY_LEVEL_BY_REGION["jp"]
_REGION_SOURCE["jp-north"] = "seed:JMA-regional-climatology-2026 (Hokkaido/N-Tohoku: no baiu, rare typhoon)"
_CITY_TO_REGION.update({
    c: "jp-north" for c in (
        "sapporo", "asahikawa", "hakodate", "otaru", "kushiro", "obihiro",
        "tomakomai", "kitami", "ebetsu",          # Hokkaido
        "aomori", "hirosaki", "hachinohe",        # Aomori pref. (northern Tohoku)
    )
})

# ===========================================================================
# Regional FLOOD-seed calibration (visa-weather cross-check 2026, workflow
# w2q1642x2, adversarially verified). National flood seeds over/under-warn
# climatically-distinct sub-regions; same jp-north pattern applied at scale.
# Each new sub-region inherits its parent country's advisory/seismic/delay/
# source; sources are the national met services per finding.
# ===========================================================================
# (a) Re-calibrate existing (national/sub-) seeds that were mis-tiered nationally.
_FLOOD_BY_REGION_MONTH["hk"] = {5: 1700, 6: 2400, 7: 1800, 8: 2400, 9: 2000}    # HK rainy May–Sep (was under-warn)
_FLOOD_BY_REGION_MONTH["tr"] = {10: 1500, 11: 1800, 12: 1900, 1: 1600, 2: 1300}  # Türkiye autumn-winter peak Oct-Dec (Istanbul/Marmara/Aegean/Med); summer→tr-blacksea
_FLOOD_BY_REGION_MONTH["tw"] = {5: 1600, 6: 1800, 7: 2000, 8: 2500, 9: 2000, 10: 1000}  # +Meiyu plum-rain May–Jun
_FLOOD_BY_REGION_MONTH["kr"] = {6: 1600, 7: 2500, 8: 2800, 9: 1800}             # +Changma onset late-June
_FLOOD_BY_REGION_MONTH["gh-ghana"] = {5: 1200, 6: 2000, 9: 1200, 10: 1500}      # drop Aug little-dry-season over-warn
_FLOOD_BY_REGION_MONTH["gh-north"] = {6: 1500, 7: 2000, 8: 2200, 9: 2500, 10: 1200}  # N. Ghana single late peak
_FLOOD_BY_REGION_MONTH["pe-coast"] = {1: 1200, 2: 1500, 3: 1200}                # desert littoral; ENSO layer amplifies
_FLOOD_BY_REGION_MONTH["es-cantabria"] = {10: 1300, 11: 1700, 12: 2000, 1: 1800, 2: 1600, 3: 1200}  # Atlantic-NW Iberia winter FLAG
_FLOOD_BY_REGION_MONTH["nz"] = {1: 1300, 2: 1700, 3: 1700, 4: 1500, 5: 1200, 6: 1500,
                                7: 1400, 8: 1300, 9: 1100, 10: 1100, 11: 900, 12: 1200}  # ex-cyclone Feb–Apr peak

# (b) New sub-regions: (region, parent_country, flood_seed, [cities]).
_FLOOD_SPLITS_2026 = [
    ("in-southeast", "in", {10: 2200, 11: 3500, 12: 2500, 1: 800},   # Tamil Nadu + coastal-SE NE-monsoon (Nov peak; Jun-Sep dry)
     ["chennai", "madurai", "coimbatore", "tiruchirappalli", "salem", "tirunelveli", "thanjavur",
      "vellore", "puducherry", "nellore", "vijayawada", "tirupati", "thoothukudi", "erode", "tiruppur",
      "dindigul", "karur", "nagercoil", "kanchipuram", "tambaram", "avadi", "ambattur", "cuddalore", "nagapattinam"]),
    ("cn-arid", "cn", {7: 500, 8: 500},                              # Gobi/Tibetan plateau — near-zero flood (worst over-warn)
     ["dunhuang", "lhasa", "darchen"]),
    ("cn-north", "cn", {7: 2000, 8: 1700},                           # N/NE China '七下八上' short July-Aug peak, semi-arid
     ["beijing", "harbin", "xi'an", "tianjin", "shenyang", "changchun", "jinan", "shijiazhuang"]),
    ("tr-blacksea", "tr", {7: 2000, 8: 2800, 9: 2500, 10: 2200, 11: 2000, 12: 1800, 1: 1600},  # year-round wet, autumn flash-flood
     ["rize", "trabzon", "ordu", "giresun", "samsun", "zonguldak",
      "kastamonu", "bolu", "duzce", "adapazari", "karabuk", "amasya", "tokat"]),  # W. Black Sea Aug-flash-flood belt
    ("sa-hejaz", "sa", {11: 2200, 12: 1800, 1: 1600, 2: 1000, 3: 1200, 4: 1500},  # Red-Sea corridor Nov-peak flash-flood
     ["jeddah", "mecca", "makkah", "yanbu"]),
    ("za-cape", "za", {4: 1200, 5: 1800, 6: 2200, 7: 2000, 8: 1500, 9: 800},  # Mediterranean WINTER-rainfall (Jun wettest)
     ["cape town", "george", "paarl", "worcester"]),
    ("cm-interior", "cm", {3: 800, 4: 1400, 5: 1800, 6: 1200, 9: 1900, 10: 2200, 11: 800},  # Yaoundé bimodal, Jul-Aug little-dry
     ["yaounde"]),
    ("sn-casamance", "sn", {6: 1200, 7: 2000, 8: 3000, 9: 2500, 10: 1500},  # high-rainfall south (~1200mm)
     ["ziguinchor"]),
    ("pe-andes", "pe", {1: 2500, 2: 3000, 3: 2500, 4: 1200, 11: 800, 12: 1800},  # Sierra austral-summer rains/huaycos
     ["cusco", "cuzco", "puno", "huaraz", "ayacucho", "huancayo", "cajamarca", "juliaca", "huanuco"]),
    ("pe-amazon", "pe", {11: 1800, 12: 2500, 1: 3000, 2: 3500, 3: 3500, 4: 2800, 5: 1800, 10: 1200},  # river-rise Nov-May
     ["iquitos", "pucallpa"]),
    ("cl-north", "cl", {2: 600},                                     # Atacama — essentially no seasonal flood
     ["arica", "iquique", "antofagasta", "calama", "copiapo", "coquimbo", "la serena"]),
    ("cl-altiplano", "cl", {1: 1500, 2: 2200, 3: 1500, 12: 1000},    # 'invierno boliviano' altiplanic summer storms
     ["san pedro de atacama"]),
    ("cl-south", "cl", {4: 1500, 5: 2000, 6: 3000, 7: 3200, 8: 2800, 9: 1800, 10: 1500},  # wet temperate, winter-amplified
     ["temuco", "valdivia", "osorno", "puerto montt", "punta arenas"]),
    ("ir-caspian", "ir", {8: 1500, 9: 2000, 10: 2400, 11: 2400, 12: 2000, 1: 1600, 4: 1000},  # Caspian autumn-peak (not spring)
     ["rasht", "sari", "gorgan", "bandar-e anzali", "babol"]),
    ("lk-southwest", "lk", {5: 2000, 6: 2200, 7: 1800, 8: 1800, 9: 1700, 10: 1800, 11: 2200},  # SW wet-zone monsoon continuity
     ["colombo", "negombo", "moratuwa"]),
]
# Coastal splits that REMAIN cyclone-exposed must keep the parent's basin cyclone seed (the flood
# split must not silently drop the named cyclone warning). in-southeast = Bay-of-Bengal Nov-Dec
# (Nivar/Michaung); lk-southwest = same basin. Inland/extratropical splits (cn-arid/cn-north/
# tr-blacksea/sa-hejaz/za-cape/...) correctly get NO cyclone — dropping it there removes an over-warn.
_CYCLONE_INHERIT_SPLITS = {"in-southeast", "lk-southwest"}
for _reg, _par, _seed, _cities in _FLOOD_SPLITS_2026:
    _FLOOD_BY_REGION_MONTH[_reg] = _seed
    if _reg in _CYCLONE_INHERIT_SPLITS and _par in _CYCLONE_BY_REGION_MONTH:
        _CYCLONE_BY_REGION_MONTH[_reg] = dict(_CYCLONE_BY_REGION_MONTH[_par])
    if _par in _DELAY_BY_REGION_MODE:
        _DELAY_BY_REGION_MODE[_reg] = dict(_DELAY_BY_REGION_MODE[_par])
    _SEISMIC_RESILIENCE_BY_REGION[_reg] = _SEISMIC_RESILIENCE_BY_REGION.get(_par, 50)
    if _par in _ADVISORY_LEVEL_BY_REGION:
        _ADVISORY_LEVEL_BY_REGION[_reg] = _ADVISORY_LEVEL_BY_REGION[_par]
    _REGION_SOURCE[_reg] = f"seed:visa-weather-crosscheck-2026 (regional flood climatology split from {_par})"
    _CITY_TO_REGION.update({_c: _reg for _c in _cities})

# (c) Reassign Atlantic-NW Spain to the existing winter-Atlantic es-cantabria region (not the
# Mediterranean-autumn national es seed), and N. Ghana (Tamale) to the now-seeded gh-north.
_CITY_TO_REGION.update({_c: "es-cantabria" for _c in
    ("a coruna", "vigo", "santiago de compostela", "oviedo", "gijon", "bilbao", "santander", "ourense")})
_CITY_TO_REGION["tamale"] = "gh-north"
# gh-north existed as an empty placeholder (no provenance/seismic) — complete it now that it carries a seed.
_REGION_SOURCE.setdefault("gh-north", "seed:visa-weather-crosscheck-2026 (N. Ghana single-peak flood)")
_SEISMIC_RESILIENCE_BY_REGION.setdefault("gh-north", _SEISMIC_RESILIENCE_BY_REGION.get("gh", 50))
_DELAY_BY_REGION_MODE.setdefault("gh-north", dict(_DELAY_BY_REGION_MODE.get("gh", {"flight": 6, "rail": 0, "bus": 8})))

# ── Cyclone audit Wave 2 — interior-China over-warn split + forward-looking basin coverage ──
# (a) Interior China: bare `cn` (Aug 35% typhoon) wrongly tagged 6 far-inland tourist cities
#     (Guilin/Huangshan/Leshan/Lijiang/Zhangjiajie) that take no direct typhoon — only the
#     Shanghai/Yangtze-delta cluster left on `cn` is genuinely coastal-exposed (In-Fa/Bebinca).
#     New `cn-interior` inherits cn's FLOOD exactly and carries NO cyclone seed (drops the
#     over-warn, keeps the real flood risk). Mirrors the cn-arid/cn-north inland-split precedent.
_FLOOD_BY_REGION_MONTH["cn-interior"] = dict(_FLOOD_BY_REGION_MONTH["cn"])
_REGION_SOURCE["cn-interior"] = "seed:cyclone-basin-audit-2026 (inland China — flood split from cn, no direct typhoon)"
_SEISMIC_RESILIENCE_BY_REGION["cn-interior"] = _SEISMIC_RESILIENCE_BY_REGION.get("cn", 50)
if "cn" in _DELAY_BY_REGION_MODE:
    _DELAY_BY_REGION_MODE["cn-interior"] = dict(_DELAY_BY_REGION_MODE["cn"])
_CITY_TO_REGION.update({_c: "cn-interior" for _c in
    ("guilin", "huangshan", "leshan", "lijiang", "zhangjiajie")})

# (b) Forward-looking cyclone coverage for genuinely exposed regions whose cities are NOT yet in
#     the booking catalog → these seeds are INERT until those cities are added (a separate data
#     task), but capture the audited calibration + basin-noun routing now. N-Atlantic→Hurricane
#     (ht/ky/tc/bm via _ATLANTIC_ISO); N-Indian/SW-Indian/S-Pacific→Cyclone (mm/re/yt/nc/ck/nu).
_CYCLONE_BY_REGION_MONTH.update({
    "ht": {6: 500, 7: 1200, 8: 2800, 9: 3500, 10: 2200, 11: 800},   # Haiti — Matthew 2016
    "ky": {6: 800, 7: 1500, 8: 2800, 9: 4000, 10: 3000, 11: 800},   # Cayman — NW-Caribbean (Ivan 2004)
    "tc": {6: 700, 7: 1100, 8: 3000, 9: 4200, 10: 2500, 11: 700},   # Turks & Caicos — Irma/Maria 2017
    "bm": {7: 600, 8: 1500, 9: 2500, 10: 1800, 11: 500},            # Bermuda — mid-Atlantic recurve
    "mm": {4: 1200, 5: 3000, 6: 800, 9: 600, 10: 2000, 11: 1500},   # Myanmar — pre-monsoon May (Nargis/Mocha)
    "re": {1: 2200, 2: 2500, 3: 1800, 4: 800, 12: 1500},            # Réunion — SWIO core
    "yt": {1: 1500, 2: 1800, 3: 1400, 4: 600, 12: 1000},            # Mayotte — Chido 2024
    "nc": {11: 800, 12: 1800, 1: 3000, 2: 3500, 3: 3200, 4: 1500},  # New Caledonia — Niran 2021
    "ck": {11: 600, 12: 1500, 1: 2500, 2: 3000, 3: 2500, 4: 1000},  # Cook Is — Pat 2010
    "nu": {11: 500, 12: 1500, 1: 2800, 2: 3000, 3: 2200, 4: 800},   # Niue — Heta 2004
})
for _fwd_reg, _fwd_seis in (("ht", 25), ("ky", 55), ("tc", 55), ("bm", 65), ("mm", 30),
                            ("re", 60), ("yt", 50), ("nc", 60), ("ck", 50), ("nu", 50)):
    _REGION_SOURCE.setdefault(_fwd_reg, "seed:cyclone-basin-audit-2026 (forward-looking; cities pending catalog)")
    _SEISMIC_RESILIENCE_BY_REGION.setdefault(_fwd_reg, _fwd_seis)

# ── Domain-correctness audit 2026-07 (E5) — Mexico state-level advisory split ──
# The blanket `mx` country seed (Level 2) badly under-warns four US State Dept
# Level-4 "Do Not Travel" states (verified travel.state.gov/mexico-travel-advisory
# 2026-07-04): Guerrero, Sinaloa, Zacatecas, Michoacan — "do not travel due to
# terrorism, crime and kidnapping" / "terrorism and crime". These four new
# sub-regions inherit `mx`'s cyclone/flood/delay/seismic seeds UNCHANGED (this is
# a SAFETY split, not a weather recalibration — the underlying climate profile
# is the same) and carry their own Level-4 advisory + "crime" cause category
# (matches the advisory text; not civil_unrest/armed_conflict per D5 #38).
for _mx_reg in ("mx-guerrero", "mx-sinaloa", "mx-zacatecas", "mx-michoacan"):
    _CYCLONE_BY_REGION_MONTH[_mx_reg] = dict(_CYCLONE_BY_REGION_MONTH.get("mx", {}))
    _FLOOD_BY_REGION_MONTH[_mx_reg] = dict(_FLOOD_BY_REGION_MONTH.get("mx", {}))
    if "mx" in _DELAY_BY_REGION_MODE:
        _DELAY_BY_REGION_MODE[_mx_reg] = dict(_DELAY_BY_REGION_MODE["mx"])
    _SEISMIC_RESILIENCE_BY_REGION[_mx_reg] = _SEISMIC_RESILIENCE_BY_REGION.get("mx", 28)
    _ADVISORY_LEVEL_BY_REGION[_mx_reg] = 4
    _ADVISORY_CATEGORY_BY_REGION[_mx_reg] = "crime"
    _REGION_SOURCE[_mx_reg] = (
        "seed:travel.state.gov-mexico-travel-advisory-2026-07 (state-level Level 4 "
        "Do Not Travel annex, verified via WebFetch 2026-07-04)"
    )
# Remap the specific catalog cities inside these four Level-4 states from the
# blanket `mx` (or the Pacific-coast weather region `mx-pacific`, for the two
# cities that sit in it) to their correct state-level sub-region. Task-named:
# Acapulco, Culiacan, Zacatecas, Morelia, Chilpancingo. Extended (same state,
# named explicitly in the advisory text or an obvious state capital/major city
# already in the catalog — not a guess): Iguala, Zihuatanejo (Guerrero);
# Mazatlan, Los Mochis (Sinaloa); Fresnillo (Zacatecas); Uruapan, Apatzingan,
# Ciudad Lazaro Cardenas, Zamora de Hidalgo (Michoacan).
_CITY_TO_REGION.update({
    "acapulco de juarez": "mx-guerrero",
    "chilpancingo": "mx-guerrero",
    "iguala de la independencia": "mx-guerrero",
    "zihuatanejo": "mx-guerrero",
    "culiacan": "mx-sinaloa",
    "mazatlan": "mx-sinaloa",
    "los mochis": "mx-sinaloa",
    "zacatecas": "mx-zacatecas",
    "fresnillo": "mx-zacatecas",
    "morelia": "mx-michoacan",
    "uruapan": "mx-michoacan",
    "apatzingan": "mx-michoacan",
    "ciudad lazaro cardenas": "mx-michoacan",
    "zamora de hidalgo": "mx-michoacan",
})

_ALL_REGIONS = frozenset(
    set(_CYCLONE_BY_REGION_MONTH) | set(_FLOOD_BY_REGION_MONTH)
    | set(_WILDFIRE_BY_REGION_MONTH) | set(_DROUGHT_BY_REGION_MONTH)
    | set(_DELAY_BY_REGION_MODE) | set(_SEISMIC_RESILIENCE_BY_REGION)
    | set(_CIVIL_UNREST_BY_REGION)
)


_MODES: frozenset[str] = frozenset({"flight", "rail", "bus"})


# ===========================================================================
# Module-load self-checks on the seed tables (fail loud at import).
# ===========================================================================

def _assert_seed_tables_valid() -> None:
    for region, by_month in _CYCLONE_BY_REGION_MONTH.items():
        for month, bp in by_month.items():
            if not (1 <= month <= 12):
                raise AssertionError(f"risk seed: cyclone month {month} for {region!r} out of 1–12")
            if not isinstance(bp, int) or not (0 <= bp <= 10000):
                raise AssertionError(f"risk seed: cyclone bp {bp} for {region!r}/{month} out of 0–10000")
    for region, by_month in _FLOOD_BY_REGION_MONTH.items():
        for month, bp in by_month.items():
            if not (1 <= month <= 12):
                raise AssertionError(f"risk seed: flood month {month} for {region!r} out of 1–12")
            if not isinstance(bp, int) or not (0 <= bp <= 10000):
                raise AssertionError(f"risk seed: flood bp {bp} for {region!r}/{month} out of 0–10000")
    for region, by_month in _WILDFIRE_BY_REGION_MONTH.items():
        for month, bp in by_month.items():
            if not (1 <= month <= 12):
                raise AssertionError(f"risk seed: wildfire month {month} for {region!r} out of 1–12")
            if not isinstance(bp, int) or not (0 <= bp <= 10000):
                raise AssertionError(f"risk seed: wildfire bp {bp} for {region!r}/{month} out of 0–10000")
    for region, by_month in _DROUGHT_BY_REGION_MONTH.items():
        for month, bp in by_month.items():
            if not (1 <= month <= 12):
                raise AssertionError(f"risk seed: drought month {month} for {region!r} out of 1–12")
            if not isinstance(bp, int) or not (0 <= bp <= 10000):
                raise AssertionError(f"risk seed: drought bp {bp} for {region!r}/{month} out of 0–10000")
    for region, by_mode in _DELAY_BY_REGION_MODE.items():
        for mode, mins in by_mode.items():
            if mode not in _MODES:
                raise AssertionError(f"risk seed: delay mode {mode!r} for {region!r} not in {sorted(_MODES)}")
            if not isinstance(mins, int) or mins < 0:
                raise AssertionError(f"risk seed: delay {mins} for {region!r}/{mode} must be a non-negative int")
    for region, score in _SEISMIC_RESILIENCE_BY_REGION.items():
        if not isinstance(score, int) or not (0 <= score <= 100):
            raise AssertionError(f"risk seed: seismic {score} for {region!r} out of 0–100")
    for region, bp in _CIVIL_UNREST_BY_REGION.items():
        if not isinstance(bp, int) or not (0 <= bp <= 10000):
            raise AssertionError(f"risk seed: civil_unrest bp {bp} for {region!r} out of 0–10000")
    for region in _ALL_REGIONS:
        if region not in _REGION_SOURCE:
            raise AssertionError(f"risk seed: region {region!r} has no provenance source")
    # D5 #27: every city must resolve to a SEEDED region (no resolved-but-unseeded
    # key), so assess_leg never hits the conservative-fallback for a typo/ordering
    # slip — caught loud at import, not silently degraded at runtime.
    unseeded = sorted(set(_CITY_TO_REGION.values()) - set(_ALL_REGIONS))
    if unseeded:
        raise AssertionError(
            f"risk seed: _CITY_TO_REGION maps to region(s) absent from _ALL_REGIONS "
            f"(resolved-but-unseeded): {unseeded}"
        )
    # audit #5: every seeded region must carry a seismic entry (no accidental
    # neutral-midpoint fallback for a fully-seeded region).
    no_seismic = sorted(set(_ALL_REGIONS) - set(_SEISMIC_RESILIENCE_BY_REGION))
    if no_seismic:
        raise AssertionError(
            f"risk seed: region(s) missing a seismic_resilience entry: {no_seismic}"
        )
    # D5 #38: every advisory CATEGORY must be a known cause keyed only to a real
    # L3/L4 region, so the categorization can never silently mis-route to WAR.
    _valid_cats = {"civil_unrest", "armed_conflict", "crime"}
    for region, cat in _ADVISORY_CATEGORY_BY_REGION.items():
        if cat not in _valid_cats:
            raise AssertionError(
                f"risk seed: advisory_category {cat!r} for {region!r} not in {sorted(_valid_cats)}"
            )
        # C2 single-authority: armed_conflict (the ONLY category that reaches
        # WAR/EXC-WAR-2 + implies a decline) is RESERVED for regions whose
        # country is a contracts.DO_NOT_RECOMMEND_COUNTRIES member. Reject any
        # armed_conflict seed for a bookable, non-member country loudly at import
        # so a conflict-adjacent country can never silently mis-route to WAR.
        if cat == "armed_conflict" and _region_iso2(region) not in DO_NOT_RECOMMEND_COUNTRIES:
            raise AssertionError(
                f"risk seed: armed_conflict category for {region!r} "
                f"(country {_region_iso2(region)!r}) is not a "
                f"DO_NOT_RECOMMEND_COUNTRIES member — armed_conflict is reserved "
                f"for the decline set (C2); use civil_unrest/advisory_elevated"
            )


_assert_seed_tables_valid()


# ===========================================================================
# Resolution + the deterministic decision (PURE functions; NO LLM, NO network).
# ===========================================================================

def _normalize_city_key(city: str) -> str:
    """Accent-fold + lowercase + hyphen/space-collapse a city name for the
    COUNTRY-QUALIFIED lookup only (málaga==malaga, são paulo==sao paulo,
    san-pedro==san pedro). NFC-normalize then strip combining marks (NFD
    decompose, drop combining chars), lowercase, collapse hyphens to spaces,
    then collapse repeated whitespace. This is intentionally a SEPARATE key
    convention from the bare _CITY_TO_REGION dict (which stays accent-PRESERVED,
    lower-only, unchanged — see #70's cebu-class normalization note) so existing
    bare-key callers see byte-identical behavior."""
    s = unicodedata.normalize("NFC", str(city))
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.strip().lower()
    s = s.replace("-", " ")
    s = " ".join(s.split())
    return s


# ---------------------------------------------------------------------------
# R1 fix (#236/#240 city→country mapping audit) — COUNTRY-QUALIFIED keying.
#
# THE BUG: _CITY_TO_REGION is keyed on a bare, un-country-qualified city name.
# Cities that share a name across two countries (homonyms — "san jose" is Costa
# Rica in the catalog but resolved to us-west; "cordoba" is Spain but resolved to
# Argentina's ar-pampas; "hamilton" is New Zealand but resolved to Canada's
# ca-east; "manzanillo" is Cuba but resolved to Mexico's mx-pacific; a 31-key
# Madrid/Barcelona cluster resolved to Mexico via a k-NN mis-anchor) silently
# inherited the WRONG country's climate/seismic/advisory data.
#
# THE FIX: a composite (ISO2, normalized-city) -> region structure, consulted
# FIRST when the caller supplies iso2. It is populated from two sources, hand
# overrides always winning (same setdefault-vs-explicit precedence pattern as
# the bare dict + osm_city_regions.json below):
#   1. society/city_region_by_country.json — MECHANICALLY derived (see
#      reference/seeding/ — the one-off migration script that produced it):
#      for every city that is UNAMBIGUOUS (exactly one country) across
#      ucp-merchant/catalog.json + catalog_supplement.json, its (iso2, city)
#      pair is safe to derive automatically from whatever region the bare key
#      CURRENTLY resolves to (no homonym conflict possible when there's only
#      one real country). This covers both the ~1,889 osm_city_regions.json
#      entries AND the plain single-country hand-authored entries.
#   2. The EXPLICIT hand overrides immediately below — the SPECIFIC confirmed
#      cross-country homonyms + known-bad single-country mis-seeds the #236
#      audit found. Every value here reuses an EXISTING region constant
#      already defined/used elsewhere in this file; none is invented.
# ---------------------------------------------------------------------------
_CITY_REGION_BY_COUNTRY: dict[str, dict[str, str]] = {}

try:
    _CITY_REGION_BY_COUNTRY_PATH = os.path.join(os.path.dirname(__file__), "..", "city_region_by_country.json")
    with open(_CITY_REGION_BY_COUNTRY_PATH, encoding="utf-8") as _f:
        for _city, _by_iso in json.load(_f).items():
            _bucket = _CITY_REGION_BY_COUNTRY.setdefault(_city, {})
            for _iso2, _region in _by_iso.items():
                _bucket.setdefault(_iso2.upper(), _region)
except (FileNotFoundError, json.JSONDecodeError, OSError):
    pass  # conservative: composite lookup degrades to the bare fallback, never crashes import

# Region-coverage gap backfill (2026-07-07, reference/seeding/backfill_regions.py)
# -- same k-NN + coastal-cyclone-override geographic assignment as the original
# osm_city_regions.json integration, but targeting the actively-growing Gemini-
# seeded lodging catalog (ucp-merchant/catalog_supplement.json), which had NEVER
# had a region-assignment step wired into its seeding pipeline. Loaded with
# setdefault RIGHT AFTER city_region_by_country.json (same precedence tier: a
# generated/inferred layer, never overriding anything already resolved) and
# BEFORE the hand-override loops below (those always win regardless). Composite
# (iso2, city) shape throughout -- never the bare osm_city_regions.json path --
# so this can't reintroduce the cross-country-homonym ambiguity the #236/#240
# fail-safe exists to catch.
try:
    _CATALOG_REGION_BACKFILL_PATH = os.path.join(os.path.dirname(__file__), "..", "catalog_region_backfill.json")
    with open(_CATALOG_REGION_BACKFILL_PATH, encoding="utf-8") as _f:
        for _city, _by_iso in json.load(_f).items():
            _bucket = _CITY_REGION_BY_COUNTRY.setdefault(_city, {})
            for _iso2, _region in _by_iso.items():
                _bucket.setdefault(_iso2.upper(), _region)
except (FileNotFoundError, json.JSONDecodeError, OSError):
    pass  # conservative: degrades to whatever resolves without it, never crashes import

# The set of NORMALIZED city names with 2+ distinct countries in the catalog
# (computed by the same migration script, BEFORE any hand-override exclusion —
# a city is ambiguous regardless of whether we chose to hand-fix it). Used only
# to decide whether a bare-key fallback hit deserves an "ambiguous" warning.
_AMBIGUOUS_CITIES: frozenset[str] = frozenset()
try:
    _CITY_REGION_AMBIGUOUS_PATH = os.path.join(os.path.dirname(__file__), "..", "city_region_ambiguous.json")
    with open(_CITY_REGION_AMBIGUOUS_PATH, encoding="utf-8") as _f:
        _AMBIGUOUS_CITIES = frozenset(json.load(_f))
except (FileNotFoundError, json.JSONDecodeError, OSError):
    pass


def _add_city_country_region(city: str, iso2: str, region: str) -> None:
    """Register/override an explicit (iso2, city) -> region pair. Hand overrides
    ALWAYS win over the mechanical JSON load — call this AFTER it (below)."""
    _CITY_REGION_BY_COUNTRY.setdefault(_normalize_city_key(city), {})[iso2.upper()] = region


for _city, _iso2, _region in [
    # Hamilton: catalog is New Zealand ONLY (20 listings); the bare hand seed
    # "hamilton" -> "ca-east" is simply wrong for every real instance. Reuse
    # auckland's existing North-Island region (Hamilton sits in the same North
    # Island climate zone as Auckland) — not a new region code.
    ("hamilton", "NZ", "nz-north"),
    # San José: catalog is Costa Rica ONLY (20 listings); the bare hand seed
    # wrongly said "us-west". Reuse the existing cr-central constant (already
    # used for costa rica / jaco / liberia / limon / quepos).
    ("san jose", "CR", "cr-central"),
    # Córdoba: catalog is Spain ONLY (14 listings); the bare hand seed wrongly
    # said "ar-pampas" (Argentina). Reuse es-east, the existing constant
    # already used for other southern-Spain cities (seville/málaga/granada).
    ("cordoba", "ES", "es-east"),
    # Victoria: catalog has Canada (22) + Hong Kong (26). ca-west is already
    # correct for the Canada instance; reuse hk (Hong Kong's own existing
    # constant) for the HK Victoria Harbour/Peak instance instead of leaving it
    # to inherit Canada's region. Seychelles/Malta have existing constants
    # (sc/mt) too — the catalog carries no rows under either yet, but wiring
    # them now means they resolve correctly the moment such rows appear rather
    # than silently inheriting ca-west.
    ("victoria", "CA", "ca-west"),
    ("victoria", "HK", "hk"),
    ("victoria", "SC", "sc"),
    ("victoria", "MT", "mt"),
    # Richmond: catalog has Canada/BC (22) + US/Virginia (18). us-northeast is
    # correct for the US case; ca-west (already used for vancouver/burnaby/
    # kelowna/surrey — all BC) is the correct existing constant for Richmond BC.
    ("richmond", "US", "us-northeast"),
    ("richmond", "CA", "ca-west"),
    # Salamanca: catalog has Spain (28, majority) + Mexico (1). The CURRENT bare
    # value ('mx', via osm k-NN) actually correctly describes the Mexico
    # instance — re-key it explicitly so it no longer also silently answers for
    # the much larger Spanish city. R1-followup: leaving the Spain instance on
    # the bare fallback does NOT degrade to "unknown" — it silently degrades to
    # MEXICO's region (the same wrong value being fixed here), which is worse
    # than conservative. Salamanca IS interior Castile-y-León Meseta — the same
    # broad continental-plateau climate zone as Madrid, not the Atlantic-coast
    # zone (es-cantabria: Galicia/Asturias/Cantabria/Basque) or the
    # Mediterranean-coast zone (es-east: Barcelona/Valencia/Sevilla). Reuse the
    # existing bare 'es' constant (already the exact value the Madrid-metro
    # cluster below resolves to) rather than leave 28 rows silently mis-served
    # Mexico's climate/seismic/advisory data.
    ("salamanca", "MX", "mx"),
    ("salamanca", "ES", "es"),
    # Salem: catalog has US (16) + India/Tamil Nadu (14). The current bare
    # value ('in-southeast', from the Tamil-Nadu flood-region split) correctly
    # describes the Indian city; re-key it explicitly. No confirmed-correct US
    # Salem region exists (could be MA or OR), so that instance is left to the
    # conservative (now-warned) fallback.
    ("salem", "IN", "in-southeast"),
    # Santa Maria: catalog has US/California (10) + Brazil/Rio Grande do Sul
    # (13, majority). The current bare value ('us-west') correctly describes
    # the Californian city; re-key it explicitly. No correct Brazil-SOUTH
    # region exists (br-southeast/br-pantanal are the wrong sub-region for Rio
    # Grande do Sul), so that instance is left to the conservative fallback.
    ("santa maria", "US", "us-west"),
    # Santa Rosa: catalog has Argentina/La Pampa (20, majority) + Philippines
    # (6). The current bare value ('ph') correctly describes the Philippines
    # instance; re-key it explicitly. Santa Rosa, Argentina IS the capital of
    # La Pampa province — squarely the existing ar-pampas region already used
    # for other Argentina cities — so wire that up too (found, not guessed).
    ("santa rosa", "PH", "ph"),
    ("santa rosa", "AR", "ar-pampas"),
    # Rosario: catalog has Argentina (8, majority) + Philippines (1). The
    # current bare value ('ar-pampas') correctly describes Argentina; re-key
    # explicitly. The Philippines instance: 'ph' is the SAME generic
    # Philippines bucket already used elsewhere (e.g. cebu) for a town without
    # its own sub-region — reuse it rather than an avoidable fallback.
    ("rosario", "AR", "ar-pampas"),
    ("rosario", "PH", "ph"),
    # Patan: catalog has Nepal (41, majority — Lalitpur/Patan, greater
    # Kathmandu) + India/Gujarat (4). The current bare value ('in') correctly
    # describes the Indian instance; re-key explicitly. Nepal's Patan clearly
    # belongs in the existing 'np' region already used for
    # kathmandu/pokhara/etc.
    ("patan", "IN", "in"),
    ("patan", "NP", "np"),
    # Orléans: catalog has France (38, majority) + Canada/Ontario (6 — Orléans
    # is a ward/suburb of Ottawa). The current bare value ('fr-north')
    # correctly describes France; re-key explicitly. Orléans ON is squarely
    # inside the existing ca-east region already used for ottawa itself.
    ("orleans", "FR", "fr-north"),
    ("orleans", "CA", "ca-east"),
    # San Pedro: catalog has Ivory Coast/San-Pédro (20, majority — the port
    # city) + Belize (2 — San Pedro, Ambergris Caye). The current bare value
    # ('bz') correctly describes Belize; re-key explicitly. Ivory Coast's
    # San-Pédro already has its OWN hand entry under the hyphenated key
    # "san-pedro" -> "ci" elsewhere in this file; the hyphen/space-collapsing
    # normalization above makes that the exact match for a "san pedro" query
    # too, so reuse 'ci' directly.
    ("san pedro", "BZ", "bz"),
    ("san pedro", "CI", "ci"),
    # Nelson / Antigua — "pre-loaded landmines": the catalog has NO rows under
    # either bare key today (in EITHER country), so there is nothing ambiguous
    # to warn about YET. Re-key now so that when Nelson BC/UK or Caribbean
    # Antigua eventually get seeded under the same bare name, they correctly
    # fall to the conservative-unknown fallback instead of silently inheriting
    # these values.
    ("nelson", "NZ", "nz-south"),
    ("antigua", "GT", "gt-highlands"),
    # Manzanillo: catalog is Cuba ONLY (4 listings); the bare hand seed wrongly
    # said 'mx-pacific' (Mexico). Reuse the existing 'cu' constant (already
    # used for havana and every other Cuban city in this file).
    ("manzanillo", "CU", "cu"),
    # George / George Town: catalog's "george town" city string is a 3-way
    # collision — South Africa (George, Western Cape — mis-tagged with a "town"
    # suffix), Bahamas (George Town, Exuma — genuinely correct), and Guyana
    # (Georgetown, written WITH a space in a subset of rows; the majority of
    # Guyana rows use the one-word "georgetown" key and already resolve
    # correctly via the existing 'gy' hand entry). Merge all three onto their
    # correct EXISTING constants — za-cape (george's own resolved value via the
    # za-cape flood-region split), bs (george town's own current, already-
    # correct for Bahamas, hand value), and gy (georgetown's own hand value) —
    # nothing invented.
    ("george town", "ZA", "za-cape"),
    ("george town", "BS", "bs"),
    ("george town", "GY", "gy"),
    # Spain's Madrid-metro districts + satellite municipalities (18 keys) and
    # Barcelona-metro districts + satellite municipalities (12 keys), plus
    # Sevilla: all 31 are Spain-ONLY in the catalog (zero Mexico rows under any
    # of these names) but osm_city_regions.json's k-NN accidentally anchored
    # every one of them to a Mexican region ('mx'). Re-key each explicitly to
    # whichever existing Spain constant its own hub city already uses: Madrid
    # itself resolves to bare 'es'; Barcelona/Seville resolve to 'es-east'.
    ("arganzuela", "ES", "es"), ("carabanchel", "ES", "es"), ("chamartin", "ES", "es"),
    ("chamberi", "ES", "es"), ("ciudad lineal", "ES", "es"), ("fuencarral", "ES", "es"),
    ("fuencarral-el pardo", "ES", "es"), ("hortaleza", "ES", "es"), ("latina", "ES", "es"),
    ("leganes", "ES", "es"), ("moncloa-aravaca", "ES", "es"), ("moratalaz", "ES", "es"),
    ("puente de vallecas", "ES", "es"), ("retiro", "ES", "es"), ("san blas-canillejas", "ES", "es"),
    ("tetuan de las victorias", "ES", "es"), ("usera", "ES", "es"), ("villaverde", "ES", "es"),
    ("badalona", "ES", "es-east"), ("ciutat vella", "ES", "es-east"), ("eixample", "ES", "es-east"),
    ("gracia", "ES", "es-east"), ("horta-guinardo", "ES", "es-east"),
    ("l'hospitalet de llobregat", "ES", "es-east"), ("nou barris", "ES", "es-east"),
    ("sant andreu", "ES", "es-east"), ("sant marti", "ES", "es-east"),
    ("santa coloma de gramenet", "ES", "es-east"), ("sants-montjuic", "ES", "es-east"),
    ("sarria-sant gervasi", "ES", "es-east"), ("sevilla", "ES", "es-east"),
]:
    _add_city_country_region(_city, _iso2, _region)


# R2 follow-up (#236/#240, previously tracked as #242 residuals) — 13
# mechanically-derived composite entries that inherited a pre-existing WRONG
# bare seed for a single-country city (no homonym conflict, just a bad k-NN/
# hand seed carried forward verbatim by the mechanical derivation script).
# Re-key each explicitly to an EXISTING region constant, same pattern as the
# hamilton/san jose/cordoba/manzanillo block above; nothing invented. These
# were previously enumerated as _KNOWN_RESIDUAL_MISMATCHES in
# test_city_country_region_invariant.py — fixing them here should let that
# forgiveness list shrink (an accidental-correct fix there is harmless).
for _city, _iso2, _region in [
    # Abbotsford/Chilliwack/Whalley/Delta/Langley/Saanich/Coquitlam/Newton are
    # all Metro Vancouver / Fraser Valley / Greater Victoria, British Columbia
    # — Canada's WEST coast, not the us-northeast the bare seed wrongly said.
    # Reuse ca-west, the existing constant already used for BC (victoria,
    # vancouver, etc.).
    ("abbotsford", "CA", "ca-west"), ("chilliwack", "CA", "ca-west"),
    ("whalley", "CA", "ca-west"), ("delta", "CA", "ca-west"),
    ("langley", "CA", "ca-west"), ("saanich", "CA", "ca-west"),
    ("coquitlam", "CA", "ca-west"), ("newton", "CA", "ca-west"),
    # Puerto Iguazú: catalog is Argentina ONLY; the bare seed wrongly said
    # 'br-southeast' (Brazil — the falls' OTHER bank). Reuse ar-pampas, the
    # only existing Argentina constant in this file.
    ("puerto iguazu", "AR", "ar-pampas"), ("puerto iguazú", "AR", "ar-pampas"),
    # Santa Maria: catalog is Brazil (Rio Grande do Sul) ONLY; the bare seed
    # wrongly said 'us-west'. Reuse br-southeast, the existing Brazil constant.
    ("santa maria", "BR", "br-southeast"),
    # Madhyapur Thimi: catalog is Nepal (greater Kathmandu) ONLY; the bare seed
    # wrongly said 'in' (India). Reuse 'np', the existing Nepal constant.
    ("madhyapur thimi", "NP", "np"),
    # Salem: catalog's US rows are Salem, Oregon (city_coords.json's own hand
    # coordinate for the bare "salem" key is 44.94/-123.04, Oregon's capital);
    # the bare seed wrongly said 'in-southeast' (India's Salem, Tamil Nadu —
    # itself a separate, correct hand entry). Reuse us-pacnw, the existing
    # Pacific-Northwest constant.
    ("salem", "US", "us-pacnw"),
]:
    _add_city_country_region(_city, _iso2, _region)


# Task-1 sweep (2026-07-06) — city_region_by_country.json's MECHANICAL
# derivation independently disagreed with the correct (verified) sub-region
# for these 6 single-country cities (same root cause as the bare-key
# duplicate-spelling fixes just above: the mechanical script anchored to the
# less-precise/wrong entry). Without these hand overrides, a caller that DOES
# pass the right iso2 would still get the wrong answer via the composite path
# (region_for_city checks the composite table FIRST, before ever reaching the
# bare fallback this file just corrected). Re-key each to the same verified
# constant used above; nothing invented.
for _city, _iso2, _region in [
    ("fussen", "DE", "de-bavaria"),
    ("hofn", "IS", "is-south"),
    ("nimes", "FR", "fr-south"),
    ("oswiecim", "PL", "pl-krakow"),
    ("puerto jimenez", "CR", "cr-osa"),
    ("vik", "IS", "is-south"),
]:
    _add_city_country_region(_city, _iso2, _region)


# Region-coverage gap sweep (2026-07-07): the daily Gemini-seeded lodging catalog
# (ucp-merchant/catalog_supplement.json) grew from a 5551-city baseline to 12000+
# cities with NO region-assignment step ever wired into that pipeline (confirmed:
# zero region-related code in scripts/seed_city_data_vertex.py) -- 39.8% of its
# (city, country) pairs resolved to no region at all. Six of the affected
# countries already have a correctly-authored country-level region constant
# (with real cyclone/seismic data) but had ZERO catalog city anchored to it, so
# reference/seeding/backfill_regions.py's same-country k-NN propagation had
# nothing to propagate FROM. One capital/prefecture-city anchor per country is
# enough to unblock the whole country -- zero new hazard authoring, the region
# profile was already correct, just unreachable.
for _city, _iso2, _region in [
    ("noumea", "NC", "nc"),
    ("montevideo", "UY", "uy"),
    ("luxembourg", "LU", "lu"),
    ("maseru", "LS", "ls"),
    ("saint-denis", "RE", "re"),
    ("mamoudzou", "YT", "yt"),
]:
    _add_city_country_region(_city, _iso2, _region)


def region_for_city(city: str | None, iso2: str | None = None) -> str | None:
    """Resolve a city (+ optional ISO2 country) → seeded region key, or None
    (UNKNOWN). Deterministic.

    Lookup order:
      1. Exact composite (iso2, normalized-city) match, if iso2 is given — the
         country-qualified path (R1 fix, #236/#240): accent-folded,
         hyphen/space-collapsed (málaga==malaga, são paulo==sao paulo,
         san-pedro==san pedro).
      2. Otherwise (no iso2 given, or no composite match): fall back to the
         bare-city-key table. If that bare fallback resolves and either (a)
         the city is a KNOWN cross-country homonym (2+ distinct countries in
         the catalog), or (b) a known-good composite override exists for this
         city (single-country or not) whose region DISAGREES with the bare
         result — meaning the bare seed itself is a confirmed mis-route (the
         hamilton/san jose/cordoba/manzanillo class, #236/#240 follow-up) —
         this is a KNOWN-WRONG-OR-UNCERTAIN answer for whatever real country
         this particular call actually means, so it is FAILED SAFE to None
         (never silently served) rather than returned. A warning is logged
         (never raised) so the miss stays visible. #236/#240/Task-1 (2026-07-
         06): silently returning a value the code ITSELF already knows may be
         wrong for this city is worse than an honest UNKNOWN — assess_leg's
         UNKNOWN-region path always degrades to a conservative FLAG, never a
         silent "safe", so failing safe here can only ever raise caution, not
         lower it.
    """
    if not city:
        return None
    city_str = str(city).strip()
    norm = _normalize_city_key(city_str)
    if iso2:
        by_country = _CITY_REGION_BY_COUNTRY.get(norm)
        if by_country:
            hit = by_country.get(str(iso2).strip().upper())
            if hit:
                return hit
    result = _CITY_TO_REGION.get(city_str.lower())
    if result is not None:
        ambiguous = norm in _AMBIGUOUS_CITIES
        # Single-country (or any) known-wrong bare seed: a hand/mechanical
        # composite override exists for this city whose region differs from
        # what the bare fallback just returned. This catches wrong-seed cities
        # that AREN'T cross-country homonyms (e.g. hamilton is NZ-only in the
        # catalog, but the bare seed wrongly said Canada) — previously these
        # returned the wrong region with zero diagnostic trace.
        override_mismatch = any(
            region != result for region in _CITY_REGION_BY_COUNTRY.get(norm, {}).values()
        )
        if ambiguous or override_mismatch:
            logger.warning(
                "risk.region_for_city: %s bare-key fallback for city=%r (iso2=%r) — "
                "REFUSING to silently serve region=%r, which is known to be WRONG "
                "for at least one of this city's real catalog countries and no "
                "matching country-qualified override resolved it here. Failing "
                "safe to UNKNOWN (assess_leg treats UNKNOWN as a conservative "
                "flag, never a silent 'safe') — pass iso2 to region_for_city to "
                "disambiguate and get the precise answer.",
                "ambiguous" if ambiguous else "known-mismatched",
                city_str, iso2, result,
            )
            return None
    return result


# ---------------------------------------------------------------------------
# #15 — OSM-integrated bookable cities: k-NN hazard-region propagation (data file).
# Each city promoted from the OSM lodging harvest (reference/integrate_lodging_all.py)
# is assigned the hazard region of the geographically NEAREST hand-authored city
# IN-COUNTRY. The hazard PROFILES stay hand-authored; only the city→region
# geographic assignment is computed (hazard zones are geographic). setdefault →
# a hand-authored seed ALWAYS wins over the inferred one. Keyed by
# city.strip().lower() (the exact key region_for_city + the coverage gate use).
# Regenerate: reference/integrate_lodging_all.py --write.
# ---------------------------------------------------------------------------
try:
    _OSM_CITY_REGIONS_PATH = os.path.join(os.path.dirname(__file__), "..", "osm_city_regions.json")
    with open(_OSM_CITY_REGIONS_PATH, encoding="utf-8") as _f:
        for _city, _region in json.load(_f).items():
            _CITY_TO_REGION.setdefault(_city, _region)
except (FileNotFoundError, json.JSONDecodeError, OSError):
    pass  # NIT#2: a truncated/partial JSON fails CONSERVATIVE (hand seed stands), never crashes import


def _month_of(date_str: str | None) -> int | None:
    """Extract the 1–12 month from a YYYY-MM-DD string, or None."""
    if isinstance(date_str, str) and len(date_str) >= 7:
        try:
            m = int(date_str[5:7])
            return m if 1 <= m <= 12 else None
        except ValueError:
            return None
    return None


def _months_in_span(checkin: str | None, checkout: str | None) -> list[int]:
    """Enumerate EVERY calendar month (1–12) the stay [checkin, checkout] spans,
    handling year-rollover (Dec→Jan) — not just the two endpoint months (D5 #3).

    A multi-month stay that straddles a seasonal PEAK (e.g. a Jun→Nov window
    through September's hurricane peak) must SEE the interior peak, never just the
    quieter endpoints. Returns a sorted, de-duplicated list of months actually
    covered. Falls back to the conservative all-12-months worst case when dates
    are absent/unparseable OR when the span cannot be reconstructed (checkout
    before checkin, or a span longer than a year) — same principle as the
    no-dates and unknown-region branches: never silently claim off-season safety.
    """
    mi = _month_of(checkin)
    mo = _month_of(checkout)
    if mi is None or mo is None:
        return list(range(1, 13))
    # Use the YEARS too (when present) so a multi-year/year-rollover span is
    # walked correctly; if we cannot read the years, fall back to month-only
    # rollover (Dec→Jan) capped at 12 distinct months.
    yi = _year_of(checkin)
    yo = _year_of(checkout)
    if yi is not None and yo is not None:
        total = (yo - yi) * 12 + (mo - mi)
        if total < 0 or total > 12:
            # checkout before checkin, or > 1 year → cannot trust the window.
            return list(range(1, 13))
        months = sorted({((mi - 1 + k) % 12) + 1 for k in range(total + 1)})
        return months
    # No reliable years: walk forward month-by-month with Dec→Jan rollover.
    months_set: set[int] = set()
    m = mi
    for _ in range(12):  # at most a full year of distinct months
        months_set.add(m)
        if m == mo:
            break
        m = m % 12 + 1
    return sorted(months_set)


def _year_of(date_str: str | None) -> int | None:
    """Extract the YYYY year from a YYYY-MM-DD string, or None."""
    if isinstance(date_str, str) and len(date_str) >= 4:
        try:
            return int(date_str[0:4])
        except ValueError:
            return None
    return None


def fetch_region_profile(
    region: str | None,
    *,
    simulate_source_unreachable: bool = False,
) -> tuple[dict[str, Any] | None, str]:
    """
    Resolve the seeded hazard profile for a region (Tier-1 cache today).

    Returns (profile_or_None, source_freshness). profile is
    {cyclone_by_month, delay_by_mode, seismic_resilience, source}.
    `simulate_source_unreachable=True` exercises the Tier-3 exhausted path → the
    seeded values tagged SEEDED_FALLBACK (terms NOT fabricated), or None if the
    region is unknown.
    """
    if region is None or region not in _ALL_REGIONS:
        return None, "SEEDED"
    freshness = "SEEDED_FALLBACK" if simulate_source_unreachable else "SEEDED"
    if simulate_source_unreachable:
        logger.warning(
            "risk: hazard source unreachable (Tier-3 exhausted) → SEEDED_FALLBACK "
            "for region %s (values NOT fabricated)", region,
        )
    return (
        {
            "cyclone_by_month": _CYCLONE_BY_REGION_MONTH.get(region, {}),
            "flood_by_month": _FLOOD_BY_REGION_MONTH.get(region, {}),
            "wildfire_by_month": _WILDFIRE_BY_REGION_MONTH.get(region, {}),
            "drought_by_month": _DROUGHT_BY_REGION_MONTH.get(region, {}),
            "delay_by_mode": _DELAY_BY_REGION_MODE.get(region, {}),
            "seismic_resilience": _SEISMIC_RESILIENCE_BY_REGION.get(region),
            "civil_unrest_bp": max(_CIVIL_UNREST_BY_REGION.get(region, 0),
                                   _advisory_civil_unrest_bp(region)),
            # D5: raw advisory LEVEL bp + its CAUSE category, so the derived
            # reason-code is honest (crime/armed-conflict/generic, not blanket
            # civil_unrest).
            "advisory_level_bp": _advisory_level_bp(region),
            "advisory_category": _advisory_category(region),
            "source": _REGION_SOURCE.get(region, "seed:society-region-profile-2026"),
        },
        freshness,
    )


def cyclone_likelihood_public(city: str, month: int, iso2: str | None = None) -> dict[str, Any] | None:
    """
    PUBLIC seeded accessor (the fair-baseline tool uses this — identical data).

    Return the seeded cyclone/typhoon likelihood for (city, month) as the SAME
    facts the society resolves: {city, region, month, likelihood_bp, likelihood_pct,
    source}. Returns None for an UNKNOWN city (the baseline sees the same gap the
    society does — no fabrication). Deterministic.

    `iso2` (optional, APPEND-ONLY, #236/#240 R2): country-qualify the region
    lookup when the caller has it. Omitting it is byte-identical to before —
    the fair-baseline harness deliberately calls this city-only (a naive
    competitor tool wouldn't get a country either), so this stays optional.
    """
    region = region_for_city(city, iso2)
    if region is None:
        return None
    by_month = _CYCLONE_BY_REGION_MONTH.get(region, {})
    bp = int(by_month.get(int(month), 0))
    return {
        "city": str(city).strip().lower(),
        "region": region,
        "month": int(month),
        "likelihood_bp": bp,
        "likelihood_pct": round(bp / 100, 1),
        "source": _REGION_SOURCE.get(region, "seed:society-region-profile-2026"),
    }


def cyclone_calendar_public(city: str, iso2: str | None = None) -> dict[str, Any] | None:
    """
    PUBLIC seeded accessor: the FULL 12-month cyclone likelihood calendar for a
    city's region (so the fair baseline can see every month, exactly as the
    society's seed). Returns None for an UNKNOWN city. Deterministic.

    `iso2` (optional, APPEND-ONLY, #236/#240 R2): see cyclone_likelihood_public.
    """
    region = region_for_city(city, iso2)
    if region is None:
        return None
    by_month = _CYCLONE_BY_REGION_MONTH.get(region, {})
    return {
        "city": str(city).strip().lower(),
        "region": region,
        "monthly_likelihood_pct": {
            str(m): round(by_month.get(m, 0) / 100, 1) for m in range(1, 13)
        },
        "source": _REGION_SOURCE.get(region, "seed:society-region-profile-2026"),
    }


def flood_likelihood_public(city: str, month: int, iso2: str | None = None) -> dict[str, Any] | None:
    """
    PUBLIC seeded accessor (the fair-baseline flood tool uses this — identical
    data the society's Risk agent resolves). Returns the seasonal flood-risk index
    for (city, month) or None for an UNKNOWN city (same gap the society sees).
    Deterministic.

    `iso2` (optional, APPEND-ONLY, #236/#240 R2): see cyclone_likelihood_public.
    """
    region = region_for_city(city, iso2)
    if region is None:
        return None
    by_month = _FLOOD_BY_REGION_MONTH.get(region, {})
    bp = int(by_month.get(int(month), 0))
    return {
        "city": str(city).strip().lower(),
        "region": region,
        "month": int(month),
        "flood_index_bp": bp,
        "flood_index_pct": round(bp / 100, 1),
        "source": _REGION_SOURCE.get(region, "seed:society-region-profile-2026"),
    }


def flood_calendar_public(city: str, iso2: str | None = None) -> dict[str, Any] | None:
    """
    PUBLIC seeded accessor: the FULL 12-month seasonal flood-risk calendar for a
    city's region (so the fair baseline sees every month, exactly as the society's
    seed). Returns None for an UNKNOWN city. Deterministic.

    `iso2` (optional, APPEND-ONLY, #236/#240 R2): see cyclone_likelihood_public.
    """
    region = region_for_city(city, iso2)
    if region is None:
        return None
    by_month = _FLOOD_BY_REGION_MONTH.get(region, {})
    return {
        "city": str(city).strip().lower(),
        "region": region,
        "monthly_flood_index_pct": {
            str(m): round(by_month.get(m, 0) / 100, 1) for m in range(1, 13)
        },
        "source": _REGION_SOURCE.get(region, "seed:society-region-profile-2026"),
    }


def _provenance(source: str, tier: str = SourceTier.SEEDED.value) -> dict[str, Any]:
    """Canonical provenance envelope for a seeded risk signal."""
    return make_provenance(
        source=source,
        fetched_at=_SEED_FETCHED_AT,
        tier=tier,
        source_url=None,
        ttl=_SEED_TTL_SECONDS,
    )


def assess_leg(
    *,
    city: str | None,
    iso2: str | None = None,
    checkin: str | None = None,
    checkout: str | None = None,
    mode: str = "flight",
    simulate_source_unreachable: bool = False,
    today: str | None = None,
) -> dict[str, Any]:
    """
    Assess ONE leg → per-leg risk signals + the deterministic avoid/buffer/flag
    DECISION.

    PURE + DETERMINISTIC (same inputs → byte-identical output; NO-LLM-NUMBERS).
    UNKNOWN region → conservative FLAG (buffer + unknown advisory), never a
    silent "safe". `mode` selects the delay lane (flight/rail/bus); unknown mode

    `iso2` (optional, APPEND-ONLY, #236/#240 R2 wiring fix): the leg's country,
    threaded straight to region_for_city so the country-qualified composite
    lookup is actually reached — without it the cross-country homonym fix
    (R1) never fires on this path. Omitting it is byte-identical to before
    (bare-key fallback, with an ambiguity warning where applicable).

    `today` (optional, APPEND-ONLY): the caller's deterministic "today" (ISO
    date), used ONLY for TTL enforcement — if the region hazard profile's own
    fetched_at+ttl freshness window has elapsed as of `today`, a stale-data
    FLAG advisory is added (never silently served as current). Omitting it
    (the default) skips the TTL check entirely — byte-identical to pre-TTL
    behaviour for any existing caller that doesn't thread a clock in.
    falls back to "flight".
    """
    region = region_for_city(city, iso2)
    mode_key = mode if mode in _MODES else "flight"
    # EVERY month the stay spans (checkin→checkout, year-rollover handled), so an
    # interior-month seasonal PEAK is never missed (D5 #3). Dates absent /
    # unparseable / unreconstructable span → conservative all-12-months worst
    # case (never silently claim off-season safety for an unknown window).
    months = _months_in_span(checkin, checkout)

    advisory: list[dict[str, Any]] = []
    reason_codes: list[str] = []

    # --- UNKNOWN region → conservative flag (never silent-safe) ---------------
    # Covers BOTH an unmapped city (region is None) AND a city that resolves to a
    # region key absent from every seed table (profile is None) — D5 #27: a
    # resolved-but-UNSEEDED region must degrade to the SAME conservative flag, not
    # TypeError on profile["source"]. (Belt-and-braces with the import-time assert
    # set(_CITY_TO_REGION.values()) <= _ALL_REGIONS.)
    profile = None
    freshness = "SEEDED"
    if region is not None:
        profile, freshness = fetch_region_profile(
            region, simulate_source_unreachable=simulate_source_unreachable
        )
    if profile is None:
        prof_source = "seed:risk-no-profile"
        decisions = {
            "avoid_window": False,           # cannot assert avoid without data…
            "buffer_connection_min": _UNKNOWN_BUFFER_MIN,  # …but DO buffer conservatively
            "flag": True,                    # and ALWAYS flag (no silent safe)
        }
        if region is None:
            unk_detail = (
                f"No seeded hazard profile for city {city!r}; risk signals are "
                f"UNKNOWN. Treating conservatively: connection buffer applied and "
                f"the leg flagged for manual review (never silently marked safe)."
            )
        else:
            unk_detail = (
                f"City {city!r} resolves to region {region!r}, which has NO seeded "
                f"hazard profile; risk signals are UNKNOWN. Treating conservatively: "
                f"connection buffer applied and the leg flagged for manual review "
                f"(never silently marked safe)."
            )
        advisory.append({
            "type": "unknown_region",
            "severity": "flag",
            "detail": unk_detail,
            "provenance": _provenance(prof_source),
        })
        # D5 #44: emit a CONSERVATIVE reason code so the flag PROPAGATES through
        # the peril crosswalk (an unknown leg must still surface a peril signal,
        # not silently map to none). ADVISORY_ELEVATED = generic elevated caution
        # (benign proxy, NOT war); TRANSPORT_DISRUPTION mirrors the buffer we
        # applied. Stable, de-duped order.
        return {
            "city": city,
            "region": region,
            "mode": mode_key,
            "cyclone_likelihood_bp": None,
            "cyclone_likelihood_pct": None,
            "flood_index_bp": None,
            "wildfire_likelihood_bp": None,
            "wildfire_likelihood_pct": None,
            "drought_index_bp": None,
            "median_delay_min": _UNKNOWN_BUFFER_MIN,
            "seismic_resilience": _UNKNOWN_SEISMIC,
            # #68: the unknown-region leg MUST carry the alert fields too (the most
            # conservative leg can't be the one silently missing the signal). We do
            # NOT claim a HIGH/MED/LOW — the tier is genuinely UNKNOWN (no profile),
            # surfaced honestly (fail-conservative, never a false 'none'/'low').
            "alert_tier": "UNKNOWN",
            "occurrence": {
                "max_likelihood_pct": None,
                "band": "unknown",
                "basis": "no seeded hazard profile for this region — treated conservatively (manual review)",
            },
            "advisory": advisory,
            "reason_codes": [_ADVISORY_ELEVATED_CODE,
                             RiskReasonCode.TRANSPORT_DISRUPTION.value],
            "decisions": decisions,
            "source_freshness": "SEEDED",
            "provenance": _provenance(prof_source),
            "planning_note": None,
        }

    tier = SourceTier.SEEDED.value if freshness == "SEEDED" else SourceTier.EXHAUSTED.value
    src = profile["source"]

    # --- (a) Cyclone likelihood (max over the spanned months) ----------------
    cyc_by_month: dict[int, int] = profile["cyclone_by_month"]
    cyclone_bp = max((cyc_by_month.get(m, 0) for m in months), default=0)
    # #51-a ENSO: capped multiplier on the in-season signal (no-op when phase is
    # neutral / region has no basin → byte-identical, var-0). Never invents 0→nonzero.
    cyclone_bp = _enso_modulate(cyclone_bp, region, months, "cyclone")
    avoid_window = cyclone_bp >= CYCLONE_AVOID_BP
    cyclone_flag = cyclone_bp >= CYCLONE_FLAG_BP
    if cyclone_flag:
        reason_codes.append(RiskReasonCode.NATURAL_DISASTER.value)
        advisory.append({
            "type": "cyclone_window",
            "severity": "high" if avoid_window else "medium",
            "detail": (
                f"{_cyclone_basin_noun(src, region)} likelihood ~{cyclone_bp / 100:.0f}% for the "
                f"travel window (months {sorted(set(months))}) in region {region}. "
                + ("AVOID this window — shift dates or prefer storm-resilient, "
                   "flexible-cancellation facilities." if avoid_window
                   else "Elevated — flag and prefer flexible-cancellation facilities.")
            ),
            "provenance": _provenance(src, tier),
        })

    # --- (a.2) Flood (seasonal climatology; max over the spanned months) ------
    # Mirrors cyclone exactly: CONTEXTUAL (silent for regions/months with no
    # seeded flood season) and month-conditional (discriminates by the trip's
    # actual season, never an always-on standing flag).
    flood_by_month: dict[int, int] = profile.get("flood_by_month", {})
    flood_bp = max((flood_by_month.get(m, 0) for m in months), default=0)
    # #51-a ENSO: wet-phase amplification of the in-season flood signal (no-op for
    # neutral phase / unseeded basin → byte-identical, var-0; never invents 0→nonzero).
    flood_bp = _enso_modulate(flood_bp, region, months, "flood")
    flood_avoid = flood_bp >= FLOOD_AVOID_BP
    if flood_bp >= FLOOD_FLAG_BP:
        avoid_window = avoid_window or flood_avoid
        reason_codes.append(RiskReasonCode.FLOOD.value)
        advisory.append({
            "type": "flood_season",
            "severity": "high" if flood_avoid else "medium",
            "detail": (
                f"Seasonal flood-risk index ~{flood_bp / 100:.0f}% for the travel "
                f"window (months {sorted(set(months))}) in region {region}. "
                + ("AVOID this window — prefer flexible-cancellation and lodging on "
                   "higher floors away from flood-prone low-lying/riverbank areas."
                   if flood_avoid else
                   "Elevated — flag and prefer flexible-cancellation facilities.")
            ),
            "provenance": _provenance(src, tier),
        })

    # --- (a.3) Wildfire (seasonal fire climatology; max over spanned months) ---
    # Mirrors cyclone EXACTLY: CONTEXTUAL (silent for regions/months with no
    # seeded fire season) and month-conditional (discriminates by the trip's
    # actual season, never an always-on standing flag). The signal is a SEASONAL
    # likelihood index, NOT a live active-fire detection (EFFIS/GWIS = Tier-2
    # swap) — so the advisory is routine-precaution oriented, never an evacuation
    # order. We track whether wildfire fired (and its advisory index) so the
    # drought block below can RAISE its severity on the drought→wildfire compound.
    wildfire_by_month: dict[int, int] = profile.get("wildfire_by_month", {})
    wildfire_bp = max((wildfire_by_month.get(m, 0) for m in months), default=0)
    wildfire_avoid = wildfire_bp >= WILDFIRE_AVOID_BP
    wildfire_fired = wildfire_bp >= WILDFIRE_FLAG_BP
    wildfire_adv: dict[str, Any] | None = None
    if wildfire_fired:
        avoid_window = avoid_window or wildfire_avoid
        reason_codes.append(RiskReasonCode.NATURAL_DISASTER.value)
        wildfire_adv = {
            "type": "wildfire_season",
            "severity": "high" if wildfire_avoid else "medium",
            "detail": (
                f"Seasonal wildfire likelihood ~{wildfire_bp / 100:.0f}% for the "
                f"travel window (months {sorted(set(months))}) in region {region}. "
                + ("AVOID this window — " if wildfire_avoid else "Elevated — ")
                + "Take routine-travel precautions; during ACTIVE fire emergencies "
                "adhere to evacuation declarations and monitor official alerts "
                "(EFFIS/local). Smoke may disrupt outdoor activities; prefer "
                "flexible-cancellation."
            ),
            "provenance": _provenance(src, tier),
        }
        advisory.append(wildfire_adv)

    # --- (a.4) Drought (seasonal dry-season index; max over spanned months) ----
    # ADVISORY-ONLY: drought never sets avoid_window — it surfaces PLANNING
    # impacts, not a do-not-travel. Uses the GENERIC elevated advisory reason code
    # (the same _ADVISORY_ELEVATED_CODE the unknown-region / generic-advisory paths
    # use), NOT NATURAL_DISASTER. CONTEXTUAL: silent for unseeded regions/months.
    drought_by_month: dict[int, int] = profile.get("drought_by_month", {})
    drought_bp = max((drought_by_month.get(m, 0) for m in months), default=0)
    # #51-a ENSO: dry-phase amplification of the in-season drought signal (no-op for
    # neutral phase / unseeded basin → byte-identical, var-0; never invents 0→nonzero).
    drought_bp = _enso_modulate(drought_bp, region, months, "drought")
    drought_fired = drought_bp >= DROUGHT_FLAG_BP
    if drought_fired:
        reason_codes.append(_ADVISORY_ELEVATED_CODE)
        advisory.append({
            "type": "drought",
            "severity": "medium",
            "detail": (
                f"Severe drought index ~{drought_bp / 100:.0f}% for the travel "
                f"window (months {sorted(set(months))}) in region {region}. "
                "Not a do-not-travel, but plan around documented impacts: "
                "(1) WATER RESTRICTIONS — hotels may ban pool-filling, restrict "
                "tap use, and limit garden/golf irrigation; "
                "(2) TRANSIT / ACTIVITY LIMITS — low water levels can curtail "
                "river cruises, ferries, and water sports; "
                "(3) ELEVATED WILDFIRE RISK + hazardous smoke — dry conditions "
                "compound fire danger and air quality; "
                "(4) HIGHER LOCAL FOOD PRICES — harvest shortfalls raise costs. "
                "Prefer flexible-cancellation."
            ),
            "provenance": _provenance(src, tier),
        })
        # DROUGHT→WILDFIRE compounding: if BOTH fired this leg, dry conditions
        # amplify fire risk → raise the wildfire advisory to high severity.
        if wildfire_adv is not None:
            wildfire_adv["severity"] = "high"

    # --- (b) Median delay → buffer connections -------------------------------
    # D5 #6: a KNOWN region that LACKS a delay sub-table (empty delay_by_mode) has
    # UNKNOWN delay — we must NOT silently resolve it to 0 (no buffer, no signal),
    # which understates a real hazard for ~2/3 of seeded regions. Mirror the
    # unknown-REGION treatment: fall back to the conservative _UNKNOWN_BUFFER_MIN
    # (=DELAY_BUFFER_THRESHOLD_MIN=45) and emit TRANSPORT_DISRUPTION with a
    # "delay data missing" advisory. A PRESENT sub-table with a 0 for the selected
    # mode (a genuine N/A, e.g. "no rail in a remote region") still falls back to
    # the seeded flight value — that is real data, not a missing table.
    delay_by_mode: dict[str, int] = profile["delay_by_mode"]
    delay_data_missing = not delay_by_mode
    if delay_data_missing:
        median_delay = _UNKNOWN_BUFFER_MIN
    else:
        median_delay = int(delay_by_mode.get(mode_key, delay_by_mode.get("flight", 0)))
    buffer_connection_min = (
        max(median_delay, DELAY_BUFFER_THRESHOLD_MIN)
        if median_delay >= DELAY_BUFFER_THRESHOLD_MIN else 0
    )
    if buffer_connection_min:
        # A PRESENT seeded delay ≥ threshold is a KNOWN destination transport
        # hazard → raise the TRANSPORT_DISRUPTION reason code. A MISSING delay
        # sub-table is a data-coverage GAP, not a known hazard: we STILL buffer
        # connections conservatively (never silently assume on-time, #6) and
        # surface an advisory — but we do NOT tag it as a destination hazard.
        # Tagging data-absence would fire the code for ~2/3 of seeded regions and
        # destroy the DISCRIMINATION signal (the code would no longer distinguish
        # real transport risk). The buffer + advisory carry the conservatism; the
        # reason_code carries the (selective) hazard taxonomy.
        if not delay_data_missing:
            reason_codes.append(RiskReasonCode.TRANSPORT_DISRUPTION.value)
            detail = (
                f"Median {mode_key} delay ~{median_delay} min in region {region} "
                f"(≥{DELAY_BUFFER_THRESHOLD_MIN} min threshold). Buffer connections "
                f"by {buffer_connection_min} min."
            )
            severity = "medium"
        else:
            detail = (
                f"No seeded median-delay data for region {region}; delay is UNKNOWN. "
                f"Buffering connections conservatively by {buffer_connection_min} min "
                f"(≥{DELAY_BUFFER_THRESHOLD_MIN} min floor) — never silently assume "
                f"on-time. Advisory-only (data-coverage gap, not a known hazard)."
            )
            severity = "info"
        advisory.append({
            "type": "median_delay",
            "severity": severity,
            "detail": detail,
            "provenance": _provenance(src, tier),
        })

    # --- (c) Seismic resilience (expected-impact weight) ---------------------
    seismic_raw = profile["seismic_resilience"]
    # Every seeded region now carries a seismic entry (asserted at import). The
    # None fallback to the conservative UNKNOWN midpoint (neither resilient nor
    # fragile, never a silent "safe") remains as a belt-and-braces guard.
    seismic = int(seismic_raw) if seismic_raw is not None else _UNKNOWN_SEISMIC
    if seismic < SEISMIC_LOW_RESILIENCE:
        advisory.append({
            "type": "seismic_resilience",
            "severity": "info",
            "detail": (
                f"Region {region} has LOW seismic resilience (score {seismic}/100): "
                f"earthquake occurrence is unforecastable but expected impact is "
                f"elevated. Prefer modern, code-compliant accommodation; surface to "
                f"the traveler (advisory, not a block)."
            ),
            "provenance": _provenance(src, tier),
        })

    # --- (d) Civil-unrest advisory → CIVIL_UNREST signal ---------------------
    # A SEEDED standing CIVIL-UNREST advisory (actual protests/riots) the Planner
    # weights and the peril crosswalk maps to the CIVIL_UNREST coverage key. Now
    # narrowed (D5): `civil_unrest_bp` is nonzero ONLY for regions whose advisory
    # CAUSE is genuinely civil unrest (the _CIVIL_UNREST_BY_REGION seed plus
    # _advisory_civil_unrest_bp, which itself only fires for the civil_unrest
    # category). Crime / armed-conflict / generic advisories flow through (d.2),
    # NOT here — so we never mislabel a kidnapping or war advisory as "unrest".
    civil_unrest_bp = int(profile.get("civil_unrest_bp", 0))
    if civil_unrest_bp >= CIVIL_UNREST_FLAG_BP:
        reason_codes.append(_CIVIL_UNREST_CODE)
        advisory.append({
            "type": "civil_unrest",
            "severity": "high" if civil_unrest_bp >= CIVIL_UNREST_ELEVATED_BP else "medium",
            "detail": (
                f"Standing civil-unrest advisory ~{civil_unrest_bp / 100:.0f}% for "
                f"region {region} (protests/riots). "
                + ("ELEVATED — reconsider need to travel; prefer flexible-cancellation "
                   "facilities and check coverage exclusions." if civil_unrest_bp >= CIVIL_UNREST_ELEVATED_BP
                   else "Flag and check coverage exclusions.")
            ),
            "provenance": _provenance(src, tier),
        })

    # --- (d.2) State-Dept advisory LEVEL → CAUSE-categorized signal (D5 #38) ---
    # An L3/L4 advisory whose CAUSE is NOT civil unrest is emitted under its true
    # reason-code: ARMED_CONFLICT for war/active conflict (the ONLY category that
    # reaches WAR/EXC-WAR-2 via the crosswalk), CRIME for crime/kidnapping (e.g.
    # Sabah ESSZONE), and the generic ADVISORY_ELEVATED for a high advisory with
    # no specific seeded cause. We never blanket-map to CIVIL_UNREST (handled in
    # (d) for genuine unrest only) and never over-claim WAR for a generic level.
    advisory_level_bp = int(profile.get("advisory_level_bp", 0))
    advisory_cause = profile.get("advisory_category")  # None ⇒ generic
    if advisory_level_bp >= CIVIL_UNREST_FLAG_BP and advisory_cause != "civil_unrest":
        adv_code = _advisory_reason_code(region)
        reason_codes.append(adv_code)
        elevated = advisory_level_bp >= CIVIL_UNREST_ELEVATED_BP
        if advisory_cause == "armed_conflict":
            cause_label = "active armed-conflict"
            adv_type = "armed_conflict"
            tail = ("DO NOT TRAVEL — armed-conflict advisory; insurance coverage is "
                    "NOT extended to armed-conflict zones (EXC-WAR-2)." if elevated
                    else "Armed-conflict advisory — flag and check war/EXC-WAR-2 "
                         "coverage exclusions.")
        elif advisory_cause == "crime":
            cause_label = "elevated crime / kidnapping"
            adv_type = "crime"
            tail = ("ELEVATED — reconsider need to travel; use a reputable local "
                    "guide, avoid high-crime/remote areas, check coverage." if elevated
                    else "Flag — elevated-crime advisory; avoid high-crime areas and "
                         "check coverage exclusions.")
        else:
            # Adversarial review (2026-07-06): this
            # branch's cause_label used to be "elevated government advisory" —
            # since the detail f-string below already appends " advisory", that
            # produced "Standing elevated government advisory advisory ~X%".
            # The other two branches' cause_label ("active armed-conflict",
            # "elevated crime / kidnapping") don't end in "advisory", so they
            # were never affected.
            cause_label = "elevated government"
            adv_type = "advisory_elevated"
            tail = ("ELEVATED — reconsider need to travel; recheck government "
                    "advisories and prefer flexible-cancellation facilities." if elevated
                    else "Flag — elevated government advisory; recheck advisories "
                         "before departure.")
        advisory.append({
            "type": adv_type,
            "severity": "high" if elevated else "medium",
            "detail": (
                f"Standing {cause_label} advisory ~{advisory_level_bp / 100:.0f}% "
                f"for region {region}. " + tail
            ),
            "provenance": _provenance(src, tier),
        })

    # TTL enforcement: the region hazard profile itself can be stale (never
    # checked before). Append a "stale_data" FLAG advisory when its own
    # fetched_at+ttl freshness window has elapsed as of `today` — this flows
    # through the EXISTING flag computation below (decisions.flag = any
    # non-info-severity advisory) rather than a parallel path, so a stale
    # profile that fired ZERO hazard advisories (the "confidently all-clear"
    # case this exists to catch) still gets flagged for manual review.
    _leg_provenance = _provenance(src, tier)
    if is_provenance_expired(_leg_provenance, as_of=today):
        advisory.append({
            "type": "stale_data",
            "severity": "flag",
            "detail": stale_provenance_note(_leg_provenance),
            "provenance": _leg_provenance,
        })
        if RiskReasonCode.ADVISORY_ELEVATED.value not in reason_codes:
            reason_codes.append(RiskReasonCode.ADVISORY_ELEVATED.value)

    decisions = {
        "avoid_window": avoid_window,
        "buffer_connection_min": buffer_connection_min,
        # #70 honesty: a leg is FLAGGED only for a real signal — at least one NON-"info"
        # advisory. An info-level advisory (e.g. the conservative connection buffer raised when
        # median-delay data is MISSING — a data-coverage gap, explicitly "not a known hazard")
        # must NOT set risk_flagged / flagged_legs, or every data-gap region reads as hazardous
        # (over-warn). Real hazards (cyclone/flood/wildfire/unrest = medium|high|flag, and a
        # SEEDED median delay = medium) still flag.
        "flag": any((a or {}).get("severity") != "info" for a in advisory),
    }

    # De-dup reason codes, stable order.
    seen: set[str] = set()
    reason_codes = [rc for rc in reason_codes if not (rc in seen or seen.add(rc))]

    # --- Planning note for L3/L4 advisory destinations (deterministic lookup) --
    # D5: the L4 note only invokes EXC-WAR-2 / armed-conflict for an actual
    # ARMED_CONFLICT-category advisory; a generic/crime L4 gets a do-not-travel
    # note WITHOUT the war framing (EXC-WAR-2 is armed-conflict-only).
    advisory_level = _ADVISORY_LEVEL_BY_REGION.get(region, 1)
    is_armed_conflict = _advisory_category(region) == "armed_conflict"
    _L3_NOTE = (
        "US State Dept Level 3 (Reconsider Travel) — travel is generally "
        "manageable with advance planning and a reputable local guide. Avoid "
        "remote and border areas. Recheck government advisories "
        "(travel.state.gov) before departure."
    )
    _L4_WAR_NOTE = (
        "US State Dept Level 4 (Do Not Travel) — insurance coverage is not "
        "extended to armed conflict zones (EXC-WAR-2). This destination is "
        "outside current booking scope."
    )
    _L4_GENERIC_NOTE = (
        "US State Dept Level 4 (Do Not Travel) — reconsider need to travel. "
        "This destination is outside current booking scope; recheck government "
        "advisories (travel.state.gov) before any travel."
    )
    if advisory_level == 4:
        planning_note: str | None = _L4_WAR_NOTE if is_armed_conflict else _L4_GENERIC_NOTE
    elif advisory_level == 3:
        planning_note = _L3_NOTE
    else:
        planning_note = None

    # --- (#68) Leg-level ALERT TIER + occurrence band ------------------------
    # Deterministic projection of the advisories already assembled above (pure
    # function of existing fields → var-0 safe; introduces no new model input).
    # Gives the UI ONE proportionate signal instead of re-deriving severity from
    # the advisory list. Tier-gated by construction:
    #   HIGH = an AVOID-tier hazard fired (advisory severity 'high')
    #   MED  = a FLAG-tier hazard fired (advisory severity 'medium')
    #   LOW  = info-only signal (seismic resilience / data-coverage gap)
    #   NONE = nothing fired
    # (audit F8) standing seismic-resilience is CONTEXT, not a trip-specific alert —
    # exclude it so it doesn't bump nearly every leg to LOW (it stays visible in
    # `advisory`). Leg-specific info (e.g. missing-delay-data) still earns LOW.
    _alert_sevs = {a["severity"] for a in advisory if a.get("type") != "seismic_resilience"}
    if "high" in _alert_sevs:
        alert_tier = "HIGH"
    elif "medium" in _alert_sevs:
        alert_tier = "MED"
    elif "info" in _alert_sevs:
        alert_tier = "LOW"
    else:
        alert_tier = "NONE"
    # Occurrence chance = the MAX modeled seasonal-climatology likelihood across the
    # %-quantifiable natural hazards. Categorical advisories (war/unrest/L3-L4/seismic)
    # are NOT a % chance — they carry their own severity in `advisory` + `alert_tier`,
    # so we never fabricate a number for them. Band cutoffs mirror the AVOID/FLAG bps.
    _max_hazard_bp = max(cyclone_bp, flood_bp, wildfire_bp, drought_bp)
    if _max_hazard_bp >= 3000:
        _occ_band = "high"
    elif _max_hazard_bp >= 1000:
        _occ_band = "moderate"
    elif _max_hazard_bp > 0:
        _occ_band = "low"
    else:
        _occ_band = "none"

    return {
        "city": city,
        "region": region,
        "mode": mode_key,
        "alert_tier": alert_tier,
        "occurrence": {
            "max_likelihood_pct": round(_max_hazard_bp / 100, 1),
            "band": _occ_band,
            # (audit F7) a categorical hazard (war/unrest/L3-L4) can drive a HIGH/MED tier
            # with NO %-quantifiable chance — flag it so a UI reading occurrence alone never
            # under-states a do-not-travel leg as "0% / none".
            "categorical_advisory_present": bool(alert_tier in ("HIGH", "MED") and _max_hazard_bp == 0),
            "basis": "modeled seasonal climatology (cyclone/flood/wildfire/drought); categorical advisories carry their own severity in alert_tier",
        },
        "cyclone_likelihood_bp": cyclone_bp,
        "cyclone_likelihood_pct": round(cyclone_bp / 100, 1),
        # Basin-correct display noun (typhoon/hurricane/cyclone) so a UI can render the
        # specific hazard instead of the generic 'natural_disaster' reason code. None when
        # no cyclone fired. Additive + deterministic (var-0).
        "cyclone_basin": _cyclone_basin_noun(src, region).lower() if cyclone_flag else None,
        "flood_index_bp": flood_bp,
        "wildfire_likelihood_bp": wildfire_bp,
        "wildfire_likelihood_pct": round(wildfire_bp / 100, 1),
        "drought_index_bp": drought_bp,
        "median_delay_min": median_delay,
        "seismic_resilience": seismic,
        "advisory": advisory,
        "reason_codes": reason_codes,
        "decisions": decisions,
        "source_freshness": freshness,
        "provenance": _leg_provenance,
        "planning_note": planning_note,
    }


def assess(
    legs: list[dict[str, Any]],
    *,
    simulate_source_unreachable: bool = False,
    today: str | None = None,
) -> dict[str, Any]:
    """
    Assess a list of legs → per-leg risk signals + a trip-level roll-up.

    Each leg: {city, iso2?, checkin?, checkout?, mode?}. PURE + DETERMINISTIC.
    Roll-up: {any_avoid_window, max_buffer_connection_min, flagged_legs,
    all_reason_codes}. The roll-up is what the Planner consumes additively.

    `iso2` (optional, APPEND-ONLY, #236/#240 R2 wiring fix): each leg's own
    country (falls back to `country` for callers that used that key),
    threaded straight to assess_leg so the country-qualified composite
    region lookup actually fires on this path. A leg that carries neither key
    is byte-identical to before (bare-key fallback).

    `today` (optional, APPEND-ONLY): threaded straight to assess_leg for TTL
    enforcement only — see assess_leg's docstring. Omitting it is
    byte-identical to pre-TTL behaviour.
    """
    per_leg: list[dict[str, Any]] = []
    for i, leg in enumerate(legs or []):
        sig = assess_leg(
            city=leg.get("city"),
            iso2=leg.get("iso2") or leg.get("country"),
            checkin=leg.get("checkin"),
            checkout=leg.get("checkout"),
            mode=leg.get("mode", "flight"),
            simulate_source_unreachable=simulate_source_unreachable,
            today=today,
        )
        sig["leg_id"] = leg.get("leg_id", f"leg-{i}")
        per_leg.append(sig)

    any_avoid = any(s["decisions"]["avoid_window"] for s in per_leg)
    max_buffer = max((s["decisions"]["buffer_connection_min"] for s in per_leg), default=0)
    flagged = [s["leg_id"] for s in per_leg if s["decisions"]["flag"]]
    all_codes: list[str] = []
    seen: set[str] = set()
    for s in per_leg:
        for rc in s["reason_codes"]:
            if rc not in seen:
                seen.add(rc)
                all_codes.append(rc)

    return {
        "per_leg": per_leg,
        "rollup": {
            "any_avoid_window": any_avoid,
            "max_buffer_connection_min": max_buffer,
            "flagged_legs": flagged,
            "all_reason_codes": all_codes,
            "n_legs": len(per_leg),
        },
    }


# ===========================================================================
# Optional cosmetic LLM rationale — COSMETIC ONLY, behind a validator. NEVER
# changes a number or a decision (NO-LLM-NUMBERS).
# ===========================================================================

def validate_rationale(text: str, assessment: dict[str, Any]) -> bool:
    """
    True iff `text` is a faithful restatement of the structured assessment: it
    must NOT call an AVOID/flagged window safe, and must not soften a high-risk
    signal. Conservative: any "safe / no risk / go ahead" softening when the
    roll-up flagged the trip → reject (never let prose overturn the signal).
    """
    if not isinstance(text, str) or not text.strip():
        return False
    rollup = assessment.get("rollup", {})
    flagged = bool(rollup.get("flagged_legs")) or bool(rollup.get("any_avoid_window"))
    if flagged:
        low = text.lower()
        soften = [
            "no risk", "no risks", "perfectly safe", "totally safe", "nothing to worry",
            "no concerns", "no need to worry", "all clear", "risk-free", "go ahead as planned",
        ]
        if any(s in low for s in soften):
            return False
    return True


def _llm_rationale(assessment: dict[str, Any]) -> str | None:
    """
    Optional cosmetic one-paragraph restatement via DashScope. Returns None if no
    key, on any error, or if the draft fails validate_rationale (→ deterministic
    fallback). NEVER changes the structured assessment / any number.
    """
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        return None
    try:
        import httpx

        rollup = assessment.get("rollup", {})
        facts = json.dumps({
            "any_avoid_window": rollup.get("any_avoid_window"),
            "max_buffer_connection_min": rollup.get("max_buffer_connection_min"),
            "flagged_legs": rollup.get("flagged_legs"),
            "reason_codes": rollup.get("all_reason_codes"),
        }, sort_keys=True)
        prompt = (
            "You are a travel-risk briefer. In ONE short paragraph, restate this "
            "deterministic risk assessment for the traveler. Use ONLY these facts; "
            "do NOT add numbers, do NOT call a flagged/avoid window safe.\n" + facts
        )
        resp = httpx.post(
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"enable_thinking": False, "model": "qwen3-max", "messages": [{"role": "user", "content": prompt}]},
            timeout=30.0,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        return text if validate_rationale(text, assessment) else None
    except Exception:
        return None


# ===========================================================================
# The A2A agent
# ===========================================================================


class RiskAgent(A2AAgent):
    """
    Risk L1 proactive signal CONSOLIDATOR (Travel Guild, Track-3).

    Implements ``risk.assess`` — per-leg PLANNING-INPUT risk signals
    (cyclone_likelihood, median_delay_min, seismic_resilience) + the deterministic
    avoid/buffer/flag decision. OFF the money path: signals only, no fee, no
    checkout, no mandate. Fully deterministic (NO-LLM-NUMBERS); the optional
    cosmetic rationale is clamped behind validate_rationale.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 9107) -> None:
        self._host = host
        self._port = port
        super().__init__()

    def _build_card(self) -> dict:
        url = f"http://{self._host}:{self._port}"
        return {
            "name": "risk-agent",
            "description": (
                "Risk L1 proactive signal CONSOLIDATOR — emits seeded, deterministic, "
                "provenance-tagged PLANNING-INPUT signals per leg: (a) cyclone/typhoon "
                "likelihood by region × month (flagship), (b) median delay by region × "
                "mode (flight/rail/bus), (c) seismic resilience by region (expected-"
                "impact weight). Returns an avoid/buffer/flag DECISION the Planner "
                "consumes ADDITIVELY: AVOID a high-cyclone-likelihood window, BUFFER "
                "connections in high-median-delay hubs, FLAG low-resilience regions. "
                "OFF the money path: signals only — no fee, no checkout, no mandate; it "
                "does NOT decide traveler-judgment (fitness/allergy). NO-LLM-NUMBERS "
                "(every likelihood/median/score is a seeded constant; the decision is a "
                "pure threshold function over closed sets; UNKNOWN region → conservative "
                "flag, never silent-safe; the LLM only drafts validated cosmetic prose). "
                "Implements 'risk.assess'. Part of the Travel Guild multi-agent "
                "pipeline (Track 3, L1 proactive)."
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
                    "id": "risk.assess",
                    "name": "Assess per-leg risk signals (cyclone / delay / seismic)",
                    "description": (
                        "Given a trip's legs (city + date window + optional mode), return "
                        "per-leg risk signals (cyclone_likelihood, median_delay_min, "
                        "seismic_resilience) + a deterministic avoid/buffer/flag decision "
                        "and a trip roll-up. Deterministic; seeded numbers; UNKNOWN region "
                        "→ conservative flag."
                    ),
                    "tags": [
                        "risk", "cyclone", "typhoon", "delay", "seismic", "advisory",
                        "planning-input", "deterministic", "L1", "consolidator",
                    ],
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "legs": {"type": "array", "items": {"type": "object"}},
                            "use_llm": {"type": "boolean"},
                            "simulate_source_unreachable": {"type": "boolean"},
                        },
                        "required": ["legs"],
                    },
                    "examples": [
                        json.dumps({
                            "legs": [
                                {"city": "darwin", "checkin": "2026-02-15",
                                 "checkout": "2026-02-22", "mode": "flight"}
                            ]
                        }),
                    ],
                },
            ],
        }

    def _register_skills(self) -> None:
        self.register_skill("risk.assess", self._assess_handler)

    async def _assess_handler(self, message: dict, task: dict) -> dict:
        payload = self._extract_payload(message)
        if not isinstance(payload, dict):
            raise ValueError(
                "risk.assess requires a data part with a JSON object "
                "{legs[], use_llm?, simulate_source_unreachable?}"
            )
        legs = payload.get("legs")
        if not isinstance(legs, list) or not legs:
            raise ValueError("risk.assess: 'legs' must be a non-empty list")

        result = assess(
            legs,
            simulate_source_unreachable=bool(payload.get("simulate_source_unreachable", False)),
            # TTL enforcement (previously the orchestrator sent "today" in this
            # payload and it was silently dropped here — never read, never
            # checked). Threaded through now so a stale seeded hazard profile
            # gets flagged rather than served as confidently current forever.
            today=payload.get("today"),
        )

        if payload.get("use_llm"):
            rationale = _llm_rationale(result)
            result["llm_rationale"] = rationale
            result["rationale_source"] = "llm" if rationale else "deterministic"
        else:
            result["rationale_source"] = "deterministic"

        rollup = result["rollup"]
        logger.info(
            "risk.assess: legs=%d avoid=%s max_buffer=%dmin flagged=%d codes=%s",
            rollup["n_legs"], rollup["any_avoid_window"],
            rollup["max_buffer_connection_min"], len(rollup["flagged_legs"]),
            rollup["all_reason_codes"],
        )
        return _new_artifact(
            name="risk.assess.result",
            parts=[_data_part(result)],
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
    port = int(os.environ.get("PORT", 9107))
    host = os.environ.get("AGENT_BIND_HOST", "127.0.0.1")

    agent = RiskAgent(host=host, port=port)
    app = agent.build_app()

    logger.info("Risk agent starting on %s:%d", host, port)
    logger.info("Agent Card: http://%s:%d/.well-known/agent-card.json", host, port)
    logger.info("RPC endpoint: http://%s:%d/", host, port)

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()


# ===========================================================================
# §AGENT-EXTENSION NOTE — Risk is the THIRD specialist on the pattern, and the
# FIRST L1 proactive / OFF-money-path one.
# ===========================================================================
# Built per the §AGENT-EXTENSION PATTERN documented at the bottom of
# insurance_agent.py. Recap of what this file did so the NEXT specialist
# (Health 9108, Fraud 9109, ...) can mirror it:
#   1. NEW FILE on a NEW PORT (9107), subclass A2AAgent, _build_card +
#      _register_skills (risk.assess).
#   2. COMPOSE against Phase-0 contracts: make_provenance / SourceTier on every
#      signal + RiskReasonCode (Risk OWNS the threat-signal reason-codes; the
#      crosswalk maps them to perils for Insurance). OFF the money path → NO
#      make_money / LineItemAssembler call: Risk emits signals, not fees.
#   3. VARIANCE CLAMP: NO-LLM-NUMBERS (cyclone bp / delay min / seismic score are
#      seeded constants); the avoid/buffer/flag DECISION is a PURE threshold
#      function over closed sets; UNKNOWN region → conservative FLAG (never a
#      silent "safe"); the LLM is cosmetic only, validated by validate_rationale.
#   4. SEEDED DATA, provenance-tagged: per-region cyclone×month / delay×mode /
#      seismic tables with an honest Tier-3 SEEDED_FALLBACK behind
#      fetch_region_profile(); a live JTWC/GDACS/OAG feed is a Tier-2 swap.
#   5. WIRING — APPEND-ONLY orchestrator hook (risk_url/risk_client +
#      _call_risk(), defaulting to None). The Planner consumes the roll-up
#      ADDITIVELY — only scenarios that CARRY a cyclone/delay condition change;
#      S1–S5 stay byte-identical var-0. Registration IS the AgentCard + the hook.
#   6. VERIFY HARD: test_risk_agent.py (CI-safe ASGI TestClient) with the CI-
#      enforced invariants (deterministic signals; numbers from seed not LLM;
#      UNKNOWN never silent-safe; pure thresholds) + the cyclone-window fair
#      baseline (baseline_risk.py + run_risk_bench.py) measuring the gain.
