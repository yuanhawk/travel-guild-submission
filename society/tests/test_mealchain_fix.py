"""test_mealchain_fix.py — regression tests for the meal-chain skew fix in
agents/day_planner_agent.py:

Root cause: `_slot_fit_score` gave cafes a BONUS for breakfast/tea slots but ZERO
penalty for lunch/dinner/supper — a cafe scored IDENTICALLY to a real restaurant for
dinner, so it could win purely on downstream tiebreaks (completeness / dedup order).

Fix (mirrors the existing breakfast/tea bonus, inverted): a cafe now scores NEGATIVE
for lunch/dinner/supper, so a real restaurant always outranks a comparable cafe for
those slots. Also: third-wave/specialty coffee chains (Blue Bottle Coffee, %Arabica,
Verve Coffee) are added to `_GLOBAL_FASTFOOD_BRANDS`, which previously only caught
classic fast-food/big chain-cafe brands.

All deterministic / var-0 — no LLM, no wall-clock, no random.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agents.day_planner_agent as dp
from agents.day_planner_agent import build_day_plan, _slot_fit_score, _is_global_fastfood


def _synth_entry(restaurants):
    return {
        "attractions": [
            {"name": "Indoor Museum", "name_en": "Indoor Museum",
             "category": "museum", "weather_exposure": "indoor"},
        ],
        "restaurants": restaurants,
    }


# ---------------------------------------------------------------------------
# 1. _slot_fit_score: the cafe penalty, unit-level (mirrors the existing
#    breakfast/tea bonus tests in test_day_planner_agent_cov3.py).
# ---------------------------------------------------------------------------

def test_cafe_scores_negative_for_lunch():
    assert _slot_fit_score({"category": "cafe"}, "lunch") == -1


def test_cafe_scores_negative_for_dinner():
    assert _slot_fit_score({"category": "cafe"}, "dinner") == -1


def test_non_cafe_still_scores_zero_for_lunch_and_dinner():
    """No regression: a real restaurant/fast_food/pizzeria is unaffected — any
    non-cafe venue still fits lunch/dinner at score 0, exactly as before."""
    assert _slot_fit_score({"category": "restaurant"}, "lunch") == 0
    assert _slot_fit_score({"category": "restaurant"}, "dinner") == 0
    assert _slot_fit_score({"category": "fast_food"}, "dinner") == 0
    assert _slot_fit_score({"category": "pizzeria"}, "dinner") == 0


def test_cafe_scores_negative_for_supper_when_not_late():
    restaurant = {"category": "cafe", "opening_hours": "08:00-18:00"}
    assert _slot_fit_score(restaurant, "supper") == -1


def test_cafe_late_open_supper_still_worse_than_a_real_late_restaurant():
    """The cafe penalty and the supper lateness bonus are additive: a LATE cafe
    nets to 0 (penalty -1 + late bonus +1), which still loses to a real late-
    opening restaurant (bonus +1, no penalty -> net +1)."""
    late_cafe = {"category": "cafe", "opening_hours": "24/7"}
    late_restaurant = {"category": "restaurant", "opening_hours": "24/7"}
    assert _slot_fit_score(late_cafe, "supper") == 0
    assert _slot_fit_score(late_restaurant, "supper") == 1
    assert _slot_fit_score(late_restaurant, "supper") > _slot_fit_score(late_cafe, "supper")


def test_cafe_breakfast_and_tea_bonus_is_unchanged():
    """No regression on the ORIGINAL bonus this fix mirrors: cafes still win
    breakfast/tea exactly as before."""
    assert _slot_fit_score({"category": "cafe"}, "breakfast") == 1
    assert _slot_fit_score({"category": "cafe"}, "tea") == 1


# ---------------------------------------------------------------------------
# 2. End-to-end: a cafe no longer wins a dinner slot against a comparable real
#    restaurant (the exact failure mode the eval surfaced).
# ---------------------------------------------------------------------------

def test_cafe_no_longer_wins_dinner_against_comparable_restaurant():
    # Deliberately give the CAFE the richer catalog row (more completeness
    # signals: website/hours/wheelchair/diet/outdoor_seating/takeaway/delivery)
    # -- pre-fix, completeness/dedup tiebreaks were the ONLY thing separating a
    # tied (slot_fit=0) cafe from a real restaurant, so a well-documented cafe
    # could win dinner outright. Post-fix, slot-fit alone must settle it.
    rests = [
        {"name": "Well-Documented Cafe", "name_en": "Well-Documented Cafe",
         "category": "cafe", "cuisine": "coffee_shop",
         "website": "https://cafe.example/", "opening_hours": "07:00-22:00",
         "wheelchair": "yes", "diet": ["vegetarian"], "outdoor_seating": "yes",
         "takeaway": "yes", "delivery": "yes"},
        {"name": "Plain Bistro", "name_en": "Plain Bistro", "category": "restaurant",
         "cuisine": "local"},
    ]
    dp._POI_CATALOG["ZZ:dinnertown"] = _synth_entry(rests)
    try:
        plan = build_day_plan("dinnertown", "ZZ", "Testland", "2026-10-01", "2026-10-02")
        dinner = plan["days"][0]["meals"]["dinner"]
        assert dinner and (dinner.get("name_en") or dinner.get("name")) == "Plain Bistro", dinner
    finally:
        dp._POI_CATALOG.pop("ZZ:dinnertown", None)


def test_cafe_still_wins_dinner_when_it_is_the_only_venue():
    """Never a filter — a cafe still fills dinner (honest reuse) when it's the
    ONLY known venue in the city; the fix only de-prioritises it vs. a real
    alternative, it never removes it from the pool."""
    rests = [
        {"name": "Only Cafe In Town", "name_en": "Only Cafe In Town", "category": "cafe",
         "cuisine": "coffee_shop"},
    ]
    dp._POI_CATALOG["ZZ:cafeonly"] = _synth_entry(rests)
    try:
        plan = build_day_plan("cafeonly", "ZZ", "Testland", "2026-10-01", "2026-10-02")
        dinner = plan["days"][0]["meals"]["dinner"]
        assert dinner and (dinner.get("name_en") or dinner.get("name")) == "Only Cafe In Town"
    finally:
        dp._POI_CATALOG.pop("ZZ:cafeonly", None)


def test_breakfast_and_tea_still_prefer_the_cafe_end_to_end():
    """No regression end-to-end: the same cafe/restaurant pool still gives the
    cafe breakfast and tea (its bonus slots), only losing lunch/dinner/supper."""
    rests = [
        {"name": "Morning Cafe", "name_en": "Morning Cafe", "category": "cafe",
         "cuisine": "coffee_shop"},
        {"name": "Plain Bistro", "name_en": "Plain Bistro", "category": "restaurant",
         "cuisine": "local"},
    ]
    dp._POI_CATALOG["ZZ:allslotstown"] = _synth_entry(rests)
    try:
        plan = build_day_plan("allslotstown", "ZZ", "Testland", "2026-10-01", "2026-10-02")
        meals = plan["days"][0]["meals"]
        assert (meals["breakfast"].get("name_en")) == "Morning Cafe"
        assert (meals["tea"].get("name_en")) == "Morning Cafe"
        assert (meals["dinner"].get("name_en")) == "Plain Bistro"
    finally:
        dp._POI_CATALOG.pop("ZZ:allslotstown", None)


# ---------------------------------------------------------------------------
# 3. Third-wave / specialty coffee chains now caught by _GLOBAL_FASTFOOD_BRANDS.
# ---------------------------------------------------------------------------

def test_blue_bottle_coffee_is_now_a_recognised_chain():
    assert _is_global_fastfood({"name": "Blue Bottle Coffee"}) is True


def test_blue_bottle_coffee_branch_variant_is_caught():
    assert _is_global_fastfood({"name": "Blue Bottle Coffee Shibuya"}) is True


def test_percent_arabica_is_now_a_recognised_chain():
    assert _is_global_fastfood({"name": "%Arabica"}) is True
    assert _is_global_fastfood({"name": "%Arabica Kyoto Higashiyama"}) is True


def test_verve_coffee_is_now_a_recognised_chain():
    assert _is_global_fastfood({"name": "Verve Coffee"}) is True
    assert _is_global_fastfood({"name": "Verve Coffee Roasters"}) is True


def test_independent_local_cafe_is_still_not_flagged():
    """No regression / no over-broad match: an unrelated independent cafe must
    NOT be caught by the new brand entries."""
    assert _is_global_fastfood({"name": "Blue Sky Coffee House"}) is False
    assert _is_global_fastfood({"name": "Neighborhood Roasters"}) is False


def test_blue_bottle_coffee_capped_like_other_chains_end_to_end():
    """A Blue Bottle Coffee branch, even a SINGLE node, is treated as a chain and
    capped at <=2 appearances per leg — same mechanism as the pre-existing global
    fast-food cap (`_chain_names |= {... _is_global_fastfood ...}`)."""
    rests = [
        {"name": "Blue Bottle Coffee", "name_en": "Blue Bottle Coffee", "category": "cafe",
         "cuisine": "coffee_shop", "breakfast": True, "opening_hours": "24/7",
         "takeaway": "yes"},
        {"name": "Solo Bistro", "name_en": "Solo Bistro", "category": "restaurant",
         "cuisine": "local", "opening_hours": "24/7", "takeaway": "yes"},
    ]
    dp._POI_CATALOG["ZZ:bluebottletown"] = _synth_entry(rests)
    try:
        plan = build_day_plan("bluebottletown", "ZZ", "Testland", "2026-10-01", "2026-10-06")
        names = [(m.get("name_en") or m.get("name"))
                 for day in plan["days"] for m in day["meals"].values() if m]
        assert names.count("Blue Bottle Coffee") <= 2, f"chain must be capped at 2/trip: {names}"
        assert "Solo Bistro" in names
    finally:
        dp._POI_CATALOG.pop("ZZ:bluebottletown", None)
