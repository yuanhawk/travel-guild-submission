"""
test_destination_agent.py — Unit tests for M-Agentic-2 Destination agent
(vibe→area edge).  CI-safe: all LLM (DashScope) calls are mocked.

CI-safe: no network. The LLM is intercepted with unittest.mock.patch of
``httpx.post`` plus a patched DASHSCOPE_API_KEY, mirroring test_intent_parser.

Run with:
    python test_destination_agent.py -v

Coverage (per task spec):
  1.  Closed-set enforcement: out-of-set area dropped (LLM returns a fake area).
  2.  beach vibe ≠ ubud (LLM unavailable → deterministic fallback excludes ubud).
  3.  culture vibe = ubud.
  4.  Garbage LLM output → deterministic fallback (no crash).
  5.  Area parsing from hotel ID correctness (all catalog ids).
  Plus:
  6.  LLM ranking accepted when valid + clamped (order preserved).
  7.  Single-area city short-circuit (no LLM call).
  8.  Empty/all-dropped LLM → fallback.
  9.  Variance clamp: divergent LLM orderings both stay within the closed set
      (selection variance bounded; never leaks an invalid area).
 10.  A2A skill handler returns a typed area_assessment artifact.
 11.  accommodation_agent._filter_by_area drops hotels outside target areas.
 12.  Catalog/closed-set parity: every catalog.go Bali area is in BALI_AREAS.
 13.  VIBE-STRICT INTERSECTION: assess() output ⊆ strict_map[vibe] for every
      known vibe — LLM returning out-of-vibe-set areas is filtered out.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agents import destination_agent as da
from agents.destination_agent import (
    BALI_AREAS,
    BALI_VIBE_FALLBACK,
    DestinationAgent,
    assess,
    parse_area_from_hotel_id,
)


# ---------------------------------------------------------------------------
# Mock LLM helpers (mirror test_intent_parser)
# ---------------------------------------------------------------------------

def _mock_llm_areas(areas: list) -> MagicMock:
    """Mock httpx.post so _llm_call returns {"areas": areas} as JSON content."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"areas": areas})}}]
    }
    return resp


def _mock_llm_text(text: str) -> MagicMock:
    """Mock httpx.post so _llm_call returns raw (possibly garbage) text."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"content": text}}]}
    return resp


KEY = "test-key-placeholder"


# ---------------------------------------------------------------------------
# Closed-set enforcement / variance clamp
# ---------------------------------------------------------------------------

class TestClosedSetClamp(unittest.TestCase):

    def test_out_of_set_area_dropped(self):
        """LLM returns a real area + a fabricated one → fabricated dropped."""
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", KEY), \
             patch("httpx.post", return_value=_mock_llm_areas(
                 ["seminyak", "atlantis", "legian", "mars-beach"])):
            res = assess("bali", "beach")
        self.assertEqual(res["source"], "llm")
        # Only real catalog areas survive, order preserved.
        self.assertEqual(res["areas"], ["seminyak", "legian"])
        for a in res["areas"]:
            self.assertIn(a, BALI_AREAS)

    def test_all_areas_subset_of_catalog(self):
        """Whatever the LLM returns, output is always a subset of the closed set."""
        for llm_out in (
            ["ubud", "fakeville"],
            ["NONSENSE"],
            ["seminyak", "seminyak", "kuta"],  # duplicates collapse
            ["Nusa Dua", "nusadua"],            # normalisation + dedup
        ):
            with patch("agents.destination_agent.DASHSCOPE_API_KEY", KEY), \
                 patch("httpx.post", return_value=_mock_llm_areas(llm_out)):
                res = assess("bali", "beach")
            for a in res["areas"]:
                self.assertIn(a, BALI_AREAS, f"leaked area from {llm_out}")
            # no duplicates
            self.assertEqual(len(res["areas"]), len(set(res["areas"])))

    def test_nusa_dua_normalisation(self):
        """'Nusa Dua' (spaced) normalises to the catalog token 'nusadua'."""
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", KEY), \
             patch("httpx.post", return_value=_mock_llm_areas(["Nusa Dua"])):
            res = assess("bali", "luxury")
        self.assertEqual(res["areas"], ["nusadua"])

    def test_llm_order_preserved_when_valid(self):
        """A valid LLM ranking is preserved verbatim after clamp."""
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", KEY), \
             patch("httpx.post", return_value=_mock_llm_areas(
                 ["legian", "kuta", "seminyak"])):
            res = assess("bali", "beach")
        self.assertEqual(res["areas"], ["legian", "kuta", "seminyak"])
        self.assertEqual(res["source"], "llm")


# ---------------------------------------------------------------------------
# Vibe-appropriateness (correctness invariant)
# ---------------------------------------------------------------------------

class TestVibeAppropriate(unittest.TestCase):

    def test_beach_vibe_not_ubud_fallback(self):
        """beach vibe (LLM down → fallback) must NEVER include ubud."""
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", ""):
            res = assess("bali", "beach")
        self.assertEqual(res["source"], "fallback")
        self.assertNotIn("ubud", res["areas"])
        self.assertIn("seminyak", res["areas"])

    def test_beach_vibe_not_ubud_even_if_llm_says_ubud(self):
        """If the LLM (wrongly) returns only ubud for beach, the vibe-strict
        intersection drops ubud (ubud is NOT in strict_set[beach]) → fallback.
        This confirms the new vibe-appropriateness guarantee: LLM cannot smuggle
        a non-beach area into a beach result even if it's a real catalog area."""
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", KEY), \
             patch("httpx.post", return_value=_mock_llm_areas(["ubud"])):
            res = assess("bali", "beach")
        # Vibe-strict intersection drops ubud (not in beach strict set) →
        # LLM result is empty → deterministic fallback fires.
        self.assertEqual(res["source"], "fallback")
        self.assertNotIn("ubud", res["areas"])
        # The strict beach set: seminyak, legian, kuta, sanur, nusadua, jimbaran
        for a in res["areas"]:
            self.assertIn(a, frozenset(BALI_VIBE_FALLBACK["beach"]))

    def test_culture_vibe_is_ubud(self):
        """culture vibe → ubud (deterministic fallback)."""
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", ""):
            res = assess("bali", "culture")
        self.assertEqual(res["areas"], ["ubud"])

    def test_surf_vibe_is_canggu(self):
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", ""):
            res = assess("bali", "surf")
        self.assertEqual(res["areas"], ["canggu"])

    def test_relax_vibe_fallback(self):
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", ""):
            res = assess("bali", "relax")
        self.assertEqual(res["areas"], ["ubud", "sanur"])

    def test_luxury_vibe_fallback(self):
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", ""):
            res = assess("bali", "luxury")
        self.assertEqual(res["areas"], ["nusadua", "jimbaran"])


