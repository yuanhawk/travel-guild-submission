"""
test_accommodation_ranking.py — Unit tests for M-Agentic-3 LLM ranking in
the AccommodationAgent (variance-clamped hotel ranking edge).

CI-safe: all LLM (DashScope) calls are mocked. No network required.
Mirrors the pattern of test_destination_agent.py for consistency.

Run with:
    python test_accommodation_ranking.py -v

Coverage (per M-Agentic-3 task spec):
  1.  Permutation invariant: LLM returns out-of-set hotel_id → dropped, real
      ids preserved in order; omitted real ids appended deterministically.
  2.  Permutation invariant: LLM omits ids → appended in review_score-desc order.
  3.  Permutation invariant: LLM returns garbage (non-JSON / empty) → review_score
      fallback (current deterministic floor).
  4.  "Highest-ranked within ceiling" pick logic: picks first LLM-ranked candidate
      whose total_cents ≤ max_cents, regardless of LLM's position for over-ceiling
      entries.
  5.  LLM can never cause an over-ceiling pick (budget safety is structural).
  6.  _clamp_ranking: LLM returning all invented ids → 100 % fallback, result
      equals full candidate set ranked by review_score.
  7.  _clamp_ranking: duplicate ids in LLM output are de-duplicated.
  8.  _pick_within_ceiling: all candidates over ceiling → None (no_fit).
  9.  Full _call_catalog path via AccommodationAgent with mocked merchant +
      mocked LLM: verifies end-to-end flow including ranking_source tag.
 10.  LLM unavailable (key not set) → deterministic fallback (no crash).
 11.  (finding #9) Price-unknown rows (missing/null/<=0 total_cents) are skipped
      by _pick_within_ceiling and the alternates loop — never treated as free.
 12.  (finding #55 / D6) Fallback sort tiebreak on hotel_id is stable.
 13.  (finding #56) adults=0 or explicitly invalid raises ValueError.
 14.  (finding #32) Alternates cap is respected; configurable via env var.
 15.  (findings #33/#36) _filter_by_area conservative no-op when area set unknown.
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
from agents.accommodation_agent import (
    _clamp_ranking,
    _filter_by_area,
    _llm_rank_hotels,
    _occupancy_ok,
    _pick_within_ceiling,
    _rank_candidates,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

KEY = "test-key-placeholder"


def _make_candidates(specs: list[dict]) -> list[dict[str, Any]]:
    """
    Build a minimal candidate list.

    Each spec: {"hotel_id": str, "total_cents": int, "review_score": float, ...}
    """
    defaults = {"title": "", "area": "", "star_rating": 3.0, "amenities": []}
    return [{**defaults, **s} for s in specs]


def _mock_llm_ids(ranked_ids: list[str]) -> MagicMock:
    """Mock httpx.post so _llm_rank_hotels returns {"ranked_ids": ranked_ids}."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"ranked_ids": ranked_ids})}}]
    }
    return resp


