"""
test_accommodation_lodging_type.py — #36 Accommodation Types Tier A.

Proves the pure id-slug lodging_type derivation and that surfacing it on the
proposal/alternates is purely ADDITIVE:

  1. _lodging_type_from_id: pure string parse over the OSM id slug segments
     (osm_pbf_ingest seg=`<slug(lodging_type)>-`, so real catalog ids carry
     `-hostel-/-guest-house-/-apartment-/-motel-`; plain hotels carry none →
     default "hotel"). Honesty: unknown slug → "hotel" (the OSM default),
     never resort/villa.
  2. var-0: same id twice → identical type; pure parse, no wall-clock/random.
  3. APPEND-ONLY: a propose() request with NO new field yields a proposal that
     is byte-identical to the pre-change output EXCEPT for the ADDED
     `lodging_type` key on the proposal + every alternate (the opt-in invariant
     — a plain city trip is unchanged for every existing key/value).
  4. Variance-clamp honesty: the emitted lodging_type is re-derived from the id
     slug, never the LLM's claim; the LLM can only permute real ids.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agents import accommodation_agent as aa
from agents.accommodation_agent import _lodging_type_from_id

KEY = "test-key-placeholder"


def _mock_llm_ids(ranked_ids: list[str]) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"ranked_ids": ranked_ids})}}]
    }
    return resp


def _mock_search_catalog_transport(results: list[dict]) -> httpx.BaseTransport:
    class _FakeTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {"result": {"structuredContent": {"results": results}}}
            return httpx.Response(200, json=body)

    return _FakeTransport()


# ---------------------------------------------------------------------------
# 1. Pure parse — the slug-segment derivation
# ---------------------------------------------------------------------------

class TestLodgingTypeFromId(unittest.TestCase):

    def test_hostel(self):
        self.assertEqual(_lodging_type_from_id("paris-hostel-le-village"), "hostel")

    def test_real_catalog_hostel_id(self):
        # Real catalog slug form (osm_pbf_ingest):
        self.assertEqual(
            _lodging_type_from_id(
                "aachen-hostel-djh-jugendherberge-aachen-euregionales-jugendg"
            ),
            "hostel",
        )

    def test_guest_house_hyphen_slug(self):
        # Real catalog uses the hyphenated slug form (slug() maps "_" -> "-").
        self.assertEqual(
            _lodging_type_from_id("aachen-guest-house-campus-boardinghouse"),
            "guest_house",
        )

    def test_guest_house_underscore_literal(self):
        # Spec unit example written with the OSM type name verbatim.
        self.assertEqual(
            _lodging_type_from_id("munich-guest_house-pension-am-park"),
            "guest_house",
        )

    def test_apartment(self):
        self.assertEqual(
            _lodging_type_from_id("6th-of-october-city-apartment-sunrise"),
            "apartment",
        )

    def test_motel(self):
        self.assertEqual(_lodging_type_from_id("abha-motel-roadside"), "motel")

    def test_plain_hotel_no_segment(self):
        # No recognised segment → the correct OSM default "hotel".
        self.assertEqual(_lodging_type_from_id("bali-seminyak-oberoibali"), "hotel")

    def test_empty_and_non_string_default_hotel(self):
        self.assertEqual(_lodging_type_from_id(""), "hotel")
        self.assertEqual(_lodging_type_from_id(None), "hotel")  # type: ignore[arg-type]

    def test_case_insensitive(self):
        self.assertEqual(_lodging_type_from_id("Paris-HOSTEL-Le-Village"), "hostel")

    def test_never_claims_resort_or_villa(self):
        # Tier A cannot recover resort/villa — must never invent them.
        for hid in ("bali-resort-paradise", "tuscany-villa-bella"):
            self.assertEqual(_lodging_type_from_id(hid), "hotel")

    def test_var0_deterministic(self):
        hid = "aachen-guest-house-campus-boardinghouse"
        self.assertEqual(_lodging_type_from_id(hid), _lodging_type_from_id(hid))


# ---------------------------------------------------------------------------
# 2. APPEND-ONLY: a plain request is unchanged except for the added key
# ---------------------------------------------------------------------------

class TestAppendOnlyProposalUnchanged(unittest.TestCase):

    def _catalog(self) -> list[dict]:
        return [
            {
                "hotel_id": "bali-seminyak-A",
                "title": "Hotel A",
                "total_cents": 20000,
                "review_score": 8.5,
                "star_rating": 4.0,
                "amenities": ["pool", "wifi"],
            },
            {
                "hotel_id": "bali-seminyak-B",
                "title": "Hotel B",
                "total_cents": 15000,
                "review_score": 7.0,
                "star_rating": 3.0,
                "amenities": ["wifi"],
            },
        ]

    def _run(self) -> dict:
        transport = _mock_search_catalog_transport(self._catalog())
        agent = aa.AccommodationAgent(merchant_transport=transport)
        with patch("agents.accommodation_agent.DASHSCOPE_API_KEY", KEY), \
             patch("httpx.post", return_value=_mock_llm_ids(
                 ["bali-seminyak-A", "bali-seminyak-B"]
             )):
            return agent._call_catalog(
                city="bali",
                checkin="2026-10-01",
                checkout="2026-10-04",
                adults=1,
                max_cents=30000,
                vibe="beach",
            )

    # Pre-change proposal keys (the exact dict shape before #36). lodging_type is
    # the ONLY key #36 adds — everything else must be byte-identical.
    _PRE_CHANGE_KEYS = {
        "hotel_id", "title", "total_cents", "review_score",
        "star_rating", "amenities", "area", "ranking_source",
    }

    def test_proposal_adds_only_lodging_type(self):
        result = self._run()
        self.assertEqual(result["fit"], "ok")
        prop = result["proposal"]
        added = set(prop) - self._PRE_CHANGE_KEYS
        self.assertEqual(added, {"lodging_type"},
                         "proposal must add ONLY the lodging_type key")
        # Every pre-existing key still present (nothing dropped).
        self.assertTrue(self._PRE_CHANGE_KEYS.issubset(set(prop)))

    def test_existing_values_unchanged(self):
        result = self._run()
        prop = result["proposal"]
        # Strip the new key → must equal the exact pre-change proposal dict.
        without_new = {k: v for k, v in prop.items() if k != "lodging_type"}
        self.assertEqual(without_new, {
            "hotel_id": "bali-seminyak-A",
            "title": "Hotel A",
            "total_cents": 20000,
            "review_score": 8.5,
            "star_rating": 4.0,
            "amenities": ["pool", "wifi"],
            "area": "seminyak",
            "ranking_source": "llm",
        })

    def test_plain_hotel_typed_hotel(self):
        # Bali ids carry no type segment → "hotel"; no new keys/values leak.
        result = self._run()
        self.assertEqual(result["proposal"]["lodging_type"], "hotel")
        for alt in result["alternates"]:
            self.assertEqual(alt["lodging_type"], "hotel")
            self.assertEqual(set(alt) - self._PRE_CHANGE_KEYS, {"lodging_type"})

    def test_var0_byte_identical_proposal(self):
        a = json.dumps(self._run(), sort_keys=True)
        b = json.dumps(self._run(), sort_keys=True)
        self.assertEqual(a, b)


# ---------------------------------------------------------------------------
# 3. Honesty / variance-clamp: type is from the id slug, never the LLM
# ---------------------------------------------------------------------------

class TestTypeFromSlugNotLLM(unittest.TestCase):

    def test_typed_inventory_surfaced_from_slug(self):
        catalog = [
            {
                "hotel_id": "berlin-hostel-circus",
                "title": "Circus Hostel",
                "total_cents": 6000,
                "review_score": 8.8,
                "star_rating": 2.0,
                "amenities": ["wifi"],
            },
            {
                "hotel_id": "berlin-apartment-mitte",
                "title": "Mitte Apartment",
                "total_cents": 9000,
                "review_score": 8.2,
                "star_rating": 3.0,
                "amenities": ["kitchen", "wifi"],
            },
        ]
        transport = _mock_search_catalog_transport(catalog)
        agent = aa.AccommodationAgent(merchant_transport=transport)
        with patch("agents.accommodation_agent.DASHSCOPE_API_KEY", KEY), \
             patch("httpx.post", return_value=_mock_llm_ids(
                 ["berlin-hostel-circus", "berlin-apartment-mitte"]
             )):
            result = agent._call_catalog(
                city="berlin",
                checkin="2026-10-01",
                checkout="2026-10-04",
                adults=1,
                max_cents=20000,
                vibe="backpacker",
            )
        self.assertEqual(result["proposal"]["hotel_id"], "berlin-hostel-circus")
        self.assertEqual(result["proposal"]["lodging_type"], "hostel")
        # The alternate apartment carries its own slug-derived type.
        types = {a["hotel_id"]: a["lodging_type"] for a in result["alternates"]}
        self.assertEqual(types.get("berlin-apartment-mitte"), "apartment")


# ---------------------------------------------------------------------------
# 4. End-to-end family regression, LLM OFF — the critical missing guard
#
# NOTE: there used to be a unit-test class here for a `_apply_lodging_type_policy`
# stable-partition-reorder helper. That function was DEAD production code — a
# duplicate, semantically-different lodging-type mechanism that `_call_catalog`
# never actually called (the REAL, live mechanism is the inline avoid-filter +
# prefer-sort inside `_call_catalog`, exercised end-to-end below). Its unit tests
# gave false confidence: reverting the real wiring would not have failed them.
# The function + its tests were removed; the e2e-style tests below (which drive
# the real `_call_catalog` inline mechanism) are the genuine coverage.
# ---------------------------------------------------------------------------

class TestFamilyLodgingTypePolicyLLMOff(unittest.TestCase):

    def test_hostel_outranks_apartment_by_review_score_but_apartment_wins(self):
        # Hostel has a HIGHER review_score than the apartment — under plain
        # fallback ranking (review_score desc) the hostel would win. The family
        # persona policy must still float the apartment to the head.
        catalog = [
            {
                "hotel_id": "hanoi-hostel-old-quarter",
                "title": "Old Quarter Hostel",
                "total_cents": 8000,
                "review_score": 9.2,
                "star_rating": 3.0,
                "amenities": ["wifi"],
            },
            {
                "hotel_id": "hanoi-apartment-tay-ho",
                "title": "Tay Ho Apartment",
                "total_cents": 12000,
                "review_score": 8.0,
                "star_rating": 4.0,
                "amenities": ["kitchen", "wifi"],
            },
        ]
        transport = _mock_search_catalog_transport(catalog)
        agent = aa.AccommodationAgent(merchant_transport=transport)
        with patch("agents.accommodation_agent.DASHSCOPE_API_KEY", ""):   # LLM OFF
            result = agent._call_catalog(
                city="hanoi",
                checkin="2026-10-01",
                checkout="2026-10-04",
                adults=2,
                max_cents=30000,
                vibe=None,
                prefer_lodging_types=["apartment", "guest_house"],
                avoid_lodging_types=["hostel", "motel"],
            )
        self.assertEqual(result["fit"], "ok")
        self.assertEqual(result["proposal"]["ranking_source"], "fallback")  # confirms LLM OFF
        self.assertEqual(result["proposal"]["hotel_id"], "hanoi-apartment-tay-ho")
        self.assertEqual(result["proposal"]["lodging_type"], "apartment")
        self.assertNotIn("lodging_type_compromise", result["proposal"])

    def test_only_avoided_type_available_is_still_served_honestly(self):
        # Catalog returns ONLY a hostel under budget -> the family persona must
        # still get a proposal (never a false decline), honestly flagged.
        catalog = [
            {
                "hotel_id": "hanoi-hostel-old-quarter",
                "title": "Old Quarter Hostel",
                "total_cents": 8000,
                "review_score": 9.2,
                "star_rating": 3.0,
                "amenities": ["wifi"],
            },
        ]
        transport = _mock_search_catalog_transport(catalog)
        agent = aa.AccommodationAgent(merchant_transport=transport)
        with patch("agents.accommodation_agent.DASHSCOPE_API_KEY", ""):   # LLM OFF
            result = agent._call_catalog(
                city="hanoi",
                checkin="2026-10-01",
                checkout="2026-10-04",
                adults=2,
                max_cents=30000,
                vibe=None,
                prefer_lodging_types=["apartment", "guest_house"],
                avoid_lodging_types=["hostel", "motel"],
            )
        self.assertEqual(result["fit"], "ok")   # honest last-resort, never a false decline
        self.assertEqual(result["proposal"]["hotel_id"], "hanoi-hostel-old-quarter")
        self.assertEqual(result["proposal"]["lodging_type"], "hostel")
        self.assertTrue(result["proposal"].get("lodging_type_compromise"))
        self.assertTrue((result["proposal"].get("note") or "").strip())


if __name__ == "__main__":
    unittest.main()
