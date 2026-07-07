"""test_itinerary_narration_cov3.py — #3 Phase 1: grounded payload + anti-fabrication validator.
Pure, no LLM/network. The headline guarantee: a hallucinated place can NEVER survive validation."""

from __future__ import annotations

import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOC = os.path.dirname(_HERE)
if _SOC not in sys.path:
    sys.path.insert(0, _SOC)

from utils import itinerary_narration as nar  # noqa: E402

# A minimal but realistic day_plans + legs (Cebu), shaped like day_planner_agent output.
DAY_PLANS = [
    {"leg_id": "leg-0", "city": "cebu", "country": "Philippines", "catalog_hit": True, "days": [
        {"day_index": 0, "attractions": [
            {"name": "Magellan's Cross", "name_en": "Magellan's Cross", "category": "monument",
             "opening_hours": "08:00-18:00", "website": "http://x"},
            {"name": "Basilica del Santo Niño", "category": "church"},
            {"name": "", "category": "noise"},  # no name → skipped
        ], "meals": {
            "lunch": {"name": "Larsian BBQ", "cuisine": "filipino"},
            "dinner": None,  # null slot → skipped
        }},
    ]},
    {"leg_id": "leg-1", "city": "nowhere", "catalog_hit": False, "days": []},  # miss → skipped
]
LEGS = [{"leg_id": "leg-0", "city": "cebu", "checkin": "2026-10-01", "checkout": "2026-10-04",
         "hotel_title": "Cebu Grand Hotel"}]


class TestPayloadAndCorpus(unittest.TestCase):
    def test_payload_extracts_real_items_skips_misses(self):
        p = nar.build_narration_payload(DAY_PLANS, LEGS)
        self.assertEqual(len(p["legs"]), 1)               # the catalog-miss leg is skipped
        leg = p["legs"][0]
        self.assertEqual(leg["hotel_title"], "Cebu Grand Hotel")
        day = leg["days"][0]
        names = [a["name"] for a in day["attractions"]]
        self.assertEqual(names, ["Magellan's Cross", "Basilica del Santo Niño"])  # empty-name dropped
        self.assertEqual([m["name"] for m in day["meals"]], ["Larsian BBQ"])      # null dinner dropped

    def test_ids_are_stable_and_unique(self):
        p = nar.build_narration_payload(DAY_PLANS, LEGS)
        corpus = nar.narration_corpus(p)
        self.assertEqual(set(corpus.values()),
                         {"Magellan's Cross", "Basilica del Santo Niño", "Larsian BBQ"})
        ids = list(corpus.keys())
        self.assertEqual(len(ids), len(set(ids)))         # unique

    def test_payload_deterministic(self):
        a = json.dumps(nar.build_narration_payload(DAY_PLANS, LEGS), sort_keys=True)
        b = json.dumps(nar.build_narration_payload(DAY_PLANS, LEGS), sort_keys=True)
        self.assertEqual(a, b)

    def test_catalog_miss_with_real_content_is_included(self):
        # catalog_hit False but real restaurants present (Bali-style: POIs live under sub-areas) →
        # narrate the real content; do NOT skip on catalog_hit.
        dp = [{"leg_id": "l", "city": "bali", "catalog_hit": False, "days": [
            {"day_index": 0, "attractions": [],
             "meals": {"lunch": {"name": "Warung Made"}, "dinner": None}}]}]
        p = nar.build_narration_payload(dp, [])
        self.assertEqual(len(p["legs"]), 1)
        self.assertEqual([m["name"] for m in p["legs"][0]["days"][0]["meals"]], ["Warung Made"])

    def test_malformed_meals_container_never_raises(self):
        # a non-dict meals container must not raise (.items()); attractions still extracted.
        dp = [{"leg_id": "l", "city": "x", "catalog_hit": True, "days": [
            {"day_index": 0, "attractions": [{"name": "Real Spot"}], "meals": "garbage"}]}]
        p = nar.build_narration_payload(dp, [])
        self.assertEqual([a["name"] for a in p["legs"][0]["days"][0]["attractions"]], ["Real Spot"])
        self.assertEqual(p["legs"][0]["days"][0]["meals"], [])