def _mock_llm_text(text: str) -> MagicMock:
    """Mock httpx.post so _llm_rank_hotels returns raw (possibly garbage) text."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"content": text}}]}
    return resp


def _mock_search_catalog_transport(results: list[dict]) -> httpx.BaseTransport:
    """
    Build an httpx BaseTransport that returns the given results from search_catalog.
    Used to inject into AccommodationAgent for end-to-end tests.
    """
    class _FakeTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {
                "result": {
                    "structuredContent": {
                        "results": results,
                    }
                }
            }
            return httpx.Response(200, json=body)

    return _FakeTransport()


# ---------------------------------------------------------------------------
# 1. Permutation invariant: out-of-set hotel_id dropped; omitted appended
# ---------------------------------------------------------------------------

class TestClampRankingDropsInvented(unittest.TestCase):

    def test_invented_id_dropped(self):
        """LLM returns an invented hotel_id → it is dropped from the result."""
        candidates = _make_candidates([
            {"hotel_id": "bali-ubud-A", "total_cents": 10000, "review_score": 8.0},
            {"hotel_id": "bali-ubud-B", "total_cents": 12000, "review_score": 7.5},
        ])
        # LLM returns real A, invented id, real B — invented should be dropped
        llm_ids = ["bali-ubud-A", "atlantis-fake-hotel", "bali-ubud-B"]
        ranked, source = _clamp_ranking(llm_ids, candidates)

        ids_out = [r["hotel_id"] for r in ranked]
        self.assertNotIn("atlantis-fake-hotel", ids_out)
        self.assertIn("bali-ubud-A", ids_out)
        self.assertIn("bali-ubud-B", ids_out)
        self.assertEqual(len(ranked), len(candidates))  # complete set
        self.assertEqual(source, "llm")

    def test_real_ids_order_preserved(self):
        """LLM ordering of real ids is preserved in the output."""
        candidates = _make_candidates([
            {"hotel_id": "h1", "total_cents": 10000, "review_score": 9.0},
            {"hotel_id": "h2", "total_cents": 10000, "review_score": 8.0},
            {"hotel_id": "h3", "total_cents": 10000, "review_score": 7.0},
        ])
        llm_ids = ["h3", "h1", "h2"]  # LLM prefers h3
        ranked, source = _clamp_ranking(llm_ids, candidates)

        ids_out = [r["hotel_id"] for r in ranked]
        self.assertEqual(ids_out, ["h3", "h1", "h2"])
        self.assertEqual(source, "llm")


# ---------------------------------------------------------------------------
# 2. Permutation invariant: LLM omits ids → appended in review_score-desc order
# ---------------------------------------------------------------------------

class TestClampRankingAppendsOmitted(unittest.TestCase):

    def test_omitted_ids_appended_by_review_score(self):
        """LLM omits hotel_ids → they are appended in deterministic review_score-desc."""
        candidates = _make_candidates([
            {"hotel_id": "h1", "total_cents": 10000, "review_score": 9.5},
            {"hotel_id": "h2", "total_cents": 10000, "review_score": 8.0},
            {"hotel_id": "h3", "total_cents": 10000, "review_score": 7.0},
            {"hotel_id": "h4", "total_cents": 10000, "review_score": 6.0},
        ])
        llm_ids = ["h3"]  # LLM only ranked one; h1, h2, h4 omitted
        ranked, source = _clamp_ranking(llm_ids, candidates)

        ids_out = [r["hotel_id"] for r in ranked]
        # h3 first (LLM choice), then h1 > h2 > h4 by review_score
        self.assertEqual(ids_out[0], "h3")
        self.assertEqual(ids_out[1], "h1")
        self.assertEqual(ids_out[2], "h2")
        self.assertEqual(ids_out[3], "h4")
        self.assertEqual(len(ids_out), 4)  # complete set

    def test_result_always_complete_set(self):
        """Regardless of LLM output, result always contains exactly the input set."""
        candidates = _make_candidates([
            {"hotel_id": "x", "total_cents": 5000, "review_score": 7.0},
            {"hotel_id": "y", "total_cents": 6000, "review_score": 8.0},
            {"hotel_id": "z", "total_cents": 7000, "review_score": 9.0},
        ])
        for llm_ids in (
            ["z", "y", "x"],          # full ordering
            ["z"],                     # partial
            ["z", "invented-id"],      # partial + invented
            [],                        # empty → fallback
        ):
            ranked, _ = _clamp_ranking(llm_ids, candidates)
            self.assertEqual(
                {r["hotel_id"] for r in ranked},
                {"x", "y", "z"},
                f"set not preserved for llm_ids={llm_ids}",
            )
            self.assertEqual(len(ranked), 3)


# ---------------------------------------------------------------------------
# 3. Permutation invariant: garbage LLM output → review_score fallback
# ---------------------------------------------------------------------------

class TestClampRankingGarbageFallback(unittest.TestCase):

    def test_none_llm_ids_gives_fallback(self):
        """_clamp_ranking(None, candidates) → fallback, review_score-desc order."""
        candidates = _make_candidates([
            {"hotel_id": "a", "total_cents": 10000, "review_score": 6.0},
            {"hotel_id": "b", "total_cents": 10000, "review_score": 9.0},
            {"hotel_id": "c", "total_cents": 10000, "review_score": 7.5},
        ])
        ranked, source = _clamp_ranking(None, candidates)
        self.assertEqual(source, "fallback")
        ids_out = [r["hotel_id"] for r in ranked]
        self.assertEqual(ids_out, ["b", "c", "a"])  # review_score desc

    def test_empty_list_gives_fallback(self):
        """_clamp_ranking([], candidates) → fallback."""
        candidates = _make_candidates([
            {"hotel_id": "p", "total_cents": 10000, "review_score": 8.0},
            {"hotel_id": "q", "total_cents": 10000, "review_score": 5.0},
        ])
        ranked, source = _clamp_ranking([], candidates)
        self.assertEqual(source, "fallback")
        self.assertEqual([r["hotel_id"] for r in ranked], ["p", "q"])

    def test_all_invented_gives_fallback(self):
        """LLM returns only invented ids → source=fallback, full set by review_score."""
        candidates = _make_candidates([
            {"hotel_id": "real1", "total_cents": 10000, "review_score": 7.0},
            {"hotel_id": "real2", "total_cents": 10000, "review_score": 9.0},
        ])
        ranked, source = _clamp_ranking(["fake-A", "fake-B", "fake-C"], candidates)
        self.assertEqual(source, "fallback")
        ids_out = [r["hotel_id"] for r in ranked]
        self.assertIn("real1", ids_out)
        self.assertIn("real2", ids_out)
        # fallback order: review_score desc → real2 first
        self.assertEqual(ids_out[0], "real2")

    def test_llm_raw_garbage_json_gives_fallback(self):
        """When httpx returns garbage text, _llm_rank_hotels returns None → fallback."""
        candidates = _make_candidates([
            {"hotel_id": "h1", "total_cents": 10000, "review_score": 8.0},
        ])
        with patch("agents.accommodation_agent.DASHSCOPE_API_KEY", KEY), \
             patch("httpx.post", return_value=_mock_llm_text("NOT JSON AT ALL @@##$$")):
            llm_ids = _llm_rank_hotels(candidates, "beach", None)
        self.assertIsNone(llm_ids)
        # _clamp_ranking with None → fallback
        ranked, source = _clamp_ranking(llm_ids, candidates)
        self.assertEqual(source, "fallback")

    def test_llm_empty_response_gives_fallback(self):
        """Empty ranked_ids from LLM → fallback in _clamp_ranking."""
        candidates = _make_candidates([
            {"hotel_id": "h1", "total_cents": 10000, "review_score": 8.0},
            {"hotel_id": "h2", "total_cents": 10000, "review_score": 6.0},
        ])
        with patch("agents.accommodation_agent.DASHSCOPE_API_KEY", KEY), \
             patch("httpx.post", return_value=_mock_llm_ids([])):
            llm_ids = _llm_rank_hotels(candidates, "relax", None)
        # empty list returned
        self.assertEqual(llm_ids, [])
        ranked, source = _clamp_ranking(llm_ids, candidates)
        self.assertEqual(source, "fallback")
        self.assertEqual(len(ranked), 2)


# ---------------------------------------------------------------------------
# 4. "Highest-ranked within ceiling" pick logic
# ---------------------------------------------------------------------------

class TestPickWithinCeiling(unittest.TestCase):

    def test_picks_first_within_ceiling(self):
        """Picks the first (highest-ranked) candidate whose total_cents ≤ max_cents."""
        candidates = _make_candidates([
            {"hotel_id": "luxury", "total_cents": 50000, "review_score": 9.5},
            {"hotel_id": "mid",    "total_cents": 30000, "review_score": 8.0},
            {"hotel_id": "budget", "total_cents": 15000, "review_score": 7.0},
        ])
        winner = _pick_within_ceiling(candidates, max_cents=35000)
        self.assertIsNotNone(winner)
        # "luxury" exceeds ceiling; "mid" is the first within ceiling
        self.assertEqual(winner["hotel_id"], "mid")

    def test_picks_top_when_all_fit(self):
        """When all candidates fit, picks the first (LLM top-ranked)."""
        candidates = _make_candidates([
            {"hotel_id": "A", "total_cents": 10000, "review_score": 9.0},
            {"hotel_id": "B", "total_cents": 11000, "review_score": 8.0},
        ])
        winner = _pick_within_ceiling(candidates, max_cents=20000)
        self.assertEqual(winner["hotel_id"], "A")

    def test_returns_none_when_all_exceed(self):
        """All candidates exceed the ceiling → None."""
        candidates = _make_candidates([
            {"hotel_id": "X", "total_cents": 50000, "review_score": 9.0},
            {"hotel_id": "Y", "total_cents": 60000, "review_score": 8.5},
        ])
        winner = _pick_within_ceiling(candidates, max_cents=10000)
        self.assertIsNone(winner)

    def test_exact_ceiling_is_accepted(self):
        """A candidate exactly at the ceiling is accepted (≤ not <)."""
        candidates = _make_candidates([
            {"hotel_id": "exact", "total_cents": 20000, "review_score": 8.0},
        ])
        winner = _pick_within_ceiling(candidates, max_cents=20000)
        self.assertIsNotNone(winner)
        self.assertEqual(winner["hotel_id"], "exact")


# ---------------------------------------------------------------------------
# 5. LLM can never cause an over-ceiling pick
# ---------------------------------------------------------------------------

class TestBudgetSafetyStructural(unittest.TestCase):

    def test_llm_ordering_cannot_force_over_ceiling_pick(self):
        """
        Even if LLM ranks the most expensive hotel first, the pick is always
        within the ceiling (structural check, not delegated to LLM ordering).
        """
        candidates = _make_candidates([
            {"hotel_id": "cheap",    "total_cents": 10000, "review_score": 6.0},
            {"hotel_id": "mid",      "total_cents": 25000, "review_score": 7.0},
            {"hotel_id": "priciest", "total_cents": 99000, "review_score": 9.0},
        ])
        max_cents = 30000

        # LLM places priciest first (it's the "nicest")
        with patch("agents.accommodation_agent.DASHSCOPE_API_KEY", KEY), \
             patch("httpx.post", return_value=_mock_llm_ids(
                 ["priciest", "mid", "cheap"]
             )):
            ranked, source = _rank_candidates(candidates, "luxury", None)

        winner = _pick_within_ceiling(ranked, max_cents=max_cents)
        self.assertIsNotNone(winner)
        # Must be within ceiling — never "priciest" (99000 > 30000)
        self.assertLessEqual(winner["total_cents"], max_cents)
        self.assertNotEqual(winner["hotel_id"], "priciest")
        # Should be "mid" (LLM's 2nd choice, within ceiling)
        self.assertEqual(winner["hotel_id"], "mid")

    def test_llm_never_invents_hotel_to_exceed_budget(self):
        """
        LLM returning an invented hotel_id (not in candidate set) is clamped out;
        the invented entry can never appear in the result or drive a pick.
        """
        candidates = _make_candidates([
            {"hotel_id": "real", "total_cents": 8000, "review_score": 7.0},
        ])
        max_cents = 10000

        with patch("agents.accommodation_agent.DASHSCOPE_API_KEY", KEY), \
             patch("httpx.post", return_value=_mock_llm_ids(
                 ["invented-hotel-999999cents", "real"]
             )):
            ranked, _ = _rank_candidates(candidates, "beach", None)

        ids = [r["hotel_id"] for r in ranked]
        self.assertNotIn("invented-hotel-999999cents", ids)
        self.assertEqual(ids, ["real"])

        winner = _pick_within_ceiling(ranked, max_cents=max_cents)
        self.assertIsNotNone(winner)
        self.assertEqual(winner["hotel_id"], "real")
        self.assertLessEqual(winner["total_cents"], max_cents)


# ---------------------------------------------------------------------------
# 6. Duplicate ids in LLM output are de-duplicated
# ---------------------------------------------------------------------------

class TestClampRankingDeduplicates(unittest.TestCase):

    def test_duplicate_ids_collapsed(self):
        """LLM returning the same hotel_id multiple times → appears once in result."""
        candidates = _make_candidates([
            {"hotel_id": "A", "total_cents": 10000, "review_score": 8.0},
            {"hotel_id": "B", "total_cents": 10000, "review_score": 7.0},
        ])
        llm_ids = ["A", "A", "B", "A"]
        ranked, source = _clamp_ranking(llm_ids, candidates)

        ids_out = [r["hotel_id"] for r in ranked]
        self.assertEqual(ids_out.count("A"), 1)
        self.assertEqual(ids_out.count("B"), 1)
        self.assertEqual(len(ranked), 2)


# ---------------------------------------------------------------------------
# 7. LLM unavailable → deterministic fallback (no crash)
# ---------------------------------------------------------------------------

class TestLLMUnavailable(unittest.TestCase):

    def test_no_api_key_gives_fallback(self):
        """When DASHSCOPE_API_KEY is empty, _llm_rank_hotels returns None → fallback."""
        candidates = _make_candidates([
            {"hotel_id": "h1", "total_cents": 10000, "review_score": 7.0},
            {"hotel_id": "h2", "total_cents": 10000, "review_score": 9.0},
        ])
        with patch("agents.accommodation_agent.DASHSCOPE_API_KEY", ""):
            llm_ids = _llm_rank_hotels(candidates, "beach", None)
        self.assertIsNone(llm_ids)

        ranked, source = _clamp_ranking(llm_ids, candidates)
        self.assertEqual(source, "fallback")
        # Fallback → review_score desc → h2 first
        self.assertEqual(ranked[0]["hotel_id"], "h2")

    def test_network_error_gives_fallback(self):
        """If httpx raises, _llm_rank_hotels returns None gracefully."""
        candidates = _make_candidates([
            {"hotel_id": "h1", "total_cents": 10000, "review_score": 8.0},
        ])

        def _raise(*args, **kwargs):
            raise httpx.ConnectError("connection refused")

        with patch("agents.accommodation_agent.DASHSCOPE_API_KEY", KEY), \
             patch("httpx.post", side_effect=_raise):
            llm_ids = _llm_rank_hotels(candidates, "surf", None)
        self.assertIsNone(llm_ids)


# ---------------------------------------------------------------------------
# 8. End-to-end _call_catalog via AccommodationAgent (mocked merchant + LLM)
# ---------------------------------------------------------------------------

class TestCallCatalogEndToEnd(unittest.TestCase):

    def _catalog_result(self) -> list[dict]:
        """Fake merchant catalog results."""
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
            {
                "hotel_id": "bali-seminyak-C",
                "title": "Hotel C",
                "total_cents": 25000,
                "review_score": 9.0,
                "star_rating": 5.0,
                "amenities": ["pool", "spa", "wifi"],
            },
        ]

    def test_llm_ranking_applied_and_source_tagged(self):
        """LLM ranking reorders candidates; winner has ranking_source='llm'."""
        catalog = self._catalog_result()
        transport = _mock_search_catalog_transport(catalog)
        agent = aa.AccommodationAgent(merchant_transport=transport)

        # LLM prefers C > B > A (but C costs 25000; we'll test ceiling)
        with patch("agents.accommodation_agent.DASHSCOPE_API_KEY", KEY), \
             patch("httpx.post", return_value=_mock_llm_ids(
                 ["bali-seminyak-C", "bali-seminyak-B", "bali-seminyak-A"]
             )):
            result = agent._call_catalog(
                city="bali",
                checkin="2026-10-01",
                checkout="2026-10-04",
                adults=1,
                max_cents=30000,
                vibe="beach",
            )

        self.assertEqual(result["fit"], "ok")
        # C is top-ranked LLM choice and fits under 30000 → picked
        self.assertEqual(result["proposal"]["hotel_id"], "bali-seminyak-C")
        self.assertEqual(result["proposal"]["ranking_source"], "llm")

    def test_ceiling_enforced_when_top_llm_pick_over_budget(self):
        """If LLM's top pick exceeds ceiling, the next within-ceiling pick is taken."""
        catalog = self._catalog_result()
        transport = _mock_search_catalog_transport(catalog)
        agent = aa.AccommodationAgent(merchant_transport=transport)

        # LLM prefers C (25000) then A (20000) then B (15000)
        # Ceiling = 22000 → C is too expensive; A (20000) is the pick
        with patch("agents.accommodation_agent.DASHSCOPE_API_KEY", KEY), \
             patch("httpx.post", return_value=_mock_llm_ids(
                 ["bali-seminyak-C", "bali-seminyak-A", "bali-seminyak-B"]
             )):
            result = agent._call_catalog(
                city="bali",
                checkin="2026-10-01",
                checkout="2026-10-04",
                adults=1,
                max_cents=22000,
                vibe="beach",
            )

        self.assertEqual(result["fit"], "ok")
        self.assertEqual(result["proposal"]["hotel_id"], "bali-seminyak-A")
        self.assertLessEqual(result["proposal"]["total_cents"], 22000)
        self.assertEqual(result["proposal"]["ranking_source"], "llm")

    def test_fallback_ranking_used_when_llm_garbage(self):
        """If LLM returns garbage, fallback (review_score-desc) is used → source='fallback'."""
        catalog = self._catalog_result()
        transport = _mock_search_catalog_transport(catalog)
        agent = aa.AccommodationAgent(merchant_transport=transport)

        with patch("agents.accommodation_agent.DASHSCOPE_API_KEY", KEY), \
             patch("httpx.post", return_value=_mock_llm_text("%%NOT JSON%%")):
            result = agent._call_catalog(
                city="bali",
                checkin="2026-10-01",
                checkout="2026-10-04",
                adults=1,
                max_cents=30000,
                vibe="relax",
            )

        self.assertEqual(result["fit"], "ok")
        # Fallback → review_score desc → C (9.0) → C is within 30000 → picked
        self.assertEqual(result["proposal"]["hotel_id"], "bali-seminyak-C")
        self.assertEqual(result["proposal"]["ranking_source"], "fallback")

    def test_no_fit_when_all_exceed_ceiling(self):
        """no_fit is returned when all candidates exceed the ceiling."""
        catalog = self._catalog_result()  # cheapest is 15000
        transport = _mock_search_catalog_transport(catalog)
        agent = aa.AccommodationAgent(merchant_transport=transport)

        with patch("agents.accommodation_agent.DASHSCOPE_API_KEY", KEY), \
             patch("httpx.post", return_value=_mock_llm_ids(
                 ["bali-seminyak-A", "bali-seminyak-B", "bali-seminyak-C"]
             )):
            result = agent._call_catalog(
                city="bali",
                checkin="2026-10-01",
                checkout="2026-10-04",
                adults=1,
                max_cents=5000,   # below all candidates
                vibe="beach",
            )

        self.assertEqual(result["fit"], "no_fit")
        self.assertIsNone(result["proposal"])

    def test_alternates_are_within_ceiling(self):
        """Alternates returned in the proposal are all within the budget ceiling."""
        catalog = self._catalog_result()
        transport = _mock_search_catalog_transport(catalog)
        agent = aa.AccommodationAgent(merchant_transport=transport)

        # LLM: C > A > B; ceiling 25000 (all fit)
        with patch("agents.accommodation_agent.DASHSCOPE_API_KEY", KEY), \
             patch("httpx.post", return_value=_mock_llm_ids(
                 ["bali-seminyak-C", "bali-seminyak-A", "bali-seminyak-B"]
             )):
            result = agent._call_catalog(
                city="bali",
                checkin="2026-10-01",
                checkout="2026-10-04",
                adults=1,
                max_cents=25000,
                vibe="luxury",
            )

        self.assertEqual(result["fit"], "ok")
        for alt in result.get("alternates", []):
            self.assertLessEqual(
                alt["total_cents"], 25000,
                f"alternate {alt['hotel_id']} exceeds ceiling",
            )


