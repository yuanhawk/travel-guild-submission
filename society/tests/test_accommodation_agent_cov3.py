"""
test_accommodation_agent_cov3.py — coverage round 3 for accommodation_agent.

Targets the residual uncovered lines/branches not already exercised by
tests/test_accommodation_ranking.py and tests/test_accommodation_lodging_type.py:

  * _llm_rank_hotels parse branches: a bare-LIST LLM response (no ranked_ids
    wrapper), and a non-list/non-dict JSON scalar response (→ None → fallback).
  * _search_catalog: the SIGNED-request branch (ucp_signing.signed_headers
    returns truthy), the UNSIGNED branch, the _meta_out city_available_count
    population, the non-list results coercion, and the merchant-error → [] path.
  * AccommodationAgent._extract_payload: dict data-part, JSON-string data-part,
    JSON text-part, bad text-part, and the no-payload → None path.
  * _propose_handler: payload-None → ValueError (719-722).
  * main(): entry point wired to a patched uvicorn.run (1027-1043).

All deterministic / var-0-safe: the module is imported and its functions called
directly with crafted inputs and mocked transports. NO live LLM / network /
merchant. A mock httpx.BaseTransport supplies all catalog rows.
"""

from __future__ import annotations

import asyncio
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
    AccommodationAgent,
    _llm_rank_hotels,
    _search_catalog,
)

KEY = "test-key-placeholder"


