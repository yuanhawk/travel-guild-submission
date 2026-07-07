"""test_accommodation_rank_cap_cov3.py — #76 LLM ranking-input cap.

A megacity metro now surfaces ~200-350 candidates (ward aggregation), which timed out the DashScope
ranking call and broke LLM-on negotiate. `_rank_candidates` now deterministically pre-ranks and only
LLM-ranks the top `_LLM_RANK_CAP`; the remainder keeps the deterministic order. These tests pin:
(1) LLM-off output is byte-identical to the full deterministic sort regardless of the cap (var-0),
(2) the LLM never receives more than the cap, (3) small sets are unchanged (all reach the LLM)."""

from __future__ import annotations

import os
import sys
import unittest

os.environ.pop("DASHSCOPE_API_KEY", None)  # force the deterministic / LLM-off path

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOC = os.path.dirname(_HERE)
if _SOC not in sys.path:
    sys.path.insert(0, _SOC)

from agents import accommodation_agent as A  # noqa: E402


def _cands(n):
    # varied review_scores so the deterministic sort order is non-trivial
    return [{"hotel_id": f"h{i:03d}", "review_score": (i * 37) % 100 / 10.0, "city": "tokyo"}
            for i in range(n)]


def _det_sort(cands):
    return [r["hotel_id"] for r in sorted(
        cands, key=lambda r: (-float(r.get("review_score", 0)), r.get("hotel_id", "")))]


class TestRankCap(unittest.TestCase):
    def test_llm_off_byte_identical_above_cap(self):
        c = _cands(A._LLM_RANK_CAP * 3)  # well above the cap
        ranked, src = A._rank_candidates(c, "culture", None)
        self.assertEqual([r["hotel_id"] for r in ranked], _det_sort(c))  # full deterministic sort
        self.assertEqual(len(ranked), len(c))   # whole set retained, nothing dropped
        self.assertEqual(src, "fallback")

    def test_llm_input_never_exceeds_cap(self):
        seen = {}
        orig = A._llm_rank_hotels
        A._llm_rank_hotels = lambda cands, vibe, hint: (seen.update(n=len(cands)), None)[1]
        try:
            A._rank_candidates(_cands(200), "culture", None)
        finally:
            A._llm_rank_hotels = orig
        self.assertLessEqual(seen["n"], A._LLM_RANK_CAP)  # LLM saw at most the cap

    def test_small_set_all_reach_llm(self):
        seen = {}
        orig = A._llm_rank_hotels
        A._llm_rank_hotels = lambda cands, vibe, hint: (seen.update(n=len(cands)), None)[1]
        try:
            A._rank_candidates(_cands(5), "culture", None)
        finally:
            A._llm_rank_hotels = orig
        self.assertEqual(seen["n"], 5)  # below cap → unchanged behavior (all candidates ranked)

    def test_exact_cap_boundary(self):
        # off-by-one guard on the `len > cap` branch: n == cap → no cap path; n == cap+1 → cap path
        cap = A._LLM_RANK_CAP
        for n in (cap, cap + 1):
            seen = {}
            orig = A._llm_rank_hotels
            A._llm_rank_hotels = lambda cands, vibe, hint: (seen.update(n=len(cands)), None)[1]
            try:
                ranked, _ = A._rank_candidates(_cands(n), "culture", None)
            finally:
                A._llm_rank_hotels = orig
            self.assertEqual([r["hotel_id"] for r in ranked], _det_sort(_cands(n)))  # var-0 at boundary
            self.assertLessEqual(seen["n"], cap)

    def test_no_duplicate_or_dropped_candidates(self):
        # a dup+drop pairing would preserve length but corrupt the set — assert the id-set is exact
        c = _cands(A._LLM_RANK_CAP * 4 + 1)
        ranked, _ = A._rank_candidates(c, "culture", None)
        ids = [r["hotel_id"] for r in ranked]
        self.assertEqual(len(ids), len(set(ids)))                      # no duplicates
        self.assertEqual(set(ids), {r["hotel_id"] for r in c})        # no drops/additions


if __name__ == "__main__":
    unittest.main(verbosity=2)