# ---------------------------------------------------------------------------
# 11. Finding #9 — price-unknown rows are skipped, never treated as free
# ---------------------------------------------------------------------------

class TestPriceUnknownSkipped(unittest.TestCase):
    """_pick_within_ceiling must skip rows with missing/null/<=0 total_cents."""

    def test_missing_total_cents_skipped(self):
        """A row with no total_cents key is skipped (not treated as 0¢ / free)."""
        candidates = _make_candidates([
            {"hotel_id": "no-price", "review_score": 9.5},             # missing total_cents
            {"hotel_id": "priced",   "total_cents": 20000, "review_score": 7.0},
        ])
        winner = _pick_within_ceiling(candidates, max_cents=50000)
        self.assertIsNotNone(winner)
        self.assertEqual(winner["hotel_id"], "priced")

    def test_null_total_cents_skipped(self):
        """A row with total_cents=None is skipped."""
        candidates = _make_candidates([
            {"hotel_id": "null-price", "total_cents": None, "review_score": 9.0},
            {"hotel_id": "priced",     "total_cents": 15000, "review_score": 6.0},
        ])
        winner = _pick_within_ceiling(candidates, max_cents=50000)
        self.assertIsNotNone(winner)
        self.assertEqual(winner["hotel_id"], "priced")

    def test_zero_total_cents_skipped(self):
        """A row with total_cents=0 is skipped (not treated as free)."""
        candidates = _make_candidates([
            {"hotel_id": "zero-price", "total_cents": 0,     "review_score": 9.0},
            {"hotel_id": "priced",     "total_cents": 10000, "review_score": 5.0},
        ])
        winner = _pick_within_ceiling(candidates, max_cents=50000)
        self.assertIsNotNone(winner)
        self.assertEqual(winner["hotel_id"], "priced")

    def test_all_price_unknown_returns_none(self):
        """When all rows have missing/null/<=0 total_cents, returns None (no_fit)."""
        candidates = _make_candidates([
            {"hotel_id": "a", "total_cents": 0,    "review_score": 9.0},
            {"hotel_id": "b", "total_cents": None, "review_score": 8.0},
            {"hotel_id": "c",                      "review_score": 7.0},
        ])
        winner = _pick_within_ceiling(candidates, max_cents=99999)
        self.assertIsNone(winner)

    def test_price_unknown_skipped_in_alternates(self):
        """Price-unknown rows are also excluded from alternates in _call_catalog."""
        catalog = [
            {"hotel_id": "bali-ubud-A", "title": "A", "total_cents": 20000,
             "review_score": 9.0, "star_rating": 4.0, "amenities": []},
            {"hotel_id": "bali-ubud-B", "title": "B", "total_cents": None,
             "review_score": 8.5, "star_rating": 4.0, "amenities": []},   # null price
            {"hotel_id": "bali-ubud-C", "title": "C", "total_cents": 15000,
             "review_score": 7.0, "star_rating": 3.0, "amenities": []},
        ]
        transport = _mock_search_catalog_transport(catalog)
        agent = aa.AccommodationAgent(merchant_transport=transport)

        with patch("agents.accommodation_agent.DASHSCOPE_API_KEY", ""):
            result = agent._call_catalog(
                city="bali",
                checkin="2026-10-01",
                checkout="2026-10-04",
                adults=1,
                max_cents=50000,
                vibe=None,
            )

        self.assertEqual(result["fit"], "ok")
        # B (null price) must not appear in proposal or alternates
        all_ids = {result["proposal"]["hotel_id"]}
        all_ids |= {a["hotel_id"] for a in result.get("alternates", [])}
        self.assertNotIn("bali-ubud-B", all_ids, "price-unknown hotel B should not appear")
        for alt in result.get("alternates", []):
            self.assertGreater(
                alt["total_cents"], 0,
                f"alternate {alt['hotel_id']} has non-positive total_cents",
            )


