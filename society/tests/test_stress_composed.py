"""
test_stress_composed.py — composed-society edge battery (the negotiate() path).

Backs the COMPOSED claims with volume + edge cases. Runs a battery of edge itineraries
through the full orchestrator.negotiate() (all gates wired), each TWICE, and asserts:
  - NO CRASH        : every edge input returns a structured result
  - VAR-0           : two identical runs produce identical stable output (no LLM/network)
  - COMPOSITION     : the expected typed signal fires for each hazard/gate leg
  - NEVER SILENT-UNSAFE : a hazard/unknown leg is never silently passed
  - DISCRIMINATION  : a seasonal hazard is silent off-season

Edge cases are built from the real data points (seeded regions, the 6 itineraries,
gate triggers). Deterministic; no DashScope key required (clamped edges fall back).
Run: cd society && python3 -m pytest test_stress_composed.py -q
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tests.test_composition_wiring import build_society  # reuse the full-society harness


def _trip(legs, budget=400000, nat="US", today="2026-01-05"):
    return {"user_id": "stress", "total_budget_cents": budget, "today": today,
            "nationality": nat, "legs": legs}


def _leg(city, ci, co, country="XX", vibe="culture"):
    return {"city": city, "dest_country": country, "departure_date": ci,
            "checkin": ci, "checkout": co, "adults": 1, "vibe": vibe}


def _codes(res):
    """All risk reason_codes surfaced anywhere in the composed result (stable)."""
    sig = res.get("risk_signals") or {}
    codes = set((sig.get("rollup") or {}).get("all_reason_codes") or [])
    for leg in (sig.get("per_leg") or []):
        codes.update(leg.get("reason_codes") or [])
    return codes


def _flagged(res):
    """Never-silent-unsafe check: ANY per-leg advisory (incl. unknown_region) or a
    typed code or a rollup flag. per_leg.advisory is authoritative — the rollup
    convenience block is empty on the cannot_satisfy path."""
    sig = res.get("risk_signals") or {}
    if (sig.get("rollup") or {}).get("flagged_legs"):
        return True
    if _codes(res):
        return True
    for leg in (sig.get("per_leg") or []):
        if leg.get("advisory") or (leg.get("decisions") or {}).get("flag"):
            return True
    return False


def _stable(res):
    """Canonical projection for var-0 comparison (drop volatile ids/timestamps)."""
    return json.dumps({
        "outcome": res.get("outcome"),
        "codes": sorted(_codes(res)),
        "any_avoid": (res.get("risk_signals") or {}).get("rollup", {}).get("any_avoid_window"),
    }, sort_keys=True)


# (label, legs, expected_codes_subset, must_flag_or_react)
CASES = [
    ("bali-wet-jan",        [_leg("bali", "2026-01-10", "2026-01-14", "ID")],            {"flood"},            True),
    ("bali-dry-jul",        [_leg("bali", "2026-07-10", "2026-07-14", "ID")],            set(),                None),
    ("tokyo-rainy-sep",     [_leg("tokyo", "2026-09-10", "2026-09-14", "JP")],           {"flood"},            True),
    ("tokyo-quake-jun",     [_leg("tokyo", "2026-06-10", "2026-06-14", "JP")],           set(),                None),  # high resilience → restraint
    ("istanbul-flood-nov",  [_leg("istanbul", "2026-11-10", "2026-11-14", "TR")],        {"flood"},            True),  # Istanbul/Marmara peak is autumn Oct-Dec; summer is dry (→ tr-blacksea split)
    ("manila-typhoon-sep",  [_leg("manila", "2026-09-10", "2026-09-14", "PH")],          {"natural_disaster"}, True),
    ("manila-offseason-mar",[_leg("manila", "2026-03-10", "2026-03-14", "PH")],          set(),                None),
    ("darwin-cyclone-feb",  [_leg("darwin", "2026-02-10", "2026-02-14", "AU")],          {"natural_disaster"}, True),
    ("png-civil-unrest",    [_leg("port moresby", "2026-05-10", "2026-05-14", "PG")],    {"civil_unrest"},     True),
    # D5: Guatemala's L3 is violent-crime driven, not protests → `crime` (not blanket civil_unrest).
    ("guatemala-L3",        [_leg("guatemala city", "2026-03-10", "2026-03-14", "GT")],  {"crime"},            True),
    ("ethiopia-yf-near",    [_leg("addis ababa", "2026-01-08", "2026-01-12", "ET")],     set(),                True),
    ("unknown-region",      [_leg("ulaanbaatar", "2026-06-10", "2026-06-14", "MN")],     set(),                True),  # defensive flag
    ("multi-hazard-3leg",   [_leg("bali", "2026-01-10", "2026-01-14", "ID"),
                             _leg("tokyo", "2026-09-20", "2026-09-24", "JP"),
                             _leg("port moresby", "2026-05-01", "2026-05-04", "PG")],    {"flood", "civil_unrest"}, True),
    ("tight-budget",        [_leg("bali", "2026-01-10", "2026-01-14", "ID")],            None,                 None),  # budget=low below
    # Stage D LP500 region cases -------------------------------------------------------
    # Norway: flood season May–Jun (no region, bp=2000 in Jun >= FLOOD_FLAG_BP=1500)
    ("norway-flood-jun",    [_leg("bergen", "2026-06-10", "2026-06-14", "NO")],          {"flood"},            True),
    # Peru: heavy flood season Jan–Apr (pe-coast, Feb bp=4500 >= FLOOD_AVOID_BP=3000)
    ("peru-flood-feb",      [_leg("lima", "2026-02-10", "2026-02-14", "PE")],            {"flood"},            True),
    # Pakistan: US State Dept L3 advisory with no specific seeded cause → generic
    # `advisory_elevated` (D5: never blanket civil_unrest); still flags + reacts.
    ("pakistan-L3",         [_leg("gilgit", "2026-06-10", "2026-06-14", "PK")],          {"advisory_elevated"}, True),
    # Norway dry season: Jan has 0 flood bp → no seasonal code, no civil_unrest (L1)
    ("norway-dry-jan",      [_leg("bergen", "2026-01-10", "2026-01-14", "NO")],          set(),                None),

    # STAGE-D REGION HAZARDS -----------------------------------------------------------
    ("jordan-winter-flood",   [_leg("wadi musa","2026-01-10","2026-01-14","JO")],  {"flood"},            True),   # jo Jan flood bp=600 → flag (advisory path)
    ("jordan-summer-dry",     [_leg("wadi musa","2026-07-10","2026-07-14","JO")],  set(),                None),
    ("nepal-evac-needed",     [_leg("kathmandu","2026-09-10","2026-09-14","NP")],  set(),                True),   # advisory-flagged; no flood code at np Sep
    ("iceland-low-risk",      [_leg("reykjavik","2026-07-10","2026-07-14","IS")],  set(),                None),   # unknown city → advisory-flagged, no seasonal code
    ("tanzania-advisory",     [_leg("arusha","2026-06-10","2026-06-14","TZ")],     {"advisory_elevated"}, True),  # D5: generic elevated advisory (no protest cause)
    ("zambia-wet-season",     [_leg("livingstone","2026-02-10","2026-02-14","ZM")],{"flood"},            True),
    ("morocco-safe",          [_leg("marrakech","2026-04-10","2026-04-14","MA")],  set(),                None),
    ("peru-amazon-risk",      [_leg("lima","2026-02-10","2026-02-14","PE")],       {"flood"},            True),

    # MULTI-LEG NEW REGIONS ------------------------------------------------------------
    ("jordan-egypt-combo",    [_leg("wadi musa","2026-03-10","2026-03-14","JO"),
                               _leg("cairo","2026-03-16","2026-03-20","EG")],       set(),                None),
    ("himalaya-circuit",      [_leg("kathmandu","2026-10-10","2026-10-14","NP"),
                               _leg("lhasa","2026-10-16","2026-10-20","CN")],       set(),                True),   # advisory-flagged; no seasonal code fires
    ("africa-safari",         [_leg("nairobi","2026-07-10","2026-07-14","KE"),
                               _leg("arusha","2026-07-16","2026-07-20","TZ")],      {"advisory_elevated"}, True),  # D5: TZ generic elevated advisory
    ("europe-city-break",     [_leg("paris","2026-06-10","2026-06-14","FR"),
                               _leg("barcelona","2026-06-16","2026-06-20","ES"),
                               _leg("lisbon","2026-06-22","2026-06-26","PT")],      {"natural_disaster"}, True),  # ES es-east June wildfire FLAG (Copernicus FWI #51, ~3013bp) — legit elevated signal, not over-warn
    ("latam-grand-tour",      [_leg("cusco","2026-06-10","2026-06-14","PE"),
                               _leg("lima","2026-06-16","2026-06-20","PE"),
                               _leg("buenos aires","2026-06-22","2026-06-26","AR")],set(),               None),
    ("southeast-asia-loop",   [_leg("bali","2026-07-10","2026-07-14","ID"),
                               _leg("siem reap","2026-07-16","2026-07-20","KH"),
                               _leg("hanoi","2026-07-22","2026-07-26","VN")],       set(),                True),   # advisory-flagged; codes collapse to []

    # BUDGET EDGE CASES ----------------------------------------------------------------
    ("very-tight-budget",     [_leg("paris","2026-06-10","2026-06-14","FR")],       None,                 None),  # budget=5000

    # ADVERSARIAL ----------------------------------------------------------------------
    ("impossible-sameday-intercontinental", [_leg("london","2026-06-10","2026-06-11","GB"),
                                             _leg("sydney","2026-06-11","2026-06-15","AU")],  set(),   None),  # same checkout/checkin

    # STAGE-D COMPOSED ORCHESTRATOR ----------------------------------------------------
    # 3-way Stage-D: JO (winter flash flood) + TZ (L3 generic advisory) + IS (seismic/clean)
    ("stage-d-3way",            [_leg("wadi musa","2026-01-10","2026-01-14","JO"),
                                 _leg("arusha","2026-02-01","2026-02-04","TZ"),
                                 _leg("reykjavik","2026-03-01","2026-03-04","IS")],
                                {"flood", "advisory_elevated"}, True),  # D5: flood from JO, advisory_elevated from TZ

    # Multi-continent clean path: SG (A-tier medical) + JP (seismic resilient) + AU (no cyclone May)
    ("multicity-clean-may",     [_leg("singapore","2026-05-10","2026-05-13","SG"),
                                 _leg("tokyo","2026-05-15","2026-05-18","JP"),
                                 _leg("sydney","2026-05-20","2026-05-23","AU")],
                                set(),                        None),  # no hazards in May; discrimination/clean-path test
]


class TestComposedStressBattery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.orch = build_society()

    def _run(self, legs, budget=400000):
        return self.orch.negotiate(_trip(legs, budget=budget))

    def test_battery(self):
        anomalies = []
        n = 0
        for label, legs, expect_codes, must_react in CASES:
            n += 1
            if label == "tight-budget":
                budget = 60000
            elif label == "very-tight-budget":
                budget = 5000
            else:
                budget = 400000
            try:
                r1 = self._run(legs, budget); r2 = self._run(legs, budget)
            except Exception as e:  # NO CRASH
                anomalies.append(f"{label}: CRASH {type(e).__name__}: {e}")
                continue
            # VAR-0
            if _stable(r1) != _stable(r2):
                anomalies.append(f"{label}: VARIANCE {_stable(r1)} != {_stable(r2)}")
            codes = _codes(r1)
            # COMPOSITION: expected codes present
            if expect_codes:
                missing = expect_codes - codes
                if missing:
                    anomalies.append(f"{label}: missing expected codes {missing} (got {sorted(codes)})")
            # DISCRIMINATION: off-season cases must NOT carry the seasonal code
            if expect_codes == set() and ({"flood", "natural_disaster"} & codes):
                anomalies.append(f"{label}: false-alarm seasonal code off-season {sorted(codes)}")
            # NEVER SILENT-UNSAFE: hazard/unknown legs must surface a flag
            if must_react and not _flagged(r1):
                anomalies.append(f"{label}: SILENT (no flag/advisory/codes on a must-react leg)")
            # outcome must be a known terminal value, never None/crash
            if r1.get("outcome") not in ("success", "cannot_satisfy", "needs_consent", "proposed"):
                anomalies.append(f"{label}: unexpected outcome {r1.get('outcome')!r}")

        print(f"\n=== COMPOSED STRESS BATTERY: {n} edge itineraries x2 runs ===")
        print("ANOMALIES:", len(anomalies))
        for a in anomalies:
            print("   -", a)
        self.assertEqual(anomalies, [], f"{len(anomalies)} composed-stress anomalies")


if __name__ == "__main__":
    unittest.main(verbosity=2)