def _post_returning(content: str) -> MagicMock:
    """Mock for httpx.post so _llm_rank_hotels sees `content` as the message text."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    return resp


# ---------------------------------------------------------------------------
# 1. _llm_rank_hotels parse branches
# ---------------------------------------------------------------------------

class TestLlmRankParseBranches(unittest.TestCase):
    CANDS = [
        {"hotel_id": "h-a", "total_cents": 100, "review_score": 9.0},
        {"hotel_id": "h-b", "total_cents": 200, "review_score": 8.0},
    ]

    def test_bare_list_response_returned_as_ids(self):
        """LLM returns a bare JSON list (no ranked_ids wrapper) → str ids kept."""
        with patch.object(aa, "DASHSCOPE_API_KEY", KEY), \
             patch.object(aa.httpx, "post",
                          return_value=_post_returning(json.dumps(["h-b", "h-a", 7]))):
            ids = _llm_rank_hotels(self.CANDS, "beach", None)
        # The int 7 is filtered out (non-str); strings preserved in order.
        self.assertEqual(ids, ["h-b", "h-a"])

    def test_scalar_json_response_gives_none(self):
        """A JSON scalar (neither dict nor list) → ranked is None → None returned."""
        with patch.object(aa, "DASHSCOPE_API_KEY", KEY), \
             patch.object(aa.httpx, "post", return_value=_post_returning("42")):
            ids = _llm_rank_hotels(self.CANDS, None, None)
        self.assertIsNone(ids)

    def test_markdown_fenced_ranked_ids(self):
        """Markdown-fenced JSON object is unwrapped and ranked_ids extracted."""
        fenced = "```json\n" + json.dumps({"ranked_ids": ["h-a", "h-b"]}) + "\n```"
        with patch.object(aa, "DASHSCOPE_API_KEY", KEY), \
             patch.object(aa.httpx, "post", return_value=_post_returning(fenced)):
            ids = _llm_rank_hotels(self.CANDS, "calm", "near beach")
        self.assertEqual(ids, ["h-a", "h-b"])


# ---------------------------------------------------------------------------
# 2. _search_catalog — signed/unsigned/meta/error branches
# ---------------------------------------------------------------------------

def _catalog_transport(structured: dict[str, Any], status: int = 200):
    class _T(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"result": {"structuredContent": structured}})
    return _T()


class TestSearchCatalog(unittest.TestCase):
    def test_signed_branch_and_meta_populated(self):
        """signed_headers truthy → signed POST branch; _meta_out gets city count."""
        structured = {
            "results": [{"hotel_id": "x-1", "total_cents": 100}],
            "city_available_count": 5,
        }
        meta: dict[str, Any] = {}
        client = httpx.Client(transport=_catalog_transport(structured))
        with patch.object(aa.ucp_signing, "signed_headers",
                          return_value={"Signature": "sig-stub"}):
            results = _search_catalog(
                client, "bali", "2026-07-01", "2026-07-03", 2, 30000, _meta_out=meta
            )
        client.close()
        self.assertEqual(len(results), 1)
        self.assertEqual(meta["city_available_count"], 5)

    def test_unsigned_branch(self):
        """signed_headers empty → unsigned POST branch still returns rows."""
        structured = {"results": [{"hotel_id": "y-1", "total_cents": 200}]}
        client = httpx.Client(transport=_catalog_transport(structured))
        with patch.object(aa.ucp_signing, "signed_headers", return_value={}):
            results = _search_catalog(
                client, "paris", "2026-07-01", "2026-07-03", 1, 50000
            )
        client.close()
        self.assertEqual(results[0]["hotel_id"], "y-1")

    def test_non_list_results_coerced_to_empty(self):
        """A non-list 'results' field is coerced to []."""
        structured = {"results": {"not": "a list"}}
        client = httpx.Client(transport=_catalog_transport(structured))
        with patch.object(aa.ucp_signing, "signed_headers", return_value={}):
            results = _search_catalog(
                client, "rome", "2026-07-01", "2026-07-03", 1, 50000
            )
        client.close()
        self.assertEqual(results, [])

    def test_merchant_error_returns_empty(self):
        """A transport that raises → caught → empty list (never propagates)."""
        class _Boom(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("merchant unreachable")

        client = httpx.Client(transport=_Boom())
        with patch.object(aa.ucp_signing, "signed_headers", return_value={}):
            results = _search_catalog(
                client, "oslo", "2026-07-01", "2026-07-03", 1, 50000
            )
        client.close()
        self.assertEqual(results, [])


# ---------------------------------------------------------------------------
# 3. _extract_payload branches
# ---------------------------------------------------------------------------

class TestExtractPayload(unittest.TestCase):
    def test_dict_data_part(self):
        msg = {"parts": [{"kind": "data", "data": {"city": "bali"}}]}
        self.assertEqual(AccommodationAgent._extract_payload(msg), {"city": "bali"})

    def test_json_string_data_part(self):
        msg = {"parts": [{"kind": "data", "data": json.dumps({"city": "paris"})}]}
        self.assertEqual(AccommodationAgent._extract_payload(msg), {"city": "paris"})

    def test_bad_string_data_part_then_text_part(self):
        """A non-JSON data string is skipped; a later JSON text part is used."""
        msg = {
            "parts": [
                {"kind": "data", "data": "not-json{{"},
                {"kind": "text", "text": json.dumps({"city": "rome"})},
            ]
        }
        self.assertEqual(AccommodationAgent._extract_payload(msg), {"city": "rome"})

    def test_text_part_non_dict_json_ignored(self):
        """A text part whose JSON is a list (not dict) is ignored → None."""
        msg = {"parts": [{"kind": "text", "text": json.dumps([1, 2, 3])}]}
        self.assertIsNone(AccommodationAgent._extract_payload(msg))

    def test_no_usable_part_returns_none(self):
        msg = {"parts": [{"kind": "text", "text": "totally not json"}]}
        self.assertIsNone(AccommodationAgent._extract_payload(msg))

    def test_empty_parts_returns_none(self):
        self.assertIsNone(AccommodationAgent._extract_payload({"parts": []}))


# ---------------------------------------------------------------------------
# 4. _propose_handler: payload None → ValueError
# ---------------------------------------------------------------------------

class TestProposeHandlerNoPayload(unittest.TestCase):
    def test_missing_payload_raises(self):
        agent = AccommodationAgent()
        # No data/text part with a usable JSON payload → _extract_payload None.
        msg = {"parts": [{"kind": "text", "text": "not json at all"}]}
        with self.assertRaises(ValueError):
            asyncio.run(agent._propose_handler(msg, {}))


# ---------------------------------------------------------------------------
# 5. main() entry point — patched uvicorn so nothing actually serves.
# ---------------------------------------------------------------------------

class TestMainEntryPoint(unittest.TestCase):
    def test_main_builds_app_and_calls_uvicorn(self):
        run_mock = MagicMock()
        with patch.object(aa.uvicorn, "run", run_mock), \
             patch.dict(os.environ, {"PORT": "9999", "HOST": "127.0.0.1"}):
            aa.main()
        self.assertEqual(run_mock.call_count, 1)
        # uvicorn.run(app, host=..., port=...) — verify host/port plumbed through.
        _args, kwargs = run_mock.call_args
        self.assertEqual(kwargs.get("host"), "127.0.0.1")
        self.assertEqual(kwargs.get("port"), 9999)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# 6. Bangkok ward-hotel regression (#123): _filter_by_area must NOT drop
#    megacity ward hotels for single-area cities (Bangkok, Singapore, KL).
# ---------------------------------------------------------------------------

class TestBangkokWardAreaFilter(unittest.TestCase):
    """_filter_by_area single-area fast-path: ward hotels survive for Bangkok."""

    def _make_ward_row(self, hotel_id: str, catalog_area: str) -> dict:
        return {"hotel_id": hotel_id, "area": catalog_area, "city": "bangkok"}

    def test_bangkok_ward_hotels_not_dropped_by_area_filter(self):
        from agents.accommodation_agent import _filter_by_area

        # Ward hotels with catalog_area = ward name (not "bangkok").
        ward_hotels = [
            self._make_ward_row("bang-khae-arun-residence", "Bang Khae"),
            self._make_ward_row("chatuchak-39", "Chatuchak"),
            self._make_ward_row("watthana-grand-hotel", "Watthana"),
            self._make_ward_row("dusit-thani-bangkok", "Dusit"),
        ]
        # target_areas=["bangkok"] — exactly what destination_agent returns for Bangkok
        kept, area_set_unknown = _filter_by_area(ward_hotels, ["bangkok"], city="bangkok")
        self.assertEqual(len(kept), len(ward_hotels),
                         "Ward hotels must not be dropped for single-area city Bangkok")
        self.assertFalse(area_set_unknown, "area_set_unknown should be False (fast-path)")

    def test_singapore_hotels_not_dropped(self):
        from agents.accommodation_agent import _filter_by_area

        sg_hotels = [
            {"hotel_id": "bukit-merah-some-hotel", "area": "Bukit Merah", "city": "singapore"},
        ]
        kept, _ = _filter_by_area(sg_hotels, ["singapore"], city="singapore")
        self.assertEqual(len(kept), 1)

    def test_bali_area_filter_still_applies(self):
        """Bali has multi-area — area filter must NOT be skipped."""
        from agents.accommodation_agent import _filter_by_area

        bali_hotels = [
            {"hotel_id": "bali-ubud-some-villa", "area": "ubud", "city": "bali"},
            {"hotel_id": "bali-kuta-beach-resort", "area": "kuta", "city": "bali"},
        ]
        kept, _ = _filter_by_area(bali_hotels, ["ubud"], city="bali")
        # Only the ubud hotel should pass for a Bali "ubud" area filter
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["hotel_id"], "bali-ubud-some-villa")