# ---------------------------------------------------------------------------
# 12. Finding #55 / D6 — fallback sort has stable tiebreak on hotel_id
# ---------------------------------------------------------------------------

class TestFallbackSortTiebreak(unittest.TestCase):
    """Equal review_scores must be broken deterministically by hotel_id."""

    def test_equal_review_scores_broken_by_hotel_id(self):
        """Two hotels with the same review_score are sorted by hotel_id (asc)."""
        candidates = _make_candidates([
            {"hotel_id": "z-hotel", "total_cents": 10000, "review_score": 8.0},
            {"hotel_id": "a-hotel", "total_cents": 10000, "review_score": 8.0},
            {"hotel_id": "m-hotel", "total_cents": 10000, "review_score": 8.0},
        ])
        ranked, source = _clamp_ranking(None, candidates)
        self.assertEqual(source, "fallback")
        ids_out = [r["hotel_id"] for r in ranked]
        # All equal review_score → sorted by hotel_id ascending
        self.assertEqual(ids_out, sorted(ids_out), "tiebreak should be hotel_id asc")

    def test_tiebreak_stable_across_calls(self):
        """Calling _clamp_ranking twice with the same input yields the same order."""
        candidates = _make_candidates([
            {"hotel_id": "b", "total_cents": 10000, "review_score": 7.0},
            {"hotel_id": "a", "total_cents": 10000, "review_score": 7.0},
            {"hotel_id": "c", "total_cents": 10000, "review_score": 9.0},
        ])
        ranked1, _ = _clamp_ranking(None, candidates)
        ranked2, _ = _clamp_ranking(None, candidates)
        self.assertEqual(
            [r["hotel_id"] for r in ranked1],
            [r["hotel_id"] for r in ranked2],
        )

    def test_tiebreak_also_in_omitted_tail(self):
        """Omitted-tail in the LLM path also uses hotel_id tiebreak for ties."""
        candidates = _make_candidates([
            {"hotel_id": "z-tail", "total_cents": 10000, "review_score": 5.0},
            {"hotel_id": "a-tail", "total_cents": 10000, "review_score": 5.0},
            {"hotel_id": "first",  "total_cents": 10000, "review_score": 9.0},
        ])
        # LLM picks "first"; "a-tail" and "z-tail" are omitted → appended by tiebreak
        ranked, source = _clamp_ranking(["first"], candidates)
        ids_out = [r["hotel_id"] for r in ranked]
        self.assertEqual(ids_out[0], "first")
        # a-tail must come before z-tail (lexicographic tiebreak)
        self.assertLess(ids_out.index("a-tail"), ids_out.index("z-tail"))