class TestValidatorAntiFabrication(unittest.TestCase):
    def _corpus(self):
        return nar.narration_corpus(nar.build_narration_payload(DAY_PLANS, LEGS))

    def test_hallucinated_id_dropped(self):
        corpus = self._corpus()
        narrative = {"legs": [{"leg_id": "leg-0", "city": "cebu", "days": [
            {"day_number": 1, "title": "Old Town", "narrative": "...",
             "highlights": [{"id": 99999, "name": "Eiffel Tower", "blurb": "iconic"}],  # fake id
             "dining": []}]}]}
        out = nar.validate_narrative(narrative, corpus)
        self.assertEqual(out["legs"][0]["days"][0]["highlights"], [])  # dropped
        self.assertEqual(out["_validation"]["dropped"], 1)

    def test_real_id_wrong_name_dropped(self):
        corpus = self._corpus()
        real_id = next(iter(corpus))  # a real id...
        narrative = {"legs": [{"days": [
            {"highlights": [{"id": real_id, "name": "Eiffel Tower", "blurb": "x"}], "dining": []}]}]}
        out = nar.validate_narrative(narrative, corpus)
        self.assertEqual(out["legs"][0]["days"][0]["highlights"], [])  # name mismatch → dropped

    def test_real_item_kept_and_canonicalised(self):
        corpus = self._corpus()
        mid = next(i for i, n in corpus.items() if n == "Magellan's Cross")
        narrative = {"legs": [{"days": [
            {"highlights": [{"id": mid, "name": "magellans cross", "blurb": "the marker"}],
             "dining": []}]}]}
        out = nar.validate_narrative(narrative, corpus)
        kept = out["legs"][0]["days"][0]["highlights"]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["name"], "Magellan's Cross")  # canonicalised to the real name

    def test_malformed_narrative_never_raises(self):
        self.assertEqual(nar.validate_narrative(None, {})["_validation"]["kept"], 0)
        nar.validate_narrative({"legs": "garbage"}, {})  # must not raise

    def test_string_id_is_coerced_and_kept(self):
        # a JSON-roundtripped string id ("5") still resolves — a real item is not dropped.
        corpus = self._corpus()
        mid = next(i for i, n in corpus.items() if n == "Larsian BBQ")
        narrative = {"legs": [{"days": [
            {"dining": [{"id": str(mid), "name": "Larsian BBQ", "blurb": "bbq"}], "highlights": []}]}]}
        out = nar.validate_narrative(narrative, corpus)
        self.assertEqual([d["name"] for d in out["legs"][0]["days"][0]["dining"]], ["Larsian BBQ"])

    def test_nonnumeric_id_fails_closed(self):
        corpus = self._corpus()
        narrative = {"legs": [{"days": [
            {"highlights": [{"id": "abc", "name": "Magellan's Cross"}], "dining": []}]}]}
        out = nar.validate_narrative(narrative, corpus)
        self.assertEqual(out["legs"][0]["days"][0]["highlights"], [])  # junk id dropped

    def test_cjk_real_name_kept_and_canonicalised(self):
        # CJK real name: _norm_tokens(real) is empty → match on id alone, then canonicalise to the
        # real CJK name. A fabricated emitted name still cannot surface (overwritten).
        corpus = {7: "海底捞"}
        narrative = {"legs": [{"days": [
            {"dining": [{"id": 7, "name": "Some Hotpot Place"}], "highlights": []}]}]}
        out = nar.validate_narrative(narrative, corpus)
        self.assertEqual([d["name"] for d in out["legs"][0]["days"][0]["dining"]], ["海底捞"])

    def test_shared_token_wrong_name_survives_but_is_renamed(self):
        # lenient gate: a wrong name sharing a token survives, but is canonicalised to the real name —
        # the SURFACED text is always the deterministic name, never the fake.
        corpus = self._corpus()
        cid = next(i for i, n in corpus.items() if n == "Magellan's Cross")
        narrative = {"legs": [{"days": [
            {"highlights": [{"id": cid, "name": "Magellan Beach Resort"}], "dining": []}]}]}
        out = nar.validate_narrative(narrative, corpus)
        self.assertEqual([h["name"] for h in out["legs"][0]["days"][0]["highlights"]],
                         ["Magellan's Cross"])

    def test_prose_is_intentionally_unscanned_residual(self):
        # documented residual: prose fields are NOT validated; the CHIP surface is the firewall.
        corpus = self._corpus()
        narrative = {"legs": [{"days": [
            {"narrative": "Then visit the fabulous Eiffel Tower!", "highlights": [], "dining": []}]}]}
        out = nar.validate_narrative(narrative, corpus)
        self.assertIn("Eiffel Tower", out["legs"][0]["days"][0]["narrative"])  # prose untouched
        self.assertEqual(out["legs"][0]["days"][0]["highlights"], [])          # no fabricated chip


if __name__ == "__main__":
    unittest.main(verbosity=2)