# ---------------------------------------------------------------------------
# Garbage / failure → deterministic fallback
# ---------------------------------------------------------------------------

class TestFallback(unittest.TestCase):

    def test_garbage_llm_output_falls_back(self):
        """Non-JSON garbage → deterministic fallback, no crash."""
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", KEY), \
             patch("httpx.post", return_value=_mock_llm_text("!!! not json {{{")):
            res = assess("bali", "culture")
        self.assertEqual(res["source"], "fallback")
        self.assertEqual(res["areas"], ["ubud"])

    def test_empty_llm_areas_falls_back(self):
        """LLM returns {"areas": []} → fallback."""
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", KEY), \
             patch("httpx.post", return_value=_mock_llm_areas([])):
            res = assess("bali", "beach")
        self.assertEqual(res["source"], "fallback")
        self.assertIn("seminyak", res["areas"])

    def test_all_dropped_llm_areas_falls_back(self):
        """LLM returns only out-of-set areas → all dropped → fallback."""
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", KEY), \
             patch("httpx.post", return_value=_mock_llm_areas(["narnia", "gotham"])):
            res = assess("bali", "surf")
        self.assertEqual(res["source"], "fallback")
        self.assertEqual(res["areas"], ["canggu"])

    def test_llm_http_error_falls_back(self):
        """LLM HTTP exception → deterministic fallback (no crash)."""
        import httpx

        def _boom(*a, **k):
            raise httpx.ConnectError("network down")

        with patch("agents.destination_agent.DASHSCOPE_API_KEY", KEY), \
             patch("httpx.post", side_effect=_boom):
            res = assess("bali", "culture")
        # assess() catches ANY LLM exception (network/parse/runtime) and falls
        # back deterministically — no crash leaks to the caller.
        self.assertEqual(res["source"], "fallback")
        self.assertEqual(res["areas"], ["ubud"])

    def test_no_key_uses_fallback(self):
        """Missing key → deterministic fallback without ever calling httpx."""
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", ""), \
             patch("httpx.post") as mock_post:
            res = assess("bali", "beach")
        mock_post.assert_not_called()
        self.assertEqual(res["source"], "fallback")


# ---------------------------------------------------------------------------
# Single-area cities
# ---------------------------------------------------------------------------

class TestSingleAreaCities(unittest.TestCase):

    def test_singapore_single_area_no_llm(self):
        with patch("httpx.post") as mock_post:
            res = assess("singapore", "city")
        mock_post.assert_not_called()
        self.assertEqual(res["areas"], ["singapore"])
        self.assertEqual(res["source"], "city_single")

    def test_bangkok_single_area(self):
        res = assess("bangkok", "city")
        self.assertEqual(res["areas"], ["bangkok"])

    def test_kl_single_area(self):
        res = assess("kuala lumpur", None)
        self.assertEqual(res["areas"], ["kuala lumpur"])

    def test_unknown_city_empty(self):
        res = assess("atlantis", "beach")
        self.assertEqual(res["areas"], [])


# ---------------------------------------------------------------------------
# Area parsing from hotel IDs (catalog.go correctness)
# ---------------------------------------------------------------------------

class TestAreaParsing(unittest.TestCase):

    def test_bali_area_parsing(self):
        cases = {
            "bali-alaya-ubud": "ubud",
            "bali-alila-seminyak": "seminyak",
            "bali-como-canggu": "canggu",
            "bali-ubud-garden": "ubud",
            "bali-kuta-paradiso": "kuta",
            "bali-kuta-beachside": "kuta",
            "bali-legian-beach": "legian",
            "bali-legian-surf": "legian",
            "bali-sanur-mainsail": "sanur",
            "bali-sanur-puri": "sanur",
            "bali-nusadua-mulia": "nusadua",
            "bali-nusadua-mercure": "nusadua",
            "bali-jimbaran-fourseasons": "jimbaran",
            "bali-jimbaran-seaview": "jimbaran",
            "bali-seminyak-oberoibali": "seminyak",
        }
        for hid, expected in cases.items():
            self.assertEqual(
                parse_area_from_hotel_id(hid), expected,
                f"{hid} should parse to {expected}",
            )

    def test_single_area_city_id_parsing(self):
        self.assertEqual(parse_area_from_hotel_id("bangkok-metro"), "bangkok")
        self.assertEqual(parse_area_from_hotel_id("singapore-marina-bay"), "singapore")
        self.assertEqual(parse_area_from_hotel_id("kl-bukit-bintang"), "kuala lumpur")

    def test_unparseable_id_returns_none(self):
        self.assertIsNone(parse_area_from_hotel_id(""))
        self.assertIsNone(parse_area_from_hotel_id("randomstring"))
        self.assertIsNone(parse_area_from_hotel_id(None))


# ---------------------------------------------------------------------------
# Catalog ↔ closed-set parity (anti-drift)
# ---------------------------------------------------------------------------

class TestCatalogParity(unittest.TestCase):

    def test_every_catalog_bali_area_in_closed_set(self):
        """Parse every Bali hotel id from catalog.go and assert its area is in
        BALI_AREAS — stops the closed set drifting from the real catalog."""
        # Catalog is now the shared data-driven JSON (Go //go:embed + Python load).
        catalog_json = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "ucp-merchant", "catalog.json",
        )
        if not os.path.exists(catalog_json):
            self.skipTest("catalog.json not found")
        import json as _json
        with open(catalog_json, "r", encoding="utf-8") as f:
            rows = _json.load(f)
        bali_ids = [r["id"] for r in rows if r["id"].startswith("bali-")]
        self.assertGreater(len(bali_ids), 5, "expected several Bali ids")
        for hid in bali_ids:
            area = parse_area_from_hotel_id(hid)
            self.assertIsNotNone(area, f"could not parse area from {hid}")
            self.assertIn(area, BALI_AREAS, f"{hid} → {area} not in closed set")


# ---------------------------------------------------------------------------
# A2A skill handler
# ---------------------------------------------------------------------------

class TestA2ASkill(unittest.TestCase):

    def _send(self, agent: DestinationAgent, payload: dict) -> dict:
        from starlette.testclient import TestClient
        client = TestClient(agent.build_app())
        msg = {
            "kind": "message",
            "messageId": "m1",
            "role": "user",
            "parts": [{"kind": "data", "data": payload}],
            "metadata": {"skillId": "destination.assess"},
        }
        rpc = {"jsonrpc": "2.0", "id": "1", "method": "message/send",
               "params": {"message": msg}}
        resp = client.post("/", json=rpc)
        task = resp.json()["result"]
        self.assertEqual(task["status"]["state"], "completed")
        for art in task["artifacts"]:
            for part in art["parts"]:
                if part["kind"] == "data":
                    return part["data"]
        raise AssertionError("no data artifact")

    def test_skill_returns_typed_assessment(self):
        agent = DestinationAgent()
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", ""):
            data = self._send(agent, {"city": "bali", "vibe": "culture"})
        self.assertEqual(data["type"], "area_assessment")
        self.assertEqual(data["city"], "bali")
        self.assertEqual(data["areas"], ["ubud"])
        self.assertEqual(data["provenance"], "catalog")

    def test_skill_missing_city_fails(self):
        agent = DestinationAgent()
        from starlette.testclient import TestClient
        client = TestClient(agent.build_app())
        msg = {
            "kind": "message", "messageId": "m1", "role": "user",
            "parts": [{"kind": "data", "data": {"vibe": "beach"}}],
            "metadata": {"skillId": "destination.assess"},
        }
        rpc = {"jsonrpc": "2.0", "id": "1", "method": "message/send",
               "params": {"message": msg}}
        task = client.post("/", json=rpc).json()["result"]
        self.assertEqual(task["status"]["state"], "failed")

    def test_card_advertises_skill(self):
        agent = DestinationAgent()
        card = agent._build_card()
        skill_ids = [s["id"] for s in card["skills"]]
        self.assertIn("destination.assess", skill_ids)


# ---------------------------------------------------------------------------
# Accommodation area filter integration
# ---------------------------------------------------------------------------

class TestAccommodationAreaFilter(unittest.TestCase):

    def test_filter_keeps_only_target_areas(self):
        from agents import accommodation_agent as aa
        rows = [
            {"hotel_id": "bali-seminyak-oberoibali", "total_cents": 100},
            {"hotel_id": "bali-alaya-ubud", "total_cents": 200},
            {"hotel_id": "bali-nusadua-mulia", "total_cents": 300},
        ]
        result = aa._filter_by_area(rows, ["seminyak", "legian"], "bali")
        # _filter_by_area returns (list, bool) — unpack.
        kept = result[0] if isinstance(result, tuple) else result
        ids = [r["hotel_id"] for r in kept]
        self.assertEqual(ids, ["bali-seminyak-oberoibali"])

    def test_filter_noop_when_no_targets(self):
        from agents import accommodation_agent as aa
        rows = [{"hotel_id": "bali-alaya-ubud"}]
        result_none = aa._filter_by_area(rows, None, "bali")
        result_empty = aa._filter_by_area(rows, [], "bali")
        kept_none = result_none[0] if isinstance(result_none, tuple) else result_none
        kept_empty = result_empty[0] if isinstance(result_empty, tuple) else result_empty
        self.assertEqual(kept_none, rows)
        self.assertEqual(kept_empty, rows)

    def test_filter_drops_unparseable_when_targets_active(self):
        from agents import accommodation_agent as aa
        rows = [
            {"hotel_id": "bali-seminyak-oberoibali"},
            {"hotel_id": "garbage-id-noarea"},
        ]
        result = aa._filter_by_area(rows, ["seminyak"], "bali")
        kept = result[0] if isinstance(result, tuple) else result
        self.assertEqual([r["hotel_id"] for r in kept], ["bali-seminyak-oberoibali"])


# ---------------------------------------------------------------------------
# VIBE-STRICT INTERSECTION (§tighten-vibe-area) — primary new tests
# ---------------------------------------------------------------------------

class TestVibeStrictIntersection(unittest.TestCase):
    """
    Assert that assess() output ⊆ BALI_VIBE_FALLBACK[vibe] for every known
    vibe, even when the LLM returns loosely-related or out-of-vibe areas.

    Invariant: returned areas ⊆ strict_map[vibe].  The LLM may only reorder
    within the strict set; it can never expand it.
    """

    def _assert_subset_of_strict(self, vibe: str, llm_areas: list, msg: str = "") -> dict:
        """Helper: mock LLM with llm_areas, run assess, assert ⊆ strict set."""
        strict = frozenset(BALI_VIBE_FALLBACK[vibe])
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", KEY), \
             patch("httpx.post", return_value=_mock_llm_areas(llm_areas)):
            res = assess("bali", vibe)
        for a in res["areas"]:
            self.assertIn(
                a, strict,
                f"area {a!r} not in strict_set[{vibe!r}]={sorted(strict)} {msg}"
            )
        self.assertGreater(len(res["areas"]), 0, f"areas must be non-empty for {vibe!r}")
        return res

    # --- culture: strict set = {ubud} only ---

    def test_culture_llm_returns_canggu_filtered(self):
        """culture + LLM returns canggu (surf town) → vibe-strict drops it → ubud."""
        res = self._assert_subset_of_strict(
            "culture", ["canggu", "ubud"], "canggu must not appear in culture result"
        )
        self.assertNotIn("canggu", res["areas"])
        self.assertIn("ubud", res["areas"])

    def test_culture_llm_returns_jimbaran_filtered(self):
        """culture + LLM returns jimbaran (luxury beach) → vibe-strict drops it."""
        res = self._assert_subset_of_strict(
            "culture", ["jimbaran", "ubud"]
        )
        self.assertNotIn("jimbaran", res["areas"])

    def test_culture_llm_returns_seminyak_filtered(self):
        """culture + LLM returns seminyak → vibe-strict drops it (not cultural)."""
        res = self._assert_subset_of_strict(
            "culture", ["seminyak", "ubud"]
        )
        self.assertNotIn("seminyak", res["areas"])
        self.assertIn("ubud", res["areas"])

    def test_culture_llm_all_wrong_falls_back_to_ubud(self):
        """culture + LLM returns only non-cultural areas → fallback → ubud only."""
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", KEY), \
             patch("httpx.post", return_value=_mock_llm_areas(
                 ["canggu", "seminyak", "jimbaran"])):
            res = assess("bali", "culture")
        self.assertEqual(res["source"], "fallback")
        self.assertEqual(res["areas"], ["ubud"])

    def test_culture_loose_set_canggu_seminyak_jimbaran_all_filtered(self):
        """culture + LLM returns {ubud, sanur, jimbaran, canggu} (old loose behavior)
        → intersection keeps only ubud."""
        res = self._assert_subset_of_strict(
            "culture", ["ubud", "sanur", "jimbaran", "canggu"]
        )
        self.assertEqual(res["areas"], ["ubud"])

    # --- relax: strict set = {ubud, sanur} ---

    def test_relax_llm_returns_nearly_all_filtered(self):
        """relax + LLM returns all 8 Bali areas → intersection keeps only ubud/sanur."""
        res = self._assert_subset_of_strict(
            "relax",
            ["seminyak", "ubud", "kuta", "legian", "sanur", "nusadua", "jimbaran", "canggu"]
        )
        self.assertEqual(frozenset(res["areas"]), frozenset({"ubud", "sanur"}))

    def test_relax_llm_returns_canggu_filtered(self):
        """relax + LLM returns canggu → vibe-strict drops it."""
        res = self._assert_subset_of_strict("relax", ["ubud", "sanur", "canggu"])
        self.assertNotIn("canggu", res["areas"])

    def test_relax_llm_order_preserved_within_strict(self):
        """relax + LLM returns [sanur, ubud] → order preserved (LLM reordering works)."""
        res = self._assert_subset_of_strict("relax", ["sanur", "ubud"])
        self.assertEqual(res["areas"], ["sanur", "ubud"])
        self.assertEqual(res["source"], "llm")

    # --- beach: strict set = {seminyak, legian, kuta, sanur, nusadua, jimbaran} ---

    def test_beach_llm_includes_ubud_filtered(self):
        """beach + LLM returns ubud → vibe-strict drops it (ubud ∉ beach set)."""
        res = self._assert_subset_of_strict(
            "beach", ["seminyak", "ubud", "legian"]
        )
        self.assertNotIn("ubud", res["areas"])
        self.assertIn("seminyak", res["areas"])

    def test_beach_llm_includes_canggu_filtered(self):
        """beach + LLM returns canggu → canggu is NOT in the strict beach set (surf town)."""
        res = self._assert_subset_of_strict(
            "beach", ["seminyak", "canggu", "kuta"]
        )
        self.assertNotIn("canggu", res["areas"])

    def test_beach_llm_order_preserved(self):
        """beach + LLM returns valid beach areas in custom order → order preserved."""
        res = self._assert_subset_of_strict(
            "beach", ["kuta", "legian", "seminyak"]
        )
        self.assertEqual(res["areas"], ["kuta", "legian", "seminyak"])
        self.assertEqual(res["source"], "llm")

    # --- surf: strict set = {canggu} ---

    def test_surf_llm_returns_seminyak_filtered(self):
        """surf + LLM returns seminyak → vibe-strict drops it → falls back to canggu."""
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", KEY), \
             patch("httpx.post", return_value=_mock_llm_areas(["seminyak"])):
            res = assess("bali", "surf")
        self.assertEqual(res["source"], "fallback")
        self.assertEqual(res["areas"], ["canggu"])

    def test_surf_llm_returns_canggu_accepted(self):
        """surf + LLM returns canggu → accepted (it's the only strict surf area)."""
        res = self._assert_subset_of_strict("surf", ["canggu"])
        self.assertEqual(res["areas"], ["canggu"])
        self.assertEqual(res["source"], "llm")

    # --- luxury: strict set = {nusadua, jimbaran} ---

    def test_luxury_llm_returns_seminyak_filtered(self):
        """luxury + LLM returns seminyak (not luxury-primary) → filtered out."""
        res = self._assert_subset_of_strict("luxury", ["nusadua", "seminyak", "jimbaran"])
        self.assertNotIn("seminyak", res["areas"])
        self.assertIn("nusadua", res["areas"])
        self.assertIn("jimbaran", res["areas"])

    def test_luxury_llm_returns_canggu_filtered(self):
        """luxury + LLM returns canggu → vibe-strict drops it."""
        res = self._assert_subset_of_strict("luxury", ["jimbaran", "canggu", "nusadua"])
        self.assertNotIn("canggu", res["areas"])

    # --- cross-vibe: all vibes, systematic ---

    def test_all_vibes_subset_invariant(self):
        """For EVERY known vibe, LLM returning all 8 areas → output ⊆ strict set."""
        all_8 = list(BALI_AREAS)
        for vibe, strict_list in BALI_VIBE_FALLBACK.items():
            strict = frozenset(strict_list)
            with patch("agents.destination_agent.DASHSCOPE_API_KEY", KEY), \
                 patch("httpx.post", return_value=_mock_llm_areas(all_8)):
                res = assess("bali", vibe)
            for a in res["areas"]:
                self.assertIn(
                    a, strict,
                    f"vibe={vibe!r}: area {a!r} leaked outside strict set {sorted(strict)}"
                )
            # Result must be non-empty (either LLM-filtered or fallback)
            self.assertGreater(len(res["areas"]), 0, f"empty result for vibe={vibe!r}")

    def test_vibe_strict_set_never_includes_canggu_in_culture(self):
        """Explicit guard: culture strict set must never contain canggu, jimbaran, seminyak."""
        strict_culture = frozenset(BALI_VIBE_FALLBACK["culture"])
        self.assertNotIn("canggu", strict_culture)
        self.assertNotIn("jimbaran", strict_culture)
        self.assertNotIn("seminyak", strict_culture)
        self.assertIn("ubud", strict_culture)