# ---------------------------------------------------------------------------
# 13. Finding #56 — adults validation
# ---------------------------------------------------------------------------

class TestAdultsValidation(unittest.TestCase):
    """adults <= 0 must raise; missing adults logs and clamps to 1."""

    def _make_message(self, payload: dict) -> dict:
        return {"parts": [{"kind": "data", "data": payload}]}

    def test_adults_zero_raises(self):
        """adults=0 must raise ValueError (not silently assume 1)."""
        import asyncio
        transport = _mock_search_catalog_transport([])
        agent = aa.AccommodationAgent(merchant_transport=transport)
        msg = self._make_message({
            "city": "bali", "checkin": "2026-10-01", "checkout": "2026-10-04",
            "adults": 0, "max_cents": 50000,
        })
        with self.assertRaises(ValueError):
            asyncio.run(agent._propose_handler(msg, {}))

    def test_adults_negative_raises(self):
        """adults=-1 must raise ValueError."""
        import asyncio
        transport = _mock_search_catalog_transport([])
        agent = aa.AccommodationAgent(merchant_transport=transport)
        msg = self._make_message({
            "city": "bali", "checkin": "2026-10-01", "checkout": "2026-10-04",
            "adults": -1, "max_cents": 50000,
        })
        with self.assertRaises(ValueError):
            asyncio.run(agent._propose_handler(msg, {}))

    def test_adults_missing_clamps_to_one(self):
        """Missing adults is clamped to 1 with a warning (not a silent pass)."""
        catalog = [
            {"hotel_id": "bali-ubud-X", "title": "X", "total_cents": 10000,
             "review_score": 8.0, "star_rating": 3.0, "amenities": []},
        ]
        import asyncio
        transport = _mock_search_catalog_transport(catalog)
        agent = aa.AccommodationAgent(merchant_transport=transport)
        msg = self._make_message({
            "city": "bali", "checkin": "2026-10-01", "checkout": "2026-10-04",
            # adults intentionally omitted
            "max_cents": 50000,
        })
        with patch("agents.accommodation_agent.DASHSCOPE_API_KEY", ""):
            artifact = asyncio.run(agent._propose_handler(msg, {}))
        # Must not raise; result should carry adults=1
        data = artifact["parts"][0]["data"]
        self.assertEqual(data["adults"], 1)


