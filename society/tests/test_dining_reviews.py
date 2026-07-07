"""
#32 — ORCHESTRATOR-level tests for the OPT-IN LIVE restaurant reviews/ratings
enrichment hook (_maybe_enrich_dining + _call_dining_reviews): an additive layer
OVER the deterministic meal plan that NEVER touches the deterministic core.

The hook operates purely on the already-built result dict (result['day_plans'] is
the deterministic seed) and self._dining_request, exactly mirroring the #44 supper
hook, so these exercise it directly on a bare TravelOrchestrator with an injected
in-process dining-reviews provider callable (the AMap/Google seam stand-in).

Invariants proven:
  - APPEND-ONLY / var-0: no `dining` request (or gate not met) → NO key added; a
    plain no-opt-in result is BYTE-IDENTICAL (json.dumps sort_keys) before/after.
  - NO day_plans MUTATION: enrichment lands ONLY in result['dining_reviews'];
    day_plans is byte-identical after the hook (the deterministic core stays var-0).
  - FAIL-CONSERVATIVE: provider seam None / raising → honest 'unavailable' note,
    NEVER a fabricated rating; the booking result stays success.
  - HONESTY: a provider venue with NO rating → rating=null + review_count=null
    (never 0 / placeholder), carrying attribution + live=True + 'not stored'
    provenance. A provider rating reconciles onto a plan venue ONLY on a
    name-token + lat/lon match; an unmatched venue is labelled external.
  - NO STORAGE: the hook performs no write to poi_catalog.json.
"""
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from orchestration.orchestrator import TravelOrchestrator


# --- a deterministic day_plans seed mirroring day_planner_agent _stamp_meal shape ---
def _day_plans():
    return [
        {
            "leg_id": "leg-0",
            "city": "abu dhabi",
            "iso2": "AE",
            "country": "United Arab Emirates",
            "catalog_hit": True,
            "num_days": 1,
            "days": [
                {
                    "day_index": 0,
                    "bad_weather": False,
                    "attractions": [],
                    "meals": {
                        "breakfast": None,
                        "lunch": {
                            "name": "Al Mrzab Restaurant",
                            "name_en": "Al Mrzab Restaurant",
                            "cuisine": "seafood",
                            "lat": 24.4812,
                            "lon": 54.3705,
                            "provenance": "OSM poistore harvest",
                        },
                        "tea": None,
                        "dinner": {
                            "name": "Lebanese Flower",
                            "name_en": "Lebanese Flower",
                            "cuisine": "lebanese",
                            "lat": 24.4700,
                            "lon": 54.3600,
                            "provenance": "OSM poistore harvest",
                        },
                        "supper": None,
                    },
                }
            ],
            "unscheduled_attractions": [],
            "provenance": "OSM poistore harvest",
            "notes": [],
        }
    ]


def _success_result():
    return {"outcome": "success", "day_plans": _day_plans()}


def _orch(dining_client=None):
    orch = TravelOrchestrator(dining_client=dining_client)
    orch._trip_id = "t-dining"
    return orch


# --------------------------------------------------------------------------
# APPEND-ONLY / var-0
# --------------------------------------------------------------------------
def test_not_opted_in_byte_identical():
    """No `dining` request → NO key added; the result is byte-identical."""
    orch = _orch()
    orch._dining_request = None
    result = _success_result()
    before = json.dumps(result, sort_keys=True)
    orch._maybe_enrich_dining(result)
    after = json.dumps(result, sort_keys=True)
    assert "dining_reviews" not in result
    assert before == after, "no-opt-in result must be byte-identical"
    print("[DINING] PASS — not opted in → byte-identical, no dining_reviews key\n")


def test_reviews_flag_absent_no_key():
    """`dining` present but reviews falsy → no-op (gate not met)."""
    orch = _orch(dining_client=lambda q: {"source": "google", "venues": [{"name": "X"}]})
    orch._dining_request = {"cuisine": "lebanese"}  # no reviews flag
    result = _success_result()
    orch._maybe_enrich_dining(result)
    assert "dining_reviews" not in result
    print("[DINING] PASS — reviews flag absent → no-op\n")


def test_cuisine_absent_no_key():
    """`reviews` truthy but no cuisine pref → no-op (interactive-consent gate)."""
    orch = _orch(dining_client=lambda q: {"source": "google", "venues": [{"name": "X"}]})
    orch._dining_request = {"reviews": True}  # no cuisine
    result = _success_result()
    orch._maybe_enrich_dining(result)
    assert "dining_reviews" not in result
    print("[DINING] PASS — cuisine pref absent → no-op\n")


def test_not_booked_no_key():
    """Trip did not book → never suggest dining, no key."""
    orch = _orch(dining_client=lambda q: {"source": "google", "venues": [{"name": "X"}]})
    orch._dining_request = {"reviews": True, "cuisine": "lebanese"}
    result = {"outcome": "cannot_satisfy", "day_plans": _day_plans()}
    orch._maybe_enrich_dining(result)
    assert "dining_reviews" not in result
    print("[DINING] PASS — unbooked trip → no dining_reviews\n")