# ---------------------------------------------------------------------------
# D6 / #19 — parse_area_from_hotel_id var-0 determinism
# ---------------------------------------------------------------------------

class TestParseAreaDeterminism(unittest.TestCase):
    """
    D6: parse_area_from_hotel_id must return the SAME value on every process
    restart regardless of PYTHONHASHSEED.  Iteration must be by segment POSITION
    in the id, not by frozenset iteration order.
    """

    def test_bali_id_with_compound_area_tokens_is_deterministic(self):
        """A bali id that has two area tokens must deterministically pick the
        FIRST matching segment (by position in the id), not a hash-seed-dependent
        element from the frozenset."""
        # "bali-kuta-legian-beach" has both 'kuta' (pos 1) and 'legian' (pos 2)
        # → should always return 'kuta' (first match by position).
        result = parse_area_from_hotel_id("bali-kuta-legian-beach")
        self.assertEqual(result, "kuta", "first matching segment must be picked")

    def test_bali_id_alias_ubud_last_segment(self):
        """bali-alaya-ubud: 'alaya' is not a BALI_AREAS token, 'ubud' is → ubud."""
        self.assertEqual(parse_area_from_hotel_id("bali-alaya-ubud"), "ubud")

    def test_non_bali_id_with_kuta_segment_returns_none(self):
        """#34 fix: lhokseumawe-wisma-kuta-karang-baru must NOT return 'kuta'
        because the id does not start with 'bali-'."""
        result = parse_area_from_hotel_id("lhokseumawe-wisma-kuta-karang-baru")
        self.assertIsNone(
            result,
            "Bali area tokens must NOT be attributed to non-Bali hotel ids (#34)",
        )

    def test_non_bali_id_with_ubud_segment_returns_none(self):
        """Another non-Bali id with 'ubud' as a segment must not be misattributed."""
        result = parse_area_from_hotel_id("bandung-ubud-guest-house")
        self.assertIsNone(result)

    def test_catalog_area_field_takes_precedence_over_id_parsing(self):
        """#20 fix: when catalog_area kwarg is provided, it is returned as-is
        (normalised to lowercase) regardless of what id parsing would yield."""
        # Even a bali id: if catalog says "Ubud", return "ubud" (from field).
        self.assertEqual(
            parse_area_from_hotel_id("bali-seminyak-oberoibali", catalog_area="Ubud"),
            "ubud",
        )
        # Completely opaque id with explicit area field.
        self.assertEqual(
            parse_area_from_hotel_id("a-coruna-some-hotel", catalog_area="A Coruña"),
            "a coruña",
        )

    def test_bali_catalog_area_nusa_dua_normalised_to_nusadua(self):
        """C3 fix: Bali catalog rows with area='Nusa Dua' must normalise to the
        closed-set token 'nusadua' (space collapsed) so they match target_areas
        from destination.assess (which returns 'nusadua' from BALI_VIBE_FALLBACK).
        Without this fix, _filter_by_area would drop all Nusa Dua hotels when
        target_areas=['nusadua'] — silently producing a false no_fit."""
        # Catalog area with space → closed-set token (space collapsed).
        self.assertEqual(
            parse_area_from_hotel_id("bali-nusadua-mercure", catalog_area="Nusa Dua"),
            "nusadua",
        )
        # Mixed case with underscore → also normalised.
        self.assertEqual(
            parse_area_from_hotel_id("bali-nusadua-mulia", catalog_area="nusa_dua"),
            "nusadua",
        )

    def test_non_bali_catalog_area_space_preserved(self):
        """C3 fix: for non-Bali ids, spaces in the catalog area name are preserved
        (e.g. 'A Coruña' → 'a coruña', not 'acoruña').  Space-collapse applies
        ONLY to Bali ids where BALI_AREAS uses space-free tokens."""
        # Non-Bali: space preserved.
        self.assertEqual(
            parse_area_from_hotel_id("a-coruna-some-hotel", catalog_area="A Coruña"),
            "a coruña",
        )
        # WA town: no space in town name, should normalise cleanly too.
        self.assertEqual(
            parse_area_from_hotel_id("margaret-river-some-hotel", catalog_area="Margaret River"),
            "margaret river",
        )

    def test_catalog_area_empty_falls_through_to_id_parse(self):
        """catalog_area='' or None → fall through to id-based parsing."""
        self.assertEqual(
            parse_area_from_hotel_id("bali-sanur-puri", catalog_area=""),
            "sanur",
        )
        self.assertEqual(
            parse_area_from_hotel_id("bali-sanur-puri", catalog_area=None),
            "sanur",
        )

    def test_bali_area_order_is_always_position_not_hash(self):
        """Simulate multiple 'runs' in the same process with different frozenset
        iteration — the result must be the SAME (first area segment by position).

        This is deterministic because we iterate segments (not BALI_AREAS frozenset)
        and return the first matching one.
        """
        cases = [
            ("bali-ubud-garden", "ubud"),
            ("bali-seminyak-hotel", "seminyak"),
            ("bali-jimbaran-fourseasons", "jimbaran"),
            ("bali-canggu-surf", "canggu"),
            ("bali-nusadua-mulia", "nusadua"),
            ("bali-legian-beach", "legian"),
        ]
        for hid, expected in cases:
            for _ in range(3):  # call multiple times to confirm stability
                self.assertEqual(parse_area_from_hotel_id(hid), expected, hid)