# ---------------------------------------------------------------------------
# 14. Finding #32 — alternates cap configurable via env var
# ---------------------------------------------------------------------------

class TestAlternatesCap(unittest.TestCase):
    """ACCOMMODATION_ALTERNATES_CAP env var controls the alternates limit."""

    def _catalog_5(self) -> list[dict]:
        return [
            {"hotel_id": f"bali-ubud-H{i}", "title": f"H{i}",
             "total_cents": 10000 + i * 1000, "review_score": float(9 - i),
             "star_rating": 4.0, "amenities": []}
            for i in range(5)
        ]

    def test_default_cap_is_two(self):
        """With default ACCOMMODATION_ALTERNATES_CAP=2, at most 2 alternates returned."""
        transport = _mock_search_catalog_transport(self._catalog_5())
        agent = aa.AccommodationAgent(merchant_transport=transport)
        with patch("agents.accommodation_agent.DASHSCOPE_API_KEY", ""), \
             patch("agents.accommodation_agent._ALTERNATES_CAP", 2):
            result = agent._call_catalog(
                city="bali", checkin="2026-10-01", checkout="2026-10-04",
                adults=1, max_cents=99999, vibe=None,
            )
        self.assertLessEqual(len(result.get("alternates", [])), 2)

    def test_cap_three_returns_up_to_three(self):
        """Setting _ALTERNATES_CAP=3 allows up to 3 alternates."""
        transport = _mock_search_catalog_transport(self._catalog_5())
        agent = aa.AccommodationAgent(merchant_transport=transport)
        with patch("agents.accommodation_agent.DASHSCOPE_API_KEY", ""), \
             patch("agents.accommodation_agent._ALTERNATES_CAP", 3):
            result = agent._call_catalog(
                city="bali", checkin="2026-10-01", checkout="2026-10-04",
                adults=1, max_cents=99999, vibe=None,
            )
        self.assertLessEqual(len(result.get("alternates", [])), 3)
        # With 5 candidates and cap=3, we should get exactly 3 alternates
        self.assertEqual(len(result.get("alternates", [])), 3)


