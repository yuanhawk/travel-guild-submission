"""
test_risk_51_remainder.py — #51 risk-model remainder (ship sub-pieces a/c/d).

Covers the SAFE, append-only sub-pieces added by feature #16/#51:

  (a) ENSO advisory channel — a SEEDED, capped MULTIPLIER on the already-firing
      cyclone/flood/drought channels, gated by a single seeded current-phase
      constant. It can only RAISE an in-season signal within a documented
      basin+phase window; it NEVER invents an off-season hazard and NEVER lowers
      a hand seed (fail-conservative, mirrors the wildfire FWI max-merge).
  (c) risk SCOPE-DISCLOSURE — an honest, deterministic, stable-ordered statement
      of the peril classes the model does NOT cover (volcano/tsunami/quake
      occurrence-forecast/etc.), so silence is never read as safety.
  (d) hazard -> interruption -> insurance/rebooking signal — a PURE function of
      the deterministic rollup yielding {likelihood_tier, drivers, rebooking_
      guidance, prefer_flexible_cancellation}, OFF the money path.

THE LOAD-BEARING INVARIANT: var-0 / deterministic. risk_agent is ALWAYS-ON
(it runs for S1-S5 too). The ENSO channel is strictly amplify-only and never
invents an off-season hazard. The shipped ENSO_CURRENT_PHASE is the 2026
forecast "el_nino" — a deliberate, separately-reviewed SEED change (not a
no-op): it amplifies el_nino-seeded basins/months and is the IDENTITY
everywhere else. assess_leg stays fully deterministic; every channel number
equals the raw seed passed through _enso_modulate under the shipped phase.
"""

from __future__ import annotations

import copy
import json

from agents import risk_agent as ra
from core.contracts import RiskReasonCode


# ---------------------------------------------------------------------------
# APPEND-ONLY / var-0 — the load-bearing guarantee
# ---------------------------------------------------------------------------

# Plain no-opt-in benchmark-style trips (no ENSO condition involved): the
# canonical "must stay byte-identical" set. Bali (off-season), Bangkok
# (off-season), a known low-risk JP leg, and an UNKNOWN city.
_PLAIN_TRIPS = [
    # Bali in June = dry season → no flood, no cyclone (off-season, additive-silent)
    dict(city="bali", checkin="2026-06-10", checkout="2026-06-17"),
    # Bangkok in February = off-season → no flood
    dict(city="bangkok", checkin="2026-02-01", checkout="2026-02-08"),
    # Tokyo in March = no cyclone/flood season
    dict(city="tokyo", checkin="2026-03-01", checkout="2026-03-08"),
    # An UNKNOWN city → conservative-flag path (must also stay byte-identical)
    dict(city="atlantis", checkin="2026-05-01", checkout="2026-05-08"),
]


def _assess_leg(t: dict) -> dict:
    return ra.assess_leg(
        city=t["city"], checkin=t["checkin"], checkout=t["checkout"]
    )


def test_assess_leg_is_deterministic() -> None:
    """N identical calls → byte-identical JSON (var-0)."""
    for t in _PLAIN_TRIPS:
        a = json.dumps(_assess_leg(t), sort_keys=True)
        b = json.dumps(_assess_leg(t), sort_keys=True)
        assert a == b


def test_enso_assess_leg_matches_modulated_seed_under_shipped_phase() -> None:
    """
    assess_leg threads the ENSO helper CONSISTENTLY: every channel number equals the
    raw seed max passed through _enso_modulate under the shipped phase. With the 2026
    forecast phase 'el_nino' this is a deliberate, separately-reviewed seed change
    (NOT byte-identical to the neutral build) — but it stays fully deterministic
    (var-0) and never invents an off-season hazard (raw 0 → 0).
    """
    assert ra.ENSO_CURRENT_PHASE in ("neutral", "el_nino", "la_nina")
    for t in _PLAIN_TRIPS:
        sig = _assess_leg(t)
        # UNKNOWN city → None numbers (skip the math).
        region = sig["region"]
        if region is None:
            continue
        months = ra._months_in_span(t["checkin"], t["checkout"])
        raw_cyc = max(
            (ra._CYCLONE_BY_REGION_MONTH.get(region, {}).get(m, 0) for m in months),
            default=0,
        )
        raw_flood = max(
            (ra._FLOOD_BY_REGION_MONTH.get(region, {}).get(m, 0) for m in months),
            default=0,
        )
        raw_drought = max(
            (ra._DROUGHT_BY_REGION_MONTH.get(region, {}).get(m, 0) for m in months),
            default=0,
        )
        # Each channel == the seed modulated by the shipped ENSO phase (identity
        # wherever the basin/phase/family/month carries no multiplier).
        assert sig["cyclone_likelihood_bp"] == ra._enso_modulate(raw_cyc, region, months, "cyclone")
        assert sig["flood_index_bp"] == ra._enso_modulate(raw_flood, region, months, "flood")
        assert sig["drought_index_bp"] == ra._enso_modulate(raw_drought, region, months, "drought")
        # ENSO never invents a hazard: a zero raw seed stays zero.
        if raw_cyc == 0:
            assert sig["cyclone_likelihood_bp"] == 0