# ---------------------------------------------------------------------------
# #34 — BALI area scoping
# ---------------------------------------------------------------------------

class TestBaliAreaScoping(unittest.TestCase):
    """BALI_AREAS segment match must be scoped to bali- ids only."""

    def test_all_bali_area_tokens_as_non_bali_ids_return_none(self):
        """For each BALI_AREAS token, a non-bali id that contains it must
        return None, not that token."""
        for area in sorted(BALI_AREAS):
            hid = f"someothercity-{area}-hotel"
            result = parse_area_from_hotel_id(hid)
            self.assertIsNone(
                result,
                f"Bali area token '{area}' must not be attributed to '{hid}' (#34)",
            )

    def test_real_lhokseumawe_kuta_collision_fixed(self):
        """Regression test for the known kuta collision in the real catalog."""
        self.assertIsNone(parse_area_from_hotel_id("lhokseumawe-wisma-kuta-karang-baru"))


# ---------------------------------------------------------------------------
# #57 — Seasonal advisory keyed by (city, country)
# ---------------------------------------------------------------------------

class TestSeasonalAdvisoryCountryKey(unittest.TestCase):
    """seasonal_advisory() must require a country to avoid wrong-location hazards."""

    def test_perth_australia_fires_advisory(self):
        """perth + australia + Dec → fires advisory."""
        from agents.destination_agent import seasonal_advisory
        adv = seasonal_advisory("perth", "2026-12-10", "2026-12-20", country="Australia")
        self.assertIsNotNone(adv)
        self.assertEqual(adv["flag"], "bushfire_season")

    def test_perth_no_country_no_advisory(self):
        """perth without country → None (cannot safely attribute WA advisory)."""
        from agents.destination_agent import seasonal_advisory
        adv = seasonal_advisory("perth", "2026-12-10", "2026-12-20", country=None)
        self.assertIsNone(adv, "No advisory without country to disambiguate")

    def test_perth_scotland_no_advisory(self):
        """perth + scotland → None (no seeded WA advisory for Scotland)."""
        from agents.destination_agent import seasonal_advisory
        adv = seasonal_advisory("perth", "2026-12-10", "2026-12-20", country="Scotland")
        self.assertIsNone(adv)

    def test_albany_australia_fires_advisory(self):
        """albany + australia + Jan → fires advisory (Jan is in the Dec–Feb window)."""
        from agents.destination_agent import seasonal_advisory
        adv = seasonal_advisory("albany", "2027-01-05", "2027-01-15", country="australia")
        self.assertIsNotNone(adv)
        self.assertEqual(adv["severity"], "high")

    def test_pemberton_no_country_no_advisory(self):
        """pemberton (also in Canada) without country → None."""
        from agents.destination_agent import seasonal_advisory
        adv = seasonal_advisory("pemberton", "2026-12-10", "2026-12-20", country=None)
        self.assertIsNone(adv)

    def test_assess_without_country_derives_from_defaults_for_wa_town(self):
        """assess() without country kwarg: for a WA catalog town ('perth'),
        _CITY_COUNTRY_DEFAULTS supplies 'australia' automatically so the advisory
        still fires (MED fix — seasonal advisory was dead in the live A2A path
        because the orchestrator's _call_destination did not pass country)."""
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", ""):
            res = assess("perth", None, checkin="2026-12-10", checkout="2026-12-20")
        # Advisory fires for Dec because _CITY_COUNTRY_DEFAULTS derives 'australia'.
        self.assertIsNotNone(res["advisory"])
        self.assertEqual(res["advisory"]["flag"], "bushfire_season")

    def test_assess_without_country_no_advisory_for_non_seeded_city(self):
        """assess() without country for a city NOT in _CITY_COUNTRY_DEFAULTS
        (e.g. 'bali') → advisory remains None (no false-positive)."""
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", ""):
            res = assess("bali", "beach", checkin="2026-12-10", checkout="2026-12-20")
        self.assertIsNone(res["advisory"], "Bali has no seeded advisory; must stay None")

    def test_assess_with_australia_country_fires_advisory(self):
        """assess() with country='Australia' → advisory fires for Dec."""
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", ""):
            res = assess(
                "perth", None,
                checkin="2026-12-10", checkout="2026-12-20",
                country="Australia",
            )
        self.assertIsNotNone(res["advisory"])
        self.assertEqual(res["advisory"]["flag"], "bushfire_season")

    def test_advisory_in_assess_result_dict(self):
        """The advisory key is always present in assess() output."""
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", ""):
            res = assess("bali", "beach")
        self.assertIn("advisory", res)