# ---------------------------------------------------------------------------
# 15. Findings #33/#36 — _filter_by_area conservative no-op for unknown cities
# ---------------------------------------------------------------------------

class TestFilterByAreaConservativeNoOp(unittest.TestCase):
    """
    When target_areas is supplied but the city has no seeded area mapping,
    _filter_by_area must return the full unfiltered list + area_set_unknown=True
    rather than silently dropping all candidates (which would produce a false no_fit).
    """

    def _make_result(self, hotel_id: str, total_cents: int = 10000) -> dict:
        return {
            "hotel_id": hotel_id,
            "title": hotel_id,
            "total_cents": total_cents,
            "review_score": 7.0,
            "star_rating": 3.0,
            "amenities": [],
        }

    def test_known_city_bali_filters_correctly(self):
        """Bali area parsing works; known-area hotels are kept, others dropped."""
        results = [
            self._make_result("bali-seminyak-hotel-a"),
            self._make_result("bali-ubud-hotel-b"),
        ]
        kept, area_set_unknown = _filter_by_area(results, ["seminyak"], city="bali")
        self.assertFalse(area_set_unknown, "Known city should not set area_set_unknown")
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["hotel_id"], "bali-seminyak-hotel-a")

    def test_unknown_city_returns_all_with_flag(self):
        """For a city whose area system is not seeded, return all + area_set_unknown=True."""
        results = [
            self._make_result("paris-montmartre-hotel"),
            self._make_result("paris-marais-hotel"),
        ]
        # target_areas supplied, but parse_area_from_hotel_id returns None for Paris
        kept, area_set_unknown = _filter_by_area(results, ["marais"], city="paris")
        self.assertTrue(
            area_set_unknown,
            "Unknown city area system should set area_set_unknown=True",
        )
        self.assertEqual(len(kept), len(results), "All results preserved (no false no_fit)")

    def test_no_target_areas_no_op(self):
        """When target_areas is empty/None, filter is a no-op (area_set_unknown=False)."""
        results = [
            self._make_result("bali-ubud-hotel-a"),
            self._make_result("bali-seminyak-hotel-b"),
        ]
        for target in (None, [], []):
            kept, flag = _filter_by_area(results, target, city="bali")
            self.assertFalse(flag)
            self.assertEqual(len(kept), len(results))

    def test_end_to_end_unknown_city_does_not_produce_no_fit(self):
        """
        In _call_catalog for an unknown city with target_areas, the result must
        be fit=ok (not no_fit) when valid hotels exist, with area_set_unknown=True.
        """
        catalog = [
            {"hotel_id": "sydney-harbour-hotel", "title": "Sydney Harbour",
             "total_cents": 30000, "review_score": 8.5, "star_rating": 4.0, "amenities": []},
            {"hotel_id": "sydney-cbd-hotel", "title": "Sydney CBD",
             "total_cents": 25000, "review_score": 7.5, "star_rating": 3.0, "amenities": []},
        ]
        transport = _mock_search_catalog_transport(catalog)
        agent = aa.AccommodationAgent(merchant_transport=transport)

        with patch("agents.accommodation_agent.DASHSCOPE_API_KEY", ""):
            result = agent._call_catalog(
                city="sydney",
                checkin="2026-12-01",
                checkout="2026-12-05",
                adults=1,
                max_cents=50000,
                vibe=None,
                target_areas=["cbd"],  # target area supplied, but Sydney not seeded
            )

        # Must not be a false no_fit even though all hotel IDs parse to area=None
        self.assertEqual(
            result["fit"], "ok",
            "Unknown city with target_areas must not produce false no_fit",
        )
        self.assertTrue(
            result.get("area_set_unknown"),
            "area_set_unknown must be True for unseeded city",
        )


# ---------------------------------------------------------------------------
# 16. C3 — catalog per-row authoritative "area" is the SOURCE OF TRUTH
# ---------------------------------------------------------------------------