# --------------------------------------------------------------------------
# day_plans is NEVER mutated (deterministic core stays var-0)
# --------------------------------------------------------------------------
def test_day_plans_unchanged_after_enrich():
    """Enrichment attaches dining_reviews but leaves day_plans byte-identical."""
    def client(q):
        return {"source": "google", "venues": [
            {"name": "Lebanese Flower", "lat": 24.4700, "lon": 54.3600,
             "rating": 4.6, "review_count": 1200},
        ]}
    orch = _orch(dining_client=client)
    orch._dining_request = {"reviews": True, "cuisine": "lebanese"}
    result = _success_result()
    day_plans_before = json.dumps(result["day_plans"], sort_keys=True)
    orch._maybe_enrich_dining(result)
    day_plans_after = json.dumps(result["day_plans"], sort_keys=True)
    assert day_plans_before == day_plans_after, "day_plans must NOT be mutated"
    assert "dining_reviews" in result, "live layer must land in its own top-level key"
    print("[DINING] PASS — day_plans byte-identical; reviews isolated in own key\n")


# --------------------------------------------------------------------------
# FAIL-CONSERVATIVE — provider seam None / raising
# --------------------------------------------------------------------------
def test_no_provider_honest_note():
    """No dining provider configured → honest 'unavailable' note, no fabrication."""
    orch = _orch(dining_client=None)
    orch._dining_request = {"reviews": True, "cuisine": "lebanese"}
    result = _success_result()
    orch._maybe_enrich_dining(result)
    dr = result["dining_reviews"]
    assert len(dr) == 1, dr
    entry = dr[0]
    assert entry["provider"] is None
    assert entry["venues"] == []
    assert "unavailable" in entry["note"].lower()
    print("[DINING] PASS — no provider → honest 'live reviews unavailable' note\n")


def test_provider_raises_fail_conservative():
    """A provider that RAISES degrades to None (honest note); result stays success."""
    def boom(q):
        raise RuntimeError("network down")
    orch = _orch(dining_client=boom)
    orch._dining_request = {"reviews": True, "cuisine": "lebanese"}
    result = _success_result()
    orch._maybe_enrich_dining(result)
    assert result["outcome"] == "success"
    assert "unavailable" in result["dining_reviews"][0]["note"].lower()
    # _call_dining_reviews itself must swallow the error and return None.
    assert orch._call_dining_reviews({"city": "x"}) is None
    print("[DINING] PASS — provider raises → None, honest note, still success\n")


# --------------------------------------------------------------------------
# HONESTY — missing rating → null; attribution/live/provenance present
# --------------------------------------------------------------------------
def test_missing_rating_is_null_never_placeholder():
    """A provider venue with NO rating → rating=null + review_count=null."""
    def client(q):
        return {"source": "amap", "venues": [
            {"name": "Mystery Diner", "lat": 24.99, "lon": 54.99},  # no rating
        ]}
    orch = _orch(dining_client=client)
    orch._dining_request = {"reviews": True, "cuisine": "local"}
    result = _success_result()
    orch._maybe_enrich_dining(result)
    venue = result["dining_reviews"][0]["venues"][0]
    assert venue["rating"] is None, "missing rating MUST be null, never a placeholder"
    assert venue["review_count"] is None
    assert venue["attribution"] == "amap"
    assert venue["live"] is True
    assert "not stored" in venue["provenance"].lower()
    print("[DINING] PASS — missing rating → null; attribution+live+provenance carried\n")


# --------------------------------------------------------------------------
# HONESTY — match vs external labelling (no fabrication-by-mislabel)
# --------------------------------------------------------------------------
def test_match_reconciles_only_on_name_and_proximity():
    """A provider rating attaches to a plan venue ONLY on name-token + lat/lon match;
    an unmatched provider venue is labelled external."""
    def client(q):
        return {"source": "google", "venues": [
            # exact name + within ~25m of the plan's Lebanese Flower → matched
            {"name": "Lebanese Flower", "lat": 24.47001, "lon": 54.36001,
             "rating": 4.6, "review_count": 1200},
            # same name but FAR away → not a match → external (no mislabel)
            {"name": "Lebanese Flower", "lat": 25.9, "lon": 55.9,
             "rating": 3.1, "review_count": 5},
            # provider-only venue not in the plan at all → external
            {"name": "New Pop-Up Cafe", "lat": 24.1, "lon": 54.1, "rating": 4.9},
        ]}
    orch = _orch(dining_client=client)
    orch._dining_request = {"reviews": True, "cuisine": "lebanese"}
    result = _success_result()
    orch._maybe_enrich_dining(result)
    venues = result["dining_reviews"][0]["venues"]
    matched = [v for v in venues if not v["external"]]
    external = [v for v in venues if v["external"]]
    assert len(matched) == 1, f"exactly one venue should match: {venues}"
    assert matched[0]["name"] == "Lebanese Flower" and matched[0]["rating"] == 4.6
    assert len(external) == 2, f"far + provider-only venues must be external: {external}"
    for v in venues:
        assert v["live"] is True and v["attribution"] == "google"
    print("[DINING] PASS — match needs name+proximity; unmatched labelled external\n")