# ---------------------------------------------------------------------------
# #53 — Type guard for non-string city/vibe in handler
# ---------------------------------------------------------------------------

class TestHandlerTypeGuards(unittest.TestCase):
    """_assess_handler must raise ValueError for non-string city/vibe."""

    def _send_raw(self, agent: DestinationAgent, payload: dict) -> dict:
        """Send a raw payload and return the full task (not just data)."""
        from starlette.testclient import TestClient
        client = TestClient(agent.build_app())
        msg = {
            "kind": "message",
            "messageId": "m1",
            "role": "user",
            "parts": [{"kind": "data", "data": payload}],
            "metadata": {"skillId": "destination.assess"},
        }
        rpc = {"jsonrpc": "2.0", "id": "1", "method": "message/send",
               "params": {"message": msg}}
        return client.post("/", json=rpc).json()["result"]

    def test_integer_city_fails(self):
        """city=123 (integer) must produce a failed task, not a crash."""
        agent = DestinationAgent()
        task = self._send_raw(agent, {"city": 123, "vibe": "beach"})
        self.assertEqual(task["status"]["state"], "failed",
                         "non-string city must fail the task")

    def test_dict_city_fails(self):
        """city={} (dict) must produce a failed task."""
        agent = DestinationAgent()
        task = self._send_raw(agent, {"city": {}, "vibe": "beach"})
        self.assertEqual(task["status"]["state"], "failed")

    def test_integer_vibe_falls_back_gracefully(self):
        """vibe=42 (integer) must be treated as None (graceful fallback, no crash)."""
        agent = DestinationAgent()
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", ""):
            task = self._send_raw(agent, {"city": "bali", "vibe": 42})
        # Should succeed (vibe coerced to None → deterministic fallback)
        self.assertEqual(task["status"]["state"], "completed")

    def test_missing_city_fails(self):
        """city absent → failed task (already covered, kept for regression)."""
        agent = DestinationAgent()
        task = self._send_raw(agent, {"vibe": "beach"})
        self.assertEqual(task["status"]["state"], "failed")