def test_enso_helper_neutral_returns_base_unchanged() -> None:
    """The ENSO multiplier is the IDENTITY for the neutral phase / unseeded basin."""
    # neutral phase → no modulation regardless of basin/month
    assert ra._enso_modulate(2800, "au-northwest", [2], "cyclone", phase="neutral") == 2800
    # an unseeded region (no basin) → identity even under an amplifying phase
    assert ra._enso_modulate(2800, "zz-nowhere", [2], "cyclone", phase="la_nina") == 2800
    # a zero base stays zero even with an amplifying phase (never invent a hazard)
    assert ra._enso_modulate(0, "au-northwest", [8], "cyclone", phase="la_nina") == 0


def test_enso_amplifies_in_season_only_and_never_lowers() -> None:
    """
    ENSO RAISES an already-firing in-season signal within the documented basin+
    phase window, but NEVER invents an off-season hazard and NEVER lowers a hand
    seed (fail-conservative).
    """
    region = "au-northwest"  # Australian basin, cyclone season Nov-Apr, peak Feb
    base_feb = ra._CYCLONE_BY_REGION_MONTH[region][2]  # 4500
    amp = ra._enso_modulate(base_feb, region, [2], "cyclone", phase="la_nina")
    # In-season + amplifying phase → strictly RAISED (or equal), never lowered…
    assert amp >= base_feb
    # …and there IS a documented La Nina amplification for the Australian basin in
    # peak season, so it must actually raise above the hand seed.
    assert amp > base_feb
    # capped at the 0-10000 basis-point ceiling (no overflow)
    assert amp <= 10000
    # Off-season month (August, 0 bp) → ENSO invents nothing.
    assert ra._enso_modulate(0, region, [8], "cyclone", phase="la_nina") == 0


def test_enso_seed_tables_well_formed() -> None:
    """Every ENSO multiplier is a sane bp >= 10000 (a multiplier, never a cut)."""
    assert ra.ENSO_CURRENT_PHASE in ("neutral", "el_nino", "la_nina")
    for basin, phases in ra._ENSO_BASIN_SENSITIVITY.items():
        for phase, families in phases.items():
            assert phase in ("el_nino", "la_nina"), (basin, phase)
            for family, by_month in families.items():
                assert family in ("wet", "dry"), (basin, phase, family)
                for month, mult_bp in by_month.items():
                    assert 1 <= month <= 12, (basin, phase, family, month)
                    # A multiplier in basis points: 10000 == 1.0x (identity). ENSO
                    # is fail-conservative — it may only AMPLIFY (>= 1.0x), never cut.
                    assert isinstance(mult_bp, int) and mult_bp >= 10000, (
                        basin, phase, family, month, mult_bp,
                    )


def test_enso_el_nino_never_amplifies_australian_cyclone() -> None:
    """HONESTY: El Niño SUPPRESSES Australian cyclones — the wet/dry family split
    must ensure the el_nino (DRY/drought) seed never raises the CYCLONE channel."""
    region = "au-northwest"
    base_jan = ra._CYCLONE_BY_REGION_MONTH[region][1]  # 4200
    # cyclone (a WET channel) under el_nino → identity (no dry-seed leakage)
    assert ra._enso_modulate(base_jan, region, [1], "cyclone", phase="el_nino") == base_jan
    # but the SAME el_nino phase DOES amplify the DRY drought channel for the basin
    amp_drought = ra._enso_modulate(3000, region, [1], "drought", phase="el_nino")
    assert amp_drought > 3000