class TestCatalogAreaSourceOfTruth(unittest.TestCase):
    """
    C3: the catalog carries an authoritative per-row 'area'. _filter_by_area and
    the enrichment path must use it as source of truth (no id-parse fallback when
    the catalog area is present).
    """

    def test_catalog_area_used_for_unseeded_city(self):
        """
        Paris has no seeded id-parse area mapping. With an authoritative catalog
        'area' on each row, _filter_by_area must filter correctly (NOT fall into
        the area_set_unknown no-op).
        """
        results = [
            {"hotel_id": "paris-x-hotel", "area": "Marais",
             "total_cents": 10000, "review_score": 7.0},
            {"hotel_id": "paris-y-hotel", "area": "Montmartre",
             "total_cents": 10000, "review_score": 7.0},
        ]
        kept, area_set_unknown = _filter_by_area(results, ["marais"], city="paris")
        self.assertFalse(
            area_set_unknown,
            "Authoritative catalog area must resolve — not the unknown no-op",
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["hotel_id"], "paris-x-hotel")

    def test_catalog_area_overrides_id_parse(self):
        """
        Even when the id WOULD parse to a Bali area, an authoritative catalog
        'area' is the source of truth (id-parse is fallback only).
        """
        results = [
            # id token 'seminyak' would parse, but catalog says 'ubud'
            {"hotel_id": "bali-seminyak-mislabeled", "area": "ubud",
             "total_cents": 10000, "review_score": 7.0},
        ]
        kept, area_set_unknown = _filter_by_area(results, ["ubud"], city="bali")
        self.assertFalse(area_set_unknown)
        self.assertEqual(len(kept), 1, "catalog area 'ubud' must win over id token")

    def test_end_to_end_catalog_area_tag_on_proposal(self):
        """_call_catalog enrichment tags the proposal with the catalog area."""
        catalog = [
            {"hotel_id": "paris-z-hotel", "title": "Z", "area": "Latin Quarter",
             "total_cents": 20000, "review_score": 8.0, "star_rating": 4.0,
             "amenities": []},
        ]
        transport = _mock_search_catalog_transport(catalog)
        agent = aa.AccommodationAgent(merchant_transport=transport)
        with patch("agents.accommodation_agent.DASHSCOPE_API_KEY", ""):
            result = agent._call_catalog(
                city="paris", checkin="2026-12-01", checkout="2026-12-05",
                adults=1, max_cents=50000, vibe=None,
            )
        self.assertEqual(result["fit"], "ok")
        self.assertEqual(result["proposal"]["area"], "latin quarter")


# ---------------------------------------------------------------------------
# 17. MED — local max_occupancy re-validation (defense-in-depth)
# ---------------------------------------------------------------------------

class TestOccupancyRevalidation(unittest.TestCase):
    """
    MED: re-validate max_occupancy locally when available — a provably
    over-capacity row must never be selected (merchant would VOID at checkout).
    Absence is NOT a hard fail here (that is the Critic's conservative-flag job).
    """

    def test_occupancy_ok_known_over_capacity(self):
        self.assertFalse(_occupancy_ok({"hotel_id": "h", "max_occupancy": 2}, adults=3))

    def test_occupancy_ok_known_fits(self):
        self.assertTrue(_occupancy_ok({"hotel_id": "h", "max_occupancy": 4}, adults=3))

    def test_occupancy_ok_absent_not_blocked(self):
        # absence/<=0 -> not blocked here (Critic flags unprovable capacity)
        self.assertTrue(_occupancy_ok({"hotel_id": "h"}, adults=3))
        self.assertTrue(_occupancy_ok({"hotel_id": "h", "max_occupancy": 0}, adults=3))

    def test_pick_skips_over_capacity_winner(self):
        """
        Top-ranked candidate is over-capacity; pick must skip it and select the
        next budget-and-capacity-fitting candidate.
        """
        ranked = [
            {"hotel_id": "small", "total_cents": 10000, "max_occupancy": 1},
            {"hotel_id": "big", "total_cents": 12000, "max_occupancy": 4},
        ]
        winner = _pick_within_ceiling(ranked, max_cents=50000, adults=3)
        self.assertIsNotNone(winner)
        self.assertEqual(winner["hotel_id"], "big")

    def test_pick_no_fit_when_all_over_capacity(self):
        ranked = [
            {"hotel_id": "a", "total_cents": 10000, "max_occupancy": 1},
            {"hotel_id": "b", "total_cents": 12000, "max_occupancy": 2},
        ]
        self.assertIsNone(_pick_within_ceiling(ranked, max_cents=50000, adults=3))

    def test_end_to_end_over_capacity_excluded(self):
        """_call_catalog must not propose a provably over-capacity hotel."""
        catalog = [
            {"hotel_id": "bali-ubud-tiny", "title": "Tiny", "area": "ubud",
             "total_cents": 10000, "review_score": 9.5, "star_rating": 5.0,
             "amenities": [], "max_occupancy": 1},
            {"hotel_id": "bali-ubud-family", "title": "Family", "area": "ubud",
             "total_cents": 15000, "review_score": 8.0, "star_rating": 4.0,
             "amenities": [], "max_occupancy": 4},
        ]
        transport = _mock_search_catalog_transport(catalog)
        agent = aa.AccommodationAgent(merchant_transport=transport)
        with patch("agents.accommodation_agent.DASHSCOPE_API_KEY", ""):
            result = agent._call_catalog(
                city="bali", checkin="2026-12-01", checkout="2026-12-05",
                adults=3, max_cents=50000, vibe=None,
            )
        self.assertEqual(result["fit"], "ok")
        self.assertEqual(result["proposal"]["hotel_id"], "bali-ubud-family")
        alt_ids = [a["hotel_id"] for a in result["alternates"]]
        self.assertNotIn("bali-ubud-tiny", alt_ids)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