def test_amap_first_inside_china_seam_hint():
    """CN leg → the dispatch query prefers provider='amap' (booking_links CN seam)."""
    captured = {}
    def client(q):
        captured.update(q)
        return {"source": "amap", "venues": []}
    orch = _orch(dining_client=client)
    orch._dining_request = {"reviews": True, "cuisine": "chinese"}
    plans = _day_plans()
    plans[0]["iso2"] = "CN"
    plans[0]["city"] = "shanghai"
    result = {"outcome": "success", "day_plans": plans}
    orch._maybe_enrich_dining(result)
    assert captured.get("provider") == "amap", captured
    assert captured.get("cuisine") == "chinese"
    print("[DINING] PASS — CN leg prefers AMap provider in the dispatch query\n")


# --------------------------------------------------------------------------
# NO STORAGE — the hook never writes the catalog
# --------------------------------------------------------------------------
def test_no_catalog_write():
    """The enrichment hook performs NO write to poi_catalog.json (Places ToS)."""
    catalog = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "poi_catalog.json")
    before_mtime = os.path.getmtime(catalog) if os.path.exists(catalog) else None
    before_bytes = None
    if before_mtime is not None:
        with open(catalog, "rb") as fh:
            before_bytes = fh.read()

    def client(q):
        return {"source": "google", "venues": [
            {"name": "Lebanese Flower", "lat": 24.4700, "lon": 54.3600, "rating": 4.6},
        ]}
    orch = _orch(dining_client=client)
    orch._dining_request = {"reviews": True, "cuisine": "lebanese"}
    orch._maybe_enrich_dining(_success_result())

    if before_mtime is not None:
        assert os.path.getmtime(catalog) == before_mtime, "catalog mtime changed"
        with open(catalog, "rb") as fh:
            assert fh.read() == before_bytes, "catalog content changed"
    print("[DINING] PASS — no write to poi_catalog.json (live content not stored)\n")


# --------------------------------------------------------------------------
# NIT#1 (audit) — every leg unavailable → an honest per-leg note for EACH leg
# --------------------------------------------------------------------------
def test_all_legs_unavailable_note_per_leg():
    """Opted in with keys pending across a MULTI-leg trip → dining_reviews carries
    one honest 'unavailable' entry per leg, never a fabricated venue anywhere."""
    plans = _day_plans()
    leg2 = json.loads(json.dumps(plans[0]))
    leg2["leg_id"] = "leg-1"
    leg2["city"] = "dubai"
    plans.append(leg2)
    orch = _orch(dining_client=None)  # keys pending
    orch._dining_request = {"reviews": True, "cuisine": "lebanese"}
    result = {"outcome": "success", "day_plans": plans}
    orch._maybe_enrich_dining(result)
    dr = result["dining_reviews"]
    assert len(dr) == 2, dr
    assert [e["leg_id"] for e in dr] == ["leg-0", "leg-1"], "per-leg, plan order"
    for entry in dr:
        assert entry["provider"] is None
        assert entry["venues"] == []
        assert "unavailable" in entry["note"].lower()
    print("[DINING] PASS — all legs unavailable → honest note per leg, no fabrication\n")


# --------------------------------------------------------------------------
# NIT#4 (audit) — surfaced venue order follows the provider response order
# --------------------------------------------------------------------------
def test_venue_output_order_follows_provider():
    """`surfaced` venues preserve the provider response list order (deterministic
    given the provider's contract — no reordering/sorting introduced by the hook)."""
    names_in = ["Zeta", "Alpha", "Mu", "Beta"]
    def client(q):
        return {"source": "google",
                "venues": [{"name": n, "lat": 1.0, "lon": 1.0} for n in names_in]}
    orch = _orch(dining_client=client)
    orch._dining_request = {"reviews": True, "cuisine": "local"}
    result = _success_result()
    orch._maybe_enrich_dining(result)
    names_out = [v["name"] for v in result["dining_reviews"][0]["venues"]]
    assert names_out == names_in, f"order must follow provider: {names_out}"
    print("[DINING] PASS — surfaced venue order follows provider response order\n")


if __name__ == "__main__":
    tests = [
        test_not_opted_in_byte_identical,
        test_reviews_flag_absent_no_key,
        test_cuisine_absent_no_key,
        test_not_booked_no_key,
        test_day_plans_unchanged_after_enrich,
        test_no_provider_honest_note,
        test_provider_raises_fail_conservative,
        test_missing_rating_is_null_never_placeholder,
        test_match_reconciles_only_on_name_and_proximity,
        test_amap_first_inside_china_seam_hint,
        test_no_catalog_write,
        test_all_legs_unavailable_note_per_leg,
        test_venue_output_order_follows_provider,
    ]
    for t in tests:
        t()
    print("ALL #32 DINING TESTS PASSED")