# ---------------------------------------------------------------------------
# (c) scope-disclosure — HONESTY: state what the model does NOT cover
# ---------------------------------------------------------------------------

def test_scope_disclosure_present_stable_and_honest() -> None:
    disc = ra.risk_scope_disclosure()
    assert isinstance(disc, dict)
    items = disc["not_covered"]
    assert isinstance(items, list) and len(items) >= 4
    # Deterministic + stable-ordered (sorted by peril key).
    assert items == sorted(items, key=lambda d: d["peril"])
    # Must honestly name the headline omissions the spec calls out.
    perils = {d["peril"] for d in items}
    for must in ("volcano", "tsunami", "earthquake_occurrence_forecast"):
        assert must in perils, f"scope-disclosure must name {must!r}"
    # Each item carries a why-not + what-to-check-instead (honest guidance).
    for d in items:
        assert d["why_not"].strip()
        assert d["check_instead"].strip()
    # Deterministic: two calls byte-identical, and a deep copy can't mutate state.
    assert json.dumps(ra.risk_scope_disclosure(), sort_keys=True) == json.dumps(
        ra.risk_scope_disclosure(), sort_keys=True
    )
    mutated = copy.deepcopy(disc)
    mutated["not_covered"].clear()
    assert len(ra.risk_scope_disclosure()["not_covered"]) >= 4


# ---------------------------------------------------------------------------
# (d) interruption outlook — PURE fn of the rollup, OFF the money path
# ---------------------------------------------------------------------------

def test_interruption_clean_trip_is_lowest_tier_no_rebooking() -> None:
    """A clean rollup → lowest tier, empty rebooking guidance, no avoid added."""
    clean = {
        "any_avoid_window": False,
        "max_buffer_connection_min": 0,
        "flagged_legs": [],
        "all_reason_codes": [],
    }
    out = ra.interruption_outlook(clean)
    assert out["likelihood_tier"] == "low"
    assert out["rebooking_guidance"] == []
    assert out["drivers"] == []
    assert out["prefer_flexible_cancellation"] is False
    # PURE: it mints no money and adds no avoid (no money/fee/total/avoid keys).
    for forbidden in ("total", "total_cents", "premium", "fee", "line_item", "avoid_window"):
        assert forbidden not in out


def test_interruption_avoid_window_elevates_and_gives_rebooking() -> None:
    """An avoid-window rollup → elevated tier + concrete rebooking guidance."""
    avoid = {
        "any_avoid_window": True,
        "max_buffer_connection_min": 45,
        "flagged_legs": ["leg-0"],
        "all_reason_codes": [
            RiskReasonCode.NATURAL_DISASTER.value,
            RiskReasonCode.FLOOD.value,
        ],
    }
    out = ra.interruption_outlook(avoid)
    assert out["likelihood_tier"] in ("elevated", "high")
    assert out["prefer_flexible_cancellation"] is True
    assert len(out["rebooking_guidance"]) >= 1
    assert all(isinstance(g, str) and g.strip() for g in out["rebooking_guidance"])
    # The hazard drivers are surfaced from the reason codes (honest provenance).
    assert RiskReasonCode.NATURAL_DISASTER.value in out["drivers"]


def test_interruption_is_pure_and_deterministic() -> None:
    """Same rollup → byte-identical outlook; input is not mutated."""
    rollup = {
        "any_avoid_window": True,
        "max_buffer_connection_min": 60,
        "flagged_legs": ["leg-0", "leg-1"],
        "all_reason_codes": [RiskReasonCode.TRANSPORT_DISRUPTION.value],
    }
    snapshot = copy.deepcopy(rollup)
    a = json.dumps(ra.interruption_outlook(rollup), sort_keys=True)
    b = json.dumps(ra.interruption_outlook(rollup), sort_keys=True)
    assert a == b
    assert rollup == snapshot  # pure: no mutation of the caller's rollup


def test_interruption_flag_only_is_middle_not_lowest() -> None:
    """A flagged-but-not-avoid rollup (buffer only) is above 'low' but below avoid."""
    flag_only = {
        "any_avoid_window": False,
        "max_buffer_connection_min": 45,
        "flagged_legs": ["leg-0"],
        "all_reason_codes": [RiskReasonCode.TRANSPORT_DISRUPTION.value],
    }
    out = ra.interruption_outlook(flag_only)
    assert out["likelihood_tier"] == "moderate"
    assert out["prefer_flexible_cancellation"] is False