# ---------------------------------------------------------------------------
# D9 — AREA_CLOSED_SETS / SINGLE_AREA_CITIES remain importable with same shape
# ---------------------------------------------------------------------------

class TestD9ContractImportability(unittest.TestCase):
    """D9: AREA_CLOSED_SETS and SINGLE_AREA_CITIES must be importable with the
    same shape (intent_parser imports them for the clarification gate)."""

    def test_area_closed_sets_importable(self):
        from agents.destination_agent import AREA_CLOSED_SETS
        self.assertIsInstance(AREA_CLOSED_SETS, dict)
        # Every value must be a frozenset of strings.
        for city, closed in AREA_CLOSED_SETS.items():
            self.assertIsInstance(closed, frozenset, f"AREA_CLOSED_SETS[{city!r}]")
            for s in closed:
                self.assertIsInstance(s, str)

    def test_single_area_cities_importable(self):
        from agents.destination_agent import SINGLE_AREA_CITIES
        self.assertIsInstance(SINGLE_AREA_CITIES, dict)
        for city, area in SINGLE_AREA_CITIES.items():
            self.assertIsInstance(city, str)
            self.assertIsInstance(area, str)

    def test_bali_areas_importable_as_frozenset(self):
        """BALI_AREAS must remain a frozenset (D9 — importers depend on the shape)."""
        from agents.destination_agent import BALI_AREAS
        self.assertIsInstance(BALI_AREAS, frozenset)
        # BALI_AREAS_ORDERED is a new sorted tuple for internal iteration.
        from agents.destination_agent import BALI_AREAS_ORDERED
        self.assertIsInstance(BALI_AREAS_ORDERED, tuple)
        self.assertEqual(frozenset(BALI_AREAS_ORDERED), BALI_AREAS)
        # BALI_AREAS_ORDERED must be sorted (deterministic).
        self.assertEqual(list(BALI_AREAS_ORDERED), sorted(BALI_AREAS))


# ---------------------------------------------------------------------------
# MED fix: _CITY_COUNTRY_DEFAULTS — seasonal advisory fires in live A2A path
# ---------------------------------------------------------------------------

class TestCityCountryDefaults(unittest.TestCase):
    """_CITY_COUNTRY_DEFAULTS is built from SEASONAL_ADVISORIES keys and allows
    assess() to derive country when the caller (e.g. orchestrator) does not pass
    one. Tests that the map is correct and advisory fires appropriately."""

    def test_city_country_defaults_importable(self):
        """_CITY_COUNTRY_DEFAULTS is a dict[str, str] at module level."""
        from agents.destination_agent import _CITY_COUNTRY_DEFAULTS, WA_TOWNS
        self.assertIsInstance(_CITY_COUNTRY_DEFAULTS, dict)
        for k, v in _CITY_COUNTRY_DEFAULTS.items():
            self.assertIsInstance(k, str)
            self.assertIsInstance(v, str)
        # Every WA town must be in the map with 'australia'
        for town in WA_TOWNS:
            self.assertIn(town, _CITY_COUNTRY_DEFAULTS,
                          f"WA town {town!r} missing from _CITY_COUNTRY_DEFAULTS")
            self.assertEqual(_CITY_COUNTRY_DEFAULTS[town], "australia",
                             f"WA town {town!r} should map to 'australia'")

    def test_bali_not_in_city_country_defaults(self):
        """'bali' must NOT appear in _CITY_COUNTRY_DEFAULTS (no seeded advisory)."""
        from agents.destination_agent import _CITY_COUNTRY_DEFAULTS
        self.assertNotIn("bali", _CITY_COUNTRY_DEFAULTS)

    def test_assess_perth_no_country_advisory_fires_dec(self):
        """assess('perth', ..., checkin=Dec, no country) → advisory fires.
        The orchestrator's _call_destination does not pass country — the MED
        fix ensures _CITY_COUNTRY_DEFAULTS supplies 'australia' automatically."""
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", ""):
            res = assess("perth", None, checkin="2027-01-10", checkout="2027-01-15")
        self.assertIsNotNone(res["advisory"])
        self.assertEqual(res["advisory"]["flag"], "bushfire_season")

    def test_assess_esperance_no_country_advisory_fires_feb(self):
        """assess('esperance', ..., Feb dates, no country) → advisory fires."""
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", ""):
            res = assess("esperance", None, checkin="2027-02-01", checkout="2027-02-05")
        self.assertIsNotNone(res["advisory"])
        self.assertEqual(res["advisory"]["severity"], "high")

    def test_assess_perth_no_country_sep_no_advisory(self):
        """assess('perth', ..., Sep dates, no country) → no advisory (out of season)."""
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", ""):
            res = assess("perth", None, checkin="2026-09-01", checkout="2026-09-10")
        self.assertIsNone(res["advisory"],
                          "Sep is outside the Dec-Feb window — no advisory expected")

    def test_explicit_country_overrides_default(self):
        """Explicit country='Scotland' for 'perth' → no WA advisory (different place)."""
        with patch("agents.destination_agent.DASHSCOPE_API_KEY", ""):
            res = assess("perth", None, checkin="2026-12-10", checkout="2026-12-20",
                         country="Scotland")
        self.assertIsNone(res["advisory"],
                          "Perth, Scotland has no seeded advisory; explicit country overrides default")


if __name__ == "__main__":
    unittest.main(verbosity=2)
